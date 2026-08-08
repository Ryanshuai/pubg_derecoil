"""Where the rounds actually LAND. The one measurement outside the loop's own chain.

    pixi run python calibration/probe_hole_pattern.py --weapon m416
    pixi run python calibration/probe_hole_pattern.py --weapon m416 --both

WHY THIS EXISTS
---------------
Every number the calibration loop produces comes off ONE chain:

    patch correlation -> view displacement -> / K -> binned by a clock
      -> compared against the curve  ->  residual, sum|e|, wander, floor, ratio

Eleven hypotheses about why the loop stalls were raised and falsified on
2026-08-07, and every one of them was about a link INSIDE that chain, judged
using the chain's own output. A stable bias anywhere in it produces numbers
that are self-consistent and wrong, and nothing downstream can see it.

Bullet holes are outside it. They are a physical record: no tracker, no K, no
binning, no clock. The vertical spread of one magazine's holes IS the residual
-- not a proxy for it -- and a human can read the picture directly.

⚠ IT MUST BE ABLE TO SAY "NOTHING IS HERE". Firing into the sky leaves no
holes, and an empty diff looks exactly like perfect compensation (every round
through the same point). So the baseline is checked for a surface first, and a
diff with too few marks is REFUSED rather than reported as a tight group. This
is the same rule the shot-latency probe had to learn the hard way today, where
"never seen" was true of an empty magazine.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from calibration.sweep import Rig                                # noqa: E402
from control.session import ensure_ready                         # noqa: E402

OUT_DIR = os.path.join(ROOT, 'calibration', 'artifacts', 'holes')

# A hole darkens the wall. 18 is well clear of frame-to-frame noise on a still
# scene (measured floor ~4) and well under the 40+ a real mark makes.
DIFF_MIN = 18
# Marks smaller than this are muzzle flash residue and compression noise.
BLOB_MIN_PX = 6
# Below this many marks the picture is not a group -- most likely the rounds
# went into the sky or past the wall. Refuse rather than report a tight group.
MARKS_MIN = 6


def marks(before, after):
    """Hole centroids as (x, y), from the darkening between two frames."""
    a = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY).astype(np.int16)
    b = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY).astype(np.int16)
    darker = np.clip(a - b, 0, 255).astype(np.uint8)
    _, m = cv2.threshold(darker, DIFF_MIN, 255, cv2.THRESH_BINARY)
    n, _lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= BLOB_MIN_PX:
            out.append((float(cent[i][0]), float(cent[i][1])))
    return out, darker


def one_burst(rig, comp_on):
    """-> (marks, before, after). Fires one magazine and reads the wall.

    No round count: `fire_magazine()` empties whatever is loaded and counts
    it itself. A `mag_size` argument sat here unread until the move into
    calibration/ put the file under `pixi run params` (2026-08-08).
    """
    if not comp_on:
        rig.fire.disarm()
    rig.gun.ensure_ads()
    rig.flush(4)
    before = rig.grab().copy()
    rig.fire.fire_magazine()
    time.sleep(0.5)
    # Put the view back where it started so the holes are in the baseline's
    # frame of reference. Without this the camera has moved and the diff
    # measures the pan, not the group.
    rig.view.recenter()
    rig.flush(4)
    after = rig.grab().copy()
    return marks(before, after), before, after


def report(name, pts):
    if len(pts) < MARKS_MIN:
        print(f'  {name}: only {len(pts)} mark(s) — REFUSING to call that a '
              f'group.\n    Either the rounds missed the surface or there is '
              f'no surface. Face a wall\n    at a fixed range and re-run; an '
              f'empty diff and perfect compensation\n    look identical here.')
        return None
    y = np.array([p[1] for p in pts])
    x = np.array([p[0] for p in pts])
    print(f'  {name}: {len(pts)} marks   vertical spread '
          f'{y.max() - y.min():6.1f} px (sd {y.std():5.1f})   '
          f'horizontal {x.max() - x.min():6.1f} px')
    return float(y.max() - y.min())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', default='m416')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--both', action='store_true',
                    help='fire a second magazine with the compensation OFF, '
                         'so the two pictures can be compared side by side')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    print('>>> FACE A WALL at a fixed range, gun held and loaded. The holes '
          'are the measurement.')
    if not ensure_ready(label='the hole-pattern probe',
                        countdown_s=args.countdown)['ok']:
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%m%d_%H%M%S')
    rig = Rig(args.sight)
    try:
        rounds, _ = rig.fire.top_up()
        if not rounds:
            print('[!] REFUSING: no ammo counter — nothing held, or empty.')
            return 2
        print(f'    {rounds} rounds\n')

        (pts, dark), before, after = one_burst(rig, True)
        cv2.imwrite(os.path.join(OUT_DIR, f'{args.weapon}_{stamp}_comp_on.png'),
                    after)
        cv2.imwrite(os.path.join(OUT_DIR,
                                 f'{args.weapon}_{stamp}_comp_on_diff.png'),
                    dark)
        on = report('compensation ON ', pts)

        off = None
        if args.both:
            rig.fire.top_up()
            (pts2, dark2), _b2, after2 = one_burst(rig, False)
            cv2.imwrite(os.path.join(OUT_DIR,
                                     f'{args.weapon}_{stamp}_comp_off.png'),
                        after2)
            cv2.imwrite(os.path.join(OUT_DIR,
                                     f'{args.weapon}_{stamp}_comp_off_diff.png'),
                        dark2)
            off = report('compensation OFF', pts2)
    finally:
        rig.close()

    print(f'\n  -> {OUT_DIR}')
    if on is not None and off is not None:
        print(f'\n  The compensation takes the vertical spread from '
              f'{off:.0f} px to {on:.0f} px '
              f'({100 * (1 - on / max(off, 1e-9)):.0f}% of the climb removed).')
        print('  That number owes nothing to the view tracker, K, the bullet '
              'binning or the\n  clock — which is the point.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
