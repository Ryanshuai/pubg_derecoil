"""
Icon layout + overlay logic for each HUD element type.

Each layout class provides a complete, self-contained interface:
  WHERE:  get_slot_rect(), crop_size()
  WHAT:   load icons, scale, position
  HOW:    apply(canvas) calls icon_merging for compositing
  META:   label_names, in_channels, preprocess(), model_input_hw

dataset.py takes any layout through this uniform interface, no task-specific code.
"""

import random
import cv2
import numpy as np
import os
from config import (
    WEAPON_HUD_1, WEAPON_HUD_2, ATTACHMENT_SLOTS, IN_TAB, POSTURE, FIRE_MODE,
    ASSET_DIR, ALPHA,
)
from dl_models.icon_merging import (
    alpha_blend, dewhite, blend_tab_background, blend_attachment, blend_status_bar,
)


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
# Weapon HUD icons
# ============================================================

_WEAPON_HUD = {1: WEAPON_HUD_1, 2: WEAPON_HUD_2}

WEAPON_ICON_MAP = {
    'Item_Weapon_ACE32_C_w.png':       'ace32',
    'Item_Weapon_AK47_C_w.png':        'akm',
    'Item_Weapon_AUG_C_w.png':         'aug',
    'Item_Weapon_AWM_C_w.png':         'awm',
    'Item_Weapon_Berreta686_C_w.png':  's686',
    'Item_Weapon_BerylM762_C_w.png':   'm762',
    'Item_Weapon_BizonPP19_C_w.png':   'pp19',
    'Item_Weapon_DP12_C_w.png':        'dbs',
    'Item_Weapon_DP28_C_w.png':        'dp28',
    'Item_Weapon_G36C_C_w.png':        'g36c',
    'Item_Weapon_GROZA_C_w.png':       'groza',
    'Item_Weapon_HK416_C_w.png':       'm416',
    'Item_Weapon_K2_C_w.png':          'k2',
    'Item_Weapon_Kar98k_C_w.png':      '98k',
    'Item_Weapon_L6_C_w.png':          'lynx',
    'Item_Weapon_M16A4_C_w.png':       'm16',
    'Item_Weapon_M249_C_w.png':        'm249',
    'Item_Weapon_M24_C_w.png':         'm24',
    'Item_Weapon_MG3_C_w.png':         'mg3',
    'Item_Weapon_MP5K_C_w.png':        'mp5k',
    'Item_Weapon_MP9_C_w.png':         'mp9',
    'Item_Weapon_Mini14_C_w.png':      'mini14',
    'Item_Weapon_Mk12_C_w.png':        'mk12',
    'Item_Weapon_Mk14_C_w.png':        'mk14',
    'Item_Weapon_Mk47Mutant_C_w.png':  'mk47',
    'Item_Weapon_Mosin_C_w.png':       'mosin',
    'Item_Weapon_OriginS12_C_w.png':   'o12',
    'Item_Weapon_P90_C_w.png':         'p90',
    'Item_Weapon_QBU88_C_w.png':       'qbu',
    'Item_Weapon_QBZ95_C_w.png':       'qbz',
    'Item_Weapon_SCAR-L_C_w.png':      'scar',
    'Item_Weapon_SKS_C_w.png':         'sks',
    'Item_Weapon_SLR_C_w.png':         'slr',
    'Item_Weapon_S1897_C_w.png':       's1897',
    'Item_Weapon_Saiga12_C_w.png':     's12k',
    'Item_Weapon_Thompson_C_w.png':    'tommy',
    'Item_Weapon_UMP_C_w.png':         'ump45',
    'Item_Weapon_UZI_C_w.png':         'uzi',
    'Item_Weapon_VSS_C_w.png':         'vss',
    'Item_Weapon_Vector_C_w.png':      'vector',
    'Item_Weapon_Win1894_C_w.png':     'win94',
}

WEAPON_CLASSES = [
    '98k', 'ace32', 'akm', 'aug', 'awm', 'dbs', 'dp28', 'g36c', 'groza', 'k2',
    'lynx', 'm16', 'm24', 'm249', 'm416', 'm762', 'mg3', 'mini14', 'mk12', 'mk14',
    'mk47', 'mosin', 'mp5k', 'mp9', 'o12', 'p90', 'pp19', 'qbu', 'qbz', 's12k',
    's1897', 's686', 'scar', 'sks', 'slr', 'tommy', 'ump45', 'uzi', 'vector',
    'vss', 'win94',
]
_ICON_HEIGHT = 53


class WeaponIconLayout(RegionLayout):
    """
    武器 HUD 图标 (右下角)

    - 右对齐, 垂直 offset=9px
    - 缩放: 100px → 53px
    - 4通道输入 (BGR + dewhite)
    """

    def __init__(self, icons_dir=None, bg_prob=0.1, red_prob=0.2, jitter_px=2):
        if icons_dir is None:
            icons_dir = os.path.join(os.path.dirname(__file__), '..', ASSET_DIR['weapon'])
        self.icons_dir = icons_dir
        self.bg_prob = bg_prob
        self.red_prob = red_prob
        self.jitter_px = jitter_px

        # Load all icons
        self.icons = {}
        for fname, cls in WEAPON_ICON_MAP.items():
            if cls not in WEAPON_CLASSES:
                continue
            bgra = load_bgra(os.path.join(icons_dir, fname))
            if bgra is None:
                continue
            h, w = bgra.shape[:2]
            scale = _ICON_HEIGHT / h
            new_w = int(w * scale)
            resized = cv2.resize(bgra, (new_w, _ICON_HEIGHT), interpolation=cv2.INTER_NEAREST)
            self.icons[cls] = (resized[:, :, :3], resized[:, :, 3].astype(np.float32) / 255.0, new_w)

        self.available = [c for c in WEAPON_CLASSES if c in self.icons]

    # ── WHERE ──

    def get_slot_rect(self, slot_id=1):
        cfg = _WEAPON_HUD[slot_id]
        return (cfg['x1'], cfg['y1'], cfg['x2'], cfg['y2'])

    # ── META ──

    @property
    def label_names(self):
        return {'gun_name': len(WEAPON_CLASSES) + 1, 'highlighted': 3}

    @property
    def model_input_hw(self):
        return (53, 206)

    @property
    def in_channels(self):
        return 4

    def preprocess(self, img_bgr):
        return np.dstack([img_bgr, dewhite(img_bgr)])

    # ── HOW ──

    def apply(self, canvas):
        if random.random() < self.bg_prob:
            return {'gun_name': 0, 'highlighted': 0}

        cls_name = random.choice(self.available)
        bgr, icon_alpha, icon_w = self.icons[cls_name]

        highlighted = random.choice([True, False])
        strength = ALPHA['weapon_highlighted'] if highlighted else ALPHA['weapon_non_highlighted']

        color = None
        if random.random() < self.red_prob:
            color = (9, 12, 150) if highlighted else (9, 12, 100)

        slot_w = self.crop_hw[1]
        jx = random.randint(-self.jitter_px, self.jitter_px)
        jy = random.randint(-self.jitter_px, self.jitter_px)
        alpha_blend(canvas, bgr, icon_alpha, slot_w - icon_w + jx, jy, strength, color)

        return {
            'gun_name': WEAPON_CLASSES.index(cls_name) + 1,
            'highlighted': 1 if highlighted else 2,
        }


# ============================================================
# Attachment icons (Tab screen, centered in 67x67 grid)
# ============================================================

ATTACHMENT_CLASSES = sorted([
    # Lower (grip)
    'Lower_AngledForeGrip_C', 'Lower_Foregrip_C', 'Lower_Foregrip_Crossbow',
    'Lower_HalfGrip_C', 'Lower_LaserPointer_C', 'Lower_LightweightForeGrip_C',
    'Lower_QuickDraw_Large_Crossbow_C', 'Lower_Sniper_CheekPad_Vss_setting',
    'Lower_ThumbGrip_C',
    # Magazine
    'Magazine_ExtendedQuickDraw_Large_C', 'Magazine_ExtendedQuickDraw_Medium_C',
    'Magazine_ExtendedQuickDraw_Small_C', 'Magazine_ExtendedQuickDraw_SniperRifle_C',
    'Magazine_Extended_DrumMagazine', 'Magazine_Extended_Large_C',
    'Magazine_Extended_Medium_C', 'Magazine_Extended_Small_C',
    'Magazine_Extended_SniperRifle_C', 'Magazine_QuickDraw_Large_C',
    'Magazine_QuickDraw_Medium_C', 'Magazine_QuickDraw_Small_C',
    'Magazine_QuickDraw_SniperRifle_C',
    'Magazine_SR_ExtendedQuick_Mag_Vss_setting',
    'Magazine_SR_Extended_Mag_Vss_setting',
    'Magazine_SR_QucikDraw_Magazine_Vss_setting',
    'Medium_ExtendedQuickDraw_Magazine_Vector',
    'Medium_Extended_Magazine_Vector', 'Medium_QuickDraw_Magazine_Vector',
    # Muzzle
    'Muzzle_Choke_C', 'Muzzle_Compensator_Large_C', 'Muzzle_Compensator_Medium_C',
    'Muzzle_Compensator_SniperRifle_C', 'Muzzle_Duckbill_C',
    'Muzzle_FlashHider_Large_C', 'Muzzle_FlashHider_Medium_C',
    'Muzzle_FlashHider_SniperRifle_C', 'Muzzle_Suppressor_Large_C',
    'Muzzle_Suppressor_Medium_C', 'Muzzle_Suppressor_Small_C',
    'Muzzle_Suppressor_SniperRifle_C',
    # SideRail
    'SideRail_DotSight_RMR_C',
    # Stock
    'Stock_AR_Composite_C', 'Stock_Shotgun_BulletLoops_C',
    'Stock_SniperRifle_BulletLoops_C', 'Stock_SniperRifle_CheekPad_C',
    'Stock_UZI_C',
    # Upper (scope)
    'Upper_ACOG_01_C', 'Upper_Aimpoint_C', 'Upper_CQBSS_C',
    'Upper_DotSight_01_C', 'Upper_Holosight_C', 'Upper_PM2_01_C',
    'Upper_Scope3x_C', 'Upper_Scope6x_C',
    # Vector special
    'Vector_VerGrip',
])


class AttachmentIconLayout(RegionLayout):
    """配件图标 (Tab 界面), 63×63, 单 head 分所有配件."""

    ICON_SCALE_PCT = 9

    def __init__(self, icons_dir=None, bg_prob=0.15, jitter_px=2):
        if icons_dir is None:
            icons_dir = os.path.join(os.path.dirname(__file__), '..', ASSET_DIR['attachment'])
        self.icons_dir = icons_dir
        self.bg_prob = bg_prob
        self.jitter_px = jitter_px

        # Load all icons
        self.icons = {}  # cls_name -> (bgra_scaled, sw, sh)
        for fname in os.listdir(icons_dir):
            if not fname.endswith('.png'):
                continue
            cls = fname.replace('Item_Attach_Weapon_', '').replace('.png', '')
            if cls not in ATTACHMENT_CLASSES:
                continue
            bgra = load_bgra(os.path.join(icons_dir, fname))
            if bgra is None:
                continue
            ih, iw = bgra.shape[:2]
            sh = int(ih * self.ICON_SCALE_PCT / 100)
            sw = int(iw * self.ICON_SCALE_PCT / 100)
            scaled = cv2.resize(bgra, (sw, sh), interpolation=cv2.INTER_NEAREST)
            self.icons[cls] = scaled

        self.available = [c for c in ATTACHMENT_CLASSES if c in self.icons]
        print(f'Loaded {len(self.available)} attachment icons')

    def get_slot_rect(self, gun_id=1, slot_name='scope'):
        """63×63 rect (bevel border excluded)."""
        return tuple(ATTACHMENT_SLOTS[gun_id][slot_name])

    # ── META ──

    @property
    def label_names(self):
        return {'attachment': len(ATTACHMENT_CLASSES) + 1}  # 0=empty

    @property
    def crop_hw(self):
        return (63, 63)

    # ── HOW ──

    def apply(self, canvas):
        if random.random() < self.bg_prob:
            # Empty slot: 0.50 * blur(bg)
            blend_attachment(canvas, None, 0, 0)
            return {'attachment': 0}

        cls_name = random.choice(self.available)
        icon_scaled = self.icons[cls_name]

        blend_attachment(canvas, icon_scaled, 0, 0)

        return {'attachment': ATTACHMENT_CLASSES.index(cls_name) + 1}


# ============================================================
# Posture icons (bottom HUD, left of health bar)
# ============================================================

POSTURE_CLASSES = ['standing', 'crouching', 'prone']

POSTURE_ICON_MAP = {
    'posture_standing_icon_bgra.png':  'standing',
    'posture_crouching_icon_bgra.png': 'crouching',
    'posture_prone_icon_bgra.png':     'prone',
}


class PostureIconLayout(RegionLayout):
    """
    姿态图标 (底部 HUD, 血条左侧)

    - 白色图标, alpha 通道自带强度 (~0.75 peak)
    - 合成: output = icon_alpha * 255 + (1 - icon_alpha) * background
    - 3 类 + 背景 = 4 类
    """

    def __init__(self, icons_dir=None, bg_prob=0.15, jitter_px=2):
        if icons_dir is None:
            icons_dir = os.path.join(os.path.dirname(__file__), '..', ASSET_DIR['posture'])
        self.bg_prob = bg_prob
        self.jitter_px = jitter_px

        self.icons = {}
        for fname, cls in POSTURE_ICON_MAP.items():
            bgra = load_bgra(os.path.join(icons_dir, fname))
            if bgra is None:
                continue
            self.icons[cls] = (
                bgra[:, :, :3],
                bgra[:, :, 3].astype(np.float32) / 255.0,
            )

        self.available = [c for c in POSTURE_CLASSES if c in self.icons]
        print(f'Loaded {len(self.available)} posture icons')

    # ── WHERE ──

    def get_slot_rect(self):
        return (POSTURE['x1'], POSTURE['y1'], POSTURE['x2'], POSTURE['y2'])

    # ── META ──

    @property
    def label_names(self):
        return {'posture': len(POSTURE_CLASSES) + 1}  # 0=background

    @property
    def crop_hw(self):
        return (POSTURE['y2'] - POSTURE['y1'], POSTURE['x2'] - POSTURE['x1'])

    # ── HOW ──

    def apply(self, canvas):
        if random.random() < self.bg_prob:
            return {'posture': 0}

        cls_name = random.choice(self.available)
        icon_bgr, icon_alpha = self.icons[cls_name]

        jx = random.randint(-self.jitter_px, self.jitter_px)
        jy = random.randint(-self.jitter_px, self.jitter_px)
        alpha_blend(canvas, icon_bgr, icon_alpha, jx, jy)

        return {'posture': POSTURE_CLASSES.index(cls_name) + 1}


# ============================================================
# Tab detection ("Type" text region)
# ============================================================

class TabDetectLayout(RegionLayout):
    """
    Tab 检测: "Type" 文字有无 (二分类)

    - 正样本: blur+darken 背景 + Type 文字
    - 负样本: 原始背景
    - 3通道 BGR
    """

    def __init__(self, icons_dir=None, positive_prob=0.5, jitter_px=1):
        if icons_dir is None:
            icons_dir = os.path.join(os.path.dirname(__file__), '..', ASSET_DIR['tab_detect'])
        self.positive_prob = positive_prob
        self.jitter_px = jitter_px

        bgra = load_bgra(os.path.join(icons_dir, 'type_crop_bgra_clean.png'))
        if bgra is None:
            raise ValueError(f"Type icon not found in {icons_dir}")
        self.type_bgr = bgra[:, :, :3]
        self.type_alpha = bgra[:, :, 3].astype(np.float32) / 255.0

    # ── WHERE ──

    def get_slot_rect(self):
        return (IN_TAB['x1'], IN_TAB['y1'], IN_TAB['x2'], IN_TAB['y2'])

    # ── META ──

    @property
    def label_names(self):
        return {'tab_open': 2}

    # ── HOW ──

    def apply(self, canvas):
        if random.random() >= self.positive_prob:
            return {'tab_open': 0}

        blend_tab_background(canvas)
        jx = random.randint(-self.jitter_px, self.jitter_px)
        jy = random.randint(-self.jitter_px, self.jitter_px)
        alpha_blend(canvas, self.type_bgr, self.type_alpha, jx, jy)
        return {'tab_open': 1}


# ============================================================
# Fire mode icons (bottom HUD, left of ammo count)
# ============================================================

FIRE_MODE_CLASSES = ['single', 'burst2', 'burst3', 'full', 'single_bot', 'high']

FIRE_MODE_ICON_MAP = {
    'fire_mode_single.png':     'single',
    'fire_mode_burst2.png':     'burst2',
    'fire_mode_burst3.png':     'burst3',
    'fire_mode_full.png':       'full',
    'fire_mode_single_bot.png': 'single_bot',
    'fire_mode_high.png':       'high',
}


class FireModeLayout(RegionLayout):
    """
    开火模式图标 (底部 HUD, 弹药数左侧)

    - 白色图标, BGRA alpha 通道 = 亮度
    - 合成: blend_status_bar (模糊 + 暗化 + 白色叠加)
    - 6 类 + 背景 = 7 类
    """

    def __init__(self, icons_dir=None, bg_prob=0.15, jitter_px=2):
        if icons_dir is None:
            icons_dir = os.path.join(os.path.dirname(__file__), '..', ASSET_DIR['fire_mode'])
        self.bg_prob = bg_prob
        self.jitter_px = jitter_px

        self.icons = {}
        for fname, cls in FIRE_MODE_ICON_MAP.items():
            bgra = load_bgra(os.path.join(icons_dir, fname))
            if bgra is None:
                continue
            self.icons[cls] = bgra[:, :, 3].astype(np.float32) / 255.0

        self.available = [c for c in FIRE_MODE_CLASSES if c in self.icons]
        print(f'Loaded {len(self.available)} fire mode icons')

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

    # ── HOW ──

    def apply(self, canvas):
        if random.random() < self.bg_prob:
            # 50% raw background, 50% blur+darken without icon
            if random.random() < 0.5:
                zero_alpha = np.zeros(canvas.shape[:2], dtype=np.float32)
                blend_status_bar(canvas, zero_alpha, 0, 0)
            return {'fire_mode': 0}

        cls_name = random.choice(self.available)
        icon_alpha = self.icons[cls_name]

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
    'weapon': WeaponIconLayout,
    'attachment': AttachmentIconLayout,
    'posture': PostureIconLayout,
    'tab_detect': TabDetectLayout,
    'fire_mode': FireModeLayout,
}

def get_layout(name, **kwargs):
    return LAYOUTS[name](**kwargs)
