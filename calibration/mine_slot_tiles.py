"""Build attachment templates by INTERSECTING the slot crops already on disk.

    pixi run mine-tiles                 # survey + solve, write nothing
    pixi run mine-tiles --write         # save into the run dir
    pixi run mine-tiles --install       # ... and into the live bank

NO GAME. Every crop it uses was captured months ago by `collect_templates`
and carries a label of the strongest kind this repo has -- `LABEL_REQUESTED`,
meaning the part was fitted ON PURPOSE and confirmed, rather than read back by
the detector under test. 2000+ of them are sitting in
`calibration/artifacts/attachments/runs/`.

THE OPERATION IS ONE LINE: keep the pixels that are byte-identical across
every crop of the same part. A scene pixel differs between backgrounds; an
opaque icon pixel does not. Nothing is modelled and there is no alpha to get
wrong.

⚠ ROW 1 AND ROW 2 ARE SEPARATE BANKS, and mixing them destroys both. The two
rack rows sit at different heights, so the world showing through their
translucent tiles differs and the SAME part on the SAME gun is not the same
pixels in the two rows -- measured: ~1000 px surviving within a row, 18-114
across them. The operator put it plainly: 第一行第二行一取交就没了.

⚠ AND THE RACK IS DERIVED FROM THE CROP'S RECTANGLE, not from a threshold on
y. The first version of this split at y<300, which separates the SCOPE tile
(y153/y455) from the other four (y316/y617) -- not row 1 from row 2. It
produced a clean-looking table saying every optic lived in row 1 and every
other part in row 2, which is an artefact of the cut and nothing else.

⚠ WHAT THIS CANNOT REMOVE is the host gun's own hardware, and only the scope
slot suffers from it: that position draws no tile, so the icon is composited
straight onto the weapon, and the weapon does not move when the view does.
Fixing that needs the same optic on TWO DIFFERENT GUNS in the SAME row --
`--two-guns` does exactly that where the corpus allows, and it is measured to
work (awm 1534 ∩ lynx 1601 -> 1045, with the grey barrel gone from the
picture).
"""
import argparse
import collections
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from config import HUD_REGIONS
from detector.attachment_catalog import ATTACHMENTS, canonical
from calibration.capture_run import CaptureRun
from calibration.collect_intersect import MIN_INSTALL_PX, TAG, alive, intersect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, 'calibration', 'artifacts', 'attachments', 'runs')
TILES = os.path.join(ROOT, 'calibration', 'artifacts', 'attachments', 'tiles')
OUT = os.path.join(TILES, 'mined')
TMPL_DIR = os.path.join(ROOT, 'data', 'templates', 'pubg_assets',
                        'Item', 'Attachment')

# y -> rack, taken from the geometry rather than guessed. Two rows, and the
# scope tile of each sits higher than that row's other four.
RACK_OF_Y = {}
for _g in (1, 2):
    for _s in ('scope', 'muzzle', 'grip', 'magazine', 'stock'):
        RACK_OF_Y[HUD_REGIONS[f'att_{_g}_{_s}'][0]] = _g


def harvest():
    """-> {(key, rack): [(gun, path), ...]} over every stored run."""
    per = collections.defaultdict(list)
    for d in sorted(glob.glob(os.path.join(RUNS, '*', ''))):
        try:
            run = CaptureRun.load_dir(d)
        except Exception as e:                      # a run this format cannot read
            print(f'  [!] {os.path.basename(d.rstrip(os.sep))}: {e}')
            continue
        for ent, lab, path in run.labelled():
            if ent.get('target') != 'slots':
                continue
            region = ent.get('region') or []
            rack = RACK_OF_Y.get(region[0] if region else None)
            if rack is None:
                # ⚠ NOT SILENTLY BUCKETED. A crop whose rectangle matches no
                # known slot is a crop this file cannot place in a row, and a
                # row is the one thing the banks must not get wrong.
                continue
            raw = lab.get('asset') or ent.get('key') or ''
            # ⚠ DO NOT canonical() A PICTURE. `canonical` answers "which key
            # names this spawner POSITION today", and 41.1 replaced the item
            # at one position in place: crops labelled `angled_grip` show the
            # Angled Foregrip, crops labelled `tilted_grip` show the Tilted
            # Grip, and they are different objects. Folding them into one pile
            # is the 41.1 hazard re-enacted -- the intersection caught it by
            # rejecting `angled_grip__grip__sks__p0fg.png` out of the
            # tilted_grip pile. Piles are keyed on the label AS WRITTEN.
            key = raw
            if key in ATTACHMENTS:
                per[(key, rack)].append((ent.get('weapon') or _gun_of(path),
                                         path))
    return per


def _gun_of(path):
    """Host weapon from the filename, for runs written before the field.

    Not a second source -- the name and the label were written by the same
    pen in the same breath -- but it is the only source those runs have.
    """
    parts = os.path.basename(path).split('__')
    return parts[2] if len(parts) > 2 else '?'


def fold(paths, floor=MIN_INSTALL_PX):
    """Intersect a pile of crops, skipping the ones that destroy it.

    -> (template, used, [rejected paths])

    ⚠ EXACT INTERSECTION IS ONE-VOTE-VETO, and that is the point everywhere
    else in this flow -- a pixel nobody observed must not survive. But it
    makes a whole GROUP hostage to one bad member: `brake_ar` has 81 labelled
    crops and folded to ZERO, while `comp_ar` has 100 and folded to 709. The
    difference is not the count, it is that one crop in the first pile does
    not show what its label says.

    So a crop that would take the accumulator below `floor` is dropped and
    RECORDED. That is not a tolerance -- every surviving pixel is still
    byte-identical across every crop that was kept -- it is a statement that
    the pile was not homogeneous, and the rejects are the evidence for which
    ones to look at. This corpus has form here: seven crops are on record as
    filed under seven wrong names.

    Greedy and order-dependent, so the order is sorted and therefore stable:
    a run that reshuffled would reject a different set each time and none of
    them could be investigated.
    """
    acc, n, bad = None, 0, []
    for p in sorted(paths):
        img = cv2.imread(p)
        if img is None:
            continue
        if acc is None:
            acc, n = img.copy(), 1
            continue
        nxt = intersect(acc, img)
        if nxt is None:                             # shape mismatch: refuse
            continue
        if alive(nxt) < floor <= alive(acc):
            bad.append(p)
            continue
        acc, n = nxt, n + 1
    return acc, n, bad


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true', help='save to the tiles dir')
    ap.add_argument('--install', action='store_true',
                    help='also write the LIVE bank (implies --write)')
    ap.add_argument('--min-crops', type=int, default=4,
                    help='a group thinner than this is reported, not solved')
    args = ap.parse_args()

    per = harvest()
    keys = sorted({k for k, _ in per})
    print(f'{len(per)} (part, rack) group(s) over {len(keys)} part(s), '
          f'{sum(len(v) for v in per.values())} labelled crop(s)\n')

    solved, thin, empty, rejects = {}, [], [], {}
    keptn, gunsn = {}, {}
    print(f'{"part":16} {"rack":>4} {"kept":>5} {"drop":>4} {"guns":>4} {"px":>6}')
    for key in keys:
        for rack in (1, 2):
            paths = [p for _g, p in per.get((key, rack), [])]
            guns = len({g for g, _ in per.get((key, rack), [])})
            if not paths:
                continue
            if len(paths) < args.min_crops:
                thin.append(f'{key}@r{rack} ({len(paths)})')
                continue
            acc, n, bad = fold(paths)
            px = alive(acc)
            keptn[(key, rack)], gunsn[(key, rack)] = n, guns
            if bad:
                rejects[(key, rack)] = bad
            print(f'{key:16} {rack:4d} {n:5d} {len(bad):4d} {guns:4d} {px:6d}'
                  + ('   ⚠ under the install floor' if px < MIN_INSTALL_PX
                     else ''))
            if px:
                solved[(key, rack)] = acc
            else:
                empty.append(f'{key}@r{rack}')

    print(f'\n{len(solved)} solved, '
          f'{sum(1 for v in solved.values() if alive(v) >= MIN_INSTALL_PX)} '
          f'above the {MIN_INSTALL_PX} px install floor')
    if thin:
        print(f'  too few crops (< {args.min_crops}): {", ".join(sorted(thin))}')
    if empty:
        print(f'  collapsed to nothing: {", ".join(sorted(empty))}')
    if rejects:
        tot = sum(len(v) for v in rejects.values())
        print(f'  {tot} crop(s) rejected as disagreeing with their own pile — '
              f'these are the ones to look at, not the ones to average away:')
        for (k, r), v in sorted(rejects.items()):
            print(f'      {k}@r{r}: {len(v)}  e.g. {os.path.basename(v[0])}')
    missing = [k for k in sorted(ATTACHMENTS)
               if not any((k, r) in solved for r in (1, 2))]
    if missing:
        print(f'  no template from either row: {", ".join(missing)}')

    if not (args.write or args.install):
        print('\n(--write to save, --install to also update the live bank)')
        return 0

    os.makedirs(OUT, exist_ok=True)
    n_live = 0
    for (key, rack), acc in sorted(solved.items()):
        a = (np.any(acc != 0, axis=2) * 255).astype(np.uint8)
        rgba = np.dstack([acc, a])
        cv2.imwrite(os.path.join(OUT, f'{key}_r{rack}.png'), rgba)
        # ⚠ THE LIVE BANK GETS ONLY WHAT CLEARS THE FLOOR. Three 30 px
        # templates from a failed experiment sat in it earlier today and took
        # `pixi run attachments` from 2060 to 2030 -- silently, because a
        # template that matches nothing well still matches something best.
        # ⚠ THREE GATES, and each one is a measured failure rather than
        # caution. Under the px floor: a scattered-dots template still matches
        # something best and cost 30 reads today. Too few crops KEPT: uzi_stock
        # rejected 48 of 50, so its 205 px rest on two frames that happened to
        # agree. A scope from ONE gun: that slot draws no tile, the icon sits
        # on the weapon, and a single host bakes its rail in (scope_2x@r2 is
        # 2142 px against ~1000 for the same optic solved across two guns).
        kept, guns = keptn.get((key, rack), 0), gunsn.get((key, rack), 0)
        if args.install and alive(acc) >= MIN_INSTALL_PX                 and kept >= args.min_crops                 and not (ATTACHMENTS[key]['slot'] == 'scope' and guns < 2):
            asset = ATTACHMENTS[key].get('asset')
            if asset:
                cv2.imwrite(os.path.join(
                    TMPL_DIR,
                    f'Item_Attach_Weapon_{asset}.{TAG}_r{rack}.png'), rgba)
                n_live += 1
    print(f'\n{len(solved)} -> {os.path.relpath(OUT, ROOT)}')
    if args.install:
        print(f'{n_live} -> the live bank. Run `pixi run attachments` now: '
              f'that score is the only thing that says these help.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
