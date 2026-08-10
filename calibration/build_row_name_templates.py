"""Cut the 库存 row LABELS into templates, and score them against each other.

    pixi run row-templates            # cut, score, write nothing
    pixi run row-templates --write    # install to data/templates/ocr_white/rows/
    pixi run row-templates --score    # score whatever is installed

NO GAME. It reads the frames the row-batch collector already captured and
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
import glob
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
# How far down a live frame `--from-icons` looks. 13 is what the list draws
# occupied (measured, see collect_inventory_vlm); past that icon_box runs off a
# 1440 screen.
MAX_SCAN_ROWS = 13


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


def cut_scrolled(pattern, existing):
    """Labels cut from frames where a SCROLLBAR narrows the text column.

    -> {'<key>.scroll': (mask, path, row)}

    ⚠ A LONG NAME WRAPS AT TWO WIDTHS AND THE BANK ONLY HAD ONE. When the list
    holds more rows than the window draws, the game puts a scrollbar down the
    right edge and the label column loses ~50 px, so a two-line name re-wraps:

        no scrollbar    Compensator (AR, DMR,  /  O12, S12K)      ink 47x198
        scrollbar       Compensator (AR,       /  DMR, O12, S12K) ink 47x150

    Every template in this bank was cut from UNSCROLLED frames, so the reader
    was systematically weakest exactly when the list is full -- which is the
    state the night harness spends most of its time in, and the one whose
    unreadable rows saturated the pack and halted four cells on 2026-08-10.
    Measured over 148 icon-named rows of 13 live frames, three parts rendered
    at a width no template held: brake_ar, comp_ar, tactical_stock. It read
    comp_ar as `comp_smg` 22 times out of 22 -- the shared prefix
    `Compensator (` is all that survives the narrower window.

    ⚠ THE LABELLER IS THE ICON READER, AND THAT IS THE WHOLE POINT. This file
    normally takes its identities from a spawner record plus a vision model.
    Here the row is named by matching its ICON -- a different rectangle, a
    different bank, a different failure mode -- and the pixels cut are the
    NAME beside it. Two independent statements about one row, which is the
    same licence `cut_all` runs on, with a different second source.

    ⚠ THE BAR IS THE ICON READER'S OWN GATES, NOT BYTE-EXACTNESS, and the
    first version of this got that wrong. Requiring `mse == 0` dropped
    `brake_ar` entirely: all eight of its scrolled rows score 113..323 because
    of the 2 px row-pitch drift, so the strictest filter silently threw away a
    third of the parts this function exists for. The gates are the right bar
    because they are the ones with a measurement behind them -- 882 emitted
    reads over the 1050-crop corpus, of which ZERO carried a wrong name, and
    that corpus is drifted throughout. Byte-exactness is a stronger signal
    about the CROP, not about the name.
    """
    from detector.tab_items import TabItemDetector
    det = TabItemDetector()
    out, seen = {}, {}
    for path in sorted(glob.glob(pattern)):
        frame = cv2.imread(path)
        if frame is None or frame.shape[0] < 1400:
            continue
        for row in range(MAX_SCAN_ROWS):
            item, occupied = det._read_row(frame, 'inventory', row)
            # source='icon' is the one hard requirement: cutting a NAME
            # template from a row the NAME reader identified is circular, and
            # would let one bad template breed more of itself.
            if not occupied or item is None or item.source != 'icon':
                continue
            if not item.key:
                continue
            x0, y0, x1, y1 = label_box(row, 'inventory')
            m = tight(text_mask(frame[y0:y1, x0:x1]))
            if m is None:
                continue
            # Only a shape NO installed template already holds is new. An
            # identical re-cut is the same rendering twice and only costs
            # match time -- `cut_all` says the same about the floor list.
            if m.shape in existing.get(item.key, ()):
                continue
            prev = seen.get(item.key)
            if prev is not None and prev != m.shape:
                print(f'[!] {item.key}: two unseen widths {prev} and '
                      f'{m.shape} -- only the first is cut')
                continue
            seen[item.key] = m.shape
            out.setdefault(f'{item.key}.scroll', (m, path, row))
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
    ap.add_argument('--from-icons', metavar='GLOB', default=None,
                    help='cut the SCROLLED rendering of each label out of live '
                         'frames, naming each row by its icon. See '
                         'cut_scrolled: a scrollbar narrows the column and a '
                         'long name re-wraps, and the whole bank was cut '
                         'without one.')
    args = ap.parse_args()

    if args.from_icons:
        det = RowNameDetector()
        have = {k: {t.shape for t in v} for k, v in det._templates.items()}
        cuts = cut_scrolled(args.from_icons, have)
        if not cuts:
            print('no label rendering found that the bank does not already '
                  'hold -- nothing to cut')
            return 0
        for stem, (m, path, row) in sorted(cuts.items()):
            key = stem.split('.')[0]
            print(f'  {stem:24} {m.shape[1]}x{m.shape[0]}   '
                  f'(bank holds {sorted(have.get(key, ()))})   '
                  f'row {row} of {os.path.basename(os.path.dirname(path))}')
        if args.write:
            write(cuts)
        else:
            print('(--write to install)')
        return 0

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
