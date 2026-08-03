"""Why find_menu() came back empty on a panel that is plainly open.

    pixi run python tools/probe_spawner_layout.py

Opens the item spawner, saves what it sees, and prints the column/row
segmentation step by step. Nothing is spawned and nothing is fired.

The failure this exists for: harvest syncs the panel and gets 21 categories,
then 14, then 10, then 0, off a panel nobody touched in between. A screenshot
taken after the run is no use -- the panel is closed by then -- so the picture
has to be taken while it is up.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))

import cv2
import numpy as np

from detector.geometry import segments
from detector.spawner_layout import (find_menu, bright_mask,
                                     PANEL_Y0, PANEL_Y1, MIN_COL_PIX, COL_GAP)
from control.focus import ensure_focus
from press.pico_mouse import get_mouse

from control.spawner import SpawnerControl, shoot_parked

OUT = os.path.join(ROOT, 'docs', 'spawner', 'spawner_open.png')


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    if not ensure_focus(countdown_s=5, label='the spawner layout probe'):
        print('[!] could not focus the game')
        return 1
    time.sleep(0.6)

    sc = SpawnerControl(verbose=False)
    print(f'panel says open: {sc.panel_open()}')
    if not sc.ensure_panel(True):
        print('[!] the panel would not open — comma is not landing, or the '
              'icon templates no longer match')
        return 1
    print(f'panel says open: {sc.panel_open()}  (after ensure_open)')

    img = shoot_parked(settle=0.35)
    cv2.imwrite(OUT, img)
    print(f'saved {OUT}')

    mask = bright_mask(img)
    colsum = mask[PANEL_Y0:PANEL_Y1].sum(axis=0)
    cols = segments(colsum, MIN_COL_PIX, min_len=40, gap=COL_GAP)
    print(f'\nbright pixels in the panel band y={PANEL_Y0}..{PANEL_Y1}: '
          f'{int(mask[PANEL_Y0:PANEL_Y1].sum())}')
    print(f'column segments at MIN_COL_PIX={MIN_COL_PIX}: {cols}')
    if not cols:
        # The threshold is the first thing to suspect: it is a pixel count per
        # screen column, so a panel drawn dimmer (a different background behind
        # a translucent panel) drops under it wholesale.
        for t in (MIN_COL_PIX // 2, MIN_COL_PIX // 4, 5, 2):
            print(f'  at MIN_COL_PIX={t}: {segments(colsum, t, min_len=40, gap=COL_GAP)}')
        print(f'  colsum max {int(colsum.max())} at x={int(np.argmax(colsum))}')

    menu = find_menu(img, verbose=True)
    print(f'\nfind_menu -> {{col: rows}} = '
          f'{ {k: len(v) for k, v in menu.items()} }')

    sc.ensure_panel(False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
