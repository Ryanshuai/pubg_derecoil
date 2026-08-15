"""Weapon template matching — reads weapon name text from Tab inventory.

Matches white text in gun name region against OCR templates.

A weapon may have more than one template, because the plate is whatever the
game's language setting prints: 自动装填步枪 in Chinese, SLR in English. Every
variant lives in data/templates/ocr_white/ under the same weapon code with a
tag after it, and the best-scoring one wins:

    slr.png       either the sole variant, or the default one
    slr.cn.png    the Chinese plate
    slr.en.png    the English plate

Nothing selects a language — all variants are matched every frame, and one
language's plate simply does not resemble the other's. So a game switched
between languages mid-session still reads, with no configuration.

Cost is linear in variants: ~1 ms per extra template over the 250x45 plate,
and only on Tab frames.
"""
import os
import re

import cv2
import numpy as np

TMPL_THRESHOLD = 0.85
# How close a rival has to score before the tie-break decides instead of the
# score. 0.05 is set by the two cases that must NOT be disturbed: k2 sits 0.23
# behind sks on an SKS plate, far outside; m24 sits 0.001-0.009 from m249 on an
# M249 plate, well inside. Anything between those is unoccupied — the gap to
# the nearest wrong answer is either a rounding error or a fifth of the scale.
TIE_MARGIN = 0.05

TMPL_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'templates', 'ocr_white')
_OPEN_KERNEL = np.ones((3, 3), np.uint8)

# The criterion is ACHROMATIC AND BRIGHT, and either half alone lets the panel
# through: the panel is a blurred darkened photograph of the world, so it holds
# plenty of bright pixels (sky) and plenty of grey ones (concrete) -- just not
# both at once in the same pixel. On a name plate the panel behind the text
# reads gray p90 = 92 against 255 for the glyphs, so 180 sits in open country.
#
# ⚠ THIS PAIR HAD TWO AUTHORS UNTIL 2026-08-15, and one of them said so:
# row_name_detector carried `GRAY_MIN = 180` / `SPREAD_MAX = 30` under the
# comment "Same threshold pair as `_white_text_mask`" -- a declaration that one
# fact lived in two places, with nothing checking it, while THIS file wrote the
# same pair as bare literals inside the expression. Two copies of a number and
# a sentence claiming they agree is the shape `pixi run params` exists to
# refuse; it could not see this one, because a literal inside an `if` is not a
# parameter.
GRAY_MIN = 180
SPREAD_MAX = 30


def _white_text_mask(img_bgr, open_kernel=_OPEN_KERNEL):
    """White glyphs on the panel, 255/0. -> uint8

    ⚠ `open_kernel=None` IS THE ROW READER, AND THE DIFFERENCE IS MEASURED,
    not stylistic. Row labels are drawn several sizes smaller than a weapon
    plate and MORPH_OPEN with a 3x3 is wider than their strokes; the numbers
    are at row_name_detector.text_mask, which is the caller that needs it. The
    kernel is therefore the ONLY thing the two readers disagree about -- which
    is why they are one function with one parameter rather than two functions
    that happen to contain the same arithmetic.
    """
    f = img_bgr.astype(np.float32)
    spread = np.max(np.abs(np.stack([f[:,:,0]-f[:,:,1], f[:,:,1]-f[:,:,2],
                                     f[:,:,2]-f[:,:,0]], axis=2)), axis=2)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    out = np.zeros_like(gray)
    out[(gray > GRAY_MIN) & (spread < SPREAD_MAX)] = 255
    if open_kernel is None:
        return out
    return cv2.morphologyEx(out, cv2.MORPH_OPEN, open_kernel)


def _template_match(crop, templates, mask_fn=None):
    """[(iou, code), ...] best first, over the white text in `crop`.

    ⚠ `mask_fn` EXISTS SO THERE IS ONE SCORER, NOT TWO. The 库存 row labels
    need the same windowed IoU and the same prefix tie-break as the weapon
    plates -- "Suppressor (DMR, SR)" against "Suppressor (Handgun, SMG)" is
    the m24/m249 problem again -- but they cannot use `_white_text_mask`,
    whose MORPH_OPEN 3x3 is wider than a row label's strokes and erases them
    (measured: 908 ink pixels before the open, 0 after). Forking the scorer to
    change one line is how two implementations drift; `row_name_detector`
    passes its own mask instead. Default is unchanged.

    The IoU is taken inside the matched window, not over the whole crop. A
    plate can hold more text than its template covers — the game prints
    'Micro UZI 冲锋枪' where the template is only 'Micro UZI' — and dividing
    by every white pixel on the plate charges the template for glyphs it was
    never meant to explain. Measured on calibration/artifacts/tab_inventory*.png, that scored
    the correct UZI at 0.575, under the 0.85 threshold, so the gun read as
    unnamed; windowed it scores 0.995.

    Dividing by the template alone (inter/template) would also fix that case,
    but it gives any template that is a subset of the plate full marks: on the
    SKS plate it lifts the wrong answer 'k2' to 0.877 against the right one's
    0.959. Keeping the window's own pixels in the denominator still charges
    for ink *under* the template, and holds that gap at 0.959 vs 0.728.

    A WINDOWED SCORE CANNOT SEPARATE A PREFIX, which is what the tie-break
    below is for. 'M24' is a literal prefix of 'M249', so the M24 template
    lands on the first three glyphs of an M249 plate and its window stops
    before the '9' — the pixel that distinguishes them is outside the window
    and costs nothing. Measured over ten backgrounds of one M249 plate:

        m24  0.995 0.933 0.980 0.989 0.971 0.982 0.982 0.984 0.989 0.971
        m249 0.986 0.932 0.990 0.992 0.995 0.992 0.987 0.990 0.989 0.995

    — the wrong answer wins twice and ties once. Raising the threshold cannot
    fix that; both are near 1.0 and they cross.

    So among candidates that score within TIE_MARGIN of the best, the one
    covering MORE of the plate's ink wins. That is the question the windowed
    IoU throws away: m249 explains the '9' and m24 leaves it unexplained. It
    is a tie-break and not a new score, which is what keeps the two cases that
    shaped the scoring intact — 'Micro UZI' has no near-scoring rival to be
    compared against, and k2 is 0.23 behind sks, far outside the margin.
    """
    binary = (mask_fn or _white_text_mask)(crop)
    if np.count_nonzero(binary) == 0:
        return []
    results = []
    for code, tmpls in templates.items():
        best, covered = -1, 0
        for tmpl in tmpls:
            if tmpl.shape[0] > binary.shape[0] or tmpl.shape[1] > binary.shape[1]:
                continue
            res = cv2.matchTemplate(binary, tmpl, cv2.TM_CCOEFF_NORMED)
            if res.max() < 0.5:
                continue
            _, _, _, (tx, ty) = cv2.minMaxLoc(res)
            th, tw = tmpl.shape[:2]
            win = binary[ty:ty+th, tx:tx+tw]
            inter = np.count_nonzero(win & tmpl)
            union = np.count_nonzero(win) + np.count_nonzero(tmpl) - inter
            iou = inter / max(union, 1)
            if iou > best:
                # How much of the plate's ink this template accounts for. Not
                # part of the score — the tie-break below, and only that.
                best, covered = iou, inter
        if best > 0:
            results.append((best, covered, code))
    if not results:
        return []
    top = max(r[0] for r in results)
    near = sorted((r for r in results if r[0] >= top - TIE_MARGIN),
                  key=lambda r: (-r[1], -r[0]))
    rest = sorted((r for r in results if r[0] < top - TIE_MARGIN),
                  key=lambda r: -r[0])
    return [(iou, code) for iou, _cov, code in near + rest]


class TabWeaponDetector:
    """Reads weapon names from Tab inventory gun_name crops."""

    def __init__(self):
        self._templates = {}
        self._load_templates()

    def _load_templates(self):
        if not os.path.isdir(TMPL_DIR):
            return
        for fname in os.listdir(TMPL_DIR):
            # <code>.png, or <code>.<tag>.png for a per-language variant. The
            # code cannot contain a dot, so the first field is unambiguous.
            m = re.match(r'^([a-z0-9]+)(?:\.[a-z0-9]+)*\.png$', fname)
            if not m:
                continue
            binary = cv2.imread(os.path.join(TMPL_DIR, fname), cv2.IMREAD_GRAYSCALE)
            if binary is None:
                continue
            coords = cv2.findNonZero(binary)
            if coords is None:
                continue
            x, y, w, h = cv2.boundingRect(coords)
            pad = 2
            tmpl = binary[max(0, y-pad):min(binary.shape[0], y+h+pad),
                          max(0, x-pad):min(binary.shape[1], x+w+pad)]
            if tmpl.ndim == 3:
                tmpl = tmpl[:, :, 0]
            self._templates.setdefault(m.group(1), []).append(tmpl)

    @staticmethod
    def ink(crop):
        """How many white-text pixels are on this plate. -> int

        NOT a reading of the name — this cannot tell an AKM from an M416 and
        must not be used as if it could. It answers the weaker question
        `classify` cannot: **is there a name plate here at all?**

        That question has a caller. Whether a spawned weapon actually ARRIVED
        cannot be settled by the OCR, because the OCR is the thing under test:
        a spawn that silently produced nothing leaves the PREVIOUS gun in
        front of the camera, and the plate reads fine and names the wrong
        weapon. calibration/legacy_collect_templates.py records that hole, and one
        run of 40 frames was captured through it.

        With the rack emptied first, an empty slot draws no plate at all, so
        `0 ink -> ink` is arrival, established without consulting a template.
        The identity then comes from the request, which is the one thing that
        was never in doubt.

        It deliberately uses `_white_text_mask`, the SAME mask `classify`
        matches through. "There is text here" and "the OCR can read it" have
        to be claims about the same pixels, or the arrival test and the
        detector it is guarding disagree about what counts as text — which is
        exactly the kind of split this hole started as.

        A crop with no colour channels is refused rather than guessed at: the
        mask is defined on BGR and `IMREAD_GRAYSCALE` does not guarantee one
        channel here (anything importing ultralytics replaces cv2.imread).
        """
        if crop is None or crop.ndim != 3 or crop.shape[2] < 3:
            return 0
        return int((_white_text_mask(crop) > 0).sum())

    def classify(self, crops):
        """Match weapon names from gun_name crops.

        crops: {'gun_name_1': np.ndarray, 'gun_name_2': np.ndarray}
        Returns: (name_1, name_2) tuple, 0 if not matched.
        """
        results = []
        for key in ['gun_name_1', 'gun_name_2']:
            crop = crops.get(key)
            if crop is None:
                results.append('')
                continue
            matches = _template_match(crop, self._templates)
            if matches and matches[0][0] >= TMPL_THRESHOLD:
                results.append(matches[0][1])
            else:
                results.append('')
        return tuple(results)
