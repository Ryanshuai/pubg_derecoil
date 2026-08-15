"""Read a 库存 / 附近 row by the NAME printed on it, not by its icon.

    from detector.row_name_detector import RowNameDetector
    det = RowNameDetector()
    det.read(frame, 3, 'inventory')     # ('bullet_loops', 0.98)
    det.classify(frame, 'inventory')    # {0: 'ext_sr', 1: 'comp_sr', ...}

    python detector/row_name_detector.py <shot.png>      # offline, one frame

WHY THE TEXT AND NOT THE ICON. Every other reader on this screen matches the
icon, and the icon is composited: `a*icon + (1-a)*(0.37*blur(scene) + 44)`, so
what it looks like depends on the world behind a translucent panel. The NAME
is not composited. Measured on a live frame, the glyphs sit at 255 with a
channel spread under 30 while the panel behind them is 86-92 and moves with
the view -- a gap of three to one that no background has to be modelled out
of.

That matters most exactly where the icon reader is weakest:

    a part with NO TEMPLATE reads as the nearest neighbour, confidently
    a part whose art drifted keeps matching, at a slowly worse score
    two same-family icons (three grey suppressor tubes) separate by 1.05

The name has none of those failure modes, because a name is not a picture of
the thing -- it is the game telling you what the thing is.

⚠ IT IS STILL A TEMPLATE, so it still has the bootstrap problem: a template
must exist before it can name what it was built from. What breaks the circle
is that the FIRST reading came from somewhere else -- a vision model read four
full-screen frames of known spawned batches, and every batch's names had to
match its keys as a set before any of it was kept (`tools/record_row_names.py`
holds that reading and its provenance). The templates here are cut from those
same frames. So the bank is downstream of a reading that needed no bank.

⚠ THE PREFIX PROBLEM IS SEVERE HERE, worse than on weapon plates. The list is
full of names that contain each other:

    Suppressor (DMR, SR)        Suppressor (AR, DMR, O12, S12K)
    QuickDraw Mag (Handgun,     Ext.QuickDraw Mag (Handgun, SMG)
    Extended Mag (DMR, SR)      Extended Mag (AR, DMR, M249, S12K)

A windowed IoU cannot separate a prefix -- the window stops before the
distinguishing glyphs, so they cost nothing. This is the m24/m249 case from
`weapon_template_detector`, which is why the scoring here is THAT function
rather than a copy of it: its tie-break (among near-equal scores, the template
covering MORE of the row's ink wins) is the thing that answers it.

⚠ ONE BACKGROUND PER TEMPLATE, deliberately, and this is the one place where
that is not the usual sin. Icons need many backgrounds because they are
blended; these glyphs are not. The mask is the same 908 pixels whatever is
behind the panel.
"""
import glob
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detector.tab_layout import PANELS, icon_box
from detector.weapon_template_detector import (_template_match,
                                              _white_text_mask)

TMPL_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'templates',
                        'ocr_white', 'rows')

# ⚠ THE THRESHOLD PAIR IS NOT HERE ANY MORE. `GRAY_MIN = 180` / `SPREAD_MAX =
# 30` used to sit at this spot under the comment "Same threshold pair as
# `_white_text_mask`" -- a sentence declaring that one fact had two authors,
# with nothing checking it. Both now live at their single author, and this
# reader gets them by CALLING that function rather than by agreeing with it.
# ⚠ NO MORPHOLOGY, AND THAT IS THE DIFFERENCE FROM THE PLATE READER. Row labels
# are drawn several sizes smaller than a weapon plate, and MORPH_OPEN with a
# 3x3 kernel is wider than their strokes. Measured over batch 0's thirteen
# rows, ink surviving the open:
#
#     none   908 861 765 965 1362 1210 1075 1247 1228 682 798 530 453
#     2x2    581 572 496 631  943  831  673  928  974 493 616 417 360
#     3x3      0   9   0   9   36   18    0   18   48  18  18   0   9
#
# The 3x3 row is not "noisier", it is EMPTY -- and an empty mask returns no
# candidates at all, which reads downstream as "this row holds nothing". The
# threshold is doing all the separating and needs no help.
OPEN_KERNEL = None
# The correct answer's floor. Set from the all-pairs scoring in
# `calibration/build_row_name_templates.py --score`; see its output for the
# margin to the best wrong answer.
IOU_MIN = 0.55


def label_box(row, panel='inventory'):
    """The name's rectangle on one list row. -> (x0, y0, x1, y1)

    It is the row minus the icon: `icon_box` gives the icon square and
    `PANELS` gives where the row ends, so the text is what lies between. Kept
    derived rather than measured so a layout change moves both together --
    a hand-copied x would keep working while pointing at the wrong strip.
    """
    _, y0, icon_x1, y1 = icon_box(row, panel)
    return icon_x1, y0, PANELS[panel][1], y1


def text_mask(bgr):
    """White glyphs on the panel, 255/0. -> uint8

    The plate reader's mask with the morphology turned off; the criterion, its
    thresholds and why they are what they are live at `_white_text_mask`, and
    what belongs HERE is the one thing this reader does differently and the
    measurement behind it (`OPEN_KERNEL` above).
    """
    return _white_text_mask(bgr, OPEN_KERNEL)


def tight(mask):
    """The mask cropped to its ink, or None if there is none.

    Padding is not free: it is charged to the template's own denominator in
    the windowed IoU, so a loosely cut template scores lower against the row
    it was cut FROM.
    """
    ys, xs = np.nonzero(mask)
    if not len(ys):
        return None
    return mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


class RowNameDetector:
    """Names the rows of a Tab list by matching their printed labels."""

    def __init__(self, tmpl_dir=None):
        self._templates = {}
        for p in sorted(glob.glob(os.path.join(tmpl_dir or TMPL_DIR, '*.png'))):
            img = cv2.imread(p, cv2.IMREAD_UNCHANGED)
            if img is None:
                continue
            # ⚠ IMREAD_GRAYSCALE DOES NOT GUARANTEE ONE CHANNEL -- anything
            # importing ultralytics replaces cv2.imread with a wrapper that
            # defaults to colour, and every mask op downstream then throws or,
            # worse, quietly compares the wrong array.
            if img.ndim == 3:
                img = img[:, :, 0]
            key = os.path.splitext(os.path.basename(p))[0].split('.')[0]
            self._templates.setdefault(key, []).append(
                (img > 127).astype(np.uint8) * 255)

    def __len__(self):
        return len(self._templates)

    def read(self, frame, row, panel='inventory'):
        """One row. -> (key, iou), or (None, best_iou) below the gate."""
        x0, y0, x1, y1 = label_box(row, panel)
        if y1 > frame.shape[0] or x1 > frame.shape[1]:
            return None, 0.0
        return self.read_crop(frame[y0:y1, x0:x1])

    def read_crop(self, crop):
        """-> (key, iou). A crop with no ink returns (None, 0.0)."""
        hits = self.rank(crop)
        if not hits:
            return None, 0.0
        iou, key = hits[0]
        return (key if iou >= IOU_MIN else None), iou

    def rank(self, crop):
        """Every template scored against this crop, best first.

        ⚠ THE SCORER IS `weapon_template_detector._template_match`, not a copy
        of it. Same windowed IoU, same prefix tie-break -- and this list needs
        the tie-break more than the plates do, because half the names here are
        prefixes of each other.
        """
        return _template_match(crop, self._templates, mask_fn=text_mask)

    def classify(self, frame, panel='inventory', rows=None):
        """-> {row: key} for the rows that read above the gate.

        Rows with no ink are simply absent: an empty list row and a row whose
        name could not be read are NOT the same answer, and returning None for
        both would merge them.
        """
        out = {}
        for i in range(rows if rows is not None else 15):
            x0, y0, x1, y1 = label_box(i, panel)
            if y1 > frame.shape[0] or x1 > frame.shape[1]:
                break
            key, _ = self.read_crop(frame[y0:y1, x0:x1])
            if key:
                out[i] = key
        return out


def main(argv):
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    if not argv:
        print(__doc__.strip().splitlines()[0])
        print('usage: python detector/row_name_detector.py <shot.png> [panel]')
        return 2
    frame = cv2.imread(argv[0])
    if frame is None:
        print(f'cannot read {argv[0]}')
        return 1
    panel = argv[1] if len(argv) > 1 else 'inventory'
    det = RowNameDetector()
    print(f'{len(det)} name template(s)')
    for i in range(15):
        x0, y0, x1, y1 = label_box(i, panel)
        if y1 > frame.shape[0]:
            break
        crop = frame[y0:y1, x0:x1]
        ink = int(text_mask(crop).sum()) // 255
        if not ink:
            continue
        hits = det.rank(crop)
        top = hits[0] if hits else (0.0, '-')
        second = hits[1] if len(hits) > 1 else (0.0, '-')
        print(f'  row {i:2d}  ink {ink:5d}  {top[1]:15} {top[0]:.3f}'
              f'   (2nd {second[1]:15} {second[0]:.3f}'
              f'  margin {top[0] - second[0]:+.3f})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
