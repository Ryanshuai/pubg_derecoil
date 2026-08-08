"""Screen capture.

  win32_cap(yxhw)          one-shot grab of an arbitrary rect (dynamic regions)
  RegionGrabber(regions)   GDI grabber for a fixed region set
  DXGIGrabber(regions)     DXGI Desktop Duplication grabber, same interface
  make_grabber(regions)    DXGI if available, else GDI
  StillGrabber(regions, imgs)  the same interface over stored PNGs, offline

  ScreenBuffer(regions)    a grabber plus the two things every caller of one
                           was writing for itself: a reused screen-coordinate
                           buffer the crops get blitted back into, and the
                           flush-N-then-read idiom
  anchor_box(...)          bounding box over a set of icon anchors

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


class CaptureLost(RuntimeError):
    """The backend stopped producing frames and cannot be grabbed from again.

    Raised instead of blocking so the caller can rebuild the grabber; see
    DXGIGrabber.grab().
    """


def capture_screen():
    """Whole primary screen as BGR. For the UI screens, where a one-shot grab
    of everything beats naming regions up front."""
    from config import SCREEN_H, SCREEN_W
    return win32_cap((0, 0, SCREEN_H, SCREEN_W))


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
        #
        # The region goes to create(), not only to start(). bettercam sets
        # `_region_set_by_user` from create()'s argument alone, and on a
        # display-mode change (Access Lost — which is what a game entering
        # exclusive fullscreen looks like) _on_output_change() resets a region
        # it believes was never set back to the full screen and rebuilds the
        # frame buffer at THAT size. The capture thread meanwhile keeps
        # producing frames at the original region size, so the next write dies
        # with a broadcast error and takes the capture thread down with it.
        self._cam = bettercam.create(output_idx=output_idx,
                                     region=self._region,
                                     output_color="BGRA")
        # video_mode=True repeats the previous frame when the screen is idle,
        # so grab() cannot block indefinitely on a static screen.
        self._cam.start(region=self._region, target_fps=target_fps,
                        video_mode=True)
        self.target_fps = target_fps
        self._closed = False

    def _alive(self):
        """Is bettercam's capture thread still running?

        `is_capturing` cannot answer this: when the capture thread hits an
        error it calls stop() on itself, which raises on `join(self)` before
        clearing the flag. The thread object can.
        """
        t = getattr(self._cam, '_BetterCam__thread', None)
        return t is not None and t.is_alive()

    def grab(self):
        """Block until the next frame, then return {name: BGR array}."""
        # Checked before waiting, not after: a dead capture thread leaves
        # `__frame_available` set exactly once, so the first get_latest_frame()
        # returns a stale frame and every later one blocks forever on an event
        # nobody will set again. Silent, and everything downstream just keeps
        # reading the same frame.
        if not self._alive():
            raise CaptureLost('bettercam capture thread is gone')
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
        cam, self._cam = self._cam, None
        try:
            cam.stop()
        except Exception:
            pass
        try:
            cam.release()
        except Exception:
            pass

        # bettercam's factory keeps one weak entry per (device, output) and
        # hands the SAME camera back to the next create() for as long as that
        # entry is alive. A camera whose capture thread has died is unusable,
        # so rebuilding after a loss would get the broken one straight back —
        # measured: three rebuild attempts in a row, all instantly dead, then
        # a permanent fall back to GDI at a third of the frame rate.
        #
        # Dropping the entry needs the strong reference gone too, and the
        # camera holds cycles that refcounting alone will not break, hence the
        # collect. tab_items uses the GDI grabber, so no other live user can
        # be evicted here.
        try:
            import gc

            import bettercam
            for key, inst in list(bettercam.DXFactory._camera_instances.items()):
                if inst is cam:
                    del bettercam.DXFactory._camera_instances[key]
            del cam, inst
            gc.collect()
        except Exception:
            pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class DXGISyncGrabber:
    """DXGI with no capture thread, and every frame carrying the instant the
    compositor PRESENTED it.

        g = DXGISyncGrabber(tracker.regions())
        t, crops = g.grab_timed()      # (None, None) when no new frame yet

    WHY A SECOND DXGI GRABBER
    -------------------------
    DXGIGrabber above runs bettercam's capture thread, and that thread does two
    things this measurement cannot have:

      - `video_mode=True` COPIES THE PREVIOUS FRAME when the compositor has
        produced nothing, and signals it as new. Measured on a static desktop:
        3 seconds, 9 frames actually presented, and the threaded path would
        have handed over roughly 500. MagazineRecorder catches those by
        comparing pixels, but that is a repair downstream of a fabrication.
      - The frame is stamped by the CALLER, before a blocking wait for a frame
        that does not exist yet. See capture/dxgi_time.

    The synchronous path has neither problem by construction: `_grab` returns
    None when nothing new was presented, and `LastPresentTime` says exactly
    when the frame that WAS returned came from.

    ⚠ ONE CAMERA PER OUTPUT, PROCESS-WIDE. bettercam's factory hands the same
    object to every create() for a given (device, output), so a ScreenBuffer on
    the threaded path and one of these cannot coexist in one process -- the
    second start()/grab() drives the same camera and the first one's frames
    stop making sense. The rig picks one.

    ⚠ AND grab_timed() RETURNS (None, None) OFTEN. That is not an error, it is
    the honest answer "the screen has not changed since you last asked". A
    caller that treats it as a failure will conclude the capture is broken
    while looking at a still scene.
    """

    def __init__(self, regions, output_idx=0, timeout_ms=8):
        import bettercam

        from capture import dxgi_time
        dxgi_time.enable()
        # 0 would spin: measured 33 000 polls a second against 66 at 8 ms.
        # It is module-global because the patch is, and the note above says why
        # only one grabber may be live anyway.
        dxgi_time.TIMEOUT_MS = timeout_ms
        self._dt = dxgi_time

        self.regions = regions
        self.left = min(r[1] for r in regions.values())
        self.top = min(r[0] for r in regions.values())
        right = max(r[1] + r[3] for r in regions.values())
        bottom = max(r[0] + r[2] for r in regions.values())
        self._region = (self.left, self.top, right, bottom)
        self._members = [
            (name, r[0] - self.top, r[1] - self.left, r[2], r[3])
            for name, r in regions.items()
        ]
        self._cam = bettercam.create(output_idx=output_idx,
                                     region=self._region,
                                     output_color="BGRA")
        self._closed = False
        # Frames the compositor made while we were busy. Not an error and not
        # jitter: it is the sampling rate falling below the refresh rate, and
        # it is the only way to know that from inside.
        self.n_missed = 0
        self.n_frames = 0

    def grab_timed(self):
        """(present_time_s, {name: BGR}) or (None, None) if nothing new."""
        img = self._cam.grab()
        if img is None:
            return None, None
        t = self._dt.present_s()
        if t is None:
            # A frame with no usable stamp is not a frame we can place on the
            # time axis, and MODEL.md's axis is the whole measurement. Dropping
            # it costs one sample; keeping it with a guessed time puts a real
            # displacement at a wrong instant, which is worse and invisible.
            return None, None
        self.n_frames += 1
        acc = self._dt.accumulated()
        if acc > 1:
            self.n_missed += acc - 1
        out = {}
        for name, dy, dx, h, w in self._members:
            out[name] = img[dy:dy + h, dx:dx + w, :3].copy()
        return t, out

    def grab(self):
        """Blocking, for callers that only want pixels. Loops until a frame."""
        while True:
            t, out = self.grab_timed()
            if out is not None:
                return out

    def close(self):
        if self._closed:
            return
        self._closed = True
        cam, self._cam = self._cam, None
        try:
            cam.release()
        except Exception:
            pass
        # Same factory eviction as DXGIGrabber.close -- see the long note
        # there. Without it the next create() gets this camera back.
        try:
            import gc

            import bettercam
            for key, inst in list(bettercam.DXFactory._camera_instances.items()):
                if inst is cam:
                    del bettercam.DXFactory._camera_instances[key]
            del cam, inst
            gc.collect()
        except Exception:
            pass

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


class StillGrabber:
    """A grabber over stored screenshots. Same grab()/close() as the real ones.

    `images` are FULL-SCREEN frames — the regions are sliced out of them by
    screen coordinates, which is the whole point: a stored PNG and the live
    desktop are then the same coordinate system, so anything built on a
    ScreenBuffer can be exercised offline without a game.

    One image is consumed per grab() and the last one repeats forever. That is
    also what DXGI does on an idle screen (video_mode re-serves the previous
    frame), so a flush count can be checked against a known sequence.
    """

    def __init__(self, regions, images):
        if isinstance(images, np.ndarray):
            images = [images]
        self.regions = dict(regions)
        self.images = list(images)
        if not self.images:
            raise ValueError('StillGrabber needs at least one image')
        self.n = 0

    def grab(self):
        img = self.images[min(self.n, len(self.images) - 1)]
        self.n += 1
        # .copy() for the same reason the live grabbers do it: callers keep
        # crops past the frame, and here the source array is shared with every
        # other region and every later grab.
        return {name: img[y:y + h, x:x + w].copy()
                for name, (y, x, h, w) in self.regions.items()}

    def close(self):
        self.images = []


class FocusLost(RuntimeError):
    """The game stopped being the foreground window, so the pixels handed back
    are not the game's.

    Distinct from CaptureLost: the backend is fine and grabbing again would
    succeed — it would just keep returning the desktop. A run that does not
    stop here completes and labels a directory of identical screenshots as
    data. See ScreenBuffer's `focus_fn`.
    """


# How many frames to drop before a read that has to reflect something that
# just happened. Shared knowledge rather than a number each caller remembers:
# DXGI in video_mode re-serves the PREVIOUS frame while the screen is idle, so
# the first grab after an action can predate it. GDI reads the desktop as it is
# now and strictly needs none, but the game's own render latency means a caller
# wanting "after the click" wants a couple either way — the existing call sites
# used 8 (sweep.Rig.flush), 3 (collect_templates.Collector.frame,
# calibration/state.Probe.read) and 1/2/4/6 at various points in sweep.
FLUSH_FRAMES = 3


def anchor_box(anchors, icon_w, icon_h, search=0):
    """One box covering every icon anchor, widened by the search margin.

    `anchors` are (x, y) top-left points — the order the SPAWNER_ICON_ANCHORS
    table in config.py uses — and the result is (y, x, h, w) like every other
    region here.

    Transcribed verbatim, quirk included, from the two copies it replaces
    (one in the recoil sweep, since deleted, and calibration/state.py's
    Probe.__init__ — the two were character-for-character identical). The quirk:
    the origin is clamped at 0 but the height and width are not reduced to
    match, so an anchor set within `search` of the top or left edge yields a
    box that reaches `search` px further than the margin asks for. Kept
    deliberately — this function exists so the two call sites can migrate onto
    it without their boxes moving by a pixel, and tools/test_frames.py asserts
    exactly that against the live source of both files.
    """
    xs = [a[0] for a in anchors]
    ys = [a[1] for a in anchors]
    s = search
    return (max(0, min(ys) - s), max(0, min(xs) - s),
            max(ys) + icon_h + 2 * s - min(ys),
            max(xs) + icon_w + 2 * s - min(xs))


class ScreenBuffer:
    """A grabber for a region set, plus a screen-coordinate view of it.

        sb = ScreenBuffer({'ammo': (1318, 1670, 48, 90)})
        f = sb.grab()               # {name: BGR crop}
        img = sb.full(f)            # crops blitted back where they came from
        f = sb.flush(3)             # drop stale frames, return the last

    WHY full() EXISTS. Half the detectors in this package index SCREEN
    coordinates, not crop coordinates — AdsDetector cuts a window out of the
    frame's own centre, SpawnerDetector looks at fixed anchors. Hand one a
    crop and it reads the middle of the crop, which is somewhere else on the
    screen entirely, and it does not complain: it returns a confident answer
    about the wrong pixels. So the crops go back into a full-size buffer at
    their own coordinates and the detector is handed that.

    The buffer is allocated ONCE and reused. It is 3440x1440x3 = 14.9 MB, and
    the three places that hand-rolled this ran it inside a per-frame loop.
    Everything outside the region set stays black forever, which is fine
    precisely because these detectors only look inside their own windows.

      *** The array full() returns is the buffer itself, valid only until the
          next grab(). Pass copy=True to keep it. ***

    FOCUS. `focus_fn` is an optional predicate — pass control.focus.game_focused
    — and when it answers False, grab() raises FocusLost instead of handing
    back a frame. This is not paranoia: when PUBG loses the foreground the
    picture freezes, so a calibration run keeps grabbing happily and writes a
    whole set of identical desktop screenshots under data filenames. Nothing
    downstream can tell. calibration/capture_ads.py already guards its own
    grab() this way and is the reason the option is here.

    It is OFF by default, and the reason is layering, not cost: detector/ may
    not import control/ (tools/check_layering.py enforces it), so this class
    cannot reach game_focused() on its own — the caller who lives above the
    line has to hand it down. Cost is not the argument; game_focused() measures
    0.008 ms, which is 0.1% of a 144 Hz frame budget. So:

        calibration / capture tools   pass focus_fn — a lost foreground
                                      silently voids the entire run
        the per-frame match loop      may leave it off — control/focus.py's
                                      FocusKeeper is already watching, and the
                                      loop's own detectors notice a frozen HUD

    If registering it per construction turns out to be forgettable, the right
    fix is a five-line factory in control/ that wires game_focused in, not a
    default here that detector/ cannot express.
    """

    def __init__(self, regions=None, *, focus_fn=None, prefer_dxgi=False,
                 dxgi_fps=0, gap=BAND_GAP, grabber=None):
        """`regions` is {name: (y, x, h, w)}; None means the whole screen.

        `grabber` injects a ready-made one (StillGrabber for offline work) and
        skips the backend choice. Otherwise GDI unless `prefer_dxgi`, matching
        what the callers being replaced each chose for themselves.
        """
        from config import SCREEN_H, SCREEN_W
        self.screen_h, self.screen_w = SCREEN_H, SCREEN_W
        if regions is None:
            regions = {'screen': (0, 0, SCREEN_H, SCREEN_W)}
        self.regions = dict(regions)
        self.focus_fn = focus_fn
        self._buf = None
        self._closed = False
        self._opts = dict(prefer_dxgi=prefer_dxgi, dxgi_fps=dxgi_fps, gap=gap)
        if grabber is not None:
            self.grabber, self.paced = grabber, False
        else:
            self.grabber, self.paced = self._open(self.regions)
        self._whole = self._whole_name()

    @classmethod
    def over_stills(cls, regions, images, **kw):
        """A ScreenBuffer reading stored full-screen frames. No game, no GDI."""
        if regions is None:
            from config import SCREEN_H, SCREEN_W
            regions = {'screen': (0, 0, SCREEN_H, SCREEN_W)}
        return cls(regions, grabber=StillGrabber(regions, images), **kw)

    def _open(self, regions):
        if self._opts['prefer_dxgi']:
            return make_grabber(regions, dxgi_fps=self._opts['dxgi_fps'])
        return RegionGrabber(regions, self._opts['gap']), False

    def _whole_name(self):
        """The region name that IS the whole screen, when there is only one.

        full() then hands that crop straight back instead of blitting 14.9 MB
        into a buffer to get the same picture. It also makes the array a fresh
        one per frame, which callers that keep frames around (capture_ads holds
        a hip frame to diff a later one against) depend on.
        """
        if len(self.regions) != 1:
            return None
        name, r = next(iter(self.regions.items()))
        return name if tuple(r) == (0, 0, self.screen_h, self.screen_w) else None

    # ── reads ──

    def grab(self):
        """{name: BGR crop}. Raises FocusLost when the guard says the game is
        not frontmost, and CaptureLost when the backend has died."""
        if self.focus_fn is not None and not self.focus_fn():
            raise FocusLost('the game is no longer the foreground window; '
                            'the screen is frozen and these pixels are stale')
        return self.grabber.grab()

    def flush(self, n=None):
        """Drop `n` frames and return the last one (None when n <= 0).

        `n` defaults to FLUSH_FRAMES. Note the return value: the old idiom was
        `for _ in range(3): f = grabber.grab()`, so `f = sb.flush(3)` is one
        line rather than a flush followed by a grab. A caller that wants the
        old `rig.flush(2)` + `rig.grab()` shape wants flush(3).
        """
        out = None
        for _ in range(FLUSH_FRAMES if n is None else n):
            out = self.grab()
        return out

    def full(self, frame=None, only=None, copy=False):
        """The frame's crops, blitted back to their screen coordinates.

        `frame` defaults to a fresh grab(). `only` restricts the blit to a
        subset of the region names, for a detector that reads one window and
        does not care that the rest of the buffer is a frame or two old.

        A region whose crop is MISSING from `frame` is zeroed rather than left
        alone. The hand-rolled version in calibration/state.py skipped it, and
        a skipped region keeps the previous frame's pixels — a detector then
        reads a stale answer with nothing to say it did. Black is at least
        wrong in a way that shows.

        REGIONS MAY OVERLAP, and two of the standing ones do: HUD_REGIONS's
        'ammo' (1318, 1670, 48, 90) and 'fire_mode' (1317, 1626, 43, 56) share
        a 12x43 px corner. With every crop present that is harmless, since both
        come from the same frame and write the same pixels. It only shows when
        one of them is missing: the zeroing above blanks the region, and then
        whichever overlapping region is blitted after it paints part of it
        back. Iteration order is the region dict's, i.e. the caller's.
        """
        if frame is None:
            frame = self.grab()
        if self._whole is not None and only is None:
            img = frame[self._whole]
            return img.copy() if copy else img
        buf = self._ensure_buf()
        for name in (self.regions if only is None else only):
            y, x, h, w = self.regions[name]
            crop = frame.get(name)
            buf[y:y + h, x:x + w] = 0 if crop is None else crop
        return buf.copy() if copy else buf

    def box(self, name):
        return self.regions[name]

    def _ensure_buf(self):
        if self._buf is None:
            for name, (y, x, h, w) in self.regions.items():
                if y < 0 or x < 0 or y + h > self.screen_h \
                        or x + w > self.screen_w:
                    raise ValueError(
                        f"region {name!r} = {(y, x, h, w)} does not fit the "
                        f"{self.screen_w}x{self.screen_h} screen, so it cannot "
                        f"be blitted back into one")
            self._buf = np.zeros((self.screen_h, self.screen_w, 3), np.uint8)
        return self._buf

    # ── lifecycle ──

    def set_regions(self, regions, grabber=None):
        """Swap the region set, keeping the buffer.

        calibration/sweep.py rebuilds its grabber whenever the sight changes,
        because each optic hides a different part of the screen and the view
        tracker's patch columns move with it. The buffer is wiped rather than
        carried over: the old regions' pixels would otherwise sit there for the
        rest of the run, in coordinates nothing writes to any more.

        THE OLD GRABBER IS CLOSED FIRST, and with DXGI it cannot be otherwise.
        bettercam's factory keeps one camera per (device, output) and hands the
        SAME object back to the next create(), so opening before closing does
        not produce two cameras — it produces one camera started twice, and
        then `old.close()` stops and releases the very camera the new grabber
        is now holding. The next grab() finds a dead capture thread.

        Measured 2026-08-03: a posture sweep reached the VSS, set_sight
        switched to vss_pso1 (three patch columns instead of seven, so the
        bounding box moves), and the run died on the first frame after —

            You already created a BetterCam Instance for Device 0--Output 0!
            Screen Capture FPS: 14273028      <- two start()s, one camera
            Screen Capture FPS: 4631615
            CaptureLost: bettercam capture thread is gone

        — taking the whole run with it, because CaptureLost propagates out of
        harvest_weapon. Closing first costs a few frames of blindness between
        the two backends and nothing else: _open() falls back to GDI on its
        own if the reopen fails, so there is no path where this leaves the
        Cropper without a grabber.
        """
        old, self.grabber = self.grabber, None
        try:
            old.close()
        except Exception:
            pass
        if grabber is not None:
            self.grabber, self.paced = grabber, False
        else:
            self.grabber, self.paced = self._open(dict(regions))
        self.regions = dict(regions)
        self._whole = self._whole_name()
        if self._buf is not None:
            self._buf[:] = 0
        return self

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.grabber.close()
        except Exception:
            pass
        self._buf = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
