"""Real-time fire mode detector.

Captures fire mode icon region from screen, classifies current fire mode.
Prints only when state changes.
"""
import os
import sys
import time

import cv2
import numpy as np
import torch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import FIRE_MODE
from detector.cropper import win32_cap
from dl_models.train import MultiHeadMobileNet
from dl_models.icon_layout import FIRE_MODE_CLASSES

# Screen rect: (y, x, h, w) for win32_cap
SLOT_RECT = (FIRE_MODE['y1'], FIRE_MODE['x1'],
             FIRE_MODE['y2'] - FIRE_MODE['y1'],
             FIRE_MODE['x2'] - FIRE_MODE['x1'])

MODEL_PATH = os.path.join(os.path.dirname(__file__), 'fire_mode.pth.tar')
HEAD_SIZES = {'fire_mode': len(FIRE_MODE_CLASSES) + 1}


def load_model(device):
    model = MultiHeadMobileNet(HEAD_SIZES, in_channels=3, hidden_dim=128).to(device)
    model.load_state_dict(torch.load(MODEL_PATH, map_location=device, weights_only=True))
    model.eval()
    return model


def classify(model, crop, device):
    """Classify fire mode from a BGR crop. Returns class name or ''."""
    t = torch.from_numpy(
        crop.transpose(2, 0, 1).astype(np.float32) / 255.0
    ).unsqueeze(0).to(device)
    with torch.no_grad():
        out = model(t)
    idx = out['fire_mode'].argmax(1).item()
    return FIRE_MODE_CLASSES[idx - 1] if idx > 0 else ''


def main():
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'Device: {device}')
    model = load_model(device)
    print(f'Model loaded. Classes: {FIRE_MODE_CLASSES}')
    print('Detecting...\n')

    prev = None
    hz = 5

    debug_dir = 'temp_debug'
    os.makedirs(debug_dir, exist_ok=True)
    debug_idx = 0

    while True:
        crop = win32_cap(SLOT_RECT)
        name = classify(model, crop, device)

        if name != prev:
            prev = name
            label = name if name else '(none)'
            print(f'[fire_mode] {label}')

            fname = f'{debug_idx:04d}_fire_mode_{name or "none"}.png'
            cv2.imwrite(os.path.join(debug_dir, fname), crop)
            debug_idx += 1

        time.sleep(1.0 / hz)


if __name__ == '__main__':
    main()
