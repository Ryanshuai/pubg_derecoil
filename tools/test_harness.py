"""The manifest and the verdict thresholds, offline. No game, no hardware.

    pixi run harness

Both are pure enough to test on made-up records, and both are the kind of code
whose bugs are silent: a manifest that loses a cell and a threshold that
passes a missing field look exactly like a good night.
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from harness.manifest import (Manifest, USABLE, FAILED, SKIPPED,   # noqa: E402
                              UNMEASURED, cell_id)
from harness.verdict import judge                                  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f'  {"ok " if ok else "FAIL"}  {name:<46} {got!r}')
    if not ok:
        FAILS.append(f'{name}: got {got!r}, want {want!r}')


def good_record(**over):
    """A record that passes every threshold, so each test can spoil one."""
    # rate_resid_ms is the spread four real AUG magazines produced (0.24 ms),
    # not a round number: a fixture that passes only because the threshold is
    # loose stops noticing when the threshold moves.
    rec = {'reached': True, 'mags_kept': 4, 'rate_resid_ms': 0.24,
           'rounds': 40, 'impulse_off_rounds': 0.1, 'ads_frac': 0.97,
           'track_alive_frac': 0.8, 'curve': [1, 2, 3]}
    rec.update(over)
    return rec


def test_verdict():
    print('\nverdict — a good record passes, and every field can fail it')
    check('a complete good record', judge(good_record())['usable'], True)
    check('never reached the config',
          judge(good_record(reached=False))['why'], 'state')
    check('too few magazines',
          judge(good_record(mags_kept=2))['why'], 'mags')
    check('fire rate never settled',
          judge(good_record(rate_resid_ms=30.0))['why'], 'rate')
    check('the impulse landed late',
          judge(good_record(impulse_off_rounds=1.4))['why'], 'impulse')
    check('firing from the hip',
          judge(good_record(ads_frac=0.4))['why'], 'ads')
    check('the tracker died early',
          judge(good_record(track_alive_frac=0.2))['why'], 'tracking')

    print('\nverdict — a MISSING field is a failure, never a pass')
    for field, why in (('impulse_off_rounds', 'impulse'),
                       ('ads_frac', 'ads'),
                       ('track_alive_frac', 'tracking'),
                       ('mags_kept', 'mags'),
                       ('rate_resid_ms', 'rate')):
        rec = good_record()
        del rec[field]
        check(f'missing {field}', judge(rec)['why'], why)

    print('\nverdict — order: the most fundamental failure is the one named')
    # Everything is broken at once. "state" has to win: a cell that never
    # reached its configuration measured a different weapon, which makes every
    # other number meaningless rather than merely bad.
    rec = good_record(reached=False, mags_kept=0, impulse_off_rounds=9.0,
                      ads_frac=0.0, track_alive_frac=0.0)
    check('all broken -> state', judge(rec)['why'], 'state')


def test_manifest():
    print('\nmanifest — build, mark, resume')
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'manifest.json')
        cells = [('aug', 'standing', 'red_dot'),
                 ('m416', 'standing', 'red_dot'),
                 ('akm', 'standing', 'red_dot')]
        m = Manifest.build(p, cells, params={'mags': 5})
        check('all cells start unmeasured', len(m.pending()), 3)
        check('the file exists immediately', os.path.exists(p), True)

        m.mark(cell_id('aug', 'standing', 'red_dot'), USABLE,
               verdict={'usable': True, 'why': 'ok'})
        check('one measured -> two pending', len(m.pending()), 2)

        # The whole reason it is written per cell rather than at the end.
        m2 = Manifest.load(p)
        check('a fresh process sees the same state', len(m2.pending()), 2)

        m.mark(cell_id('m416', 'standing', 'red_dot'), FAILED,
               verdict={'usable': False, 'why': 'tracking'},
               evidence='docs/runs/x/fail_m416')
        check('failed cells route by reason',
              m.by_reason(), {'tracking': ['m416|standing|red_dot']})
        check('counts', m.counts(),
              {UNMEASURED: 1, USABLE: 1, FAILED: 1, SKIPPED: 0})

    print('\nmanifest — the halt streak')
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'm.json')
        cells = [(w, 'standing', 'red_dot')
                 for w in ('a', 'b', 'c', 'd', 'e')]
        m = Manifest.build(p, cells)
        for w in ('a', 'b'):
            m.mark(cell_id(w, 'standing', 'red_dot'), FAILED,
                   verdict={'why': 'state'})
        check('two failures', m.consecutive_failures(), 2)
        m.mark(cell_id('c', 'standing', 'red_dot'), USABLE,
               verdict={'why': 'ok'})
        check('a success resets it', m.consecutive_failures(), 0)
        m.mark(cell_id('d', 'standing', 'red_dot'), FAILED,
               verdict={'why': 'ads'})
        # Skipped was never attempted, so it must not break a streak that a
        # later failure should extend.
        m.mark(cell_id('e', 'standing', 'red_dot'), SKIPPED)
        check('skipped does not break the streak', m.consecutive_failures(), 1)

    print('\nmanifest — a resumed run keeps the ORIGINAL plan')
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'm.json')
        Manifest.build(p, [('aug', 'standing', 'red_dot')], params={'mags': 5})
        m, resumed = Manifest.open_or_build(
            p, [('scar', 'prone', 'x4')], params={'mags': 99})
        check('resume flag', resumed, True)
        check('the plan on disk wins', [c['weapon'] for c in m.cells], ['aug'])
        check('so do its parameters', m.data['params']['mags'], 5)

    print('\nmanifest — the write survives being interrupted')
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'm.json')
        m = Manifest.build(p, [('aug', 'standing', 'red_dot')])
        m.mark(cell_id('aug', 'standing', 'red_dot'), USABLE,
               verdict={'why': 'ok'})
        with open(p, encoding='utf-8') as f:
            raw = json.load(f)          # parses = the replace was atomic
        check('valid JSON after a mark', raw['cells'][0]['state'], USABLE)
        check('no temp files left behind',
              [x for x in os.listdir(d) if x.endswith('.tmp')], [])


def a_mag(**over):
    """One kept magazine, as measure_cell records it."""
    row = {'per_bullet_counts': [10.0] * 40, 'n_out_of_range': 0,
           'ads_cross_frac': 0.98, 'measured_interval_ms': 83.0}
    row.update(over)
    return row


def a_cell(rows, discarded=(), **over):
    rec = {'mags': list(rows), 'mags_discarded': list(discarded),
           'mags_asked': 5, 'bullets_fired': 40,
           'residual_counts_mean': 12.0, 'true_counts': 1900.0}
    rec.update(over)
    return rec


def test_adapter():
    """_fill turns a cell record into the harness's numbers.

    Every check here is on a quantity that is WRONG IN A PLAUSIBLE DIRECTION
    when the code is wrong: a tracking fraction measured over survivors reads
    high, a rate residual from a two-endpoint fit reads zero, an ADS mean
    hides one bad magazine behind four good ones. All three pass the verdict
    gates while being false, which is the only reason they are worth testing.
    """
    from harness import adapter

    print('\nadapter — tracking is measured over what was FIRED, not kept')
    # Four of five magazines thrown away. The one survivor tracked perfectly;
    # a fraction computed from `mags` alone would say 100%.
    rec = adapter._fill(adapter._blank({'id': 'aug|standing|red_dot'}),
                        a_cell([a_mag()], discarded=['tracking'] * 4), 0.0)
    check('one of five kept, tracker did not hold the rest',
          round(rec['track_alive_frac'], 2), 0.2)
    check('  and the survivor count is not the tracking number',
          rec['mags_kept'], 1)

    # All five kept, one of them half blind.
    rows = [a_mag() for _ in range(4)] + [a_mag(n_out_of_range=20)]
    rec = adapter._fill(adapter._blank({'id': 'x'}), a_cell(rows), 0.0)
    check('out-of-range rounds come off the numerator',
          round(rec['track_alive_frac'], 2), 0.9)

    print('\nadapter — the rate check is agreement, not a fit residual')
    rows = [a_mag(measured_interval_ms=83.0), a_mag(measured_interval_ms=83.4)]
    rec = adapter._fill(adapter._blank({'id': 'x'}), a_cell(rows), 0.0)
    check('magazines that agree', round(rec['rate_resid_ms'], 2), 0.2)
    rows = [a_mag(measured_interval_ms=83.0), a_mag(measured_interval_ms=60.0)]
    rec = adapter._fill(adapter._blank({'id': 'x'}), a_cell(rows), 0.0)
    check('a missed last change reads as a faster gun',
          round(rec['rate_resid_ms'], 1), 11.5)
    check('  and that fails the rate gate',
          judge(dict(rec, mags_kept=4, reached=True))['why'], 'rate')
    # THE point of the whole field. One magazine has nothing to disagree with,
    # and 0.0 would sail through the gate having checked nothing.
    rec = adapter._fill(adapter._blank({'id': 'x'}), a_cell([a_mag()]), 0.0)
    check('one magazine cannot agree with itself', rec['rate_resid_ms'], None)
    check('  so it fails closed rather than passing at 0.0',
          judge(dict(rec, mags_kept=4, reached=True))['why'], 'rate')

    print('\nadapter — ADS is the worst accepted magazine, not the mean')
    rows = [a_mag() for _ in range(4)] + [a_mag(ads_cross_frac=0.40)]
    rec = adapter._fill(adapter._blank({'id': 'x'}), a_cell(rows), 0.0)
    check('one hip-fired magazine is not averaged away',
          rec['ads_frac'], 0.40)
    check('  and it fails the ads gate',
          judge(dict(rec, mags_kept=4, reached=True,
                     rate_resid_ms=0.2))['why'], 'ads')

    print('\nadapter — a crash is a cell, not the end of the night')
    # The first real run died here: a KeyError inside one cell took the
    # process, and with it every cell that had not run yet. The loop catches
    # it now; this pins that a caught crash is still a FAILURE, and routed
    # somewhere different from "the game would not cooperate".
    crashed = {'reached': False, 'crashed': True,
               'reached_why': "KeyError: (slice(123, 168), slice(2275, 2525))"}
    check('a crash does not pass', judge(crashed)['usable'], False)
    check('and is not filed as a game-state problem',
          judge(crashed)['why'], 'crash')
    check('the traceback reaches the verdict detail',
          'KeyError' in judge(crashed)['detail'], True)
    from harness.verdict import PROBE_FOR
    check('every verdict reason routes somewhere',
          [w for w in ('crash', 'state', 'mags', 'rate', 'impulse', 'ads',
                       'tracking') if w not in PROBE_FOR], [])

    print('\nadapter — an unreached cell claims nothing')
    rec = adapter._blank({'id': 'aug|standing|red_dot'})
    check('every field present', sorted(rec) != [], True)
    check('nothing is missing rather than None',
          [k for k in adapter.RECORD_FIELDS if k not in rec], [])
    check('and it judges as state', judge(rec)['why'], 'state')

    print('\nadapter — the rate threshold has one meaning, in two places')
    # harness/verdict.py may not import calibration (only adapter.py may), so
    # the number is written twice. This is what keeps the copies honest: a
    # change to either is a failing test rather than two layers quietly
    # disagreeing about what a good fire rate is.
    from calibration.rpm_store import AGREE_MS
    from harness.verdict import RATE_RESID_MS_MAX
    check('verdict and rpm_store agree on the tolerance',
          RATE_RESID_MS_MAX, AGREE_MS)

    # Same story for ADS, and the same reason it is written twice. This one
    # was NOT equal for a while: verdict held 0.90 against analysis's 0.80,
    # which is not a second opinion, it is the same opinion with a harsher
    # number nobody derived. It threw away an akm cell that was clean on every
    # other axis, twice, at 0.897 and 0.898.
    from calibration.analysis import ADS_FRAC_MIN as MEASURED_MIN
    from harness.verdict import ADS_FRAC_MIN as JUDGED_MIN
    check('verdict and analysis agree on the ADS floor',
          JUDGED_MIN, MEASURED_MIN)

    print('\nadapter — ragged magazines are truncated, not padded')
    rows = [a_mag(per_bullet_counts=[10.0] * 40),
            a_mag(per_bullet_counts=[10.0] * 12)]
    rec = adapter._fill(adapter._blank({'id': 'x'}), a_cell(rows), 0.0)
    check('the curve is as long as the shortest magazine',
          len(rec['curve']), 12)


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    test_verdict()
    test_manifest()
    test_adapter()
    if FAILS:
        print(f'\n{len(FAILS)} failed:')
        for f in FAILS:
            print(f'  {f}')
        return 1
    print('\nall ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
