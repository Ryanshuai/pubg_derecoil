"""Collect slot tiles while the operator plays, for the rows the bank is short of.

    pixi run tiles-todo            # what is still short, and by how much
    pixi run tiles-todo --solve    # intersect what has been collected

It rides on the live loop's Tab reading, which is the only place a frame and a
name meet without anyone driving the game on purpose. It writes nothing else
and reads no hardware.

⚠ THE LABELS HERE ARE `DETECTED`, NOT `REQUESTED`, and that is the whole
caveat. Everything under `attachments/runs/` was fitted deliberately and
confirmed; these are the detector's own readings, so a drifted template
poisons its own successor. Three things keep that in bounds:

    a quota          10 per (part, rack) and then it stops, so a wrong reading
                     cannot flood a key
    a movement test  a crop is only kept if the tile CHANGED since the last one
                     stored for that key. Ten copies of one backdrop intersect
                     to themselves and teach nothing -- measured tonight, two
                     frame pairs in a hand-shot session moved 0.1 and 0.5
    only the gaps    a (part, rack) that already has a template is skipped, so
                     this can only fill holes, never overwrite evidence that
                     came from a stronger source

⚠ AND THE SIDECAR SAYS SO. Every crop is written with `source: detected`
beside it. `CaptureRun.labelled()` exists because this repo has already paid
for a corpus that could not say where its labels came from; a directory that
mixes the two kinds under one name is the same mistake with a new path.
"""
import argparse
import collections
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from config import HUD_REGIONS
from detector.attachment_catalog import ATTACHMENTS
from detector.geometry import cut
from detector.tab_layout import SLOT_NAMES

# ⚠ THE LOOP SPEAKS ASSET NAMES, THIS FILE SPEAKS CATALOGUE KEYS. `read_slots`
# returns `Muzzle_Suppressor_Large_C`; ATTACHMENTS is keyed `supp_sr`. The
# first version tested `key not in ATTACHMENTS` and therefore skipped EVERY
# reading -- silently, returning 0 with no error, which is indistinguishable
# from "nothing worth keeping was on screen". Two different vocabularies for
# the same thing is this repo's second law in miniature.
KEY_OF_ASSET = {v['asset']: k for k, v in ATTACHMENTS.items() if v.get('asset')}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'calibration', 'artifacts', 'attachments', 'harvest')
TMPL_DIR = os.path.join(ROOT, 'data', 'templates', 'pubg_assets',
                        'Item', 'Attachment')

QUOTA = 10          # for a (part, rack) with no template at all
# ⚠ AND A SMALLER QUOTA FOR THE ONES THAT ALREADY HAVE ONE, because a template
# is never finished. Intersection is MONOTONE -- folding fresh crops in can
# only remove pixels, never add one -- so every extra backdrop can only strip
# something that was never the icon. A template built from ten views is a
# claim about ten backdrops; five more either confirm it (nothing changes, and
# that is a real result) or shave off pixels nobody should have believed.
EXTRA = 5
# Mean absolute difference from the last crop kept for this key. Below it the
# backdrop has not moved and the crop is a duplicate for intersection
# purposes. Same constant the collectors use for "did this tile change".
MOVED_MIN = 6.0
# ⚠ STRICTER THAN THE DETECTOR'S OWN GATE, and the reason is self-selection.
# A rack-2 crop is named by the rack-1 template (the bank has no r2 for these
# and the matcher does not know about rows) -- measured aligned MSE between
# the two rows is median 310 against a gate of 1000, so it usually holds. It
# does NOT hold for the thin icons: uzi_stock is 1298. Those are exactly the
# parts worth collecting, and exactly the ones a loose gate would file under a
# NEIGHBOUR'S name. A corpus that quietly renames its hardest members is worse
# than a corpus that skips them, so the bar here is well inside the matcher's.
KEEP_MSE_MAX = 400.0
KEEP_MARGIN_MIN = 1.6


def banked():
    """(part, rack) pairs the live bank already has a template for. -> set"""
    have = set()
    for p in glob.glob(os.path.join(TMPL_DIR, '*.xsect_r*.png')):
        stem = os.path.basename(p)
        asset = stem.split('.')[0].replace('Item_Attach_Weapon_', '')
        have.add((asset, stem.split('_r')[-1][0]))
    return {(k, r) for k in ATTACHMENTS for r in ('1', '2')
            if (ATTACHMENTS[k].get('asset'), r) in have}


def quotas():
    """How many crops each (part, rack) still wants. -> {(key, rack): n}

    Two tiers, and the reason they differ is what the crops are FOR. A pair
    with no template needs enough views to solve one from nothing; a pair that
    has one needs only enough to test it -- and a test that changes nothing is
    a pass, not a wasted trip.
    """
    got = banked()
    return {(k, r): (EXTRA if (k, r) in got else QUOTA)
            for k in ATTACHMENTS for r in ('1', '2')}


def held():
    """How many crops are already collected. -> {(key, rack): n}"""
    n = collections.Counter()
    for p in glob.glob(os.path.join(OUT, '*', '*.png')):
        key = os.path.basename(os.path.dirname(p))
        rack = os.path.basename(p).split('_r')[-1][0]
        n[(key, rack)] += 1
    return n


class TileHarvester:
    """Saves slot tiles for the (part, rack) pairs still missing a template.

    Constructed once by the dispatcher and handed each Tab reading. Cheap: one
    dict lookup per slot, and a file write only when a crop is both wanted and
    different from the last one kept.
    """

    def __init__(self, detector=None):
        # The live AttachmentDetector, so the crop can be RE-SCORED here. The
        # dispatcher passes only names on, and a name with no score cannot say
        # whether it was a comfortable read or a coin flip -- which is the one
        # thing this corpus needs to record about itself.
        self.det = detector
        self.want = quotas()
        self.count = held()
        self._last = {}                 # (key, rack) -> last crop kept

    def on_read(self, frame, got):
        """TabWatch's sink: one whole reading. -> how many crops were kept

        ⚠ THIS HANGS OFF `TabWatch`, NOT off the dispatcher's DETECT_TABLE.
        The first version hooked `result_field == 'attachments'` in
        control/match.py, which LOOKS like the place and is dead: `config.py`
        has no such result any more, TabWatch took the Tab reading over on
        2026-08-09, and nothing has walked that branch since. It collected
        zero crops across a full play session and reported no error, because
        nothing called it. A hook on a path nothing takes is indistinguishable
        from a hook that works and finds nothing.
        """
        kept = 0
        for gun_id, detected in (got.get('attachments') or {}).items():
            kept += self.offer(gun_id, detected,
                               {f'att_{gun_id}_{s}': cut(frame, HUD_REGIONS[f'att_{gun_id}_{s}'])
                                for s in SLOT_NAMES})
        return kept

    def offer(self, gun, detected, crops):
        """One rack's reading. -> how many crops were kept"""
        kept = 0
        rack = str(gun)
        for slot, name in (detected or {}).items():
            # `?` is the detector's AMBIGUOUS marker; '' is an empty slot.
            key = KEY_OF_ASSET.get(name) if name else None
            if key is None:
                continue
            want = self.want.get((key, rack))
            if not want or self.count[(key, rack)] >= want:
                continue
            crop = (crops or {}).get(f'att_{gun}_{slot}')
            if crop is None:
                continue
            mse = margin = None
            if self.det is not None:
                try:
                    got, mse, margin = self.det.read_tile(crop, slot, None)
                except Exception:
                    got, mse, margin = key, None, None
                if KEY_OF_ASSET.get(got) != key                         or (mse is not None and mse > KEEP_MSE_MAX)                         or (margin is not None and margin < KEEP_MARGIN_MIN):
                    continue
            prev = self._last.get((key, rack))
            if prev is not None and prev.shape == crop.shape:
                if float(np.abs(prev.astype(np.float32)
                                - crop.astype(np.float32)).mean()) < MOVED_MIN:
                    continue
            d = os.path.join(OUT, key)
            os.makedirs(d, exist_ok=True)
            stamp = time.strftime('%Y%m%d_%H%M%S')
            base = f'{stamp}_{os.getpid()}_{self.count[(key, rack)]:02d}_r{rack}'
            cv2.imwrite(os.path.join(d, base + '.png'), crop)
            with open(os.path.join(d, base + '.json'), 'w', encoding='utf-8') as f:
                json.dump({'key': key, 'rack': int(rack), 'slot': slot,
                           'source': 'detected', 'mse': mse, 'margin': margin,
                           'note': 'read by AttachmentDetector during play; '
                                   'NOT a requested-and-confirmed fit'}, f,
                          ensure_ascii=False, indent=1)
            self._last[(key, rack)] = crop.copy()
            self.count[(key, rack)] += 1
            kept += 1
        return kept


def refine(key, rack, paths, floor_frac=0.5):
    """Fold fresh crops INTO the stored template. -> (before, after, rejected)

    ⚠ WITH OUTLIER REJECTION, and without it this operation destroys banks.
    Exact intersection is one-vote-veto: folding a single disagreeing crop in
    zeroes the whole template, and that is exactly what happened when the
    hand-shot solves were merged into the mined bank -- a screen of black
    squares. `mine_slot_tiles` folds 62 crops from SIX different host guns and
    survives at 1045 px, and the only difference is that it skips the members
    that would collapse the pile and says which they were.

    So a crop that would take the template below `floor_frac` of where it
    started is skipped and counted. Everything surviving is still byte-identical
    across every crop that was kept -- no tolerance is introduced, only an
    admission that the pile was not homogeneous.
    """
    from calibration.collect_intersect import alive, intersect
    asset = ATTACHMENTS[key].get('asset')
    dst = os.path.join(TMPL_DIR, f'Item_Attach_Weapon_{asset}.xsect_r{rack}.png')
    old = cv2.imread(dst, cv2.IMREAD_UNCHANGED)
    if old is None or old.ndim != 3 or old.shape[2] != 4:
        return None
    acc = old[:, :, :3].copy()
    acc[old[:, :, 3] <= 128] = 0
    before = alive(acc)
    floor = before * floor_frac
    bad = 0
    for pth in sorted(paths):
        img = cv2.imread(pth)
        nxt = intersect(acc, img) if img is not None else None
        if nxt is None or alive(nxt) < floor:
            bad += 1
            continue
        acc = nxt
    return before, alive(acc), bad, acc, dst


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--solve', action='store_true',
                    help='intersect the gap collections and report')
    ap.add_argument('--refine', action='store_true',
                    help='fold the extra crops into templates that already exist')
    ap.add_argument('--install', action='store_true',
                    help='write the results into the live bank')
    args = ap.parse_args()

    want, have, got = quotas(), held(), banked()
    gaps = sorted(k for k in want if k not in got)
    tops = sorted(k for k in want if k in got)
    print(f'gaps  {sum(1 for k in gaps if have[k] >= want[k])}/{len(gaps)} '
          f'at quota {QUOTA}')
    print(f'extra {sum(1 for k in tops if have[k] >= want[k])}/{len(tops)} '
          f'at quota {EXTRA}   (crops to re-test a template that exists)')
    short = [(k, have[k], want[k]) for k in gaps if have[k] < want[k]]
    for (kk, r), h, w in short[:12]:
        print(f'    {kk:16} r{r}  {h}/{w}')
    if len(short) > 12:
        print(f'    ... and {len(short) - 12} more')

    if args.solve or args.install:
        from calibration.collect_intersect import alive, intersect
        from calibration.solve_tiles import is_icon
        print('\nsolving the gaps:')
        for key, rack in gaps:
            paths = sorted(glob.glob(os.path.join(OUT, key, f'*_r{rack}.png')))
            if len(paths) < 3:
                continue
            acc = None
            for pth in paths:
                acc = intersect(acc, cv2.imread(pth))
            ok, frac = is_icon(acc)
            print(f'  {key:16} r{rack}  {len(paths):2d} crop(s) -> '
                  f'{alive(acc):5d} px  blob {frac:.2f}'
                  + ('' if ok else '   ⚠ scattered'))
            if args.install and ok:
                asset = ATTACHMENTS[key].get('asset')
                dst = os.path.join(
                    TMPL_DIR, f'Item_Attach_Weapon_{asset}.xsect_r{rack}.png')
                if not os.path.exists(dst):
                    m = (np.any(acc != 0, axis=2) * 255).astype(np.uint8)
                    cv2.imwrite(dst, np.dstack([acc, m]))
                    print(f'      -> {os.path.basename(dst)}')

    if args.refine:
        print('\nrefining the templates that already exist:')
        for key, rack in tops:
            paths = sorted(glob.glob(os.path.join(OUT, key, f'*_r{rack}.png')))
            if not paths:
                continue
            out = refine(key, rack, paths)
            if out is None:
                continue
            before, after, bad, acc, dst = out
            note = ('unchanged — the new backdrops agreed with it'
                    if after == before else f'{before - after} px removed')
            print(f'  {key:16} r{rack}  {len(paths)} crop(s), {bad} rejected   '
                  f'{before} -> {after}   {note}')
            if args.install and after != before:
                m = (np.any(acc != 0, axis=2) * 255).astype(np.uint8)
                cv2.imwrite(dst, np.dstack([acc, m]))

    if not (args.solve or args.refine or args.install):
        print('\n(--solve fills gaps, --refine tightens what exists, '
              '--install writes either)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
