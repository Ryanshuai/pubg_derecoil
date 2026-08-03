"""Pin "is the Tab screen up" against every stored full-screen shot.

Ground truth is which directory a shot lives in — docs/ads/runs/** was captured
while shooting, Tab shut; docs/compat/runs/**, docs/runs/** and
docs/tab_inventory*.png are captures OF the Tab screen. Offline: no game, no
hardware.

    pixi run tab-open

WHY THIS EXISTS. Three copies of the predicate had drifted apart — the channel
maximum here, cv2 luma in control/gun.py, a closed band instead of an open one
in calibration/state.py — and all three counted bright pixels and nothing else.
HUD_REGIONS['type'] sits over the training range's sky, and ADS magnifies a
patch of pale blue straight into it, at a brightness that lands inside the
count band. Scored over 960 shots at 3440x1440 (92 Tab up, 868 Tab shut):

    max-chan, count only  (was detector/)      15 false-open   0 false-shut
    luma,     count only  (was control/gun)    10 false-open   0 false-shut
    max-chan + dark floor (shipping)            0 false-open   0 false-shut

Not cosmetic. A dozen `cond: '!tab_open'` entries in config.py gate on this,
including whether recoil compensation runs, so a false "open" while aiming at
the sky silently disarms the thing this repository is for.

The test is two-sided ON PURPOSE. Chasing the false positives alone invites a
threshold that reads everything as shut, which fails silently in the other
direction — every stored Tab capture must still come back open.

Margins it also pins, so a threshold nudge fails here rather than in the game:

    bright count   Tab up     190 .. 249      inside the 150..400 band
    dark floor     Tab up      23 ..  91
                   Tab shut   190 .. 199      the 15 sky frames in the band

The floor gap is 91..190 and only exists once the count band has passed: over
ALL Tab-shut frames the floor runs 27..227, because a dark crop with no ink is
dark too. TAB_DARK_FLOOR_MAX = 150 sits in the middle of the conditional gap.

NOT IN THE CORPUS: docs/tab_full_check.png. It looks like a 3440x1440 Tab
capture and is not one — it is a black canvas with two rectangles pasted on it
(a montage of attachment icons composited over gameplay, plus the Tab regions'
bounding box), so 'type' reads as literally all zeros. It was labelled "Tab up"
in the first pass of this probe and produced the sole false negative of every
predicate scored above. The label was wrong, not the predicates.
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import cv2
import numpy as np

from config import (HUD_REGIONS, TAB_PIXEL_THRESH, TAB_COUNT_MIN,
                    TAB_COUNT_MAX, TAB_DARK_FLOOR_MAX)
from detector.tab_detector import TabTypeDetector

Y, X, H, W = HUD_REGIONS['type']
SHOT = (1440, 3440)          # the coordinates above assume this and only this


def truth(path):
    """Was the Tab screen up when this was taken? None = do not know."""
    p = path.replace('\\', '/')
    if '/ads/runs/' in p:
        return False                       # gameplay frames, Tab shut
    if '/compat/runs/' in p or '/docs/runs/' in p:
        return True                        # scans OF the Tab screen
    if 'tab_inventory' in p:
        return True
    # docs/tab_full_check.png deliberately falls through to None -- see the
    # module docstring. It is a composite, not a capture.
    return None


def corpus():
    """[(tab_was_up, crop, relative path)] for every shot with a known answer."""
    rows = []
    pats = ['docs/**/*.jpg', 'docs/**/*.png']
    for path in sorted(sum((glob.glob(os.path.join(ROOT, p), recursive=True)
                            for p in pats), [])):
        want = truth(path)
        if want is None:
            continue
        img = cv2.imread(path)
        if img is None or img.ndim != 3 or img.shape[:2] != SHOT:
            continue
        rows.append((want, img[Y:Y + H, X:X + W].copy(),
                     os.path.relpath(path, ROOT)))
    return rows


def main():
    rows = corpus()
    if not rows:
        raise SystemExit('no stored 3440x1440 shots under docs/')

    det = TabTypeDetector()
    n_up = sum(r[0] for r in rows)
    print(f'{len(rows)} shots at {SHOT[1]}x{SHOT[0]}: '
          f'{n_up} Tab up, {len(rows) - n_up} Tab shut')

    bad = []
    for want, crop, rel in rows:
        got = bool(det.classify({'type': crop}))
        if got != want:
            m = np.max(crop, axis=2)
            bad.append((rel, want, int((m > TAB_PIXEL_THRESH).sum()),
                        int(np.percentile(m, 10))))

    false_open = [b for b in bad if not b[1]]
    false_shut = [b for b in bad if b[1]]
    print(f'  false-open {len(false_open)}   false-shut {len(false_shut)}')
    for rel, _, count, floor in bad:
        print(f'  FAIL {rel}: count {count} '
              f'(band {TAB_COUNT_MIN}..{TAB_COUNT_MAX}), '
              f'floor {floor} (max {TAB_DARK_FLOOR_MAX})')

    # The margins, so a threshold nudged into the gap fails here loudly rather
    # than in the game quietly.
    ok = not bad
    stats = {}
    for tag, keep in (('up', True), ('shut', False)):
        sel = [np.max(c, axis=2) for w, c, _ in rows if w is keep]
        stats[tag] = ([int((m > TAB_PIXEL_THRESH).sum()) for m in sel],
                      [int(np.percentile(m, 10)) for m in sel])

    counts_up = stats['up'][0]
    floors_up = stats['up'][1]
    floors_sky = [f for c, f in zip(*stats['shut'])
                  if TAB_COUNT_MIN < c < TAB_COUNT_MAX]
    print(f'  Tab up   count {min(counts_up)}..{max(counts_up)}  '
          f'floor {min(floors_up)}..{max(floors_up)}')
    print(f'  Tab shut, count inside the band: {len(floors_sky)} shots, '
          f'floor {min(floors_sky)}..{max(floors_sky)}')

    gap = min(floors_sky) - max(floors_up)
    print(f'  floor gap {max(floors_up)}..{min(floors_sky)} '
          f'({gap} wide), threshold {TAB_DARK_FLOOR_MAX}')
    if not max(floors_up) < TAB_DARK_FLOOR_MAX < min(floors_sky):
        print('  FAIL TAB_DARK_FLOOR_MAX is no longer inside the gap')
        ok = False

    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
