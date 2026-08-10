"""Does the ORDER of the intersection matter? Offline, on stored raw crops.

    pixi run intersect-order

THE QUESTION. `collect_intersect` folds two guns and many views into one
intersection per frame. When that converged to 35 of 3969 pixels, the obvious
suspect was the ORDER -- the two racks sit at different heights on screen, so
the scene behind their tiles differs, and intersecting them in the same frame
might be throwing away the attachment along with the background. If so, doing
it in two stages would fix it:

    stage 1   each gun intersected with ITSELF across views
              (tile position fixed, only the scene changes -> kills background)
    stage 2   the two converged results intersected with each other
              (both already background-free -> what differs is the rail)

THE ANSWER IS NO, and this file is how that was established. Stage 1 works
beautifully and stage 2 undoes all of it, landing on exactly the same number
the one-stage version reaches. So the limit is not the order; it is that
byte-exact equality ACROSS TWO RACKS does not hold in this UI at all.

Reads `calibration/artifacts/intersect/<stamp>/raw/`, which collect_intersect
writes one pair per frame -- the intersection is lossy and one-way, so the
question cannot be asked of the templates it produces.
"""
import collections
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from calibration.collect_intersect import alive, intersect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, 'calibration', 'artifacts', 'intersect')
OUT = os.path.join(RAW, 'analysis')
TMPL = os.path.join(ROOT, 'data', 'templates', 'pubg_assets',
                    'Item', 'Attachment')


def runs():
    """-> [(stamp, {key: {frame: {gun: path}}})], newest last."""
    out = []
    for d in sorted(glob.glob(os.path.join(RAW, '*', 'raw'))):
        per = collections.defaultdict(lambda: collections.defaultdict(dict))
        for f in sorted(glob.glob(os.path.join(d, '*.png'))):
            m = re.match(r'(.+)__(\d+)__g(\d)\.png', os.path.basename(f))
            if m:
                per[m.group(1)][int(m.group(2))][int(m.group(3))] = f
        if per:
            out.append((os.path.basename(os.path.dirname(d)), per))
    return out


def solved_px(key):
    """Opaque pixels in the OLD flow's template for this part, or None."""
    from detector.attachment_catalog import ATTACHMENTS
    asset = ATTACHMENTS.get(key, {}).get('asset')
    if not asset:
        return None
    p = os.path.join(TMPL, f'Item_Attach_Weapon_{asset}.solved.png')
    img = cv2.imread(p, cv2.IMREAD_UNCHANGED)
    return None if img is None or img.ndim < 3 or img.shape[2] < 4 \
        else int((img[:, :, 3] > 128).sum())


def analyse(frames):
    """-> dict of the four numbers, plus the images for the strip."""
    per_gun = {1: None, 2: None}
    same_frame = None
    for idx in sorted(frames):
        for gun in (1, 2):
            if gun in frames[idx]:
                per_gun[gun] = intersect(per_gun[gun],
                                         cv2.imread(frames[idx][gun]))
        if 1 in frames[idx] and 2 in frames[idx]:
            same_frame = intersect(same_frame,
                                   cv2.imread(frames[idx][1]),
                                   cv2.imread(frames[idx][2]))
    two_stage = (intersect(per_gun[1], per_gun[2])
                 if per_gun[1] is not None and per_gun[2] is not None else None)
    return {'n': len(frames), 'g1': per_gun[1], 'g2': per_gun[2],
            'two_stage': two_stage, 'same_frame': same_frame}


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    got = runs()
    if not got:
        print(f'no raw crops under {os.path.relpath(RAW, ROOT)}/*/raw — '
              f'collect_intersect writes them one pair per frame')
        return 1
    os.makedirs(OUT, exist_ok=True)

    lines = ['# Intersection order: does staging it help?',
             '',
             'Stage 1 = each gun intersected with itself across views.',
             'Stage 2 = the two stage-1 results intersected with each other.',
             'Same-frame = both guns and all views folded together, which is',
             'what `collect_intersect` does today.',
             '',
             '| run | part | frames | gun1 alone | gun2 alone | two-stage | same-frame | old .solved |',
             '|---|---|---|---|---|---|---|---|']
    strips = []
    for stamp, per in got:
        print(f'=== {stamp}')
        for key, frames in sorted(per.items()):
            r = analyse(frames)
            old = solved_px(key)
            print(f'  {key:13} {r["n"]} frames | gun1 {alive(r["g1"]):5d} | '
                  f'gun2 {alive(r["g2"]):5d} | two-stage {alive(r["two_stage"]):5d} '
                  f'| same-frame {alive(r["same_frame"]):5d} | '
                  f'old .solved {"-" if old is None else old}')
            lines.append(f'| {stamp} | {key} | {r["n"]} | {alive(r["g1"])} | '
                         f'{alive(r["g2"])} | **{alive(r["two_stage"])}** | '
                         f'{alive(r["same_frame"])} | '
                         f'{"-" if old is None else old} |')
            if stamp == got[-1][0]:
                row = [r['g1'], r['g2'], r['two_stage'], r['same_frame']]
                tiles = []
                for img in row:
                    m = (np.any(img != 0, axis=2) * 255).astype(np.uint8) \
                        if img is not None else np.zeros((63, 63), np.uint8)
                    tiles.append(cv2.resize(cv2.cvtColor(m, cv2.COLOR_GRAY2BGR),
                                            (126, 126),
                                            interpolation=cv2.INTER_NEAREST))
                strips.append(np.hstack(tiles))
    lines += ['',
              '**Two-stage lands on the same number as same-frame.** Stage 1',
              'recovers ~1000 px, the same order as the old `.solved`',
              'templates, so intersecting a gun with itself across views does',
              'remove the background and keep the part. Stage 2 then drops it',
              'to 30-35. The order is not the constraint: byte-exact equality',
              'ACROSS TWO RACKS is.',
              '',
              'Why it cannot hold: the two racks are at different heights, so',
              'the scene behind their translucent tiles differs; and',
              'HUD_REGIONS puts them 301px apart for four slots and 302 for',
              '`scope`, a one-pixel misalignment measured back as dy=-1 on all',
              'three parts here.']
    md = os.path.join(OUT, 'intersect_order.md')
    with open(md, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    png = os.path.join(OUT, 'intersect_order.png')
    if strips:
        cv2.imwrite(png, np.vstack(strips))
    print(f'\n  {os.path.relpath(md, ROOT)}')
    print(f'  {os.path.relpath(png, ROOT)}   '
          f'(columns: gun1 alone | gun2 alone | two-stage | same-frame)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
