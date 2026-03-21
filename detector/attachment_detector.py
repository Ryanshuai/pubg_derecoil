"""Attachment detector — classifies slot crops from Tab inventory view."""
import os
import sys

import cv2
import torch
import torch.nn.functional as F
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from detector.utils import load_model as _load, crop_to_tensor, img_hash as _img_hash
from dl_models.icon_layout import ATTACHMENT_CLASSES

_logger = logger.bind(detector='attachment')

SLOT_NAMES = ['scope', 'muzzle', 'grip', 'magazine', 'stock']

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'weapon_attachment.pth.tar')
HEAD_SIZES = {'attachment': len(ATTACHMENT_CLASSES) + 1}

FEEDBACK_DIR = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'attachment')
BRIGHT_THRESHOLD = 250


class AttachmentDetector:

    def __init__(self, device, state):
        self.device = device
        self.state = state
        self.model = _load(MODEL_PATH, HEAD_SIZES, device, hidden_dim=512)

    def classify_slot(self, crop, slot_name):
        """Returns attachment class name or '' if empty."""
        if crop.max() < BRIGHT_THRESHOLD:
            return ''

        t = crop_to_tensor(crop, self.device)
        with torch.no_grad():
            out = self.model(t)

        probs = F.softmax(out['attachment'][0], dim=0)
        conf = probs.max().item()
        idx = probs.argmax().item()
        name = ATTACHMENT_CLASSES[idx - 1] if idx > 0 else ''

        _logger.info(f'{slot_name} | {name or "empty"} conf={conf:.3f}')

        # Save for feedback
        os.makedirs(FEEDBACK_DIR, exist_ok=True)
        h = _img_hash(crop)
        tag = name or 'empty'
        path = os.path.join(FEEDBACK_DIR, f'{slot_name}_{tag}_{conf:.2f}_{h}.png')
        if not os.path.exists(path):
            cv2.imwrite(path, crop)

        return name

    def classify_gun(self, screen, gun_id):
        """Classify all 5 slots for a gun from fullscreen image.

        Returns dict {slot_name: attachment_class_name or ''}.
        """
        from config import ATTACHMENT_SLOTS
        rects = ATTACHMENT_SLOTS[gun_id]
        result = {}
        for slot_name in SLOT_NAMES:
            x1, y1, x2, y2 = rects[slot_name]
            crop = screen[y1:y2, x1:x2].copy()
            result[slot_name] = self.classify_slot(crop, slot_name)
        return result
