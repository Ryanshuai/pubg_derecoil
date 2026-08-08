"""Fit the ADS crosshair templates from labelled runs, and score them.

The templates are two medians of dewhite over the middle of the screen: one
over frames where the player was standing with the crosshair wide, one over
frames where the right button was held and it had tightened. A median across
views is what leaves only the crosshair — the scene moves between views, the
overlay does not.

    python calibration/fit_ads_detector.py                  # fit, then evaluate
    python calibration/fit_ads_detector.py --eval-only      # evaluate what is on disk

Evaluation deliberately uses runs the templates were not fitted on, including
run 20260801_222936, whose frames are all shoulder aim and none of them ADS —
the negative that a crosshair-shape match gets wrong if it only knows the wide
crosshair.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from calibration.capture_run import CaptureRun
from detector.ads_detector import AdsDetector, CROP_R, TEMPLATE_PATH, THRESHOLD
from dl_models.icon_merging import dewhite

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, 'calibration', 'artifacts', 'ads', 'runs')

# Where each template comes from: (run, predicate over one frame's record).
FIT = {
    'wide': ('20260802_022804', lambda r: r['state'] == 'hip'),
    'tight': ('20260801_222936',
              lambda r: r['state'] == 'ads' and r['t_ms'] == 400),
}

# Ground truth, which is not always the label in the file. Run 20260801_222936
# was captured while holding the right button, which never scopes in, so every
# frame of it is un-scoped no matter what its state says.
NOT_SCOPED = [
    ('20260802_022804', lambda r: r['state'] in ('hip', 'hip_after')),
    ('20260802_021631', lambda r: r['state'] in ('hip', 'hip_after')),
    ('20260801_222936', lambda r: True),
]
SCOPED = [
    ('20260802_022804', lambda r: r['state'] == 'ads' and r['t_ms'] == 700),
    ('20260802_021631', lambda r: r['state'] == 'ads' and r['t_ms'] == 400),
    ('20260802_015545', lambda r: r['state'] == 'ads' and r['t_ms'] == 400),
]


def records(run):
    """Every frame of a run, in whichever format it was stored.

    Through capture_run's adapter rather than a bare index.jsonl read: a run
    captured after 2026-08-03 writes manifest.json and NO index.jsonl, and the
    old code returned [] for it — which reads exactly like a run holding no
    matching frames. The next ADS capture would have been silently invisible
    to the fitter it exists to feed.

    The predicates above still key on `state` and `t_ms` because the adapter
    keeps those as facts. What it does NOT do is turn them into labels: see
    capture_run.py, and note that NOT_SCOPED / SCOPED below is the human
    adjudication that no capture program can produce.
    """
    path = os.path.join(RUNS, run)
    if not os.path.isdir(path):
        return []
    try:
        return CaptureRun.load_dir(path).entries
    except FileNotFoundError:
        return []


def centre(run, rec):
    img = cv2.imread(os.path.join(RUNS, run, rec['capture']))
    if img is None:
        return None
    h, w = img.shape[:2]
    cy, cx = h // 2, w // 2
    return img[cy - CROP_R:cy + CROP_R, cx - CROP_R:cx + CROP_R]


def fit():
    out = {}
    for name, (run, pred) in FIT.items():
        crops = [centre(run, r) for r in records(run) if pred(r)]
        crops = [c for c in crops if c is not None]
        if not crops:
            print(f'{name}: no frames in {run}')
            return 1
        med = np.median(np.stack([dewhite(c) for c in crops]).astype(np.float32),
                        axis=0)
        out[name] = med
        print(f'{name:6s} from {run}  {len(crops):3d} frames  '
              f'peak {med.max():.0f}  crosshair px {int((med > 60).sum())}')
    os.makedirs(os.path.dirname(TEMPLATE_PATH), exist_ok=True)
    np.savez_compressed(TEMPLATE_PATH, **out)
    print(f'-> {os.path.relpath(TEMPLATE_PATH, ROOT)}')
    return 0


def collect(det, spec):
    scores = []
    for run, pred in spec:
        for r in records(run):
            if not pred(r):
                continue
            crop = centre(run, r)
            if crop is None:
                continue
            d = dewhite(crop).astype(np.float32)
            best = -1e9
            for parts, ring in det.templates:
                bg = float(d[ring].mean())
                best = max(best, min(float(d[p].mean()) - bg for p in parts))
            scores.append((best, run, r['capture']))
    return scores


def evaluate():
    det = AdsDetector()
    pos, neg = collect(det, NOT_SCOPED), collect(det, SCOPED)
    pv = np.array([s for s, _, _ in pos])
    nv = np.array([s for s, _, _ in neg])
    print(f'\nnot scoped  n={len(pv):3d}  min {pv.min():7.1f}  '
          f'median {np.median(pv):7.1f}  max {pv.max():7.1f}')
    print(f'scoped      n={len(nv):3d}  min {nv.min():7.1f}  '
          f'median {np.median(nv):7.1f}  max {nv.max():7.1f}')
    fp = [x for x in pos if x[0] < THRESHOLD]
    fn = [x for x in neg if x[0] >= THRESHOLD]
    print(f'\nthreshold {THRESHOLD}: {len(fp)} un-scoped frames called scoped, '
          f'{len(fn)} scoped frames called un-scoped')
    print(f'margin: worst un-scoped {pv.min():.1f} vs worst scoped '
          f'{nv.max():.1f}  ({pv.min() / max(nv.max(), 1e-6):.1f}x)')
    for label, rows in (('un-scoped, weakest', sorted(pos)[:3]),
                        ('scoped, strongest', sorted(neg)[-3:][::-1])):
        print(f'  {label}:')
        for s, run, f in rows:
            print(f'    {s:7.1f}  {run}/{f}')
    return 0 if not (fp or fn) else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--eval-only', action='store_true')
    args = ap.parse_args()
    if not args.eval_only and fit():
        return 1
    return evaluate()


if __name__ == '__main__':
    sys.exit(main())
