"""Pin the capture-run format, and the two pre-CaptureRun shapes it reads.

    pixi run runs

Offline: no game, no hardware, and it WRITES NOTHING under docs/. The synthetic
runs go to a temp directory; the real ones under docs/ads/runs are opened
read-only, because those 867 frames cost tens of minutes of game time each and
cannot be re-made.

WHAT IT GUARDS, and why each one is worth a test rather than a comment:

  the adapter reads what is there      An adapter that silently returns zero
                                       entries looks exactly like a run that
                                       captured nothing. Entry counts are
                                       checked against the raw index files,
                                       not against a number written here.

  legacy labels are NEVER ground truth The reason this whole format exists.
                                       docs/ads/runs holds two runs whose
                                       labels are known-wrong — one is 64
                                       frames of shoulder aim filed as ADS,
                                       one is 40 frames of the wrong weapon
                                       filed as `iron`. Read through this API
                                       every legacy run yields 0 from
                                       labelled(), so neither can be picked up
                                       as truth by a consumer that has not read
                                       detector/CLAUDE.md.

  a legacy run cannot be written back  save() beside 400 irreplaceable frames
                                       would start a second index for the same
                                       run. It raises instead.

  the decision tables                  scope_label() and label_for() are where
                                       "we asked for it" gets separated from
                                       "a detector said so" and from "nobody
                                       looked". Those are judgement calls in
                                       prose everywhere else in this codebase;
                                       here they are executable, so promoting
                                       an unverified intention to ground truth
                                       fails HERE rather than six months later
                                       in a template fitted to bad samples.
"""
import contextlib
import glob
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import numpy as np

from calibration.capture_run import (CaptureRun, LABEL_DETECTED, LABEL_REQUESTED,
                         MANIFEST, VERSION)

FAILS = []


def check(name, cond, detail=''):
    print(f'  {"OK  " if cond else "FAIL"} {name}' + (f'   {detail}' if detail
                                                      else ''))
    if not cond:
        FAILS.append(name)
    return cond


def frame(v=7):
    return np.full((8, 12, 3), v, np.uint8)


# ════════════════════════════════════════════════════════════
# The real runs on disk — read-only
# ════════════════════════════════════════════════════════════

def t_real_ads():
    """Every stored ADS run, against its own index.jsonl."""
    print('\nlegacy ADS runs under docs/ads/runs (read-only)')
    dirs = sorted(glob.glob(os.path.join(ROOT, 'docs', 'ads', 'runs', '*',
                                         'index.jsonl')))
    check('there are legacy ADS runs to read', bool(dirs), f'{len(dirs)} runs')
    total = 0
    for idx in dirs:
        d = os.path.dirname(idx)
        with open(idx, encoding='utf-8') as f:
            want = sum(1 for line in f if line.strip())
        run = CaptureRun.load_dir(d)
        total += len(run.entries)
        stamp = os.path.basename(d)
        ok = len(run.entries) == want and not run.labelled()
        check(f'{stamp}', ok,
              f'{len(run.entries)}/{want} entries, {len(run.labelled())} '
              f'ground truth (must be 0)')
        # meta.json is the run's parameters, and losing it would lose the
        # notes that say WHICH runs are the bad ones.
        if os.path.exists(os.path.join(d, 'meta.json')):
            check(f'{stamp} meta.json -> facts',
                  run.facts.get('stamp') == stamp or 'weapon' in run.facts)
    check('every frame is reachable', total > 800, f'{total} captures')

    # The two known-bad runs, named. If either ever came back with ground
    # truth, a template or a detector could be fitted to frames that are
    # documented as wrong.
    for stamp in ('20260801_222936', '20260802_015545'):
        d = os.path.join(ROOT, 'docs', 'ads', 'runs', stamp)
        run = CaptureRun.load_dir(d)
        has_scope = any(lab.get('slot') == 'scope'
                        for e in run.entries for lab in e['labels'])
        check(f'{stamp} is context, not truth',
              has_scope and not run.labelled(),
              'scope recorded as detected, labelled() empty')


def t_readonly():
    print('\na legacy run is read-only')
    d = os.path.join(ROOT, 'docs', 'ads', 'runs', '20260802_012217')
    run = CaptureRun.load_dir(d)
    check('load_dir marks it readonly', run.readonly)
    try:
        run.save()
        check('save() refuses', False, 'it wrote a manifest!')
    except RuntimeError:
        check('save() refuses', True)
    check('nothing was written', not os.path.exists(os.path.join(d, MANIFEST)))


# ════════════════════════════════════════════════════════════
# Synthetic legacy runs — the fields, in a temp directory
# ════════════════════════════════════════════════════════════

def t_legacy_ads(tmp):
    print('\nlegacy ADS format: index.jsonl + meta.json')
    d = os.path.join(tmp, 'ads_run')
    os.makedirs(os.path.join(d, 'scope_6x'))
    recs = [
        dict(file='scope_6x/hip_v0_t0000.jpg', scope='scope_6x', state='hip',
             t_ms=0, view=0, weapon='kar98k', slot=2, verified='ScopeAsset6x'),
        dict(file='scope_6x/ads_v0_t0700.jpg', scope='scope_6x', state='ads',
             t_ms=700, view=0, weapon='kar98k', slot=2, steady=True),
        dict(file='iron/hip_v0_t0000.jpg', state='hip', t_ms=0, view=0),
    ]
    with open(os.path.join(d, 'index.jsonl'), 'w', encoding='utf-8') as f:
        for r in recs:
            f.write(json.dumps(r) + '\n')
    with open(os.path.join(d, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump({'stamp': 'ads_run', 'weapon': 'kar98k', 'steady_ms': 700},
                  f)

    run = CaptureRun.load_dir(d)
    check('every record is an entry', len(run.entries) == 3)
    check("'file' becomes 'capture'",
          run.entries[0]['capture'] == 'scope_6x/hip_v0_t0000.jpg'
          and 'file' not in run.entries[0])
    check('meta.json becomes facts', run.facts.get('steady_ms') == 700)
    check('the scope is the label, as DETECTED',
          run.entries[0]['labels'] == [{'slot': 'scope', 'asset': 'scope_6x',
                                        'source': LABEL_DETECTED}])
    check('a record with no scope gets no label', run.entries[2]['labels'] == [])
    # The whole point: `state` describes the procedure, not the screen, so it
    # must not arrive as something labelled() can hand out.
    check("`state` stays a fact", run.entries[1]['state'] == 'ads'
          and not any(l.get('asset') == 'ads' for l in run.entries[1]['labels']))
    check('no ground truth at all', run.labelled() == [])


def t_legacy_attachments(tmp):
    print('\nlegacy attachment format: index.json')
    d = os.path.join(tmp, 'att_run')
    os.makedirs(d)
    crops = [
        dict(file='vert_grip__grip__m416__fbg0.png', target='slots',
             key='vert_grip', slot='grip', region=[1, 2, 3, 4], read='laser',
             has_template=True, ok=False),
        dict(file='comp_ar__row03__m416__lbg0.png', target='rows',
             key='comp_ar', row=3, region=[1, 2, 3, 4], read='comp_ar',
             has_template=True, ok=True),
        dict(file='type__m416__fbg0.png', target='type', key='type',
             region=[1, 2, 3, 4], read='210', has_template=True, ok=True,
             ink=210),
    ]
    with open(os.path.join(d, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump({'gun': 2, 'angles': 6, 'targets': ['slots'],
                   'bad': [{'target': 'slots', 'key': 'vert_grip'}],
                   'crops': crops}, f)

    run = CaptureRun.load_dir(d)
    check('every crop is an entry', len(run.entries) == 3)
    check("'file' becomes 'capture'",
          run.entries[0]['capture'] == 'vert_grip__grip__m416__fbg0.png')
    check('the rebuild queue survives as a fact', len(run.facts['bad']) == 1)
    check('a slot crop is labelled by its slot',
          run.entries[0]['labels'] == [{'slot': 'grip', 'asset': 'vert_grip',
                                        'source': LABEL_DETECTED}])
    check('a row crop falls back to its target',
          run.entries[1]['labels'][0]['slot'] == 'rows')
    check("'type' has no asset to name", run.entries[2]['labels'] == [])
    check('no ground truth at all', run.labelled() == [])


# ════════════════════════════════════════════════════════════
# The current format
# ════════════════════════════════════════════════════════════

def t_roundtrip(tmp):
    print('\nwrite a run, read it back')
    d = os.path.join(tmp, 'new_run')
    run = CaptureRun.create('ads', stamp='20260803_000000', path=d,
                            quality=95, facts={'weapon': 'kar98k'})
    run.add(frame(), 'scope_6x/ads_v0_t0700.jpg', state='ads', t_ms=700,
            labels=[{'slot': 'scope', 'asset': 'ScopeAsset6x',
                     'source': LABEL_REQUESTED, 'by': 'slot readback'}])
    run.add(frame(), 'scope_4x/ads_v0_t0700.jpg', state='ads', t_ms=700,
            labels=[{'slot': 'scope', 'asset': 'ScopeAsset2x',
                     'source': LABEL_DETECTED, 'by': 'slot readback'}])
    run.add(frame(), 'plain_png', state='hip')
    run.facts['frames'] = 3
    run.save()

    check('a subdirectory capture lands where it says',
          os.path.isfile(os.path.join(d, 'scope_6x', 'ads_v0_t0700.jpg')))
    check('a name with no extension is still PNG',
          os.path.isfile(os.path.join(d, 'plain_png.png')))
    # JPEG q95 vs PNG is ~6x the disk over a 220-frame run, which is why
    # `quality` exists at all; if it stopped reaching cv2 the frames would
    # still be readable and the run would just be huge.
    with open(os.path.join(d, 'scope_6x', 'ads_v0_t0700.jpg'), 'rb') as f:
        check('it is really a JPEG', f.read(2) == b'\xff\xd8')

    back = CaptureRun.load_dir(d)
    check('version is recorded', json.load(
        open(os.path.join(d, MANIFEST), encoding='utf-8'))['version'] == VERSION)
    check('entries round-trip', len(back.entries) == 3)
    check('facts round-trip', back.facts == {'weapon': 'kar98k', 'frames': 3})
    check('quality round-trips', back.quality == 95)
    check('labelled() returns only the requested one',
          [lab['asset'] for _, lab, _ in back.labelled()] == ['ScopeAsset6x'])
    check('the capture path it hands back exists',
          os.path.isfile(back.labelled()[0][2]))
    check('a written run is not readonly', not back.readonly)
    check('frame() reads the capture', back.frame(back.entries[0]) is not None)

    # A new run written into a legacy root has both files beside it. The
    # manifest must win, or capture_ads' own --report would read the old index.
    with open(os.path.join(d, 'index.jsonl'), 'w', encoding='utf-8') as f:
        f.write(json.dumps({'file': 'x.jpg', 'state': 'hip'}) + '\n')
    check('manifest.json beats a stray index.jsonl',
          len(CaptureRun.load_dir(d).entries) == 3)


def t_capture_ads_roundtrip(tmp):
    """capture_ads' own writer -> its own --report, with no game.

    The gap this closes: every other check here builds a run by hand, so a
    capture_scope() that stopped writing `state` or wrote the capture path
    under the wrong key would pass all of them and only fail on the game, hours
    into a run that cannot be re-taken cheaply. The screen and the Pico are
    stubbed; everything between them is the real code path.
    """
    print('\ncapture_ads writes a run its own --report can read')
    from calibration import capture_ads as ca

    shot = np.zeros((240, 320, 3), np.uint8)
    shot[60:180, 80:240] = 40                      # something for the median

    class FakeAimer:
        def look_at(self, yaw, pitch, settle=0.0):
            pass

        def ads_burst(self, sample_ms):
            # A brighter middle for the ADS frames, so hip and ads differ the
            # way --report expects and the numbers it prints are not all zero.
            lit = shot.copy()
            lit[100:140, 140:180] = 200
            return [(ms, float(ms), lit) for ms in sample_ms]

    d = os.path.join(tmp, 'ads_written')
    run = CaptureRun.create('ads', stamp='20260803_010203', path=d, quality=95)
    real_grab, ca.grab = ca.grab, lambda: shot.copy()
    try:
        labels, why = ca.scope_label('scope_6x', ca.scope_info('scope_6x')[1],
                                     operator=False)
        n = ca.capture_scope('scope_6x', FakeAimer(), run, [(0, 0)],
                             (400, 700), False, 'kar98k', 2,
                             ca.scope_info('scope_6x')[1], labels)
    finally:
        ca.grab = real_grab

    check('one view yields hip + samples + hip_after', n == 4, f'{n} frames')
    check('every frame is in the manifest', len(run.entries) == n)
    check('the frames are on disk',
          all(os.path.isfile(os.path.join(d, e['capture']))
              for e in run.entries))
    check('the scope label reaches every frame',
          len(run.labelled()) == n, f'{len(run.labelled())} ground truth')
    check('`state` is a fact, not a label',
          {e['state'] for e in run.entries} == {'hip', 'ads', 'hip_after'}
          and all(lab['slot'] == 'scope'
                  for e in run.entries for lab in e['labels']))
    check('the stuck check ran', 'stuck_diff' in run.entries[-1])

    back = CaptureRun.load_dir(d)
    check('--report can load what was written', len(back.entries) == n)
    check('probe.jpg builds from the entries', ca.probe_sheet(back) is not None)
    buf = io.StringIO()                    # --report prints a whole table
    with contextlib.redirect_stdout(buf):
        rc = ca.report(d)
    check('--report runs offline on it', rc == 0,
          f'{len(buf.getvalue().splitlines())} lines of heatmap summary')


def t_v1_manifest(tmp):
    """A manifest written before facts/quality existed still loads."""
    print('\nversion 1 manifests still load')
    d = os.path.join(tmp, 'v1_run')
    os.makedirs(d)
    with open(os.path.join(d, MANIFEST), 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'kind': 'slot_scan', 'stamp': 's', 'note': 'n',
                   'entries': [{'capture': 'a.png', 'labels': []}]}, f)
    run = CaptureRun.load_dir(d)
    check('loads', len(run.entries) == 1 and run.note == 'n')
    check('missing facts default to empty', run.facts == {})
    check('missing quality defaults to None', run.quality is None)


# ════════════════════════════════════════════════════════════
# The decision tables
# ════════════════════════════════════════════════════════════

def t_scope_label():
    """capture_ads: what is known about the scope on the gun."""
    print('\ncapture_ads.scope_label()')
    from calibration.capture_ads import scope_info, scope_label
    # The catalogue's own asset names, not made-up ones: half of what this
    # function decides is whether the readback matches what --scopes asked for,
    # and a literal here would let scope_info() drift out from under it.
    six, four = scope_info('scope_6x')[1], scope_info('scope_4x')[1]
    cases = [
        # key,     verified, operator, expect source, expect asset
        ('scope_6x', six, False, LABEL_REQUESTED, six),
        ('scope_6x', four, False, LABEL_DETECTED, four),
        ('scope_6x', '', False, LABEL_DETECTED, ''),
        ('scope_6x', None, False, None, None),
        ('scope_6x', None, True, LABEL_REQUESTED, six),
        ('iron', '', False, LABEL_REQUESTED, ''),
        ('iron', six, False, LABEL_DETECTED, six),
    ]
    for key, verified, operator, want_src, want_asset in cases:
        labs, why = scope_label(key, verified, operator)
        got_src = labs[0]['source'] if labs else None
        got_asset = labs[0]['asset'] if labs else None
        check(f'{key:9} verified={verified!r:15} operator={operator}',
              got_src == want_src and got_asset == want_asset,
              f'-> {got_src or "no label"} {got_asset!r}  ({why})')

    # The one that matters most, stated as its own claim: an intention nobody
    # confirmed produces NO ground truth. This is the bug that filed 40 frames
    # of the wrong weapon under `iron`.
    labs, _ = scope_label('scope_8x', None, False)
    check('an unconfirmed equip yields no ground truth', labs == [])


def t_label_for():
    """collect_templates: which targets can claim ground truth."""
    print('\ncollect_templates.label_for()')
    from calibration.collect_templates import label_for
    cases = [
        ('slots', 'vert_grip', 'grip', LABEL_REQUESTED, 'grip'),
        ('rows', 'comp_ar', None, LABEL_REQUESTED, 'inventory'),
        ('plate', 'm416', None, None, None),
        ('type', 'type', None, None, None),
    ]
    for target, key, slot, want_src, want_slot in cases:
        labs = label_for(target, key, slot, 'spawn')
        got_src = labs[0]['source'] if labs else None
        got_slot = labs[0]['slot'] if labs else None
        check(f'{target:6} -> {want_src or "no label"}',
              got_src == want_src and got_slot == want_slot,
              f'{got_slot}')
    check("`by` reaches the label",
          label_for('slots', 'k', 'grip', 'operator')[0]['by'] == 'operator')

    # THE PLATE, and the condition it now turns on. A crop nobody watched
    # arrive must not become a sample: the plate OCR is the detector under
    # test, so a spawn that silently produced nothing leaves the previous
    # weapon in front of the camera under the new name.
    check('a plate with no arrival is not ground truth',
          label_for('plate', 'm416', None, 'operator') == [])
    labs = label_for('plate', 'm416', None, 'spawn', arrived=True)
    check('a plate watched arriving IS ground truth',
          bool(labs) and labs[0]['source'] == LABEL_REQUESTED
          and labs[0]['asset'] == 'm416', f'{labs}')

    # The predicate itself, on the MEASURED numbers. Both directions matter:
    # a floor alone would believe scenery, and no floor at all would believe a
    # gun that was already sitting there.
    print('\ncollect_templates.plate_arrived()')
    from calibration.collect_templates import plate_arrived, PLATE_INK_MIN, PLATE_INK_MAX
    check('cleared rack -> a real plate (0 -> 682, measured)',
          plate_arrived(0, 682) is True)
    check('the two extremes of the occupied samples',
          plate_arrived(0, 679) and plate_arrived(0, 901))
    check('a plate that was ALREADY there is not an arrival',
          plate_arrived(682, 857) is False)
    check('saturation is scenery, not glyphs (0 -> 11250, measured Tab-shut)',
          plate_arrived(0, 11250) is False)
    check('nothing arrived (0 -> 0)', plate_arrived(0, 0) is False)
    check('the band brackets every occupied sample seen',
          PLATE_INK_MIN < 679 and 901 < PLATE_INK_MAX)

    # A row photographed before anything was fitted has no key yet -- the game
    # sorts 库存 its own way, so which row holds which part is not known until
    # the fits reveal it. A label naming nothing is worse than no label: it
    # claims ground truth for the one thing still unknown.
    print('\ncollect_templates: a crop with no key gets no label')
    for target in ('slots', 'rows', 'plate'):
        check(f'{target} with key=None',
              label_for(target, None, 'grip', 'spawn', arrived=True) == [])


def main():
    tmp = tempfile.mkdtemp(prefix='capture_run_test_')
    try:
        t_real_ads()
        t_readonly()
        t_legacy_ads(tmp)
        t_legacy_attachments(tmp)
        t_roundtrip(tmp)
        t_capture_ads_roundtrip(tmp)
        t_v1_manifest(tmp)
        t_scope_label()
        t_label_for()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f'{len(FAILS)} check(s) failed:')
        for n in FAILS:
            print(f'  {n}')
        return 1
    print('capture-run format holds')
    return 0


if __name__ == '__main__':
    sys.exit(main())
