"""Rebuild every weapon name-plate template from the captured plates. Offline.

    pixi run python calibration/build_name_templates.py            # report only
    pixi run python calibration/build_name_templates.py --write    # install them
    pixi run python calibration/build_name_templates.py --holdout 3

WHAT A TEMPLATE IS HERE. detector/weapon_template_detector.py is not an OCR,
whatever it is called: it holds ONE BINARY MASK per weapon and matches the
plate's white-text mask against it. So rebuilding a template means producing a
cleaner mask, not training anything.

WHY ONE CAPTURE IS NOT ENOUGH, and what the backgrounds buy. The mask is
"near-white and achromatic", which is the glyphs plus whatever scenery behind
the translucent panel happens to be near-white and achromatic at that moment —
a bright sky, a concrete wall, a headlight. That leak is different in every
background and the glyphs are the same in all of them, so a PER-PIXEL VOTE
across backgrounds keeps the text and drops the scenery. Ten backgrounds, a
pixel kept when at least VOTE_FRAC of them agree.

This is the same idea as the icon solve (calibration/legacy_solve_template.py) reached by a
different route. Icons are alpha-blended, so they need the backdrop measured
and the blend inverted. Plates are thresholded, so the scene is already mostly
gone and what is left is noise that averages out. Neither can be done from one
capture.

HOW IT IS JUDGED, and why not on everything. Building from all ten backgrounds
and then scoring on those same ten answers a question nobody asked -- the
template would be graded on the pixels it was made of. `--holdout k` builds
from N-k backgrounds and scores on the k it never saw, which is the only
number here worth quoting.

The corpus is calibration/legacy_collect_templates.py --plates. Every crop is labelled
by WHICH GUN WAS SPAWNED INTO THAT RACK ROW, watched arriving on an emptied
rack -- never by reading the plate. Grading a plate reader on labels the plate
reader produced would be circular, and that is exactly the trap this whole
collection chain exists to avoid.
"""
import argparse
import collections
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import cv2                                                    # noqa: E402
import numpy as np                                            # noqa: E402

from detector.weapon_template_detector import (                # noqa: E402
    TMPL_DIR, TMPL_THRESHOLD, TabWeaponDetector, _template_match,
    _white_text_mask)

RUNS = os.path.join(ROOT, 'calibration', 'artifacts', 'attachments', 'runs')

# A pixel is text when this fraction of the backgrounds agree it is. Not a
# majority (0.5) and not unanimity (1.0): antialiasing makes the outermost
# glyph pixels flicker, so unanimity erodes the strokes, while a bare majority
# keeps scenery that happens to be bright in half the shots.
VOTE_FRAC = 0.7

MIN_BACKGROUNDS = 4     # below this the vote has nothing to average out


def labelled_plates():
    """{weapon: [crop, ...]} — every plate crop whose label was REQUESTED."""
    out = collections.defaultdict(list)
    for man in sorted(glob.glob(os.path.join(RUNS, '*', 'manifest.json'))):
        m = json.load(open(man, encoding='utf-8'))
        for e in m['entries']:
            lab = [l for l in e.get('labels', []) if l.get('slot') == 'plate']
            if not lab:
                continue
            img = cv2.imread(os.path.join(os.path.dirname(man), e['capture']))
            if img is not None:
                out[lab[0]['asset']].append(img)
    return out


def vote(crops, frac=VOTE_FRAC):
    """One binary mask from many captures of the same plate. -> mask | None"""
    masks = [(_white_text_mask(c) > 0) for c in crops]
    shape = collections.Counter(m.shape for m in masks).most_common(1)[0][0]
    masks = [m for m in masks if m.shape == shape]
    if len(masks) < 2:
        return None
    keep = np.sum(np.stack(masks), axis=0) >= max(2, int(round(len(masks) * frac)))
    out = np.zeros(shape, np.uint8)
    out[keep] = 255
    coords = cv2.findNonZero(out)
    if coords is None:
        return None
    x, y, w, h = cv2.boundingRect(coords)
    return out[y:y + h, x:x + w]


def score(templates, corpus):
    """(correct, total, [(want, got), ...]) over every crop in `corpus`."""
    good = total = 0
    bad = collections.Counter()
    for want, crops in corpus.items():
        for c in crops:
            hits = _template_match(c, templates)
            got = hits[0][1] if hits and hits[0][0] >= TMPL_THRESHOLD else ''
            total += 1
            good += (got == want)
            if got != want:
                bad[(want, got)] += 1
    return good, total, bad


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--write', action='store_true',
                    help='install into data/templates/ocr_white/')
    ap.add_argument('--holdout', type=int, default=3,
                    help='backgrounds per weapon kept out of the build and '
                         'used to score it (default 3)')
    args = ap.parse_args()

    corpus = labelled_plates()
    thin = {k: len(v) for k, v in corpus.items() if len(v) < MIN_BACKGROUNDS}
    print(f'{sum(len(v) for v in corpus.values())} labelled plates, '
          f'{len(corpus)} weapons')
    if thin:
        print(f'  too few backgrounds to vote on: {thin}')

    # HELD OUT FIRST, so the build never sees what it is scored on.
    k = max(1, args.holdout)
    build = {w: c[:-k] for w, c in corpus.items() if len(c) > k}
    test = {w: c[-k:] for w, c in corpus.items() if len(c) > k}

    fresh = {}
    for w, crops in sorted(build.items()):
        if len(crops) < MIN_BACKGROUNDS:
            continue
        t = vote(crops)
        if t is not None:
            fresh[w] = [t]

    old = TabWeaponDetector()._templates
    o_ok, o_n, o_bad = score(old, test)
    n_ok, n_n, n_bad = score(fresh, test)
    print(f'\nheld out {k} background(s) per weapon — {o_n} crops neither side '
          f'was built from\n')
    print(f'  templates in the repo now   {o_ok}/{o_n}')
    print(f'  rebuilt from the other {len(build) and len(next(iter(build.values())))}'
          f'-ish  {n_ok}/{n_n}   ({len(fresh)} weapons rebuilt)')
    for name, bad in (('now', o_bad), ('rebuilt', n_bad)):
        for (want, got), c in bad.most_common(8):
            print(f'    {name:<8} {want:<9} -> {got or "<nothing>":<12} x{c}')

    if not args.write:
        print('\n  --write installs the rebuilt masks. Nothing written.')
        return 0
    if n_ok < o_ok:
        print(f'\n  REFUSING to write: the rebuild scores {n_ok} against the '
              f'current {o_ok} on crops neither was built from. A template set '
              f'that reads worse is not an update.')
        return 1
    os.makedirs(TMPL_DIR, exist_ok=True)
    for w, (t,) in sorted(fresh.items()):
        cv2.imwrite(os.path.join(TMPL_DIR, f'{w}.png'), t)
    print(f'\n  wrote {len(fresh)} masks -> '
          f'{os.path.relpath(TMPL_DIR, ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
