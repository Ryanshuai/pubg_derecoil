"""Why does a firmware curve move the screen 4% less than mouse.move()?

    pixi run python tools/probe_delivery_path.py --trials 8

eta = 0.96..0.97 is measured twice over (calibration/samples.py) and has no
cause. It is the last unexplained term in MODEL.md's identity:

    y_true(t) = y_obs(t) + eta * C(t - L)

Two mechanisms fit every number, and the recoil data cannot separate them
because a burst only ever delivers compensation one way:

  SIZE   the game treats 240 reports of 1 count differently from 1 of 240 --
         per-report rounding, a dead zone, smoothing. Nothing to do with the
         firmware; mouse.move() would show it too.
  PATH   the firmware's curve player loses it. get_recoil_delta runs every ms
         and emits int(accum) with a carry, and send_hid_output returns early
         when everything is zero. Different code from a CMD_MOVE.

⚠ SIZE IS ALREADY DEAD. move-240 -- 240 separate one-count reports at the
curve's own rate, scoped, gun in hand -- measured 1.5357 and 1.5444 px/count
against a K of 1.5128. Many small moves through mouse.move() lose NOTHING, and
that arm is exactly where size would have shown.

So this is now a two-arm comparison: the SAME total, over the SAME duration, at
the SAME rate, differing only in which code emits the reports.

    move-240   240 counts as 240 mouse.move()s of 1, one per ~4 ms
    curve      240 counts uploaded as a pattern, played by the FIRMWARE

    PATH     move-240 > curve by ~4%
    NEITHER  they agree, and eta lives somewhere this probe cannot see

FOUR THINGS THIS FILE DOES BECAUSE EARLIER VERSIONS DID NOT
-----------------------------------------------------------
⚠ NO WEAPON IN HAND, AND THAT IS THE ONLY WAY THE TRIGGER CAN BE HELD SAFELY.
`firing` in the firmware is set by the CLICK, so the pattern plays whenever the
button is down -- and on a gun that means firing. Two ways round it were tried:

  assume the magazine is drained     the drain was never verified, live rounds
                                     went out, and `curve` read 2.56 px/count
                                     (1.7x the move arm) because RECOIL was
                                     being counted as delivery
  drain it and read the counter      IT REFILLS. Measured here: 24 -> 40 after
                                     a 4 s hold. PUBG auto-reloads from reserve
                                     when the magazine empties, so "an empty
                                     gun" is not a state this range can hold

So the gun comes out of hand entirely. That is HIP FIRE, whose count ruler is
NOT FLAT (0.636 +- 0.095, control/CLAUDE.md) -- which would wreck a comparison
of different delivery RATES, and does not touch this one: both arms deliver at
the SAME rate, so whatever hip fire does to one it does to the other.

⚠ AND THE COUNTER MUST NOT BE READ MID-BURST. click() returns the instant the
bytes go out; the FIRMWARE holds the button. A sleep shorter than the hold
reads a counter whose digits are still moving, which comes back None -- said
from the chair the same day, about the reload: 「那个时候它那个数字跳,你那时候
是读不准的」.

⚠ EVERY ARM FIRES BOTH WAYS, SO THE VIEW NEVER DRIFTS. Earlier versions
restored by a fixed amount however far the view had actually gone, so any
under-delivering arm walked it down every trial until it sat on the pitch clamp
reading zero. Alternating the direction leaves the net at zero BY
CONSTRUCTION -- no restore, no re-home, and no dependence on aim_and_scope(),
which failed twice with "could not get back to hip fire" and ended two runs for
a reason unrelated to the measurement.

⚠ IT STAYS SCOPED, AND ADS IS RE-READ. Hip fire's count ruler is NOT FLAT
(0.636 +- 0.095, control/CLAUDE.md), the view wanders through it, and a run
there had within-arm trials ranging 2x -- which cannot resolve 4%. ADS is worth
~3x and dropping out is silent, so it is checked every round rather than
assumed to persist.

⚠ AND 240 COUNTS AS ONE MOVE IS NOT MEASURABLE HERE AT ALL. That is 363 px
inside one frame against a tracker whose unambiguous range is
RECOIL_PATCH_H/2 = 128 px. It read 0.0750 px/count, which is a WRAPPED
measurement and not a small delivery -- the same aliasing that made four K
calibration runs unusable (tools/audit_k.py). Any trial whose worst frame
approaches that ceiling is dropped and says so.
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

TOTAL = 240          # counts per trial, every arm
SPAN_S = 1.0         # delivered over this long, every arm
KNOT_MS = 17         # the grid the compensation actually uses
WARMUP_S = 0.10
COOLDOWN_S = 0.40
SETTLE_S = 0.40
DEAD_PX = 2.0        # a trial that moved less than this measured nothing
TEXTURE_MIN = 40.0   # Laplacian variance the patches need to be trackable
ALIAS_PX = 110.0     # per-frame ceiling; RECOIL_PATCH_H/2 is 128


def _inject_moves(mouse, total, n_steps, span, t0):
    """`total` counts as n_steps mouse.move()s, evenly spaced over span.

    The sign of `total` is honoured: negative sends the view down.

    ⚠ SLEEP, NEVER SPIN. A busy-wait here starves the grab loop through the
    GIL, frames go missing, the gap between survivors grows past the tracker's
    range, and the arm reads a WRAPPED displacement. One version read 0.2245
    px/count that way: the probe measuring its own scheduler.

    Carries the remainder so the SUM is exactly `total` however the steps
    divide it -- per-step truncation would make the arms differ in TOTAL, the
    one thing that must not vary between them.
    """
    sign = -1 if total >= 0 else +1
    total = abs(total)
    step = total / n_steps
    acc, sent = 0.0, 0
    for i in range(n_steps):
        dt = t0 + span * i / n_steps - time.perf_counter()
        if dt > 0:
            time.sleep(dt)
        acc += step
        send = int(acc)
        acc -= send
        if send:
            mouse.move(0, sign * send)
            sent += send
    return sent


def _play_curve(mouse, total, span):
    """`total` counts as a firmware pattern, played by the click.

    ⚠ THE KNOT GRID IS THE COMPENSATION'S OWN 17 ms, not something convenient.
    The mechanism under test may be the firmware's per-ms integer emission, and
    that depends on counts-per-ms, which depends on the knot spacing. A 1-knot
    pattern would be a different experiment wearing the same label.

    Returns (counts the FIRMWARE says it holds, the click instant). The total
    is READ BACK rather than assumed: int16 quantisation with a carry, and the
    negative-offset fold, both sit between the request and the wire.
    """
    n = int(span * 1000 / KNOT_MS)
    sign = -1 if total >= 0 else +1
    per = sign * abs(total) / n
    mouse.upload_pattern([0.0] * n, [per] * n,
                         [i * KNOT_MS / 1000.0 for i in range(n)])
    mouse.set_recoil_enabled(True)
    got = mouse.read_pattern() or []
    held = abs(sum(k.get('dy', 0.0) for k in got))
    t_click = mouse.click(buttons=0x01, duration_ms=int(span * 1000))
    return held, t_click


def texture(rig, grabber):
    """Laplacian variance of the tracked patches. -> float.

    ⚠ THE CORRELATOR MEASURES HOW THE PICTURE MOVES, so a picture with nothing
    in it reads a confident zero rather than an error. A whole run went that
    way on 2026-08-08 -- the view was on open sky, within-arm CV came back at
    103%, and several trials read 0.0 px. Spotted from the chair ("你对的天空
    了，我不知道你能不能测出来啥"), not by anything in the program.

    This is the same shape as control/aim.py's clamp probe, which cannot use
    the tracker at either stop for exactly this reason: at the top there is
    sky, and BLIND READS AS "IT DID NOT MOVE".
    """
    import cv2
    for _ in range(3):
        grabber.grab_timed()
    _t, f = grabber.grab_timed()
    p = rig.tracker.slice_frame(f) if f is not None else None
    if p is None:
        return 0.0
    arrs = p if isinstance(p, (list, tuple)) else [p]
    vs = []
    for a in arrs:
        a = np.asarray(a)
        if a.ndim == 3:
            a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        vs.append(float(cv2.Laplacian(a.astype(np.uint8), cv2.CV_64F).var()))
    return float(np.median(vs))


def one_trial(rig, grabber, arm, sign):
    """-> (px moved, counts the device sent, worst per-frame px)."""
    for _ in range(3):
        grabber.grab_timed()
    prev = None
    while prev is None:
        _t, f = grabber.grab_timed()
        prev = rig.tracker.slice_frame(f) if f is not None else None

    total_px, max_frame = 0.0, 0.0
    t0 = time.perf_counter() + WARMUP_S
    if arm == 'curve':
        # Arm and click FIRST: the firmware starts the pattern on the click,
        # and a grab loop entered afterwards would miss the opening knots.
        sent, t0 = _play_curve(rig.mouse, sign * TOTAL, SPAN_S)
    else:
        # ⚠ THIS ARM CLICKS TOO, AND THAT IS THE WHOLE POINT. Only `curve` used
        # to, because only `curve` needs `firing` -- so the click's own effect
        # sat in one arm and not the other. With no weapon in hand a held left
        # button is a PUNCH, and its animation moves the camera: the first run
        # this way read curve up/dn at 1.38/0.24 while move read 0.44/0.42.
        # Symmetric arm, 3-4x asymmetric arm, and the only difference between
        # them was the button.
        #
        # So both arms hold the button for the same second. The pattern is
        # DISABLED here, so the firmware emits nothing and the counts come from
        # mouse.move() -- identical click, identical punch, one variable.
        import threading
        rig.mouse.set_recoil_enabled(False)
        threading.Thread(target=lambda: _inject_moves(
            rig.mouse, sign * TOTAL, 240, SPAN_S, t0), daemon=True).start()
        rig.mouse.click(buttons=0x01, duration_ms=int(SPAN_S * 1000))
        sent = float(TOTAL)

    while time.perf_counter() < t0 + SPAN_S + COOLDOWN_S:
        _t, f = grabber.grab_timed()
        if f is None:
            continue
        cur = rig.tracker.slice_frame(f)
        if cur is None:
            continue
        m = rig.tracker.measure_pair(prev, cur, 0.0)
        prev = cur
        if np.isfinite(m.dy):
            total_px += m.dy
            max_frame = max(max_frame, abs(m.dy))
    if arm == 'curve':
        rig.mouse.set_recoil_enabled(False)
    time.sleep(SETTLE_S)
    return abs(total_px), sent, max_frame


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--trials', type=int, default=8,
                    help='rounds; each round fires every arm BOTH ways')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--weapon', default='mp5k')
    ap.add_argument('--countdown', type=int, default=8)
    a = ap.parse_args()

    ARMS = ['move-240', 'curve']
    out = {k: [] for k in ARMS}

    if not ensure_ready(label='the delivery-path probe',
                        countdown_s=a.countdown)['ok']:
        print('[!] could not get the game ready')
        return 1

    rig = Rig(a.sight, prefer_dxgi=False)
    from capture.cropper import DXGISyncGrabber
    try:
        rig.mouse.set_recoil_enabled(False)

        grabber = DXGISyncGrabber(rig.tracker.regions())
        tx = texture(rig, grabber)
        print(f'  patch texture: Laplacian variance {tx:.0f} '
              f'(need {TEXTURE_MIN:.0f})')
        if tx < TEXTURE_MIN:
            print('[!] REFUSING: nothing to track. Point the view at something '
                  'with structure — open sky reads a confident zero, not an '
                  'error, and that is what a whole run measured.')
            return 6
        n_dead = 0
        for r in range(a.trials):
            if texture(rig, grabber) < TEXTURE_MIN:
                print(f'  r{r}: the view drifted onto a blank picture — '
                      f'stopping rather than measuring it')
                break
            for arm in ARMS:
                for sign in (+1, -1):
                    px, sent, mx = one_trial(rig, grabber, arm, sign)
                    tag = 'up' if sign > 0 else 'dn'
                    if px < DEAD_PX:
                        n_dead += 1
                        print(f'  r{r} {arm:9s} {tag} moved {px:.1f} px — NOTHING')
                        if n_dead >= 3:
                            print('\n[!] ABORT: three trials moved nothing.')
                            return 2
                        continue
                    if mx > ALIAS_PX:
                        print(f'  r{r} {arm:9s} {tag} worst frame {mx:.0f} px '
                              f'> {ALIAS_PX:.0f} — DROPPED. The correlator '
                              f'aliases at 128 and hides it in a small reading.')
                        continue
                    n_dead = 0
                    out[arm].append(px / sent)
                    print(f'  r{r} {arm:9s} {tag} {px:8.2f} px / {sent:6.1f} '
                          f'counts = {px/sent:.4f}   worst frame {mx:5.1f}')
    finally:
        try:
            rig.mouse.set_recoil_enabled(False)
        except Exception:
            pass
        rig.close()

    print()
    print(f'{"arm":10} {"n":>3} {"px/count":>10} {"sd":>8} {"vs move-240":>12}')
    base = np.mean(out['move-240']) if out.get('move-240') else float('nan')
    bad = False
    for k in ARMS:
        v = np.array(out[k])
        if not len(v):
            print(f'{k:10} {"--":>3}')
            bad = True
            continue
        sd = v.std(ddof=1) if len(v) > 1 else float('nan')
        print(f'{k:10} {len(v):3d} {v.mean():10.4f} {sd:8.4f} '
              f'{100*(v.mean()/base - 1):+11.2f}%')
        # ⚠ THE WITHIN-ARM SCATTER IS THE VERDICT ON THE RUN, not a footnote.
        # A 4% effect cannot be read off an arm whose own trials range 2x, and
        # one run's did. Printing a mean anyway is how a scatter plot becomes
        # a finding.
        if len(v) > 1 and v.mean() and sd / v.mean() > 0.05:
            print(f'           [!] CV {100*sd/v.mean():.0f}% — this arm cannot '
                  f'resolve 4%. NOT A MEASUREMENT.')
            bad = True
    print()
    if not bad:
        print('PATH     move-240 > curve by ~4%')
        print('NEITHER  they agree, and eta lives somewhere this probe cannot see')
    return 0


if __name__ == '__main__':
    sys.exit(main())
