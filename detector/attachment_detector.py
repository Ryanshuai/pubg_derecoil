"""Attachment detector — template matching + DL feedback.

Template matching (primary): 500→48 BGRA templates, fixed offset (8,8),
3x3 neighborhood BGR MSE with alpha>150 mask. ~14ms per slot.

DL model (feedback only): saves crops with DL predictions for retraining.
"""
import os
import sys

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from detector.utils import load_model as _load, crop_to_tensor
from dl_models.icon_layout import ATTACHMENT_CLASSES

_logger = logger.bind(detector='attachment')

SLOT_NAMES = ['scope', 'muzzle', 'grip', 'magazine', 'stock']

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'weapon_attachment.pth.tar')
HEAD_SIZES = {'attachment': len(ATTACHMENT_CLASSES) + 1}

TMPL_DIR = os.path.join(os.path.dirname(__file__), '..', 'training_data', 'pubg_assets', 'Item', 'Attachment')
FEEDBACK_DIR = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'attachment')

# Template matching parameters
TMPL_SIZE = 48        # 500 * 0.096
ALPHA_TH = 150        # only opaque pixels participate in MSE
OFFSET_Y = 8          # fixed offset in 63x63 crop
OFFSET_X = 8
STD_EMPTY_TH = 10     # std < 10 → empty slot (no icon)
MSE_EMPTY_TH = 300    # MSE > 300 → no confident match → empty


# Slot name → template name prefixes
SLOT_PREFIXES = {
    'scope':    ('Upper_', 'SideRail_'),
    'muzzle':   ('Muzzle_',),
    'grip':     ('Lower_', 'Vector_VerGrip'),
    'magazine':  ('Magazine_', 'Medium_'),
    'stock':    ('Stock_',),
}


class AttachmentTemplateMatcher:
    """Template matching for attachment icons using BGRA templates."""

    def __init__(self):
        self._templates = {}      # name → (tmpl_vals, ys, xs)
        self._slot_index = {}     # slot_name → [name, ...]
        self._load_templates()

    def _load_templates(self):
        if not os.path.isdir(TMPL_DIR):
            return
        for fname in os.listdir(TMPL_DIR):
            if not fname.endswith('.png'):
                continue
            img = cv2.imread(os.path.join(TMPL_DIR, fname), cv2.IMREAD_UNCHANGED)
            if img is None or img.shape[2] != 4:
                continue
            name = fname.replace('Item_Attach_Weapon_', '').replace('.png', '')

            resized = cv2.resize(img, (TMPL_SIZE, TMPL_SIZE), interpolation=cv2.INTER_AREA)
            mask = resized[:, :, 3] > ALPHA_TH
            if int(mask.sum()) < 30:
                continue

            tmpl_bgr = resized[:, :, :3].astype(np.float32)
            ys, xs = np.where(mask)
            tmpl_vals = tmpl_bgr[ys, xs]  # (N, 3)

            self._templates[name] = (tmpl_vals, ys, xs)

            # Build slot index
            for slot_name, prefixes in SLOT_PREFIXES.items():
                if any(name.startswith(p) for p in prefixes):
                    self._slot_index.setdefault(slot_name, []).append(name)

        if self._templates:
            counts = {s: len(ns) for s, ns in self._slot_index.items()}
            print(f'[AttachTmpl] {len(self._templates)} templates: {counts}')

    def classify(self, crop, slot_name=''):
        """Classify a 63x63 attachment crop. Returns (name, mse)."""
        crop_f = crop.astype(np.float32)
        h, w = crop_f.shape[:2]

        # Only compare against templates valid for this slot
        candidates = self._slot_index.get(slot_name, self._templates.keys())

        best_name = ''
        best_mse = float('inf')

        for name in candidates:
            tmpl_vals, ys, xs = self._templates[name]
            cy = ys + OFFSET_Y
            cx = xs + OFFSET_X

            # 3x3 neighborhood search: find min SE per pixel
            min_se = np.full(len(ys), np.inf)
            for sy_off in (-1, 0, 1):
                for sx_off in (-1, 0, 1):
                    ny = np.clip(cy + sy_off, 0, h - 1)
                    nx = np.clip(cx + sx_off, 0, w - 1)
                    crop_vals = crop_f[ny, nx]
                    se = ((crop_vals - tmpl_vals) ** 2).sum(axis=1)
                    min_se = np.minimum(min_se, se)

            mse = min_se.mean() / 3  # per channel
            if mse < best_mse:
                best_mse = mse
                best_name = name

        if best_mse > MSE_EMPTY_TH:
            return '', best_mse

        return best_name, best_mse


class AttachmentDetector:
    """Attachment detector: template matching + DL feedback."""

    def __init__(self, device, state):
        self.device = device
        self.state = state
        self._matcher = AttachmentTemplateMatcher()
        self._dl_model = _load(MODEL_PATH, HEAD_SIZES, device, hidden_dim=512)

    def classify_slot(self, crop, slot_name):
        """Returns attachment class name or '' if empty."""
        # Primary: template matching (slot-constrained)
        name, mse = self._matcher.classify(crop, slot_name)

        # Feedback: DL model prediction for comparison/retraining
        self._save_feedback(crop, name, mse)

        # Log with std for threshold tuning
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32)
        std = gray.std()
        _logger.info(f'{slot_name} | tmpl={name or "bg"} mse={mse:.0f} std={std:.1f}')
        return name

    @staticmethod
    def _short_name(name):
        """Shorten attachment class name for filenames."""
        if not name:
            return 'bg'
        for prefix in ('Upper_', 'Lower_', 'Muzzle_', 'Magazine_', 'Stock_', 'SideRail_', 'Medium_'):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        # Remove common suffixes
        name = name.replace('_C', '').replace('_setting', '').replace('Foregrip', 'FG').replace('Compensator', 'Comp').replace('Suppressor', 'Supp').replace('FlashHider', 'FH').replace('Extended', 'Ext').replace('QuickDraw', 'QD')
        return name[:8]

    def _save_feedback(self, crop, tmpl_name, tmpl_mse):
        """Save crop with both template and DL predictions for review."""
        os.makedirs(FEEDBACK_DIR, exist_ok=True)

        # DL prediction
        t = crop_to_tensor(crop, self.device)
        with torch.no_grad():
            out = self._dl_model(t)
        probs = F.softmax(out['attachment'][0], dim=0)
        conf = probs.max().item()
        idx = probs.argmax().item()
        dl_name = ATTACHMENT_CLASSES[idx - 1] if idx > 0 else ''

        t_tag = self._short_name(tmpl_name)
        d_tag = self._short_name(dl_name)
        if t_tag == d_tag:
            return
        fname = f'T_{t_tag}_m{tmpl_mse:.0f}_dl_{d_tag}_c{int(conf*100)}.png'
        path = os.path.join(FEEDBACK_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)

    CONFLICT_DIR = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'attachment_conflict')

    def _save_conflict_crop(self, gun_name, slot_name, detected, crop):
        """Save crop image when detector returns an attachment the weapon can't equip."""
        os.makedirs(self.CONFLICT_DIR, exist_ok=True)
        import time
        ts = time.strftime('%Y%m%d_%H%M%S')
        short = self._short_name(detected)
        fname = f'{ts}_{gun_name}_{slot_name}_{short}.png'
        cv2.imwrite(os.path.join(self.CONFLICT_DIR, fname), crop)
        _logger.warning(f'CONFLICT: {gun_name} cannot equip {slot_name}={detected}')

    def classify_gun(self, screen, gun_id):
        """Classify all 5 slots for a gun. Updates state directly."""
        from config import ATTACHMENT_SLOTS
        from detector.weapon_attachments import validate_attachments
        rects = ATTACHMENT_SLOTS[gun_id]
        w = self.state.weapon_1 if gun_id == 1 else self.state.weapon_2
        result = {}
        crops = {}
        for slot_name in SLOT_NAMES:
            x1, y1, x2, y2 = rects[slot_name]
            crop = screen[y1:y2, x1:x2].copy()
            crops[slot_name] = crop
            result[slot_name] = self.classify_slot(crop, slot_name)
        # Validate and save conflict crops
        filtered = validate_attachments(w.name, result)
        for k in ('muzzle', 'grip'):
            if result.get(k) and result[k] != filtered.get(k):
                self._save_conflict_crop(w.name, k, result[k], crops[k])
        self.state.set_attachments(gun_id, result)
