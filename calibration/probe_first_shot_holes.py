"""The FIRST shot, measured by the wall instead of by the screen. Autonomous.

    pixi run python calibration/probe_first_shot_holes.py --weapon m416

THE QUESTION. `MODEL.md` fits `y_true(t)` from the view tracker, and the tracker
reads a displacement per FRAME PAIR -- so the opening kick, which lands inside
one or two frames at 144 fps, is exactly where it is weakest. Worse, the stored
`y_obs` starts from a `cumsum`, so t=0 reads 0 BY CONSTRUCTION, not by
measurement. Bullet holes owe none of that: no tracker, no K, no clock.

THE METHOD, and it is the operator's, not this file's:

    strip the optic  ->  baseline screenshot  ->  fire THREE rounds
      ->  align the readback to the baseline  ->  diff  ->  the dark blobs

⚠ THREE ROUNDS, NOT ONE, AND THAT IS THE WHOLE DESIGN. A single hole is one
compact dark blob, and so is the lower arc of a red dot's ring, a sliver of the
receiver, or a patch of registration residue -- eight different criteria were
tried against that ambiguity (area floor, opening, was-it-bright-before,
top-hat, fill ratio, below-the-aim, gun-region exclusion, brightening partner)
and NONE of them separated the two. Three rounds turn it into a different
question: a GROUP of similar compact blobs in a small cluster is a signature
the weapon's own edges cannot fake, because the weapon contributes its edge
once, not three times in a line.

⚠ AND THE OPTIC COMES OFF. The red dot's ring sweeps across the aim area as the
gun idles, and its arcs are the same size, the same darkness, and the same
distance from the aim as the impact. On irons there is no ring to confuse.

⚠ CONCRETE ONLY. Measured this session: dirt, sand and grass record nothing a
diff can find, and every "reading" taken on them was the criterion latching
onto the weapon. The one surface confirmed to hold a readable decal is the
Jump School wall, so this script keeps re-placing the character until a TEST
VOLLEY actually produces a group -- "can this surface record a hole" is a
measurement here, not an assumption.

⚠ IT RUNS WITHOUT A HUMAN IN THE MIDDLE ON PURPOSE. PUBG's idle timer counts
the operator's THINKING time, and three sessions died tonight in the gaps where
frames were being looked at. Everything is captured to disk and read afterwards.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from calibration.probe_hole_pattern import (BAND_EXCLUDE,  # noqa: E402
                                            OUT_DIR, _overlaps, align,
                                            find_band)
from calibration.sweep import Rig                             # noqa: E402
from capture.cropper import capture_screen                    # noqa: E402
from control.inventory import InventoryControl                # noqa: E402
from control.lobby import LobbyControl                        # noqa: E402
from control.map import MapControl                            # noqa: E402
from control.spawner import SpawnerControl                    # noqa: E402
from control.stock import ensure_weapon_in_hand               # noqa: E402

# The practice-area list on the in-match map: clicking a row teleports. Jump
# School is the parachute one and its wall is the surface this needs.
# ⚠ THE CLICK IS NOT RELIABLE -- the same coordinate has landed at the 200m
# lane, the Race Track and the docks -- which is why the caller VERIFIES by
# firing rather than trusting it.
JUMP_SCHOOL_XY = (251, 1060)

# A 5.56 decal measured 550-1100 px on concrete at this range; anything much
# bigger is scenery or HUD, not an impact.
BLOB_MIN, BLOB_MAX, FILL_MIN = 150, 2500, 0.35
GROUP_MIN = 2          # blobs a test volley must leave before the spot is used
GROUP_SPAN = 250       # ...within this many px of each other


def blobs(base, shot):
    """Compact darkenings between two frames, in the baseline's coordinates."""
    band, resp = find_band(base, shot)
    if band is None:
        return [], None, resp
    g0, g1, shift, resp = align(base, shot, band)
    dark = np.clip(g0 - g1, 0, 255).astype(np.uint8)
    _, m = cv2.threshold(dark, 18, 255, cv2.THRESH_BINARY)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, _lab, st, ce = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, n):
        ar = int(st[i, cv2.CC_STAT_AREA])
        x0, y0 = int(st[i, cv2.CC_STAT_LEFT]), int(st[i, cv2.CC_STAT_TOP])
        w_, h_ = int(st[i, cv2.CC_STAT_WIDTH]), int(st[i, cv2.CC_STAT_HEIGHT])
        bw = w_ * h_
        if not (BLOB_MIN <= ar <= BLOB_MAX and bw and ar / bw >= FILL_MIN):
            continue
        # ⚠ THE HUD IS NOT THE WORLD, AND LEAVING IT IN MADE THE GROUP TEST
        # ALWAYS SUCCEED. Two runs reported a "group" centred on the minimap at
        # 29400 and 33741 px -- the ammo bar and the map redrawing between the
        # two frames. This filter existed in the throwaway analysis that found
        # the first real group and was never carried into the script, which is
        # the difference between a check that ran once and a check that runs.
        if any(_overlaps((y0, y0 + h_, x0, x0 + w_), bx) for bx in BAND_EXCLUDE):
            continue
        out.append((ar, float(ce[i][0]), float(ce[i][1])))
    out.sort(reverse=True)
    return out, shift, resp


def group(bs):
    """The tightest cluster of >=GROUP_MIN blobs. -> (members, span) or (None, None).

    A bullet group is small and its members are alike. The weapon's own edges
    are one blob, far from the others, so a cluster test rejects them without
    needing to know what they are.
    """
    for i, (_a, x, y) in enumerate(bs):
        near = [b for b in bs
                if abs(b[1] - x) <= GROUP_SPAN and abs(b[2] - y) <= GROUP_SPAN]
        if len(near) >= GROUP_MIN:
            ys = [b[2] for b in near]
            return near, max(ys) - min(ys)
    return None, None


def volley(rig, n, tag):
    """Baseline, n rounds, readback. -> (blobs, shift, resp, fired)"""
    # ⚠ BOTH FRAMES ARE TAKEN WITH THE WEAPON PUT AWAY, and that is the whole
    # fix. Nine criteria for "which dark blob is the hole" were defeated by the
    # gun being IN both frames and moving between them -- ring, rim, receiver
    # edge, iron notches, each a compact dark blob the size of an impact, each
    # about as far from the aim as the impact is. A frame with no weapon in it
    # has nothing to confuse. The view is not touched, so the two frames still
    # describe the same aim.
    # ⚠ VERIFIED, NOT COUNTED. `X` is a toggle and the first version pressed it
    # a fixed number of times: the compensated arm fired ZERO rounds with the
    # weapon put away and still reported a group, having found HUD clutter.
    # `ensure_stowed` asks the ammo counter, which PUBG hides with the gun down.
    if not rig.gun.ensure_stowed(True, rig.fire.read_ammo):
        return [], None, 0.0, -1
    rig.flush(4)
    base = capture_screen()
    if not rig.gun.ensure_stowed(False, rig.fire.read_ammo):
        return [], None, 0.0, -1
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
    shot = capture_screen()
    os.makedirs(OUT_DIR, exist_ok=True)
    cv2.imwrite(os.path.join(OUT_DIR, f'{tag}_base.png'), base)
    cv2.imwrite(os.path.join(OUT_DIR, f'{tag}_shot.png'), shot)
    bs, shift, resp = blobs(base, shot)
    return bs, shift, resp, fired


def in_hand(rig, ac, tries=4):
    """Ammo digits are the proof the gun is out. Absence is not evidence."""
    for _ in range(tries):
        n = rig.fire.read_ammo()
        if n:
            return n
        ac.hold(1)
        time.sleep(1.0)
    return None


def strip_optic(ac, slot, tries=3):
    """Take the optic off and PROVE the slot is empty. -> True / False

    ⚠ THE READBACK WAS PRINTED AND IGNORED, and that is the root CLAUDE.md's
    second law in three words. One run announced `optic stripped, slot reads
    'Upper_Scope6x_C'` -- a spawned gun wears whatever the backpack can autofit,
    so a 6x had gone on while the line above said it had come off -- and then
    fired eight rounds through a scope whose rim is exactly the confuser this
    whole approach exists to remove. The record described an object that was not
    the one being measured, nothing raised, and every number after it was about
    a different gun than the one in the log.
    """
    for _ in range(tries):
        with ac.tab_up():
            ac.unequip(slot, 'scope')
            got = (ac.loadout()['slots'] or {}).get(slot, {}).get('scope')
        if not got:
            print('optic stripped, slot reads empty')
            return True
        print(f'  [!] slot still reads {got!r} — trying again')
    print(f'[!] REFUSING: could not clear the scope slot (reads {got!r}). '
          f'The optic is the thing this measurement removes; carrying on '
          f'would measure a different gun than the one reported.')
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', default='m416')
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--venues', type=int, default=4,
                    help='how many times to re-place the character before '
                         'giving up on finding a surface that records a hole')
    a = ap.parse_args()

    with LobbyControl() as lc:
        if not lc.ensure_in_match(launch=True)['ok']:
            print('[!] could not get into a match')
            return 1
    ac = InventoryControl(verbose=False)
    # ⚠ UN-STOW BEFORE ASKING WHO IS HOLDING WHAT. This probe puts the weapon
    # away to get it out of the frame, and a run that ends there leaves the next
    # one starting there. `ensure_weapon_in_hand` proves the hold by the AMMO
    # COUNTER, and PUBG hides that with the gun down -- so it reads "in the rack
    # and will not come to hand" and refuses, which is the right call on the
    # evidence it has. Measured: one whole run lost to exactly this.
    warm = Rig('red_dot')
    try:
        warm.gun.ensure_stowed(False, warm.fire.read_ammo)
    finally:
        warm.close()
    with SpawnerControl() as sc:
        slot = ensure_weapon_in_hand(ac, sc, weapon=a.weapon)
    if slot is None:
        print(f'[!] REFUSING: no {a.weapon} in the rack')
        return 2
    if not strip_optic(ac, slot):
        return 2

    for venue in range(a.venues):
        with MapControl() as mc:
            mc.ensure_map(True)
            mc.pointer.click_at(*JUMP_SCHOOL_XY)
            time.sleep(1.2)
            mc.ensure_map(False)
        rig = Rig('red_dot')
        try:
            n = in_hand(rig, ac)
            if not n:
                print(f'venue {venue}: no ammo counter — nothing held')
                continue
            bs, shift, resp, fired = volley(rig, a.rounds, f'fs{venue}_test')
            g, span = group(bs)
            print(f'venue {venue}: fired {fired}/{a.rounds}  resp {resp:.2f}  '
                  f'blobs {len(bs)}  group {0 if g is None else len(g)}'
                  f'  span {span}')
            for b in bs[:6]:
                print(f'    area {int(b[0]):>5}  at ({b[1]:.0f}, {b[2]:.0f})')
            if g is None:
                continue

            print('\n-- this surface records holes; measuring --')
            for arm, on in (('on', True), ('off', False)):
                if not on:
                    rig.fire.disarm()
                rig.fire.top_up()
                bs2, sh2, r2, f2 = volley(rig, a.rounds, f'fs{venue}_{arm}')
                g2, sp2 = group(bs2)
                ys = [b[2] for b in g2] if g2 else []
                print(f'  {arm:<3} fired {f2}  resp {r2:.2f}  '
                      f'group {len(ys)}  y mean '
                      f'{np.mean(ys) if ys else float("nan"):.1f}  span {sp2}')
                for b in (g2 or [])[:6]:
                    print(f'    area {int(b[0]):>5}  at ({b[1]:.0f}, {b[2]:.0f})')
            return 0
        finally:
            rig.close()

    print(f'\n[!] {a.venues} placements, none of them a surface that records a '
          f'hole. Everything is in {OUT_DIR}; the frames are the evidence.')
    return 3


if __name__ == '__main__':
    raise SystemExit(main())
