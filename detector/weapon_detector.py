"""Weapon detector — combines HUD watermark model + Tab OCR.

Two signal sources:
  - Model: classifies weapon icon watermark on HUD (real-time, may be noisy)
  - OCR: reads weapon name text in Tab view (accurate, only when Tab open)

Priority: OCR ground truth > model prediction.
Feedback: mismatch between OCR GT and model → save crop. Hard case → save crop.
"""
import os
import sys
import time
from datetime import datetime

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from rapidocr_onnxruntime import RapidOCR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import WEAPON_HUD_1, WEAPON_HUD_2, GUN_NAME_1, GUN_NAME_2, HARD_CASE_CONF
from detector.cropper import win32_cap
from detector.utils import load_model as _load, crop_to_tensor_4ch, img_hash as _img_hash
from dl_models.icon_layout import WEAPON_CLASSES

HL_NAMES = {0: '', 1: 'highlighted', 2: 'non-highlighted'}

# ── Screen rects ──

ICON_H = 53

def _icon_rect(hud):
    w = hud['x2'] - hud['x1']
    y = hud['y1'] + hud['icon_offset_y']
    return (y, hud['x1'], ICON_H, w)

SLOT_RECTS = {
    1: _icon_rect(WEAPON_HUD_1),
    2: _icon_rect(WEAPON_HUD_2),
}

OCR_RECTS = {
    1: (GUN_NAME_1['y1'], GUN_NAME_1['x1'],
        GUN_NAME_1['y2'] - GUN_NAME_1['y1'], GUN_NAME_1['x2'] - GUN_NAME_1['x1']),
    2: (GUN_NAME_2['y1'], GUN_NAME_2['x1'],
        GUN_NAME_2['y2'] - GUN_NAME_2['y1'], GUN_NAME_2['x2'] - GUN_NAME_2['x1']),
}

# ── Model ──

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'gun_name.pth.tar')
HEAD_SIZES = {'gun_name': len(WEAPON_CLASSES) + 1, 'highlighted': 3}

# ── Feedback ──

FEEDBACK_BASE = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'weapon')
FEEDBACK_GT_DIR = os.path.join(FEEDBACK_BASE, 'gt_mismatch')
FEEDBACK_OCR_DIR = os.path.join(FEEDBACK_BASE, 'ocr_changed')
FEEDBACK_HL_DIR = os.path.join(FEEDBACK_BASE, 'highlight')
FEEDBACK_HARD_DIR = os.path.join(FEEDBACK_BASE, 'hard_case')
OCR_CONF_THRESHOLD = 0.6



class WeaponDetector:
    """Unified weapon detector: model + OCR, with feedback."""

    def __init__(self, device):
        self.device = device
        self.model = _load(MODEL_PATH, HEAD_SIZES, device, in_channels=4, hidden_dim=1024)

        # OCR
        self.ocr = RapidOCR()
        dummy = np.zeros((45, 250, 3), dtype=np.uint8)
        self.ocr(dummy, use_det=False, use_cls=False)  # warmup

        # Tab OCR ground truth
        self._gt = {'weapon_1': '', 'weapon_2': ''}
        self._gt_valid = False
        self._ocr_cache = {'weapon_1': '', 'weapon_2': ''}

        # Track highlight state for mutex check
        self._hl = {1: '', 2: ''}
        self._last_crops = {1: None, 2: None}


    # ── OCR ──

    def _ocr_recognize(self, crop):
        """Recognize weapon name from Tab text crop. Returns (name, conf)."""
        result, _ = self.ocr(crop, use_det=False, use_cls=False)
        if not result:
            return '', 0.0
        text, conf = result[0]
        if conf < OCR_CONF_THRESHOLD or not text.strip():
            return '', conf
        text_lower = text.strip().lower()
        for cls in WEAPON_CLASSES:
            if cls == text_lower:
                return cls, conf
        for cls in WEAPON_CLASSES:
            if cls in text_lower or text_lower in cls:
                return cls, conf
        return text_lower, conf

    def ocr_from_screen(self):
        """Capture and OCR both weapon name slots. Returns {1: (name, conf), 2: (name, conf)}."""
        results = {}
        for slot_id in [1, 2]:
            crop = win32_cap(OCR_RECTS[slot_id])
            results[slot_id] = self._ocr_recognize(crop)
        return results

    # ── Tab OCR ground truth ──

    def update_ocr_cache(self):
        """Called while Tab is open. Read weapon names and cache them.
        Empty OCR = no weapon → cache cleared. Changed = save for review."""
        ocr = self.ocr_from_screen()
        for slot_id in [1, 2]:
            name, conf = ocr[slot_id]
            key = f'weapon_{slot_id}'
            prev = self._ocr_cache.get(key, '')

            if name and conf > OCR_CONF_THRESHOLD:
                # Got a valid name
                if prev and prev != name:
                    crop = win32_cap(OCR_RECTS[slot_id])
                    os.makedirs(FEEDBACK_OCR_DIR, exist_ok=True)
                    h = _img_hash(crop)
                    fname = f'was={prev}_now={name}_{h}.png'
                    path = os.path.join(FEEDBACK_OCR_DIR, fname)
                    if not os.path.exists(path):
                        cv2.imwrite(path, crop)
                self._ocr_cache[key] = name
            else:
                # OCR empty → no weapon in this slot
                self._ocr_cache[key] = ''

    def lock_ocr_gt(self):
        """Called when Tab closes. Lock cached OCR as ground truth.
        Empty cache = no weapon in that slot, GT updated accordingly."""
        for slot_key in ['weapon_1', 'weapon_2']:
            self._gt[slot_key] = self._ocr_cache.get(slot_key, '')
        self._gt_valid = True
        print(f'[GT weapon] locked: weapon_1={self._gt["weapon_1"]!r}, '
              f'weapon_2={self._gt["weapon_2"]!r}')

    def invalidate_gt(self, reason=''):
        """Called when weapon state may have changed (switch/pickup/drop).
        Clears GT so no more feedback until next Tab."""
        if self._gt_valid:
            self._gt_valid = False
            self._gt = {'weapon_1': '', 'weapon_2': ''}
            if reason:
                print(f'[GT weapon] invalidated: {reason}')

    # ── Model classify ──

    def classify_slot(self, crop, slot_id):
        """Classify weapon from HUD watermark crop.

        Returns (gun_name, hl_name).
        Saves feedback: GT mismatch or hard case.
        Output priority: GT (if available) > model.
        """
        tensor = crop_to_tensor_4ch(crop, self.device)
        with torch.no_grad():
            out = self.model(tensor)

        gun_logits = out['gun_name'][0]
        gun_probs = F.softmax(gun_logits, dim=0)
        gun_conf = gun_probs.max().item()
        gun_id = gun_probs.argmax().item()
        hl_id = out['highlighted'].argmax(1).item()

        model_name = WEAPON_CLASSES[gun_id - 1] if gun_id > 0 else ''
        hl_name = HL_NAMES[hl_id]

        slot_key = f'weapon_{slot_id}'
        gt = self._gt.get(slot_key, '') if self._gt_valid else ''

        if model_name:
            # GT mismatch (only when GT is valid)
            if gt and gt != model_name:
                self._save_gt_mismatch(gt, model_name, hl_name, crop)
                self._save_highlight(gt, model_name, hl_name, crop)

            # Hard case (only when GT is available)
            elif gt and HARD_CASE_CONF[0] < gun_conf < HARD_CASE_CONF[1]:
                self._save_hard_case(gt, model_name, hl_name, gun_conf, crop)
                self._save_highlight(gt, model_name, hl_name, crop)

        # Track highlight + crop for mutex check
        self._hl[slot_id] = hl_name
        self._last_crops[slot_id] = crop

        # Highlight mutex: both slots can't be highlighted
        if self._hl[1] == 'highlighted' and self._hl[2] == 'highlighted':
            self._save_highlight('unk', model_name, hl_name, crop)

        # Output: prefer valid GT, fallback to model
        out_name = gt if (gt and self._gt_valid) else model_name

        return out_name, hl_name

    # ── Feedback save ──

    def _save_gt_mismatch(self, ocr_name, model_name, hl_name, crop):
        """Save GT mismatch: ocr_A_dl_B_l/h_<hash>.png"""
        os.makedirs(FEEDBACK_GT_DIR, exist_ok=True)
        hl_tag = 'h' if hl_name == 'highlighted' else 'l'
        h = _img_hash(crop)
        fname = f'ocr_{ocr_name}_dl_{model_name}_{hl_tag}_{h}.png'
        path = os.path.join(FEEDBACK_GT_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)

    def _save_highlight(self, ocr_name, model_name, hl_name, crop):
        """Save highlight info: ocr_A_dl_B_l/h_<hash>.png"""
        os.makedirs(FEEDBACK_HL_DIR, exist_ok=True)
        hl_tag = 'h' if hl_name == 'highlighted' else 'l'
        h = _img_hash(crop)
        fname = f'ocr_{ocr_name}_dl_{model_name}_{hl_tag}_{h}.png'
        path = os.path.join(FEEDBACK_HL_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)

    def _save_hard_case(self, ocr_name, model_name, hl_name, conf, crop):
        """Save hard case: ocr_A_dl_B_l/h_conf_<hash>.png"""
        os.makedirs(FEEDBACK_HARD_DIR, exist_ok=True)
        hl_tag = 'h' if hl_name == 'highlighted' else 'l'
        h = _img_hash(crop)
        fname = f'ocr_{ocr_name}_dl_{model_name}_{hl_tag}_{conf:.2f}_{h}.png'
        path = os.path.join(FEEDBACK_HARD_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)


# ── Module-level API (for hud_poller compatibility) ──

_instance = None

def load_model(device):
    """Init WeaponDetector. Returns the instance (used as 'model' by poller)."""
    global _instance
    _instance = WeaponDetector(device)
    return _instance

def classify_slot(model_or_instance, crop, device, slot_id=None):
    """Poller calls this. model_or_instance is the WeaponDetector."""
    inst = model_or_instance
    if slot_id is None:
        slot_id = 1
    return inst.classify_slot(crop, slot_id)

def update_ocr_cache():
    """Called by poller while tab is open."""
    if _instance:
        _instance.update_ocr_cache()

def lock_ocr_gt():
    """Called by poller when tab closes."""
    if _instance:
        _instance.lock_ocr_gt()

def invalidate_gt(reason=''):
    """Called when weapon state may have changed."""
    if _instance:
        _instance.invalidate_gt(reason)


# ── Standalone main ──

def main():
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'Device: {device}')
    detector = WeaponDetector(device)
    print('Weapon detector ready (model + OCR).\n')

    prev_state = {1: None, 2: None}
    hz = 5

    while True:
        for slot_id in [1, 2]:
            crop = win32_cap(SLOT_RECTS[slot_id])
            gun_name, hl_name = detector.classify_slot(crop, slot_id)
            state = (gun_name, hl_name)

            if state != prev_state[slot_id]:
                prev_state[slot_id] = state
                slot_label = 'main' if slot_id == 1 else 'sub'
                if gun_name:
                    print(f'[slot {slot_id} {slot_label}] {gun_name}  ({hl_name})')
                else:
                    print(f'[slot {slot_id} {slot_label}] (empty)')

        time.sleep(1.0 / hz)


if __name__ == '__main__':
    main()
