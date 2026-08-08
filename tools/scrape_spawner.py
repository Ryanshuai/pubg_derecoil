"""Scrape the training-range item spawner: expand every category, shoot it,
collapse it again.

The spawner panel lists categories in columns (weapons / attachments / items).
Clicking one drops a submenu open *inline*, which pushes every entry below it
down — so the row coordinates are only valid while everything is collapsed.
Hence the cycle this module implements:

    grab a collapsed baseline  ->  find all rows once
    for each row:  click open -> screenshot -> click closed -> verify collapsed

Where the rows are is detector/spawner_layout.py; how the mouse gets there is
press/pointer.py. This file is only the capture flow around them.

Usage (from the repo root)
    # offline, no game needed
    python tools/scrape_spawner.py --from docs/spawner/runs/*/00_baseline.png
    python tools/scrape_spawner.py --recrop docs/spawner/runs/<stamp>
    python tools/scrape_spawner.py --build-icons <spawner screenshot>

    # live, no clicking: grab the panel and dump the detected layout
    python tools/scrape_spawner.py --layout-only

    # live, full run — all 21 categories, ~40s
    python tools/scrape_spawner.py
    python tools/scrape_spawner.py --columns 1,2            # only these
    python tools/scrape_spawner.py --limit 3                # rows per column
    python tools/scrape_spawner.py --start-from col2_row03  # resume an abort

Runs land in docs/spawner/runs/<timestamp>/. Per category it writes
<key>_open.png (full screen), <key>_submenu.png (just the region the expansion
changed) and, from the baseline, <key>_label.png so each capture can be tied
back to a category without OCR. summary.json records what happened to every
row.
"""
import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from config import SCREEN_H, SPAWNER_MIN_SCORE
from detector.spawner_detector import SpawnerDetector, build_templates
from capture.cropper import capture_screen
from detector.spawner_layout import (CHANGE_MIN, PARK_XY, annotate,
                                     bright_mask, changed_rows, column_boxes,
                                     column_diff, find_menu)
from press.pointer import Pointer, move_cursor
from control.focus import game_focused

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# Runs are kept under docs/, not a scratch dir: the captures are the record of
# what the spawner offers and later work builds on them.
DATA_DIR = os.path.join(ROOT, 'docs', 'spawner')
OUT_ROOT = os.path.join(DATA_DIR, 'runs')

OPEN_WAIT = 0.45       # submenu slide-open animation
CLOSE_WAIT = 0.40

# The changed-rows span ends at the last row of *text*; the box drawn around
# the final submenu entry is too dim to reach the text threshold, so the crop
# needs more slack below than above.
SUBMENU_PAD_TOP, SUBMENU_PAD_BOTTOM = 12, 30


def shoot_parked(settle=0.10):
    """Screenshot with the cursor off the panel, so no row is hover-lit."""
    move_cursor(PARK_XY)
    time.sleep(settle)
    return capture_screen()


# ════════════════════════════════════════════════════════════
# Scrape cycle
# ════════════════════════════════════════════════════════════

def expand_capture_collapse(item, pointer, base_mask, boxes, out_dir,
                            open_wait=OPEN_WAIT, close_wait=CLOSE_WAIT):
    """Click one category open, screenshot the submenu, click it shut.

    Returns a per-item record; record['ok'] is False when the category did not
    open, or when the panel could not be returned to the collapsed baseline —
    in which case every later row's coordinates are stale and the caller must
    stop.
    """
    box = boxes[item.col]
    rec = {'key': item.key, 'click': [item.click_x, item.y]}

    pointer.click_at(item.click_x, item.y)
    time.sleep(open_wait)
    shot = shoot_parked()
    cv2.imwrite(os.path.join(out_dir, f'{item.key}_open.png'), shot)

    m = bright_mask(shot)
    own = column_diff(base_mask, m, box)
    other = max((column_diff(base_mask, m, b)
                 for c, b in boxes.items() if c != item.col), default=0)
    changed = own > CHANGE_MIN
    rec['diff_px'], rec['other_col_diff_px'] = own, other
    rec['expanded'] = bool(changed)

    span = changed_rows(base_mask, m, box)
    rec['submenu_bbox'] = None
    if span:
        y0 = max(0, span[0] - SUBMENU_PAD_TOP)
        y1 = min(SCREEN_H, span[1] + SUBMENU_PAD_BOTTOM)
        rec['submenu_bbox'] = [box[0], y0, box[1], y1]
        cv2.imwrite(os.path.join(out_dir, f'{item.key}_submenu.png'),
                    shot[y0:y1, box[0]:box[1]])
    print(f'  {item.key}: {"opened" if changed else "NO CHANGE"} '
          f'(diff {own} px, other cols {other}), rows {span}')

    # Collapse: same spot, since an inline submenu opens *below* the header
    # and never moves it.
    for attempt in range(3):
        pointer.click_at(item.click_x, item.y)
        time.sleep(close_wait)
        back = column_diff(base_mask, bright_mask(shoot_parked()), box)
        if back <= CHANGE_MIN:
            rec['ok'] = bool(changed)
            rec['collapse_attempts'] = attempt + 1
            rec['residual_px'] = back
            return rec
        print(f'  {item.key}: still expanded after collapse attempt '
              f'{attempt + 1} (diff {back} px)')
    rec['ok'] = False
    rec['collapse_attempts'] = 3
    rec['residual_px'] = back
    rec['error'] = 'stuck expanded'
    return rec


def scrape(columns=None, limit=None, layout_only=False,
           countdown=5, start_from=None):
    os.makedirs(OUT_ROOT, exist_ok=True)
    out_dir = os.path.join(OUT_ROOT, time.strftime('%Y%m%d_%H%M%S'))
    os.makedirs(out_dir, exist_ok=True)

    print('>>> Switch to the game with the item spawner panel OPEN and every '
          'category COLLAPSED.')
    for s in range(countdown, 0, -1):
        print(f'    starting in {s} ...', flush=True)
        time.sleep(1.0)
    if not game_focused():
        print('[!] ABORT: game is not the foreground window.')
        return None

    base = shoot_parked(settle=0.25)
    cv2.imwrite(os.path.join(out_dir, '00_baseline.png'), base)

    det = SpawnerDetector()
    if not det.ready:
        print('[!] skipping screen check: templates missing, run '
              '--build-icons <spawner screenshot>')
    else:
        scores = det.scores(base)
        print(f'\nspawner screen check: '
              f'{" ".join(f"{s:.3f}" for s in scores)}')
        if not all(s >= SPAWNER_MIN_SCORE for s in scores):
            print('[!] ABORT: not on the item-spawner screen.')
            return None

    print('\nbaseline layout:')
    menu = find_menu(base)
    if not menu:
        print('[!] ABORT: no category columns found — is the spawner open?')
        return None
    boxes = column_boxes(menu)
    base_mask = bright_mask(base)
    print(f'  column boxes: {boxes}')

    cv2.imwrite(os.path.join(out_dir, '00_layout.png'), annotate(base, menu))
    for items in menu.values():
        for it in items:
            cv2.imwrite(os.path.join(out_dir, f'{it.key}_label.png'),
                        it.label_crop(base))
    with open(os.path.join(out_dir, 'layout.json'), 'w') as f:
        json.dump({'boxes': {str(c): list(b) for c, b in boxes.items()},
                   'columns': {str(c): [it.as_dict() for it in items]
                               for c, items in menu.items()}}, f, indent=2)
    print(f'\nwrote layout.json + labels to {out_dir}')

    if layout_only:
        return out_dir

    targets = []
    for c, items in sorted(menu.items()):
        if columns and c not in columns:
            continue
        targets.extend(items[:limit] if limit else items)
    if start_from:
        keys = [it.key for it in targets]
        if start_from not in keys:
            print(f'[!] ABORT: --start-from {start_from} is not in this run '
                  f'({keys[0]}..{keys[-1]})')
            return None
        targets = targets[keys.index(start_from):]
    print(f'\nscraping {len(targets)} categories '
          f'({targets[0].key}..{targets[-1].key})\n')

    pointer = Pointer()
    records, done, failed = [], 0, []
    t0 = time.perf_counter()
    for it in targets:
        rec = expand_capture_collapse(it, pointer, base_mask, boxes, out_dir)
        records.append(rec)
        if rec['ok']:
            done += 1
            continue
        # Either the click did nothing, or the panel is stuck expanded. Both
        # make every remaining coordinate unreliable.
        failed.append(it.key)
        if len(failed) >= 2 or done == 0:
            print(f'[!] ABORT after {it.key}: layout is no longer the one the '
                  f'coordinates were taken from.\n'
                  f'    collapse the panel by hand and resume with '
                  f'--start-from {it.key}')
            break

    with open(os.path.join(out_dir, 'summary.json'), 'w') as f:
        json.dump({'captured': done, 'targets': len(targets),
                   'seconds': round(time.perf_counter() - t0, 1),
                   'items': records}, f, indent=2)
    print(f'\n{"=" * 54}\ncaptured {done}/{len(targets)} categories in '
          f'{time.perf_counter() - t0:.0f}s -> {out_dir}')
    if failed:
        print(f'problem rows: {", ".join(failed)}')
    return out_dir


# ════════════════════════════════════════════════════════════

def recrop(d):
    """Redo the submenu crops of a finished run from its full-screen shots.

    The crop padding is a guess about how far the submenu's border extends
    past its last line of text; tuning it should not cost another pass over
    the live game.
    """
    base = cv2.imread(os.path.join(d, '00_baseline.png'))
    if base is None:
        print(f'no 00_baseline.png in {d}')
        return 1
    base_mask = bright_mask(base)
    boxes = column_boxes(find_menu(base, verbose=False))

    summary_path = os.path.join(d, 'summary.json')
    summary = None
    if os.path.exists(summary_path):
        with open(summary_path) as f:
            summary = json.load(f)

    n = 0
    for f in sorted(glob.glob(os.path.join(d, '*_open.png'))):
        key = os.path.basename(f)[:-len('_open.png')]
        col = int(key[3])
        if col not in boxes:
            continue
        box = boxes[col]
        span = changed_rows(base_mask, bright_mask(cv2.imread(f)), box)
        if not span:
            print(f'  {key}: no change detected, skipped')
            continue
        y0 = max(0, span[0] - SUBMENU_PAD_TOP)
        y1 = min(SCREEN_H, span[1] + SUBMENU_PAD_BOTTOM)
        cv2.imwrite(os.path.join(d, f'{key}_submenu.png'),
                    cv2.imread(f)[y0:y1, box[0]:box[1]])
        if summary:
            for rec in summary['items']:
                if rec['key'] == key:
                    rec['submenu_bbox'] = [box[0], y0, box[1], y1]
        n += 1
    if summary:
        with open(summary_path, 'w') as f:
            json.dump(summary, f, indent=2)
    print(f'recropped {n} submenus in {d}')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--from', dest='src',
                    help='detect layout on a saved screenshot, no game needed')
    ap.add_argument('--layout-only', action='store_true',
                    help='grab the panel and dump coordinates, never click')
    ap.add_argument('--columns', help='comma-separated column indices, e.g. 1,2')
    ap.add_argument('--limit', type=int, help='rows per column')
    ap.add_argument('--countdown', type=int, default=5)
    ap.add_argument('--start-from', help='resume at this row key, e.g. col2_row03')
    ap.add_argument('--recrop', metavar='DIR',
                    help='redo submenu crops of a finished run, no game needed')
    ap.add_argument('--build-icons', metavar='PNG',
                    help='rebuild the screen-check templates from a known-good '
                         'spawner screenshot')
    args = ap.parse_args()

    if args.recrop:
        return recrop(args.recrop)

    if args.build_icons:
        img = cv2.imread(args.build_icons)
        if img is None:
            print(f'cannot read {args.build_icons}')
            return 1
        for p in build_templates(img):
            print('wrote', os.path.normpath(p))
        print(f'self-check scores: '
              f'{" ".join(f"{s:.3f}" for s in SpawnerDetector().scores(img))}')
        return 0

    if args.src:
        img = cv2.imread(args.src)
        if img is None:
            print(f'cannot read {args.src}')
            return 1
        print(f'{args.src}  {img.shape[1]}x{img.shape[0]}')
        menu = find_menu(img)
        total = sum(len(v) for v in menu.values())
        out = os.path.join(DATA_DIR, 'layout_probe.png')
        cv2.imwrite(out, annotate(img, menu))
        print(f'\n{len(menu)} columns, {total} rows -> {out}')
        print(f'column boxes: {column_boxes(menu)}')
        for c, items in sorted(menu.items()):
            for it in items:
                print(f'  {it.key}  click=({it.click_x},{it.y})  '
                      f'bbox=({it.x0},{it.y0})-({it.x1},{it.y1})')
        return 0

    cols = None
    if args.columns:
        cols = {int(c) for c in args.columns.split(',') if c.strip()}
    return 0 if scrape(cols, args.limit, args.layout_only,
                       args.countdown, args.start_from) else 1


if __name__ == '__main__':
    sys.exit(main())
