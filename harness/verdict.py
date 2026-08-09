"""Is this cell's measurement usable? Numbers against thresholds, nothing else.

The one rule: **the code that took the measurement does not get to judge it.**

That is not a style preference. This project's central failure mode is a
closed loop that agrees with itself -- the curve is fitted on bins anchored to
the ammo counter while the firmware plays it on a grid anchored to the click,
so a wrong offset produces a self-consistent distorted curve with a near-zero
residual. **A small residual proves nothing about the timing.** Whatever
judges the cell has to look at something the fit could not have arranged.

Anthropic's harness work found the same thing from the other end: an agent
asked to grade its own output "confidently praises the work, even when the
quality is obviously mediocre", and separating generator from evaluator was
what fixed it. Here the separation is stronger still, because the evaluator is
not a model at all -- every criterion below is a number with a threshold, so
the same record judged twice gives the same answer, tonight and next month.

Order matters. `why` is the routing key the morning uses to pick a probe, so
the checks run cheapest-and-most-fundamental first: a cell that never reached
its configuration is `state`, not `tracking`, however bad the tracking looked.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── thresholds ──
#
# ── THE OUT-OF-LOOP CHECK ────────────────────────────────────────────────
#
# MODEL.md is the law and its external check is this: magazines fired under
# DIFFERENT compensation curves must, once each one's own y_comp is added back,
# estimate the SAME y_true. The fitter never sees which arm a magazine came
# from, so it is the one signal a fit cannot arrange.
#
# ⚠ IT USED TO ASK WHICH ROUND MOVED, AND THAT TECHNIQUE IS REJECTED OUTRIGHT
# -- not merely superseded. Three independent reasons, and any one is fatal:
# the instant is not recorded accurately, the two coordinates cannot be put in
# correspondence to begin with, and THE AMMO COUNTER DOES NOT RESOLVE ROUNDS
# (control/fire.py: it reads about five times in a 42-round magazine while
# firing). Nothing has written that field since the coordinate went, so item 4
# answered "no check was run" for EVERY cell -- a gate that cannot pass is not
# a gate. MODEL.md's ruled-out table.
#
# ⚠ ONE ARM IS "NOT CHECKED", NOT "PASSED". _agreement returns (1, None) for a
# single-arm pool and this refuses it. That asymmetry is the entire value of
# the check.
AGREE_ARMS_MIN = 2

# The MID-BAND, not the endpoint. Four arms measured agreeing to 0.9% at
# t=1.5 s and diverging to 15% by t=3.8 s, with the divergence coming entirely
# from the strongest arm in the last 1.1 s -- a region MODEL.md says is not
# understood. Judging the model there would judge it on the one part nobody
# claims to model. MODEL.md's own two-arm table is read at 2.40 s.
AGREE_BAND_S = (1.0, 2.4)

# A FRACTION of y_true, not counts: 30 counts means something different on a
# Vector than on an MG3.
#
# Placed against the noise, not picked: MODEL.md's strongest pass is 0.4%
# across an 85x excursion, and an arm difference carries ~1.5% by construction
# (one arm n=8, per-magazine CV 3%). 5% is ~3x that noise, and it sits well
# under the 6.41% that cross-session comparison produced on the same question
# -- the artefact interleaving exists to remove.
AGREE_SPREAD_MAX = 0.05

# DERIVED FROM THE FAILURE MODE, and the same number the measurement layer
# uses (calibration/analysis.ADS_FRAC_MIN). Hip fire is what this is for: the
# posture icon stops rendering, the burst is analysed with the scoped K of 1.55
# against the hip's 0.50, and a confident +498 counts comes back on a weapon
# that measured -31 an hour earlier. That is a ~3x error, nowhere near 90%.
#
# It was 0.90, and 0.90 was wrong in a way worth keeping written down. The
# comment justifying it said "the crosshair agreed 96% of polls, the posture
# icon 48% -- this is the canary on that decision", which describes a check on
# the GAP between the two signals. What was implemented was a floor on the
# crosshair's absolute level, set just under one weapon's happy number from one
# session. It is not a canary, and it is not independent: `ads_frac` here and
# the `ads_frac` magazine_fault gates are the same crosshair-led quantity, so
# every magazine reaching this function already passed 0.80.
#
# It cost a good cell within the hour: the akm measured 0.897 and then 0.898 on
# two full attempts, with 5/5 magazines kept, rate spread 0.19 ms, tracking 99%
# and every other check clean. Twice rejected for two tenths of a point.
#
# Kept here rather than deleted as redundant, and kept at the SAME value: this
# layer has to judge a record even if the measurement layer's gate is changed,
# bypassed, or the record came from somewhere else. Defence in depth with the
# same derived number is honest; a stricter arbitrary one is a second opinion
# nobody derived. tools/test_harness.py asserts the two stay equal.
#
# 2026-08-06: 0.80 -> 0.30, following calibration/analysis, and the reason is
# not a loosening of standards -- it is that the quantity turned out not to
# measure what the floor assumed. FIRING CANNOT CHANGE ADS (right click is a
# toggle, nothing touches it during a burst, the reload is after), so
# GunDriver.ensure_ads settles the question before the trigger and this reading
# adds nothing about whether the magazine was aimed. What it DOES do is read
# low under recoil shake: measured 387/387 scoped while still and 0.79 while
# firing on the same VSS in the same ADS, scaling with recoil across the
# roster. The old floor refused 55 of 1044 magazines, every one of them at
# 61-80% and none near the ~0 of a real hip-fired burst, 46 of them the vector.
# What survives at 0.30 is the contradiction check: ensure_ads said up, this
# says never up, and one of them is lying.
ADS_FRAC_MIN = 0.30

# NOT a target -- a floor under a known defect. Phase-correlation tracking is
# lost after 3-4 magazines of 5 ("the reference match has wrapped"), so half of
# every cell is currently thrown away. Raise this as the wrap is fixed; if it
# ever rejects a cell that used to pass, the tracker regressed.
TRACK_ALIVE_MIN = 0.50

# Below this the curve is fitted on too little. Five are fired per cell and the
# tracker eats one or two, so three is what is left on a good night.
MAGS_MIN = 3

# DERIVED, then checked against real magazines.
#
# What is measured is not a fit residual -- interval_from_span uses the two
# endpoints of the magazine, so its residual is ZERO BY CONSTRUCTION and a
# gate on it would pass everything. What the harness measures instead is
# DISAGREEMENT BETWEEN MAGAZINES, which is what that function's own docstring
# asks the caller to require: "a missed LAST change shortens the span and
# reads as a faster gun ... it shows up as a rate that disagrees between
# magazines of the same cell".
#
# The threshold follows from what an interval error DOES. It is not an offset,
# it compounds: round k lands k*d/T bullets late for an interval error of d.
# Over a 42-round magazine at T=83 ms, d = 1.0 ms puts the last round
# 41*1.0/83 = 0.49 bullets off -- which is IMPULSE_OFF_MAX, deliberately, so
# the two timing gates allow the same error at the same place rather than
# each allowing its own.
#
# Measured for scale: four AUG magazines fired 2026-08-03 read 83.08, 82.93,
# 82.73 and 83.39 ms -- a spread of 0.24 ms, four times inside this.
#
# This used to import rpm_store.RESID_MS_MAX (12.0) "so the two cannot drift".
# They are not the same quantity: that one bounds a straight-line fit through
# per-round counter transitions located to ~25 ms, and borrowing it let 11.5 ms
# of disagreement -- 5.5 bullets of phase by the end of the magazine -- pass as
# a good cell. Caught by tools/test_harness.py the hour the check was written.
RATE_RESID_MS_MAX = 1.0

OK = 'ok'


def judge(rec):
    """-> {'usable': bool, 'why': str, 'metrics': {...}}

    `rec` is a cell record. Missing fields are NOT treated as passes: a metric
    that is absent means nobody measured it, and "unmeasured" and "fine" are
    the two things this function exists to keep apart. Each check therefore
    fails closed, and says which field was missing.
    """
    m = {k: rec.get(k) for k in ('reached', 'mags_kept', 'agree_arms',
                                 'agree_spread', 'ads_frac',
                                 'track_alive_frac', 'rate_resid_ms',
                                 'rounds')}

    def bad(why, detail):
        return {'usable': False, 'why': why, 'detail': detail, 'metrics': m}

    # 0. The code broke, as opposed to the game not cooperating. Routed apart
    #    from `state` because the two send a human to completely different
    #    places: `state` means the game did something the driver did not
    #    expect, `crash` means this repo has a bug and the traceback says
    #    where. Merging them would have filed the night's first failure -- a
    #    KeyError from indexing a dict of crops with a pair of slices -- as
    #    "the weapon would not spawn".
    if rec.get('crashed'):
        return bad('crash', rec.get('reached_why') or 'exception')

    # 1. Did the cell ever reach the configuration it is labelled with? A
    #    weapon that never got into the rack, a sight that never went on, a
    #    posture that never took -- the measurement after that is of something
    #    else, and it is the only failure that makes every other number a lie.
    if not rec.get('reached', False):
        return bad('state', rec.get('reached_why') or
                   'the configuration was never reached')

    # 2. Enough magazines to fit on.
    kept = rec.get('mags_kept')
    if kept is None:
        return bad('mags', 'mags_kept missing')
    if kept < MAGS_MIN:
        return bad('mags', f'{kept} magazines kept, need {MAGS_MIN}')

    # 3. Fire rate. A wrong interval is not a small error: it puts round n's
    #    pulse 0.01*x*n rounds late, so it compounds along the very axis being
    #    fitted.
    resid = rec.get('rate_resid_ms')
    if resid is None:
        return bad('rate', rec.get('rate_note') or 'rate_resid_ms missing')
    if resid > RATE_RESID_MS_MAX:
        return bad('rate', f'the magazines disagree about the fire rate by '
                           f'{resid:.2f} ms, over {RATE_RESID_MS_MAX:.1f}')

    # 4. THE out-of-loop check, and the reason the other three are not enough.
    #    A run can satisfy every check above and still be measuring on a grid
    #    shifted from the one the firmware plays on -- the fit absorbs the
    #    shift and reports a small residual for a distorted curve. Only a
    #    signal the fit could not have arranged separates them: magazines from
    #    DIFFERENT curve strengths must give the same y_true once each one's
    #    own y_comp is added back, and the fitter cannot see which arm a
    #    magazine came from.
    arms = rec.get('agree_arms')
    if arms is None:
        return bad('agree', 'agree_arms missing — nobody checked the arms')
    if arms < AGREE_ARMS_MIN:
        return bad('agree', f'only {arms} curve arm(s) in the pool; one arm is '
                            f'NOT CHECKED, which is not the same as passed')
    spread = rec.get('agree_spread')
    if spread is None:
        return bad('agree', 'agree_spread missing — the arms exist but not '
                            'enough of them reach the comparison band')
    if spread > AGREE_SPREAD_MAX:
        return bad('agree', f'{arms} arms disagree about y_true by '
                            f'{spread:.1%} over t={AGREE_BAND_S[0]:.1f}'
                            f'..{AGREE_BAND_S[1]:.1f}s, over '
                            f'{AGREE_SPREAD_MAX:.0%}')

    # 5. Was the player actually aiming? Firing from the hip measures a
    #    different weapon.
    ads = rec.get('ads_frac')
    if ads is None:
        return bad('ads', 'ads_frac missing')
    if ads < ADS_FRAC_MIN:
        return bad('ads', f'aiming for {ads:.0%} of polls, want '
                          f'{ADS_FRAC_MIN:.0%}')

    # 6. How much of each magazine the tracker survived. Last because it is a
    #    known defect rather than a symptom of anything: a cell can be perfect
    #    in every other respect and still be thin.
    alive = rec.get('track_alive_frac')
    if alive is None:
        return bad('tracking', 'track_alive_frac missing')
    if alive < TRACK_ALIVE_MIN:
        return bad('tracking', f'the view was tracked for {alive:.0%} of the '
                               f'rounds fired')

    return {'usable': True, 'why': OK, 'detail': '', 'metrics': m}


# Which probe answers each `why`. Printed by the morning report, so the
# routing lives next to the thresholds that produce it rather than in prose
# somewhere that can fall out of step.
PROBE_FOR = {
    'crash':    'this repo has a bug — the traceback is in the evidence '
                'directory, state.json. Not a game problem.',
    'state':    'the cell never reached its configuration — control/, not '
                'measurement. Read the evidence frame first.',
    'mags':     'tools/probe_ammo_during_fire.py — magazines were discarded, '
                'find out at which stage',
    'rate':     'calibration/rpm_store.py + the cell trace — the fire rate '
                'never settled',
    'agree':    "MODEL.md's out-of-loop check -- fire more than one curve "
                "strength, INTERLEAVED (calibration/collect_timed.py "
                "--scale-sweep). One arm is NOT CHECKED, not passed, and "
                "nothing downstream is trustworthy until this passes.",
    'ads':      'detector/ads_detector.py + calibration/capture_ads.py — the '
                'crosshair gate',
    'tracking': 'the ViewTracker reference frame (the wrap after 3-4 '
                'magazines). Expected until that is fixed.',
}

