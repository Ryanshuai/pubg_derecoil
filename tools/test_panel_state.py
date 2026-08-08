"""Check `expansions()` against the ground truth in the checked-in run shots.

Every colN_rowMM_open.png under calibration/artifacts/spawner/runs/ is a frame whose correct
answer is written in its name, and every 00_baseline.png must read as nothing
expanded. Offline: no game, no hardware, no Pico.

    pixi run panel-state
"""
import glob
import os
import re
import sys

import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from control.spawner import builtin_layout
from detector.spawner_layout import expansions

NAME_RE = re.compile(r'col(\d+)_row(\d+)_open\.png$')


def main():
    runs = sorted(glob.glob(os.path.join(ROOT, 'calibration', 'artifacts', 'spawner', 'runs', '*')))
    if not runs:
        raise SystemExit('no runs under calibration/artifacts/spawner/runs/')

    # The coordinates under test are the ones production uses. This read
    # find_menu(base) + column_boxes(menu) per run, which is the RECALIBRATE
    # path -- sync() has used the measured constants since 5b and only falls
    # back to find_menu when asked to. So the constants could go wrong by a
    # pixel and this stayed at 44/44, having quietly re-derived a second set
    # from each frame it was supposed to be judging.
    #
    # builtin_layout() rather than known_layout(): the former is the entry
    # sync() actually calls, and the latter returns dicts where expansions()
    # wants .y -- despite a docstring claiming they are the same shape.
    #
    # What this does and does not catch, measured by shifting COLUMN_BOX:
    # +40px is still 44/44, +200px drops to 28/44. The columns are 475 wide
    # with a 25px gap, and the verdict counts changed text pixels inside the
    # box, so a small slip still lands on the same words. It catches a column
    # that has moved, not a pixel that has.
    menu, boxes = builtin_layout()

    total = ok = 0
    failures = []
    for run in runs:
        base = cv2.imread(os.path.join(run, '00_baseline.png'))
        if base is None:
            continue
        name = os.path.basename(run)

        # collapsed must read as nothing expanded
        total += 1
        got = expansions(base, menu, boxes)
        if got:
            failures.append((f'{name}/00_baseline.png', None,
                             [(c, r) for c, r, _ in got]))
        else:
            ok += 1

        for p in sorted(glob.glob(os.path.join(run, 'col*_row*_open.png'))):
            m = NAME_RE.search(p.replace('\\', '/'))
            img = cv2.imread(p)
            if not m or img is None:
                continue
            want = (int(m.group(1)), int(m.group(2)))
            total += 1
            got = [(c, r) for c, r, _ in expansions(img, menu, boxes)]
            if got == [want]:
                ok += 1
            else:
                failures.append((f'{name}/{os.path.basename(p)}', want, got))

    print(f'{ok}/{total} frames read correctly')
    for path, want, got in failures:
        print(f'  FAIL {path}: want {want}, got {got}')
    return 0 if ok == total else 1


if __name__ == '__main__':
    sys.exit(main())
