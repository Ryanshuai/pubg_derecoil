"""Diff with-UI and no-UI screenshots to find UI element regions.

Usage:
    python diff_overview.py <with_ui_path> <no_ui_path> --save-dir docs/debug
"""
import argparse
import cv2
import numpy as np
import os


def find_ui_regions(with_ui_path, no_ui_path, threshold=15, min_area=500):
    with_ui = cv2.imread(with_ui_path)
    no_ui = cv2.imread(no_ui_path)

    if with_ui is None:
        raise FileNotFoundError(f"Cannot read: {with_ui_path}")
    if no_ui is None:
        raise FileNotFoundError(f"Cannot read: {no_ui_path}")

    diff = cv2.absdiff(with_ui, no_ui)
    diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)

    # Threshold to find UI regions
    _, binary = cv2.threshold(diff_gray, threshold, 255, cv2.THRESH_BINARY)

    # Dilate to connect nearby regions
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 20))
    dilated = cv2.dilate(binary, kernel, iterations=2)

    # Find contours
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    regions = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        x, y, w, h = cv2.boundingRect(c)
        # Mean diff intensity in this region
        mean_diff = diff_gray[y:y+h, x:x+w].mean()
        regions.append({
            'x1': int(x), 'y1': int(y),
            'x2': int(x + w), 'y2': int(y + h),
            'width': int(w), 'height': int(h),
            'mean_diff': float(mean_diff),
        })

    regions.sort(key=lambda r: r['mean_diff'], reverse=True)
    return regions, diff, with_ui


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('with_ui', help='Screenshot with UI')
    parser.add_argument('no_ui', help='Screenshot without UI')
    parser.add_argument('--save-dir', default='docs/debug')
    parser.add_argument('--threshold', type=int, default=15)
    args = parser.parse_args()

    regions, diff, with_ui = find_ui_regions(args.with_ui, args.no_ui, args.threshold)

    # Save overview
    os.makedirs(args.save_dir, exist_ok=True)

    # Amplified diff
    diff_vis = np.clip(diff.astype(np.float32) * 5, 0, 255).astype(np.uint8)
    cv2.imwrite(os.path.join(args.save_dir, 'diff_overview.png'), diff_vis)

    # Annotated screenshot with region boxes
    vis = with_ui.copy()
    for i, r in enumerate(regions):
        cv2.rectangle(vis, (r['x1'], r['y1']), (r['x2'], r['y2']), (0, 255, 0), 2)
        cv2.putText(vis, f"R{i}", (r['x1'], r['y1'] - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    cv2.imwrite(os.path.join(args.save_dir, 'diff_regions.png'), vis)

    # Save each region crop
    for i, r in enumerate(regions):
        crop = with_ui[r['y1']:r['y2'], r['x1']:r['x2']]
        cv2.imwrite(os.path.join(args.save_dir, f'region_{i:02d}.png'), crop)

    # Print results
    print(f"Found {len(regions)} UI regions:")
    for i, r in enumerate(regions):
        print(f"  R{i}: ({r['x1']},{r['y1']})~({r['x2']},{r['y2']}) "
              f"{r['width']}x{r['height']} mean_diff={r['mean_diff']:.1f}")


if __name__ == '__main__':
    main()
