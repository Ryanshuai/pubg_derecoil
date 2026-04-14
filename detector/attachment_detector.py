"""Attachment detector — template matching on Tab inventory slots.

Classifies 10 attachment slots (5 per weapon) from 63×63 crops.
"""
import os

import cv2
import numpy as np

SLOT_NAMES = ['scope', 'muzzle', 'grip', 'magazine', 'stock']

TMPL_DIR = os.path.join(os.path.dirname(__file__), '..', 'training_data',
                        'pubg_assets', 'Item', 'Attachment')

TMPL_SIZE = 48
ALPHA_TH = 150
OFFSET_Y = 8
OFFSET_X = 8
MSE_EMPTY_TH = 300

SLOT_PREFIXES = {
    'scope':    ('Upper_', 'SideRail_'),
    'muzzle':   ('Muzzle_',),
    'grip':     ('Lower_', 'Vector_VerGrip'),
    'magazine': ('Magazine_', 'Medium_'),
    'stock':    ('Stock_',),
}


class AttachmentDetector:

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
            self._templates[name] = (tmpl_bgr[ys, xs], ys, xs)
            for slot_name, prefixes in SLOT_PREFIXES.items():
                if any(name.startswith(p) for p in prefixes):
                    self._slot_index.setdefault(slot_name, []).append(name)

    def _classify_slot(self, crop, slot_name):
        crop_f = crop.astype(np.float32)
        h, w = crop_f.shape[:2]
        candidates = self._slot_index.get(slot_name, self._templates.keys())
        best_name, best_mse = '', float('inf')
        for name in candidates:
            tmpl_vals, ys, xs = self._templates[name]
            cy, cx = ys + OFFSET_Y, xs + OFFSET_X
            min_se = np.full(len(ys), np.inf)
            for sy in (-1, 0, 1):
                for sx in (-1, 0, 1):
                    ny = np.clip(cy + sy, 0, h - 1)
                    nx = np.clip(cx + sx, 0, w - 1)
                    se = ((crop_f[ny, nx] - tmpl_vals) ** 2).sum(axis=1)
                    min_se = np.minimum(min_se, se)
            mse = min_se.mean() / 3
            if mse < best_mse:
                best_mse, best_name = mse, name
        if best_mse > MSE_EMPTY_TH:
            return ''
        return best_name

    def classify(self, crops):
        """Classify all attachment slots from crop dict.

        crops: {'att_1_scope': ndarray, 'att_1_muzzle': ndarray, ...}
        Returns: {1: {scope: name, ...}, 2: {scope: name, ...}}
        """
        result = {}
        for gun_id in [1, 2]:
            gun_result = {}
            for slot_name in SLOT_NAMES:
                key = f'att_{gun_id}_{slot_name}'
                crop = crops.get(key)
                if crop is None:
                    gun_result[slot_name] = ''
                else:
                    gun_result[slot_name] = self._classify_slot(crop, slot_name)
            result[gun_id] = gun_result
        return result
