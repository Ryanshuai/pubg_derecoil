"""Weapon template matching — reads weapon name text from Tab inventory.

Matches white text in gun name region against OCR templates.
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
    binary = _white_text_mask(crop)
    crop_px = np.count_nonzero(binary)
    if crop_px == 0:
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
            inter = np.count_nonzero(binary[ty:ty+th, tx:tx+tw] & tmpl)
            iou = inter / max(crop_px + np.count_nonzero(tmpl) - inter, 1)
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
            m = re.match(r'^([a-z0-9]+)\.png$', fname)
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
