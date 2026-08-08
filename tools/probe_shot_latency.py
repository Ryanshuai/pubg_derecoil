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

from control.session import ensure_ready

from calibration.sweep import Rig

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
    prev_sig = rig.fire.ammo_sig(base)
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
        sig = rig.fire.ammo_sig(frame)
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
    if not ensure_ready(label='the shot-latency probe', countdown_s=args.countdown)['ok']:
        print('[!] could not focus the game')
        return 1

    rig = Rig(args.sight)
    rec, ammo, both = [], [], []
    try:
        # ⚠ A GUN AND ROUNDS IN IT, verified -- because the failure without
        # them is a lie that reads as a result. 2026-08-07: this ran straight
        # after an impulse probe had emptied the magazine, tapped 20 times into
        # a gun with nothing in it, and reported
        #
        #     S_recoil  view starts moving : never seen
        #     S_ammo    counter changes    : never seen
        #
        # Every word of which is TRUE. The view really did not move and the
        # counter really did not change -- because no round was fired, which is
        # not what the probe set out to measure and not what "never seen"
        # sounds like.
        #
        # Same shape as probe_pitch_range's "posture unreadable" against the
        # lobby (tools/CLAUDE.md): a real reading of the wrong situation.
        # ensure_ready covers "can the game be driven"; it says nothing about
        # what is in the character's hands, which is the experiment's business.
        #
        # This REFUSES rather than spawning: a probe that quietly fixes its own
        # preconditions hides the one fact its caller most needs -- that the
        # gun it thinks it measured is not the gun that was there.
        rounds = rig.fire.read_ammo(rig.grab())
        if not rounds:
            rounds, _ = rig.fire.top_up()
        if not rounds:
            print('[!] REFUSING: no ammo counter, so either nothing is held or '
                  'the magazine is empty.\n'
                  '    Tapping now would report "never seen" for both signals, '
                  'which is true and\n'
                  '    means nothing. Rack a loaded gun (harvest or '
                  'control/stock.py) and re-run.')
            return 2
        print(f'    {rounds} rounds in the magazine — enough for '
              f'{args.taps} taps' if rounds >= args.taps else
              f'    [!] only {rounds} rounds for {args.taps} taps; the tail '
              f'will read "never seen" once it runs dry')
        if not args.keep_comp:
            rig.fire.disarm()
        if not rig.gun.ensure_ads():
            print('[!] could not enter ADS')
            return 1
        for i in range(args.taps):
            if not rig.gun.ensure_ads():
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
        rig.close()          # disarms; see FireDriver.disarm

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
        # ⚠ BOTH NUMBERS HERE WERE ONCE HARDCODED AS 36, AND BOTH WERE STALE.
        # L is 38 ms (tools/probe_input_latency.py, n=44, sd 4.8) and the
        # constant is 13, not 36 -- 36 was an earlier value of the constant
        # itself, retired because it came from S = 72 measured with a coarse
        # motion threshold on the RAMPING recoil rather than off the counter.
        # press/pico_mouse.py says so directly above the constant.
        #
        # So on 2026-08-07 this probe measured S_recoil = 51.3 (the docs say
        # 51: a clean independent confirmation, and the first on a weapon other
        # than the AUG) and then advised changing 13 to 15 while reporting the
        # value as 36. A probe that reads its own subject from a literal will
        # eventually argue against a correct calibration, confidently.
        #
        # Read the constant. Do not restate it.
        # A CLASS attribute of PicoMouse, not a module global -- importing it
        # by name raises, which is its own small reminder that reading the
        # subject beats restating it.
        # ⚠ THE OFFSET COMES OFF THE COUNTER, NOT OFF S_recoil, and this probe
        # used to do the opposite -- which made it argue for exactly the value
        # press/pico_mouse.py records as retired. Its comment there:
        #
        #     36 ms came from S = 72, measured with a coarse motion threshold
        #     on the ramping recoil. Re-measured off the counter it is 51,
        #     and W is 13.
        #
        # S_recoil is that same coarse threshold, and it is biased LATE by
        # construction: the detector needs displacement to accumulate before
        # it will call the view moved, so it can only ever fire at or after
        # the true instant. Measured 2026-08-07, m416, 40 taps -- the paired
        # gap median is 0.0 ms (the two ARE the same event), yet 5 of 40 taps
        # read recoil 12-17 ms LATE and NOT ONE read it early. That one-sided
        # tail is what pushed S_recoil's median 5 ms above S_ammo's and made
        # this probe recommend 21 against a stored 13.
        #
        # The counter has no such tail: it is a discrete glyph change, present
        # or not. Both are rendered through the same chain, so L cancels the
        # same way for either -- the counter is simply the cleaner clock.
        from press.pico_mouse import PicoMouse
        CUR = PicoMouse.RECOIL_FIRE_DELAY_MS
        # Also measured, also drifts: tools/probe_input_latency.py, 35.8 +-5.1
        # over n=44. Its SPREAD is the reason for the tolerance below -- an
        # offset quoted without it looks far more decided than it is.
        L_MS, L_SD = 38.0, 5.1
        s_amm = float(np.median(ammo)) if len(ammo) else None
        if s_amm is None:
            print('\n  No counter readings — cannot site the offset. '
                  'S_recoil alone will not do it; see the comment here.')
            return 0
        want = s_amm - L_MS
        sem = float(np.std(ammo, ddof=1)) / max(len(ammo), 1) ** 0.5
        tol = (sem ** 2 + L_SD ** 2) ** 0.5
        print(f'\n  Pattern start offset = S_ammo - L = {s_amm:.0f} - '
              f'{L_MS:.0f} = {want:.0f} ms  (+-{tol:.0f}, and L carries '
              f'{L_SD:.0f} of that)')
        print(f'  S_recoil would say {s_rec - L_MS:.0f} — NOT the number to '
              f'use, see the comment in this file')
        # ⚠ NAMES config.py, NOT press/pico_mouse.py. PicoMouse still
        # carries a RECOIL_FIRE_DELAY_MS class attribute, but it merely
        # reads config's -- and this line used to send the operator to edit
        # the attribute, where a new value would silently shadow the ~70
        # lines of measurement that justify the one in config.py.
        print(f'  config.RECOIL_FIRE_DELAY_MS is {CUR}')
        if abs(want - CUR) <= tol:
            print(f'  Inside {tol:.0f} ms — this run CONFIRMS the stored '
                  f'value, it does not move it.')
        else:
            print(f'  [!] {want - CUR:+.0f} ms apart, against a tolerance of '
                  f'{tol:.0f}. Re-measure L before touching the constant:\n'
                  f'      it is the bigger term here, and it is a literal in '
                  f'this file rather than a reading.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
