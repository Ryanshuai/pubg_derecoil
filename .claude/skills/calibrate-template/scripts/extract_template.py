"""Extract icon alpha template from with_ui / no_ui screenshot pairs.

Supports two blend modes:
  - "alpha":      result = alpha * white + (1 - alpha) * bg
  - "status_bar": result = alpha * 255 + (1 - alpha) * gradient * blur(bg, k)

Given a crop region and blend mode, computes the icon alpha mask and saves as BGRA.

Usage:
    # Simple alpha blend (posture, weapon icons)
    python extract_template.py --mode alpha \
        --with-ui img_with_ui.png --no-ui img_no_ui.png \
        --region 1626,1325,1682,1368 \
        --output template.png --save-dir calibration/artifacts/debug

    # Status bar blend (fire mode, with blur+darken)
    python extract_template.py --mode status_bar \
        --with-ui img_with_ui.png --no-ui img_no_ui.png \
        --region 1626,1325,1682,1368 \
        --blur-k 17 --gradient 0.65 \
        --output template.png --save-dir calibration/artifacts/debug

    # Batch: multiple with_ui files, same no_ui prefix pattern
    python extract_template.py --mode status_bar \
        --with-ui a_with_ui.png b_with_ui.png c_with_ui.png \
        --no-ui a_no_ui.png b_no_ui.png c_no_ui.png \
        --region 1626,1325,1682,1368 \
        --blur-k 17 --gradient 0.65 \
        --output-dir templates/ --save-dir calibration/artifacts/debug
"""
import argparse
import cv2
import numpy as np
import os
import json


def extract_alpha_blend(with_ui, no_ui, region):
    """Extract alpha for simple blend: result = alpha * white + (1-alpha) * bg."""
    x1, y1, x2, y2 = region
    ui_crop = with_ui[y1:y2, x1:x2].astype(np.float32)
    bg_crop = no_ui[y1:y2, x1:x2].astype(np.float32)

    # alpha = (ui - bg) / (255 - bg)
    denom = np.clip(255.0 - bg_crop, 1.0, 255.0)
    alpha_rgb = (ui_crop - bg_crop) / denom
    alpha = np.mean(alpha_rgb, axis=2)
    alpha = np.clip(alpha, 0, 1)
    alpha[alpha < 0.03] = 0
    return alpha


def extract_status_bar(with_ui, no_ui, region, blur_k=17, gradient=0.65):
    """Extract alpha for status bar: result = alpha*255 + (1-alpha)*gradient*blur(bg,k)."""
    x1, y1, x2, y2 = region
    h, w = y2 - y1, x2 - x1

    # Blur needs padding for edge accuracy
    pad = blur_k * 2
    bg_wide = no_ui[y1 - pad:y2 + pad, x1 - pad:x2 + pad].astype(np.float32)
    blurred = cv2.GaussianBlur(bg_wide, (blur_k, blur_k), 0)[pad:pad + h, pad:pad + w]
    darkened = gradient * blurred

    ui_crop = with_ui[y1:y2, x1:x2].astype(np.float32)

    # alpha = (ui - darkened) / (255 - darkened)
    denom = np.clip(255.0 - darkened, 1.0, 255.0)
    alpha_rgb = (ui_crop - darkened) / denom
    alpha = np.mean(alpha_rgb, axis=2)
    alpha = np.clip(alpha, 0, 1)
    alpha[alpha < 0.03] = 0

    # Compute reconstruction error
    recon = alpha[:, :, np.newaxis] * 255 + (1 - alpha[:, :, np.newaxis]) * darkened
    error = float(np.abs(recon - ui_crop).mean())

    return alpha, error


def alpha_to_bgra(alpha):
    """Convert alpha mask to white BGRA image."""
    h, w = alpha.shape
    bgra = np.zeros((h, w, 4), dtype=np.uint8)
    bgra[:, :, :3] = 255
    bgra[:, :, 3] = (alpha * 255).astype(np.uint8)
    return bgra


def main():
    parser = argparse.ArgumentParser(description='Extract icon template from screenshots')
    parser.add_argument('--mode', choices=['alpha', 'status_bar'], default='alpha',
                        help='Blend mode: alpha (simple) or status_bar (blur+darken)')
    parser.add_argument('--with-ui', nargs='+', required=True, help='Screenshot(s) with UI')
    parser.add_argument('--no-ui', nargs='+', required=True, help='Screenshot(s) without UI')
    parser.add_argument('--region', required=True, help='Crop region: x1,y1,x2,y2')
    parser.add_argument('--blur-k', type=int, default=17, help='Blur kernel (status_bar mode)')
    parser.add_argument('--gradient', type=float, default=0.65, help='Darken factor (status_bar mode)')
    parser.add_argument('--output', default=None, help='Output BGRA template path (single file)')
    parser.add_argument('--output-dir', default=None, help='Output dir for batch (one per input)')
    parser.add_argument('--save-dir', default=None, help='Save visualizations')
    args = parser.parse_args()

    region = tuple(int(x) for x in args.region.split(','))
    assert len(region) == 4, "Region must be x1,y1,x2,y2"

    if len(args.with_ui) != len(args.no_ui):
        parser.error("--with-ui and --no-ui must have same number of files")

    results = []
    for i, (ui_path, noui_path) in enumerate(zip(args.with_ui, args.no_ui)):
        with_ui = cv2.imread(ui_path)
        no_ui = cv2.imread(noui_path)
        if with_ui is None:
            print(f"ERROR: Cannot read {ui_path}")
            continue
        if no_ui is None:
            print(f"ERROR: Cannot read {noui_path}")
            continue

        if args.mode == 'alpha':
            alpha = extract_alpha_blend(with_ui, no_ui, region)
            error = 0.0
        else:
            alpha, error = extract_status_bar(with_ui, no_ui, region,
                                              args.blur_k, args.gradient)

        bgra = alpha_to_bgra(alpha)
        name = os.path.splitext(os.path.basename(ui_path))[0].replace('_with_ui', '')

        # Save output
        if args.output and len(args.with_ui) == 1:
            cv2.imwrite(args.output, bgra)
            print(f"Saved: {args.output}")
        elif args.output_dir:
            os.makedirs(args.output_dir, exist_ok=True)
            out_path = os.path.join(args.output_dir, f'{name}.png')
            cv2.imwrite(out_path, bgra)
            print(f"Saved: {out_path}")

        # Save visualization
        if args.save_dir:
            os.makedirs(args.save_dir, exist_ok=True)
            vis = (alpha * 255).astype(np.uint8)
            zoomed = cv2.resize(vis, (vis.shape[1] * 8, vis.shape[0] * 8),
                                interpolation=cv2.INTER_NEAREST)
            cv2.imwrite(os.path.join(args.save_dir, f'{name}_alpha.png'), zoomed)

        info = {
            'file': ui_path,
            'name': name,
            'size': list(alpha.shape),
            'icon_pixels': int((alpha > 0.1).sum()),
            'alpha_max': round(float(alpha.max()), 3),
            'recon_error': round(error, 2),
        }
        results.append(info)
        print(f"  {name}: {alpha.shape[1]}x{alpha.shape[0]}  "
              f"icon_px={info['icon_pixels']}  alpha_max={info['alpha_max']}  "
              f"recon_err={info['recon_error']}")

    print(json.dumps({'mode': args.mode, 'region': list(region), 'results': results}, indent=2))


if __name__ == '__main__':
    main()
