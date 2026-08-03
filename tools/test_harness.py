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
    rec = {'reached': True, 'mags_kept': 4, 'rate_resid_ms': 3.2,
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


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    test_verdict()
    test_manifest()
    if FAILS:
        print(f'\n{len(FAILS)} failed:')
        for f in FAILS:
            print(f'  {f}')
        return 1
    print('\nall ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
