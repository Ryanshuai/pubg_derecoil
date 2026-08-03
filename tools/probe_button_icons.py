"""Are the three bottom-right button icons white, and are they opaque?

Whether they can anchor an "am I on the spawner screen?" check depends on
both: a flat opaque white glyph matches by raw pixels, an alpha-blended one
carries the scene through and only its shape survives. Three screenshots with
different scenes behind the same buttons answer it directly.
"""
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNS = os.path.join(ROOT, 'docs', 'spawner', 'runs')
SHOTS = [
    os.path.join(ROOT, 'temp_debug', 'screenshot_main_20260801_204338.png'),
    os.path.join(RUNS, '20260801_205423', '00_baseline.png'),
    os.path.join(RUNS, '20260801_210656', '00_baseline.png'),
]

# Button strip, read off icon_region.png (crop origin was 2500,880)
STRIP = (2500, 880, 2960, 1290)   # x0, y0, x1, y1


def find_buttons(img):
    """The three button frames: bright 1px rectangles inside the strip."""
    x0, y0, x1, y1 = STRIP
    g = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    # Frame edges are brighter than the button fill but dimmer than the text
    edge = (g > 120).astype(np.uint8)
    rows = edge.sum(axis=1)
    wide = rows > (x1 - x0) * 0.7          # a full horizontal frame line
    ys = np.where(wide)[0]
    groups = []
    for y in ys:
        if groups and y - groups[-1][-1] <= 3:
            groups[-1].append(y)
        else:
            groups.append([y])
    lines = [int(np.mean(gp)) for gp in groups]
    boxes = []
    for a, b in zip(lines, lines[1:]):
        if 40 <= b - a <= 90:              # a button, not the gap between two
            boxes.append((y0 + a, y0 + b))
    return boxes


imgs = [cv2.imread(p) for p in SHOTS]
for p, im in zip(SHOTS, imgs):
    print(f'{os.path.basename(os.path.dirname(p)) or "."}/'
          f'{os.path.basename(p)}: {"missing" if im is None else im.shape}')
imgs = [im for im in imgs if im is not None]

boxes = find_buttons(imgs[-1])
print(f'\nbutton rows in strip: {boxes}')

# Icon sits at the left end of each button
ICON_W = 70
for i, (ya, yb) in enumerate(boxes, 1):
    xa = STRIP[0] + 14
    crops = [im[ya:yb, xa:xa + ICON_W] for im in imgs]
    cv2.imwrite(os.path.join(ROOT, 'docs', 'spawner', 'icons',
                             f'button_icon_{i}_grey.png'), crops[-1])

    c = crops[-1]
    b, g, r = c[:, :, 0].astype(int), c[:, :, 1].astype(int), c[:, :, 2].astype(int)
    mx = np.maximum(np.maximum(b, g), r)
    mn = np.minimum(np.minimum(b, g), r)
    chroma = mx - mn
    gray = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)

    hot = gray > 200
    print(f'\nicon {i}  crop {c.shape[1]}x{c.shape[0]}  y {ya}..{yb}')
    print(f'  gray: min {gray.min()} max {gray.max()} '
          f'mean {gray.mean():.1f}  px>200: {int(hot.sum())} '
          f'({100 * hot.mean():.1f}%)')
    print(f'  chroma |max-min| over whole crop: max {int(chroma.max())} '
          f'mean {chroma.mean():.2f}')
    if hot.any():
        print(f'  chroma on bright px only: max {int(chroma[hot].max())} '
              f'mean {chroma[hot].mean():.2f}')
        print(f'  bright px BGR mean: '
              f'{b[hot].mean():.1f}, {g[hot].mean():.1f}, {r[hot].mean():.1f}')

    # Opacity: same pixels, different scenes behind
    if len(crops) > 1:
        stack = np.stack([cv2.cvtColor(x, cv2.COLOR_BGR2GRAY).astype(int)
                          for x in crops])
        spread = stack.max(axis=0) - stack.min(axis=0)
        print(f'  across {len(crops)} scenes: spread max {int(spread.max())} '
              f'mean {spread.mean():.1f}')
        if hot.any():
            print(f'    on bright px: spread max {int(spread[hot].max())} '
                  f'mean {spread[hot].mean():.1f}')
        dark = gray < 120
        if dark.any():
            print(f'    on dark px:   spread max {int(spread[dark].max())} '
                  f'mean {spread[dark].mean():.1f}')
