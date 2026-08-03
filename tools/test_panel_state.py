"""Check `expansions()` against the ground truth in the checked-in run shots.

Every colN_rowMM_open.png under docs/spawner/runs/ is a frame whose correct
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

from detector.spawner_layout import column_boxes, expansions, find_menu

NAME_RE = re.compile(r'col(\d+)_row(\d+)_open\.png$')


def main():
    runs = sorted(glob.glob(os.path.join(ROOT, 'docs', 'spawner', 'runs', '*')))
    if not runs:
        raise SystemExit('no runs under docs/spawner/runs/')

    total = ok = 0
    failures = []
    for run in runs:
        base = cv2.imread(os.path.join(run, '00_baseline.png'))
        if base is None:
            continue
        menu = find_menu(base, verbose=False)
        boxes = column_boxes(menu)
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
