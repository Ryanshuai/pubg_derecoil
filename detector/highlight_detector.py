"""Highlight detector — CV-based active weapon slot detection.

Compares two weapon slot crops to determine which is highlighted (active).
Uses dewhite + template hypothesis test (4 hypotheses: white/red × hl/lo).

Returns: int (1 or 2) indicating which slot is highlighted, or 0 if unknown.
"""
import os

import cv2
import numpy as np

from config import ALPHA_HL, ALPHA_LO
from dl_models.icon_layout import WEAPON_ICON_MAP

ASSET_DIR = os.path.join(os.path.dirname(__file__), '..', 'training_data',
                         'pubg_assets', 'Item', 'Weapon', 'Main')
ICON_H = 53
_NAME_TO_FILE = {v: k for k, v in WEAPON_ICON_MAP.items()}

# How far _align re-crops the template before matching. 0 means "trust
# matchTemplate", which already slides the template over the whole crop; each
# extra step multiplies the matchTemplate calls by (2j+1)^2 x 2 sources — j=2
# is 50 calls per slot for j=0's 2.
#
# Measured on the 254 labelled pairs in training_data/highlight_eval
# (temp_debug/eval_highlight_jitter.py):
#
#     jitter=2   254/254   11.7 ms/pair
#     jitter=1   254/254    5.9 ms/pair
#     jitter=0   254/254    3.6 ms/pair
#
# Identical accuracy, so it is off by default. It is still wired all the way
# through — HighlightDetector(state, jitter=2), or raise this constant — for
# the case the eval set does not cover: a crop that clips the icon at its edge,
# where re-cropping the template is the only thing that can recover it. That
# set has no such case (errors_v4/ is empty), which is the only reason to keep
# the knob rather than delete the loop.
ALIGN_JITTER = 0


def _dewhite(img_bgr):
    bg = cv2.GaussianBlur(img_bgr.astype(np.float32), (31, 31), 10)
    signal = np.clip((img_bgr.astype(np.float32) - bg) * 2, 0, 255)
    return cv2.cvtColor(signal.astype(np.uint8), cv2.COLOR_BGR2GRAY)


def _combined_max(img_bgr):
    dw = _dewhite(img_bgr).astype(np.float32)
    r = img_bgr[:, :, 2].astype(np.float32)
    g = img_bgr[:, :, 1].astype(np.float32)
    return max(float(np.percentile(dw, 95)), float(np.percentile(r - g, 90)))


class _TemplateCache:
    def __init__(self):
        self._cache = {}

    def get(self, weapon_name):
        if weapon_name in self._cache:
            return self._cache[weapon_name]
        fname = _NAME_TO_FILE.get(weapon_name)
        if not fname:
            self._cache[weapon_name] = (None, None)
            return None, None
        path = os.path.join(ASSET_DIR, fname)
        if not os.path.exists(path):
            self._cache[weapon_name] = (None, None)
            return None, None
        bgra = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if bgra is None or bgra.shape[2] != 4:
            self._cache[weapon_name] = (None, None)
            return None, None
        h, w = bgra.shape[:2]
        nw = int(w * ICON_H / h)
        bgra = cv2.resize(bgra, (nw, ICON_H), interpolation=cv2.INTER_NEAREST)
        icon_bgr = bgra[:, :, :3].astype(np.float32)
        icon_alpha = bgra[:, :, 3].astype(np.float32) / 255.0
        self._cache[weapon_name] = (icon_bgr, icon_alpha)
        return icon_bgr, icon_alpha


def _align(crop, tmpl_alpha, jitter=ALIGN_JITTER):
    tmpl_u8 = (tmpl_alpha * 255).astype(np.uint8)
    dw = _dewhite(crop)
    r_ch = crop[:, :, 2]
    th, tw = tmpl_u8.shape[:2]
    ch, cw = crop.shape[:2]
    best_score, best_loc = -1.0, (cw - tw, 0)
    for dy in range(-jitter, jitter + 1):
        for dx in range(-jitter, jitter + 1):
            ty1, ty2 = max(0, -dy), th - max(0, dy)
            tx1, tx2 = max(0, -dx), tw - max(0, dx)
            if ty2 <= ty1 or tx2 <= tx1:
                continue
            sub = tmpl_u8[ty1:ty2, tx1:tx2]
            if sub.shape[0] > ch or sub.shape[1] > cw:
                continue
            for src in (dw, r_ch):
                res = cv2.matchTemplate(src, sub, cv2.TM_CCOEFF_NORMED)
                _, s, _, loc = cv2.minMaxLoc(res)
                rx = loc[0] - tx1 + dx
                ry = loc[1] - ty1 + dy
                if s > best_score:
                    best_score, best_loc = s, (rx, ry)
    return best_loc


def _place(crop_h, crop_w, icon_bgr, icon_alpha, loc):
    x, y = loc
    th, tw = icon_bgr.shape[:2]
    full_bgr = np.zeros((crop_h, crop_w, 3), np.float32)
    full_alpha = np.zeros((crop_h, crop_w), np.float32)
    y1, y2 = max(y, 0), min(y + th, crop_h)
    x1, x2 = max(x, 0), min(x + tw, crop_w)
    full_bgr[y1:y2, x1:x2] = icon_bgr[y1 - y:y2 - y, x1 - x:x2 - x]
    full_alpha[y1:y2, x1:x2] = icon_alpha[y1 - y:y2 - y, x1 - x:x2 - x]
    return full_bgr, full_alpha


def _hypothesis_score(crop, icon_bgr, icon_alpha, jitter=ALIGN_JITTER):
    ch, cw = crop.shape[:2]
    loc = _align(crop, icon_alpha, jitter)
    full_bgr, full_alpha = _place(ch, cw, icon_bgr, icon_alpha, loc)
    mask = full_alpha > 0.1
    outside = ~mask
    if mask.sum() < 50 or outside.sum() < 50:
        return 0.0

    crop_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32)
    bg_mean = float(crop_gray[outside].mean())

    def recover_mean(icon, strength):
        a = strength * full_alpha[:, :, None]
        denom = np.clip(1.0 - a, 0.01, 1.0)
        bg = np.clip((crop.astype(np.float32) - a * icon) / denom, 0, 255)
        bg_g = cv2.cvtColor(bg.astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
        return float(bg_g[mask].mean())

    red_icon = np.zeros_like(full_bgr)
    red_icon[:, :, 2] = 248

    diff_wh = abs(recover_mean(full_bgr, ALPHA_HL) - bg_mean)
    diff_wl = abs(recover_mean(full_bgr, ALPHA_LO) - bg_mean)
    diff_rh = abs(recover_mean(red_icon, ALPHA_HL) - bg_mean)
    diff_rl = abs(recover_mean(red_icon, ALPHA_LO) - bg_mean)

    return min(diff_wl, diff_rl) - min(diff_wh, diff_rh)


def _score(crop, icon_bgr, icon_alpha, jitter=ALIGN_JITTER):
    cm = _combined_max(crop)
    if icon_bgr is not None:
        cm += _hypothesis_score(crop, icon_bgr, icon_alpha, jitter)
    return cm


class HighlightDetector:

    def __init__(self, state, jitter=ALIGN_JITTER):
        self.state = state
        self.jitter = jitter
        self._templates = _TemplateCache()

    def classify(self, crops):
        """Determine which slot is highlighted.

        crops: {'weapon_1': np.ndarray, 'weapon_2': np.ndarray}
        Returns: int (1 or 2), or 0 if can't determine.
        """
        crop1 = crops.get('weapon_1')
        crop2 = crops.get('weapon_2')
        if crop1 is None or crop2 is None:
            return 0

        w1_name = self.state.weapon_name[0]
        w2_name = self.state.weapon_name[1]

        icon1_bgr, icon1_alpha = self._templates.get(w1_name) if w1_name else (None, None)
        icon2_bgr, icon2_alpha = self._templates.get(w2_name) if w2_name else (None, None)

        s1 = _score(crop1, icon1_bgr, icon1_alpha, self.jitter)
        s2 = _score(crop2, icon2_bgr, icon2_alpha, self.jitter)

        if s1 == s2:
            return 0
        return 1 if s1 > s2 else 2
