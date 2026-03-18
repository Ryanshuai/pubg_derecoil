"""Real-time weapon HUD detector.

Captures two weapon slots from screen, classifies gun name + highlighted status.
Prints only when state changes.
"""
import os
import sys
import time

import cv2
import numpy as np
import torch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import WEAPON_HUD_1, WEAPON_HUD_2, MODEL_CLASSES
from detector.cropper import win32_cap
from dl_models.train import MultiHeadMobileNet

WEAPON_CLASSES = MODEL_CLASSES['gun_name']
HL_NAMES = {0: '', 1: 'highlighted', 2: 'non-highlighted'}

# Icon screen coordinates: (y, x, h, w) for win32_cap
# Tight 53px crop at icon position within each slot
ICON_H = 53

def _icon_rect(hud):
    w = hud['x2'] - hud['x1']
    y = hud['y1'] + hud['icon_offset_y']
    return (y, hud['x1'], ICON_H, w)

SLOT_RECTS = {
    1: _icon_rect(WEAPON_HUD_1),  # main (bottom)
    2: _icon_rect(WEAPON_HUD_2),  # secondary (top)
}

MODEL_PATH = os.path.join(os.path.dirname(__file__), 'gun_name.pth.tar')
HEAD_SIZES = {'gun_name': len(WEAPON_CLASSES) + 1, 'highlighted': 3}


def load_model(device):
    model = MultiHeadMobileNet(HEAD_SIZES, in_channels=4).to(device)
    model.load_state_dict(torch.load(MODEL_PATH, map_location=device, weights_only=True))
    model.eval()
    return model


def crop_to_tensor(crop, device):
    """BGR uint8 (H,W,3) -> (1,4,H,W) float32 tensor (BGR + dewhite)."""
    # Dewhite: subtract blurred background to isolate icon signal
    bg_est = cv2.GaussianBlur(crop.astype(np.float32), (31, 31), 10)
    signal = np.clip((crop.astype(np.float32) - bg_est) * 2, 0, 255)
    dewhite = cv2.cvtColor(signal.astype(np.uint8), cv2.COLOR_BGR2GRAY)
    bgrd = np.dstack([crop, dewhite])  # (H, W, 4)
    t = torch.from_numpy(bgrd.transpose(2, 0, 1).astype(np.float32) / 255.0)
    return t.unsqueeze(0).to(device)


def classify_slot(model, crop, device):
    """Classify a single slot crop. Returns (gun_name_str, highlighted_str)."""
    tensor = crop_to_tensor(crop, device)
    with torch.no_grad():
        out = model(tensor)
    gun_id = out['gun_name'].argmax(1).item()
    hl_id = out['highlighted'].argmax(1).item()
    gun_name = WEAPON_CLASSES[gun_id - 1] if gun_id > 0 else ''
    hl_name = HL_NAMES[hl_id]
    return gun_name, hl_name


def main():
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'Device: {device}')
    model = load_model(device)
    print('Model loaded. Detecting...\n')

    prev_state = {1: None, 2: None}  # None so first detection always prints
    hz = 5  # detection frequency

    # Debug: save screenshot on every state change
    debug_dir = 'temp_debug'
    os.makedirs(debug_dir, exist_ok=True)
    debug_idx = 0

    while True:
        for slot_id in [1, 2]:
            crop = win32_cap(SLOT_RECTS[slot_id])
            gun_name, hl_name = classify_slot(model, crop, device)
            state = (gun_name, hl_name)

            if state != prev_state[slot_id]:
                prev_state[slot_id] = state
                slot_label = 'main' if slot_id == 1 else 'sub'
                if gun_name:
                    print(f'[slot {slot_id} {slot_label}] {gun_name}  ({hl_name})')
                else:
                    print(f'[slot {slot_id} {slot_label}] (empty)')

                # Save debug screenshot on every change
                label = gun_name if gun_name else 'empty'
                fname = f'{debug_idx:04d}_slot{slot_id}_{label}_{hl_name}.png'
                cv2.imwrite(os.path.join(debug_dir, fname), crop)
                debug_idx += 1

        time.sleep(1.0 / hz)


if __name__ == '__main__':
    main()
