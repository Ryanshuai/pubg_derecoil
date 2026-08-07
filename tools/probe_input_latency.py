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

So the weapon's fire delay is S - L, and that is exactly how late the
compensation should be, not how early. press.pico_mouse.RECOIL_LEAD_FRAC
currently shifts the pattern 30% of a bullet interval EARLIER, which on the
AUG stacks with S to put every pulse more than a full bullet ahead of the round
it was meant to cancel. The fit then hides it by suppressing the opening
rounds of the curve -- the AUG's first entry is -0.6 counts, on a gun whose
second is 5.1 and third 11.9.

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
from sweep import Rig

MOVE = 250          # counts per probe; big enough to clear the noise floor
WATCH_S = 0.40      # how long to keep looking after the command
MOVED_PX = 2.0      # per-frame displacement that counts as "it started"
SETTLE_S = 0.35     # let the view stop between trials


def one_trial(rig):
    """Milliseconds from issuing the move to the first frame that shows it."""
    rig.flush(4)
    prev = rig.tracker.slice_frame(rig.grab())
    t_cmd = time.perf_counter()
    rig.mouse.move(0, -MOVE)
    seen = None
    while time.perf_counter() - t_cmd < WATCH_S:
        now = time.perf_counter()
        cur = rig.tracker.slice_frame(rig.grab())
        m = rig.tracker.measure_pair(prev, cur, 0.0)
        prev = cur
        if np.isfinite(m.dy) and abs(m.dy) >= MOVED_PX:
            seen = now
            break
    if seen is None:
        return None
    # Put it back, so a run of trials does not walk the view into a clamp.
    rig.mouse.move(0, MOVE)
    time.sleep(SETTLE_S)
    return 1000.0 * (seen - t_cmd)


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

    rig = Rig(args.sight)
    try:
        fps = None
        out = []
        for i in range(args.trials):
            ms = one_trial(rig)
            if ms is None:
                print(f'  trial {i}: never moved')
                continue
            out.append(ms)
            print(f'  trial {i:2d}: {ms:6.1f} ms')
        if not out:
            print('[!] nothing measured')
            return 1
        a = np.array(out)
        print(f'\ncommand -> visible:  mean {a.mean():.1f} ms   median '
              f'{np.median(a):.1f}   sd {a.std(ddof=1):.1f}   n={len(a)}')
        print('\nThe frame interval is part of this: the move can land just '
              'after a\ngrab and wait a whole frame to be seen, so the mean '
              'carries about half\na frame of quantisation on top of the real '
              'latency.')
    finally:
        rig.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
