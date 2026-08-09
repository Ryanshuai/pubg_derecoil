"""harness/verdict.py, offline. -> exit 1 on the first disagreement.

    pixi run harness

WHY A TEST AND NOT A LOOK. `judge()` is what decides whether a night's work is
usable, and it is a pure function of a record — same record, same thresholds,
same answer. That is the property that makes `night.py --rejudge` sound, and it
is only worth relying on if something checks it.

⚠ EVERY CASE IS TWO-SIDED. A gate is only a gate if it refuses; a check with
one direction tested is a check that has been seen to pass. This file pairs
each threshold with the record just inside it and the record just outside.

PORTED TO MODEL.md ON 2026-08-08. Four of the old criteria — the fire-rate
disagreement, the impulse offset, the collection-time magazine count and the
convergence window — were questions about the bullet-bucket coordinate, and
they did not get easier to satisfy, they stopped being askable. What replaced
them is check 4 below, which is the one worth reading: the model's licence to
pool magazines is that magazines fired under DIFFERENT compensation curves
estimate the same y_true, and nothing but firing two arms can test it.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

from harness.verdict import (judge, PROBE_FOR, OK,            # noqa: E402
                             ADS_FRAC_MIN, TRACK_ALIVE_MIN, MAGS_MIN,
                             AGREE_ARMS_MIN, AGREE_SPREAD_MAX,
                             RATE_RESID_MS_MAX)

# A record that passes everything. Every case below is this, one field moved.
#
# ⚠ THE FIELD NAMES MOVED AND THIS FILE DID NOT, so `pixi run harness` was RED
# on an ImportError for CLUSTER_MIN. The measurement layer's names won --
# harness/adapter.py writes `mags_kept` and a `rate_resid_ms` -- but every
# BEHAVIOUR asserted below is unchanged, including the one that mattered:
# MODEL.md's out-of-loop check, which verdict.py had meanwhile replaced with an
# impulse check whose probe no longer exists.
GOOD = dict(reached=True, mags_kept=6, fired=3, ads_frac=0.95,
            track_alive_frac=0.99, agree_arms=2, agree_spread=0.03,
            rate_resid_ms=0.2,
            span_s=3.8, total_counts=900.0, spread_counts=25.0)


def case(name, rec, want_usable, want_why):
    v = judge(rec)
    ok = v['usable'] == want_usable and v['why'] == want_why
    mark = 'ok  ' if ok else 'FAIL'
    print(f'  {mark}  {name:<46s} {v["why"]:<9s} {v.get("detail", "")[:44]}')
    return 0 if ok else 1


def main():
    bad = 0
    print('\n=== the happy record, and every field that can spoil it ===')
    bad += case('a complete record passes', GOOD, True, OK)

    print('\n=== 0-1. the code broke vs the game did not cooperate ===')
    # These route a human to completely different places, so merging them
    # would file a traceback as "the weapon would not spawn".
    bad += case('a crash is not a state failure',
                dict(GOOD, crashed=True, reached_why='KeyError'), False, 'crash')
    bad += case('never reached the configuration',
                dict(GOOD, reached=False), False, 'state')

    print('\n=== 2. enough magazines in the fitter\'s MAIN CLUSTER ===')
    # ⚠ THE POOL, NOT THE NIGHT. Samples accumulate forever, so a thin night
    # on top of a fat history is a good cell.
    bad += case(f'{MAGS_MIN} in the cluster is enough',
                dict(GOOD, mags_kept=MAGS_MIN), True, OK)
    bad += case(f'{MAGS_MIN - 1} is not',
                dict(GOOD, mags_kept=MAGS_MIN - 1), False, 'mags')
    bad += case('an absent count is not a pass',
                {k: v for k, v in GOOD.items() if k != 'mags_kept'},
                False, 'mags')

    print('\n=== 2b. the fire rate settled ===')
    bad += case('a settled rate passes',
                dict(GOOD, rate_resid_ms=RATE_RESID_MS_MAX), True, OK)
    bad += case('magazines disagreeing about the rate fails',
                dict(GOOD, rate_resid_ms=RATE_RESID_MS_MAX + 0.1),
                False, 'rate')
    bad += case('an absent rate is not a pass',
                {k: v for k, v in GOOD.items() if k != 'rate_resid_ms'},
                False, 'rate')

    print('\n=== 3. was the burst aimed ===')
    bad += case(f'{ADS_FRAC_MIN:.0%} passes',
                dict(GOOD, ads_frac=ADS_FRAC_MIN), True, OK)
    bad += case('just under does not',
                dict(GOOD, ads_frac=ADS_FRAC_MIN - 0.01), False, 'ads')

    print('\n=== 4. THE OUT-OF-LOOP CHECK — the arms must agree ===')
    # The fitter never sees which arm a magazine came from, so agreement
    # between arms is not something a fit can arrange. This is the one check
    # here that a self-consistent-but-wrong pipeline cannot satisfy.
    bad += case('two arms agreeing is what a good cell looks like',
                dict(GOOD, agree_arms=2, agree_spread=AGREE_SPREAD_MAX),
                True, OK)
    bad += case('two arms disagreeing fails',
                dict(GOOD, agree_spread=AGREE_SPREAD_MAX + 0.01),
                False, 'agree')
    # ⚠ THE CASE THAT MATTERS MOST. One arm is not a passed check, it is an
    # unrun one -- and "unmeasured" reading as "fine" is the failure this whole
    # layer exists to prevent.
    bad += case('ONE arm is untested, not passed',
                dict(GOOD, agree_arms=1), False, 'agree')
    bad += case('an absent arm count is not a pass',
                {k: v for k, v in GOOD.items() if k != 'agree_arms'},
                False, 'agree')
    bad += case('an absent spread is not a pass',
                {k: v for k, v in GOOD.items() if k != 'agree_spread'},
                False, 'agree')

    print('\n=== 5. how much of the burst the correlator placed ===')
    bad += case(f'{TRACK_ALIVE_MIN:.0%} passes',
                dict(GOOD, track_alive_frac=TRACK_ALIVE_MIN), True, OK)
    bad += case('under it does not',
                dict(GOOD, track_alive_frac=TRACK_ALIVE_MIN - 0.01),
                False, 'tracking')

    print('\n=== order: the cheapest and most fundamental first ===')
    # A cell that never reached its configuration is `state`, however bad
    # everything downstream looks -- `why` is the routing key the morning uses
    # to pick a probe, and pointing it at the tracker for a gun that never
    # spawned sends somebody to the wrong place for an hour.
    bad += case('state wins over every later failure',
                dict(GOOD, reached=False, n_kept=0, ads_frac=0.0,
                     agree_arms=1, track_alive_frac=0.0), False, 'state')
    bad += case('a crash wins over state',
                dict(GOOD, crashed=True, reached=False), False, 'crash')

    print('\n=== every `why` routes somewhere ===')
    # A verdict nobody can act on is a verdict that gets ignored. The routing
    # lives beside the thresholds so it cannot fall out of step; this checks
    # that it did not.
    whys = {'crash', 'state', 'mags', 'rate', 'ads', 'agree', 'tracking'}
    missing = sorted(whys - set(PROBE_FOR))
    extra = sorted(set(PROBE_FOR) - whys)
    print(f'  {"ok  " if not missing and not extra else "FAIL"}  '
          f'PROBE_FOR covers exactly the reasons judge() emits'
          f'{"" if not missing and not extra else f" missing={missing} extra={extra}"}')
    bad += bool(missing or extra)

    print()
    if bad:
        print(f'{bad} FAILED')
        return 1
    print('all ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
