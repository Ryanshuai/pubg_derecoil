"""Live verification of ROW_PITCH, on a real Tab frame. Drives the game.

Two questions, and they are different:

  1. WHERE ARE THE ROWS?  For each occupied row, the (dy, dx) at which the
     icon template reproduces the crop. The refit predicts ~0 everywhere; the
     old constants gave +2 at row 0 falling to -1 at row 11. This is measured
     on a frame the fit never saw.

  2. DO DRAGS STILL LAND?  `row_point` moved by at most 1 px, and one drag
     cannot tell 93% from 92%. So this fires a burst and reads the landing
     rate out of the drag journal, which is the only statistic that can.
"""
import os
import sys
import time

sys.path.insert(0, r'D:\10_projects\pubg_derecoil')

import cv2
import numpy as np

from control.session import ensure_ready
from control.spawner import SpawnerControl
from control.inventory import InventoryControl
from control.kitting import BACKPACK
from capture.cropper import capture_screen
from calibration.sweep import Rig
from detector.attachment_detector import AttachmentDetector
from detector.tab_items import TabItemDetector, ROW_MSE_MAX, ROW_MARGIN_MIN
from detector.tab_layout import icon_box, ROW_Y_FIRST, ROW_PITCH

OUT = r'D:\agent-space'
PARTS = ['comp_ar', 'flash_ar', 'supp_ar', 'brake_ar', 'half_grip',
         'vert_grip', 'tilted_grip', 'heavy_stock', 'tactical_stock',
         'red_dot', 'holo', 'scope_2x', 'quick_ar']


def locate(frame, det):
    """(row, name, best_dy, best_dx, mse) for every occupied row."""
    out = []
    span = tuple((dy, dx) for dy in range(-4, 5) for dx in range(-2, 3))
    for r in range(13):
        x0, y0, x1, y1 = icon_box(r, 'inventory')
        cell = frame[y0:y1, x0:x1]
        if not det.drawn(cell):
            continue
        cf = cell.astype(np.float32)
        nm = min(det._templates, key=lambda n: det.score(cf, n, shifts=span))
        best = min(((det.score(cf, nm, shifts=((dy, dx),)), dy, dx)
                    for dy, dx in span))
        out.append((r, nm, best[1], best[2], best[0]))
    return out


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    print(f'constants under test: ROW_Y_FIRST={ROW_Y_FIRST} '
          f'ROW_PITCH={ROW_PITCH}')
    ready = ensure_ready(label='row pitch probe', countdown_s=4)
    if not ready['ok']:
        print(f'[!] ABORT: not ready — failed at {ready["failed"]!r}')
        return 1

    rig = Rig('red_dot')
    sc, ac = SpawnerControl(verbose=False), InventoryControl(verbose=False)
    det = AttachmentDetector()
    ti = TabItemDetector(detector=det)
    try:
        rig.gun.ensure_inventory_closed()
        sc.give_many([BACKPACK])
        if not rig.gun.ensure_inventory_open():
            print('[!] the inventory would not open')
            return 1
        ac.clear_rack()
        ac.clear_inventory()
        rig.gun.ensure_inventory_closed()
        rec = sc.give_many(list(PARTS))
        if not rec.get('ok'):
            print(f'[!] the spawner refused: {rec.get("error")}')
        time.sleep(1.0)
        if not rig.gun.ensure_inventory_open():
            print('[!] the inventory would not reopen')
            return 1
        ac.frame()
        time.sleep(0.3)
        frame = capture_screen()
        os.makedirs(OUT, exist_ok=True)
        stamp = time.strftime('%Y%m%d_%H%M%S')
        cv2.imwrite(os.path.join(OUT, f'rowpitch_{stamp}.png'), frame)

        # ── 1. where are the rows ──
        rows = locate(frame, det)
        print(f'\n=== WHERE THE ROWS ARE ({len(rows)} occupied), live frame\n')
        print(f'{"row":>3} {"asset":34} {"dy":>3} {"dx":>3} {"mse":>8}  read?')
        named = 0
        for r, nm, dy, dx, mse in rows:
            item, _ = ti._read_row(frame, 'inventory', r)
            got = (item.key or '?') if item else '--'
            named += item is not None
            print(f'{r:>3} {nm:34} {dy:>+3} {dx:>+3} {mse:8.1f}  {got}')
        dys = [d for _, _, d, _, _ in rows]
        inside = sum(1 for _, _, dy, dx, _ in rows
                     if max(abs(dy), abs(dx)) <= 1)
        print(f'\n  dy range {min(dys):+d}..{max(dys):+d}   '
              f'inside the +-1 search {inside}/{len(rows)}   '
              f'named {named}/{len(rows)}')
        print(f'  (old constants on stored frames: dy +2 at row 0 -> -1 at '
              f'row 11, 87.5% inside)')

        # ── 2. do drags still land ──
        print(f'\n=== DO DRAGS LAND — clearing {len(rows)} rows to the floor\n')
        t0 = time.time()
        ac.clear_inventory()
        after = ti.detect(capture_screen())
        left = after.rows('inventory')
        print(f'  {len(rows)} rows -> {left} left after clear_inventory, '
              f'{time.time() - t0:.1f}s')
        print(f'\n  the landing rate itself: pixi run drag-log')
        return 0
    finally:
        try:
            ac.close()
        except Exception:
            pass
        rig.close()


if __name__ == '__main__':
    raise SystemExit(main())
