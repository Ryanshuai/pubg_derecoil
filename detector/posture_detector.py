"""Posture detector — standing/crouching/prone from HUD icon.

Saves hard cases (low confidence) for later labeling.
Uses state-machine constraints: only valid transitions are accepted.
"""
import os
import sys
import time

import cv2
import torch
import torch.nn.functional as F

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import POSTURE
from detector.cropper import win32_cap
from detector.utils import load_model as _load, crop_to_tensor_4ch, img_hash as _img_hash
from dl_models.icon_layout import POSTURE_CLASSES

# Screen rect: (y, x, h, w) for win32_cap
SLOT_RECT = (POSTURE['y1'], POSTURE['x1'],
             POSTURE['y2'] - POSTURE['y1'],
             POSTURE['x2'] - POSTURE['x1'])

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'posture.pth.tar')
HEAD_SIZES = {'posture': len(POSTURE_CLASSES) + 1}

COLLECT_DIR = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'posture_collect')
COLLECT_MAX = 2000

# State machine: key → {current_posture: set of valid next postures}
_VALID_TRANSITIONS = {
    'c': {
        'standing':  {'crouching'},
        'crouching': {'standing'},
        'prone':     {'crouching'},
    },
    'z': {
        'standing':  {'prone'},
        'crouching': {'prone'},
        'prone':     {'standing'},
    },
}


class PostureDetector:

    def __init__(self, device, state):
        self.device = device
        self.state = state
        self.model = _load(MODEL_PATH, HEAD_SIZES, device, in_channels=4)
        self._collect_count = 0
        self._last_collect_time = 0

    def classify(self, crop):
        from dl_models.icon_merging import dewhite
        dw = dewhite(crop)
        if dw.mean() < 3:
            return '', 0.0
        t = crop_to_tensor_4ch(crop, self.device)
        with torch.no_grad():
            out = self.model(t)

        logits = out['posture'][0]
        probs = F.softmax(logits, dim=0)
        conf = probs.max().item()
        idx = probs.argmax().item()
        name = POSTURE_CLASSES[idx - 1] if idx > 0 else ''
        return name, conf

    def _save_collect(self, crop, label):
        """Save labeled screenshot for training data (up to COLLECT_MAX)."""
        if self._collect_count >= COLLECT_MAX:
            return
        os.makedirs(COLLECT_DIR, exist_ok=True)
        h = _img_hash(crop)
        fname = f'{label}_{h}.png'
        path = os.path.join(COLLECT_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)
            self._collect_count += 1
            if self._collect_count >= COLLECT_MAX:
                print(f'[posture] collected {COLLECT_MAX} samples, stopping', flush=True)

    def query(self, key_name=None):
        """Capture posture region, classify, update state.

        key_name: the trigger key ('c', 'z', 'right_down', etc.).
        If a state-machine constraint exists for this key, only valid
        transitions are accepted; violations are saved for review.
        right_down (ADS): no posture change, save crop labeled with current
        posture as ground truth for training data.
        """
        crop = win32_cap(SLOT_RECT)
        pos, conf = self.classify(crop)
        if not pos:
            return

        # Save hard cases (conf 0.3-0.6) for review
        if 0.3 < conf < 0.6:
            self._save_collect(crop, pos)

        cur = self.state.posture
        valid = _VALID_TRANSITIONS.get(key_name, {}).get(cur)

        if valid is not None and pos not in valid:
            self._save_collect(crop, f'{cur}_to_{pos}_key_{key_name}')
            # Force to the only valid target
            if len(valid) == 1:
                pos = next(iter(valid))
            else:
                return
            self._save_collect(crop, pos)
        elif key_name not in _VALID_TRANSITIONS:
            # No posture-change key (e.g. right_down / ADS) →
            # state machine posture is ground truth, only save once
            self._save_collect(crop, cur)
        else:
            # Valid transition from c/z
            self._save_collect(crop, pos)

        self.state.set_posture(pos)

    def collect_tick(self):
        """Call periodically (e.g. from aim loop). Saves posture crop every 0.5s.
        Uses state-machine posture (more reliable) as label.
        Always saves hard cases (conf 0.3-0.6)."""
        now = time.monotonic()
        if now - self._last_collect_time < 0.5:
            return
        self._last_collect_time = now
        crop = win32_cap(SLOT_RECT)
        _, conf = self.classify(crop)
        label = self.state.posture
        if label and 0.3 < conf < 0.6:
            self._save_collect(crop, label)
