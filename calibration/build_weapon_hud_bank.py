"""Build the weapon-HUD exemplar bank from labelled screen captures.

Writes data/templates/weapon_hud_bank.npz: a PCA basis plus the projection of
every reference frame, which is what detector/weapon_hud_detector.py loads.

WHY EXEMPLARS AND NOT ONE TEMPLATE PER WEAPON. The HUD draws each icon at
alpha 0.80 when the weapon is selected and 0.405 when it is not, over open
world rather than a controlled panel. Collapsing that spread into a single
median template and matching by MSE scores 0.871; keeping the frames and
taking the nearest scores 0.975. Measured, both ways, on the same split.

WHY NOT THE GAME'S OWN ART. It was tried first and scores 0.489. The extracted
icon is the INPUT to the game's compositing, and the detector only ever sees
the output. This is the same finding as the attachment bank, which is why no
game-file art remains anywhere in this tree.

    pixi run python calibration/build_weapon_hud_bank.py
    pixi run python calibration/build_weapon_hud_bank.py --eval    # hold-out score
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detector.attachment_catalog import ROSTER
from detector.weapon_hud_detector import DIMS, feature, BANK_PATH

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, 'data', 'templates', 'Manual', 'weapon_hud')

# The corpus was foldered under the retired CNN's class names. 98k IS kar98k --
# the same gun under two spellings, and the reason a roster-filtered run once
# scored WORSE than an unfiltered one: the entry was dropped while its frames
# stayed in the score.
ALIAS = {'98k': 'kar98k'}
PER_CLASS = 48      # knee: 12->0.934, 24->0.953, 48->0.962, 96->0.964


def load_corpus():
    by = defaultdict(list)
    for cls in sorted(os.listdir(CORPUS)):
        d = os.path.join(CORPUS, cls)
        if not os.path.isdir(d):
            continue
        code = ALIAS.get(cls, cls)
        if code not in ROSTER:
            continue        # patched-out weapons, and the no-weapon class
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(('.png', '.jpg')):
                by[code].append(os.path.join(d, f))
    return by


def features(paths):
    out = []
    for p in paths:
        f = feature(cv2.imread(p))
        if f is not None:
            out.append(f)
    return out


def build(by, per_class, holdout):
    """-> (labels, X, mean, basis, test_items)"""
    labels, rows, test = [], [], []
    for cls, fs in sorted(by.items()):
        keep = fs[:per_class]
        for f in features(keep):
            labels.append(cls)
            rows.append(f)
        if holdout:
            # LAST frames, not a random draw: consecutive captures of one gun
            # are near-duplicates, so a random split scores memorisation.
            test += [(cls, p) for p in fs[per_class:][-40:]]
    X = np.stack(rows).astype(np.float32)
    mean = X.mean(0)
    _, _, vt = np.linalg.svd(X - mean, full_matrices=False)
    basis = vt[:DIMS].astype(np.float32)
    return labels, (X - mean) @ basis.T, mean, basis, test


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--per-class', type=int, default=PER_CLASS)
    ap.add_argument('--eval', action='store_true',
                    help='score on frames the bank does not contain')
    ap.add_argument('-o', default=BANK_PATH)
    a = ap.parse_args()

    by = load_corpus()
    thin = [(c, len(f)) for c, f in sorted(by.items()) if len(f) < 12]
    missing = sorted(set(ROSTER) - set(by))
    print(f'corpus  : {sum(len(v) for v in by.values())} frames, '
          f'{len(by)} of {len(ROSTER)} roster weapons')
    if missing:
        print(f'  NO FRAMES AT ALL   : {", ".join(missing)}')
    if thin:
        print('  thin (<12 frames)  : '
              + ', '.join(f'{c}({n})' for c, n in thin))

    labels, P, mean, basis, test = build(by, a.per_class, a.eval)
    codes = sorted(set(labels))
    np.savez_compressed(a.o, mean=mean, basis=basis, proj=P,
                        labels=np.array(labels), codes=np.array(codes))
    size = os.path.getsize(a.o) / 1e6
    print(f'bank    : {len(labels)} exemplars x {DIMS}d over {len(codes)} '
          f'weapons -> {a.o} ({size:.1f} MB)')

    if a.eval:
        from detector.weapon_hud_detector import WeaponHudDetector
        det = WeaponHudDetector(path=a.o)
        hit, tot, conf = Counter(), Counter(), Counter()
        for cls, p in test:
            name, _m = det.read(cv2.imread(p))
            tot[cls] += 1
            if name == cls:
                hit[cls] += 1
            else:
                conf[(cls, name or '<none>')] += 1
        H, T = sum(hit.values()), sum(tot.values())
        print(f'\nhold-out: {H}/{T} = {H / max(T, 1):.4f} '
              f'({T} frames the bank has never seen)')
        for acc, c, h, t in sorted((hit[c] / tot[c], c, hit[c], tot[c])
                                   for c in tot if tot[c])[:6]:
            print(f'    {c:12} {h:4d}/{t:<4d} {acc:.3f}')
        for (g, p), n in conf.most_common(6):
            print(f'    {n:4d}  {g:12} -> {p}')

        # WHERE SHOULD MARGIN_MIN SIT? It is 0.011, and the module docstring
        # records why that is the wrong end: correct reads have a median
        # margin of 0.198 and WRONG ones 0.011. A floor placed at the median
        # of the error distribution passes half the errors by construction.
        #
        # So sweep it, and print BOTH costs the way attachment_detector's
        # table does -- a floor is a trade, and a report that only shows what
        # it saves is an argument, not a measurement.
        # ⚠ MEASURE WITH THE GATE OPEN. Reading through `det` applies
        # MARGIN_MIN first, so every refused frame leaves the "wrong" pile and
        # the table reports the CURRENT floor as 100% kept / 0% rejected --
        # whatever it is set to. A sweep that always says the status quo is
        # perfect is not a measurement. margin_min=0 makes it always name a
        # winner, which is what the raw margin distribution needs.
        raw = WeaponHudDetector(path=a.o, margin_min=0.0)
        marg = []
        for cls, p in test:
            name, m = raw.read(cv2.imread(p))
            marg.append((m, name == cls, bool(name)))
        got = [m for m, ok, named in marg if ok]
        bad = [m for m, ok, named in marg if named and not ok]
        if got and bad:
            got.sort(), bad.sort()
            print(f'\n  margin: correct median {got[len(got) // 2]:.3f}  '
                  f'wrong median {bad[len(bad) // 2]:.3f}')
            print('  floor   correct kept   wrong rejected')
            for f in (0.011, 0.02, 0.03, 0.05, 0.08, 0.12, 0.20):
                keep = sum(m >= f for m in got) / len(got)
                rej = sum(m < f for m in bad) / len(bad)
                mark = '   <- now' if abs(f - det.margin_min) < 1e-9 else ''
                print(f'  {f:<6.3f}  {keep:11.1%}   {rej:12.1%}{mark}')


if __name__ == '__main__':
    main()
