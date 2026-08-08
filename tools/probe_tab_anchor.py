"""Measure the Tab screen's anchor: is the inventory up, or is that the sky?

The incumbent judgement counts bright pixels in HUD_REGIONS['type'] and calls
150..400 "open" (config.TAB_PIXEL_THRESH / TAB_COUNT_MIN / TAB_COUNT_MAX,
used by auto_calibrate.tab_is_open). On hand-picked negatives it looks
perfect — lobby, results, ESC menu and plain gameplay all measure exactly 0.

On 96 real ADS frames it is not: 13 carry ink and one lands inside the
window, a false "the inventory is open". Nine of them measure exactly 738,
which is 41x18 — the whole crop saturated. That region sits over the training
range's bright sky, and ADS magnifies the sky into it.

A count cannot separate "the glyph is drawn" from "everything here is white".
Matching the glyph's shape can. This probe builds a masked template of the
类型 header and reports both judgements over the same frames, so the
replacement is chosen on separation rather than on the negatives someone
happened to pick.

    pixi run python tools/probe_tab_anchor.py
    pixi run python tools/probe_tab_anchor.py --write    # save the template
"""
import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from config import HUD_REGIONS, TAB_COUNT_MAX, TAB_COUNT_MIN, TAB_PIXEL_THRESH
from detector.tab_layout import type_ink

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMPL_DIR = os.path.join(ROOT, 'data', 'templates', 'pubg_assets', 'tab')

# The glyph is drawn well above the threshold; the point of masking is to
# score only the strokes, so a uniformly bright crop cannot match.
GLYPH_THRESH = TAB_PIXEL_THRESH
SEARCH = 8              # +- px searched around the nominal position

# THE HEADER IS NOT ALWAYS THE SAME GLYPHS. calibration/artifacts/tab_inventory*.png render
# 类型; calibration/artifacts/lobby/in_game_tab.png renders "Type". Same screen, same place,
# different client language — and a single-language template scores the other
# one at 0.27, below the brightest negative. One template per language, and
# the score is the best of them, so the anchor survives a language switch
# instead of silently deciding the inventory is closed.
SOURCES = {
    'zh': os.path.join(ROOT, 'docs', 'tab_inventory.png'),
    'en': os.path.join(ROOT, 'calibration', 'artifacts', 'lobby', 'in_game_tab.png'),
}
POSITIVES = ('calibration/artifacts/lobby/in_game_tab.png', 'calibration/artifacts/tab_inventory.png',
             'calibration/artifacts/tab_inventory_2.png')
NEGATIVES = ('calibration/artifacts/lobby/in_game.png', 'calibration/artifacts/lobby/lobby.png',
             'calibration/artifacts/lobby/play_normal.png', 'calibration/artifacts/lobby/results.png',
             'calibration/artifacts/lobby/system_menu.png')


def _gray(path):
    im = cv2.imread(path)
    if im is None:
        return None
    if im.shape[0] != 1440 or im.shape[1] != 3440:
        return None
    return cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)


def build_template(gray):
    """Glyph-only mask of the header from one capture."""
    y, x, h, w = HUD_REGIONS['type']
    crop = gray[y:y + h, x:x + w]
    return (crop > GLYPH_THRESH).astype(np.uint8) * 255


def build_all(sources=None):
    """-> {lang: mask}. Skips any source that is missing."""
    out = {}
    for lang, path in (sources or SOURCES).items():
        g = _gray(path)
        if g is not None:
            out[lang] = build_template(g)
    return out


def score_one(gray, mask, search=SEARCH):
    """Best glyph IoU near the header's nominal position.

    IoU, not correlation. TM_CCORR_NORMED was tried first and inverted the
    problem completely — it scored the negatives 0.985..0.999 against
    positives of 0.887..1.000, because a normalised correlation over a dark
    or flat window says nothing about whether the strokes are there.

    IoU bounds the failure case by construction: a saturated crop matches
    every pixel of the template but also fills the union, so it can score no
    better than |template| / |crop| = 206/738 = 0.28 however bright it gets.
    """
    y, x, h, w = HUD_REGIONS['type']
    ref = mask > 0
    ref_n = int(ref.sum())
    best = 0.0
    for dy in range(-search, search + 1):
        for dx in range(-search, search + 1):
            yy, xx = y + dy, x + dx
            if yy < 0 or xx < 0 or yy + h > gray.shape[0] or \
                    xx + w > gray.shape[1]:
                continue
            cand = gray[yy:yy + h, xx:xx + w] > GLYPH_THRESH
            inter = int(np.logical_and(cand, ref).sum())
            union = int(cand.sum()) + ref_n - inter
            if union:
                best = max(best, inter / union)
    return best


def score(gray, masks, search=SEARCH):
    """Best IoU over every language template. -> (score, lang)"""
    best, who = 0.0, None
    for lang, m in masks.items():
        s = score_one(gray, m, search)
        if s > best:
            best, who = s, lang
    return best, who


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true',
                    help=f"save the masks to {TMPL_DIR}")
    ap.add_argument('--stride', type=int, default=9,
                    help='sample every Nth ADS frame')
    args = ap.parse_args()

    masks = build_all()
    if not masks:
        print('no template sources readable')
        return 1
    for lang, m in masks.items():
        n = int((m > 0).sum())
        print(f'template {lang}: {n} glyph px of {m.size} '
              f'({SOURCES[lang]})')
        print(f'   a saturated crop can score at most {n / m.size:.3f} '
              f'against it')

    rows = []
    for p in POSITIVES:
        g = _gray(p)
        if g is not None:
            sc, who = score(g, masks)
            rows.append(('open', type_ink(g), sc, who, p))
    for p in NEGATIVES:
        g = _gray(p)
        if g is not None:
            sc, who = score(g, masks)
            rows.append(('shut', type_ink(g), sc, who, p))

    print(f'\n{"state":6} {"ink":>5} {"score":>7} {"lang":>5}  file')
    for st, ink, sc, who, p in rows:
        print(f'{st:6} {ink:5d} {sc:7.3f} {who or "-":>5}  {p}')

    frames = sorted(glob.glob(os.path.join(ROOT, 'calibration', 'artifacts', 'ads', 'runs',
                                           '**', '*.jpg'), recursive=True))
    inks, scores = [], []
    for p in frames[::args.stride]:
        g = _gray(p)
        if g is None:
            continue
        inks.append(type_ink(g))
        scores.append(score(g, masks)[0])
    if not inks:
        print('\nno ADS frames found — skipping the bulk negative sweep')
        return 0

    fp_ink = sum(1 for v in inks if TAB_COUNT_MIN <= v <= TAB_COUNT_MAX)
    pos_scores = [sc for st, _, sc, _, _ in rows if st == 'open']
    print(f'\nADS negatives, n={len(inks)}')
    print(f'  ink    min {min(inks):4d}  max {max(inks):4d}  '
          f'inside {TAB_COUNT_MIN}..{TAB_COUNT_MAX}: {fp_ink}  <-- '
          f'{"FALSE POSITIVES" if fp_ink else "clean"}')
    print(f'  score  min {min(scores):.3f}  max {max(scores):.3f}')
    print(f'  positives score {min(pos_scores):.3f}..{max(pos_scores):.3f}')
    gap = min(pos_scores) - max(scores)
    print(f'  separation {gap:+.3f}  '
          f'({"usable" if gap > 0.05 else "NOT ENOUGH"})')

    if args.write:
        os.makedirs(TMPL_DIR, exist_ok=True)
        for lang, m in masks.items():
            p = os.path.join(TMPL_DIR, f'type_header_{lang}.png')
            cv2.imwrite(p, m)
            print(f'wrote {p}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
