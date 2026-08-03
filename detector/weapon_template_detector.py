"""Weapon template matching — reads weapon name text from Tab inventory.

Matches white text in gun name region against OCR templates.

A weapon may have more than one template, because the plate is whatever the
game's language setting prints: 自动装填步枪 in Chinese, SLR in English. Every
variant lives in training_data/ocr_white/ under the same weapon code with a
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
TMPL_DIR = os.path.join(os.path.dirname(__file__), '..', 'training_data', 'ocr_white')
_OPEN_KERNEL = np.ones((3, 3), np.uint8)


def _white_text_mask(img_bgr):
    f = img_bgr.astype(np.float32)
    spread = np.max(np.abs(np.stack([f[:,:,0]-f[:,:,1], f[:,:,1]-f[:,:,2],
                                     f[:,:,2]-f[:,:,0]], axis=2)), axis=2)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    out = np.zeros_like(gray)
    out[(gray > 180) & (spread < 30)] = 255
    return cv2.morphologyEx(out, cv2.MORPH_OPEN, _OPEN_KERNEL)


def _template_match(crop, templates):
    """[(iou, code), ...] best first, over the white text in `crop`.

    The IoU is taken inside the matched window, not over the whole crop. A
    plate can hold more text than its template covers — the game prints
    'Micro UZI 冲锋枪' where the template is only 'Micro UZI' — and dividing
    by every white pixel on the plate charges the template for glyphs it was
    never meant to explain. Measured on docs/tab_inventory*.png, that scored
    the correct UZI at 0.575, under the 0.85 threshold, so the gun read as
    unnamed; windowed it scores 0.995.

    Dividing by the template alone (inter/template) would also fix that case,
    but it gives any template that is a subset of the plate full marks: on the
    SKS plate it lifts the wrong answer 'k2' to 0.877 against the right one's
    0.959. Keeping the window's own pixels in the denominator still charges
    for ink *under* the template, and holds that gap at 0.959 vs 0.728.
    """
    binary = _white_text_mask(crop)
    if np.count_nonzero(binary) == 0:
        return []
    results = []
    for code, tmpls in templates.items():
        best = -1
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
            best = max(best, iou)
        if best > 0:
            results.append((best, code))
    results.sort(reverse=True)
    return results


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
