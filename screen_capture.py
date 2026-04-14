"""ScreenCapture — single-thread capture loop with ring buffer.

Continuously captures all HUD regions defined in config.HUD_REGIONS.
Each frame is timestamped and stored in a deque (last ~1 second).
Other threads read from the buffer, never call win32_cap directly.
"""
import time
import threading
from collections import deque

import numpy as np

from config import HUD_REGIONS
from detector.cropper import win32_cap


class ScreenCapture:
    """Capture thread: owns all GDI calls, stores timestamped frames."""

    def __init__(self, buffer_size=60):
        self._buffer = deque(maxlen=buffer_size)
        self._regions = HUD_REGIONS
        self._running = False
        self._thread = None

    # ── Thread lifecycle ──

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def join(self):
        if self._thread:
            self._thread.join()

    # ── Capture loop ──

    def _loop(self):
        while self._running:
            ts = time.perf_counter()
            frame = {}
            for name, rect in self._regions.items():
                frame[name] = win32_cap(rect)
            self._buffer.append((ts, frame))
            time.sleep(0.016)  # ~60 fps

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

    def get_crop(self, target_ts, region, max_age=0.5):
        """Convenience: find frame and return a single region's crop."""
        ts, frame = self.find_frame(target_ts, max_age)
        if frame is None:
            return None
        return frame.get(region)

    def get_crops(self, target_ts, regions, max_age=0.5):
        """Convenience: find frame and return multiple regions as dict."""
        ts, frame = self.find_frame(target_ts, max_age)
        if frame is None:
            return None
        return {r: frame[r] for r in regions if r in frame}
