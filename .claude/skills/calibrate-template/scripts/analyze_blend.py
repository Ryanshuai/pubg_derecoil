"""Analyze how an icon is blended onto the game background.

Given with_ui, no_ui screenshots and icon position, analyzes:
- Alpha blend value
- Whether the icon is tinted (white/red/other color)
- Whether there's shadow, blur, or glow
- Blend formula

Usage:
    python analyze_blend.py <icon_path> <with_ui> <no_ui> <x> <y> <scale_pct>
"""
import argparse
import cv2
import numpy as np
import json


def analyze(icon_path, with_ui_path, no_ui_path, x, y, scale_pct):
    icon_bgra = cv2.imread(icon_path, cv2.IMREAD_UNCHANGED)
    with_ui = cv2.imread(with_ui_path)
    no_ui = cv2.imread(no_ui_path)

    # Scale icon
    scale = scale_pct / 100.0
    sh = int(icon_bgra.shape[0] * scale)
    sw = int(icon_bgra.shape[1] * scale)
    resized = cv2.resize(icon_bgra, (sw, sh), interpolation=cv2.INTER_AREA)
    icon_bgr = resized[:, :, :3].astype(np.float32)
    icon_alpha = resized[:, :, 3].astype(np.float32) / 255.0

    # Extract regions
    fg_real = with_ui[y:y+sh, x:x+sw].astype(np.float32)
    bg = no_ui[y:y+sh, x:x+sw].astype(np.float32)

    # Only analyze pixels where icon has content (alpha > 0.5)
    mask = icon_alpha > 0.5
    if mask.sum() < 10:
        return {'error': 'Icon has too few visible pixels'}

    # ── 1. Estimate alpha blend value ──
    # Model: result = alpha * icon_color + (1 - alpha) * bg
    # If icon is white (255,255,255): result = alpha * 255 + (1 - alpha) * bg
    # So: alpha = (result - bg) / (icon_color - bg)

    # Check if icon is white
    icon_mean = icon_bgr[mask].mean(axis=0)
    is_white_icon = all(c > 200 for c in icon_mean)

    # Estimate alpha per pixel where icon has content
    # alpha = (fg_real - bg) / (icon_color - bg)
    alphas_per_channel = []
    for c in range(3):
        icon_c = icon_bgr[:, :, c]
        fg_c = fg_real[:, :, c]
        bg_c = bg[:, :, c]
        denom = icon_c - bg_c
        valid = mask & (np.abs(denom) > 20)  # avoid division by near-zero
        if valid.sum() > 10:
            alpha_est = (fg_c[valid] - bg_c[valid]) / denom[valid]
            alpha_est = np.clip(alpha_est, 0, 1)
            alphas_per_channel.append(float(np.median(alpha_est)))

    alpha_estimate = float(np.median(alphas_per_channel)) if alphas_per_channel else 0.0

    # ── 2. Detect icon color/tint ──
    # Reconstruct what the icon color would be given estimated alpha
    # icon_color = (fg_real - (1-alpha) * bg) / alpha
    if alpha_estimate > 0.05:
        reconstructed = (fg_real - (1 - alpha_estimate) * bg) / alpha_estimate
        reconstructed = np.clip(reconstructed, 0, 255)
        color_in_icon = reconstructed[mask].mean(axis=0)  # BGR
    else:
        color_in_icon = [0, 0, 0]

    # Classify color
    b, g, r = color_in_icon
    if r > 200 and g > 200 and b > 200:
        color_name = 'white'
    elif r > 100 and g < 50 and b < 50:
        color_name = 'red'
    elif r > 200 and g > 150 and b < 50:
        color_name = 'yellow'
    else:
        color_name = f'custom_bgr({b:.0f},{g:.0f},{r:.0f})'

    # ── 3. Detect shadow/glow ──
    # Look at pixels just outside the icon boundary
    # Dilate mask and look at the ring
    kernel = np.ones((5, 5), np.uint8)
    dilated = cv2.dilate(mask.astype(np.uint8), kernel, iterations=2)
    ring = (dilated > 0) & (~mask)

    if ring.sum() > 10:
        ring_diff = np.abs(fg_real[ring] - bg[ring]).mean()
    else:
        ring_diff = 0.0

    has_shadow_or_glow = ring_diff > 5.0

    # ── 4. Detect blur ──
    # Compare sharpness of icon in screenshot vs original
    if mask.sum() > 100:
        real_gray = cv2.cvtColor(fg_real.astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
        icon_gray = cv2.cvtColor(icon_bgr.astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
        real_lap = cv2.Laplacian(real_gray, cv2.CV_32F)
        icon_lap = cv2.Laplacian(icon_gray, cv2.CV_32F)
        real_sharpness = float(real_lap[mask].var())
        icon_sharpness = float(icon_lap[mask].var())
        blur_ratio = real_sharpness / (icon_sharpness + 1e-6)
    else:
        real_sharpness = 0
        icon_sharpness = 0
        blur_ratio = 1.0

    is_blurred = blur_ratio < 0.3

    # ── 5. Compute reconstruction error ──
    # Synthesize with estimated params and compare
    synth = bg.copy()
    a = (icon_alpha * alpha_estimate)[:, :, None]
    if color_name == 'white':
        fg_color = np.full_like(icon_bgr, 255.0)
    elif color_name == 'red':
        fg_color = np.full_like(icon_bgr, [9, 12, 150], dtype=np.float32)
    else:
        fg_color = icon_bgr
    synth_region = a * fg_color + (1 - a) * bg
    synth = np.clip(synth_region, 0, 255)

    recon_error = float(np.abs(synth[mask] - fg_real[mask]).mean())

    return {
        'alpha_estimate': round(alpha_estimate, 4),
        'icon_color': color_name,
        'icon_color_bgr': [round(c, 1) for c in color_in_icon],
        'has_shadow_or_glow': has_shadow_or_glow,
        'shadow_ring_diff': round(ring_diff, 2),
        'is_blurred': is_blurred,
        'sharpness_ratio': round(float(blur_ratio), 3),
        'reconstruction_error': round(recon_error, 2),
        'blend_formula': f'output = {alpha_estimate:.3f} * {color_name} + {1-alpha_estimate:.3f} * background',
    }


def main():
    parser = argparse.ArgumentParser(description='Analyze icon blend mode')
    parser.add_argument('icon', help='BGRA icon image path')
    parser.add_argument('with_ui', help='Screenshot with UI')
    parser.add_argument('no_ui', help='Screenshot without UI')
    parser.add_argument('x', type=int, help='Icon x position')
    parser.add_argument('y', type=int, help='Icon y position')
    parser.add_argument('scale_pct', type=int, help='Icon scale percentage')
    args = parser.parse_args()

    result = analyze(args.icon, args.with_ui, args.no_ui, args.x, args.y, args.scale_pct)
    def to_native(obj):
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        raise TypeError(type(obj))
    print(json.dumps(result, indent=2, default=to_native))


if __name__ == '__main__':
    main()
