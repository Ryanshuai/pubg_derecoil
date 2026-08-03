"""Where to put the spawner-screen match threshold.

Positives: every full screenshot known to be on the spawner screen.
Negatives: full screenshots of ordinary gameplay.
"""
import glob
import os
import sys

import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from detector.spawner_detector import SpawnerDetector, build_templates

RUNS = os.path.join(ROOT, 'docs', 'spawner', 'runs')
HERE = os.path.join(ROOT, 'temp_debug')

good = os.path.join(RUNS, '20260801_210656', '00_baseline.png')
print('templates from', good)
for p in build_templates(cv2.imread(good)):
    t = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    print(f'  {os.path.basename(p)}  {t.shape[1]}x{t.shape[0]}  '
          f'{int((t > 0).sum())} set px')
det = SpawnerDetector()

pos = sorted(glob.glob(os.path.join(RUNS, '*', '00_baseline.png')))
pos += sorted(glob.glob(os.path.join(RUNS, '20260801_210656', '*_open.png')))
pos += [os.path.join(HERE, 'screenshot_main_20260801_204338.png')]

neg = []
for pat in ('raw_screen.png', 'reload_*.png', 'ammo_now.png'):
    neg += sorted(glob.glob(os.path.join(HERE, pat)))

print(f'\n{"file":<46}{"icon scores":<34}{"verdict"}')
worst_pos, best_neg = 1.0, 0.0
for label, files in (('POS', pos), ('NEG', neg)):
    for f in files:
        img = cv2.imread(f)
        if img is None or img.shape[:2] != (1440, 3440):
            print(f'  skip {os.path.basename(f)} '
                  f'({None if img is None else img.shape})')
            continue
        s = det.scores(img)
        ok = det.classify(img)
        name = os.path.relpath(f, ROOT)
        if len(name) > 44:
            name = '...' + name[-41:]
        print(f'{label} {name:<44}'
              f'{" ".join(f"{v:.3f}" for v in s):<34}{ok}')
        if label == 'POS':
            worst_pos = min(worst_pos, min(s))
        else:
            best_neg = max(best_neg, min(s))

print(f'\nworst positive (min icon score): {worst_pos:.3f}')
print(f'best negative  (min icon score): {best_neg:.3f}')
