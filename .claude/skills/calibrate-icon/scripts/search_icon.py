"""Search for an icon in a screenshot using multi-scale template matching.

Usage:
    python search_icon.py <icon_path> <screenshot_path> <x1> <y1> <x2> <y2> [--verify <no_ui_path>]

Args:
    icon_path:       BGRA icon image (with transparency)
    screenshot_path: full game screenshot (with UI)
    x1,y1,x2,y2:    search region in screen coordinates
    --verify:        optional no-UI screenshot for diff verification
"""
import argparse
import cv2
import numpy as np
import json
import os


def search(icon_path, screenshot_path, region, scale_range=(15, 85)):
    icon_bgra = cv2.imread(icon_path, cv2.IMREAD_UNCHANGED)
    if icon_bgra is None:
        raise FileNotFoundError(f"Cannot read icon: {icon_path}")
    if icon_bgra.shape[2] == 3:
        # No alpha, create from white pixels
        gray = cv2.cvtColor(icon_bgra, cv2.COLOR_BGR2GRAY)
        _, alpha = cv2.threshold(gray, 10, 255, cv2.THRESH_BINARY)
        icon_bgra = np.dstack([icon_bgra, alpha])

    screenshot = cv2.imread(screenshot_path)
    if screenshot is None:
        raise FileNotFoundError(f"Cannot read screenshot: {screenshot_path}")

    x1, y1, x2, y2 = region
    search_area = screenshot[y1:y2, x1:x2]
    search_gray = cv2.cvtColor(search_area, cv2.COLOR_BGR2GRAY).astype(np.float32)

    icon_h, icon_w = icon_bgra.shape[:2]
    best_score = -1
    best = None

    # Coarse search
    for scale_pct in range(scale_range[0], scale_range[1]):
        scale = scale_pct / 100.0
        sh = int(icon_h * scale)
        sw = int(icon_w * scale)
        if sh > search_area.shape[0] or sw > search_area.shape[1] or sh < 5:
            continue

        resized = cv2.resize(icon_bgra, (sw, sh), interpolation=cv2.INTER_AREA)
        tg = cv2.cvtColor(resized[:, :, :3], cv2.COLOR_BGR2GRAY).astype(np.float32)
        mask = resized[:, :, 3].astype(np.float32)

        result = cv2.matchTemplate(search_gray, tg, cv2.TM_CCORR_NORMED, mask=mask)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val > best_score:
            best_score = max_val
            best = {
                'scale_pct': scale_pct,
                'icon_size': [sh, sw],
                'screen_pos': [x1 + max_loc[0], y1 + max_loc[1]],
                'screen_end': [x1 + max_loc[0] + sw, y1 + max_loc[1] + sh],
                'score': float(max_val),
            }

    # Fine search around best scale
    fine_results = []
    best_pct = best['scale_pct']
    for scale_pct in range(max(best_pct - 5, 10), best_pct + 6):
        scale = scale_pct / 100.0
        sh = int(icon_h * scale)
        sw = int(icon_w * scale)
        if sh > search_area.shape[0] or sw > search_area.shape[1] or sh < 5:
            continue

        resized = cv2.resize(icon_bgra, (sw, sh), interpolation=cv2.INTER_AREA)
        tg = cv2.cvtColor(resized[:, :, :3], cv2.COLOR_BGR2GRAY).astype(np.float32)
        mask = resized[:, :, 3].astype(np.float32)

        result = cv2.matchTemplate(search_gray, tg, cv2.TM_CCORR_NORMED, mask=mask)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        ax, ay = x1 + max_loc[0], y1 + max_loc[1]

        entry = {
            'scale_pct': scale_pct,
            'icon_size': [sh, sw],
            'screen_pos': [ax, ay],
            'screen_end': [ax + sw, ay + sh],
            'score': float(max_val),
        }
        fine_results.append(entry)
        if max_val > best['score']:
            best = entry

    return best, fine_results


def verify(icon_path, no_ui_path, with_ui_path, best, alpha_blend_val=0.80):
    """Synthesize icon on no-UI image and diff with real with-UI image."""
    icon_bgra = cv2.imread(icon_path, cv2.IMREAD_UNCHANGED)
    no_ui = cv2.imread(no_ui_path)
    with_ui = cv2.imread(with_ui_path)

    sh, sw = best['icon_size']
    resized = cv2.resize(icon_bgra, (sw, sh), interpolation=cv2.INTER_AREA)
    bgr = resized[:, :, :3].astype(np.float32)
    alpha = resized[:, :, 3].astype(np.float32) / 255.0

    x, y = best['screen_pos']
    synth = no_ui.copy()
    region = synth[y:y + sh, x:x + sw].astype(np.float32)
    a = (alpha * alpha_blend_val)[:, :, None]
    synth[y:y + sh, x:x + sw] = np.clip(a * bgr + (1 - a) * region, 0, 255).astype(np.uint8)

    # Compute diff in icon region
    synth_crop = synth[y:y + sh, x:x + sw].astype(np.float32)
    real_crop = with_ui[y:y + sh, x:x + sw].astype(np.float32)
    abs_diff = np.abs(synth_crop - real_crop)

    return {
        'mean_diff': float(abs_diff.mean()),
        'max_diff': float(abs_diff.max()),
    }


def main():
    parser = argparse.ArgumentParser(description='Search for icon in screenshot')
    parser.add_argument('icon', help='BGRA icon image path')
    parser.add_argument('screenshot', help='Game screenshot path (with UI)')
    parser.add_argument('x1', type=int, help='Search region x1')
    parser.add_argument('y1', type=int, help='Search region y1')
    parser.add_argument('x2', type=int, help='Search region x2')
    parser.add_argument('y2', type=int, help='Search region y2')
    parser.add_argument('--verify', help='No-UI screenshot for verification')
    parser.add_argument('--alpha', type=float, default=0.80, help='Alpha blend value')
    parser.add_argument('--save-dir', default=None, help='Save visualization to dir')
    args = parser.parse_args()

    region = (args.x1, args.y1, args.x2, args.y2)
    best, fine = search(args.icon, args.screenshot, region)

    print(json.dumps({
        'best': best,
        'fine_search': fine,
    }, indent=2))

    if args.verify:
        v = verify(args.icon, args.verify, args.screenshot, best, args.alpha)
        print(f"\nVerification: mean_diff={v['mean_diff']:.2f}, max_diff={v['max_diff']:.2f}")

    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)
        screenshot = cv2.imread(args.screenshot)
        x, y = best['screen_pos']
        sh, sw = best['icon_size']
        vis = screenshot.copy()
        cv2.rectangle(vis, (x, y), (x + sw, y + sh), (0, 255, 0), 2)
        # Crop around match, with margin
        margin = 50
        cx, cy = x + sw // 2, y + sh // 2
        crop = vis[max(cy-margin, 0):cy+margin+sh, max(cx-margin-sw, 0):cx+margin]
        crop_big = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
        cv2.imwrite(os.path.join(args.save_dir, 'match_result.png'), crop_big)


if __name__ == '__main__':
    main()
