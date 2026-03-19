"""Tab (inventory) open/close detector.

Model-based classification + pixel counting cross-check.
When they disagree, saves crop for feedback. Returns model result.
"""
import os
import sys
import time
from datetime import datetime

import cv2
import numpy as np
import torch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import IN_TAB
from detector.cropper import win32_cap
from detector.utils import load_model as _load, crop_to_tensor_4ch

# Screen rect: (y, x, h, w) for win32_cap
SLOT_RECT = (IN_TAB['y1'], IN_TAB['x1'],
             IN_TAB['y2'] - IN_TAB['y1'],
             IN_TAB['x2'] - IN_TAB['x1'])

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'tab_detect.pth.tar')
HEAD_SIZES = {'tab_open': 2}

# Pixel counting params
PIXEL_THRESHOLD = 200
COUNT_MIN = 150
COUNT_MAX = 400

FEEDBACK_DIR = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'tab')
_feedback_idx = 0


def load_model(device):
    return _load(MODEL_PATH, HEAD_SIZES, device, in_channels=4)


def classify(model, crop, device):
    """Classify whether Tab is open. Cross-checks with pixel counting.
    Saves crop when model and pixels disagree. Returns model result."""
    global _feedback_idx

    # Model prediction
    t = crop_to_tensor_4ch(crop, device)
    with torch.no_grad():
        out = model(t)
    model_open = out['tab_open'].argmax(1).item() == 1

    # Pixel counting cross-check
    gray = np.max(crop, axis=2)
    bright_count = int((gray > PIXEL_THRESHOLD).sum())
    pixels_open = COUNT_MIN < bright_count < COUNT_MAX

    # Model says open but pixels say closed → model may be wrong, save feedback
    if model_open and not pixels_open:
        os.makedirs(FEEDBACK_DIR, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        m = 'open' if model_open else 'closed'
        p = 'open' if pixels_open else 'closed'
        fname = f'{_feedback_idx:04d}_{ts}_tab_model={m}_pixels={p}_count={bright_count}.png'
        cv2.imwrite(os.path.join(FEEDBACK_DIR, fname), crop)
        _feedback_idx += 1
        print(f'[tab feedback] model={m}, pixels={p}, count={bright_count}')

    return model_open


def main():
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'Device: {device}')
    model = load_model(device)
    print('Model loaded. Detecting...\n')

    prev = None
    hz = 5

    while True:
        crop = win32_cap(SLOT_RECT)
        tab_open = classify(model, crop, device)

        if tab_open != prev:
            prev = tab_open
            print(f'[tab] {"OPEN" if tab_open else "closed"}')

        time.sleep(1.0 / hz)


if __name__ == '__main__':
    main()
