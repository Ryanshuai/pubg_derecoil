"""Which weapons' own render bleeds into an EMPTY attachment tile.

    pixi run python tools/scan_slot_bleed.py
    pixi run python tools/scan_slot_bleed.py --slot magazine --show 12

`SlotDetector` calls a slot `filled` when Canny edges inside the tile reach
TAB_SLOT_FILLED_EDGES. The tile is supposed to be flat when nothing is fitted
-- but the weapon's own picture is drawn behind it, and a long magazine
reaches down into the box. config.py already records this happening on the
`scope` position (71 edges, "weapon render showing through") and the number
that sets the threshold was measured on an M416.

An AKM's magazine slot was watched costing a gun on 2026-08-04: the magazine
came off and landed on the floor, the tile still read `filled`, and the next
gesture at that slot reached the weapon row underneath and threw the whole
weapon on the ground (see unequip). So the question "which other weapons do
this" is worth a number per weapon rather than a guess.

WHAT COUNTS AS GROUND TRUTH HERE. Only `backdrop` captures: the collector
takes those with the host stripped bare and the round's part not yet fitted,
so the slot is empty BY CONSTRUCTION -- no detector was asked. Everything
else in those runs is either a fitted slot or an unlabelled row.

A weapon with no backdrop captures gets `no data`, not a pass. That
distinction is the whole point: this scan can only convict, never acquit a
weapon it has never seen.
"""
import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from config import TAB_SLOT_FILLED_EDGES
from calibration.capture_run import CaptureRun
from detector.attachment_catalog import SLOTS
from detector.attachment_detector import AttachmentDetector, MSE_EMPTY_TH

RUNS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'docs', 'attachments', 'runs')

# `{key}__{slot}__{weapon}__{tag}bg.png`. The weapon is in the NAME and not in
# the facts -- _shot() never passed it -- so it is parsed back out here. A name
# that does not match is skipped and counted, never guessed at.
NAME_RE = re.compile(r'^(?P<key>[^_]+(?:_[^_]+)*?)__(?P<slot>scope|muzzle|grip|'
                     r'magazine|stock)__(?P<weapon>[^_]+(?:_[^_]+)*?)__')


def edges(crop):
    """The number SlotDetector.fill_edges computes, on an already-cut tile."""
    g = (cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop)
    return int((cv2.Canny(g, 40, 120) > 0).sum())


def match(det, crop, slot, weapon):
    """What the template bank makes of this tile. -> (name, mse, margin)

    `AttachmentDetector.read_tile`, which is what the live reader calls — the
    whole value of this probe is that its numbers ARE the reader's numbers, and
    the only way to keep that true is to share the code rather than restate it.

    ⚠ This used to restate it, and the restatement had drifted: it called
    best_two without `prefer='solved'`, so it ranked tiles against the 库存 row
    picture for 38 of the bank's 41 assets while the reader ranked them against
    the slot picture. Its docstring claimed parity the whole time. Numbers
    published before 2026-08-06 — including the MSE percentiles quoted in
    detector/slot_detector.py — were measured that way.
    """
    return det.read_tile(crop, slot, weapon)


def gather(root, want_slot=None, targets=('backdrop',)):
    """-> ({(weapon, slot): [sample]}, stats), sample = dict of measurements."""
    det = AttachmentDetector()
    out = defaultdict(list)
    stats = {'runs': 0, 'seen': 0, 'unparsed': 0, 'unreadable': 0}
    for stamp in sorted(os.listdir(root)):
        d = os.path.join(root, stamp)
        if not os.path.isdir(d):
            continue
        try:
            run = CaptureRun.load_dir(d)
        except Exception:
            continue
        stats['runs'] += 1
        # `entries` and not `labelled()`: a backdrop carries no label by
        # design (nobody looked at it), so labelled() returns none of them.
        # The truth being used here is not a label, it is HOW the capture was
        # produced -- which is what `target` records.
        for e in run.entries:
            if e.get('target') not in targets:
                continue
            name = e.get('capture') or e.get('name') or ''
            m = NAME_RE.match(os.path.basename(name))
            if not m:
                stats['unparsed'] += 1
                continue
            slot = e.get('slot') or m.group('slot')
            if want_slot and slot != want_slot:
                continue
            path = os.path.join(d, os.path.basename(name))
            img = cv2.imread(path)
            if img is None:
                stats['unreadable'] += 1
                continue
            stats['seen'] += 1
            weapon = m.group('weapon')
            hit, mse, margin = match(det, img, slot, weapon)
            out[(weapon, slot)].append(
                {'edges': edges(img), 'path': path, 'weapon': weapon,
                 'slot': slot, 'key': e.get('key'), 'hit': hit, 'mse': mse,
                 'margin': margin, 'target': e.get('target')})
    return out, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--root', default=RUNS)
    ap.add_argument('--slot', help='only this slot')
    ap.add_argument('--th', type=int, default=TAB_SLOT_FILLED_EDGES)
    ap.add_argument('--show', type=int, default=0,
                    help='print the N worst crops with their paths')
    ap.add_argument('--mse', action='store_true',
                    help='the fitted-vs-empty template distributions instead')
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        print(f'no runs under {args.root}')
        return 1

    if args.mse:
        return report_mse(args)

    got, stats = gather(args.root, args.slot)
    if not got:
        print(f'no backdrop captures found in {stats["runs"]} run(s)')
        return 1

    print(f'{stats["seen"]} empty-slot captures over {stats["runs"]} runs   '
          f'threshold {args.th}'
          + (f'   ({stats["unparsed"]} names unparsed)' if stats['unparsed']
             else '')
          + (f'   ({stats["unreadable"]} missing files)' if stats['unreadable']
             else ''))
    print()

    rows = []
    for (weapon, slot), vals in got.items():
        e = sorted(v['edges'] for v in vals)
        over = sum(1 for x in e if x >= args.th)
        rows.append({'weapon': weapon, 'slot': slot, 'n': len(e),
                     'med': int(np.median(e)), 'max': e[-1], 'over': over})
    # Worst first: how often it reads filled, then how close the rest come.
    rows.sort(key=lambda r: (-r['over'] / r['n'], -r['max']))

    bad = [r for r in rows if r['over']]
    near = [r for r in rows if not r['over'] and r['max'] >= args.th // 2]

    print(f'{"weapon":<12} {"slot":<9} {"n":>3} {"median":>7} {"max":>5} '
          f'{"reads filled":>13}')
    print('-' * 54)
    for r in rows:
        flag = ('  <-- BLEEDS' if r['over'] else
                '  (close)' if r['max'] >= args.th // 2 else '')
        print(f'{r["weapon"]:<12} {r["slot"]:<9} {r["n"]:>3} {r["med"]:>7} '
              f'{r["max"]:>5} {r["over"]:>7}/{r["n"]:<5}{flag}')

    print()
    if bad:
        print(f'{len(bad)} weapon/slot pair(s) read FILLED on an empty tile. '
              f'A gesture aimed at one of these drops the whole weapon.')
    else:
        print('no empty tile in this corpus reaches the threshold.')
    if near:
        print(f'{len(near)} more come within half the threshold — same '
              f'failure, less margin.')

    # WHAT THIS CORPUS CANNOT SAY. Named explicitly rather than left to the
    # reader: a weapon absent from the table has not been cleared, it has not
    # been looked at, and the two are opposite answers to "is it safe".
    seen_w = {r['weapon'] for r in rows}
    blind = sorted(set(SLOTS) - seen_w)
    if blind:
        print()
        print(f'{len(blind)} weapon(s) have NO empty-slot capture here — not '
              f'cleared, unmeasured:')
        print('  ' + ', '.join(blind))

    if args.show:
        print()
        worst = sorted((v for vals in got.values() for v in vals),
                       key=lambda v: -v['edges'])[:args.show]
        for v in worst:
            print(f'{v["edges"]:>5}  {os.path.relpath(v["path"], args.root)}')
    return 0


def pct(vals, q):
    return float(np.percentile(vals, q)) if vals else float('nan')


def report_mse(args):
    """The two MSE distributions the positive-match gate has to separate.

    POSITIVE   `slots` captures — a part was fitted on purpose and the round's
               construction says which, so these are what a fitted tile scores.
    NEGATIVE   `backdrop` captures — the same tile before the part went on.

    The gate this feeds: a slot is FILLED only when a template actually
    matches, so the number that matters is how far the two distributions sit
    apart, not whether edges cross 120. Edges cannot separate them at all: the
    weapon's own magazine draws edges in the tile and IS a magazine, which is
    what cost a gun on 2026-08-04.

    ⚠ The negative class here covers three weapons (sks, uzi, vector) and none
    of them bleeds. A weapon whose render reaches into the tile is exactly the
    case this corpus lacks, so a threshold read off this table is a FLOOR on
    what is needed, never a proof that it is enough.
    """
    both, stats = gather(args.root, args.slot, ('backdrop', 'slots'))
    neg = [v for vals in both.values() for v in vals
           if v['target'] == 'backdrop']
    pos = [v for vals in both.values() for v in vals if v['target'] == 'slots']
    if not neg or not pos:
        print(f'need both classes; got {len(pos)} fitted, {len(neg)} empty')
        return 1

    # Only tiles the bank could speak about at all. An `inf` is `drawn()` or
    # the candidate list refusing, which is a different rejection and would
    # flatter the percentiles if mixed in.
    pm = sorted(v['mse'] for v in pos if v['mse'] < float('inf'))
    nm = sorted(v['mse'] for v in neg if v['mse'] < float('inf'))
    print(f'fitted tiles  n={len(pm):<5} mse  p50 {pct(pm,50):7.1f}  '
          f'p90 {pct(pm,90):7.1f}  p99 {pct(pm,99):7.1f}  max {pm[-1]:7.1f}')
    print(f'empty tiles   n={len(nm):<5} mse  p50 {pct(nm,50):7.1f}  '
          f'p10 {pct(nm,10):7.1f}  p01 {pct(nm,1):7.1f}  min {nm[0]:7.1f}')
    print(f'current gate  MSE_EMPTY_TH = {MSE_EMPTY_TH}')
    print()

    # The cost table, in the shape MARGIN_MIN's was settled with: what each
    # floor keeps and what it throws away.
    print(f'{"floor":>7} {"fitted kept":>13} {"empty rejected":>16}')
    for th in (150, 200, 250, 300, 350, 400, MSE_EMPTY_TH, 600):
        keep = sum(1 for x in pm if x <= th) / len(pm)
        rej = sum(1 for x in nm if x > th) / len(nm)
        mark = '   <- now' if th == MSE_EMPTY_TH else ''
        print(f'{th:>7} {keep:>12.1%} {rej:>15.1%}{mark}')

    worst = sorted(neg, key=lambda v: v['mse'])[:args.show or 5]
    print(f'\nempty tiles the bank came CLOSEST to naming:')
    for v in worst:
        print(f'  mse {v["mse"]:7.1f}  margin {v["margin"]:.2f}  '
              f'{v["weapon"]:<8} {v["slot"]:<9} -> {v["hit"] or "(none)"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
