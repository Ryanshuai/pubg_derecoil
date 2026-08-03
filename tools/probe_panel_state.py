"""What does an expanded spawner panel actually look like, structurally?

Reads only the checked-in runs under docs/spawner/runs/ — no game, no
hardware. Those shots come with their own ground truth: colN_rowMM_open.png
IS the panel with category (N, MM) expanded, so the file name says what the
answer should be.

The question this answers, which nothing in the code says today: when a
category expands, does the submenu PUSH the categories below it down, or does
it draw over them? The answer decides how `panel_state` maps a submenu back to
the row that opened it — and therefore whether the panel can be read without
the collapsed baseline that forces every action back to the root.

    pixi run python tools/probe_panel_state.py
    pixi run python tools/probe_panel_state.py --run 20260801_205423 --verbose
"""
import argparse
import glob
import os
import re
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from detector.spawner_layout import (bright_mask, column_boxes, find_menu,
                                     find_submenu_items)

NAME_RE = re.compile(r'col(\d+)_row(\d+)_open\.png$')


def truth(path):
    """(col, row) the file name claims is expanded."""
    m = NAME_RE.search(path.replace('\\', '/'))
    return (int(m.group(1)), int(m.group(2))) if m else None


def describe(img, base_menu, base_boxes):
    """What one frame looks like through the existing primitives."""
    menu = find_menu(img, verbose=False)
    boxes = column_boxes(menu) if menu else {}
    # Read submenus through the BASELINE boxes as well: if expanding shifts
    # the column text, boxes derived from the expanded frame move with it and
    # the comparison would be circular.
    subs_own = {c: find_submenu_items(img, b) for c, b in boxes.items()}
    subs_base = {c: find_submenu_items(img, b) for c, b in base_boxes.items()}
    return menu, boxes, subs_own, subs_base


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', default=None, help='timestamp dir under docs/spawner/runs')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    runs = sorted(glob.glob(os.path.join(ROOT, 'docs', 'spawner', 'runs', '*')))
    if args.run:
        runs = [r for r in runs if args.run in r]
    if not runs:
        raise SystemExit('no runs found under docs/spawner/runs/')

    for run in runs:
        print(f'\n{"=" * 72}\n{os.path.basename(run)}\n{"=" * 72}')
        base_path = os.path.join(run, '00_baseline.png')
        base = cv2.imread(base_path)
        if base is None:
            print('  no 00_baseline.png, skipping')
            continue

        base_menu = find_menu(base, verbose=False)
        base_boxes = column_boxes(base_menu)
        base_mask = bright_mask(base)
        print(f'baseline: { {c: len(v) for c, v in base_menu.items()} } '
              f'boxes {base_boxes}')
        for c, items in sorted(base_menu.items()):
            ys = [it.y for it in items]
            print(f'  col{c}: rows y = {ys}')
        # Does the collapsed panel already look like it has submenu entries?
        for c, b in sorted(base_boxes.items()):
            n = len(find_submenu_items(base, b))
            if n:
                print(f'  [!] col{c} shows {n} centred rows while COLLAPSED — '
                      f'centring alone would false-positive here')

        opens = sorted(glob.glob(os.path.join(run, 'col*_row*_open.png')))
        print(f'\n{len(opens)} expanded frames:')
        rows = []
        for p in opens:
            gt = truth(p)
            img = cv2.imread(p)
            if img is None:
                continue
            menu, boxes, subs_own, subs_base = describe(img, base_menu, base_boxes)

            # which columns show centred rows, read through the baseline boxes
            lit = {c: len(v) for c, v in subs_base.items() if v}
            first_y = {c: v[0]['y0'] for c, v in subs_base.items() if v}

            # did the category rows move in the expanded column?
            shift = None
            if gt and gt[0] in menu and gt[0] in base_menu:
                a = [it.y for it in base_menu[gt[0]]]
                b = [it.y for it in menu[gt[0]]]
                shift = (len(a), len(b))

            rows.append((gt, lit, first_y, shift))
            if args.verbose:
                print(f'  {os.path.basename(p):28s} gt={gt}  lit={lit}  '
                      f'first_entry_y={first_y}  rows(base->now)={shift}')

        # ── the two questions ──
        print('\n  Q1: does the submenu appear in the column it belongs to?')
        ok = sum(1 for gt, lit, _, _ in rows if gt and list(lit) == [gt[0]])
        multi = [(gt, lit) for gt, lit, _, _ in rows if gt and len(lit) > 1]
        none = [gt for gt, lit, _, _ in rows if gt and not lit]
        print(f'      {ok}/{len(rows)} frames light exactly their own column')
        if multi:
            print(f'      {len(multi)} light MORE than one: {multi[:4]}')
        if none:
            print(f'      {len(none)} light NOTHING: {none[:6]}')

        print('\n  Q2: where does the submenu sit relative to its category row?')
        for gt, lit, first_y, _ in rows[:40]:
            if not gt or gt[0] not in first_y:
                continue
            col, row = gt
            cats = base_menu.get(col, [])
            if row > len(cats):
                continue
            cat_y = cats[row - 1].y
            above = [i + 1 for i, it in enumerate(cats) if it.y < first_y[col]]
            print(f'      col{col}_row{row:02d}: category y={cat_y:4d}  '
                  f'first entry y={first_y[col]:4d}  '
                  f'delta={first_y[col] - cat_y:+5d}  '
                  f'categories above it: {above[-1] if above else None}')

        print('\n  Q3: how many rows does find_menu see, collapsed vs expanded?')
        for gt, _, _, shift in rows[:12]:
            if shift:
                print(f'      {gt}: {shift[0]} -> {shift[1]}')


if __name__ == '__main__':
    main()
