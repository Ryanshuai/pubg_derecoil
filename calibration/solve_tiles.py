"""Solve templates from hand-shot Tab frames, GROUPED BY WHAT IS ON SCREEN.

    pixi run solve-tiles                    # survey every tiles/ batch
    pixi run solve-tiles --install          # ... and update the live bank

⚠ THE GROUPING IS THE WHOLE FILE. A shooting session is one directory but
many configurations: the operator fits a set, turns the view, shoots a few,
swaps the set, shoots a few more. Intersecting a directory therefore
intersects UNRELATED loadouts and collapses to zero -- which is exactly what
happened when this was first tried by globbing: three frames gave 833 px, and
re-running after six more had been shot gave 0.

So frames are bucketed by a SIGNATURE read off the frame itself: which weapon
is in each rack, which slots are occupied, and what 库存 is holding. Frames
that agree on all of that are showing the same thing and may be intersected;
frames that do not, may not. Nothing is inferred from filenames or from the
order the shutter was pressed.

⚠ AND THE ROWS STAY APART. A part in rack 1 and the same part in rack 2 are
not the same pixels -- measured, ~1000 px surviving within a row against
18-114 across them -- so every group is solved once per rack and stored as
`.xsect_r1` / `.xsect_r2`.

⚠ WHAT THIS CAN AND CANNOT LABEL. The 库存 names are read with the row-name
bank, which is text and needs no icon template. A part whose spare is in the
backpack is therefore named by the screen. A part with no spare is NOT named:
the group is solved and reported as unlabelled rather than guessed at, because
the near neighbours here (three grey suppressors, six grips) are precisely
where a look-and-name is confidently wrong.
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
from detector.attachment_catalog import ATTACHMENTS
from detector.geometry import cut, detail
from detector.row_name_detector import RowNameDetector
from detector.tab_layout import SLOT_NAMES
from detector.weapon_template_detector import TabWeaponDetector
from calibration.collect_intersect import (MIN_INSTALL_PX, TAG, alive, change,
                                           intersect)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TILES = os.path.join(ROOT, 'calibration', 'artifacts', 'attachments', 'tiles')
TMPL_DIR = os.path.join(ROOT, 'data', 'templates', 'pubg_assets',
                        'Item', 'Attachment')

# A tile with something in it against a tile with nothing. Measured on the
# inner 63x63 (the crop HUD_REGIONS gives, which excludes the bevel): occupied
# 350-2650, empty 0-7. Two orders of magnitude, so the gate is not delicate.
#
# ⚠ `scope` IS EXCLUDED FROM THIS TEST, because that position draws no tile:
# the region shows the weapon itself and reads 1650-2100 whether an optic is
# fitted or not. Occupancy there is not readable this way and is not asked.
OCCUPIED_MIN = 100.0
# Frames whose backdrop moved less than this add nothing: the intersection
# only removes a pixel when something behind it changed. Two pairs in one
# session moved 0.1 and 0.5 -- the same picture shot twice.
MOVED_MIN = 5.0
# ⚠ SHAPE, NOT SIZE, decides whether a solve is an icon. A fixed pixel floor
# was set from "~950 px when it works, ~35 when it fails" and it is wrong for
# a part that is simply SMALL: the Folding Stock solves to ~150 px because the
# icon occupies that much tile, not because anything failed. What actually
# separates the two cases is connectivity -- a failed intersection leaves dots
# scattered across the whole tile, a real icon leaves one blob.
BLOB_MIN = 0.45         # largest connected component / surviving pixels
ABS_FLOOR = 40          # below this there is nothing to judge the shape of


def is_icon(acc):
    """Does this solve look like a picture rather than confetti? -> (ok, frac)"""
    if acc is None:
        return False, 0.0
    m = (np.any(acc != 0, axis=2)).astype(np.uint8)
    n = int(m.sum())
    if n < ABS_FLOOR:
        return False, 0.0
    cnt, _lab, stats, _c = cv2.connectedComponentsWithStats(m, connectivity=8)
    big = max(stats[1:, cv2.CC_STAT_AREA]) if cnt > 1 else 0
    return (big / n) >= BLOB_MIN, big / n


def signature(frame, guns, names):
    """What this frame is showing. -> hashable

    Read off the screen, never from the filename: the shutter records nothing
    about what was fitted, and the operator changes the loadout mid-session.
    """
    plates = guns.classify({f'gun_name_{g}': cut(frame, HUD_REGIONS[f'gun_name_{g}'])
                            for g in (1, 2)})
    occ = tuple((g, s) for g in (1, 2) for s in SLOT_NAMES
                if s != 'scope'
                and detail(cut(frame, HUD_REGIONS[f'att_{g}_{s}'])) > OCCUPIED_MIN)
    inv = tuple(sorted(names.classify(frame, 'inventory').values()))
    return (tuple(plates), occ, inv)


def batches():
    """-> [(signature, [(path, frame), ...]), ...] over every tiles batch."""
    guns, names = TabWeaponDetector(), RowNameDetector()
    per = collections.defaultdict(list)
    for d in sorted(glob.glob(os.path.join(TILES, '2*', ''))):
        for p in sorted(glob.glob(os.path.join(d, '*.png'))):
            f = cv2.imread(p)
            if f is None or f.shape[0] < 1000:      # a crop, not a full screen
                continue
            # ⚠ THE SHUTTER DOES NOT CHECK FOCUS, so a session picks up
            # whatever was on screen -- four frames of this corpus are the
            # DESKTOP, shot while the operator had Explorer open. They are not
            # harmless: `detail > 100` finds "occupied slots" in browser
            # chrome and the grouper solemnly solves them.
            #
            # A rack plate is the cheapest proof that this is the Tab screen
            # AND that a gun is on it, which is the only case worth grouping.
            plates = guns.classify({f'gun_name_{g}': cut(f, HUD_REGIONS[f'gun_name_{g}'])
                                    for g in (1, 2)})
            if not any(plates):
                continue
            per[signature(f, guns, names)].append((p, f))
    return sorted(per.items(), key=lambda kv: -len(kv[1]))


# A rectangle of the Tab panel that never holds a tile or a row: the strip
# between the 附近 list and the character. Whatever is in it is the world
# showing through, which is exactly what has to move.
BACKDROP = (300, 300, 200, 200)         # y, x, h, w


def moved(frames):
    """Backdrop movement between consecutive frames. -> [float]

    ⚠ THE WITNESS MUST EXIST IN EVERY FRAME. The first version used the first
    EMPTY slot tile, which is a fine witness right up until a group has no
    empty slot -- and then it returned [] and the report printed "0 frames
    with a moved backdrop". That reads as "the view never moved" while
    actually meaning "I could not tell", and those are the two answers this
    repo keeps paying for confusing: the 21-frame m416+m416 group was written
    off on it.
    """
    return [change(cut(a[1], BACKDROP), cut(b[1], BACKDROP))
            for a, b in zip(frames, frames[1:])]


def install(key, rack, acc):
    """Fold a new solve INTO the stored template. -> (before, after) px

    ⚠ INTERSECT, DO NOT OVERWRITE, and the difference was measured the hard
    way: writing these solves over the bank mined from 2000+ stored crops took
    `pixi run attachments` from 1943 to 1869. A hand-shot batch is a handful of
    frames and some of them share a backdrop; the mined one folded dozens. The
    newer file is not the better file.

    Intersection has no such ordering problem. It is MONOTONE -- folding more
    evidence in can only remove pixels, never add one -- so a batch can only
    make a template stricter, and a batch that saw nothing new changes nothing
    at all. That is also why this is safe to run repeatedly on the same
    directory.
    """
    asset = ATTACHMENTS[key].get('asset')
    if not asset:
        return None
    dst = os.path.join(TMPL_DIR, f'Item_Attach_Weapon_{asset}.{TAG}_r{rack}.png')
    if os.path.exists(dst):
        # ⚠ FILL EMPTY SLOTS, DO NOT REPLACE. Two policies were tried on the
        # live bank and both are on record: overwriting took `pixi run
        # attachments` from 1943 to 1869, because a hand-shot batch is a
        # handful of frames while the stored one folded dozens; and
        # INTERSECTING with the stored one blanked most of the bank, because
        # the stored ones were mined from OTHER HOST GUNS and the same part
        # differs by 0.9-6.3 grey levels across hosts -- which exact equality
        # does not forgive. What is left is the policy that can only add.
        return None
    m = (np.any(acc != 0, axis=2) * 255).astype(np.uint8)
    cv2.imwrite(dst, np.dstack([acc, m]))
    return alive(acc)


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--install', action='store_true',
                    help='write the LIVE bank for groups the 库存 names label')
    ap.add_argument('--min-frames', type=int, default=3)
    args = ap.parse_args()

    got = batches()
    print(f'{len(got)} configuration(s) across the tiles batches\n')
    installed = 0
    # (part, rack) -> {host gun: solve}. Filled per group, used afterwards for
    # the one thing a single group cannot do: remove the HOST WEAPON from an
    # optic. The scope position draws no tile, so the icon sits on the gun and
    # the gun does not move with the view -- only a second host does. Measured:
    # awm 1534 ∩ lynx 1601 -> 1045, with the grey barrel gone from the picture.
    per_host = collections.defaultdict(dict)
    sheet = []
    for sig, frames in got:
        (g1, g2), occ, inv = sig
        mv = moved(frames)
        useful = sum(1 for m in mv if m >= MOVED_MIN)
        print(f'rack1={g1 or "?"} rack2={g2 or "?"}  {len(frames)} frame(s), '
              f'{useful} with a moved backdrop')
        shown = ' '.join(inv) or '(empty — nothing here is named by the screen)'
        print(f'   库存 {shown}')
        if len(frames) < args.min_frames:
            print('   too few frames to intersect\n')
            continue
        # One spare of a part in 库存 names that part; two different parts of
        # the same slot in there name neither, so the slot is left unlabelled.
        by_slot = collections.defaultdict(list)
        for k in inv:
            if k in ATTACHMENTS:
                by_slot[ATTACHMENTS[k]['slot']].append(k)
        # ⚠ THE SCOPE SLOT IS SOLVED BUT NOT DETECTED. Occupancy there is
        # unreadable (no tile is drawn, so the region shows the weapon and
        # reads high either way), which is why it is absent from the
        # signature -- but absent from the SOLVE it was simply forgotten, and
        # optics are a third of the catalogue. It is attempted whenever 库存
        # names exactly one optic, which is also the only case that can label
        # it.
        todo = list(occ)
        if len(set(by_slot.get('scope', []))) == 1:
            todo += [(g, 'scope') for g in (1, 2)]
        for gun, slot in todo:
            acc = None
            for _p, f in frames:
                acc = intersect(acc, cut(f, HUD_REGIONS[f'att_{gun}_{slot}']))
            px = alive(acc)
            cand = sorted(set(by_slot.get(slot, [])))
            label = cand[0] if len(cand) == 1 else None
            why = ('no spare in 库存' if not cand
                   else 'two candidates: ' + ' '.join(cand))
            note = '' if label else f'   UNLABELLED ({why})'
            ok, frac = is_icon(acc)
            print(f'   r{gun} {slot:9} {px:5d} px  blob {frac:.2f}  '
                  f'{label or "?":14}{note}'
                  + ('' if ok else '   ⚠ scattered, not an icon'))
            if ok and not label:
                sheet.append((f'{(g1 if gun==1 else g2) or "?"} r{gun} {slot} '
                              f'{px}px', acc))
            if label and ok:
                host = (g1 if gun == 1 else g2) or '?'
                per_host[(label, gun)][host] = acc
            if args.install and label and ok:
                got = install(label, gun, acc)
                if got:
                    print(f'      NEW in the bank: {label} r{gun}, {got} px')
                    installed += 1
        print()
    print('optics solved across two different hosts in the same row:')
    any_two = False
    for (key, rack), hosts in sorted(per_host.items()):
        if ATTACHMENTS[key]['slot'] != 'scope' or len(hosts) < 2:
            continue
        any_two = True
        accs = list(hosts.values())
        x = accs[0]
        for a in accs[1:]:
            x = intersect(x, a)
        ok, frac = is_icon(x)
        print(f'   {key}@r{rack}  ' + '  '.join(f'{h} {alive(v)}'
                                                for h, v in sorted(hosts.items()))
              + f'   ->  {alive(x)} px  blob {frac:.2f}'
              + ('' if ok else '   ⚠ scattered'))
        if args.install and ok:
            got = install(key, rack, x)
            if got:
                print(f'      NEW in the bank: {key} r{rack}, {got} px')
                installed += 1
    if not any_two:
        print('   none — an optic needs the SAME row on TWO different guns')
    print()
    # ⚠ A CONTACT SHEET, BECAUSE THE LABEL IS THE BOTTLENECK AND IT IS NOT
    # MINE TO GUESS. 库存 names only the SPARE; the operator proved that is
    # not the fitted part (two guns wearing Tilted Grips over a backpack
    # holding two Vertical Foregrips), and same-family icons are exactly where
    # a look-and-name is confidently wrong. So every solve is written out
    # numbered, and the person who fitted them says which is which.
    if sheet:
        cols = 8
        rows_img = []
        for i in range(0, len(sheet), cols):
            row = [cv2.resize(a, (126, 126), interpolation=cv2.INTER_NEAREST)
                   for _n, a in sheet[i:i + cols]]
            while len(row) < cols:
                row.append(np.zeros((126, 126, 3), np.uint8))
            rows_img.append(np.hstack(row))
        dst = os.path.join(TILES, '_unlabelled.png')
        cv2.imwrite(dst, np.vstack(rows_img))
        # ⚠ ONE FILE EACH AS WELL AS THE SHEET. The sheet is for looking at;
        # the files are what a name gets attached to. Numbered in the sheet's
        # reading order so "12 is comp_ar" resolves to exactly one picture,
        # and carrying gun/rack/slot/px in the name so the file still says
        # what it is after it has been moved somewhere else.
        one = os.path.join(TILES, 'unlabelled')
        os.makedirs(one, exist_ok=True)
        for old in glob.glob(os.path.join(one, '*.png')):
            os.remove(old)                  # the numbering changes every run
        for i, (n, a) in enumerate(sheet):
            tag = n.replace(' ', '_')
            m = (np.any(a != 0, axis=2) * 255).astype(np.uint8)
            cv2.imwrite(os.path.join(one, f'{i:02d}_{tag}.png'),
                        np.dstack([a, m]))
        print(f'{len(sheet)} unlabelled solve(s) -> {os.path.relpath(dst, ROOT)}'
              f'   ({cols} per row, reading order)')
        for i, (n, _a) in enumerate(sheet):
            print(f'   {i:2d}  {n}')
    if args.install:
        print(f'{installed} template(s) written to the live bank. '
              f'`pixi run attachments` is what says whether they help.')
    else:
        print('(--install to write the labelled ones to the live bank)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
