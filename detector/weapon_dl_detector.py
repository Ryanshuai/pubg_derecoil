"""Weapon DL classifier — real-time HUD watermark recognition.

Reads GT from GameState for feedback and fallback. No dependency on weapon_template.
"""
import os
import sys

import cv2
import torch
import torch.nn.functional as F
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import WEAPON_HUD_1, WEAPON_HUD_2, HARD_CASE_CONF
from detector.cropper import win32_cap
from detector.utils import load_model as _load, crop_to_tensor_4ch, img_hash as _img_hash
from dl_models.icon_layout import WEAPON_CLASSES

_logger = logger.bind(detector='weapon')

HL_NAMES = {0: '', 1: 'highlighted', 2: 'non-highlighted'}
ICON_H = 53


def _icon_rect(hud):
    w = hud['x2'] - hud['x1']
    y = hud['y1'] + hud['icon_offset_y']
    return (y, hud['x1'], ICON_H, w)


SLOT_RECTS = {1: _icon_rect(WEAPON_HUD_1), 2: _icon_rect(WEAPON_HUD_2)}

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'gun_name.pth.tar')
HEAD_SIZES = {'gun_name': len(WEAPON_CLASSES) + 1, 'highlighted': 3}

FEEDBACK_BASE = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'weapon')
FEEDBACK_GT_DIR = os.path.join(FEEDBACK_BASE, 'gt_mismatch')
FEEDBACK_HARD_DIR = os.path.join(FEEDBACK_BASE, 'hard_case')


class WeaponClassifier:

    def __init__(self, device, state):
        self.device = device
        self.state = state
        self.model = _load(MODEL_PATH, HEAD_SIZES, device, in_channels=4, hidden_dim=1024)

    def classify(self, crop, slot_id):
        """Returns (gun_name, hl_name). Uses GT from state as fallback."""
        tensor = crop_to_tensor_4ch(crop, self.device)
        with torch.no_grad():
            out = self.model(tensor)

        gun_probs = F.softmax(out['gun_name'][0], dim=0)
        gun_conf = gun_probs.max().item()
        gun_id = gun_probs.argmax().item()
        hl_id = out['highlighted'].argmax(1).item()

        model_name = WEAPON_CLASSES[gun_id - 1] if gun_id > 0 else ''
        hl_name = HL_NAMES[hl_id]

        # GT from state
        w = self.state.weapon_1 if slot_id == 1 else self.state.weapon_2
        gt = w.name if self.state.gt_valid else ''

        # Feedback: compare DL vs GT
        if model_name and self.state.gt_valid:
            if gt and gt != model_name:
                self._save_feedback(FEEDBACK_GT_DIR, 'gt_mismatch',
                                    gt, model_name, hl_name, gun_conf, crop, gun_probs, slot_id)
            elif gt and HARD_CASE_CONF[0] < gun_conf < HARD_CASE_CONF[1]:
                self._save_feedback(FEEDBACK_HARD_DIR, 'hard_case',
                                    gt, model_name, hl_name, gun_conf, crop, gun_probs, slot_id)

        # Output: GT fallback, then DL
        return (gt or model_name), hl_name

    def query(self, **_):
        """Capture both weapon slots. When GT not available, set state from DL."""
        names = {}
        for slot_id in [1, 2]:
            crop = win32_cap(SLOT_RECTS[slot_id])
            name, hl = self.classify(crop, slot_id)
            names[slot_id] = name
        if not self.state.gt_valid:
            changed = False
            for slot_id, name in names.items():
                w = self.state.weapon_1 if slot_id == 1 else self.state.weapon_2
                if name and name != w.name:
                    self.state.set_weapon(slot_id, name)
                    changed = True
            if changed:
                self.state.auto_select_active()

    def _save_feedback(self, out_dir, reason, gt, model_name, hl_name,
                       conf, crop, probs, slot_id):
        os.makedirs(out_dir, exist_ok=True)
        hl_tag = 'h' if hl_name == 'highlighted' else 'l'
        h = _img_hash(crop)
        fname = f'gt_{gt}_dl_{model_name}_{hl_tag}_{conf:.2f}_{h}.png'
        path = os.path.join(out_dir, fname)
        if os.path.exists(path):
            return
        cv2.imwrite(path, crop)

        from datetime import datetime
        top_k = torch.topk(probs, min(5, len(probs)))
        top_str = ', '.join(
            f'{WEAPON_CLASSES[i.item()-1] if i.item()>0 else "bg"}={p.item():.3f}'
            for p, i in zip(top_k.values, top_k.indices))
        with open(os.path.join(out_dir, 'feedback.log'), 'a', encoding='utf-8') as f:
            f.write(f'{datetime.now().isoformat()} | {reason} | '
                    f'slot={slot_id} hl={hl_name} | '
                    f'gt={gt} dl={model_name} conf={conf:.3f} | '
                    f'top5=[{top_str}] | {fname}\n')
