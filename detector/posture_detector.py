"""Real-time posture detector.

Captures posture icon region from screen, classifies standing/crouching/prone.
Prints only when state changes.
"""
import os
import sys
import time

import cv2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import POSTURE
from detector.cropper import win32_cap
from detector.utils import load_model as _load, classify_single
from dl_models.icon_layout import POSTURE_CLASSES

# Screen rect: (y, x, h, w) for win32_cap
SLOT_RECT = (POSTURE['y1'], POSTURE['x1'],
             POSTURE['y2'] - POSTURE['y1'],
             POSTURE['x2'] - POSTURE['x1'])

MODEL_PATH = os.path.join(os.path.dirname(__file__), 'posture.pth.tar')
HEAD_SIZES = {'posture': len(POSTURE_CLASSES) + 1}


def load_model(device):
    return _load(MODEL_PATH, HEAD_SIZES, device)


def classify(model, crop, device):
    return classify_single(model, crop, device, 'posture', POSTURE_CLASSES)


def main():
    import torch
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'Device: {device}')
    model = load_model(device)
    print(f'Model loaded. Classes: {POSTURE_CLASSES}')
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
            print(f'[posture] {label}')

            fname = f'{debug_idx:04d}_posture_{name or "none"}.png'
            cv2.imwrite(os.path.join(debug_dir, fname), crop)
            debug_idx += 1

        time.sleep(1.0 / hz)


if __name__ == '__main__':
    main()
