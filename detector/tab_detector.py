"""Real-time Tab (inventory) open/close detector.

Captures "Type" text region from screen, classifies whether Tab is open.
Prints only when state changes.
"""
import os
import sys
import time

import cv2
import numpy as np
import torch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import IN_TAB
from detector.cropper import win32_cap
from dl_models.train import MultiHeadMobileNet

# Screen rect: (y, x, h, w) for win32_cap
SLOT_RECT = (IN_TAB['y1'], IN_TAB['x1'],
             IN_TAB['y2'] - IN_TAB['y1'],
             IN_TAB['x2'] - IN_TAB['x1'])

MODEL_PATH = os.path.join(os.path.dirname(__file__), 'tab_detect.pth.tar')
HEAD_SIZES = {'tab_open': 2}


def load_model(device):
    model = MultiHeadMobileNet(HEAD_SIZES, in_channels=3, hidden_dim=128).to(device)
    model.load_state_dict(torch.load(MODEL_PATH, map_location=device, weights_only=True))
    model.eval()
    return model


def classify(model, crop, device):
    """Classify whether Tab is open. Returns True/False."""
    t = torch.from_numpy(
        crop.transpose(2, 0, 1).astype(np.float32) / 255.0
    ).unsqueeze(0).to(device)
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
