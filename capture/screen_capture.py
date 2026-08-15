"""ScreenCapture — single-thread capture loop with ring buffer.

Continuously captures all HUD regions defined in config.HUD_REGIONS.
Each frame is timestamped and stored in a deque covering ~1 second.
Other threads read from the buffer, never call the grabber directly.

The backend is DXGI Desktop Duplication when available, otherwise a banded
GDI grabber. See capture.cropper.

Measured against the live game on a 16-core machine:

    backend             fps    cpu    frame-match p95
    GDI banded         47.9    40%         10.12 ms
    DXGI dxgi_fps=60   61.5    61%          8.12 ms
    DXGI dxgi_fps=120 120.3   133%          4.87 ms
    DXGI unthrottled  137.4   163%          4.07 ms

frame-match is how far find_frame() lands from a requested timestamp, which
is what DETECT_TABLE's delay values ride on. dxgi_fps trades CPU against
that precision almost linearly; lower it if the game needs the cores back.
"""
import time
from daemon_loop import DaemonLoop
from logbook import note
from collections import deque

from config import FRAME_REGIONS, HUD_REGIONS
from capture.cropper import make_grabber


class ScreenCapture(DaemonLoop):
    """Capture thread: owns all capture calls, stores timestamped frames."""

    # How many times a lost DXGI backend is rebuilt as DXGI before the loop
    # settles for GDI. Repeated losses mean the compositor is not giving the
    # duplication interface back (exclusive fullscreen), and GDI at 48 fps
    # beats a backend that keeps dying.
    MAX_DXGI_RETRIES = 2

    def __init__(self, buffer_seconds=1.0, target_fps=144, prefer_dxgi=True,
                 dxgi_fps=120):
        # Sized by duration, not frame count, so the span does not change with
        # the achieved frame rate.
        #
        # It used to need the span: DETECT_TABLE read frames from BEFORE the
        # key event (delay: -50) and the buffer had to still be holding them.
        # Those entries are gone (control/tab_watch.py), and every remaining
        # delay is positive, which means the frame wanted is the newest one at
        # the moment it is asked for. So this is now oversized rather than
        # load-bearing — left alone because a deep buffer is also what lets a
        # detection survive a scheduling hiccup, and at FRAME_REGIONS' size it
        # costs ~14 MB instead of the ~41 MB it did before the split.
        self._buffer = deque(maxlen=max(8, int(buffer_seconds * target_fps)))
        # FRAME_REGIONS, not HUD_REGIONS: the Tab screen's regions are read on
        # demand by control/tab_watch.py. Grabbing them here cost 5.46 ms of
        # every frame for a panel that is usually not on screen — see the
        # comment on FRAME_REGIONS in config.py.
        self._regions = {k: HUD_REGIONS[k] for k in FRAME_REGIONS}
        self._interval = 1.0 / target_fps
        self._prefer_dxgi = prefer_dxgi
        self._dxgi_fps = dxgi_fps
        # Measured loop stats, updated once per second by the capture thread
        self.fps = 0.0
        self.grab_ms = 0.0
        self.backend = None

    # ── Capture loop ──

    def _loop(self):
        # The grabber is created and destroyed on this thread so its GDI or
        # D3D resources are never touched concurrently by the thread calling
        # stop().
        grabber, paced = make_grabber(self._regions, self._prefer_dxgi,
                                      self._dxgi_fps)
        self.backend = type(grabber).__name__
        # 文件。**下面那两条不是** —— 后端掉了、帧率塌了，都是「本该发生的事
        # 没发生」，而这一条只是「它按预期起来了」。
        note(f'[capture] backend={self.backend} '
             f'buffer={self._buffer.maxlen} frames')

        frames = 0
        grab_total = 0.0
        window_start = time.perf_counter()
        reported = None   # becomes the first measured fps, used as baseline
        last_warn = 0.0
        failures = 0
        try:
            while self._running:
                ts = time.perf_counter()
                try:
                    frame = grabber.grab()
                except Exception as e:
                    # A display-mode change (game entering exclusive
                    # fullscreen, resolution switch) can take the DXGI capture
                    # thread down. Rebuild instead of letting the loop die:
                    # a dead capture loop is silent, and every detector
                    # downstream would go on reading the last frame it got.
                    failures += 1
                    retry_dxgi = (self._prefer_dxgi
                                  and failures <= self.MAX_DXGI_RETRIES)
                    print(f'[capture] {self.backend} lost ({e}); rebuilding'
                          f'{"" if retry_dxgi else " on GDI"}', flush=True)
                    try:
                        grabber.close()
                    except Exception:
                        pass
                    grabber, paced = make_grabber(self._regions, retry_dxgi,
                                                  self._dxgi_fps)
                    self.backend = type(grabber).__name__
                    # The new backend runs at its own rate, so the old
                    # baseline would fire the slow-capture warning forever.
                    reported = None
                    frames, grab_total = 0, 0.0
                    window_start = time.perf_counter()
                    continue

                self._buffer.append((ts, frame))

                elapsed = time.perf_counter() - ts
                frames += 1
                grab_total += elapsed

                if ts - window_start >= 1.0:
                    self.fps = frames / (ts - window_start)
                    self.grab_ms = grab_total / frames * 1000
                    frames, grab_total = 0, 0.0
                    window_start = ts

                    # Report the real rate once, then stay quiet unless it
                    # falls well below that baseline — a silent capture loop
                    # running at a fraction of the assumed rate is exactly
                    # the bug this replaced. The baseline is what this
                    # machine actually achieves, not a theoretical target,
                    # so it works for either backend.
                    span = len(self._buffer) / max(self.fps, 1e-6)
                    if not reported:
                        reported = self.fps
                        note(f'[capture] {self.fps:.0f} fps, '
                             f'{self.grab_ms:.1f} ms/frame, buffer spans '
                             f'{span:.2f}s')
                    elif self.fps < reported * 0.6 and ts - last_warn > 10.0:
                        last_warn = ts
                        print(f'[capture] rate dropped to {self.fps:.0f} fps '
                              f'(baseline {reported:.0f}), '
                              f'{self.grab_ms:.1f} ms/frame, buffer spans '
                              f'{span:.2f}s', flush=True)

                # A paced grabber already blocks until the next monitor frame;
                # sleeping on top of that would just drop frames.
                if not paced:
                    time.sleep(max(0.0, self._interval - elapsed))
        finally:
            grabber.close()

    # ── Read API (called by Dispatcher) ──

    def find_frame(self, target_ts, max_age=0.5):
        """Find the frame closest to target_ts.

        Returns (ts, frame_dict) or (None, None) if no frame within max_age.
        """
        best_ts, best_frame = None, None
        best_diff = float('inf')
        for ts, frame in list(self._buffer):
            diff = abs(ts - target_ts)
            if diff < best_diff:
                best_diff = diff
                best_ts = ts
                best_frame = frame
        if best_ts is not None and best_diff <= max_age:
            return best_ts, best_frame
        return None, None

    def latest(self):
        """Return the most recent (ts, frame_dict) or (None, None)."""
        if self._buffer:
            return self._buffer[-1]
        return None, None

    def get_crops(self, target_ts, regions, max_age=0.5):
        """Convenience: find frame and return multiple regions as dict."""
        ts, frame = self.find_frame(target_ts, max_age)
        if frame is None:
            return None
        return {r: frame[r] for r in regions if r in frame}
