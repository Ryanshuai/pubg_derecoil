"""How separable is 'expanded' from 'collapsed' using the text mask alone?

The row-y signature just failed on col3_row05: three submenu entries landed
close enough to the list pitch that the regular-run filter swallowed one and
the rest fell outside, leaving a signature 3px from the baseline.

A whole-window mask diff is not the answer either — the gaps *between* the
panel's column boxes show the undimmed scene, which speckles across the
threshold (355..1161 px of pure noise). Inside a column box the scene is
dimmed and quiet. So: diff only the box of the column that was clicked, and
use the other columns' boxes as the noise floor.
"""
import glob
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from detector.spawner_layout import bright_mask, find_menu, column_boxes

d = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(
    os.path.join(ROOT, 'docs', 'spawner', 'runs', '*')))[-1]

base = cv2.imread(os.path.join(d, '00_baseline.png'))
bm = bright_mask(base)
menu = find_menu(base, verbose=False)
boxes = column_boxes(menu)
print('column boxes:', boxes, '\n')
print(f'{"key":<12}{"own box":>9}{"other boxes":>13}   submenu rows')

own_all, other_all = [], []
for f in sorted(glob.glob(os.path.join(d, '*_open.png'))):
    key = os.path.basename(f).replace('_open.png', '')
    col = int(key[3])
    m = bright_mask(cv2.imread(f))
    dm = (bm ^ m).astype(np.uint8)

    n_own = 0
    n_other = 0
    for c, (x0, x1) in boxes.items():
        n = int(dm[:, x0:x1].sum())
        if c == col:
            n_own = n
        else:
            n_other += n
    own_all.append(n_own)
    other_all.append(n_other)

    x0, x1 = boxes[col]
    rows = dm[:, x0:x1].sum(axis=1)
    ys = np.where(rows > 3)[0]
    span = f'y {ys.min()}..{ys.max()}' if len(ys) else '-'
    print(f'{key:<12}{n_own:>9}{n_other:>13}   {span}')

print(f'\nown box   (real change): min {min(own_all)}  max {max(own_all)}')
print(f'other box (noise floor): min {min(other_all)}  max {max(other_all)}')

