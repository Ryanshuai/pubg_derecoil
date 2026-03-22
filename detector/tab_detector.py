"""Tab (inventory) open/close detector.

Model prediction + pixel counting cross-check.
"""
import os
import sys

import cv2
import numpy as np
import torch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import IN_TAB
from detector.cropper import win32_cap
from detector.utils import load_model as _load, crop_to_tensor_4ch, img_hash as _img_hash

SLOT_RECT = (IN_TAB['y1'], IN_TAB['x1'],
             IN_TAB['y2'] - IN_TAB['y1'],
             IN_TAB['x2'] - IN_TAB['x1'])

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'tab_detect.pth.tar')
HEAD_SIZES = {'tab_open': 2}

PIXEL_THRESHOLD = 200
COUNT_MIN = 150
COUNT_MAX = 400

FEEDBACK_DIR = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'tab')


class TabDetector:

    def __init__(self, device, state):
        self.device = device
        self.state = state
        self.model = _load(MODEL_PATH, HEAD_SIZES, device, in_channels=4)

    def classify(self, crop):
        """Classify and update state. Returns True if Tab is open."""
        t = crop_to_tensor_4ch(crop, self.device)
        with torch.no_grad():
            out = self.model(t)
        model_open = out['tab_open'].argmax(1).item() == 1

        # Pixel cross-check — save mismatch for feedback
        gray = np.max(crop, axis=2)
        bright_count = int((gray > PIXEL_THRESHOLD).sum())
        pixels_open = COUNT_MIN < bright_count < COUNT_MAX

        if model_open and not pixels_open:
            os.makedirs(FEEDBACK_DIR, exist_ok=True)
            h = _img_hash(crop)
            path = os.path.join(FEEDBACK_DIR, f'gt_closed_dl_open_{h}.png')
            if not os.path.exists(path):
                cv2.imwrite(path, crop)

        self.state.tab_open = model_open
        if model_open:
            self.state.stop_recoil = True

        return model_open
