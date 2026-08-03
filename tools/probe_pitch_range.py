"""Map the pitch travel once, per posture, so nothing has to scan for it again.

The bottom pitch clamp is the one absolutely repeatable position this game
offers: push the view further down than the travel could possibly be and it is
against the stop, wherever it started. Everything else can be expressed as an
offset from there.

Today every cell re-discovers where it can aim by sweeping ground-to-sky in
100-count steps and keeping whatever tracked (sweep.calibrate_pitch). That is
slow, it is visible, and worst of all it is not repeatable: the band came back
100..1900 in one run and 800..2200 in the next, because it depends on what the
character happens to be facing. Two cells aimed at different pitches are not
comparable, and the measured recoil moves with the aim.

So measure it ONCE on flat ground, per posture, and store counts-above-the-stop:

    pixi run python tools/probe_pitch_range.py --postures standing,crouching,prone

For each posture it homes to the bottom clamp, then steps up, and at every step
records a screenshot plus two numbers: whether the view tracks there, and where
the horizon sits on screen. The horizon crossing the screen centre IS level.

Output: docs/pitch/<posture>_NNNN.png per step, and a summary table. The chosen
level offset goes in docs/pitch/pitch_range.json, which sweep.py then uses
instead of scanning — home, move up by that, done.

Run it on FLAT ground facing open terrain. On a slope or against a wall the
horizon is not the horizon.
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))

import cv2
import numpy as np

from control.focus import ensure_focus
from config import SCREEN_W, SCREEN_H
from detector.cropper import win32_cap
from press.pico_mouse import HID_KEY_2
from control.aim import CLAMP_SETTLE_S
from sweep import Rig, POSTURES

OUT = os.path.join(ROOT, 'docs', 'pitch')
STORE = os.path.join(OUT, 'pitch_range.json')

STEP = 100          # counts per step, same granularity as the old band scan
MAX_UP = 3000       # past any plausible travel
SETTLE = 0.25


def horizon_row(frame):
    """Screen row where sky ends, or None if no sky is visible.

    Sky is bright and smooth; terrain is textured. Scanning rows top-down for
    the first one whose detail rises above the floor finds the boundary
    without needing to know the colour of either.
    """
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # One detail number per row, over the middle half of the screen so the HUD
    # down either edge does not vote.
    x0, x1 = g.shape[1] // 4, g.shape[1] * 3 // 4
    band = g[:, x0:x1]
    lap = np.abs(cv2.Laplacian(band, cv2.CV_32F))
    rows = lap.mean(axis=1)
    # Smooth over 9 rows: a lone bright cloud edge is not the horizon.
    k = np.ones(9) / 9
    rows = np.convolve(rows, k, mode='same')
    sky = rows < 2.0
    if not sky[:50].any():          # nothing sky-like at the very top
        return None
    for y in range(len(rows)):
        if not sky[y] and not sky[max(0, y - 5):y].all():
            continue
        if not sky[y]:
            return y
    return None


def draw_weapon(rig):
    """Get a gun in hand. Postures are read off the posture icon, which only
    renders while aiming, and aiming needs a weapon out — an empty-handed
    character reports 'no posture icon' for all three and nothing works."""
    rig.mouse.key(HID_KEY_2, 60)
    time.sleep(0.5)
    return rig.ensure_ads()


def probe(rig, posture, out_dir, step=STEP, max_up=MAX_UP):
    if not rig.ensure_posture(posture):
        print(f"  [!] could not reach {posture}")
        return []
    rig.home_to_clamp(+1)           # +1 is down
    time.sleep(CLAMP_SETTLE_S)

    rows, rises = [], 0
    cy = None
    while rises < max_up:
        rig.flush(3)
        frame = win32_cap((0, 0, SCREEN_H, SCREEN_W))
        h = horizon_row(frame)
        if cy is None:
            cy = frame.shape[0] // 2
        # Does the view track here? Command a small move and see it arrive.
        # `prev` must be grabbed BEFORE the move — see track_still.
        prev = rig.tracker.slice_frame(rig.grab())
        rig.mouse.move(0, -step)
        got = abs(rig.track_still(timeout_s=0.7, still_s=0.10, prev=prev))
        rises += step
        rows.append({'up': rises, 'horizon_y': h,
                     'tracks': got > step * 0.5, 'got': round(got, 1)})
        path = os.path.join(out_dir, f'{posture}_{rises:04d}.png')
        cv2.imwrite(path, cv2.resize(frame, (frame.shape[1] // 2,
                                             frame.shape[0] // 2)))
        note = '' if h is None else f'horizon y={h} (centre {cy})'
        print(f"    +{rises:5d}  tracks={'yes' if rows[-1]['tracks'] else 'no ':<3}"
              f"  moved {got:6.1f}  {note}")
        if h is not None and h >= cy:
            print(f"    horizon has reached the screen centre — this is level")
            break
    return rows


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--postures', default='standing,crouching,prone')
    ap.add_argument('--step', type=int, default=STEP)
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--countdown', type=int, default=5)
    args = ap.parse_args()

    postures = [p.strip() for p in args.postures.split(',') if p.strip()]
    bad = [p for p in postures if p not in POSTURES]
    if bad:
        print(f'[!] unknown posture(s): {bad}')
        return 1
    os.makedirs(OUT, exist_ok=True)

    print('>>> Stand on FLAT ground facing open terrain, then let go.')
    if not ensure_focus(countdown_s=args.countdown, label='the pitch probe'):
        print('[!] could not focus the game')
        return 1

    rig = Rig(args.sight)
    result = {}
    try:
        if not draw_weapon(rig):
            print('[!] could not get a weapon out and aim — is one in slot 2?')
            return 1
        for p in postures:
            print(f'\n{p}:')
            rows = probe(rig, p, OUT, step=args.step)
            if not rows:
                continue
            tracking = [r['up'] for r in rows if r['tracks']]
            level = next((r['up'] for r in rows
                          if r['horizon_y'] is not None), None)
            result[p] = {
                'level_up': level,
                'tracks_from': tracking[0] if tracking else None,
                'tracks_to': tracking[-1] if tracking else None,
                'steps': rows,
            }
            print(f'  -> level at +{level} counts above the bottom stop; '
                  f'tracks {tracking[0] if tracking else "?"}..'
                  f'{tracking[-1] if tracking else "?"}')
    finally:
        try:
            rig.ensure_posture('standing')
        except Exception:
            pass
        rig.close()

    if result:
        json.dump(result, open(STORE, 'w', encoding='utf-8'), indent=2)
        print(f'\nwrote {os.path.relpath(STORE)}')
        print(f'screenshots -> {os.path.relpath(OUT)}  (check that the one at '
              f'level really looks level)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
