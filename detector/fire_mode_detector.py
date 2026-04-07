"""Fire mode detector — DL model + RF structural verification.

RF is more reliable; used as output when both have a result.
Mismatch and hard cases saved for retraining.
"""
import os
import sys
import pickle

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import FIRE_MODE, HARD_CASE_CONF
from detector.cropper import win32_cap
from detector.utils import load_model as _load, crop_to_tensor_4ch, img_hash as _img_hash
from dl_models.icon_layout import FIRE_MODE_CLASSES

_logger = logger.bind(detector='fire_mode')

# Screen rect (y, x, h, w)
SLOT_RECT = (FIRE_MODE['y1'], FIRE_MODE['x1'],
             FIRE_MODE['y2'] - FIRE_MODE['y1'],
             FIRE_MODE['x2'] - FIRE_MODE['x1'])

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'fire_mode.pth.tar')
HEAD_SIZES = {'fire_mode': len(FIRE_MODE_CLASSES) + 1}

FEEDBACK_BASE = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'fire_mode')
FEEDBACK_MISMATCH_DIR = os.path.join(FEEDBACK_BASE, 'rf_dl_mismatch')
FEEDBACK_HARD_DIR = os.path.join(FEEDBACK_BASE, 'hard_case')
FEEDBACK_HIGH_DIR = os.path.join(FEEDBACK_BASE, 'high_collect')

# RF structural classifier
_RF_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'fire_mode_structural_rf.pkl')
with open(_RF_PATH, 'rb') as _f:
    _rf_model = pickle.load(_f)


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
    return _rf_model.predict([_extract_features(gray)])[0]


class FireModeDetector:

    def __init__(self, device, state):
        self.device = device
        self.state = state
        self.model = _load(MODEL_PATH, HEAD_SIZES, device, in_channels=4)

    def classify(self, crop):
        t = crop_to_tensor_4ch(crop, self.device)
        with torch.no_grad():
            out = self.model(t)

        logits = out['fire_mode'][0]
        probs = F.softmax(logits, dim=0)
        conf = probs.max().item()
        idx = probs.argmax().item()
        model_name = FIRE_MODE_CLASSES[idx - 1] if idx > 0 else 'bg'

        rf_name = _structural_classify(crop)

        # Feedback — always save "high" (rare class, need more data)
        if rf_name == 'high' or model_name == 'high':
            self._save_feedback(FEEDBACK_HIGH_DIR, 'high_collect',
                                rf_name, model_name, conf, crop, probs)
        elif rf_name != model_name and rf_name != 'bg' and model_name != 'bg':
            self._save_feedback(FEEDBACK_MISMATCH_DIR, 'mismatch',
                                rf_name, model_name, conf, crop, probs)
            _logger.info(f'MISMATCH | rf={rf_name} dl={model_name} conf={conf:.3f}')
        elif model_name != 'bg' and HARD_CASE_CONF[0] < conf < HARD_CASE_CONF[1]:
            self._save_feedback(FEEDBACK_HARD_DIR, 'hard_case',
                                rf_name, model_name, conf, crop, probs)

        return rf_name if (rf_name and rf_name != 'bg') else model_name

    def query(self, **_):
        """Capture fire mode region, classify, update state."""
        crop = win32_cap(SLOT_RECT)
        fm = self.classify(crop)
        if fm:
            self.state.set_fire_mode(fm)

    def _save_feedback(self, out_dir, reason, rf_name, model_name, conf, crop, probs):
        os.makedirs(out_dir, exist_ok=True)
        h = _img_hash(crop)
        fname = f'rf_{rf_name}_dl_{model_name}_{conf:.2f}_{h}.png'
        path = os.path.join(out_dir, fname)
        if os.path.exists(path):
            return
        cv2.imwrite(path, crop)

        from datetime import datetime
        top_k = torch.topk(probs, min(5, len(probs)))
        top_str = ', '.join(
            f'{FIRE_MODE_CLASSES[i.item()-1] if i.item()>0 else "bg"}={p.item():.3f}'
            for p, i in zip(top_k.values, top_k.indices))
        with open(os.path.join(out_dir, 'feedback.log'), 'a', encoding='utf-8') as f:
            f.write(f'{datetime.now().isoformat()} | {reason} | '
                    f'rf={rf_name} dl={model_name} conf={conf:.3f} | '
                    f'top5=[{top_str}] | {fname}\n')
