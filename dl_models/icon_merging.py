"""
Icon compositing / blending functions.

Each HUD element type has its own blend mode. This file handles HOW to render,
while icon_layout.py handles WHERE to place.

All functions operate on a canvas (BGR uint8/float) in-place.
"""

import cv2
import numpy as np


# ============================================================
# General alpha blend (used by all icon types)
# ============================================================

def alpha_blend(canvas, icon_bgr, icon_alpha, x, y, strength=1.0, color=None):
    """
    通用 alpha blend, 所有图标合成的基础函数.

    Parameters:
        canvas:     (H, W, 3) uint8 BGR
        icon_bgr:   (h, w, 3) uint8 or float32
        icon_alpha:  (h, w) float32 [0,1]
        x, y:       top-left position on canvas
        strength:   overall opacity scalar [0,1]
        color:      None=use icon_bgr, or (B,G,R) tuple to recolor

    Formula: output = alpha * strength * fg + (1 - alpha * strength) * bg
    """
    ih, iw = icon_bgr.shape[:2]
    x1, y1 = max(x, 0), max(y, 0)
    x2, y2 = min(x + iw, canvas.shape[1]), min(y + ih, canvas.shape[0])
    if x2 <= x1 or y2 <= y1:
        return
    ix1, iy1 = x1 - x, y1 - y
    ix2, iy2 = ix1 + (x2 - x1), iy1 + (y2 - y1)

    fg = icon_bgr[iy1:iy2, ix1:ix2].astype(np.float32)
    if color is not None:
        fg = np.full_like(fg, color, dtype=np.float32)

    bg = canvas[y1:y2, x1:x2].astype(np.float32)
    a = (icon_alpha[iy1:iy2, ix1:ix2] * strength)[:, :, None]
    canvas[y1:y2, x1:x2] = np.clip(a * fg + (1 - a) * bg, 0, 255).astype(np.uint8)


# ============================================================
# Dewhite preprocessing (isolate white icon signal)
# ============================================================

def dewhite(img_bgr):
    """
    减去估计背景, 提取白色图标信号, 返回单通道 grayscale.

    用于 weapon HUD 检测的第 4 通道.
    """
    bg_est = cv2.GaussianBlur(img_bgr.astype(np.float32), (31, 31), 10)
    signal = np.clip((img_bgr.astype(np.float32) - bg_est) * 2, 0, 255)
    return cv2.cvtColor(signal.astype(np.uint8), cv2.COLOR_BGR2GRAY)


# ============================================================
# Tab inventory background (blur + darken)
# ============================================================

def blend_tab_background(canvas):
    """
    Tab 背包界面背景: 高斯模糊 + 压暗到 ~49%

    Formula: result = GaussianBlur(bg, k=41) * 0.49
    """
    blurred = cv2.GaussianBlur(canvas, (41, 41), 0).astype(np.float32)
    canvas[:] = np.clip(blurred * 0.49, 0, 255).astype(np.uint8)


# ============================================================
# Attachment slot blend (blur + bevel + outline + icon)
# ============================================================

def blend_attachment(canvas, icon_bgra_scaled, x, y,
                     blur_k=49, blur_sigma=8, brightness=1.0,
                     outline_dilate=1, outline_sigma=1.0, outline_pad=8):
    """
    Tab 界面配件格子: 模糊 + 暗化 + 黑描边 + 图标叠加
    全程 63×63 (已裁掉 2px bevel 边框)

    Parameters:
        canvas:             (H, W, 3) uint8/float32, raw background (not tab-darkened)
        icon_bgra_scaled:   (sh, sw, 4) uint8, already scaled icon (or None for empty)
        x, y:               top-left of the 63x63 slot in canvas
        blur_k/blur_sigma:  background blur params
        brightness:         icon color scale (1.0 = original API color)
        outline_dilate:     hard black outline width (1px)
        outline_sigma:      fade sigma (1.0)
        outline_pad:        padding for fade (8px)

    Rendering:
        1. Empty slot: 0.50 * blur(bg)
        2. Occupied slot bg: 0.37 * blur(bg) + 44
        3. outline = dilate(alpha, 1px) → blur(σ=1) → max(solid, blur) → black blend
        4. icon alpha blend (INTER_NEAREST scaled, original color)
    """
    slot_h, slot_w = 63, 63
    region = canvas[y:y + slot_h, x:x + slot_w].astype(np.float32)
    blurred = cv2.GaussianBlur(region, (blur_k, blur_k), blur_sigma).astype(np.float32)

    if icon_bgra_scaled is None:
        # Empty slot: uniform 0.50 darkening
        canvas[y:y + slot_h, x:x + slot_w] = np.clip(
            0.50 * blurred, 0, 255
        ).astype(np.uint8)
        return

    # Step 1: occupied slot background (no bevel, inner area only)
    box_bg = 0.37 * blurred + 44

    # Step 2: prepare icon
    sh, sw = icon_bgra_scaled.shape[:2]
    icon_bgr = np.clip(brightness * icon_bgra_scaled[:, :, :3].astype(np.float32), 0, 255)
    icon_alpha = icon_bgra_scaled[:, :, 3].astype(np.float32) / 255.0

    # Step 3: pad for outline fade
    pad = outline_pad
    alpha_pad = np.zeros((sh + pad * 2, sw + pad * 2), dtype=np.float32)
    alpha_pad[pad:pad + sh, pad:pad + sw] = icon_alpha
    bgr_pad = np.zeros((sh + pad * 2, sw + pad * 2, 3), dtype=np.float32)
    bgr_pad[pad:pad + sh, pad:pad + sw] = icon_bgr
    au8 = (alpha_pad * 255).astype(np.uint8)

    # Step 4: outline = dilate → blur → max(solid, blur)
    kern = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (outline_dilate * 2 + 1, outline_dilate * 2 + 1),
    )
    solid = cv2.dilate(au8, kern, iterations=1).astype(np.float32) / 255.0
    blur_k_outline = max(int(outline_sigma * 6) | 1, 3)
    faded = cv2.GaussianBlur(solid, (blur_k_outline, blur_k_outline), outline_sigma)
    outline_alpha = np.maximum(solid, faded)

    # Step 5: place on box_bg (centered)
    cx = (slot_w - sw) // 2
    cy = (slot_h - sh) // 2
    px, py = cx - pad, cy - pad
    ph, pw = outline_alpha.shape

    # Clip to slot bounds
    sy1, sx1 = max(0, -py), max(0, -px)
    sy2, sx2 = min(ph, slot_h - py), min(pw, slot_w - px)
    dy1, dx1 = max(0, py), max(0, px)
    dy2 = dy1 + (sy2 - sy1)
    dx2 = dx1 + (sx2 - sx1)

    # Black outline blend
    oa = outline_alpha[sy1:sy2, sx1:sx2, np.newaxis]
    box_bg[dy1:dy2, dx1:dx2] = (1 - oa) * box_bg[dy1:dy2, dx1:dx2]

    # Icon alpha blend
    ia = alpha_pad[sy1:sy2, sx1:sx2, np.newaxis]
    ib = bgr_pad[sy1:sy2, sx1:sx2]
    box_bg[dy1:dy2, dx1:dx2] = ia * ib + (1 - ia) * box_bg[dy1:dy2, dx1:dx2]

    canvas[y:y + slot_h, x:x + slot_w] = np.clip(box_bg, 0, 255).astype(np.uint8)


# ============================================================
# Posture / fire mode blend (TBD, placeholder)
# ============================================================

def blend_status_bar(canvas, icon_alpha_mask, x, y, blur_k=21, gradient=0.66):
    """
    底部状态栏: 模糊 + 暗化 + 图标叠加 (placeholder, needs calibration)

    Formula: output = alpha * 255 + (1 - alpha) * gradient * blur(bg)
    """
    ih, iw = icon_alpha_mask.shape[:2]
    region = canvas[y:y + ih, x:x + iw].astype(np.float32)
    blurred = cv2.GaussianBlur(region, (blur_k, blur_k), 0)
    darkened = gradient * blurred
    alpha = icon_alpha_mask[:, :, np.newaxis]
    canvas[y:y + ih, x:x + iw] = np.clip(
        alpha * 255 + (1 - alpha) * darkened, 0, 255
    ).astype(np.uint8)
