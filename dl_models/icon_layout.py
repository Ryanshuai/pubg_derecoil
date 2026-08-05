"""
Icon layout + overlay logic for each HUD element type.

Each layout class provides a complete, self-contained interface:
  WHERE:  get_slot_rect(), crop_size()
  WHAT:   load icons, scale, position
  HOW:    apply(canvas) calls icon_merging for compositing
  META:   label_names, in_channels, preprocess(), model_input_hw

dataset.py takes any layout through this uniform interface, no task-specific code.

FIRE MODE IS THE ONLY LAYOUT LEFT. The weapon, attachment and tab_detect
layouts were removed on 2026-08-05 along with the checkpoints they synthesised
training data for: all three questions are now answered by template matching
against real screen captures, which beat the nets on the two that were ever
measured (attachments 0.984, weapon HUD 0.975 against the net's silhouette
confusions). tab_detect's net had already been unwired as "never consulted".
"""

import random
import cv2
import numpy as np
import os
from config import FIRE_MODE, ASSET_DIR
from dl_models.icon_merging import dewhite, blend_status_bar


# ============================================================
# Helpers
# ============================================================

def load_bgra(path):
    """Load image as BGRA, handling grayscale and BGR inputs."""
    im = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if im is None:
        return None
    if im.ndim == 2:
        im = cv2.cvtColor(im, cv2.COLOR_GRAY2BGRA)
    elif im.shape[2] == 3:
        im = cv2.cvtColor(im, cv2.COLOR_BGR2BGRA)
    return im


# ============================================================
# Base class
# ============================================================

class RegionLayout:
    """统一接口, dataset.py 只认这个."""

    # ── WHERE ──
    def get_slot_rect(self, **kwargs):
        raise NotImplementedError

    def crop_size(self, **kwargs):
        x1, y1, x2, y2 = self.get_slot_rect(**kwargs)
        return (x2 - x1, y2 - y1)

    # ── META (子类必须覆写) ──
    @property
    def label_names(self):
        """Dict {head_name: num_classes}."""
        raise NotImplementedError

    @property
    def crop_hw(self):
        """(H, W) canvas size for training."""
        w, h = self.crop_size()
        return (h, w)

    @property
    def model_input_hw(self):
        """(H, W) model input size."""
        return self.crop_hw

    @property
    def in_channels(self):
        """Model input channels."""
        return 3

    # ── HOW ──
    def preprocess(self, img_bgr):
        """BGR uint8 -> model-ready HWC array."""
        return img_bgr

    def apply(self, canvas):
        """Composite onto canvas (in-place). Return {head_name: label_int}."""
        raise NotImplementedError


# ============================================================
# Fire mode icons (bottom HUD, left of ammo count)
# ============================================================

FIRE_MODE_CLASSES = ['single', 'burst2', 'burst3', 'full', 'single_sniper', 'single_shotgun', 'high', 'single_smoke']

# Two variants per class: original position + _low (shifted down 7px for new crop)
# Training randomly picks one variant per sample
FIRE_MODE_ICON_PAIRS = {
    'single':             ('fire_mode_single.png', 'fire_mode_single_low.png'),
    'burst2':             ('fire_mode_burst2.png', 'fire_mode_burst2_low.png'),
    'burst3':             ('fire_mode_burst3.png', 'fire_mode_burst3_low.png'),
    'full':               ('fire_mode_full.png', 'fire_mode_full_low.png'),
    'single_sniper':      ('fire_mode_single_sniper.png', 'fire_mode_single_sniper_low.png'),
    'single_shotgun':     ('fire_mode_single_shotgun.png', 'fire_mode_single_shotgun_low.png'),
    'high':               ('fire_mode_high.png', 'fire_mode_high_low.png'),
    'single_smoke':       ('fire_mode_single_smoke.png', 'fire_mode_single_smoke_low.png'),
}


class FireModeLayout(RegionLayout):
    """
    开火模式图标 (底部 HUD, 弹药数左侧)

    - 白色图标, BGRA alpha 通道 = 亮度
    - 合成: blend_status_bar (模糊 + 暗化 + 白色叠加)
    - 每类两个模板变体 (原位 + 下移7px), 训练时随机二选一
    - 7 类 + 背景 = 8 类
    """

    def __init__(self, icons_dir=None, bg_prob=0.15, jitter_px=2):
        if icons_dir is None:
            icons_dir = os.path.join(os.path.dirname(__file__), '..', ASSET_DIR['fire_mode'])
        self.bg_prob = bg_prob
        self.jitter_px = jitter_px

        # icons[cls] = [alpha_variant_1, alpha_variant_2]
        self.icons = {}
        for cls, (fname1, fname2) in FIRE_MODE_ICON_PAIRS.items():
            variants = []
            for fname in (fname1, fname2):
                bgra = load_bgra(os.path.join(icons_dir, fname))
                if bgra is not None:
                    variants.append(bgra[:, :, 3].astype(np.float32) / 255.0)
            if variants:
                self.icons[cls] = variants

        self.available = [c for c in FIRE_MODE_CLASSES if c in self.icons]
        print(f'Loaded {len(self.available)} fire mode icons ({sum(len(v) for v in self.icons.values())} variants)')

    # ── WHERE ──

    def get_slot_rect(self):
        return (FIRE_MODE['x1'], FIRE_MODE['y1'], FIRE_MODE['x2'], FIRE_MODE['y2'])

    # ── META ──

    @property
    def label_names(self):
        return {'fire_mode': len(FIRE_MODE_CLASSES) + 1}  # 0=background

    @property
    def crop_hw(self):
        return (FIRE_MODE['y2'] - FIRE_MODE['y1'], FIRE_MODE['x2'] - FIRE_MODE['x1'])

    @property
    def in_channels(self):
        return 4

    def preprocess(self, img_bgr):
        return np.dstack([img_bgr, dewhite(img_bgr)])

    # ── HOW ──

    def apply(self, canvas):
        if random.random() < self.bg_prob:
            # 50% raw background, 50% blur+darken without icon
            if random.random() < 0.5:
                zero_alpha = np.zeros(canvas.shape[:2], dtype=np.float32)
                blend_status_bar(canvas, zero_alpha, 0, 0)
            return {'fire_mode': 0}

        cls_name = random.choice(self.available)
        icon_alpha = random.choice(self.icons[cls_name])

        jx = random.randint(-self.jitter_px, self.jitter_px)
        jy = random.randint(-self.jitter_px, self.jitter_px)

        # Build full-canvas alpha: entire region gets blur+darken, icon placed at offset
        ch, cw = canvas.shape[:2]
        full_alpha = np.zeros((ch, cw), dtype=np.float32)
        ih, iw = icon_alpha.shape[:2]
        # Clip to canvas bounds
        sx1, sy1 = max(0, -jx), max(0, -jy)
        dx1, dy1 = max(0, jx), max(0, jy)
        dx2 = min(cw, jx + iw)
        dy2 = min(ch, jy + ih)
        full_alpha[dy1:dy2, dx1:dx2] = icon_alpha[sy1:sy1+(dy2-dy1), sx1:sx1+(dx2-dx1)]

        blend_status_bar(canvas, full_alpha, 0, 0)

        return {'fire_mode': FIRE_MODE_CLASSES.index(cls_name) + 1}


# ============================================================
# Registry
# ============================================================

LAYOUTS = {
    'fire_mode': FireModeLayout,
}

