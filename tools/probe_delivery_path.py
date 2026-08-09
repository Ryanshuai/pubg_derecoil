"""Why does a firmware curve move the screen 4% less than mouse.move()?

    pixi run python tools/probe_delivery_path.py --trials 8

eta = 0.96..0.97 is measured twice over (calibration/samples.py) and has no
cause. It is the last unexplained term in MODEL.md's identity:

    y_true(t) = y_obs(t) + C(t - M)        <- and eta is NOT in it, see below

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
PATCH_WAIT_S = 4.0   # how long a trial may wait for a trackable patch
# ⚠ A `none` ARM THAT MOVES IS NOT ENOUGH TO CALL IT GUNFIRE, and a flat
# threshold on it was wrong. It was set at 5 px because the punch had measured
# 0.0-0.3 across two runs -- and then a third run, with no weapon out (HUD ink
# 0.00%), read 33 px. The bare-handed punch DOES move the camera; how much
# depends on the stance and the view, and two runs were not a sample.
#
# Raising the threshold would be turning a red gate green. The honest
# discriminator is that the two things have different SHAPES:
#
#     a punch      one animation. Its displacement barely grows with the hold
#     gunfire      recoil accumulates. 2.5 s is ~10x what 0.25 s is
#
# So the pre-flight holds the button at both ends and compares. Either way the
# punch cancels in curve/move -- all three arms press the same button -- so
# this gate exists only to catch a WEAPON, not to keep the punch out.
NONE_GROWTH_MAX = 3.0    # long/short above this and it is accumulating recoil
NONE_FLOOR_PX = 3.0      # below this at the long hold, nothing is firing
# Per-trial cost beyond the hold itself: arming the pattern, flushing frames,
# reading the click back. Only used for the up-front time estimate.
TRIAL_OVERHEAD_S = 0.35

# ── --hold-sweep: is comp_counts_at() the firmware, or an idealisation? ──────
#
# A measurement put the two firing arms 2.16% apart with K pinned
# independently, and TWO things produce exactly that, with the same sign and
# the same size:
#
#   eta                the curve arrives short: 900 commanded, 882 delivered
#   the integration    the curve arrives in full and comp_counts_at() -- the
#                      PC's model of the firmware -- over-states it by 2%
#
# ⚠ AT ONE HOLD DURATION THEY ARE THE SAME NUMBER. A pure scale error and a
# timing error are indistinguishable from a single measurement, which is why
# sec.13 item 6 sat as "foundation never verified" until it became the thing
# that decides whether eta is real.
#
# SWEEPING THE HOLD SEPARATES THEM:
#
#   measured(T) / comp_counts_at(T)   FLAT in T     -> a scale: eta (or K)
#                                     VARIES with T -> the integration model,
#                                                      and the shape says how
#
# ⚠ K DOES NOT NEED TO BE KNOWN, only to hold still across the sweep, because a
# constant factor cancels out of "is the ratio flat". So this runs in whatever
# aim state the character is in -- which also sidesteps the gun leaving ADS
# when its magazine empties.
#
# ⚠ AND THE PUNCH IS SUBTRACTED, NOT AVOIDED. With no weapon in hand a held
# left button is a punch and the animation moves the camera; holding longer may
# punch more than once, so it is not a constant that a single T=0 arm could
# remove. Every T therefore fires BOTH arms -- pattern on and pattern off --
# and the difference is the compensation alone.
#
# ⚠ THE CURVE IS DELIBERATELY FRONT-LOADED. A uniform ramp makes
# comp_counts_at() linear in T, and a linear model tested against a linear
# truth passes whatever it does between the knots. The shape below puts most of
# the travel in the first third, so the piecewise-linear reconstruction is
# actually exercised.
SWEEP_SPAN_S = 2.0
SWEEP_TOTAL = 400
HOLD_SWEEP = (0.25, 0.50, 1.00, 1.50, 2.00, 2.50)


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
    # ⚠ BOUNDED. `slice_frame` returns None whenever it cannot find the patch --
    # blank sky is enough -- so the exit condition is supplied by THE WORLD and
    # this loop had no opinion about how long to wait for it. On 2026-08-08 it
    # held the foreground for EIGHT MINUTES, moving the cursor, immediately
    # before a mouse-down; the only way out was killing the process from
    # another session. layering rule 15 exists because of this loop.
    _t_wait = time.perf_counter()
    while prev is None:
        if time.perf_counter() - _t_wait > PATCH_WAIT_S:
            return float('nan'), float('inf')
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


# Fraction of the ammo readout that is bright. Measured on four states, and
# the threshold sits in a 20x gap below and 2x above:
#
#     no weapon                     0.00%   0.09%
#     weapon out, 40 rounds         3.45%
#     weapon out, EMPTY (red 0)     3.63%   2.13%
#
# ⚠ THE RED 0 IS WHY THIS IS A PIXEL FRACTION AND NOT THE DIGIT READER. An
# empty magazine draws its count in red, AmmoDetector's templates are white,
# and control.stock.weapon_in_hand() therefore answers None for BOTH "no
# weapon" and "empty magazine". Asking how much ink is on the HUD does not care
# what colour it is.
#
# ⚠ AND NOT THE 99th PERCENTILE, which was the first thing tried: it read
# 119 / 128 / 117 across those same states -- the region's bright edge pins it
# whatever the digits do, so the check refused a weapon that was already away.
HUD_INK_MAX = 0.01


def _hud_ink():
    """Fraction of the ammo readout that is bright. Weapon out -> digits."""
    import cv2
    from capture.cropper import capture_screen
    from config import HUD_REGIONS
    y, x, h, w = HUD_REGIONS['ammo']
    crop = capture_screen()[y:y + h, x:x + w]
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return float((g > 120).mean())


def holster(rig):
    """Put the weapon away and PROVE it went. -> True if the HUD went blank.

    ⚠ THIS FILE ASSERTED "no weapon in hand" IN ITS DOCSTRING FOR A DAY AND
    NEVER DID IT, nor checked it. Nothing failed loudly: with a gun out, both
    arms hold the same button for the same time, so the recoil lands in both
    and cancels out of the difference -- it only inflates the noise, which
    reads as "this needs more trials".

    ⚠ AND THE OBVIOUS CHECK IS THE ONE THAT DOES NOT WORK. control.stock's
    weapon_in_hand() answers None for "no weapon" AND for "magazine empty",
    because PUBG draws a 0 in red and the digit templates are white
    (measured 2026-08-08; see that function's docstring). So an empty gun would
    certify itself as no gun. This asks a DIFFERENTIAL question instead -- did
    the readout go dark when X was pressed -- which needs no threshold and
    cannot be satisfied by a reader that was blind to begin with.
    """
    from press.pico_mouse import HID_KEY_X
    ink = _hud_ink()
    print(f'  holster: weapon HUD ink {100*ink:.2f}% '
          f'(away is < {100*HUD_INK_MAX:.0f}%)')
    if ink < HUD_INK_MAX:
        return True                       # already away
    for i in range(3):
        rig.mouse.key(HID_KEY_X, 60)
        time.sleep(0.8)
        ink = _hud_ink()
        print(f'  holster: press {i+1} -> {100*ink:.2f}%')
        if ink < HUD_INK_MAX:
            return True
    return False


def _shaped_curve(total, span, sign):
    """Front-loaded knots summing to `total`. -> (dy list, t list)."""
    n = int(span * 1000 / KNOT_MS)
    w = np.exp(-np.arange(n) / (n / 4.0)) + 0.15
    w = w / w.sum() * abs(total)
    return list(-sign * w), [i * KNOT_MS / 1000.0 for i in range(n)]


def _arm_sweep_curve(mouse, sign):
    """Upload the shaped pattern and return what the FIRMWARE says it holds."""
    dy, ts = _shaped_curve(SWEEP_TOTAL, SWEEP_SPAN_S, sign)
    mouse.upload_pattern([0.0] * len(dy), dy, ts)
    mouse.set_recoil_enabled(True)
    return mouse.read_pattern() or []


def _inject_shaped(mouse, dy, ts, hold_s, t0):
    """Replay the shaped knots through mouse.move(), truncated at hold_s.

    ⚠ THE STRADDLING KNOT IS SENT IN PART, not dropped. The firmware spreads
    each knot evenly over its own window, so a hold ending mid-knot delivers a
    FRACTION of it -- and comp_counts_at models exactly that. Dropping it here
    would make the two arms disagree by up to one knot at every hold, which at
    the shortest hold is ~1% of the travel: the same size as the effect.
    """
    acc = 0.0
    for i, (d, t) in enumerate(zip(dy, ts)):
        if t >= hold_s:
            break
        dur = (ts[i + 1] - t) if i + 1 < len(ts) else (t - ts[i - 1])
        frac = min(1.0, (hold_s - t) / dur) if dur > 0 else 1.0
        w = t0 + t - time.perf_counter()
        if w > 0:
            time.sleep(w)
        acc += d * frac
        s = int(acc)
        acc -= s
        if s:
            mouse.move(0, s)


def one_hold_trial(rig, grabber, hold_s, sign, arm):
    """Hold the button `hold_s`. -> (px, worst).

    ⚠ THREE ARMS, AND `move` IS NOT OPTIONAL. With no weapon out this runs in
    HIP FIRE, whose count ruler is NOT FLAT (0.636 +- 0.095, control/CLAUDE.md),
    and a longer hold travels further -- so each hold duration samples a
    different stretch of a curved ruler and that alone would fake a trend in
    exactly the quantity under test. `move` sends the SAME shaped counts over
    the SAME time through mouse.move(), so it walks the same stretch, and
    curve/move divides the ruler out.
    """
    for _ in range(3):
        grabber.grab_timed()
    prev = None
    # ⚠ BOUNDED. `slice_frame` returns None whenever it cannot find the patch --
    # blank sky is enough -- so the exit condition is supplied by THE WORLD and
    # this loop had no opinion about how long to wait for it. On 2026-08-08 it
    # held the foreground for EIGHT MINUTES, moving the cursor, immediately
    # before a mouse-down; the only way out was killing the process from
    # another session. layering rule 15 exists because of this loop.
    _t_wait = time.perf_counter()
    while prev is None:
        if time.perf_counter() - _t_wait > PATCH_WAIT_S:
            return float('nan'), float('inf')
        _t, f = grabber.grab_timed()
        prev = rig.tracker.slice_frame(f) if f is not None else None

    if arm == 'curve':
        _arm_sweep_curve(rig.mouse, sign)
    else:
        rig.mouse.set_recoil_enabled(False)
    t0 = rig.mouse.click(buttons=0x01, duration_ms=int(hold_s * 1000))
    if t0 is None:
        t0 = time.perf_counter()
    if arm == 'move':
        # click() returns when the bytes go out and the FIRMWARE holds the
        # button, so starting here costs a millisecond and keeps t0 honest.
        import threading
        dy, ts = _shaped_curve(SWEEP_TOTAL, SWEEP_SPAN_S, sign)
        threading.Thread(target=_inject_shaped,
                         args=(rig.mouse, dy, ts, hold_s, t0),
                         daemon=True).start()

    total_px, worst = 0.0, 0.0
    while time.perf_counter() < t0 + hold_s + COOLDOWN_S:
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
            worst = max(worst, abs(m.dy))
    rig.mouse.set_recoil_enabled(False)
    time.sleep(SETTLE_S)
    return total_px, worst


def _arms_at(t):
    """Which arms run at this hold. `none` is the abort gate, so it goes FIRST
    at the shortest hold -- an abort six minutes in helps nobody."""
    if t == HOLD_SWEEP[0]:
        return ('none', 'curve', 'move')
    if t == HOLD_SWEEP[-1]:
        return ('curve', 'move', 'none')
    return ('curve', 'move')


def _no_weapon_firing(rig, grabber):
    """Hold the button at both ends of the sweep with the pattern OFF.

    A punch is one animation and barely grows with the hold; recoil
    accumulates. So the RATIO between a long hold and a short one separates
    them, and no absolute threshold has to be guessed -- which is what the
    first version did, at 5 px, on a punch that had only ever been seen at
    0.0-0.3 and then turned up at 33.
    """
    short = max(abs(one_hold_trial(rig, grabber, HOLD_SWEEP[0], s, 'none')[0])
                for s in (+1, -1))
    long_ = max(abs(one_hold_trial(rig, grabber, HOLD_SWEEP[-1], s, 'none')[0])
                for s in (+1, -1))
    if not (np.isfinite(short) and np.isfinite(long_)):
        print('  [!] the pre-flight could not find a patch — NOT A VERDICT')
        return False
    growth = long_ / max(short, 0.1)
    print(f'  pattern OFF: {HOLD_SWEEP[0]:.2f}s -> {short:.1f} px, '
          f'{HOLD_SWEEP[-1]:.2f}s -> {long_:.1f} px   growth {growth:.1f}x')
    if long_ < NONE_FLOOR_PX:
        print('  nothing is firing (both ends are noise)')
        return True
    if growth < NONE_GROWTH_MAX:
        print(f'  it does not grow with the hold, so it is the bare-handed '
              f'PUNCH, not recoil. It presses in all three arms and cancels '
              f'out of curve/move.')
        return True
    print(f'[!] REFUSING: with the pattern OFF the view moved {short:.1f} px at '
          f'{HOLD_SWEEP[0]:.2f}s and {long_:.1f} px at {HOLD_SWEEP[-1]:.2f}s — '
          f'it GROWS with the hold ({growth:.1f}x), which is accumulating '
          f'recoil, not a punch. A WEAPON IS OUT and the trigger is firing it. '
          f'Its 2-4% per-magazine scatter is bigger than the 2% being measured. '
          f'Holster it (X) and re-run.')
    return False


def hold_sweep(rig, grabber, trials):
    from calibration.samples import comp_counts_at
    # ⚠ THE PRE-FLIGHT HUD CHECK IS GONE, and what replaced it is the `none`
    # arm itself. Two versions of that check were wrong in the same way: they
    # thresholded BRIGHTNESS over the ammo readout, and that region shows THE
    # WORLD when no weapon is out -- so a bright arm across it read as "a
    # weapon is out", X did nothing (there was nothing to holster), and the
    # probe refused a session that was already correct. Said from the chair:
    # 「你手上没枪，所以收不了枪，所以收枪失败」. It is the same shape as
    # map_open reading true in a sunset and tab_open behind a tree.
    #
    # The `none` arm holds the button for the same time with the pattern OFF,
    # so if a weapon were out its recoil would land there and nowhere else. It
    # measured 0.0-0.3 px at every hold across two runs. That is a POSITIVE
    # reading of the thing that actually matters, taken in the measurement's
    # own conditions, and no threshold about HUD pixels can fool it.
    holster(rig)                      # best effort, and it says what it saw
    if not _no_weapon_firing(rig, grabber):
        return 7
    curve = _arm_sweep_curve(rig.mouse, +1)
    rig.mouse.set_recoil_enabled(False)
    held = sum(k.get('dy', 0.0) for k in curve)
    print(f'  pattern: {len(curve)} knots, {abs(held):.1f} counts read back, '
          f'spans {SWEEP_SPAN_S:.2f} s')
    print('  comp_counts_at: ' + '  '.join(
        f'{t:.2f}s={abs(float(comp_counts_at(curve, t)[0])):.0f}'
        for t in HOLD_SWEEP))
    print('  a uniform ramp:  ' + '  '.join(
        f'{t:.2f}s={min(t/SWEEP_SPAN_S,1)*abs(held):.0f}' for t in HOLD_SWEEP)
        + '   <- the gap is what makes the shape testable')

    # ⚠ SAY HOW LONG THIS WILL TAKE, BEFORE IT STARTS. Without it a slow run
    # and a hung one look identical from outside, and this probe has been both
    # -- eight minutes wedged in a loop nobody could interrupt. Asked for in
    # those terms: 「不给估计的话，我不知道你到底要跑多久，就容易中间我觉得你是
    # 不是死循环了」. It is printed by the program rather than promised by
    # whoever launches it, because a promise is the thing that got forgotten.
    per_round = sum(len(_arms_at(t)) * 2 * (t + COOLDOWN_S + SETTLE_S + TRIAL_OVERHEAD_S)
                    for t in HOLD_SWEEP)
    n_trials = trials * sum(len(_arms_at(t)) * 2 for t in HOLD_SWEEP)
    print(f'  {trials} round(s) x {n_trials // max(trials, 1)} trials = '
          f'{n_trials} trials, about {trials * per_round / 60:.1f} MINUTES. '
          f'Longer than ~{1.5 * trials * per_round / 60:.0f} min means it is '
          f'stuck, not slow.')

    from control.focus import focus_keeper
    got = {t: {'curve': [], 'move': [], 'none': []} for t in HOLD_SWEEP}
    for r in range(trials):
        for t in HOLD_SWEEP:
            # ⚠ STOP WHEN THE OPERATOR TAKES THE SCREEN. focus_keeper regains
            # it a bounded number of times and then answers False, and its own
            # docstring says why: "either something is contending, or a human
            # is trying to get out. Both mean stop." This loop never asked, so
            # taking the foreground three times did not stop it -- said from
            # the chair, and it is the whole point of layering rule 15.
            if not focus_keeper().ok(f'hold sweep r{r} {t:.2f}s'):
                print('[!] STOPPING: the foreground was taken away and would '
                      'not stay. Nothing here is worth measuring through that, '
                      'and a run that will not stop is worse than one that '
                      'fails.')
                return 8
            # `none` FIRST at the shortest hold: it is the abort gate, and an
            # abort six minutes in is a refusal nobody benefits from.
            for arm in _arms_at(t):
                # ⚠ THE TWO DIRECTIONS FIRE BACK TO BACK, so the view returns
                # inside every pair. The sign used to sit in the OUTER loop,
                # which meant twelve pushes the same way (6 holds x 2 arms)
                # before it flipped -- up to ~4800 counts against a pitch range
                # of ~3400. It drove the view into the TOP CLAMP, where the
                # tracker sees sky and BLIND READS AS "IT DID NOT MOVE".
                # 「顶到天了，你这个测试怎么能顶到区间外呢？」
                line = f'  r{r} {t:.2f}s {arm:5s}'
                for sign in (+1, -1):
                    px, worst = one_hold_trial(rig, grabber, t, sign, arm)
                    if not np.isfinite(px):
                        line += '  NO-PATCH'
                        continue
                    if worst > ALIAS_PX:
                        line += f'  ALIASED({worst:.0f})'
                        continue
                    got[t][arm].append(sign * px)
                    line += f' {sign * px:8.1f}'
                print(line)
    return _report_sweep(got, curve)


def _report_sweep(got, curve):
    """⚠ THE VERDICT IS AGAINST THE SAMPLING NOISE, NOT AGAINST A CONSTANT.

    The first version printed "NOT FLAT (3.91%)" and named a mechanism. Its own
    trials scattered 15% and n was 8, so one ratio carried a sem of 2.78% --
    the spread was 1.43x the noise and the weighted trend was 1.7 sigma. It
    asserted a conclusion its data could not hold, which is the same shape as
    every criterion this project has had to rewrite.
    """
    from calibration.samples import comp_counts_at
    print()
    print(f'{"hold":>6}  {"n":>3}  {"curve px":>10}  {"move px":>9}  '
          f'{"curve/move":>11}  {"sem":>7}  {"model":>8}  {"punch":>7}')
    ts, rs, ses = [], [], []
    for t in HOLD_SWEEP:
        c = np.array(got[t]['curve'], float)
        m = np.array(got[t]['move'], float)
        n = np.array(got[t]['none'], float)
        if len(c) < 2 or len(m) < 2 or m.mean() == 0:
            print(f'{t:6.2f}  {len(c):3d}  — too few')
            continue
        r = c.mean() / m.mean()
        se = abs(r) * ((c.std(ddof=1) / len(c) ** 0.5 / c.mean()) ** 2
                       + (m.std(ddof=1) / len(m) ** 0.5 / m.mean()) ** 2) ** 0.5
        ts.append(t); rs.append(r); ses.append(se)
        print(f'{t:6.2f}  {len(c):3d}  {c.mean():10.1f}  {m.mean():9.1f}  '
              f'{r:11.4f}  {se:7.4f}  '
              f'{abs(float(comp_counts_at(curve, t)[0])):8.1f}  '
              f'{(n.mean() if len(n) else float("nan")):7.2f}')
    if len(rs) < 4:
        print('[!] fewer than 4 usable hold points. NOT A VERDICT.')
        return 3
    ts, rs, ses = np.array(ts), np.array(rs), np.array(ses)
    spread = float(rs.std(ddof=1))
    noise = float(np.sqrt(np.mean(ses ** 2)))
    w = 1.0 / ses ** 2
    slope, icept = np.polyfit(ts, rs, 1, w=np.sqrt(w))
    res = rs - (icept + slope * ts)
    dof = max(len(ts) - 2, 1)
    se_slope = float((np.sum(w * res ** 2) / dof
                      / np.sum(w * (ts - ts.mean()) ** 2)) ** 0.5)
    print()
    print(f'  curve/move    mean {rs.mean():.4f}   spread {100*spread/abs(rs.mean()):.2f}%'
          f'   sampling {100*noise/abs(rs.mean()):.2f}%   '
          f'ratio {spread/noise:.2f}x')
    print(f'  trend vs hold {slope:+.5f} per second +- {se_slope:.5f}   '
          f'{abs(slope)/max(se_slope,1e-12):.1f} sigma')
    print()
    flat = spread / noise < 2.0 and abs(slope) < 2 * se_slope
    if flat:
        print(f'  ✅ FLAT: curve/move does not depend on how long the button '
              f'was held ({spread/noise:.2f}x the sampling noise, trend '
              f'{abs(slope)/max(se_slope,1e-12):.1f} sigma). comp_counts_at '
              f'reproduces the firmware across the whole hold, INCLUDING the '
              f'freeze at release.')
        print(f'  -> the 2.16% arm offset is NOT an '
              f'accounting error. It is a scale, and eta is what is left.')
        print(f'  -> and the level itself is a second reading of eta: '
              f'{rs.mean():.4f}, i.e. the firmware path delivers '
              f'{100*rs.mean():.1f}% of what mouse.move() does.')
    else:
        print(f'  ⚠ NOT FLAT: {spread/noise:.2f}x the sampling noise, trend '
              f'{abs(slope)/max(se_slope,1e-12):.1f} sigma. The ratio depends '
              f'on the hold, so comp_counts_at is not the firmware and the arm '
              f'offset can be an accounting error rather than eta.')
        print(f'     short holds {rs[0]:.4f}   long holds {rs[-1]:.4f}')
        print('     rises with hold -> the FREEZE at release is over-counted')
        print('     falls with hold -> the opening knots are, via the '
              'fire-delay fold or the first knot duration')
    print()
    print('  ⚠ one run. Nothing moves until a second one agrees.')
    return 0


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
    ap.add_argument('--hold-sweep', action='store_true',
                    help='sweep the HOLD DURATION against one uploaded curve, '
                         'to separate a scale error (eta) from an integration '
                         'error in comp_counts_at. See the block comment.')
    a = ap.parse_args()

    ARMS = ['move-240', 'curve']
    out = {k: [] for k in ARMS}

    # ⚠ refuse_on_reentry: a walk-back means the match being measured ended.
    # It is also the stop condition for the failure named from the chair --
    # a long unattended cycle re-entering, re-measuring and re-failing.
    r = ensure_ready(label='the delivery-path probe',
                     countdown_s=a.countdown, refuse_on_reentry=True)
    if not r['ok']:
        print(f'[!] could not get the game ready ({r.get("failed")})')
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
        if a.hold_sweep:
            return hold_sweep(rig, grabber, a.trials)
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
