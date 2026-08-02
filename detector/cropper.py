"""Screen capture.

  win32_cap(yxhw)          one-shot grab of an arbitrary rect (dynamic regions)
  RegionGrabber(regions)   GDI grabber for a fixed region set
  DXGIGrabber(regions)     DXGI Desktop Duplication grabber, same interface
  make_grabber(regions)    DXGI if available, else GDI

Creating and destroying GDI objects costs ~6 ms regardless of how many
pixels are copied, so grabbing N regions one-by-one costs N × 6 ms.
RegionGrabber allocates its DCs and bitmaps once, groups the regions into
a few bounding boxes, and slices the crops out of those — one BitBlt per
box per frame instead of one per region. That lands at ~18 ms/frame.

DXGIGrabber instead receives frames the compositor already has in VRAM,
so it runs at the monitor refresh rate (~6.9 ms on a 144 Hz panel) and is
bounded by vsync rather than by copy cost. It needs the `bettercam`
package and can fail on exclusive-fullscreen or when another process holds
the duplication interface, hence make_grabber()'s fallback.
"""
import threading

import numpy as np
import win32con
import win32gui
import win32ui

_cap_lock = threading.Lock()

# Regions further apart than this vertically go into separate bounding
# boxes rather than one box spanning the empty space between them.
BAND_GAP = 200


def win32_cap(yxhw):
    """Grab one rect, (y, x, h, w) -> BGR array. Allocates and frees GDI
    objects on every call; use RegionGrabber for anything per-frame."""
    y, x, h, w = yxhw
    with _cap_lock:
        hwnd = 0
        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()
        saveBitMap = win32ui.CreateBitmap()
        saveBitMap.CreateCompatibleBitmap(mfcDC, w, h)
        saveDC.SelectObject(saveBitMap)
        saveDC.BitBlt((0, 0), (w, h), mfcDC, (x, y), win32con.SRCCOPY)

        bmpstr = saveBitMap.GetBitmapBits(True)
        im = np.frombuffer(bmpstr, dtype=np.uint8).reshape(h, w, 4)
        im = im[:, :, :3].copy()  # BGRA -> BGR

        win32gui.DeleteObject(saveBitMap.GetHandle())
        saveDC.DeleteDC()
        mfcDC.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwndDC)
        return im


class _Band:
    """One bounding box with its GDI objects held open, plus the regions
    that get sliced out of it."""

    def __init__(self, members):
        # members: [(name, (y, x, h, w)), ...]
        self.x = min(r[1] for _, r in members)
        self.y = min(r[0] for _, r in members)
        self.w = max(r[1] + r[3] for _, r in members) - self.x
        self.h = max(r[0] + r[2] for _, r in members) - self.y
        # Precompute slice offsets relative to the band origin
        self.members = [
            (name, r[0] - self.y, r[1] - self.x, r[2], r[3])
            for name, r in members
        ]

        self._hwndDC = win32gui.GetWindowDC(0)
        self._mfcDC = win32ui.CreateDCFromHandle(self._hwndDC)
        self._memDC = self._mfcDC.CreateCompatibleDC()
        self._bmp = win32ui.CreateBitmap()
        self._bmp.CreateCompatibleBitmap(self._mfcDC, self.w, self.h)
        self._memDC.SelectObject(self._bmp)
        self._closed = False

    def blit_into(self, out):
        """BitBlt once, then slice each region into `out`."""
        self._memDC.BitBlt((0, 0), (self.w, self.h), self._mfcDC,
                           (self.x, self.y), win32con.SRCCOPY)
        buf = self._bmp.GetBitmapBits(True)
        big = np.frombuffer(buf, dtype=np.uint8).reshape(self.h, self.w, 4)
        for name, dy, dx, h, w in self.members:
            # .copy() is required: `big` is backed by a buffer that the next
            # BitBlt overwrites, and crops outlive the frame in the ring buffer.
            out[name] = big[dy:dy + h, dx:dx + w, :3].copy()

    def close(self):
        if self._closed:
            return
        self._closed = True
        win32gui.DeleteObject(self._bmp.GetHandle())
        self._memDC.DeleteDC()
        self._mfcDC.DeleteDC()
        win32gui.ReleaseDC(0, self._hwndDC)


def _cluster(regions, gap=BAND_GAP):
    """Split regions into horizontal bands separated by more than `gap` px.

    HUD_REGIONS sits in two clusters (tab inventory near the top, gameplay
    HUD at the bottom); one box over both would copy the ~1000 empty rows
    between them every frame.
    """
    items = sorted(regions.items(), key=lambda kv: kv[1][0])
    bands, cur, cur_ymax = [], [], 0
    for name, r in items:
        if cur and r[0] - cur_ymax > gap:
            bands.append(cur)
            cur = []
            cur_ymax = 0
        cur.append((name, r))
        cur_ymax = max(cur_ymax, r[0] + r[2])
    if cur:
        bands.append(cur)
    return bands


class RegionGrabber:
    """Grabs a fixed set of named regions, one BitBlt per band per frame.

    Not thread-safe on its own; it shares the module capture lock with
    win32_cap so the two can be used from different threads.
    """

    def __init__(self, regions, gap=BAND_GAP):
        self.regions = regions
        self.bands = [_Band(m) for m in _cluster(regions, gap)]

    def grab(self):
        """Return {name: BGR array} for every region."""
        out = {}
        with _cap_lock:
            for band in self.bands:
                band.blit_into(out)
        return out

    def close(self):
        for band in self.bands:
            band.close()
        self.bands = []

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class DXGIGrabber:
    """Grabs a fixed set of named regions via DXGI Desktop Duplication.

    Same grab()/close() interface as RegionGrabber, but grab() blocks until
    the compositor produces the next frame, so the caller's loop is paced by
    the monitor refresh rate and must not add its own sleep.

    One bounding box covers every region: DXGI allows a single duplication
    interface per output, so banding would not help — and the copy happens
    on the GPU side, where the extra area is close to free.
    """

    def __init__(self, regions, output_idx=0, target_fps=0):
        import bettercam  # imported lazily; optional dependency

        self.regions = regions
        self.left = min(r[1] for r in regions.values())
        self.top = min(r[0] for r in regions.values())
        right = max(r[1] + r[3] for r in regions.values())
        bottom = max(r[0] + r[2] for r in regions.values())
        self.w = right - self.left
        self.h = bottom - self.top
        self._region = (self.left, self.top, right, bottom)

        # Slice offsets relative to the captured box
        self._members = [
            (name, r[0] - self.top, r[1] - self.left, r[2], r[3])
            for name, r in regions.items()
        ]

        # BGRA, not BGR: asking bettercam for BGR makes it drop the alpha
        # channel across the whole captured box every frame (2.65 Mpx here),
        # which measured 3x the CPU of doing it per-crop below.
        self._cam = bettercam.create(output_idx=output_idx,
                                     output_color="BGRA")
        # video_mode=True repeats the previous frame when the screen is idle,
        # so grab() cannot block indefinitely on a static screen.
        self._cam.start(region=self._region, target_fps=target_fps,
                        video_mode=True)
        self.target_fps = target_fps
        self._closed = False

    def grab(self):
        """Block until the next frame, then return {name: BGR array}."""
        img = self._cam.get_latest_frame()
        out = {}
        for name, dy, dx, h, w in self._members:
            # [:, :, :3] drops alpha on the crop only; .copy() because
            # bettercam reuses its frame buffer and crops outlive the frame.
            out[name] = img[dy:dy + h, dx:dx + w, :3].copy()
        return out

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._cam.stop()
        except Exception:
            pass
        try:
            self._cam.release()
        except Exception:
            pass
        self._cam = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def make_grabber(regions, prefer_dxgi=True, dxgi_fps=0):
    """Return (grabber, paced) for `regions`.

    `paced` is True when the grabber waits for the compositor's next frame,
    so the caller should not add its own sleep. It is still only a hint:
    DXGI in video_mode returns the previous frame immediately while the
    screen is idle, so a paced caller must tolerate running hot when
    nothing on screen changes.
    """
    if prefer_dxgi:
        try:
            return DXGIGrabber(regions, target_fps=dxgi_fps), True
        except Exception as e:
            print(f'[capture] DXGI unavailable ({e}); falling back to GDI',
                  flush=True)
    return RegionGrabber(regions), False
