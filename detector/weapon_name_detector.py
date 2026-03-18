"""Weapon name detector via OCR (Tab inventory view).

Reads weapon name text from GUN_NAME_1 / GUN_NAME_2 regions using PP-OCR rec-only mode.
Returns weapon name string or '' if empty/not in Tab view.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import GUN_NAME_1, GUN_NAME_2, MODEL_CLASSES
from detector.cropper import win32_cap
from rapidocr_onnxruntime import RapidOCR

WEAPON_CLASSES = MODEL_CLASSES['gun_name']

# Screen rects: (y, x, h, w) for win32_cap
SLOT_RECTS = {
    1: (GUN_NAME_1['y1'], GUN_NAME_1['x1'],
        GUN_NAME_1['y2'] - GUN_NAME_1['y1'], GUN_NAME_1['x2'] - GUN_NAME_1['x1']),
    2: (GUN_NAME_2['y1'], GUN_NAME_2['x1'],
        GUN_NAME_2['y2'] - GUN_NAME_2['y1'], GUN_NAME_2['x2'] - GUN_NAME_2['x1']),
}

# Confidence threshold — below this treat as empty
CONF_THRESHOLD = 0.6


class WeaponNameDetector:
    """OCR-based weapon name reader for Tab inventory view."""

    def __init__(self):
        self.ocr = RapidOCR()
        # Warmup
        import numpy as np
        dummy = np.zeros((45, 250, 3), dtype=np.uint8)
        self.ocr(dummy, use_det=False, use_cls=False)

    def recognize(self, crop):
        """Recognize weapon name from a BGR crop.

        Returns (name, conf) where name is lowercase weapon class or ''.
        """
        result, _ = self.ocr(crop, use_det=False, use_cls=False)
        if not result:
            return '', 0.0

        text, conf = result[0]
        if conf < CONF_THRESHOLD or not text.strip():
            return '', conf

        # Match to known weapon classes (case-insensitive)
        text_lower = text.strip().lower()
        for cls in WEAPON_CLASSES:
            if cls == text_lower:
                return cls, conf

        # Fuzzy: check if OCR text is contained in or contains a class name
        for cls in WEAPON_CLASSES:
            if cls in text_lower or text_lower in cls:
                return cls, conf

        return text_lower, conf

    def detect_from_screen(self):
        """Capture and recognize both weapon name slots.

        Returns {1: (name, conf), 2: (name, conf)}.
        """
        results = {}
        for slot_id in [1, 2]:
            crop = win32_cap(SLOT_RECTS[slot_id])
            results[slot_id] = self.recognize(crop)
        return results


def main():
    import time
    detector = WeaponNameDetector()
    print('Weapon name detector ready. Detecting...\n')

    prev_state = {1: None, 2: None}
    hz = 5

    while True:
        results = detector.detect_from_screen()
        for slot_id in [1, 2]:
            name, conf = results[slot_id]
            if (name, conf) != prev_state[slot_id]:
                prev_state[slot_id] = (name, conf)
                slot_label = 'main' if slot_id == 1 else 'sub'
                if name:
                    print(f'[slot {slot_id} {slot_label}] {name}  (conf={conf:.3f})')
                else:
                    print(f'[slot {slot_id} {slot_label}] (empty)')
        time.sleep(1.0 / hz)


if __name__ == '__main__':
    main()
