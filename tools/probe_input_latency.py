"""How long from a mouse command to the view actually moving on screen.

    pixi run python tools/probe_input_latency.py --trials 20

This is the missing constant in the compensation's timing. Two delays matter
and only one of them was ever measured:

    L  command -> visible.  Pico USB report, the game sampling input, render,
       present, and DXGI making the frame available. Everything the
       compensation has to travel through.

    S  click -> first round visible. Measured by harvest as shot_delay_ms and
       running about 72 ms on the AUG. Same chain as L, PLUS the weapon's own
       delay between the trigger registering and the round leaving.

⚠ THE PARAGRAPH THAT STOOD HERE WAS THE BIN COORDINATE'S ANSWER, and under
MODEL.md it is the wrong question. It reasoned that the weapon's fire delay is
S - L and that the compensation should therefore be LATE by that much, because
the curve was indexed by ROUND and each pulse had to be aligned to the round it
cancelled. (It also argued against a RECOIL_LEAD_FRAC that no longer exists.)

In the time coordinate the curve IS y_true(t), the screen's displacement at
time t after the click, MEASURED ON THE SCREEN. The recoil's own path to the
photons is therefore already inside the curve and cancels. What does not cancel
is the compensation's own path, which the recoil never travels -- and that is
exactly L. So:

    the offset is  -L.  A LEAD, not a lag, and S does not enter at all.

Confirmed 2026-08-08 by firing it: over 25 magazines swept per-magazine across
five offsets off ONE fitted curve, the residual minimises near -L, and
config.RECOIL_FIRE_DELAY_MS moved off +13 on the strength of the two agreeing.

⚠ IT LANDED AT -19, NOT THE -46 THIS PARAGRAPH USED TO NAME. A later sweep read
per-magazine RMS at four offsets -- -50 -> 15.9, -36 -> 13.5, -19 -> 6.8,
-5 -> 7.7 -- and -19 is where the RMS optimum and -M intersect. MODEL.md sec.3
carries it as D = -19 ms, flagged because -19 and -5 are not separable. Do not
quote -46 from here: it was one arm of one sweep and it is not the stored value.

Nothing here touches the game beyond moving the view a little, and it does not
need a weapon in hand.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))

import numpy as np

from control.session import ensure_ready
from calibration.sweep import Rig

MOVE = 250          # counts per probe; big enough to clear the noise floor
WATCH_S = 0.40      # how long to keep looking after the command
MOVED_PX = 2.0      # per-frame displacement that counts as "it started"
SETTLE_S = 0.35     # let the view stop between trials


def one_trial(rig, grabber):
    """(ms from the command to the PRESENT time of the first frame showing it,
    the frame interval around it). (None, None) if it never moved.

    ⚠ THE PRESENT TIME, NOT `time.perf_counter()` AT THE GRAB. This used to
    stamp `seen = now` the moment the polling loop noticed, which adds the grab
    and the loop's own period to every reading and -- worse -- puts the answer
    on a DIFFERENT clock from the thing it has to be subtracted from. The
    samples' time axis is `dxgi_time.present_s() - click_time`, so a latency
    measured against the poll instant is not the same quantity at all.

    present_s() is documented as returning perf_counter seconds, so the
    subtraction against `t_cmd` is exact, and what is left is ONE bias: the
    frame that shows the move is presented at the first present-instant after
    the true latency, so every reading carries U(0, T). That is why the frame
    interval comes back with it -- see main(), which corrects with the T it
    actually observed rather than with a nominal refresh rate.
    """
    for _ in range(4):
        grabber.grab_timed()
    prev = None
    while prev is None:
        _t, f = grabber.grab_timed()
        prev = rig.tracker.slice_frame(f) if f is not None else None
    t_cmd = time.perf_counter()
    rig.mouse.move(0, -MOVE)
    seen, prev_present, gap = None, None, None
    while time.perf_counter() - t_cmd < WATCH_S:
        t, f = grabber.grab_timed()
        if f is None:
            continue
        cur = rig.tracker.slice_frame(f)
        if cur is None:
            continue
        m = rig.tracker.measure_pair(prev, cur, 0.0)
        prev = cur
        if np.isfinite(m.dy) and abs(m.dy) >= MOVED_PX:
            seen = t
            if prev_present is not None:
                gap = t - prev_present
            break
        prev_present = t
    if seen is None:
        return None, None
    rig.mouse.move(0, MOVE)
    time.sleep(SETTLE_S)
    return 1000.0 * (seen - t_cmd), (1000.0 * gap if gap else None)


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--trials', type=int, default=20)
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--countdown', type=int, default=5)
    args = ap.parse_args()

    print('>>> Face something with texture. No weapon needed.')
    if not ensure_ready(label='the latency probe', countdown_s=args.countdown)['ok']:
        print('[!] could not focus the game')
        return 1

    # ⚠ prefer_dxgi=False, same reason collect_timed passes it: DXGI allows
    # ONE duplication interface per output per process, and the sync grabber
    # below needs it. A Rig that took it first makes grab_timed() raise
    # COMError E_INVALIDARG from inside AcquireNextFrame, four frames down.
    rig = Rig(args.sight, prefer_dxgi=False)
    from capture.cropper import DXGISyncGrabber
    grabber = DXGISyncGrabber(rig.tracker.regions())
    try:
        out, gaps = [], []
        for i in range(args.trials):
            ms, gap = one_trial(rig, grabber)
            if ms is None:
                print(f'  trial {i}: never moved')
                continue
            out.append(ms)
            if gap:
                gaps.append(gap)
            print(f'  trial {i:2d}: {ms:6.1f} ms'
                  + (f'   (frame gap {gap:.2f} ms)' if gap else ''))
        if not out:
            print('[!] nothing measured')
            return 1
        a = np.array(out)
        T = float(np.median(gaps)) if gaps else float('nan')
        print()
        print('command -> first frame SHOWING it (present time):')
        print(f'   mean {a.mean():.2f} ms   median {np.median(a):.2f}   '
              f'sd {a.std(ddof=1):.2f}   sem {a.std(ddof=1)/len(a)**0.5:.2f}   '
              f'n={len(a)}')
        print(f'   observed frame interval T = {T:.2f} ms '
              f'({1000.0/T:.0f} fps) from the same frames')
        print()
        # ⚠ TWO ESTIMATORS OF THE SAME THING, and they have to agree or the
        # quantisation model is wrong. The frame showing the move is presented
        # at the first present-instant after the true latency, so each reading
        # is L + U(0, T):
        #
        #   mean - T/2   uses every trial, and is unbiased IF U really is
        #                uniform, which it is only when the command instants
        #                are unsynchronised with the present grid.
        #   min          converges to L from ABOVE with no distributional
        #                assumption at all, but slowly, and it is the estimator
        #                a single early frame can drag down.
        #
        # Reporting both is the point. One of them agreeing with the offset
        # that 25 fired magazines picked out is a coincidence; both agreeing is
        # a measurement.
        print(f'   L, mean minus half a frame : {a.mean() - T/2:.2f} ms')
        print(f'   L, minimum over {len(a)} trials  : {a.min():.2f} ms')
        print(f'   -> the offset the compensation needs is the NEGATIVE of it')
    finally:
        rig.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
