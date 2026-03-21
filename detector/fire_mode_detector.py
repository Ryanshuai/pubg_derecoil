"""Real-time fire mode detector — DL model + structural verification.

Two signal sources:
  - Model: MobileNetV3, classifies 7 fire modes from HUD icon (real-time, may confuse similar icons)
  - Structural: connected-components + contour-area algorithm (3 groups, very reliable)

Priority: structural verification > model prediction.
Feedback: structural/model mismatch → save crop for retraining.
"""
import logging
import os
import sys
import time

import cv2
import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import FIRE_MODE, HARD_CASE_CONF
from detector.cropper import win32_cap
from detector.utils import load_model as _load, crop_to_tensor_4ch, img_hash as _img_hash
from dl_models.icon_layout import FIRE_MODE_CLASSES

# ── Screen rect ──

SLOT_RECT = (FIRE_MODE['y1'], FIRE_MODE['x1'],
             FIRE_MODE['y2'] - FIRE_MODE['y1'],
             FIRE_MODE['x2'] - FIRE_MODE['x1'])

# ── Model ──

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'fire_mode.pth.tar')
HEAD_SIZES = {'fire_mode': len(FIRE_MODE_CLASSES) + 1}

# ── Structural classifier: DL class → structural group ──

STRUCTURAL_GROUPS = {
    'single':             'full',
    'burst2':             'full',
    'burst3':             'full',
    'full':               'full',
    'high':               'full',
    'single_bot_sniper':  'single_bot_sniper',
    'single_bot_shotgun': 'single_bot_shotgun',
    'single_bot_smook':   'single_bot_smook',
}

# ── Feedback ──

FEEDBACK_BASE = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'fire_mode')
FEEDBACK_MISMATCH_DIR = os.path.join(FEEDBACK_BASE, 'structural_mismatch')
FEEDBACK_HARD_DIR = os.path.join(FEEDBACK_BASE, 'hard_case')
FEEDBACK_UNKNOWN_DIR = os.path.join(FEEDBACK_BASE, 'unknown')

# ── Logger ──

os.makedirs(FEEDBACK_BASE, exist_ok=True)
_logger = logging.getLogger('fire_mode_detector')
_logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(os.path.join(FEEDBACK_BASE, 'fire_mode_detector.log'), encoding='utf-8')
_fh.setFormatter(logging.Formatter('%(asctime)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
_logger.addHandler(_fh)


# ── Structural classifier ──

def structural_classify(crop):
    """Classify fire mode icon by structure (3-sigma thresholds from confirmed data).

    Returns one of:
      'full'               — bar pattern (components >= 8, from 3-sigma lower bound of 10.4 +/- 0.9)
      'single_bot_smook' — smoke launcher (area >= 150, extent >= 0.80)
      'single_bot_sniper'  — sniper bullet (components <= 4, area <= 76)
      'unknown'            — borderline, doesn't fit confirmed distributions
      'bg'                 — background / unrecognizable

    Thresholds derived from confirmed data (3-sigma):
      full:   components 10.4 +/- 0.9  -> >= 8 confident
      sniper: components 2.2 +/- 0.5, area 57.8 +/- 6.0  -> comp <= 4 AND area <= 76
      smoke:  area 178.0 +/- 0, extent 0.852 +/- 0  -> area >= 150 AND extent >= 0.80
    """
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop

    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, -5)
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    big_components = sum(
        1 for i in range(1, n_labels) if stats[i, cv2.CC_STAT_AREA] > 20)

    # Bar pattern: full auto — 3-sigma lower bound = 7.8, use >= 8
    if big_components >= 8:
        return 'full'

    # Bullet-type: need contour analysis
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return 'bg'

    c = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(c)
    x, y, bw, bh = cv2.boundingRect(c)
    extent = area / max(bw * bh, 1)

    # Smoke launcher — area 178 +/- 0, extent 0.852 +/- 0, use generous margin
    if area >= 150 and extent >= 0.80:
        return 'single_bot_smook'

    # Sniper bullet — components 3-sigma upper = 3.8, area 3-sigma upper = 75.9
    if big_components <= 4 and area <= 76:
        return 'single_bot_sniper'

    # Borderline: components 5-7, or unusual area/extent → unknown
    if area >= 20:
        return 'unknown'

    return 'bg'


# ── Detector class ──

class FireModeDetector:
    """Fire mode detector: DL model + structural verification with feedback."""

    def __init__(self, device):
        self.device = device
        self.model = _load(MODEL_PATH, HEAD_SIZES, device, in_channels=4)

    def classify(self, crop):
        """Classify fire mode from HUD crop.

        Returns fire mode name (structural result preferred over model).
        Saves feedback on structural/model mismatch or hard cases.
        """
        # DL model
        t = crop_to_tensor_4ch(crop, self.device)
        with torch.no_grad():
            out = self.model(t)

        logits = out['fire_mode'][0]
        probs = F.softmax(logits, dim=0)
        conf = probs.max().item()
        idx = probs.argmax().item()
        model_name = FIRE_MODE_CLASSES[idx - 1] if idx > 0 else 'bg'

        # Structural verification
        struct_name = structural_classify(crop)

        # Compare: map model class to structural group
        model_group = STRUCTURAL_GROUPS.get(model_name, '')

        if struct_name == 'unknown':
            self._save_unknown(model_name, conf, crop, probs)
            _logger.info(f'UNKNOWN | dl={model_name} conf={conf:.3f}')
        elif struct_name != 'bg' and model_group and struct_name != model_group:
            self._save_mismatch(struct_name, model_name, conf, crop, probs)
            _logger.info(f'MISMATCH | structural={struct_name} model={model_name} '
                         f'conf={conf:.3f}')
        elif model_name != 'bg' and HARD_CASE_CONF[0] < conf < HARD_CASE_CONF[1]:
            self._save_hard_case(struct_name, model_name, conf, crop, probs)

        # Output: prefer structural result (unknown → fall back to model)
        if struct_name not in ('unknown', 'bg'):
            return struct_name
        return model_name

    # ── Feedback save ──

    def _save_unknown(self, model_name, conf, crop, probs):
        os.makedirs(FEEDBACK_UNKNOWN_DIR, exist_ok=True)
        h = _img_hash(crop)
        dl_tag = model_name if model_name else 'bg'
        fname = f'dl_{dl_tag}_{conf:.2f}_{h}.png'
        path = os.path.join(FEEDBACK_UNKNOWN_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)
            self._append_log(FEEDBACK_UNKNOWN_DIR, fname, 'unknown',
                             'unknown', model_name, conf, probs)

    def _save_mismatch(self, struct_name, model_name, conf, crop, probs):
        os.makedirs(FEEDBACK_MISMATCH_DIR, exist_ok=True)
        h = _img_hash(crop)
        fname = f'struct_{struct_name}_dl_{model_name}_{conf:.2f}_{h}.png'
        path = os.path.join(FEEDBACK_MISMATCH_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)
            self._append_log(FEEDBACK_MISMATCH_DIR, fname, 'mismatch',
                             struct_name, model_name, conf, probs)

    def _save_hard_case(self, struct_name, model_name, conf, crop, probs):
        os.makedirs(FEEDBACK_HARD_DIR, exist_ok=True)
        h = _img_hash(crop)
        fname = f'struct_{struct_name}_dl_{model_name}_{conf:.2f}_{h}.png'
        path = os.path.join(FEEDBACK_HARD_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)
            self._append_log(FEEDBACK_HARD_DIR, fname, 'hard_case',
                             struct_name, model_name, conf, probs)

    def _append_log(self, log_dir, fname, reason, struct_name, model_name,
                    conf, probs):
        from datetime import datetime
        top_k = torch.topk(probs, min(5, len(probs)))
        top_items = []
        for prob, idx in zip(top_k.values, top_k.indices):
            name = FIRE_MODE_CLASSES[idx.item() - 1] if idx.item() > 0 else 'bg'
            top_items.append(f'{name}={prob.item():.3f}')

        line = (f'{datetime.now().isoformat()} | {reason} | '
                f'structural={struct_name} dl={model_name} conf={conf:.3f} | '
                f'top5=[{", ".join(top_items)}] | {fname}\n')
        with open(os.path.join(log_dir, 'feedback.log'), 'a', encoding='utf-8') as f:
            f.write(line)


# ── Module-level API (for hud_poller compatibility) ──

_instance = None


def load_model(device):
    """Init FireModeDetector. Returns the instance."""
    global _instance
    _instance = FireModeDetector(device)
    return _instance


def classify(model_or_instance, crop, device):
    """Poller calls this. model_or_instance is the FireModeDetector."""
    return model_or_instance.classify(crop)


# ── Standalone main ──

def main():
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'Device: {device}')
    detector = FireModeDetector(device)
    print(f'Fire mode detector ready (model + structural).')
    print(f'Classes: {FIRE_MODE_CLASSES}')
    print(f'Structural groups: full / single_bot_sniper / single_bot_smook\n')

    prev = None
    hz = 5

    while True:
        crop = win32_cap(SLOT_RECT)
        name = detector.classify(crop)

        if name != prev:
            prev = name
            print(f'[fire_mode] {name if name else "(none)"}')

        time.sleep(1.0 / hz)


if __name__ == '__main__':
    main()
