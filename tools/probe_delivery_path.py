"""Why does a firmware curve move the screen 4% less than mouse.move()?

    pixi run python tools/probe_delivery_path.py --trials 6

eta = 0.96..0.97 is measured twice over (calibration/samples.py) and has no
cause. It is the last unexplained term in MODEL.md's identity:

    y_true(t) = y_obs(t) + eta * C(t - L)

Two mechanisms fit every number so far and they are not distinguishable from
the recoil data, because a burst only ever delivers compensation ONE way:

  SIZE   the game treats 240 reports of 1 count differently from 1 report of
         240 -- per-report rounding, a dead zone, or smoothing. Nothing to do
         with the firmware; mouse.move() would show it too if it sent many
         small moves.
  PATH   the firmware's curve player loses it. get_recoil_delta runs every ms
         and emits int(accum) with a carry, and send_hid_output returns early
         when everything is zero. Different code from a CMD_MOVE.

⚠ THE EXPERIMENT IS THE SAME TOTAL, OVER THE SAME DURATION, DELIVERED FOUR
WAYS. Anything that varies only the total or only the duration cannot separate
them -- that is why the +-10% scale sweep could not, and why it took a scale-0
arm to see eta at all.

    move-240   240 counts as 240 moves of 1, one per ~4 ms  <- the curve's rate
    curve      240 counts uploaded as a pattern, played by the FIRMWARE

    They deliver the SAME total at the SAME rate; the only difference is which
    code emits the HID reports. So:

    PATH     move-240 > curve by ~4%
    NEITHER  they agree, and eta lives somewhere this probe does not look

⚠ THE BIG-CHUNK ARMS ARE GONE AND THE REASON IS THE CORRELATOR, NOT THE GAME.
240 counts as ONE mouse.move() is 363 px inside a single frame, against a
tracker whose unambiguous range is +-RECOIL_PATCH_H/2 = 128 px -- the same
aliasing that made four K calibration runs unusable. It read 0.0750 px/count,
which is not a small delivery, it is a wrapped measurement. move-24 read 0.2245
for a related reason of my own making (below).

⚠ AND THE SIZE HYPOTHESIS IS ALREADY DEAD WITHOUT THEM. move-240 -- 240
separate one-count reports at the curve's own rate -- measured 1.5361 px/count
against a K of 1.5128. Many small moves through mouse.move() lose NOTHING. If
size were the mechanism, that arm is where it would have shown.

⚠ NO GUN, NO SCOPE, NO SHOOTING -- AND THAT IS A CONSEQUENCE OF NARROWING TO
TWO ARMS, not a shortcut. Hip fire's count ruler is NOT FLAT (0.636 +- 0.095,
control/CLAUDE.md), so it would wreck a comparison of different delivery RATES.
These two arms deliver at the SAME rate, so whatever hip fire does to one it
does to the other, and the ratio survives it.

Two earlier versions paid for the alternative. Scoped with a gun, the magazine
has to be drained or the held trigger fires it -- and when the drain silently
did not happen, `curve` read 2.56 px/count, 1.7x the move arm, because the
recoil was being added to the delivery. The drain was ASSUMED, not read back,
which is this repository's second cross-layer law with a gun in it. And
aim_and_scope() then failed twice with "could not get back to hip fire",
ending runs for a reason unrelated to the measurement.

⚠ THE VIEW IS RESTORED BY THE SAME METHOD IT WAS MOVED BY, one count at a
time, which is the arm measured at full K. The first version restored a fixed
-TOTAL however far the view had actually gone, so any under-delivering arm
walked it down a little every trial and by trial 2 it sat on the pitch clamp
reading zero -- caught by the dead-trial gate, which is what that gate is for.

⚠ ARMS INTERLEAVE PER TRIAL. Every cross-arm comparison this project made
without interleaving turned out to be a comparison of two sessions; the two
that mattered cost 30 counts and 0.8 percentage points. Rotating per trial
means session drift lands on all four arms equally.
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
WARMUP_S = 0.10
COOLDOWN_S = 0.40
SETTLE_S = 0.45
DEAD_PX = 2.0        # a trial that moved less than this measured nothing


def _inject_moves(mouse, total, n_steps, span, t0):
    """total counts as `n_steps` mouse.move() calls, evenly spaced over span.

    Carries the remainder so the SUM is exactly `total` however the steps
    divide it -- 240/24 is exact but 240/7 is not, and per-step truncation
    would make the arms differ in TOTAL, which is the one thing that must not
    vary between them.
    """
    # ⚠ THE SIGN OF `total` IS HONOURED, because this function is also the
    # RESTORE between arms and a restore that moved the same way as the trial
    # would double the drift rather than undo it. It emitted a hardcoded -send.
    sign = -1 if total >= 0 else +1
    total = abs(total)
    step = total / n_steps
    acc, sent_total = 0.0, 0
    for i in range(n_steps):
        # ⚠ SLEEP, DO NOT SPIN. This was a busy-wait, and a busy-wait in a
        # daemon thread starves the grab loop through the GIL: frames go
        # missing, the gap between surviving frames grows past the tracker's
        # 128 px range, and the arm reads a WRAPPED displacement. move-24 came
        # back at 0.2245 px/count that way -- the probe measuring its own
        # scheduler, which is the failure tools/CLAUDE.md records for the drag
        # scan ("扫描循环里绝不能有 look()").
        dt = t0 + span * i / n_steps - time.perf_counter()
        if dt > 0:
            time.sleep(dt)
        acc += step
        send = int(acc)
        acc -= send
        if send:
            mouse.move(0, sign * send)   # -ve = view up, away from the floor
            sent_total += send
    return sent_total


def _play_curve(mouse, total, span):
    """total counts as a firmware pattern, played by the click.

    ⚠ THE KNOT GRID MATCHES WHAT THE COMPENSATION ACTUALLY USES (17 ms), not
    something convenient. The mechanism under test may be the firmware's per-ms
    integer emission, and that depends on counts-per-ms, which depends on the
    knot spacing. A 1-knot pattern would be a different experiment wearing the
    same label.
    """
    n = int(span * 1000 / 17)
    t_s = [i * 0.017 for i in range(n)]
    dy_s = [-total / n] * n
    dx_s = [0.0] * n
    mouse.upload_pattern(dx_s, dy_s, t_s)
    mouse.set_recoil_enabled(True)
    got = mouse.read_pattern() or []
    delivered = -sum(k.get('dy', 0.0) for k in got)
    t_click = mouse.click(buttons=0x01, duration_ms=int(span * 1000))
    return delivered, t_click


def one_trial(rig, grabber, arm):
    """-> (px moved, counts the device says it sent). (None, None) if nothing."""
    for _ in range(3):
        grabber.grab_timed()
    prev = None
    while prev is None:
        _t, f = grabber.grab_timed()
        prev = rig.tracker.slice_frame(f) if f is not None else None

    total_px = 0.0
    t0 = time.perf_counter() + WARMUP_S
    sent = None
    if arm == 'curve':
        # ⚠ ARM AND CLICK FIRST, THEN WATCH. The firmware starts the pattern on
        # the click, and a grab loop entered afterwards would miss the opening
        # knots -- which is where the compensation puts most of its counts.
        sent, t_click = _play_curve(rig.mouse, TOTAL, SPAN_S)
        t0 = t_click
    else:
        n = int(arm.split('-')[1])
        import threading
        th = threading.Thread(target=lambda: _inject_moves(
            rig.mouse, TOTAL, n, SPAN_S, t0), daemon=True)
        th.start()

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
    if arm == 'curve':
        rig.mouse.set_recoil_enabled(False)
    else:
        sent = TOTAL
    time.sleep(SETTLE_S)
    return abs(total_px), sent


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--trials', type=int, default=6,
                    help='repeats PER ARM; the arms rotate, so this many rounds')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--weapon', default='mp5k')
    ap.add_argument('--countdown', type=int, default=8)
    a = ap.parse_args()

    ARMS = ['move-240', 'curve']

    if not ensure_ready(label='the delivery-path probe',
                        countdown_s=a.countdown)['ok']:
        print('[!] could not get the game ready')
        return 1

    rig = Rig(a.sight, prefer_dxgi=False)
    from capture.cropper import DXGISyncGrabber
    grabber = DXGISyncGrabber(rig.tracker.regions())
    out = {k: [] for k in ARMS}
    n_dead = 0
    try:
        for r in range(a.trials):
            for arm in ARMS:
                px, sent = one_trial(rig, grabber, arm)
                _inject_moves(rig.mouse, -TOTAL, 240, 0.6,
                              time.perf_counter())   # put the view back
                time.sleep(0.25)
                if px is None or px < DEAD_PX:
                    n_dead += 1
                    print(f'  r{r} {arm:9s} moved {px:.1f} px — NOTHING')
                    if n_dead >= 3:
                        print('\n[!] ABORT: three trials moved nothing. The '
                              'counts are not reaching the view (a modal '
                              'panel, the window, a clamp, a blank picture).')
                        return 2
                    continue
                n_dead = 0
                out[arm].append(px / sent)
                print(f'  r{r} {arm:9s} {px:8.2f} px / {sent:4.0f} counts '
                      f'= {px/sent:.4f} px per count')
    finally:
        try:
            rig.mouse.set_recoil_enabled(False)
        except Exception:
            pass
        rig.close()

    print()
    print(f'{"arm":10} {"n":>3} {"px/count":>10} {"sd":>8} {'vs ' + ARMS[0]:>12}')
    base = np.mean(out[ARMS[0]]) if out[ARMS[0]] else float('nan')
    for k in ARMS:
        v = np.array(out[k])
        if not len(v):
            print(f'{k:10} {"--":>3}')
            continue
        print(f'{k:10} {len(v):3d} {v.mean():10.4f} '
              f'{v.std(ddof=1) if len(v) > 1 else float("nan"):8.4f} '
              f'{100*(v.mean()/base - 1):+9.2f}%')
    print()
    # ⚠ AND THE WITHIN-ARM SCATTER IS THE VERDICT ON THE RUN, not a footnote.
    # A 3-4% effect cannot be read off arms whose own trials range 2x, and in
    # hip fire without a gun they do: the view wanders, hip fire's count ruler
    # is not flat, and each trial starts from wherever the last one left it.
    for k in ARMS:
        v = np.array(out[k])
        if len(v) > 1 and v.mean() and v.std(ddof=1) / v.mean() > 0.10:
            print(f'[!] {k}: trials range {v.min():.3f}..{v.max():.3f}, '
                  f'CV {100*v.std(ddof=1)/v.mean():.0f}% — this run cannot '
                  f'resolve a 4% difference. NOT A MEASUREMENT.')
    print()
    print('PATH     move-240 > curve by ~4%')
    print('NEITHER  they agree, and eta lives somewhere this probe cannot see')
    return 0


if __name__ == '__main__':
    sys.exit(main())
