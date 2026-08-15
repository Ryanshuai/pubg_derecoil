"""Watch the holes appear, one per round, at 150 fps.

    pixi run python calibration/hole_timeline.py --weapon aug --rounds 5

WHY THIS EXISTS
---------------
calibration/hole_groups.py photographs the wall before and after a burst and
reads the holes off the after frame. That gives the group's TOTAL spread
reliably -- it matches the camera's total climb on two independent runs -- and
it does NOT give the per-round gaps, because assigning holes to rounds needs an
ORDER, and the after frame has no time in it. The order was assumed ("a group
walks up, so the lowest hole is the first round") and the assumption is doing
all the work: reversing it turns a front-loaded pattern into a back-loaded one,
and the two readings disagree about the first round by a factor of four.

⚠ SO THE ORDER IS OBSERVED HERE, NOT INFERRED. The burst lasts 330 ms and the
capture runs at ~150 fps, so roughly 12 frames separate one round from the
next: each hole appears in a frame where the previous ones are already there
and the later ones are not. Which round made which hole stops being a question
about geometry and becomes a question about which frame it showed up in.

This is the same rule the repository already applies to the compensation curve:
a measurement that needs an ordering must get the ordering from something
ordered, not from a property of the aggregate.

HOW POSITIONS STAY COMPARABLE
-----------------------------
The view climbs ~50 px during the burst, so a hole's SCREEN position depends on
when you look. Every frame is therefore phase-correlated back to the first one,
and each hole is recorded in frame-0 coordinates -- the wall's own frame. The
gaps are then differences on the wall, which is what a bullet hole is.

⚠ AND THE SHIFT IS MEASURED ON THE SAME PIXELS THE HOLES ARE IN, so a wrong
shift and a wrong hole position cannot cancel: an error shows up as holes that
drift frame to frame instead of staying put, which the output prints.
"""
import argparse
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from capture.cropper import DXGISyncGrabber, capture_screen        # noqa: E402
from config import SCREEN_H, SCREEN_W                              # noqa: E402
from control.session import ensure_ready                           # noqa: E402
from detector.wall_detector import WallDetector                     # noqa: E402

OUT = os.path.join('calibration', 'artifacts', 'holes')
# Around the crosshair, big enough for a five-round group's ~70 px of climb
# plus the ~50 px the view itself moves, small enough to stay at refresh rate.
BOX = (SCREEN_H // 2 - 210, SCREEN_W // 2 - 200, 400, 400)     # y, x, h, w


def dark_blobs(gray, rel=0.45, area=(5, 140), max_side=18):
    """Hole-shaped dark spots in one frame. Threshold relative to the wall."""
    med = float(np.median(gray))
    m = (gray < med * rel).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, _l, st, ce = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, n):
        a, w, h = st[i, 4], st[i, 2], st[i, 3]
        if not (area[0] <= a <= area[1]) or max(w, h) > max_side:
            continue
        if h == 0 or not (0.4 <= w / h <= 2.5):
            continue
        out.append((float(ce[i][0]), float(ce[i][1]), int(a)))
    return out


def timeline(frames, ts, match_px=9.0):
    """[(t_first_seen, x0, y0, n_frames_seen)] in APPEARANCE order.

    Every frame is aligned back to frame 0 first, so positions are on the wall
    rather than on the screen.
    """
    g0 = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY)
    win = cv2.createHanningWindow((g0.shape[1], g0.shape[0]), cv2.CV_32F)
    tracks = []          # [x, y, t_first, count]
    drift = []
    for i, f in enumerate(frames):
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        if i:
            (sx, sy), _ = cv2.phaseCorrelate(g0.astype(np.float32),
                                             g.astype(np.float32), win)
        else:
            sx = sy = 0.0
        drift.append((sx, sy))
        for bx, by, _a in dark_blobs(g):
            wx, wy = bx - sx, by - sy       # back into frame-0 coordinates
            for tr in tracks:
                if abs(tr[0] - wx) <= match_px and abs(tr[1] - wy) <= match_px:
                    tr[3] += 1
                    break
            else:
                tracks.append([wx, wy, ts[i], 1])
    # A hole, once made, stays. Something seen in one or two frames is noise or
    # a muzzle-flash shadow, and dropping it is the only filter here that is
    # not about shape.
    keep = [t for t in tracks if t[3] >= 4]
    keep.sort(key=lambda t: t[2])
    return keep, drift


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', default='aug')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--rounds', type=int, default=5)
    ap.add_argument('--groups', type=int, default=2)
    ap.add_argument('--yaw-step', type=int, default=-120)
    ap.add_argument('--no-teleport', action='store_true')
    a = ap.parse_args()

    if not ensure_ready(label='the hole timeline', range_name=None)['ok']:
        return 1
    from calibration.hole_groups import (measure_scale, settle_aim,
                                         teleport_to_wall)
    if not a.no_teleport:
        teleport_to_wall()
        from control.spawner import SpawnerControl
        SpawnerControl().ensure_panel(False)

    from calibration.sweep import Rig
    from calibration import collect_timed as CT, rpm_store
    from control.inventory import InventoryControl
    from control.stock import ensure_weapon_in_hand
    from control.spawner import SpawnerControl

    stamp = time.strftime('%m%d_%H%M%S')
    out = os.path.join(OUT, f'timeline_{stamp}')
    os.makedirs(out, exist_ok=True)
    rig = Rig(a.sight, prefer_dxgi=False)
    grabber = None
    try:
        ac, sc = InventoryControl(), SpawnerControl()
        if not ensure_weapon_in_hand(ac, sc, weapon=a.weapon):
            return 1
        if not CT.ensure_sight(ac, sc, 1, a.weapon, a.sight):
            return 1
        if rig.gun.ensure_fire_mode(a.weapon, want='full') != 'full':
            print('  fire mode is not full — refusing')
            return 1
        rig.gun.ensure_ads()
        if not rig.fire.disarm():
            print('  compensation would not confirm OFF')
            return 1

        iv = (rpm_store.load() or {}).get(a.weapon, {}).get('interval_ms', 85.0)
        iv /= 1000.0
        K = rig.K
        wd = WallDetector(climb_px=120, margin_px=30)
        centre = (SCREEN_W // 2, SCREEN_H // 2)
        grabber = DXGISyncGrabber({'impact': BOX})
        scale = measure_scale(rig)
        print(f'px/count: {scale[0]:.2f} yaw, {scale[1]:.2f} pitch')

        for k in range(a.groups):
            print(f'\n── group {k} ──')
            ok, _f, frac, why = settle_aim(rig, wd, centre, scale)
            print(f'  aim: {ok} | {why}')
            if not ok:
                break
            a0 = rig.fire.read_ammo()
            o = rig.fire.fire_magazine_timed(grabber,
                                             max(1, a.rounds - 1), iv)
            time.sleep(0.8)
            a1 = rig.fire.read_ammo()
            fired = (a0 - a1) if (a0 is not None and a1 is not None) else None
            frames = [p['impact'] for p in o['patches']]
            ts = list(o['t'])
            fps = len(ts) / (ts[-1] - ts[0]) if len(ts) > 1 else 0
            print(f'  ammo {a0} -> {a1} (rounds out {fired}); '
                  f'{len(frames)} frames at {fps:.0f} fps')
            tracks, drift = timeline(frames, ts)
            print(f'  holes seen appearing: {len(tracks)}   '
                  f'AGREE: {fired is not None and len(tracks) == fired}')
            print('  %8s %9s %9s %7s' % ('t ms', 'x(wall)', 'y(wall)', 'frames'))
            for j, (x, y, t, cnt) in enumerate(tracks):
                gap = (tracks[j - 1][1] - y) if j else None
                line = '  %8.1f %9.1f %9.1f %7d' % (t * 1000, x, y, cnt)
                if gap is not None:
                    line += ('   round %d recoil: %6.1f px = %6.2f counts'
                             % (j, gap, gap / K))
                print(line)
            dy = drift[-1][1]
            print(f'  view drift over the burst: {dy:+.1f} px '
                  f'= {dy / K:+.1f} counts')
            np.savez_compressed(
                os.path.join(out, f'g{k}.npz'),
                t=np.asarray(ts), frames=np.stack(frames),
                tracks=np.asarray(tracks, dtype=float), K=K, fired=fired or -1)
            if k + 1 < a.groups:
                rig.view.turn(a.yaw_step, 0)
                time.sleep(0.6)
        print(f'\nOUT {out}')
    finally:
        if grabber is not None:
            grabber.close()
        rig.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
