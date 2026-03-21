"""Real-time weapon attachment detector (Tab inventory view).

Captures 5 attachment slots per weapon from screen, classifies each.
Only runs when Tab is open. Prints on state change.
Hard case mining: saves crops with confidence 0.3~0.8 for later labeling.
"""
import os
import sys
import time
from datetime import datetime

import cv2
import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import ATTACHMENT_SLOTS, HARD_CASE_CONF
from detector.cropper import win32_cap
from detector.utils import load_model as _load, crop_to_tensor, img_hash as _img_hash
from dl_models.icon_layout import ATTACHMENT_CLASSES

SLOT_NAMES = ['scope', 'muzzle', 'grip', 'magazine', 'stock']

# Screen rects: (y, x, h, w) for win32_cap
def _slot_rect(x1, y1, x2, y2):
    return (y1, x1, y2 - y1, x2 - x1)

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'weapon_attachment.pth.tar')
HEAD_SIZES = {'attachment': len(ATTACHMENT_CLASSES) + 1}

# Hard case feedback
FEEDBACK_DIR = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'attachment')

def load_model(device):
    return _load(MODEL_PATH, HEAD_SIZES, device, hidden_dim=512)


BRIGHT_THRESHOLD = 250

def is_slot_empty(crop):
    """Check if slot is empty via max brightness (icon is white 255 on dark bg)."""
    return crop.max() < BRIGHT_THRESHOLD


def classify_slot(model, crop, slot_name, device):
    """Classify a single attachment slot crop.

    Returns attachment class name or '' if empty.
    Saves hard cases (confidence 0.3~0.8) for later labeling.
    """
    if is_slot_empty(crop):
        return ''

    t = crop_to_tensor(crop, device)

    with torch.no_grad():
        out = model(t)

    logits = out['attachment'][0]
    probs = F.softmax(logits, dim=0)
    conf = probs.max().item()
    idx = probs.argmax().item()
    name = ATTACHMENT_CLASSES[idx - 1] if idx > 0 else ''

    # Hard case: model is uncertain → save for labeling
    if HARD_CASE_CONF[0] < conf < HARD_CASE_CONF[1] and name:
        os.makedirs(FEEDBACK_DIR, exist_ok=True)
        h = _img_hash(crop)
        fname = f'{slot_name}_{name}_{h}.png'
        path = os.path.join(FEEDBACK_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)

    return name


def detect_all_slots(model, gun_id, device):
    """Detect all 5 attachment slots for a given gun.

    Returns dict {slot_name: attachment_class_name_or_empty}.
    """
    results = {}
    rects = ATTACHMENT_SLOTS[gun_id]
    for slot_name in SLOT_NAMES:
        x1, y1, x2, y2 = rects[slot_name]
        crop = win32_cap(_slot_rect(x1, y1, x2, y2))
        results[slot_name] = classify_slot(model, crop, slot_name, device)
    return results


def main():
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'Device: {device}')
    model = load_model(device)
    print(f'Model loaded. {len(ATTACHMENT_CLASSES)} attachment classes.')
    print(f'Slot constraints: {{{", ".join(f"{k}: {len(v)}" for k, v in SLOT_VALID.items())}}}')
    print('Detecting...\n')

    prev_state = {1: {}, 2: {}}
    hz = 2

    debug_dir = 'temp_debug'
    os.makedirs(debug_dir, exist_ok=True)
    debug_idx = 0

    while True:
        for gun_id in [1, 2]:
            results = detect_all_slots(model, gun_id, device)

            if results != prev_state[gun_id]:
                # Find what changed
                for slot_name in SLOT_NAMES:
                    new_val = results[slot_name]
                    old_val = prev_state[gun_id].get(slot_name, None)
                    if new_val != old_val:
                        label = new_val if new_val else 'empty'
                        print(f'[gun{gun_id} {slot_name:8s}] {label}')

                        # Save debug crop
                        x1, y1, x2, y2 = ATTACHMENT_SLOTS[gun_id][slot_name]
                        crop = win32_cap(_slot_rect(x1, y1, x2, y2))
                        fname = f'{debug_idx:04d}_gun{gun_id}_{slot_name}_{label}.png'
                        cv2.imwrite(os.path.join(debug_dir, fname), crop)
                        debug_idx += 1

                prev_state[gun_id] = results

        time.sleep(1.0 / hz)


if __name__ == '__main__':
    main()
