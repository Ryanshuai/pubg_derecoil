"""Is it `y_obs = dy_px/K + human` or `- human`? Move the mouse and find out.

    pixi run python tools/probe_human_sign.py --seconds 8

MOVE THE MOUSE UP AND DOWN WHILE IT RUNS. Nothing else should happen: no
firing, no compensation, no injected motion.

WHY THIS IS UNTESTED AFTER FOUR MONTHS. `human_dy` is 2 nonzero values out of
131146 intervals across the ENTIRE sample store, because every magazine is
fired by the machine with nobody touching the mouse. The term has never been
exercised, so its SIGN has never been checked -- and a wrong sign does not fail
loudly, it DOUBLES the hand's contribution instead of removing it, the first
time anyone's hand moves mid-burst.

⚠ THE TWO SOURCES DISAGREE ON PAPER, WHICH IS WHY THIS IS A MEASUREMENT.

    press/firmware/src/main.c:604   "Publish the human-movement totals so the
                                     PC can SUBTRACT the hand"
    calibration/samples.py          counts = dy / self.K + human

Both can be right at once IF the two carry opposite sign conventions: the
firmware adds `raw_dy` (mouse down positive) while the correlator reports view
rotation (up positive), so the hand's contribution enters the screen reading
already negated. That is an argument, not evidence, and this repository has
had two arguments of exactly that shape overturned by firing them tonight.

THE TEST IS A PER-FRAME REGRESSION, not a cumulative sum, and the first
version got that wrong twice over. Screen dy (px) against hand dy (counts),
one point per frame pair:

    slope > 0    screen and hand are reported the SAME way round, so removing
                 the hand means  dy/K - human
    slope < 0    opposite conventions, and  dy/K + human  is right

⚠ WHY NOT THE CUMULATIVE SUM. It was tried and refused itself: screen +797.6
counts against a hand of +4310.0, so NEITHER candidate cancelled and the gate
printed NOT A VERDICT. Two reasons, both mine, and both invisible in a total:

  the probe was in HIP FIRE while dividing by the red dot's K -- a 3x error
  the hand pushed 4310 counts, which walks the view into the PITCH CLAMP, and
  past the stop the hand keeps counting while the screen does not move

The regression survives both: clamped frames are (hand != 0, screen ~ 0), which
drags the slope toward zero but CANNOT flip its sign. And the slope's magnitude
is a free second reading of K.

⚠ BUT A FAST HAND IS NOT SURVIVABLE AND HAS TO BE THROWN AWAY. The correlator
is unambiguous only to RECOIL_PATCH_H/2 = 128 px between frames, which at the
ADS K is 83 counts -- and at ~150 fps that is 83 counts in 6.7 ms. A flick
clears it easily, and past it the reading wraps by 256 px, so an aliased pair
comes back SMALL and possibly with the WRONG SIGN. Sign is the entire answer
here, so pairs near the ceiling are dropped and counted rather than fitted.

Raised from the chair before the second run: 「我如果真的上下浮动很大的话，那个
能不能接住？」 It cannot. Move far but SLOWLY.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np

from control.session import ensure_ready
from calibration.sweep import Rig

# Per-pair ceiling. RECOIL_PATCH_H/2 is 128 px; past it the correlation wraps
# by a full patch height and reports a small, possibly wrong-signed number.
SAFE_PX = 100.0


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--seconds', type=float, default=8.0)
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--countdown', type=int, default=8)
    a = ap.parse_args()

    if not ensure_ready(label='the human-sign probe',
                        countdown_s=a.countdown)['ok']:
        print('[!] could not get the game ready')
        return 1

    rig = Rig(a.sight, prefer_dxgi=False)
    from capture.cropper import DXGISyncGrabber
    try:
        rig.mouse.set_recoil_enabled(False)
        # ⚠ ADS, or K is wrong by 3x. The first run did not do this and
        # divided hip-fire pixels by the red dot's constant.
        if not rig.gun.ensure_ads():
            print('[!] could not get into ADS — K would be the hip-fire one')
            return 1
        fn = getattr(rig.mouse, 'human_totals', None)
        if fn is None:
            print('[!] this backend does not publish human totals')
            return 1
        grabber = DXGISyncGrabber(rig.tracker.regions())
        for _ in range(4):
            grabber.grab_timed()
        prev = None
        while prev is None:
            _t, f = grabber.grab_timed()
            prev = rig.tracker.slice_frame(f) if f is not None else None
        h0 = fn()
        print()
        print(f'>>> MOVE THE MOUSE UP AND DOWN for {a.seconds:.0f} s. '
              f'Do not click. Starting now.')
        t0 = time.perf_counter()
        S_, H_ = [], []
        n_seen = 0
        hprev = h0[1]
        while time.perf_counter() - t0 < a.seconds:
            _t, f = grabber.grab_timed()
            if f is None:
                continue
            cur = rig.tracker.slice_frame(f)
            if cur is None:
                continue
            m = rig.tracker.measure_pair(prev, cur, 0.0)
            prev = cur
            h = fn()[1]
            if np.isfinite(m.dy):
                S_.append(m.dy)
                H_.append(h - hprev)
            hprev = h
            n_seen += 1
        print('>>> done, you can let go')
    finally:
        rig.close()

    S_ = np.asarray(S_, dtype=float)
    H_ = np.asarray(H_, dtype=float)
    print()
    print(f'  frame pairs              {len(S_)}')
    print(f'  hand moved (per frame)   sd {H_.std():.2f} counts, '
          f'total |{np.abs(H_).sum():.0f}|')
    if np.abs(H_).sum() < 500:
        print('[!] the hand barely moved — nothing to regress against.')
        return 2
    fast = np.abs(S_) > SAFE_PX
    if fast.any():
        print(f'  [!] {fast.sum()} pair(s) over {SAFE_PX:.0f} px — DROPPED. '
              f'Past 128 px the correlation wraps by a whole patch height and '
              f'comes back small, sometimes with the wrong sign, and sign is '
              f'the whole answer here. Move far but SLOWLY.')
    sel = (np.abs(H_) > 0.5) & ~fast
    if sel.sum() < 30:
        print('[!] too few frames where the hand actually moved.')
        return 2
    slope, icept = np.polyfit(H_[sel], S_[sel], 1)
    r = float(np.corrcoef(H_[sel], S_[sel])[0, 1])
    print(f'  regression on {sel.sum()} moving frames: '
          f'screen_px = {slope:+.4f} * hand_counts {icept:+.2f}   r = {r:+.3f}')
    print()
    if abs(r) < 0.5:
        print('[!] r is too weak to read a sign off. NOT A VERDICT.')
        return 3
    print(f'  |slope| = {abs(slope):.4f} px/count, and K is {rig.K:.4f} — '
          f'a free second reading, {100*(abs(slope)/rig.K-1):+.1f}% off')
    print()
    if slope > 0:
        print('  slope POSITIVE: screen and hand are reported the same way, so')
        print('  the hand is removed by  dy/K - human.')
        print('  samples.py uses + human  ->  THE CODE IS WRONG, and a magazine')
        print('  with a moving hand carries TWICE the hand instead of none.')
    else:
        print('  slope NEGATIVE: opposite conventions, so the hand is already')
        print('  negated in the screen reading and  dy/K + human  removes it.')
        print('  samples.py uses + human  ->  CORRECT, and now measured rather')
        print('  than argued from two comments that disagree.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
