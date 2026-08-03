"""Offline detector regression over the checked-in full-screen screenshots.

Every detector is deterministic given a frame, so a dependency bump can be
checked numerically instead of trusted: dump the results before the upgrade,
dump them after, diff. Covers the cv2 call sites that `smoke` does not reach —
matchTemplate, phaseCorrelate, connectedComponents, findContours, adaptive
threshold, Sobel/Canny — plus the sklearn RF and the four torch heads.

    pixi run python tools/regression_check.py --save      # before upgrading
    pixi run python tools/regression_check.py --compare   # after

Needs no game and no hardware; reads only files under docs/.
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

import torch

from config import HUD_REGIONS, SCREEN_H, SCREEN_W

DEFAULT_BASELINE = os.path.join(ROOT, 'docs', 'regression_baseline.json')

# Labels must match exactly; floats get a tolerance. phaseCorrelate is an FFT
# reduction over 128x128 float32, so a library rebuild moves the sub-pixel
# reading in the 4th decimal — measured 6e-4 px across the opencv 4.11 -> 5.0
# bump. That is three orders below the 0.3-3% trial-to-trial scatter the K
# calibration already lives with, so it is noise, not regression. Anything
# above this is worth looking at.
ATOL = 2e-3


def _frames():
    """Full-screen shots under docs/, sorted so the report is stable."""
    out = []
    for f in sorted(glob.glob(os.path.join(ROOT, 'docs', '**', '*.png'), recursive=True)):
        img = cv2.imread(f)
        if img is not None and img.shape[:2] == (SCREEN_H, SCREEN_W):
            out.append((os.path.relpath(f, ROOT).replace('\\', '/'), img))
    return out


def _crops(img, names):
    out = {}
    for n in names:
        y, x, h, w = HUD_REGIONS[n]
        out[n] = img[y:y + h, x:x + w]
    return out


def _jsonable(v):
    """numpy / torch scalars and containers -> plain JSON, floats rounded."""
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
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    from detector.game_state import GameState
    from detector.weapon_dl_detector import WeaponClassifier
    from detector.fire_mode_detector import FireModeDetector
    from detector.posture_detector import PostureDetector
    from detector.highlight_detector import HighlightDetector
    from detector.tab_detector import TabTypeDetector
    from detector.weapon_template_detector import TabWeaponDetector
    from detector.attachment_detector import AttachmentDetector
    from detector.spawner_detector import SpawnerDetector
    from detector.view_tracker import ViewTracker

    state = GameState()
    weapon_dl = WeaponClassifier(device)
    fire_mode = FireModeDetector(device)      # torch head + sklearn RF
    posture = PostureDetector()               # Canny / Sobel / connectedComponents
    highlight = HighlightDetector(state)      # matchTemplate + alpha unmix
    tab_type = TabTypeDetector(device)
    tab_weapon = TabWeaponDetector()          # white-text mask + matchTemplate
    attachment = AttachmentDetector()
    spawner = SpawnerDetector()               # matchTemplate on binary masks
    tracker = ViewTracker()                   # phaseCorrelate

    att_regions = [k for k in HUD_REGIONS if k.startswith('att_')]
    results = {}
    prev_patches = None

    frames = _frames()
    if not frames:
        raise SystemExit(f"no {SCREEN_W}x{SCREEN_H} screenshots under docs/")

    for name, img in frames:
        row = {}

        def _run(key, fn):
            try:
                row[key] = _jsonable(fn())
            except Exception as e:
                row[key] = f"ERROR {type(e).__name__}: {e}"

        _run('weapon_dl', lambda: weapon_dl.classify(_crops(img, ['weapon_1', 'weapon_2'])))
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
            'torch': torch.__version__,
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--save', action='store_true', help='write the baseline')
    ap.add_argument('--compare', action='store_true', help='diff against the baseline')
    ap.add_argument('--baseline', default=DEFAULT_BASELINE)
    args = ap.parse_args()
    if not (args.save or args.compare):
        ap.error('pass --save or --compare')

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

    diffs = list(_diff(base['results'], cur['results']))
    if not diffs:
        print(f"\nidentical across all {cur['n_frames']} frames (atol={ATOL})")
        return 0
    print(f"\n{len(diffs)} difference(s):")
    for d in diffs[:80]:
        print('  ' + d)
    if len(diffs) > 80:
        print(f"  ... {len(diffs) - 80} more")
    return 1


if __name__ == '__main__':
    sys.exit(main())
