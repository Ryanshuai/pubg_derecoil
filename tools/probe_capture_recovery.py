"""Does capture survive a display-mode change?

Reproduces the two ways the DXGI backend fails when a game enters exclusive
fullscreen, without needing a game:

  1. Access Lost. bettercam's _on_output_change() rebuilds its frame buffer
     from `self.region`, which it resets to the full screen unless the region
     was passed to create(). The capture thread keeps producing frames at the
     original region size, so the next buffer write raises
     "could not broadcast input array from shape (h,w,4) into shape (H,W,4)"
     and the thread dies.
  2. The aftermath. bettercam's error path calls stop() from inside the
     capture thread, which raises on join(self) before clearing its state and
     leaves `__frame_available` set once. get_latest_frame() then returns one
     stale frame and blocks forever after that — no exception, no log line,
     detectors quietly reading a frozen screen.

Every grab here runs under a watchdog, so a regression shows up as HUNG
rather than as a script that never returns.

    pixi run python tools/probe_capture_recovery.py

Reads the screen only; injects nothing and needs no game.
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from config import HUD_REGIONS
from capture.cropper import CaptureLost, DXGIGrabber

GRAB_TIMEOUT = 5.0
_failures = []


def _check(name, ok, detail=''):
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ''))
    if not ok:
        _failures.append(name)


def timed_grab(grabber):
    """grab() under a watchdog -> ('ok', crops) | ('raised', exc) | ('hung', None)."""
    box = {}

    def run():
        try:
            box['ok'] = grabber.grab()
        except Exception as e:  # noqa: BLE001 - reporting, not handling
            box['err'] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(GRAB_TIMEOUT)
    if t.is_alive():
        return 'hung', None
    if 'err' in box:
        return 'raised', box['err']
    return 'ok', box['ok']


def case_access_lost():
    """Force the Access Lost path and keep grabbing."""
    print("\n=== 1. Access Lost (display-mode change) ===")
    g = DXGIGrabber(HUD_REGIONS, target_fps=0)
    try:
        cam = g._cam
        status, _ = timed_grab(g)
        _check('grab before', status == 'ok', status)

        buf = getattr(cam, '_BetterCam__frame_buffer')
        want = (g.h, g.w)
        _check('buffer matches region before', tuple(buf.shape[1:3]) == want,
               f"{tuple(buf.shape[1:3])} vs {want}")

        # Make the capture thread take the Access Lost path itself, by having
        # update_frame() report a lost frame once. Calling _on_output_change()
        # from here instead would race the capture thread against a released
        # duplicator and crash on something the real code never hits: on a
        # real mode change it is _grab() — on the capture thread — that calls
        # it. _on_output_change() replaces the duplicator, so this patch
        # applies exactly once.
        dup = cam._duplicator
        orig_update = dup.update_frame
        seen = {'n': 0}

        def lose_once():
            seen['n'] += 1
            return False if seen['n'] == 1 else orig_update()

        dup.update_frame = lose_once

        deadline = time.time() + 10.0
        while cam._duplicator is dup and time.time() < deadline:
            time.sleep(0.05)
        _check('Access Lost path taken', cam._duplicator is not dup)

        buf = getattr(cam, '_BetterCam__frame_buffer')
        _check('buffer still matches region after Access Lost',
               tuple(buf.shape[1:3]) == want,
               f"{tuple(buf.shape[1:3])} vs {want}")

        ok = True
        for i in range(30):
            status, val = timed_grab(g)
            if status != 'ok':
                _check(f'grab after Access Lost (frame {i})', False,
                       f"{status}: {val}")
                ok = False
                break
        if ok:
            _check('30 grabs after Access Lost', True)
            crops = val
            _check('crops intact', set(crops) == set(HUD_REGIONS),
                   f"{len(crops)}/{len(HUD_REGIONS)} regions")
    finally:
        g.close()


def case_dead_thread():
    """Kill the capture thread; grab() must raise, not block."""
    print("\n=== 2. Dead capture thread ===")
    g = DXGIGrabber(HUD_REGIONS, target_fps=0)
    try:
        status, _ = timed_grab(g)
        _check('grab before', status == 'ok', status)

        cam = g._cam
        getattr(cam, '_BetterCam__stop_capture').set()
        deadline = time.time() + 5.0
        thread = getattr(cam, '_BetterCam__thread')
        while thread.is_alive() and time.time() < deadline:
            time.sleep(0.05)
        _check('capture thread stopped', not thread.is_alive())

        # Without the liveness check this is where it hangs forever.
        status, val = timed_grab(g)
        _check('grab raises CaptureLost instead of hanging',
               status == 'raised' and isinstance(val, CaptureLost),
               f"{status}: {val}")
    finally:
        g.close()


def case_loop_recovers():
    """End to end: ScreenCapture must keep filling its buffer after a kill."""
    print("\n=== 3. ScreenCapture rebuilds after a kill ===")
    from capture.screen_capture import ScreenCapture

    cap = ScreenCapture()
    cap.start()
    try:
        time.sleep(1.5)
        ts_before, _ = cap.latest()
        _check('buffer filling', ts_before is not None)
        _check('DXGI backend in use', cap.backend == 'DXGIGrabber', cap.backend)

        # The loop owns its grabber on its own thread and hands out no
        # reference, but bettercam's factory is a singleton keyed by output —
        # create() returns the very camera the loop is using. Its region
        # argument is ignored for an existing instance (bettercam prints a
        # notice saying so), which is exactly why this reaches the right one.
        import bettercam
        cam = bettercam.create(region=(0, 0, 1, 1), output_color="BGRA")
        getattr(cam, '_BetterCam__stop_capture').set()
        # Let go immediately: while anything holds the dead camera, the
        # factory's weak entry stays alive and every rebuild gets it back.
        del cam

        deadline = time.time() + 15.0
        recovered = False
        while time.time() < deadline:
            time.sleep(0.5)
            ts_now, _ = cap.latest()
            if ts_now is not None and ts_before is not None and ts_now > ts_before + 1.0:
                recovered = True
                break
        _check('buffer still advancing after the kill', recovered,
               f"backend now {cap.backend}, {cap.fps:.0f} fps")
        # The point of evicting the factory entry: recovery keeps DXGI rather
        # than limping along on GDI at a third of the rate.
        _check('recovered on DXGI, not GDI', cap.backend == 'DXGIGrabber',
               cap.backend)
    finally:
        cap.stop()
        time.sleep(0.5)


if __name__ == '__main__':
    case_access_lost()
    case_dead_thread()
    case_loop_recovers()
    print()
    if _failures:
        print(f"FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("all green")
