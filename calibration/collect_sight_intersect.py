"""Photograph each SIGHT PICTURE through several view angles, and intersect.

    pixi run sight-intersect --plan                 # what it would do, no game
    pixi run sight-intersect --sights red_dot,holo  # drive it
    pixi run sight-intersect --solve <stamp>        # offline, intersect + write

WHAT THIS IS FOR. `ads_detector` answers "am I scoped?" from the hip
crosshair's ABSENCE, and an absence criterion says yes for every reason the
thing might not be drawn -- an empty-handed character draws no crosshair and
reads "scoped" forever. What is missing is the other half: a POSITIVE picture
of each sight, so the question becomes "is THIS sight on screen".

THE METHOD IS THE ONE THE SLOT TILES AND THE 库存 ROWS ALREADY USE. Hold the
thing still, MOVE THE WORLD, keep the bytes that did not change. Here the
"thing" is the sight overlay, which is drawn at fixed screen coordinates, and
the world is whatever the view is pointed at. Turning the view is free in this
state -- unlike the 库存 collector, which had to shut Tab to turn at all,
because with the panel up raw counts land on the cursor.

⚠ ONE BANK PER SIGHT, because they are not one picture. A red dot draws a dot,
a holo draws a ring, a 4x draws a crosshair inside a vignette, the VSS draws
its own PSO-1. They are currently collapsed into a single boolean, and the
per-sight `ads_end=False` rates say that collapse is where the trouble is:

    2x / 3x / 4x    0%        red_dot   44.5%       integral sights   84-93%

⚠ NO BLOB CRITERION HERE, and that is a real difference from the 库存 bank. A
row icon is one connected shape, so `largest_blob_frac` is a good "did this
collapse to scatter" test. A RETICLE IS NOT: four ticks at the compass points,
or a ring plus a centre dot, are legitimately several components. Copying that
gate across would have rejected exactly the sights that draw the most
distinctive picture. What survives here is the kept-fraction floor and the
did-the-world-move gate.

⚠ AND THE HOST WEAPON IS A VARIABLE, unresolved on purpose in this first pass.
At 1x the gun body is drawn and it does not move with the view, so it survives
the intersection and lands in the template -- which makes that template
weapon-specific. `detector/CLAUDE.md` already measured this on the scope SLOT
tiles (cross-gun alpha differs 32.4 against 0.9-6.3 for every other slot) and
the fix there was a second HOST, not a second backdrop. The same fix applies
here and is not done yet: `--weapon` is recorded on every batch so a later run
can intersect across two.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from control.session import ensure_ready
from control.spawner import SpawnerControl
from control.inventory import InventoryControl
from capture.cropper import capture_screen
from calibration.sweep import Rig
from calibration.collect_timed import ensure_sight
from control.stock import ensure_weapon_in_hand
from detector.weapon import INTEGRAL_SIGHT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'calibration', 'artifacts', 'sight_intersect')
TMPL_OUT = os.path.join(ROOT, 'data', 'templates', 'pubg_assets', 'sight')

# (yaw, pitch) sent BEFORE each shot, cumulative. Yaw-dominant on purpose: a
# horizontal sweep passes entirely different scenery, which is the axis the
# 库存 collector measured its own gate against (19.9-22.8 mean |diff| on a
# 600-count turn), while looking further up or down mostly shows more of the
# same sky or the same ground.
#
# ⚠ THE FIRST SHOT TURNS TOO. A batch whose first frame is "wherever the last
# thing left the view" is a batch whose first frame is not reproducible.
SHOT_TURNS = ((700, 0), (700, -120), (700, 120), (700, 0), (700, -120),
              (700, 120))

# The world must actually have changed between neighbouring shots. WITHOUT THIS
# THE INTERSECTION CANNOT FAIL: two identical frames intersect to a perfect
# copy of themselves, sight and scenery alike, and every downstream check
# passes. Measured over the whole frame here rather than a strip, so it is not
# the 库存 number and must not be copied from it.
BACKDROP_MOVE_MIN = 8.0

# Below this fraction of surviving pixels the intersection has collapsed and
# what is left is coincidence. Deliberately far below any real reticle rather
# than tuned near one -- an absolute pixel floor is the mistake this repository
# already made once, on `uzi_stock`.
KEEP_MIN_FRAC = 0.002

STATES = ('ads', 'hip')


def sights_for(weapon):
    """The sight keys worth shooting on this weapon. -> [key]

    A gun with an INTEGRAL sight has no scope slot to fit anything into, so
    the only picture it can produce is its own -- and that is the picture we
    want, since those guns carry the highest `ads_end=False` rates in the
    store (vss_pso1 83.7%, p90_integral 92.9%).
    """
    if weapon in INTEGRAL_SIGHT:
        return [INTEGRAL_SIGHT[weapon]]
    return None


def reach(rig, state):
    """Put the character in `state` and PROVE it, statically. -> bool

    ⚠ THE PROOF IS TAKEN WHILE NOTHING IS MOVING, which is the whole design of
    the experiment this feeds. `ads_detector` is measured at 387/387 on a still
    character and 0.79 on a firing one, so a state established here is
    trustworthy in a way the same reading mid-burst is not.
    """
    if state == 'ads':
        return bool(rig.gun.ensure_ads())
    if state == 'hip':
        return bool(rig.gun.ensure_hip())
    raise ValueError(f'unknown state {state!r}')


def shoot(rig, stamp, sight, state):
    """One full-screen frame per entry in SHOT_TURNS. -> [dict] | None"""
    shots = []
    for idx, (yaw, pitch) in enumerate(SHOT_TURNS):
        rig.view.turn(yaw, pitch)
        time.sleep(0.25)               # the view coasts; let it settle
        # ⚠ RE-PROVE THE STATE AFTER EVERY TURN. ADS is a toggle and turning
        # does not cancel it, but "does not" is a belief and this is the one
        # run whose entire point is not to hold beliefs about this state.
        if not reach(rig, state):
            print(f'      [!] lost {state} after turn {idx} — batch void')
            return None
        frame = capture_screen()
        name = f'{stamp}__{sight}__{state}__v{idx}.png'
        cv2.imwrite(os.path.join(OUT, name), frame)
        shots.append({'idx': idx, 'yaw': yaw, 'pitch': pitch,
                      'name': name, 'frame': frame})
        print(f'      shot {idx} (yaw +{yaw}, pitch {pitch:+}) -> {name}')
    return shots


def backdrop_move(shots):
    """Smallest mean |difference| between NEIGHBOURING shots. -> float

    SMALLEST, not the mean over all pairs: the intersection is only as clean as
    its weakest pair, and two shots that happen to land on the same scenery
    contribute nothing however far the others turned. An average hides exactly
    that.
    """
    if len(shots) < 2:
        return 0.0
    return min(float(np.abs(a['frame'].astype('int16') -
                            b['frame'].astype('int16')).mean())
               for a, b in zip(shots, shots[1:]))


def solve(stamp, write=False):
    """Intersect every (sight, state) in a batch. OFFLINE. -> int"""
    with open(os.path.join(OUT, f'{stamp}__meta.json'), encoding='utf-8') as f:
        meta = json.load(f)
    os.makedirs(TMPL_OUT, exist_ok=True)
    kept = 0
    for grp in meta['groups']:
        sight, state = grp['sight'], grp['state']
        moved = grp.get('backdrop_move', 0.0)
        if moved < BACKDROP_MOVE_MIN:
            print(f'  {sight}/{state:4} VOID: the world moved {moved:.1f}, '
                  f'under {BACKDROP_MOVE_MIN}. The intersection would keep the '
                  f'scenery and every check after it would pass.')
            continue
        cells = [cv2.imread(os.path.join(OUT, s)) for s in grp['shots']]
        if any(c is None for c in cells) or len(cells) < 2:
            print(f'  {sight}/{state:4} MISSING a frame — skipped')
            continue
        keep = np.logical_and.reduce([(c == cells[0]).all(axis=2)
                                      for c in cells[1:]])
        icon = np.dstack([cells[0], keep.astype('uint8') * 255])
        icon[~keep] = 0
        frac = float(keep.mean())
        out = os.path.join(TMPL_OUT, f'{sight}__{state}.png')
        exists = os.path.exists(out)
        if frac < KEEP_MIN_FRAC:
            verdict = f'only {frac:.3%} survived — refused'
        elif exists:
            verdict = 'already on disk — NOT overwritten'
        else:
            verdict = 'ok'
        print(f'  {sight:14}/{state:4} kept {frac:7.3%} of the screen '
              f'({int(keep.sum()):7} px)  world moved {moved:5.1f}  {verdict}')
        if write and frac >= KEEP_MIN_FRAC and not exists:
            cv2.imwrite(out, icon)
            kept += 1
    print(f'\n{kept} template(s) written to '
          f'{os.path.relpath(TMPL_OUT, ROOT)}' if write else
          '\n(nothing written — pass --write)')
    return 0


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', default='m416')
    # ⚠ THESE ARE control.kitting.SIGHT_SCOPE KEYS, not catalogue part keys.
    # `scope_4x` is the PART; `4x` is the sight this repository fits and keys
    # K on, and ensure_sight only knows the second. The first run of this file
    # passed the part names and lost four of its six jobs to
    # "'scope_2x' is not in SIGHT_SCOPE" -- a refusal that named its own cause,
    # which is the only reason it cost minutes instead of a batch.
    ap.add_argument('--sights', default='red_dot,2x,3x,4x')
    ap.add_argument('--integral', default='vss',
                    help='comma-separated guns whose own sight to shoot too, '
                         'or "" for none')
    ap.add_argument('--plan', action='store_true')
    ap.add_argument('--solve', metavar='STAMP')
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--countdown', type=int, default=5)
    args = ap.parse_args()

    if args.solve:
        return solve(args.solve, write=args.write)

    fitted = [s.strip() for s in args.sights.split(',') if s.strip()]
    integral = [g.strip() for g in args.integral.split(',') if g.strip()]
    jobs = [(args.weapon, s) for s in fitted]
    for g in integral:
        own = sights_for(g)
        if not own:
            print(f'[!] {g} has no integral sight in INTEGRAL_SIGHT — skipped')
            continue
        jobs.append((g, own[0]))
    if args.plan:
        print(f'{len(jobs)} (weapon, sight) job(s) x {len(STATES)} state(s) '
              f'x {len(SHOT_TURNS)} view(s) = '
              f'{len(jobs) * len(STATES) * len(SHOT_TURNS)} frames')
        for w, s in jobs:
            print(f'  {w:8} {s}')
        return 0

    ready = ensure_ready(label='sight intersect', countdown_s=args.countdown)
    if not ready['ok']:
        print(f'[!] ABORT: not ready — failed at {ready["failed"]!r}')
        return 1

    stamp = time.strftime('%Y%m%d_%H%M%S')
    os.makedirs(OUT, exist_ok=True)
    rig = Rig('red_dot')
    sc, ac = SpawnerControl(verbose=False), InventoryControl(verbose=False)
    groups = []
    try:
        for weapon, sight in jobs:
            print(f'\n── {weapon} / {sight}')
            # ⚠ ensure_weapon_in_hand, NOT a bare hold(): entering the range
            # EMPTIES the rack, and ensure_ready re-enters whenever it finds
            # the game back in the lobby, so "there was a gun a minute ago" is
            # not a reason to skip it. It also refuses two same-named guns,
            # which is the state nothing downstream can describe.
            slot = ensure_weapon_in_hand(ac, sc, weapon=weapon, verbose=False)
            if not slot:
                print(f'   [!] could not get {weapon} in hand — skipped')
                continue
            if weapon not in INTEGRAL_SIGHT:
                worn, asset = ensure_sight(ac, sc, slot, weapon, sight)
                if worn is None:
                    print(f'   [!] {asset} — skipped')
                    continue
                print(f'   wearing {worn} ({asset})')
            for state in STATES:
                if not reach(rig, state):
                    print(f'   [!] could not reach {state} — skipped')
                    continue
                shots = shoot(rig, stamp, sight, state)
                if shots is None:
                    continue
                mv = backdrop_move(shots)
                print(f'      world moved {mv:.1f} between neighbours '
                      f'(need {BACKDROP_MOVE_MIN})')
                groups.append({'weapon': weapon, 'sight': sight,
                               'state': state, 'backdrop_move': mv,
                               'turns': [(s['yaw'], s['pitch']) for s in shots],
                               'shots': [s['name'] for s in shots]})
    finally:
        meta = {'stamp': stamp, 'groups': groups,
                'note': 'intersect with --solve; each group is one (weapon, '
                        'sight, state). The HOST WEAPON is recorded because at '
                        '1x the gun body survives the intersection — see the '
                        'module docstring.'}
        with open(os.path.join(OUT, f'{stamp}__meta.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(meta, f, indent=1, ensure_ascii=False)
        try:
            ac.close()
        except Exception:
            pass
        rig.close()
    print(f'\n{len(groups)} group(s) under {os.path.relpath(OUT, ROOT)}')
    print(f'next:  pixi run sight-intersect --solve {stamp} --write')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
