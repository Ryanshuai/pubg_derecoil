"""Ammo counter — rounds left in the magazine, read off the HUD digits.

The count already drives the unattended calibration runs, but only as a
*change* signal: `sweep.py` binarises the same crop and watches the pixels
move, which says "a shot landed" or "the reload finished" without ever saying
how many rounds are left. That is enough to segment a magazine and nothing
more. Reading the value turns it into a counter — how many rounds a burst
actually fired, which shot a measured recoil sample belongs to, whether the
magazine is the 30 the catalogue claims or the 40 an extended mag made it.

Why segment-then-match rather than an OCR library: the digits are opaque
white on a dim translucent bar, always the same font at the same size, and
nothing else in the band survives the threshold. Measured over 856 captures:

    every digit is 17-18 px wide and **37 px tall, without exception**
    the top edge sits at y=1323 in all 856
    the run is centred on x=1719, not left- or right-aligned
    the only other bright things in the band are the magazine icon's
      stripes and the HUD underline, all <= 4 px tall

So a height filter alone separates glyphs from furniture, and the glyphs come
out pixel-identical frame to frame. Template IoU on a fixed canvas is exact
here in a way no general OCR would be, and costs ~0.2 ms.

Centred, not right-aligned, is the load-bearing measurement: a three-digit
count (100-round drums) grows *both* ways from x=1719, spanning roughly
1686..1752. That still fits inside HUD_REGIONS['ammo'] (1670..1760), so the
existing region needs no change — but a crop anchored on the right edge would
have clipped the leading digit.
"""
import os

import cv2
import numpy as np

ASSETS_DIR = os.path.join(os.path.dirname(__file__), '..', 'docs', 'training_data',
                          'pubg_assets', 'ammo')

# The glyphs are fully opaque, the bar behind them is not. Measured on the
# reference band: digits peak at 249, the bar and the alpha-blended icons stay
# under 200. 220 sits in empty space between the two.
WHITE_THRESH = 220

# Height is the whole segmentation. Digits are 37; the magazine icon's stripes
# are 3-4 and the HUD underline is 2, so any window around 37 works. The band
# is generous because a future HUD scale change should read as "no digits"
# rather than as a wrong number.
MIN_H, MAX_H = 25, 45
MAX_W = 30              # a digit is 17-18; wider means two glyphs merged
MAX_DIGITS = 3          # 100-round drums are the largest magazine in the game

# Glyphs are placed, not resized, into a canvas with room to spare: resizing
# 17->18 would thin the strokes and cost more IoU than the 1 px it corrects.
# The jitter absorbs that 1 px instead.
CANVAS_W, CANVAS_H = 24, 42
JITTER = 1
_N_SHIFTS = (2 * JITTER + 1) ** 2

# Set from measurement, and set high on purpose. Over a full magazine swept
# 150 -> 1 (tools/collect_ammo_digits.py --verify) the worst genuine match was
# 0.968, while the closest impostor pair in the installed set is 6 vs 9 at
# 0.869 (tools/probe_ammo_ocr.py --confusion). 0.90 sits in that gap.
#
# A low threshold here does not fail safely. Before the set was complete, the
# missing digits did not read as None — they were confidently captured by the
# nearest template, every '3' landing on 8 at 0.748 and every '9' on 0 at
# 0.800, consistently enough to look like a real reading. That is the silent
# wrong answer detector/CLAUDE.md's first rule is about, and the only defence
# is to demand a score no impostor can reach.
MIN_IOU = 0.90
MIN_MARGIN = 0.05       # over the runner-up, so a half-drawn glyph reads None


def _place(glyph):
    """Centre a tight glyph mask in the fixed canvas."""
    out = np.zeros((CANVAS_H, CANVAS_W), np.uint8)
    h, w = glyph.shape
    if h > CANVAS_H or w > CANVAS_W:
        return out
    y = (CANVAS_H - h) // 2
    x = (CANVAS_W - w) // 2
    out[y:y + h, x:x + w] = glyph
    return out


def _iou(a, b):
    union = np.count_nonzero(a | b)
    return np.count_nonzero(a & b) / union if union else 0.0


def _shifts(t):
    """Every +-JITTER translation of a canvas template.

    Both glyph and template are centred with >=2 px of blank margin on each
    side, so np.roll's wraparound only ever carries empty rows — and the
    pixel count is therefore identical across shifts, which is what lets the
    union be computed from two scalars instead of a second bitwise pass.
    """
    out = []
    for dy in range(-JITTER, JITTER + 1):
        for dx in range(-JITTER, JITTER + 1):
            out.append(t if (dx == 0 and dy == 0) else
                       np.roll(np.roll(t, dy, axis=0), dx, axis=1))
    return out


def _best_iou(a, b):
    """IoU over +-JITTER px of shift. Used by the offline confusion audit;
    the detector itself runs the vectorised path in AmmoDetector."""
    return max(_iou(a, s) for s in _shifts(b))


def segment(crop):
    """[(x, tight glyph mask), ...] left to right, furniture removed."""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    mask = (gray > WHITE_THRESH).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if not (MIN_H <= h <= MAX_H) or w > MAX_W:
            continue
        out.append((int(x), (lab[y:y + h, x:x + w] == i).astype(np.uint8)))
    out.sort(key=lambda r: r[0])
    return out


def load_templates(directory=ASSETS_DIR):
    """{digit: canvas mask} from digit_<n>.png."""
    templates = {}
    if not os.path.isdir(directory):
        return templates
    for d in range(10):
        path = os.path.join(directory, f'digit_{d}.png')
        if not os.path.exists(path):
            continue        # a digit not yet harvested, not an error
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        if img.ndim == 3:
            # IMREAD_GRAYSCALE is not always honoured: ultralytics replaces
            # cv2.imread with its own wrapper that defaults to IMREAD_COLOR,
            # so once anything in the process imports it, this comes back
            # 3-channel and findNonZero/boundingRect fall over. Caught by
            # smoke_check, which imports ultralytics; a script that does not
            # would have shipped this bug. weapon_template_detector guards the
            # same way.
            img = img[:, :, 0]
        coords = cv2.findNonZero((img > 127).astype(np.uint8))
        if coords is None:
            continue
        x, y, w, h = cv2.boundingRect(coords)
        templates[d] = _place((img[y:y + h, x:x + w] > 127).astype(np.uint8))
    return templates


class AmmoDetector:
    """Reads the magazine counter from the HUD_REGIONS['ammo'] crop."""

    def __init__(self, directory=ASSETS_DIR):
        self._templates = load_templates(directory)
        # One (digits*shifts, H, W) block, so scoring a glyph is a single
        # broadcast AND plus a row-sum instead of a Python loop over 10
        # templates x 9 shifts. Measured 0.667 -> 0.05 ms per read.
        self._digit_of = np.array(
            [d for d in sorted(self._templates) for _ in range(_N_SHIFTS)],
            dtype=np.int32)
        stack = [s for d in sorted(self._templates)
                 for s in _shifts(self._templates[d])]
        self._stack = np.stack(stack) if stack else np.zeros((0, 1, 1), np.uint8)
        self._stack_px = self._stack.reshape(len(self._stack), -1) \
                             .sum(1, dtype=np.int32) if stack else \
            np.zeros(0, np.int32)

    @property
    def digits_known(self):
        return sorted(self._templates)

    def _score(self, canvas):
        """[(iou, digit), ...] best first, over every template and shift."""
        flat = canvas.reshape(-1)
        inter = (flat[None] & self._stack.reshape(len(self._stack), -1)) \
            .sum(1, dtype=np.int32)
        union = int(flat.sum()) + self._stack_px - inter
        iou = np.where(union > 0, inter / np.maximum(union, 1), 0.0)
        best = {}
        for d, s in zip(self._digit_of, iou):
            d = int(d)
            if s > best.get(d, -1.0):
                best[d] = float(s)
        return sorted(((s, d) for d, s in best.items()), reverse=True)

    def read(self, crops):
        """Full detail for tools: value, per-glyph scores, why it failed.

        Returns {'value': int|None, 'glyphs': [{'x', 'digit', 'iou',
        'runner_up', 'margin'}], 'reason': str}.
        """
        crop = crops.get('ammo') if isinstance(crops, dict) else crops
        if crop is None or crop.size == 0:
            return {'value': None, 'glyphs': [], 'reason': 'no crop'}
        if not self._templates:
            return {'value': None, 'glyphs': [], 'reason': 'no templates'}

        found = segment(crop)
        if not found:
            return {'value': None, 'glyphs': [], 'reason': 'no glyphs'}
        if len(found) > MAX_DIGITS:
            return {'value': None, 'glyphs': [],
                    'reason': f'{len(found)} glyphs, max {MAX_DIGITS}'}

        out, value, ok = [], 0, True
        for x, glyph in found:
            scored = self._score(_place(glyph))
            top_iou, top_d = scored[0]
            runner = scored[1][0] if len(scored) > 1 else 0.0
            out.append({'x': x, 'digit': top_d, 'iou': top_iou,
                        'runner_up': scored[1][1] if len(scored) > 1 else None,
                        'margin': top_iou - runner})
            if top_iou < MIN_IOU or top_iou - runner < MIN_MARGIN:
                ok = False
            value = value * 10 + top_d

        if not ok:
            worst = min(out, key=lambda g: g['iou'])
            return {'value': None, 'glyphs': out,
                    'reason': f"weak glyph at x={worst['x']} "
                              f"iou={worst['iou']:.3f} "
                              f"margin={worst['margin']:.3f}"}
        return {'value': value, 'glyphs': out, 'reason': ''}

    def classify(self, crops):
        """Rounds left in the magazine, or None if the HUD did not read.

        None is not zero. An empty magazine still prints '0'; None means the
        digits were absent or unrecognisable — inventory open, weapon holstered,
        mid-animation — and a caller that treats it as 0 will think it just
        fired the whole magazine.
        """
        return self.read(crops)['value']
