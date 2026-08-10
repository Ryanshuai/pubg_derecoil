"""Cut the 库存 row LABELS into templates, and score them against each other.

    pixi run row-templates            # cut, score, write nothing
    pixi run row-templates --write    # install to data/templates/ocr_white/rows/
    pixi run row-templates --score    # score whatever is installed

NO GAME. It reads the four frames `pixi run rows-batch` already captured and
the reading in `tools/record_row_names.py`, both of which are on disk.

WHERE THE LABELS COME FROM. Each frame holds one batch of parts whose keys
were known before the shutter -- the spawner was told what to produce -- and a
vision model read the printed names off the frame afterwards. The two had to
agree AS SETS before anything was kept. So a row's identity here rests on two
independent statements, and this file only turns the agreed ones into pixels.

⚠ THE SCORE THAT MATTERS IS THE MARGIN, not the hit rate. A bank of 41 names
where half are prefixes of the other half will report 41/41 while sitting one
antialiasing artefact away from a swap. `--score` therefore prints, for every
row, the gap between the right answer and the best WRONG one, and the summary
is the worst of those gaps. That is the number to watch when the game restyles
its font.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from detector.row_name_detector import (IOU_MIN, RowNameDetector, TMPL_DIR,
                                        label_box, text_mask, tight)
from tools.record_row_names import BATCHES, NEARBY, READING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, 'calibration', 'artifacts', 'rows_vlm')


def cut_all():
    """-> {filename_stem: (mask, stamp, row)} over both panels.

    ⚠ TWO VARIANTS PER PART, NOT ONE, and the second is not redundancy. The
    same label WRAPS DIFFERENTLY in the narrower 附近 column, so a bank cut
    only from 库存 scores the floor rendering at 0.54 -- measured, on
    `quickext_ar` and `tactical_stock`, both of which then fall below any
    usable gate. `<key>.nearby.png` is a separate PICTURE of the same name,
    which is what the repo's variant mechanism is for (all variants matched,
    best wins, no flag anywhere).
    """
    out = {}
    for panel, table, suffix in (('inventory', BATCHES, ''),
                                 ('nearby', NEARBY, '.nearby')):
        for stamp, keys in sorted(table.items()):
            path = os.path.join(SHOTS, f'{stamp}__rows.png')
            frame = cv2.imread(path)
            if frame is None:
                print(f'[!] missing frame {os.path.relpath(path, ROOT)} — '
                      f'{len(keys)} {panel} label(s) cannot be cut')
                continue
            for row, key in enumerate(keys):
                x0, y0, x1, y1 = label_box(row, panel)
                m = tight(text_mask(frame[y0:y1, x0:x1]))
                if m is None:
                    print(f'[!] {stamp} {panel} row {row} ({key}): no ink')
                    continue
                # First cut wins: a part shown on the floor of two different
                # frames is the same rendering twice, and a second copy only
                # costs match time.
                out.setdefault(f'{key}{suffix}', (m, stamp, row))
    return out


def write(cuts):
    os.makedirs(TMPL_DIR, exist_ok=True)
    for stem, (m, _stamp, _row) in sorted(cuts.items()):
        cv2.imwrite(os.path.join(TMPL_DIR, f'{stem}.png'), m)
    print(f'{len(cuts)} template(s) -> '
          f'{os.path.relpath(TMPL_DIR, ROOT)}')


def score(det, sources):
    """Every stored row against the whole bank. -> (rows, hits, worst)

    ⚠ SELF AND HELD-OUT ARE REPORTED SEPARATELY, because a bank cut from these
    frames scores 1.000 on the rows it was cut from and that number says
    nothing at all. `sources` names the exact (stamp, panel, row) each template
    came from; everything else is a rendering the bank has not seen -- the same
    part further down the floor list, over different scenery, wrapped by a
    different column. Those are the rows worth reading.
    """
    stats = {False: [0, 0], True: [0, 0]}          # held_out -> [rows, hits]
    worst, worst_at, misses, thin = 9.9, None, [], []
    for panel, table in (('inventory', BATCHES), ('nearby', NEARBY)):
        for stamp, keys in sorted(table.items()):
            frame = cv2.imread(os.path.join(SHOTS, f'{stamp}__rows.png'))
            if frame is None:
                continue
            for row, key in enumerate(keys):
                held = sources.get(key) != (stamp, panel, row) and \
                    sources.get(f'{key}.nearby') != (stamp, panel, row)
                x0, y0, x1, y1 = label_box(row, panel)
                ranked = det.rank(frame[y0:y1, x0:x1])
                stats[held][0] += 1
                if not ranked:
                    misses.append((key, panel, 'no ink'))
                    continue
                top_iou, top_key = ranked[0]
                # ⚠ THE MARGIN IS TO THE BEST WRONG ANSWER, not to second
                # place. When the right answer wins those are the same thing;
                # when it LOSES, second place IS the right answer and the gap
                # would come out positive -- a comfortable margin printed over
                # a misread.
                wrong = next(((i, k) for i, k in ranked if k != key),
                             (0.0, '-'))
                by_key = {}
                for i, k in ranked:
                    by_key.setdefault(k, i)
                margin = (top_iou - wrong[0] if top_key == key
                          else by_key.get(key, 0.0) - wrong[0])
                ok = top_key == key and top_iou >= IOU_MIN
                stats[held][1] += ok
                if not ok:
                    misses.append((key, panel, f'read {top_key} {top_iou:.3f}'))
                if held and margin < worst:
                    worst, worst_at = margin, (key, wrong[1])
                if held and margin < 0.05:
                    thin.append((key, wrong[1], margin))
                print(f'  {stamp[-6:]} {panel[:3]} {row:2d} '
                      f'{"held-out" if held else "self    "}  {key:15} '
                      f'{top_iou:.3f}   best wrong {wrong[1]:15} {wrong[0]:.3f}'
                      f'   margin {margin:+.3f}{"" if ok else "   <-- MISS"}')
    sr, sh = stats[False]
    hr, hh = stats[True]
    print(f'\nself     {sh}/{sr}   (cut from these very rows; 1.000 by '
          f'construction, so this number is a wiring check and nothing more)')
    print(f'held-out {hh}/{hr}   gate {IOU_MIN}')
    for k, p, why in misses:
        print(f'  [!] {k} ({p}): {why}')
    if thin:
        print(f'  {len(thin)} held-out row(s) within 0.05 of a wrong answer:')
        for k, w, m in sorted(thin, key=lambda t: t[2]):
            print(f'      {k:15} vs {w:15} {m:+.3f}')
    if worst_at:
        print(f'  worst held-out margin {worst:+.3f}  '
              f'({worst_at[0]} vs {worst_at[1]})')
    return sr + hr, sh + hh, worst


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--score', action='store_true',
                    help='score what is installed, do not cut')
    args = ap.parse_args()

    cuts = cut_all()
    sources = {stem: (st, 'nearby' if stem.endswith('.nearby') else 'inventory',
                      row) for stem, (_m, st, row) in cuts.items()}
    if not args.score:
        sizes = sorted((m.shape[1], m.shape[0], k)
                       for k, (m, _, _) in cuts.items())
        n_inv = sum(1 for k in cuts if not k.endswith('.nearby'))
        print(f'cut {n_inv}/{len(READING)} 库存 labels + '
              f'{len(cuts) - n_inv} 附近 variants   '
              f'widest {sizes[-1][2]} {sizes[-1][0]}x{sizes[-1][1]}   '
              f'narrowest {sizes[0][2]} {sizes[0][0]}x{sizes[0][1]}')
        if args.write:
            write(cuts)
        else:
            print('(--write to install)')
            return 0

    det = RowNameDetector()
    total = sum(len(v) for v in BATCHES.values()) + \
        sum(len(v) for v in NEARBY.values())
    print(f'\nscoring {len(det)} part(s) against {total} stored rows:')
    rows, hits, worst = score(det, sources)
    return 0 if hits == rows else 1


if __name__ == '__main__':
    raise SystemExit(main())
