"""Click to recoil, and click to counter — the two halves of the fire delay.

    pixi run python tools/probe_shot_latency.py --weapon aug --taps 20

Taps the trigger once, with the compensation off, and watches two things:

    S_recoil   click -> the view starts moving
    S_ammo     click -> the ammo counter changes

Everything about the compensation's timing rests on these, and only one of
them had ever been measured -- S_ammo, logged by harvest as shot_delay_ms and
running about 65 ms. The pattern is scheduled against S_recoil, because it is
the recoil the counts have to cancel, and the two were ASSUMED equal.

If they are equal, the calibration loop is on solid ground for a second
reason: the residual's time origin is the first counter change, and the
counter is rendered through the same capture chain as the recoil, so the
capture latency cancels out of the measurement entirely. If they are not, the
whole curve is fitted on a grid shifted from the one it is played on -- which
is invisible in the residual, because the fit converges to whatever nulls the
sums on its own grid.

Combined with L from tools/probe_input_latency.py (command -> visible, 35.8
+-5.1 ms here), the pattern's start offset is S_recoil - L. That is the only
term: there is no half-interval correction, because the game's recoil is
spread over the bullet interval just as the firmware's compensation is, and
the two centroids move together. See press/pico_mouse.RECOIL_FIRE_DELAY_MS.

One round per tap, so the view barely moves and nothing needs re-aiming.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))

import numpy as np

from control.focus import ensure_focus

from sweep import Rig

TAP_MS = 30           # trigger hold; long enough for one round, short of two
WATCH_S = 0.45        # how long to look after the click
SETTLE_S = 0.9        # let the shot finish and the view settle

# Onset is taken from the CUMULATIVE displacement crossing this, not from a
# per-frame rate. The kick ramps -- the first 7 ms of an AUG bullet carries
# 0.9 counts, the middle carries 2.7 -- so a per-frame threshold fires only
# once the rate has built, which is a bias that is always LATE and never early.
# With a 1.5-count rate threshold the recoil read 14 ms after the ammo counter,
# and 7 taps of 16 still landed in the same frame: a one-sided gap like that is
# the threshold talking, not the game.
#
# Cumulative removes it. 1.0 count is crossed inside the first frame or two of
# the real onset whatever the ramp looks like, and the correlator's own noise
# floor sits near 0.5 counts per frame.
ONSET_COUNTS = 1.0


def one_tap(rig):
    """(ms to the view moving, ms to the counter changing), either may be None."""
    rig.flush(4)
    base = rig.grab()
    prev_patch = rig.tracker.slice_frame(base)
    prev_sig = rig.ammo_sig(base)
    t_click = time.perf_counter()
    rig.mouse.click(buttons=0x01, duration_ms=TAP_MS)
    t_recoil = t_ammo = None
    cum = 0.0
    while time.perf_counter() - t_click < WATCH_S:
        now = time.perf_counter()
        frame = rig.grab()
        patch = rig.tracker.slice_frame(frame)
        m = rig.tracker.measure_pair(prev_patch, patch, 0.0)
        prev_patch = patch
        if t_recoil is None and np.isfinite(m.dy):
            cum += m.dy / rig.K
            if abs(cum) >= ONSET_COUNTS:
                t_recoil = now
        sig = rig.ammo_sig(frame)
        if t_ammo is None and float(np.mean(sig != prev_sig)) > 0.02:
            t_ammo = now
        prev_sig = sig
        if t_recoil is not None and t_ammo is not None:
            break
    time.sleep(SETTLE_S)
    return (None if t_recoil is None else 1000.0 * (t_recoil - t_click),
            None if t_ammo is None else 1000.0 * (t_ammo - t_click))


def report(name, xs):
    if not xs:
        print(f'  {name}: never seen')
        return None
    a = np.array(xs)
    print(f'  {name}: mean {a.mean():6.1f}  median {np.median(a):6.1f}  '
          f'sd {a.std(ddof=1) if len(a) > 1 else 0:5.1f}  n={len(a)}')
    return float(np.median(a))


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapon', default='?', help='for the report only')
    ap.add_argument('--taps', type=int, default=20)
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--countdown', type=int, default=6)
    ap.add_argument('--keep-comp', action='store_true',
                    help='leave the compensation on; the default turns it off '
                         'so the view motion seen is the recoil alone')
    args = ap.parse_args()

    print('>>> Hold the gun, face texture. One round per tap, compensation '
          + ('ON' if args.keep_comp else 'OFF') + '.')
    if not ensure_focus(countdown_s=args.countdown, label='the shot-latency probe'):
        print('[!] could not focus the game')
        return 1
    time.sleep(0.6)

    rig = Rig(args.sight)
    rec, ammo, both = [], [], []
    try:
        if not args.keep_comp:
            rig.mouse.set_recoil_enabled(False)
        if not rig.ensure_ads():
            print('[!] could not enter ADS')
            return 1
        for i in range(args.taps):
            if not rig.ensure_ads():
                print(f'  tap {i}: lost ADS')
                continue
            r, a = one_tap(rig)
            print(f'  tap {i:2d}: recoil '
                  + (f'{r:6.1f}' if r is not None else '     -')
                  + '   counter '
                  + (f'{a:6.1f}' if a is not None else '     -')
                  + ('' if r is None or a is None else
                     f'   gap {a - r:+6.1f} ms'))
            if r is not None:
                rec.append(r)
            if a is not None:
                ammo.append(a)
            if r is not None and a is not None:
                both.append(a - r)
    finally:
        rig.mouse.set_recoil_enabled(True)
        rig.close()

    print(f'\n{args.weapon}, {args.taps} taps, milliseconds from the click:')
    s_rec = report('S_recoil  view starts moving ', rec)
    report('S_ammo    counter changes    ', ammo)
    gap = report('gap       counter - recoil  ', both)

    if gap is not None:
        if abs(gap) < 12:
            print('\n  The counter and the recoil land together, within a frame.'
                  '\n  That is what makes the residual\'s time origin free of '
                  'capture latency:\n  both are rendered events on the same '
                  'chain, so it cancels.')
        else:
            print(f'\n  [!] They are {gap:+.0f} ms apart, which is '
                  f'{abs(gap)/8.3:.1f} frames. The curve is\n  fitted on bins '
                  f'anchored to the COUNTER and played on a grid anchored to\n'
                  f'  the RECOIL, so every bullet is compensated {gap:+.0f} ms '
                  f'off. The residual\n  cannot see it — the fit converges to '
                  f'whatever nulls its own grid.')
    if s_rec is not None:
        print(f'\n  Pattern start offset should be S_recoil - L = '
              f'{s_rec:.0f} - 36 = {s_rec - 36:.0f} ms')
        print(f'  press/pico_mouse.RECOIL_FIRE_DELAY_MS is currently 36')
    return 0


if __name__ == '__main__':
    sys.exit(main())
