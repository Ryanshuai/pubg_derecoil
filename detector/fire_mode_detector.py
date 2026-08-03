"""Fire mode detector — DL model + RF structural verification.

RF is more reliable; used as output when both have a result.
"""
import os
import pickle

import cv2
import numpy as np
import torch
import torch.nn.functional as F

from detector.utils import load_model as _load, crop_to_tensor_4ch
from dl_models.icon_layout import FIRE_MODE_CLASSES

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'fire_mode.pth.tar')
HEAD_SIZES = {'fire_mode': len(FIRE_MODE_CLASSES) + 1}

_RF_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'fire_mode_structural_rf.pkl')
_rf_model = None


def _rf():
    """Load the structural forest on first use.

    Unpickling at import time meant `import detector.fire_mode_detector` — or
    anything that transitively imports it — died outright if the .pkl was
    missing, which is a strange way for an unrelated module to fail.
    """
    global _rf_model
    if _rf_model is None:
        with open(_RF_PATH, 'rb') as f:
            _rf_model = pickle.load(f)
    return _rf_model


def _extract_features(gray):
    h, w = gray.shape
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, -5)
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    big_comp = sum(1 for i in range(1, n_labels) if stats[i, cv2.CC_STAT_AREA] > 20)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        c = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(c)
        x, y, bw, bh = cv2.boundingRect(c)
        extent = area / max(bw * bh, 1)
        aspect = bw / max(bh, 1)
    else:
        area, extent, aspect = 0, 0, 0

    mean_b, std_b = gray.mean(), gray.std()
    bar_h = h // 5
    bars = [gray[i * bar_h:(i + 1) * bar_h, w // 4:3 * w // 4].mean() for i in range(5)]
    bar_range = max(bars) - min(bars)
    bright_bars = sum(1 for b in bars if b > (min(bars) + bar_range * 0.5)) if bar_range > 10 else 0

    return [big_comp, area, extent, aspect, mean_b, std_b, bar_range, bright_bars]


def _structural_classify(crop):
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return _rf().predict([_extract_features(gray)])[0]


class FireModeDetector:

    def __init__(self, device):
        self.device = device
        self.model = _load(MODEL_PATH, HEAD_SIZES, device, in_channels=4)

    def classify(self, crops):
        """Classify fire mode from crop dict. Returns mode string or None."""
        crop = crops.get('fire_mode') if isinstance(crops, dict) else crops
        if crop is None:
            return None

        # RF first, and only fall through to the net when it abstains. The
        # result was already "RF unless it says bg", so running the forward
        # pass up front just paid for a tensor upload and a GPU sync whose
        # answer was then thrown away on most frames.
        rf_name = _structural_classify(crop)
        if rf_name and rf_name != 'bg':
            return rf_name

        t = crop_to_tensor_4ch(crop, self.device)
        with torch.no_grad():
            out = self.model(t)
        idx = F.softmax(out['fire_mode'][0], dim=0).argmax().item()
        model_name = FIRE_MODE_CLASSES[idx - 1] if idx > 0 else 'bg'
        return model_name if model_name != 'bg' else None
