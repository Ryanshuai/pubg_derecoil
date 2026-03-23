"""Weapon template matching — reads weapon name text from Tab inventory."""
import os
import sys
import re

import cv2
import numpy as np
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import GUN_NAME_1, GUN_NAME_2

_logger = logger.bind(detector='weapon')

OCR_RECTS = {
    1: (GUN_NAME_1['y1'], GUN_NAME_1['x1'],
        GUN_NAME_1['y2'] - GUN_NAME_1['y1'], GUN_NAME_1['x2'] - GUN_NAME_1['x1']),
    2: (GUN_NAME_2['y1'], GUN_NAME_2['x1'],
        GUN_NAME_2['y2'] - GUN_NAME_2['y1'], GUN_NAME_2['x2'] - GUN_NAME_2['x1']),
}

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


class WeaponTemplateDetector:

    def __init__(self, state):
        self.state = state
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
        if self._templates:
            n = sum(len(v) for v in self._templates.values())
            print(f'[Template] {len(self._templates)} weapons, {n} templates')

    def classify(self, crop):
        """Match weapon name from a single crop. Returns name or ''."""
        matches = _template_match(crop, self._templates)
        if not matches:
            return ''
        best_iou, best_code = matches[0]
        if best_iou >= TMPL_THRESHOLD:
            return best_code
        return ''

    def read_from_crops(self, crop_1, crop_2):
        """Match weapon names from two crops. Updates state directly."""
        for slot_id, crop in [(1, crop_1), (2, crop_2)]:
            matches = _template_match(crop, self._templates)
            if not matches:
                _logger.info(f'OCR slot{slot_id} | no match')
                continue
            best_iou, best_code = matches[0]
            if best_iou >= TMPL_THRESHOLD:
                second = f' 2nd={matches[1][1]}={matches[1][0]:.3f}' if len(matches) > 1 else ''
                _logger.info(f'OCR slot{slot_id} | {best_code} iou={best_iou:.3f}{second}')
                self.state.set_weapon(slot_id, best_code)
            else:
                _logger.info(f'OCR slot{slot_id} | best={best_code} iou={best_iou:.3f} | rejected')
        self.state.gt_valid = True
