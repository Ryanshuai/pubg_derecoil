"""Tab type detector — is inventory open?

Fast pixel counting on the "Type" text region.
DL model used as cross-check (optional).
"""
import os

import cv2
import numpy as np
import torch

from config import TAB_PIXEL_THRESH, TAB_COUNT_MIN, TAB_COUNT_MAX
from detector.utils import load_model as _load, crop_to_tensor_4ch

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'tab_detect.pth.tar')
HEAD_SIZES = {'tab_open': 2}


class TabTypeDetector:
    """Pixel-based tab open/close detection."""

    def __init__(self, device=None):
        self.device = device
        self.model = None
        if device is not None:
            self.model = _load(MODEL_PATH, HEAD_SIZES, device, in_channels=4)

    def classify(self, crops):
        """Check if Type text is currently visible.

        Returns True if tab is open (Type visible), False if closed.
        Used for calibration — directly reflects screen state.
        """
        crop = crops.get('type') if isinstance(crops, dict) else crops
        if crop is None:
            return False

        gray = np.max(crop, axis=2)
        bright_count = int((gray > TAB_PIXEL_THRESH).sum())
        return TAB_COUNT_MIN < bright_count < TAB_COUNT_MAX
