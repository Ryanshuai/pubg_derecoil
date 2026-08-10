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

from detector.attachment_catalog import ATTACHMENTS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'calibration', 'artifacts', 'attachments', 'harvest')
TMPL_DIR = os.path.join(ROOT, 'data', 'templates', 'pubg_assets',
                        'Item', 'Attachment')

QUOTA = 10
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


def missing():
    """Which (part, rack) the live bank has no template for. -> set"""
    have = set()
    for p in glob.glob(os.path.join(TMPL_DIR, '*.xsect_r*.png')):
        stem = os.path.basename(p)
        asset = stem.split('.')[0].replace('Item_Attach_Weapon_', '')
        rack = stem.split('_r')[-1][0]
        have.add((asset, rack))
    return {(k, r) for k in ATTACHMENTS for r in ('1', '2')
            if (ATTACHMENTS[k].get('asset'), r) not in have}


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

    def __init__(self, detector=None, quota=QUOTA):
        # The live AttachmentDetector, so the crop can be RE-SCORED here. The
        # dispatcher passes only names on, and a name with no score cannot say
        # whether it was a comfortable read or a coin flip -- which is the one
        # thing this corpus needs to record about itself.
        self.det = detector
        self.quota = quota
        self.want = missing()
        self.count = held()
        self._last = {}                 # (key, rack) -> last crop kept

    def offer(self, gun, detected, crops):
        """One rack's reading. -> how many crops were kept

        `detected` is {slot: name} as the detector read it, `crops` the frame
        crops keyed `att_{gun}_{slot}` -- exactly what the dispatcher already
        holds when it calls `set_attachments`.
        """
        kept = 0
        rack = str(gun)
        for slot, key in (detected or {}).items():
            if not key or key not in ATTACHMENTS:
                continue
            if (key, rack) not in self.want:
                continue
            if self.count[(key, rack)] >= self.quota:
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
                if got != key or (mse is not None and mse > KEEP_MSE_MAX)                         or (margin is not None and margin < KEEP_MARGIN_MIN):
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


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--solve', action='store_true',
                    help='intersect what is collected and report')
    ap.add_argument('--install', action='store_true',
                    help='write the ones that converge into the live bank')
    args = ap.parse_args()

    want, have = missing(), held()
    short = sorted((k, r) for (k, r) in want if have[(k, r)] < QUOTA)
    full = sorted((k, r) for (k, r) in want if have[(k, r)] >= QUOTA)
    print(f'{len(want)} (part, rack) pair(s) have no template; quota {QUOTA}')
    print(f'  {len(full)} at quota, {len(short)} still short')
    for k, r in short:
        print(f'    {k:16} r{r}  {have[(k, r)]}/{QUOTA}')

    if not (args.solve or args.install):
        return 0

    from calibration.collect_intersect import alive, intersect
    from calibration.solve_tiles import is_icon
    print()
    for k, r in full + short:
        paths = sorted(glob.glob(os.path.join(OUT, k, f'*_r{r}.png')))
        if len(paths) < 3:
            continue
        acc = None
        for p in paths:
            acc = intersect(acc, cv2.imread(p))
        ok, frac = is_icon(acc)
        print(f'  {k:16} r{r}  {len(paths):2d} crop(s) -> {alive(acc):5d} px  '
              f'blob {frac:.2f}' + ('' if ok else '   ⚠ scattered'))
        if args.install and ok:
            asset = ATTACHMENTS[k].get('asset')
            dst = os.path.join(TMPL_DIR,
                               f'Item_Attach_Weapon_{asset}.xsect_r{r}.png')
            if not os.path.exists(dst):
                m = (np.any(acc != 0, axis=2) * 255).astype(np.uint8)
                cv2.imwrite(dst, np.dstack([acc, m]))
                print(f'      -> {os.path.basename(dst)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
