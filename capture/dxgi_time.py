"""When a frame was actually PRESENTED, not when we got around to asking.

    from capture.dxgi_time import enable, present_s

    enable()                       # patches bettercam, idempotent
    frame = grabber.grab()
    t = present_s()                # seconds, same clock as time.perf_counter()

WHY THIS EXISTS
---------------
`AcquireNextFrame` fills a DXGI_OUTDUPL_FRAME_INFO whose `LastPresentTime` is
the QPC instant the compositor presented that frame. bettercam declares the
struct (`_libs/dxgi.py`), passes it by reference, and then never reads a single
field -- `info` is a local that dies three lines later.

What the measurement loop used instead was a `time.perf_counter()` taken BEFORE
`grab()`, i.e. before the frame it labels even existed. On the threaded path
grab() blocks on `__frame_available.wait()`, so that stamp precedes the frame's
content by the whole wait. The bias is one-directional (always early) and its
size is set by how long the loop's own per-frame work took on the PREVIOUS
iteration -- structured, not random.

MODEL.md's whole horizontal axis is "how long since the click". Everything --
the fit, the clustering, the anchor windows -- is placed on it. A timestamp
that is early by a variable amount is a horizontal error on every sample.

THREE THINGS THAT CAN GO WRONG, AND WHAT EACH LOOKS LIKE
--------------------------------------------------------
1. WAIT_TIMEOUT. No new frame within the timeout. bettercam sets `updated`
   False and `_grab` returns None. `LastPresentTime` is then stale, not
   merely absent -- it still holds the PREVIOUS frame's value, so a caller
   that reads it anyway gets a plausible number for a frame it never got.
   `present_s()` returns None in that case, and `stats()['stale']` counts it.

2. LastPresentTime == 0. Windows documents this for an acquire whose only
   change was the mouse pointer: there is a frame, but no new CONTENT was
   presented. Zero is not a time; treating it as one puts the sample at the
   epoch. Counted as `zero` and reported as None.

3. The clock. `time.perf_counter()` on Windows is QueryPerformanceCounter
   divided by its frequency, so the two are the same clock -- measured on this
   machine at a 1 us agreement, which is the cost of the two calls. That is
   not promised by the language, so the offset is MEASURED once at import and
   applied, rather than assumed to be zero.

⚠ THE PATCH IS OURS AND LIVES HERE, not in .pixi. An edit inside
   .pixi/envs/default/ is erased by the next `pixi install` and exists on
   exactly one machine, and the failure would be silent: capture keeps working,
   the timestamps quietly go back to being wrong.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

# How long AcquireNextFrame waits for a new frame. bettercam hardcodes 0, which
# is right for its spin thread and wrong for a synchronous caller: at 0 the loop
# burns a core polling. This default keeps bettercam's behaviour so enabling the
# timestamp changes nothing on its own; the synchronous grabber raises it.
TIMEOUT_MS = 0

_freq = ctypes.c_int64()
ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(_freq))
QPC_HZ = float(_freq.value)

# QPC -> perf_counter offset, measured rather than assumed (see note 3).
# Sampled a few times and the median taken so a scheduling hiccup between the
# two calls does not become a permanent bias.
def _measure_offset(n=9):
    c = ctypes.c_int64()
    d = []
    for _ in range(n):
        ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(c))
        p = time.perf_counter()
        d.append(p - c.value / QPC_HZ)
    d.sort()
    return d[len(d) // 2]


QPC_TO_PERF = _measure_offset()

_state = {
    'enabled': False,
    'qpc': 0,           # LastPresentTime of the most recent acquire
    'ok': False,        # ...and whether that acquire actually produced a frame
    'accumulated': 0,   # frames the compositor presented since our last acquire
    'n': 0,
    'stale': 0,         # acquires that timed out (no new frame)
    'zero': 0,          # frames whose LastPresentTime was 0 (pointer-only)
}


def enable():
    """Patch bettercam's Duplicator to keep the frame info. Idempotent."""
    if _state['enabled']:
        return True
    import comtypes
    from bettercam._libs.dxgi import (DXGI_ERROR_ACCESS_LOST,
                                      DXGI_ERROR_WAIT_TIMEOUT,
                                      DXGI_OUTDUPL_FRAME_INFO, IDXGIResource)
    from bettercam.core.duplicator import Duplicator

    def update_frame(self):
        info = DXGI_OUTDUPL_FRAME_INFO()
        res = ctypes.POINTER(IDXGIResource)()
        try:
            self.duplicator.AcquireNextFrame(
                TIMEOUT_MS,
                ctypes.byref(info),
                ctypes.byref(res),
            )
        except comtypes.COMError as ce:
            if ctypes.c_int32(DXGI_ERROR_ACCESS_LOST).value == ce.args[0]:
                _state['ok'] = False
                return False
            if ctypes.c_int32(DXGI_ERROR_WAIT_TIMEOUT).value == ce.args[0]:
                self.updated = False
                # ⚠ NOT just "no timestamp": the previous frame's value is
                # still sitting in _state['qpc'] and would read as a perfectly
                # ordinary time for a frame that was never handed over.
                _state['ok'] = False
                _state['stale'] += 1
                return True
            raise
        try:
            self.texture = res.QueryInterface(
                __import__('bettercam._libs.d3d11', fromlist=['ID3D11Texture2D'])
                .ID3D11Texture2D)
        except comtypes.COMError:
            self.duplicator.ReleaseFrame()
        self.updated = True
        qpc = int(info.LastPresentTime)
        _state['accumulated'] = int(info.AccumulatedFrames)
        _state['n'] += 1
        if qpc == 0:
            _state['ok'] = False
            _state['zero'] += 1
        else:
            _state['qpc'] = qpc
            _state['ok'] = True
        return True

    Duplicator.update_frame = update_frame
    _state['enabled'] = True
    return True


def present_s():
    """Present time of the frame the last grab returned, in perf_counter
    seconds. None when that grab produced no frame, or produced one the
    compositor did not stamp -- see the three cases in the module docstring.

    ⚠ None is not "use the old one". A caller that falls back to the previous
    value is re-labelling a frame with another frame's time, which is the exact
    error this module exists to remove.
    """
    if not _state['ok']:
        return None
    return _state['qpc'] / QPC_HZ + QPC_TO_PERF


def accumulated():
    """Frames the compositor presented since our previous acquire.

    1 means we kept up. Greater than 1 means frames were composed and thrown
    away before we asked -- the sampling rate is below the refresh rate and
    the gap is real, not jitter. 0 happens on a pointer-only update.
    """
    return _state['accumulated']


def stats():
    return dict(_state)


def reset_stats():
    _state.update(n=0, stale=0, zero=0)
