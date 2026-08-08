"""Offline detector regression over the checked-in full-screen screenshots.

Every detector is deterministic given a frame, so a dependency bump can be
checked numerically instead of trusted: dump the results before the upgrade,
dump them after, diff. Covers the cv2 call sites that `smoke` does not reach —
matchTemplate, phaseCorrelate, connectedComponents, findContours, adaptive
threshold, Sobel/Canny — plus the sklearn RF. (There were four torch heads
here too; the last one went on 2026-08-08, see detector/fire_mode_detector.)

    pixi run python tools/regression_check.py --save      # before upgrading
    pixi run python tools/regression_check.py --compare   # after

Needs no game and no hardware; reads only files under calibration/artifacts/.
"""
import argparse
import glob
import json
import math
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass


from config import HUD_REGIONS, SCREEN_H, SCREEN_W

DEFAULT_BASELINE = os.path.join(ROOT, 'data', 'regression_baseline.json')

# Labels must match exactly; floats get a tolerance. phaseCorrelate is an FFT
# reduction over 128x128 float32, so a library rebuild moves the sub-pixel
# reading in the 4th decimal — measured 6e-4 px across the opencv 4.11 -> 5.0
# bump. That is three orders below the 0.3-3% trial-to-trial scatter the K
# calibration already lives with, so it is noise, not regression. Anything
# above this is worth looking at.
ATOL = 2e-3


def _frames():
    """Full-screen shots under calibration/artifacts/, sorted so the report is stable."""
    out = []
    for f in sorted(glob.glob(os.path.join(ROOT, 'calibration', 'artifacts', '**', '*.png'), recursive=True)):
        img = cv2.imread(f)
        if img is not None and img.shape[:2] == (SCREEN_H, SCREEN_W):
            out.append((os.path.relpath(f, ROOT).replace('\\', '/'), img))
    return out


def _labelled_crops():
    """Crops under calibration/artifacts/ whose sidecar carries GROUND TRUTH.

    -> [(relpath, img, [label, ...])]

    This is the half of the corpus that was worth nothing until sidecars
    existed. `_frames()` above keeps a file only if it happens to be
    full-screen, so every cut-out region — which is most of what is on disk,
    and exactly what template matching wants — was invisible to the harness.

    And the assertion is a different KIND: _frames() compares against last
    time, which catches a library bump; this compares against the truth, which
    catches a wrong answer. A detector that has silently drifted passes the
    first and fails the second.

    DETECTED labels are not returned — snapshot.truth() refuses them. A
    detector's own reading cannot be the truth it is judged against.
    """
    from detector.snapshot import KIND_CROP, read_sidecar, truth
    out = []
    for f in sorted(glob.glob(os.path.join(ROOT, 'calibration', 'artifacts', '**', '*.png'),
                              recursive=True)):
        meta = read_sidecar(f)
        if not meta or meta.get('kind') != KIND_CROP:
            continue
        labs = truth(meta)
        if not labs:
            continue
        img = cv2.imread(f)
        if img is None:
            continue
        out.append((os.path.relpath(f, ROOT).replace('\\', '/'), img, labs))
    return out


def _checkers():
    """target -> (callable(crop) -> value). Built once; each is one detector.

    A target with no checker is REPORTED, not skipped quietly: an unchecked
    label looks exactly like a passing one in a summary line, and the whole
    point of the sidecar is to stop truth from going unexamined.
    """
    from detector.attachment_detector import AttachmentDetector
    from detector.weapon_template_detector import TabWeaponDetector
    from detector.tab_detector import TabTypeDetector

    att = AttachmentDetector()
    gun = TabWeaponDetector()
    tab = TabTypeDetector()

    def _att(crop):
        # classify() takes a full frame now, so a single crop goes through
        # classify_crop instead. The slot name picks the template bank; no
        # weapon is named, which is the harder case and the right one to
        # assert against a labelled crop.
        return att.classify_crop(crop, 'muzzle')

    def _gun(crop):
        return (gun.classify({'gun_name_1': crop}) or [None])[0]

    return {
        'attachment': _att,
        'weapon_name': _gun,
        'tab_open': lambda crop: bool(tab.classify({'type': crop})),
    }


def check_labels():
    """Run every ground-truth crop past its detector. -> (n_ok, [failure, ...])"""
    samples = _labelled_crops()
    if not samples:
        return 0, [], 0
    checkers = _checkers()
    ok, bad, unchecked = 0, [], 0
    for name, img, labs in samples:
        for lab in labs:
            fn = checkers.get(lab['target'])
            if fn is None:
                unchecked += 1
                bad.append((name, lab['target'], lab['value'],
                            f'NO CHECKER for target {lab["target"]!r}'))
                continue
            try:
                got = fn(img)
            except Exception as e:
                got = f'ERROR {type(e).__name__}: {e}'
            if got == lab['value']:
                ok += 1
            else:
                bad.append((name, lab['target'], lab['value'], got))
    return ok, bad, unchecked


def _crops(img, names):
    out = {}
    for n in names:
        y, x, h, w = HUD_REGIONS[n]
        out[n] = img[y:y + h, x:x + w]
    return out


def _jsonable(v):
    """numpy scalars and containers -> plain JSON, floats rounded."""
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) else round(f, 6)
    if isinstance(v, (np.integer, int)) and not isinstance(v, bool):
        return int(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, np.ndarray):
        return _jsonable(v.tolist())
    return v


def collect():

    from detector.game_state import GameState
    from detector.weapon_hud_detector import WeaponHudDetector
    from detector.fire_mode_detector import FireModeDetector
    from detector.posture_detector import PostureDetector
    from detector.highlight_detector import HighlightDetector
    from detector.tab_detector import TabTypeDetector
    from detector.weapon_template_detector import TabWeaponDetector
    from detector.attachment_detector import AttachmentDetector
    from detector.spawner_detector import SpawnerDetector
    from detector.view_tracker import ViewTracker

    state = GameState()
    weapon_hud = WeaponHudDetector()          # exemplar bank + PCA, no torch
    fire_mode = FireModeDetector()            # sklearn RF, 8 structural features
    posture = PostureDetector()               # Canny / Sobel / connectedComponents
    highlight = HighlightDetector()           # dewhite + red-channel, no templates
    tab_type = TabTypeDetector()
    tab_weapon = TabWeaponDetector()          # white-text mask + matchTemplate
    attachment = AttachmentDetector()
    spawner = SpawnerDetector()               # matchTemplate on binary masks
    tracker = ViewTracker()                   # phaseCorrelate

    att_regions = [k for k in HUD_REGIONS if k.startswith('att_')]
    results = {}
    prev_patches = None

    frames = _frames()
    if not frames:
        raise SystemExit(f"no {SCREEN_W}x{SCREEN_H} screenshots under calibration/artifacts/")

    for name, img in frames:
        row = {}

        def _run(key, fn):
            try:
                row[key] = _jsonable(fn())
            except Exception as e:
                row[key] = f"ERROR {type(e).__name__}: {e}"

        _run('weapon_hud', lambda: weapon_hud.classify(_crops(img, ['weapon_1', 'weapon_2'])))
        _run('fire_mode', lambda: fire_mode.classify(_crops(img, ['fire_mode'])))
        _run('posture', lambda: posture.classify(_crops(img, ['posture'])))
        _run('highlight', lambda: highlight.classify(_crops(img, ['weapon_1', 'weapon_2'])))
        _run('tab_type', lambda: tab_type.classify(_crops(img, ['type'])))
        _run('tab_weapon', lambda: tab_weapon.classify(_crops(img, ['gun_name_1', 'gun_name_2'])))
        _run('attachment', lambda: attachment.classify(_crops(img, att_regions)))
        _run('spawner_scores', lambda: spawner.scores(img))
        _run('spawner_class', lambda: spawner.classify(img))

        # phaseCorrelate against the previous frame — the pair is arbitrary,
        # what matters is that the number is reproducible.
        patches = [np.ascontiguousarray(img[y:y + h, x:x + w, tracker.channel])
                   for (y, x, h, w) in tracker.regions().values()]
        if prev_patches is not None:
            def _measure():
                m = tracker.measure_pair(prev_patches, patches)
                return {'dx': m.dx, 'dy': m.dy, 'mad': m.mad,
                        'n_valid': m.n_valid, 'per_patch_dy': m.per_patch_dy}
            _run('view_tracker', _measure)
        prev_patches = patches

        results[name] = row
        print('.', end='', flush=True)

    print()
    return {
        'versions': {
            'python': sys.version.split()[0],
            'numpy': np.__version__,
            'cv2': cv2.__version__,
        },
        'n_frames': len(frames),
        'results': results,
    }


def _diff(a, b, path=''):
    """Yield human-readable differences between two nested structures."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                yield f"{path}/{k}: only in new"
            elif k not in b:
                yield f"{path}/{k}: only in baseline"
            else:
                yield from _diff(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            yield f"{path}: length {len(a)} -> {len(b)}"
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                yield from _diff(x, y, f"{path}[{i}]")
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        if abs(a - b) > ATOL:
            yield f"{path}: {a} -> {b}"
    elif a != b:
        yield f"{path}: {a!r} -> {b!r}"


def _report_labels():
    """The ground-truth pass on its own. -> exit code.

    Separate from --compare on purpose. --compare answers "did anything move
    since the baseline", and takes a while; this answers "is any
    detector wrong", needs no baseline at all, and is the one worth running in
    the gate set after touching a detector or a template.
    """
    ok, bad, unchecked = check_labels()
    total = ok + len(bad)
    if not total:
        print('no ground-truth crops on disk yet.\n\n'
              'A crop earns one by being saved through detector.snapshot.snap()'
              ' with a REQUESTED label. Until then the corpus is full-screen '
              'shots compared against themselves.')
        return 0
    print(f'{ok}/{total} ground-truth crops read correctly'
          + (f'   ({unchecked} had no checker)' if unchecked else ''))
    for name, target, want, got in bad:
        print(f'  FAIL {name}\n       {target}: want {want!r}, got {got!r}')
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--save', action='store_true', help='write the baseline')
    ap.add_argument('--compare', action='store_true', help='diff against the baseline')
    ap.add_argument('--baseline', default=DEFAULT_BASELINE)
    ap.add_argument('--labels', action='store_true',
                    help='only run the ground-truth crop assertions (fast, '
                         'no baseline)')
    args = ap.parse_args()
    if not (args.save or args.compare or args.labels):
        ap.error('pass --save, --compare or --labels')

    if args.labels:
        return _report_labels()

    cur = collect()
    print(f"{cur['n_frames']} frames  |  " +
          '  '.join(f"{k} {v}" for k, v in cur['versions'].items()))

    if args.save:
        os.makedirs(os.path.dirname(args.baseline), exist_ok=True)
        with open(args.baseline, 'w', encoding='utf-8') as f:
            json.dump(cur, f, indent=1, ensure_ascii=False, sort_keys=True)
        print(f"baseline written: {os.path.relpath(args.baseline, ROOT)}")
        return 0

    with open(args.baseline, encoding='utf-8') as f:
        base = json.load(f)
    print("baseline versions: " +
          '  '.join(f"{k} {v}" for k, v in base['versions'].items()))

    # Corpus coverage is reported apart from value drift, because they are
    # different failures and the loud one hides the quiet one. 47 of the
    # baseline's 52 frames live under calibration/artifacts/spawner/runs/, which is run product
    # and stays out of git -- so a fresh clone is missing them and every one
    # would land in _diff as "only in baseline", burying an actual reading
    # that moved. Only the overlap is diffed; what is absent is named.
    missing = sorted(set(base['results']) - set(cur['results']))
    added = sorted(set(cur['results']) - set(base['results']))
    if missing:
        print(f"\n{len(missing)} baseline frame(s) not on disk -- NOT CHECKED:")
        for m in missing[:10]:
            print('  ' + m)
        if len(missing) > 10:
            print(f"  ... {len(missing) - 10} more")
        print("  (run product is not in git; re-capture or ignore, but know "
              "the corpus is partial)")
    if added:
        print(f"\n{len(added)} new frame(s) with no baseline entry:")
        for a in added[:10]:
            print('  ' + a)
        if len(added) > 10:
            print(f"  ... {len(added) - 10} more")

    shared = set(base['results']) & set(cur['results'])
    diffs = list(_diff({k: base['results'][k] for k in shared},
                       {k: cur['results'][k] for k in shared}))
    if not diffs:
        print(f"\nidentical across all {len(shared)} compared frames "
              f"(atol={ATOL})")
        return 0
    print(f"\n{len(diffs)} difference(s):")
    for d in diffs[:80]:
        print('  ' + d)
    if len(diffs) > 80:
        print(f"  ... {len(diffs) - 80} more")
    return 1


if __name__ == '__main__':
    sys.exit(main())
