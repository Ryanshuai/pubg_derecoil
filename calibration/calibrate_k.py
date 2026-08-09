"""Calibrate K (screen px per mouse count) and validate the view tracker.

This is the gate for the whole screen-observation approach: it injects a
known number of mouse counts through the Pico and checks what the tracker
reads back off the screen. If K is not linear and repeatable here, nothing
downstream (residual learning, curve updates) can work.

It answers three things at once:
  1. K for the current sight  — the px/count scale everything else needs
  2. linearity                — is view rotation proportional to counts?
  3. real inter-frame accuracy — every prior measurement was same-frame
                                 integer shifts, i.e. an optimistic bound

Run it in the training range, standing still, aimed at something with
structure (buildings, treeline, rocks) — NOT at open sky or a blank wall,
and NOT while anything is moving through the patches.

    python calibration/calibrate_k.py
    python calibration/calibrate_k.py --amounts 50,100,200 --repeats 5
"""
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from config import RECOIL_PATCH
from control.focus import game_focused
from control.session import ensure_ready
from capture.cropper import make_grabber
from detector.view_tracker import ViewTracker, MagazineRecorder
from press.pico_mouse import get_mouse

cv2.setNumThreads(1)

# Phases are wall-clock, NOT frame counts: DXGI in video_mode re-serves the
# previous frame without blocking whenever the screen is idle, so a
# frame-counted loop races through a static scene (measured 1474 "fps") and
# would end the cooldown long before the view has actually settled.
WARMUP_S = 0.10         # before injection starts
INJECT_S = 0.15         # spread of the injected counts
COOLDOWN_S = 0.35       # after injection, for the view to settle
INJECT_STEPS = 20       # sub-steps the counts are broken into
DEAD_PX = 2.0           # a trial moving less than this measured nothing
DEAD_TRIALS = 3         # consecutive dead trials that end the run
SETTLE_S = 0.45         # pause between trials (and after the reset move)
ADS_SETTLE_S = 0.40     # time for the scope-in animation before measuring


# `foreground_name()` lived here: window title + exe of whatever has focus,
# for printing when the guard says no. Deleted 2026-08-07 with zero callers --
# control.focus.window_info() is the same read, and control.focus.game_focused()
# is the guard. The lesson it carried is in tools/CLAUDE.md and does not need a
# function to hold it: this file used to DECIDE with
# `any(k in (title+exe).lower() for k in ('battlegrounds','pubg','tslgame'))`,
# and the repository is called pubg_derecoil, so an editor showing the path
# answered yes. That matters most here of all -- K is the counts-to-degrees map
# every recoil curve is scaled by, and an unfocused run measures zero motion
# while reporting focused=True.


def _inject(mouse, counts, n_steps, duration, t_start, log):
    """Injection runs on its own thread so its timing is independent of how
    fast grab() happens to return."""
    accum = 0.0
    step = counts / n_steps
    for i in range(n_steps):
        target = t_start + duration * i / n_steps
        while time.perf_counter() < target:
            pass
        accum += step
        sent = int(accum)               # sub-count remainder carries over,
        accum -= sent                   # same as press.Press does
        if sent:
            mouse.move(0, sent)
        log.append((time.perf_counter() - t_start, sent))


_ADS_DET = [None]


def _scoped_now():
    """Is the sight up, right now? Reads the crosshair's ABSENCE off a frame.

    Its own grabber: the tracker's may be banded to the patch rows, and the
    crosshair lives in the middle of the screen. 0.32 ms, and only called
    between trials.
    """
    from detector.ads_detector import AdsDetector
    from capture.cropper import capture_screen
    if _ADS_DET[0] is None:
        _ADS_DET[0] = AdsDetector()
    return bool(_ADS_DET[0].scoped(capture_screen()))


def _ensure_scoped(mouse, tries=3):
    """Toggle until the screen says the sight is up. -> did it get there.

    Not `click and assume`: right-click is a toggle, so a blind click is as
    likely to put the sight DOWN as up, and the reading is the only thing
    that can tell which happened.
    """
    for _ in range(tries):
        if _scoped_now():
            return True
        mouse.click(buttons=0x02, duration_ms=60)
        time.sleep(ADS_SETTLE_S)
    return _scoped_now()


def run_trial(grabber, tracker, mouse, counts, dry_run=False, ads=False,
              inject_s=None, keep_duplicates=False):
    """Inject `counts` vertically over `inject_s` and capture throughout.

    ⚠ THE RATE IS A PARAMETER BECAUSE THE DEFAULT ALIASES. The view tracker's
    correlation is unambiguous only up to RECOIL_PATCH_H = 256 px between
    frames; past that it wraps by EXACTLY one patch height, every patch wraps
    the same way so they still agree, and the trial comes back with a
    plausible-looking K that is short by 256 px. 8 of the 22 usable rows in
    the stored red_dot runs are aliased -- see tools/audit_k.py, which found
    it in two rows of one run differing by 256.01.

    ⚠ `max_abs_frame` CANNOT CATCH IT. An aliased pair reports a SMALL
    displacement, so the statistic that would flag a too-fast frame is the one
    the aliasing hides in: agreeing and disagreeing rows have the same median
    max_abs_frame (91 vs 92 px). The defence is the rate, not a filter.

    With ads=True the Pico holds right-click for the whole trial *including*
    the reset move, so that the reset is undone at the same K it was applied
    at — resetting in hip-fire after injecting while scoped would leave the
    view drifting a little further every trial.

    Returns (MagazineRecorder, injection_log).
    """
    # ⚠ THIS FLAG IS THE DIFFERENCE BETWEEN THE TWO PATHS, and until 2026-08-08
    # it was not a flag. K is calibrated here with duplicates DROPPED, while
    # collect_timed measures every presented frame (drop_duplicates=False, and
    # its comment says that is the point of the synchronous grabber). Dropping
    # duplicates NORMALISES the per-frame displacement -- a rate sweep from
    # 0.3 s to 3.0 s moved px/frame only from 10.2 to 6.4, because the frames
    # where the view barely moved were being discarded. So K has only ever been
    # measured in a regime the recoil measurement never occupies, and eta is a
    # multiplicative deficit that lands on exactly the arm where y_obs is the
    # whole measurement. That is a hypothesis this flag can now test.
    rec = MagazineRecorder(tracker, drop_duplicates=not keep_duplicates)
    log = []
    inject_s = INJECT_S if inject_s is None else float(inject_s)
    # ⚠ STEPS SCALE WITH THE DURATION. Spreading the SAME 20 sub-steps over a
    # longer window leaves each individual step exactly as large -- the peak
    # per-frame displacement, which is the thing that aliases, would not move
    # at all. One step every 5 ms.
    n_steps = max(INJECT_STEPS, int(inject_s * 200))
    total = WARMUP_S + inject_s + COOLDOWN_S

    scoped_before = scoped_after = None
    if ads:
        # ⚠ RIGHT-CLICK IS A TOGGLE HERE, NOT A HOLD, and this used to assume
        # the opposite: one long `click(0x02, hold_ms)` per trial. A toggle
        # turns the sight ON for trial 1, OFF for trial 2, ON for trial 3 --
        # so a run alternated between scoped and hip-fire and reported the
        # mixture as one K. That is the 38-53% CV and R^2 0.72 measured on
        # 2026-08-05, and it is why the same evening's red-dot and VSS runs
        # disagreed by 2x when hip-fire should look identical through either.
        #
        # Holding right-click is not ADS in this setup either -- it is
        # shoulder aim, a third state. So the state has to be CHECKED, not
        # commanded: AdsDetector answers off one frame in 0.32 ms.
        # Try to get the sight up, but do NOT refuse the trial if the check
        # says no -- the state is RECORDED and travels with the result, which
        # is what the reader actually needs. A gate here dropped 4 of 5 trials
        # at 240 counts and 5 of 5 at -240 on 2026-08-05, and the detector it
        # gates on has never been validated during large view motion, so the
        # drops were as likely to be its own false negatives as real.
        scoped_before = _ensure_scoped(mouse)

    t0 = time.perf_counter()
    th = None
    if not dry_run:
        th = threading.Thread(target=_inject, daemon=True,
                              args=(mouse, counts, n_steps, inject_s,
                                    t0 + WARMUP_S, log))
        th.start()

    while time.perf_counter() - t0 < total:
        frame = grabber.grab()
        rec.push(time.perf_counter(), frame)

    if th is not None:
        th.join(timeout=1.0)
    if ads:
        # AFTER as well as before: the sight can come down mid-trial, and a
        # trial that started scoped and ended hip-fire is not a measurement of
        # either. Recorded rather than corrected -- the caller drops it.
        scoped_after = _scoped_now()
    return rec, log, (scoped_before, scoped_after)


def summarise(rec, res, log, counts):
    """Fold one trial into the numbers that matter."""
    dy = np.asarray(res.dy, dtype=float)
    finite = np.isfinite(dy)
    cum = float(np.nansum(dy))
    # Per-patch cumulative, before any rejection. If a scope applies optical
    # distortion these will disagree systematically by position rather than
    # randomly, which is the difference between "noisy patch" and "wrong
    # place to put a patch".
    pp = np.asarray(res.per_patch_dy, dtype=float)
    pp_cum = np.nansum(pp, axis=0).tolist() if pp.size else []
    # Mouse down (+counts) rotates the view down, so screen content moves UP,
    # which reads negative under this module's sign convention.
    k = (-cum / counts) if counts else float('nan')
    return {
        'counts': counts,
        'cum_px': cum,
        'K': k,
        'n_frames': len(dy),
        'n_dup': rec.n_duplicates,
        'span_s': round(rec.span_s(), 4),
        'fps': round(rec.effective_fps(), 1),
        'n_nan': int((~finite).sum()),
        'max_abs_frame': float(np.nanmax(np.abs(dy))) if finite.any() else float('nan'),
        'mean_mad': float(np.mean(res.mad)) if res.mad else float('nan'),
        'n_rejected': int(np.sum(res.n_rejected)),
        'n_out_of_range': int(np.sum(res.out_of_range)),
        'n_low_gate': int(np.sum(res.gates)),
        'injected_total': int(sum(s for _, s in log)),
        'per_patch_cum': [round(v, 3) for v in pp_cum],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--amounts', default='50,100,200,300',
                    help='comma-separated count magnitudes to test')
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--keep-duplicates', action='store_true',
                    help='measure every presented frame, the way collect_timed '
                         'does, instead of dropping frames where the view did '
                         'not change. K has only ever been calibrated with '
                         'them DROPPED, which normalises the per-frame '
                         'displacement to 6-10 px -- a regime a recoil burst '
                         '(~3 px/frame, with many near-zero frames) never '
                         'occupies.')
    ap.add_argument('--inject-sweep', default=None,
                    help='comma-separated injection durations, ROTATED PER '
                         'TRIAL. Fixing the counts and varying the duration '
                         'varies the PER-FRAME displacement, which is the one '
                         'thing K is silently assumed not to depend on: it is '
                         'calibrated around 8 px/frame and a recoil burst runs '
                         'at ~3. If the correlator has a magnitude-dependent '
                         'bias, K differs between those regimes -- and since '
                         'y_obs = px/K, that lands entirely on the '
                         'compensation-OFF arm and not at all on the ON arm, '
                         "which is exactly eta's signature. Rotated rather "
                         'than one run per duration, because every cross-arm '
                         'comparison this project made across sessions turned '
                         'out to be a comparison of sessions.')
    ap.add_argument('--inject-s', type=float, default=INJECT_S,
                    help='seconds to spread the injection over. The default '
                         f'{INJECT_S} puts 480 counts at ~50 px/frame NOMINAL '
                         'and observed peaks of 124, against a correlator that '
                         'aliases by RECOIL_PATCH_H = 256 px and hides it. '
                         'One second per injection puts it under 2 px/frame.')
    ap.add_argument('--countdown', type=int, default=6)
    ap.add_argument('--label', default='', help='tag for the output files '
                    '(e.g. hipfire, reddot, 4x)')
    ap.add_argument('--dry-run', action='store_true',
                    help='capture without injecting — measures the noise '
                         'floor of a stationary view')
    ap.add_argument('--ads', action='store_true',
                    help='have the Pico hold right-click for each trial, so '
                         'the whole run happens scoped in. Assumes '
                         'hold-to-ADS (the PUBG default), not toggle.')
    ap.add_argument('--patch-xs', default='',
                    help='override patch x positions (comma-separated). '
                         'Needed for magnified scopes, where the default set '
                         'lands on the scope body — those patches are fixed to '
                         'the screen and read zero, dragging the median with '
                         'them.')
    args = ap.parse_args()

    amounts = [int(a) for a in args.amounts.split(',') if a.strip()]
    inject_sweep = ([float(x) for x in args.inject_sweep.split(',')]
                    if args.inject_sweep else None)
    if inject_sweep:
        print(f'injection durations rotated per trial: {inject_sweep} s')
    xs = ([int(x) for x in args.patch_xs.split(',') if x.strip()]
          if args.patch_xs else None)
    tracker = ViewTracker(patch_xs=xs)
    if xs and len(xs) % 2 == 0:
        print(f"[!] {len(xs)} patches (even) — the median averages the middle "
              "two, so\n    a single bad patch shifts the result instead of "
              "being outvoted.")

    print(f"patches   : {len(tracker.xs)} x {tracker.patch}px at "
          f"y={tracker.band_y}, x={tracker.xs}")

    try:
        mouse = get_mouse()
    except Exception as e:
        print(f"\n[!] mouse backend unavailable: {e}")
        print("    Connect the Pico. There is no software fallback: PUBG "
              "reads aiming off raw HID,")
        print("    so a SendInput backend measures nothing. (It existed "
              "until 2026-08-08 and this")
        print("    message used to recommend it.)")
        return 1

    grabber, paced = make_grabber(tracker.regions())
    print(f"grabber   : {type(grabber).__name__} (paced={paced})")
    if not paced:
        print("[!] GDI fallback — ~55 fps instead of 144. Results still valid, "
              "but fewer samples per trial.")

    print(f"\nTrials    : {len(amounts)} amounts x {args.repeats} repeats "
          f"x2 directions = {len(amounts) * args.repeats * 2}")
    print(f"Per trial : {WARMUP_S}+{args.inject_s}+{COOLDOWN_S} s "
          f"(wall-clock, not frame-counted)")
    # ensure_ready rather than a countdown plus a focus check: it takes the
    # foreground itself (the countdown survives as its fallback), and its match
    # leg moves to the 200m lane -- which is what "aim at something with
    # structure" was asking a human to arrange. K is measured off the picture
    # moving, so a screen full of sky reads zero at every amount, exactly like
    # an unfocused one does.
    #
    # Skipped under --dry-run for the same reason the focus check was: that
    # mode injects nothing, it measures the noise floor.
    if not args.dry_run:
        print("\n>>> Taking the foreground and moving to the 200m lane. "
              "Do not touch the mouse until it finishes.")
        ready = ensure_ready(label='calibrate_k', countdown_s=args.countdown)
        if not ready['ok']:
            print(f"\n[!] ABORT: could not get the game ready — failed at "
                  f"{ready['failed']!r}. Injected input is ignored while the "
                  f"game is unfocused, so every trial would read zero.")
            grabber.close()
            return 1

    rows = []
    raw = []
    lost_focus = 0
    n_dead = 0
    try:
        for amount in amounts:
            for sign in (+1, -1):
                counts = amount * sign
                for r in range(args.repeats):
                    inj = (inject_sweep[r % len(inject_sweep)]
                           if inject_sweep else args.inject_s)
                    rec, log, ads_state = run_trial(
                        grabber, tracker, mouse, counts,
                        args.dry_run, args.ads, inject_s=inj,
                        keep_duplicates=args.keep_duplicates)
                    if args.ads and not all(ads_state):
                        print(f"    [!] {counts:+d} #{r}: ADS "
                              f"{ads_state[0]} -> {ads_state[1]} — kept, but "
                              f"the state is not what was asked for")
                    focused = args.dry_run or game_focused()
                    if not focused:
                        lost_focus += 1
                    res = rec.finish()
                    # ⚠ A TRIAL THAT MOVED NOTHING MEASURED NOTHING, and this
                    # has to stop the run rather than be averaged in. On
                    # 2026-08-08 all 32 trials read ~0.01 px because the SPAWN
                    # TOOL panel was open: it is MODAL to the world, so the HUD
                    # is drawn, the character does not move, and the correlator
                    # returns a beautiful zero. Two minutes of injection into a
                    # menu, and the final verdict (R^2 -0.019, CV 556%) only
                    # said so at the END.
                    #
                    # ⚠ THE POINT IS THAT THIS GATE NAMES NO MECHANISM. The
                    # panel is one way to get here; a lost window, the view
                    # against a pitch clamp, a screen full of sky, and the game
                    # simply not taking input are others, and they are
                    # indistinguishable from inside. What they share is that
                    # the counts did not reach the view -- so that is what is
                    # tested, not the list.
                    #
                    # ⚠ AND IT EXISTS BECAUSE THE READINESS CHECK IS ONCE.
                    # ensure_ready runs before the first trial and the loop
                    # then runs for two minutes; `focused` above is re-read
                    # EVERY trial while the other four legs are not. One
                    # precondition polled and four assumed, in the same loop.
                    if not args.dry_run and abs(float(np.nansum(res.dy))) < DEAD_PX:
                        n_dead += 1
                        print(f"    [!] {counts:+d} #{r}: the view did not move "
                              f"({float(np.nansum(res.dy)):+.2f} px for "
                              f"{counts:+d} counts)")
                        if n_dead >= DEAD_TRIALS:
                            print(f"\n[!] ABORT: {n_dead} trials in a row moved "
                                  f"less than {DEAD_PX} px. The injected counts "
                                  f"are not reaching the view — a modal panel "
                                  f"(SPAWN TOOL is modal to the world), the "
                                  f"window, a pitch clamp, or a featureless "
                                  f"picture. Nothing measured so far is worth "
                                  f"keeping.")
                            raise SystemExit(2)
                    else:
                        n_dead = 0
                    s = summarise(rec, res, log,
                                  counts if not args.dry_run else 1)
                    s['repeat'] = r
                    s['inject_s'] = inj
                    # ⚠ px PER FRAME IS THE INDEPENDENT VARIABLE, so it is
                    # recorded rather than re-derived later from a nominal fps.
                    s['px_per_frame'] = (abs(s['cum_px']) / max(1, s['n_frames'] - 1))
                    s['focused'] = focused
                    rows.append(s)
                    raw.append({'counts': counts, 'repeat': r,
                                'ts': [round(t - res.ts[0], 5) for t in res.ts],
                                'dy': [None if not np.isfinite(v) else round(v, 4)
                                       for v in res.dy],
                                'mad': [round(v, 4) for v in res.mad],
                                'n_rejected': res.n_rejected,
                                'inject': log})
                    print(f"  counts={counts:+5d} r{r}  cum={s['cum_px']:+9.2f}px  "
                          f"K={s['K']:7.4f}  max={s['max_abs_frame']:5.2f}  "
                          f"n={s['n_frames']:3d} dup={s['n_dup']:4d} "
                          f"fps={s['fps']:6.1f}  rej={s['n_rejected']:3d} "
                          f"gate={s['n_low_gate']:3d} oor={s['n_out_of_range']} "
                          f"nan={s['n_nan']}")

                    if not args.dry_run:
                        # Still inside the ADS hold window when args.ads.
                        mouse.move(0, -counts)     # reset the view
                    time.sleep(SETTLE_S)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        grabber.close()

    if not rows:
        return 1
    if lost_focus:
        print(f"\n[!] {lost_focus}/{len(rows)} trials ended with the game "
              "unfocused — those readings are invalid.")
    report(rows, raw, args)
    return 0


def report(rows, raw, args):
    # calibration/artifacts/k/, not next to this script: the json and the plot are measurements.
    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'calibration', 'artifacts', 'k')
    os.makedirs(out_dir, exist_ok=True)
    tag = args.label or datetime.now().strftime('%m%d_%H%M')

    counts = np.array([r['counts'] for r in rows], float)
    cum = np.array([r['cum_px'] for r in rows], float)
    ks = np.array([r['K'] for r in rows], float)

    print("\n" + "=" * 74)
    if args.dry_run:
        print("DRY RUN — stationary noise floor")
        print("=" * 74)
        span = np.array([r['span_s'] for r in rows], float)
        nfr = np.array([r['n_frames'] for r in rows], float)
        dup = np.array([r['n_dup'] for r in rows], float)
        gate = np.array([r['n_low_gate'] for r in rows], float)
        rej = np.array([r['n_rejected'] for r in rows], float)
        print(f"  |cumulative drift| per trial: mean {np.mean(np.abs(cum)):.3f} px, "
              f"max {np.max(np.abs(cum)):.3f} px")
        print(f"  worst single frame          : "
              f"{np.nanmax([r['max_abs_frame'] for r in rows]):.3f} px")
        print(f"  drift rate                  : "
              f"{np.mean(np.abs(cum) / np.maximum(span, 1e-6)):.3f} px/s")
        print(f"\n  trial span                  : {np.mean(span):.3f} s "
              f"(target {WARMUP_S + INJECT_S + COOLDOWN_S:.2f} s)")
        print(f"  unique frames per trial     : {np.mean(nfr):.1f}"
              f"   duplicates dropped: {np.mean(dup):.1f}")
        print(f"  effective new-frame rate    : "
              f"{np.mean(nfr) / np.mean(span):.1f} fps")
        print(f"  patch-frames rejected       : {np.mean(rej):.1f}/trial "
              f"(of {np.mean(nfr) * 7:.0f}), of which low-gate: {np.mean(gate):.1f}")
        print("\n  A stationary view should read ~0. Above ~1 px of drift per "
              "trial means\n  something in the patches is moving (wind, water, "
              "NPCs, UI animation).")
    else:
        print("CALIBRATION RESULT")
        print("=" * 74)
        # Trials with out-of-range frames measured a wrapped value, so their
        # cumulative is meaningless. Fitting them in poisons K and makes the
        # linearity check report a failure that is really just bad input.
        good = [r for r in rows if r['n_out_of_range'] == 0 and r['n_nan'] == 0]
        n_drop = len(rows) - len(good)
        if n_drop:
            dropped = sorted({abs(r['counts']) for r in rows
                              if r['n_out_of_range'] or r['n_nan']})
            print(f"  excluded {n_drop}/{len(rows)} trials with out-of-range "
                  f"frames (amounts: {dropped})")
            print(f"  -> those magnitudes move the view more than {RECOIL_PATCH // 2} px "
                  f"between\n     captured frames; use smaller ones or a longer "
                  f"INJECT_S.\n")
        if len(good) < 3:
            print("  [!] too few valid trials to fit. Lower --amounts.")
            good = rows
        counts = np.array([r['counts'] for r in good], float)
        cum = np.array([r['cum_px'] for r in good], float)
        ks = np.array([r['K'] for r in good], float)
        # Fit through the origin: zero counts must mean zero rotation.
        k_fit = float(np.sum(counts * -cum) / np.sum(counts * counts))
        pred = -k_fit * counts
        ss_res = float(np.sum((cum - pred) ** 2))
        ss_tot = float(np.sum((cum - np.mean(cum)) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float('nan')

        print(f"  K (fit through origin) = {k_fit:.4f} px/count")
        print(f"  R^2                    = {r2:.5f}")
        print(f"  K spread across trials = {np.nanmean(ks):.4f} "
              f"+- {np.nanstd(ks):.4f}  "
              f"(CV {100 * np.nanstd(ks) / abs(np.nanmean(ks)):.1f}%)")

        print(f"\n  {'counts':>8}{'n':>4}{'mean K':>10}{'std':>9}"
              f"{'mean cum px':>14}")
        for c in sorted(set(counts)):
            m = counts == c
            print(f"  {int(c):>8}{m.sum():>4}{np.nanmean(ks[m]):>10.4f}"
                  f"{np.nanstd(ks[m]):>9.4f}{np.mean(cum[m]):>14.2f}")

        pos = ks[counts > 0]
        neg = ks[counts < 0]
        if len(pos) and len(neg):
            asym = 200 * abs(np.nanmean(pos) - np.nanmean(neg)) / (
                abs(np.nanmean(pos)) + abs(np.nanmean(neg)))
            print(f"\n  up/down asymmetry      = {asym:.2f}%   "
                  f"(K+ {np.nanmean(pos):.4f} / K- {np.nanmean(neg):.4f})")

        print("\n  VERDICT")
        ok = True
        if r2 < 0.999:
            print(f"    [!] R^2 {r2:.5f} < 0.999 — response is not linear in "
                  "counts.\n        Check for in-game mouse smoothing/"
                  "acceleration, or over-range frames.")
            ok = False
        cv = 100 * np.nanstd(ks) / abs(np.nanmean(ks))
        if cv > 3.0:
            print(f"    [!] K varies {cv:.1f}% across trials — too noisy to "
                  "learn from.")
            ok = False
        tot_nan = sum(r['n_nan'] for r in good)
        if tot_nan:
            print(f"    [!] {tot_nan} frames had no valid patch at all.")
            ok = False
        if ok:
            print("    OK — linear, repeatable. Safe to build the residual "
                  "learner on this.")

        # What this K implies for the observer's operating envelope.
        fps = np.mean([r['fps'] for r in good])
        safe_px = RECOIL_PATCH * 3 / 8
        # Per-patch K. A scope with optical distortion makes these vary with
        # distance from the scope centre, in which case adding patches near
        # the rim makes the estimate worse, not better.
        pps = [r for r in good if r.get('per_patch_cum')]
        if pps and len(pps[0]['per_patch_cum']) > 1:
            xs_used = args.patch_xs.split(',') if args.patch_xs else None
            n_p = len(pps[0]['per_patch_cum'])
            print(f"\n  PER-PATCH K (distortion check)")
            print(f"    {'x':>7}{'dist from 1720':>16}{'mean K':>10}"
                  f"{'std':>9}{'vs median':>12}")
            per = []
            for i in range(n_p):
                kk = [(-r['per_patch_cum'][i]) / r['counts'] for r in pps]
                per.append(float(np.mean(kk)))
            med_k = float(np.median(per))
            for i in range(n_p):
                kk = [(-r['per_patch_cum'][i]) / r['counts'] for r in pps]
                x = int(xs_used[i]) + RECOIL_PATCH // 2 if xs_used else -1
                d = abs(x - 1720) if x > 0 else -1
                dev = (per[i] - med_k) / med_k * 100
                print(f"    {x:>7}{d:>16}{np.mean(kk):>10.4f}"
                      f"{np.std(kk):>9.4f}{dev:>+11.2f}%")
            spread = (max(per) - min(per)) / med_k * 100
            stds = []
            for i in range(n_p):
                kk = [(-r['per_patch_cum'][i]) / r['counts'] for r in pps]
                stds.append(float(np.std(kk)))
            med_std = float(np.median(stds))
            unstable = [i for i, s in enumerate(stds)
                        if s > max(4 * med_std, 0.05)]
            print(f"    spread across patches: {spread:.2f}%")
            # Two very different faults look alike in `spread` alone:
            #   optical distortion -> every patch is stable, K drifts with
            #                         distance from the scope centre
            #   fixed HUD overlay  -> one patch is wildly unstable because a
            #                         screen-locked reticle marking competes
            #                         with the real texture for the peak
            if unstable:
                for i in unstable:
                    x = int(xs_used[i]) if xs_used else -1
                    print(f"    -> patch x={x} is UNSTABLE (std {stds[i]:.4f} "
                          f"vs {med_std:.4f} typical).")
                print("       That is a screen-locked overlay (reticle line, "
                      "range ladder, HUD),\n       not lens distortion — move "
                      "the patch off it. The remaining patches\n       "
                      "outvoted it, so the fitted K is still usable.")
            elif spread > 3:
                print("    -> all patches stable but K varies with position: "
                      "genuine distortion.\n       Keep patches near the scope "
                      "centre.")
            else:
                print("    -> no positional bias; disagreement is noise, not "
                      "distortion.")

        print(f"\n  OPERATING ENVELOPE at K={k_fit:.4f}")
        print(f"    safe shift per frame : {safe_px:.0f} px "
              f"= {safe_px / abs(k_fit):.0f} counts")
        print(f"    at {fps:.0f} fps        : "
              f"{safe_px / abs(k_fit) * fps / 1000:.1f} counts/ms sustained")
        print(f"    a 12-count bullet    : {12 * abs(k_fit):.1f} px total, "
              f"spread over ~4 frames -> {12 * abs(k_fit) / 4:.1f} px/frame")
        print(f"    -> real recoil uses "
              f"{12 * abs(k_fit) / 4 / safe_px * 100:.1f}% of the range")

    js = os.path.join(out_dir, f'calib_k_{tag}.json')
    with open(js, 'w') as fh:
        json.dump({'args': vars(args), 'rows': rows, 'raw': raw}, fh, indent=1)
    print(f"\n  raw -> {js}")

    try:
        plot(rows, raw, out_dir, tag, args)
    except Exception as e:
        print(f"  (plot skipped: {e})")


def plot(rows, raw, out_dir, tag, args):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(16, 4.6))
    counts = np.array([r['counts'] for r in rows], float)
    cum = np.array([r['cum_px'] for r in rows], float)
    ks = np.array([r['K'] for r in rows], float)

    ax = axes[0]
    ax.scatter(counts, -cum, s=26, alpha=0.8)
    if not args.dry_run and np.any(counts):
        k_fit = float(np.sum(counts * -cum) / np.sum(counts * counts))
        xr = np.linspace(counts.min(), counts.max(), 10)
        ax.plot(xr, k_fit * xr, 'r--', lw=1.2, label=f'K={k_fit:.4f}')
        ax.legend()
    ax.set_xlabel('injected counts')
    ax.set_ylabel('measured shift (px)')
    ax.set_title('linearity')
    ax.grid(alpha=0.3)

    ax = axes[1]
    for c in sorted(set(counts)):
        m = counts == c
        ax.scatter([c] * m.sum(), ks[m], s=26, alpha=0.8)
    ax.axhline(np.nanmean(ks), color='r', ls='--', lw=1,
               label=f'mean {np.nanmean(ks):.4f}')
    ax.set_xlabel('injected counts')
    ax.set_ylabel('K (px/count)')
    ax.set_title('K stability')
    ax.legend()
    ax.grid(alpha=0.3)

    ax = axes[2]
    for tr in raw[:12]:
        dy = np.array([np.nan if v is None else v for v in tr['dy']], float)
        ts = np.array(tr.get('ts') or range(len(dy)), float)[:len(dy)]
        ax.plot(ts, np.nancumsum(dy), lw=0.9, alpha=0.75)
    ax.axvline(WARMUP_S, color='gray', ls=':', lw=1)
    ax.axvline(WARMUP_S + INJECT_S, color='gray', ls=':', lw=1)
    ax.set_xlabel('time (s)')
    ax.set_ylabel('cumulative shift (px)')
    ax.set_title('per-trial trajectories (injection between dotted lines)')
    ax.grid(alpha=0.3)

    plt.tight_layout()
    p = os.path.join(out_dir, f'calib_k_{tag}.png')
    plt.savefig(p, dpi=100)
    print(f"  plot -> {p}")


if __name__ == '__main__':
    sys.exit(main())
