"""Solve an attachment's icon and alpha out of paired captures. Offline.

    pixi run python tools/solve_template.py docs/attachments/runs/<stamp>
    pixi run python tools/solve_template.py <run> --write

Reads the (backdrop, filled) pairs calibration/collect_templates.py's
paired_sweep produces, recovers the icon and its alpha per pixel, and reports
how well the result reconstructs the captures it came from.

THE PROBLEM WITH PHOTOGRAPHING A TEMPLATE. What a slot shows is not the icon.
The panel is translucent, so every captured pixel is

    c = a*icon + (1-a)*backdrop

Storing `c` bakes in one particular scene. It then matches well against that
scene and badly against every other, which is exactly the failure
blend_attachment was written to model rather than suffer.

WHY MORE BACKGROUNDS IS BETTER, not merely sufficient. Two unknowns per pixel
and per channel share one alpha, so ONE pair already determines the answer --
and determines it with whatever antialiasing noise that pair happened to
carry, straight into the template. Six pairs make it overdetermined: the solve
below is a least squares over every pair at once, so per-capture noise averages
down instead of being copied in.

THE SOLVE, per pixel:

    subtract two captures      c_i - c_j = (1-a) * (b_i - b_j)

`icon` drops out entirely, leaving one unknown. Stacked over every pair and
every channel that is a single least squares for (1-a):

    (1-a) = SUM(dc . db) / SUM(db . db)

Pixels where the backdrops barely differ contribute almost nothing to both
sums, which is the right weighting rather than a special case -- a pixel the
scene never moved behind carries no information about transparency.

With `a` known, the icon follows from any capture, averaged over all of them:

    icon = mean_i( c_i - (1-a)*b_i ) / a

and is left undefined where a is ~0, because a fully transparent pixel has no
colour to recover. That is what the alpha channel is for.
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2
import numpy as np

# Below this the pixel is treated as fully transparent and its colour is not
# recovered. Measured against the shipped icons, whose alpha is a hard-edged
# mask with a one-pixel feather, so a real edge pixel sits far above it.
ALPHA_FLOOR = 0.05

# A pair contributes to the alpha solve only where the two backdrops actually
# differ. In grey levels, per channel.
BG_SPREAD_MIN = 6.0


def solve(backdrops, filled):
    """-> (icon BGR float, alpha float, per-capture reconstruction error)"""
    b = np.stack(backdrops).astype(np.float32)      # (N, H, W, 3)
    c = np.stack(filled).astype(np.float32)
    n = len(b)

    # Every unordered pair, which is what makes six captures worth more than
    # two: 15 differences instead of 1.
    num = np.zeros(b.shape[1:3], np.float32)
    den = np.zeros(b.shape[1:3], np.float32)
    for i in range(n):
        for j in range(i + 1, n):
            db, dc = b[i] - b[j], c[i] - c[j]
            keep = np.abs(db) >= BG_SPREAD_MIN
            num += np.sum(dc * db * keep, axis=2)
            den += np.sum(db * db * keep, axis=2)

    one_minus_a = np.divide(num, den, out=np.ones_like(num), where=den > 1e-3)
    alpha = np.clip(1.0 - one_minus_a, 0.0, 1.0)

    # icon = mean over captures of (c - (1-a)*b) / a
    acc = np.zeros(b.shape[1:], np.float32)
    for i in range(n):
        acc += c[i] - (1.0 - alpha)[..., None] * b[i]
    prem = acc / n                                   # a * icon
    safe = np.maximum(alpha, ALPHA_FLOOR)[..., None]
    icon = np.clip(prem / safe, 0, 255)

    err = [float(np.abs(alpha[..., None] * icon
                        + (1 - alpha)[..., None] * b[i] - c[i]).mean())
           for i in range(n)]
    return icon, alpha, err


def stability(run, pairs):
    """How many backgrounds are enough, measured on HELD-OUT captures.

    For k = 2..N-1: solve using k of the pairs, then reconstruct one that was
    NOT in the solve and report the error against what was actually captured.

    That number cannot be talked up by the model. Reconstructing the captures
    a solve was fitted to always looks good -- it is the residual of its own
    fit -- and synthesising captures with the compositing formula the solve
    inverts is worse still: it would only demonstrate that the arithmetic
    undoes itself, and would pass just as happily if that formula were wrong
    about the game. A capture the solve never saw is neither.

    `moved` is the second question: how far the recovered alpha shifts when
    one more background is added. When that stops moving, more captures are
    buying nothing.
    """
    print('  key              k   held-out err   alpha moved vs k-1')
    worst = 0.0
    for key, angles in sorted(pairs.items()):
        usable = [v for v in angles.values()
                  if 'backdrop' in v and 'slots' in v]
        if len(usable) < 3:
            print(f'  {key:<16} only {len(usable)} pair(s) — need at least 3 '
                  f'to hold one out')
            continue
        bg = [cv2.imread(os.path.join(run, v['backdrop'])) for v in usable]
        fg = [cv2.imread(os.path.join(run, v['slots'])) for v in usable]
        prev_a = None
        for k in range(2, len(usable)):
            icon, alpha, _ = solve(bg[:k], fg[:k])
            # The first capture the solve did not use.
            b, c = bg[k].astype(np.float32), fg[k].astype(np.float32)
            pred = alpha[..., None] * icon + (1 - alpha)[..., None] * b
            held = float(np.abs(pred - c).mean())
            moved = ('' if prev_a is None
                     else f'{float(np.abs(alpha - prev_a).mean()):.4f}')
            prev_a = alpha
            print(f'  {key:<16}{k:>3}{held:>15.2f}{moved:>22}')
            worst = max(worst, held)
        print()
    print('  held-out err is grey levels on a crop the solve never saw. It is'
          '\n  the only one of the three numbers here that a wrong compositing'
          '\n  model cannot flatter.'
          '\n  `alpha moved` near zero means another background adds nothing.')
    return 0 if worst < 3.0 else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run', nargs='?', default='')
    ap.add_argument('--write', action='store_true',
                    help='save the solved BGRA icons into the run directory')
    ap.add_argument('--stability', action='store_true',
                    help='how many backgrounds the solve needs, measured by '
                         'HOLD-OUT: solve on k captures, predict one that was '
                         'not used. No synthetic data and no ground truth — '
                         'compositing captures with the same formula the solve '
                         'inverts would only prove the solver can undo its own '
                         'arithmetic.')
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    run = args.run or sorted(glob.glob(
        os.path.join(ROOT, 'docs', 'attachments', 'runs', '*')))[-1]
    man = json.load(open(os.path.join(run, 'manifest.json'), encoding='utf-8'))
    print(f'run: {os.path.relpath(run, ROOT)}\n')

    pairs = defaultdict(dict)
    for e in man['entries']:
        if e.get('target') not in ('backdrop', 'slots'):
            continue
        a = e.get('angle')
        if a is None:
            continue
        pairs[e['key']].setdefault(a, {})[e['target']] = e['capture']

    if not pairs:
        print('No paired captures in this run. paired_sweep writes `backdrop`\n'
              'entries alongside `slots` ones with an `angle`; a run collected\n'
              'before that existed has the filled crops only, and the icon\n'
              'cannot be separated from the scene behind it.')
        return 1

    if args.stability:
        return stability(run, pairs)

    out = os.path.join(run, 'solved')
    if args.write:
        os.makedirs(out, exist_ok=True)
    print(f'{"key":<16}{"pairs":>6}{"mean a":>9}{"recon err":>11}   verdict')
    bad = 0
    for key, angles in sorted(pairs.items()):
        usable = [v for v in angles.values()
                  if 'backdrop' in v and 'slots' in v]
        if len(usable) < 2:
            print(f'{key:<16}{len(usable):>6}   — at least two backgrounds are '
                  f'needed to separate the icon from the scene')
            bad += 1
            continue
        bg = [cv2.imread(os.path.join(run, v['backdrop'])) for v in usable]
        fg = [cv2.imread(os.path.join(run, v['slots'])) for v in usable]
        if any(x is None for x in bg + fg):
            print(f'{key:<16}{len(usable):>6}   — a capture is missing')
            bad += 1
            continue
        icon, alpha, err = solve(bg, fg)
        m = float(alpha.mean())
        e = float(np.mean(err))
        verdict = ('good' if e < 1.5 else
                   'usable' if e < 3 else 'WRONG — look at the alpha')
        if e >= 3:
            bad += 1
        print(f'{key:<16}{len(usable):>6}{m:>9.3f}{e:>11.2f}   {verdict}')
        if args.write:
            bgra = np.dstack([icon.astype(np.uint8),
                              (alpha * 255).astype(np.uint8)])
            cv2.imwrite(os.path.join(out, f'{key}.png'), bgra)
            cv2.imwrite(os.path.join(out, f'{key}_alpha.png'),
                        (alpha * 255).astype(np.uint8))

    print('\n  recon err <1.5 good, 1.5-3 usable, >3 means the pairs are not '
          'pairs\n  (different angle, or the equip did not land between them).'
          '\n  ALWAYS LOOK AT _alpha.png before trusting one: clean shape, '
          'bright\n  where the icon is solid, no scene bleeding through.')
    if args.write:
        print(f'\n  -> {os.path.relpath(out, ROOT)}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
