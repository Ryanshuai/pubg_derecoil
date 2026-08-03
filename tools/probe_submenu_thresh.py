"""The submenu tiles have borders, and a long list's borders bridge the row
gaps so the whole column projects as one band. Is the border dimmer than the
text — i.e. can one threshold separate them?
"""
import glob
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from detector.geometry import segments
from detector.spawner_layout import find_menu, column_boxes, \
    ROW_H_MIN, ROW_H_MAX, MIN_ROW_PIX, PANEL_Y0, PANEL_Y1

RUN = sorted(glob.glob(os.path.join(ROOT, 'docs', 'spawner', 'runs', '*')))[-1]
base = cv2.imread(os.path.join(RUN, '00_baseline.png'))
boxes = column_boxes(find_menu(base, verbose=False))

CASES = {'col1_row01': 13, 'col2_row03': 12, 'col3_row06': 3,
         'col1_row10': 2, 'col1_row09': 10}
CENTRE_TOL = 45

img = cv2.imread(os.path.join(RUN, 'col1_row01_open.png'))
g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
x0, x1 = boxes[1]
print('col1_row01, column box grey levels by row (y 380..500):')
for y in range(380, 500, 4):
    row = g[y, x0:x1]
    print(f'  y={y}  max {row.max():3d}  '
          f'>190 {int((row > 190).sum()):3d}  >210 {int((row > 210).sum()):3d}  '
          f'>230 {int((row > 230).sum()):3d}')

print(f'\n{"thresh":>7}' + ''.join(f'{k[3:]:>12}' for k in CASES))
print(f'{"":>7}' + ''.join(f'{"exp " + str(v):>12}' for v in CASES.values()))
for th in range(190, 251, 5):
    cells = []
    for key in CASES:
        col = int(key[3])
        bx = boxes[col]
        m = ((g if key == 'col1_row01' else cv2.cvtColor(
            cv2.imread(os.path.join(RUN, f'{key}_open.png')),
            cv2.COLOR_BGR2GRAY)) > th).astype(np.uint8)
        sub = m[PANEL_Y0:PANEL_Y1, bx[0]:bx[1]]
        cx = (bx[1] - bx[0]) // 2
        n = 0
        for a, b in segments(sub.sum(axis=1), MIN_ROW_PIX,
                              min_len=ROW_H_MIN, max_len=ROW_H_MAX):
            xs = np.where(sub[a:b + 1].any(axis=0))[0]
            if len(xs) and abs((int(xs[0]) + int(xs[-1])) // 2 - cx) <= CENTRE_TOL:
                n += 1
        cells.append(n)
    print(f'{th:>7}' + ''.join(
        f'{c:>12}' if c == e else f'{str(c) + " x":>12}'
        for c, e in zip(cells, CASES.values())))

