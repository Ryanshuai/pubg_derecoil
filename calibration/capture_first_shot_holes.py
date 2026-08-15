"""Fire the volleys and KEEP THE FRAMES. No judgement, no analysis. Autonomous.

    pixi run python calibration/capture_first_shot_holes.py
    pixi run python calibration/capture_first_shot_holes.py --weapons m416,aug

WHY THIS IS SEPARATE FROM DECIDING WHERE THE HOLES ARE
------------------------------------------------------
Nine successive criteria for "which dark blob in this diff is the bullet hole"
were built and measured against real frames in one session, and every one of
them was defeated by the same thing: the weapon and the HUD are in both frames
and they move between them. A red dot's ring, a 6x rim, the receiver's top
edge, an iron rear sight's notches, the minimap redrawing -- each lands in the
diff as a compact dark blob the size of an impact, and several sit about as far
from the aim as the impact does.

⚠ THE PICTURES WERE NEVER THE PROBLEM. A hole is obvious to a human eye in a
single glance; three separate frames were read correctly by eye that session,
including one where the automatic criterion had confidently returned a rear
sight notch. So this file stops trying to automate the judgement and does what
`detector/zeroing_detector.py` already had to do for ADS:

    docs/ads_eyeball_labels.json -- 60 labels read off the frames BY EYE,
    because every automatic label available for that question was produced by
    one of the detectors under test.

Same shape here. The volley is machinery; the reading is a human.

⚠ AND THE SPLIT ALSO BUYS THE SESSION. PUBG's idle timer counts the operator's
THINKING time, and three sessions died in the gaps where frames were being
looked at. Firing everything first and looking afterwards removes the gaps.

WHAT IT SAVES, per (weapon, arm):

    <w>_<arm>_base.png    weapon HOLSTERED, before any round
    <w>_<arm>_shot.png    weapon HOLSTERED, after three rounds
    <w>_scene.png         one look at where the character is standing

Holstered on both sides so the gun is not in either frame -- the fix that no
amount of thresholding could substitute for.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from calibration.probe_first_shot_holes import (JUMP_SCHOOL_XY,  # noqa: E402
                                                in_hand, strip_optic)
from calibration.probe_hole_pattern import OUT_DIR                # noqa: E402
from calibration.sweep import Rig                                 # noqa: E402
from capture.cropper import capture_screen                        # noqa: E402
from control.inventory import InventoryControl                    # noqa: E402
from control.lobby import LobbyControl                            # noqa: E402
from control.map import MapControl                                # noqa: E402
from control.spawner import SpawnerControl                        # noqa: E402
from control.stock import ensure_weapon_in_hand                   # noqa: E402

CAP_DIR = os.path.join(OUT_DIR, 'eyeball')


def volley(rig, n, tag):
    """Holstered baseline, n rounds, holstered readback. -> rounds actually fired."""
    if not rig.gun.ensure_stowed(True, rig.fire.read_ammo):
        return -1
    rig.flush(4)
    cv2.imwrite(os.path.join(CAP_DIR, f'{tag}_base.png'), capture_screen())
    if not rig.gun.ensure_stowed(False, rig.fire.read_ammo):
        return -1
    rig.gun.ensure_ads()
    rig.flush(4)
    fired = 0
    for _ in range(n):
        r = rig.fire.fire_once()
        fired += (r['fired'] or 0)
        time.sleep(0.2)
    time.sleep(0.4)
    rig.gun.ensure_hip()
    rig.gun.ensure_stowed(True, rig.fire.read_ammo)
    rig.flush(4)
    cv2.imwrite(os.path.join(CAP_DIR, f'{tag}_shot.png'), capture_screen())
    return fired


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapons', default='m416,mp5k,aug,vector')
    ap.add_argument('--rounds', type=int, default=3)
    a = ap.parse_args()
    os.makedirs(CAP_DIR, exist_ok=True)
    weapons = [w.strip() for w in a.weapons.split(',') if w.strip()]

    with LobbyControl() as lc:
        if not lc.ensure_in_match(launch=True)['ok']:
            print('[!] could not get into a match')
            return 1
    ac = InventoryControl(verbose=False)
    # ⚠ UN-STOW FIRST. A previous run ends with the weapon put away, and
    # `ensure_weapon_in_hand` proves the hold by the AMMO COUNTER -- which PUBG
    # hides with the gun down -- so it reads "in the rack and will not come to
    # hand" and refuses. One whole run was lost to that.
    warm = Rig('red_dot')
    try:
        warm.gun.ensure_stowed(False, warm.fire.read_ammo)
    finally:
        warm.close()

    # ⚠ TELEPORT ONCE, NOT PER WEAPON. The measurement compares arms, and the
    # ballistic offset in pixels depends on the range to the surface -- so every
    # volley has to be fired from the SAME spot. Re-placing between weapons
    # would make each gun's pair internally valid and the guns incomparable.
    with MapControl() as mc:
        mc.ensure_map(True)
        mc.pointer.click_at(*JUMP_SCHOOL_XY)
        time.sleep(1.2)
        mc.ensure_map(False)

    done = []
    for w in weapons:
        with SpawnerControl() as sc:
            ac.clear_rack()
            slot = ensure_weapon_in_hand(ac, sc, weapon=w)
        if slot is None:
            print(f'{w}: REFUSING — could not get one into the rack')
            continue
        if not strip_optic(ac, slot):
            print(f'{w}: skipped — the scope slot would not clear')
            continue
        rig = Rig('red_dot')
        try:
            if not in_hand(rig, ac):
                print(f'{w}: no ammo counter — nothing held')
                continue
            cv2.imwrite(os.path.join(CAP_DIR, f'{w}_scene.png'), capture_screen())
            for arm, on in (('on', True), ('off', False)):
                if on:
                    rig.fire.arm(w)
                else:
                    rig.fire.disarm()
                rig.fire.top_up()
                fired = volley(rig, a.rounds, f'{w}_{arm}')
                print(f'{w:<7} {arm:<3} fired {fired}/{a.rounds}')
                done.append((w, arm, fired))
        finally:
            rig.close()

    print(f'\n{len(done)} volley(s) -> {CAP_DIR}')
    print('Nothing here decided where a hole is. Read the diffs.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
