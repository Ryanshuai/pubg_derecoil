"""Real-time Tab (inventory) open/close detector.

Captures "Type" text region from screen, classifies whether Tab is open.
Prints only when state changes.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import IN_TAB
from detector.cropper import win32_cap
from detector.utils import load_model as _load, crop_to_tensor

import torch

# Screen rect: (y, x, h, w) for win32_cap
SLOT_RECT = (IN_TAB['y1'], IN_TAB['x1'],
             IN_TAB['y2'] - IN_TAB['y1'],
             IN_TAB['x2'] - IN_TAB['x1'])

MODEL_PATH = os.path.join(os.path.dirname(__file__), 'tab_detect.pth.tar')
HEAD_SIZES = {'tab_open': 2}


def load_model(device):
    return _load(MODEL_PATH, HEAD_SIZES, device)


def classify(model, crop, device):
    """Classify whether Tab is open. Returns True/False."""
    t = crop_to_tensor(crop, device)
    with torch.no_grad():
        out = model(t)
    return out['tab_open'].argmax(1).item() == 1


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
