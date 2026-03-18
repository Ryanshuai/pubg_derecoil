"""Real-time weapon attachment detector (Tab inventory view).

Captures 5 attachment slots per weapon from screen, classifies each.
Only runs when Tab is open. Prints on state change.

Inference constraint: each slot only considers its valid attachment subset,
taking argmax over allowed classes rather than all 56.
"""
import os
import sys
import time

import cv2
import numpy as np
import torch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import ATTACHMENT_SLOTS, LAPLACIAN_THRESHOLD
from detector.cropper import win32_cap
from dl_models.train import MultiHeadMobileNet
from dl_models.icon_layout import ATTACHMENT_CLASSES

# ── Slot → valid attachment mapping ──
# Each slot type can only hold certain attachments
SLOT_VALID = {
    'scope': [c for c in ATTACHMENT_CLASSES if c.startswith('Upper_') or c.startswith('SideRail_')],
    'muzzle': [c for c in ATTACHMENT_CLASSES if c.startswith('Muzzle_')],
    'grip': [c for c in ATTACHMENT_CLASSES if c.startswith('Lower_') or c == 'Vector_VerGrip'],
    'magazine': [c for c in ATTACHMENT_CLASSES if c.startswith('Magazine_') or c.startswith('Medium_')],
    'stock': [c for c in ATTACHMENT_CLASSES if c.startswith('Stock_')],
}

# Convert to index masks (label 0 = empty, 1..N = attachment classes)
SLOT_VALID_IDX = {}
for slot_name, valid_classes in SLOT_VALID.items():
    indices = [0]  # always allow empty
    for cls in valid_classes:
        if cls in ATTACHMENT_CLASSES:
            indices.append(ATTACHMENT_CLASSES.index(cls) + 1)
    SLOT_VALID_IDX[slot_name] = indices

SLOT_NAMES = ['scope', 'muzzle', 'grip', 'magazine', 'stock']

# Screen rects: (y, x, h, w) for win32_cap
def _slot_rect(x1, y1, x2, y2):
    return (y1, x1, y2 - y1, x2 - x1)

MODEL_PATH = os.path.join(os.path.dirname(__file__), 'weapon_attachment.pth.tar')
HEAD_SIZES = {'attachment': len(ATTACHMENT_CLASSES) + 1}


def load_model(device):
    model = MultiHeadMobileNet(HEAD_SIZES, in_channels=3, hidden_dim=512).to(device)
    model.load_state_dict(torch.load(MODEL_PATH, map_location=device, weights_only=True))
    model.eval()
    return model


def is_slot_empty(crop):
    """Check if slot is empty via Laplacian variance (low = empty)."""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    return lap_var < LAPLACIAN_THRESHOLD


def classify_slot(model, crop, slot_name, device):
    """Classify a single attachment slot crop.

    Returns attachment class name or '' if empty.
    Uses slot-constrained argmax: only considers valid attachments for this slot type.
    """
    if is_slot_empty(crop):
        return ''

    t = torch.from_numpy(
        crop.transpose(2, 0, 1).astype(np.float32) / 255.0
    ).unsqueeze(0).to(device)

    with torch.no_grad():
        out = model(t)

    logits = out['attachment'][0]  # (num_classes,)

    # Constrained argmax: only consider valid indices for this slot
    valid_idx = SLOT_VALID_IDX[slot_name]
    valid_logits = logits[valid_idx]
    best_local = valid_logits.argmax().item()
    best_global = valid_idx[best_local]

    if best_global == 0:
        return ''
    return ATTACHMENT_CLASSES[best_global - 1]


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
