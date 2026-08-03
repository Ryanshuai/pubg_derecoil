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

from calibration.rpm_store import RESID_MS_MAX      # noqa: E402

# ── thresholds ──
#
# MEASURED. tools/probe_impulse_align.py fired a curve that is zero except one
# spiked bullet and watched which round the view jumped on: 0 rounds off at
# bullet 12 AND at bullet 30, three magazines each. That excludes a constant
# offset and a proportional (interval) error at once. Half a round is the
# slack this allows on top of a result that measured exactly zero twice.
IMPULSE_OFF_MAX = 0.5           # rounds

# MEASURED. Instrumenting the two ADS signals separately over five magazines:
# the crosshair agreed 96% of polls, the posture icon 48%. The gate is now
# crosshair-led; this is the canary on that decision, not a new claim.
ADS_FRAC_MIN = 0.90

# NOT a target -- a floor under a known defect. Phase-correlation tracking is
# lost after 3-4 magazines of 5 ("the reference match has wrapped"), so half of
# every cell is currently thrown away. Raise this as the wrap is fixed; if it
# ever rejects a cell that used to pass, the tracker regressed.
TRACK_ALIVE_MIN = 0.50

# Below this the curve is fitted on too little. Five are fired per cell and the
# tracker eats one or two, so three is what is left on a good night.
MAGS_MIN = 3

# The fire rate is measured per magazine from two endpoints; RESID_MS_MAX is
# the same constant rpm_store accepts a rate at, imported rather than retyped
# so the two cannot drift into disagreeing about what a good fit is.
RATE_RESID_MS_MAX = RESID_MS_MAX

OK = 'ok'


def judge(rec):
    """-> {'usable': bool, 'why': str, 'metrics': {...}}

    `rec` is a cell record. Missing fields are NOT treated as passes: a metric
    that is absent means nobody measured it, and "unmeasured" and "fine" are
    the two things this function exists to keep apart. Each check therefore
    fails closed, and says which field was missing.
    """
    m = {k: rec.get(k) for k in ('reached', 'mags_kept', 'impulse_off_rounds',
                                 'ads_frac', 'track_alive_frac',
                                 'rate_resid_ms', 'rounds')}

    def bad(why, detail):
        return {'usable': False, 'why': why, 'detail': detail, 'metrics': m}

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
        return bad('rate', 'rate_resid_ms missing')
    if resid > RATE_RESID_MS_MAX:
        return bad('rate', f'fire-rate fit residual {resid:.1f} ms > '
                           f'{RATE_RESID_MS_MAX:.0f}')

    # 4. THE out-of-loop check, and the reason the other three are not enough.
    #    A run can satisfy every check above and still be measuring on a grid
    #    shifted from the one the firmware plays on -- the fit absorbs the
    #    shift and reports a small residual for a distorted curve. Only a
    #    signal the fit could not have arranged separates them, which is what
    #    the impulse is: a curve that is zero except one spiked bullet, and an
    #    observation of which round actually moved.
    off = rec.get('impulse_off_rounds')
    if off is None:
        return bad('impulse', 'no impulse check was run for this cell')
    if abs(off) > IMPULSE_OFF_MAX:
        return bad('impulse', f'the spike landed {off:+.1f} rounds from where '
                              f'it was commanded')

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
    'state':    'the cell never reached its configuration — control/, not '
                'measurement. Read the evidence frame first.',
    'mags':     'tools/probe_ammo_during_fire.py — magazines were discarded, '
                'find out at which stage',
    'rate':     'calibration/rpm_store.py + the cell trace — the fire rate '
                'never settled',
    'impulse':  'tools/probe_impulse_align.py — the timing chain. Nothing '
                'downstream is trustworthy until this passes.',
    'ads':      'detector/ads_detector.py + calibration/capture_ads.py — the '
                'crosshair gate',
    'tracking': 'the ViewTracker reference frame (the wrap after 3-4 '
                'magazines). Expected until that is fixed.',
}

