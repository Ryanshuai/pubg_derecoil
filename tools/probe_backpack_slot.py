"""Crop the character equipment slots out of the reference Tab captures.

Answering one question offline: can "is a backpack worn" be read from
EQUIP_SLOTS['backpack'] without a template? Yes — a worn backpack draws its
artwork and an empty slot shows the blurred world behind the panel, which is a
60x gap in Laplacian variance. That gap is where control/stock.py's
BACKPACK_DETAIL_MIN comes from; re-run this after a UI change to confirm it is
still there.

    pixi run python tools/probe_backpack_slot.py [shot.png ...]

Prints the metric per slot and writes 3x crops to temp_debug/ to eyeball.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from detector.tab_layout import EQUIP_SLOTS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(ROOT, 'temp_debug')
os.makedirs(HERE, exist_ok=True)

shots = sys.argv[1:] or [
    os.path.join(ROOT, 'docs', n) for n in
    ('tab_inventory.png', 'tab_inventory_2.png', 'tab_live_aug_vss.png')]

for path in shots:
    frame = cv2.imread(path)
    if frame is None:
        print(f'cannot read {path}')
        continue
    stem = os.path.splitext(os.path.basename(path))[0]
    print(f'\n=== {stem} ===')
    for name, (x0, y0, x1, y1) in EQUIP_SLOTS.items():
        crop = frame[y0:y1, x0:x1]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        detail = float(cv2.Laplacian(gray, cv2.CV_32F).var())
        print(f'  {name:9s} detail={detail:8.1f}  mean={gray.mean():6.1f}  '
              f'std={gray.std():6.1f}  bright%={100*np.mean(gray>140):5.1f}')
        cv2.imwrite(os.path.join(HERE, f'{stem}__{name}.png'),
                    cv2.resize(crop, None, fx=3, fy=3,
                               interpolation=cv2.INTER_NEAREST))
