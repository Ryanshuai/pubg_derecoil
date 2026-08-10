"""Cross-check GunTagDetector against the name plate on the SAME frame.

    pixi run python .claude/skills/calibrate-gun-tag/scripts/cross_check.py
    pixi run python .claude/skills/calibrate-gun-tag/scripts/cross_check.py <dir> --out <review_dir>

The boxed slot number and the weapon name plate are painted under the same
condition -- panel up AND a gun in that slot -- and they sit inside the same
grabbed rectangle, so the plate is an independent witness at the same moment
for free. This prints the 2x2 per slot and dumps only the disagreements.

⚠ THE WITNESS IS `classify`, NOT `ink`. On the 38-frame corpus the name agrees
38/38 while `ink > 0` disagrees on two world frames reading 8132 and 9234 --
the same sky false positive that refuted "how much white is here" in the first
place. Sky has white; sky does not spell SKS.

⚠ AND THE SAVED FRAMES ARE STRIP+BLOCK. control/tab_watch.py:_compose lays the
「类型」 anchor strip above the panel block, so files in the sink are 587 rows
where tab_blocks()['right'] declares 557. Pasting one back whole shifts every
row by 30 px, puts 「类型」 in the tag box, and both detectors then return
confident numbers off the wrong pixels -- 80 fake disagreements out of 340,
against 2 real ones once the strip comes off. The subtraction below is derived
from tab_blocks() and never from the literal 30.
"""
import argparse
import collections
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    '..', '..', '..', '..'))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import cv2                                                      # noqa: E402
import numpy as np                                              # noqa: E402

from config import HUD_REGIONS                                  # noqa: E402
from detector.gun_tag_detector import GunTagDetector            # noqa: E402
from detector.tab_items import tab_blocks                       # noqa: E402
from detector.tab_layout import gun_tag_box                     # noqa: E402
from detector.weapon_template_detector import TabWeaponDetector  # noqa: E402

SINK = os.path.join(ROOT, 'calibration', 'artifacts', 'robot', 'tab')
BLOCK_Y, BLOCK_X, BLOCK_H, _ = tab_blocks()['right']

PER_SHEET = 10
BAR_H = 22


def as_screen(crop):
    """A saved block (with or without the anchor strip) -> screen coordinates.

    The strip height is whatever the file has ABOVE the declared block height,
    so a bare block loses nothing and a composed one loses exactly its strip.
    Deriving it beats hardcoding: the strip is another module's business and it
    has changed once already.
    """
    extra = crop.shape[0] - BLOCK_H
    if extra < 0:
        return None                     # not a block from this rectangle
    body = crop[extra:]
    frame = np.zeros((BLOCK_Y + body.shape[0], BLOCK_X + body.shape[1], 3),
                     np.uint8)
    frame[BLOCK_Y:, BLOCK_X:] = body
    return frame


def check(path, det, plate):
    """-> [{...}] one record per slot, or [] if the file is not usable."""
    crop = cv2.imread(path)
    if crop is None:
        return []
    frame = as_screen(crop)
    if frame is None:
        return []
    out = []
    for slot in (1, 2):
        y, x, h, w = HUD_REGIONS[f'gun_name_{slot}']
        if y + h > frame.shape[0] or x + w > frame.shape[1]:
            continue
        pc = frame[y:y + h, x:x + w]
        s = det.score(frame, slot)
        # Fed under gun_name_1 deliberately: classify keys off the dict, and
        # this crop IS the slot's plate whichever slot it came from.
        name = plate.classify({'gun_name_1': pc})[0]
        out.append({'file': path, 'slot': slot, 'tag': bool(s['drawn']),
                    'white': int(s['white']), 'median_v': float(s['median_v']),
                    'name': name, 'ink': int(plate.ink(pc)),
                    'frame': frame})
    return out


def _row(rec):
    """[tag box | name plate] side by side, under a caption bar.

    Both pictures, because the verdict is about whether those pixels are a
    slot number -- and the tag box alone cannot be argued with.
    """
    ty, tx, th, tw = gun_tag_box(rec['slot'])
    py, px, ph, pw = HUD_REGIONS[f'gun_name_{rec["slot"]}']
    f = rec['frame']
    tag = cv2.copyMakeBorder(f[ty:ty + th, tx:tx + tw], 0, max(0, ph - th), 0, 8,
                             cv2.BORDER_CONSTANT)
    body = np.hstack([tag, f[py:py + ph, px:px + pw]])
    text = (f"#{rec['idx']}  slot{rec['slot']}  "
            f"tag={'DRAWN' if rec['tag'] else 'no'}  white={rec['white']}  "
            f"medV={rec['median_v']:.0f}  plate={rec['name'] or '<none>'}  "
            f"ink={rec['ink']}  {os.path.basename(rec['file'])}")
    bar = np.zeros((BAR_H, body.shape[1], 3), np.uint8)
    scale = 0.45
    while scale > 0.20:
        (w, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        if w <= body.shape[1] - 8:
            break
        scale -= 0.02
    cv2.putText(bar, text, (4, BAR_H - 7), cv2.FONT_HERSHEY_SIMPLEX, scale,
                (90, 200, 255), 1, cv2.LINE_AA)
    return np.vstack([bar, body])


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('directory', nargs='?', default=SINK,
                    help='saved Tab blocks (default: the tab_watch sink)')
    ap.add_argument('--out', default=None,
                    help='write sheets + verdicts.jsonl for the disagreements')
    args = ap.parse_args()

    if not os.path.isdir(args.directory):
        print(f'no such directory: {args.directory}')
        return 1
    det, plate = GunTagDetector(), TabWeaponDetector()

    recs, strips = [], collections.Counter()
    for n in sorted(os.listdir(args.directory)):
        if not n.lower().endswith('.png'):
            continue
        p = os.path.join(args.directory, n)
        img = cv2.imread(p)
        if img is not None:
            strips[img.shape[0] - BLOCK_H] += 1
        recs.extend(check(p, det, plate))
    if not recs:
        print(f'no usable blocks under {args.directory}')
        return 1

    print(f'{args.directory}')
    print(f'  anchor-strip heights seen: '
          f'{dict(sorted(strips.items()))}   (block is {BLOCK_H} rows)')
    tally = collections.Counter((r['slot'], r['tag'], bool(r['name']))
                                for r in recs)
    print('\n  slot  tag      plate      n')
    for (slot, tag, named), n in sorted(tally.items()):
        mark = '' if tag == named else '   <- disagree'
        print(f'  {slot:>4}  {"DRAWN" if tag else "no":<7}  '
              f'{"named" if named else "<none>":<9}  {n:>3}{mark}')

    need = [r for r in recs if r['tag'] != bool(r['name'])]
    for i, r in enumerate(need):
        r['idx'] = i
    print(f'\n  {len(recs)} slot-readings, {len(recs) - len(need)} agree, '
          f'{len(need)} need an eye')
    if not need:
        print('  nothing to review')
        return 0
    if not args.out:
        print('  --out <dir> writes the sheets and a seeded verdicts.jsonl')
        return 0

    os.makedirs(args.out, exist_ok=True)
    for i in range(0, len(need), PER_SHEET):
        rows = [_row(r) for r in need[i:i + PER_SHEET]]
        w = max(x.shape[1] for x in rows)
        sheet = np.vstack([np.pad(x, ((0, 0), (0, w - x.shape[1]), (0, 0)))
                           for x in rows])
        cv2.imwrite(os.path.join(args.out, f'sheet_{i // PER_SHEET:03d}.png'),
                    sheet)
    vpath = os.path.join(args.out, 'verdicts.jsonl')
    with open(vpath, 'w', encoding='utf-8') as fh:
        for r in need:
            fh.write(json.dumps({
                'idx': r['idx'],
                'file': os.path.relpath(r['file'], ROOT).replace('\\', '/'),
                'slot': r['slot'], 'tag_drawn': r['tag'],
                'white': r['white'], 'median_v': round(r['median_v'], 1),
                'plate_name': r['name'], 'plate_ink': r['ink'],
                # tag_false_positive | tag_false_negative | plate_cannot_read
                'verdict': None,
                'reason': '',       # what you SAW, in words
            }, ensure_ascii=False) + '\n')
    print(f'  sheets + {os.path.relpath(vpath, ROOT)} -> {args.out}')
    print('  verdict is null on every line; unsure is a legal outcome')
    return 0


if __name__ == '__main__':
    sys.exit(main())
