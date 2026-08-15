"""Short groups on a concrete wall: where the rounds ACTUALLY land.

    pixi run python calibration/hole_groups.py --groups 2 --rounds 3
    pixi run python calibration/hole_groups.py --groups 4 --rounds 3 --arms off,off,on,on

WHY SHORT GROUPS, AND WHY SEPARATE ONES
---------------------------------------
The operator's report is about the FIRST bullet: its hole sits 2-3x further
from the group than the rest. Everything this repository measures is camera
motion, and the camera says the opposite -- the first shot is the SMALLEST
(aug: 6.9 counts against ~22 for a later round, measured on isolated single
shots at 155 fps). Bullet holes are the only instrument that can hold both
claims at once, because they are outside the loop's chain entirely.

Three rounds, not forty. Uncompensated, a full magazine walks the view ~1080
counts -- about 1660 px -- so the group leaves the wall long before it ends,
and the gaps that matter are the first two.

Separate groups, because HOLES PERSIST. Two bursts into the same patch of wall
produce one picture that no longer says which round made which hole, and the
decals fade on the game's own schedule rather than ours. So each group gets its
own aim point, yawed away from the last by more than the group is wide.

WHAT IT REFUSES TO DO
---------------------
⚠ It will not fire at a surface `WallDetector` did not accept. Firing into sand
or sky leaves no marks, and an empty diff is pixel-for-pixel identical to a
perfect group -- the failure mode reads as the best possible result. This is
the same rule probe_hole_pattern learned by reporting 1385 moving-cloud marks
as a bullet group.

⚠ And it checks the AMMO COUNTER around every group. "Three rounds went out" is
a claim about the world; the command we sent is not a witness to it. A
swallowed click and a perfect three-round group differ only here.

⚠ ensure_ready() FIRST, ALWAYS. The game idle-kicks after a few minutes with no
input ("You have been logged off due to inactivity"), and on 2026-08-11 a run
that skipped this step read four detectors against that error dialog and got
four different-looking failures, none of which named it.
"""
import argparse
import json
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ⚠ WITHOUT THIS THE FILE DIES ON ITS OWN FIRST WARNING. Windows' console
# codec is cp1252, every ⚠ in here is unencodable, and `teleport_to_wall`
# prints one before it does anything -- so the whole tool crashed at the line
# saying "arrival is NOT proved by that click", which is the most load-bearing
# sentence it has. It is why nothing on this machine had ever run it to the end.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from capture.cropper import DXGISyncGrabber, capture_screen        # noqa: E402
from config import (FIRE_MODE_FOR, SCREEN_H, SCREEN_W,            # noqa: E402
                    fire_mode_for)
from control.session import ensure_ready                           # noqa: E402
from detector.wall_detector import (HUD_BLANK, WallDetector,      # noqa: E402
                                    confirm_shot, find_holes)

OUT = os.path.join('calibration', 'artifacts', 'holes')
# The parachute icon on the training-range map. Clicking it teleports to Jump
# School, whose north face is the only large unbroken concrete slab this
# repository has found. MEASURED 2026-08-11 by clicking it; it is a POINT, not
# one of MAP_RANGE_BOXES' rectangles, which is why goto_range cannot reach it.
JUMP_SCHOOL_MAP_XY = (1148, 476)


def teleport_to_wall(verbose=True):
    """Map click -> Jump School. Must run AFTER ensure_ready, never before:
    re-entering the match resets the character to the main spawn, so a
    teleport taken first is silently undone."""
    from control.map import MapControl
    mc = MapControl()
    try:
        rec = mc.goto_point(JUMP_SCHOOL_MAP_XY)
    finally:
        mc.close()
    time.sleep(2.5)
    if verbose:
        print(f'  map click at {JUMP_SCHOOL_MAP_XY}: ok={rec["ok"]}'
              + (f'  {rec["error"]}' if rec.get('error') else ''))
        print('  ⚠ arrival is NOT proved by that click — the wall survey below'
              ' is what proves it')
    return rec


AIM_BOX = (150, 200, 1500, 950)          # x, y, w, h -- a big bite of the wall


def _shift(a_bgr, b_bgr):
    """(dx, dy) px between two full frames, over AIM_BOX."""
    x, y, w, h = AIM_BOX
    a = cv2.cvtColor(a_bgr[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY).astype(np.float32)
    b = cv2.cvtColor(b_bgr[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY).astype(np.float32)
    (sx, sy), _ = cv2.phaseCorrelate(a, b, cv2.createHanningWindow((w, h),
                                                                  cv2.CV_32F))
    return sx, sy


def aim_at(rig, target, probe=60):
    """Put the crosshair on `target` (a screen point), measuring the sign first.

    ⚠ THE SIGN OF `turn` IS NOT ASSUMED, IT IS MEASURED, and the reason is that
    it is a one-line assumption whose failure looks like a miss: fire at the
    wrong side of the wall and the diff is empty, which is the same picture as
    a perfect group. So a small known turn is sent and the scene is read back
    for how far and which way it actually moved.

    ⚠ AND IT IS READ OFF A BIG FRAME CROP, NOT THE TRACKER'S PATCHES. The first
    version used ViewTracker, which is the purpose-built displacement sensor --
    and it returned dx = -0.00 px/count against a working dy = -1.68. Its seven
    columns are 128 px wide and this wall is a flat slab: it has vertical
    staining to lock onto and almost no horizontal structure, so a yaw is
    genuinely unresolvable there. The door, the poster and the seams are what
    make yaw measurable, and they are only inside a wide crop.

    Open loop after that, deliberately: the caller re-surveys and checks the
    crosshair landed on wall. Arriving is proved against the world, not against
    the arithmetic that aimed.
    """
    f0 = capture_screen()
    rig.view.turn(probe, 0)
    time.sleep(0.45)
    f1 = capture_screen()
    rig.view.turn(0, probe)
    time.sleep(0.45)
    f2 = capture_screen()
    dx_per = _shift(f0, f1)[0] / probe
    dy_per = _shift(f1, f2)[1] / probe
    if abs(dx_per) < 0.05 or abs(dy_per) < 0.05:
        return None, (dx_per, dy_per)
    cx, cy = SCREEN_W // 2, SCREEN_H // 2
    # The probes moved the world, so the target moved with it.
    tx = target[0] + dx_per * probe
    ty = target[1] + dy_per * probe
    yaw = (cx - tx) / dx_per
    pitch = (cy - ty) / dy_per
    rig.view.turn(int(round(yaw)), int(round(pitch)))
    time.sleep(0.5)
    return (int(round(yaw)), int(round(pitch))), (dx_per, dy_per)



def measure_scale(rig, probe=60):
    """(px per count) for yaw and pitch, measured once. See aim_at."""
    f0 = capture_screen()
    rig.view.turn(probe, 0)
    time.sleep(0.45)
    f1 = capture_screen()
    rig.view.turn(0, probe)
    time.sleep(0.45)
    f2 = capture_screen()
    return _shift(f0, f1)[0] / probe, _shift(f1, f2)[1] / probe


def settle_aim(rig, wd, centre, scale, tries=12, step_cap=70, half=70):
    """Nudge until the strip ABOVE the crosshair is wall. Bounded, small steps.

    ⚠ THIS REPLACES "find the best wall and turn to it", which on 2026-08-11
    computed a 553-count swing (~1300 px) to reach a region at the screen edge
    and took the whole wall out of view. survey() still supplies the
    DIRECTION -- it is good at that -- but the magnitude is capped, and the
    thing that decides when to stop is at(): the surface actually under the
    crosshair. Direction from the map, arrival from the ground.

    ⚠ `tries` AND `step_cap` TOGETHER SET THE REACH, and the default used to be
    6 x 70 = 420 counts. Measured 2026-08-12: akm's survey pointed at a region
    722 px away, which at the 1.70 px/count of that spot is 425 counts -- it ran
    out of tries by ONE step and reported "could not get onto clean concrete"
    while looking at a wall it had correctly found. Four of five guns in that
    batch failed the same way. The survey was never wrong; the walk was short.

    -> (ok, frame, frac, why)
    """
    dx_per, dy_per = scale
    frame = capture_screen()
    ok, frac, why = wd.at(frame, centre, half=half)
    for _ in range(tries):
        if ok:
            return True, frame, frac, why
        s = wd.survey(frame)
        target = s.aim if s.ok else (centre[0] - 260, centre[1])
        yaw = (centre[0] - (target[0])) / dx_per
        pitch = (centre[1] - (target[1])) / dy_per
        yaw = max(-step_cap, min(step_cap, yaw))
        pitch = max(-step_cap, min(step_cap, pitch))
        if abs(yaw) < 2 and abs(pitch) < 2:
            break
        rig.view.turn(int(round(yaw)), int(round(pitch)))
        time.sleep(0.45)
        frame = capture_screen()
        ok, frac, why = wd.at(frame, centre, half=half)
    return ok, frame, frac, why


def _save(out, meta, rows):
    """Every group's numbers AND the conditions they were taken under.

    ⚠ THE CONDITIONS ARE THE PART THAT CANNOT BE RE-DERIVED. The frames can be
    re-detected offline with any threshold anyone likes; whether compensation
    was armed, which of two cyclic rates the gun was in, and what the ammo
    counter said are not in the pixels. A ratio without them is a number with
    no subject -- which is this repository's second cross-layer law, in the one
    layer whose whole job is to write measurements down.
    """
    with open(os.path.join(out, 'groups.json'), 'w', encoding='utf-8') as fh:
        json.dump({**meta, 'groups': rows}, fh, indent=1, ensure_ascii=False)


def hole_marks(before, after, region=None, thresh=22, area_lo=8, area_hi=900):
    """New marks between two frames, as (x, y, area), lowest on screen first.

    Ordered by y DESCENDING because a group walks UP: the first round is the
    lowest hole. That ordering is the whole measurement -- the gaps between
    consecutive holes ARE the per-round aim change, and getting the order
    backwards inverts the one number this run exists to produce.

    ⚠ THREE FILTERS, and the first run had none of them: it reported 3850
    "holes", every one at y=1438.5 -- the bottom row of the screen. A raw diff
    of two combat frames is muzzle flash, smoke, the weapon's own animation,
    the HUD and a changing sky; the holes are a rounding error inside it.

      region   only inside the surveyed wall. Everything that made those 3850
               marks lives outside it.
      darker   a decal DARKENS concrete. Smoke and flash brighten. This is the
               one filter that separates the mark from the event that made it.
      size     a hole is tens of px; smoke is thousands, sensor noise is ones.
    """
    b = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY).astype(np.int16)
    a = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY).astype(np.int16)
    darker = ((b - a) > thresh).astype(np.uint8)
    if region is not None:
        keep = np.zeros_like(darker)
        x, y, w, h = region
        keep[y:y + h, x:x + w] = 1
        darker *= keep
    for x, y, w, h in HUD_BLANK:
        darker[max(0, y):y + h, max(0, x):x + w] = 0
    darker = cv2.morphologyEx(darker, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _lab, stats, cent = cv2.connectedComponentsWithStats(darker, 8)
    out = []
    for i in range(1, n):
        if not (area_lo <= stats[i, 4] <= area_hi):
            continue
        out.append((float(cent[i][0]), float(cent[i][1]), int(stats[i, 4])))
    out.sort(key=lambda p: -p[1])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', default='aug')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--groups', type=int, default=2)
    ap.add_argument('--arms', default='off,off',
                    help='comma list of on/off, one per group, fired in order')
    ap.add_argument('--yaw-step', type=int, default=-120,
                    help='counts of yaw between groups. NEGATIVE = left, and '
                         'that sign is measured: the walls right edge sits at '
                         'x~2020 and group 0 aims near x=1720, so +140 counts '
                         '(235 px) walked group 1 off the wall and onto the '
                         'ROAD -- which is grey, neutral and flat, so the wall '
                         'test accepted it and the group left no holes.')
    ap.add_argument('--climb-px', type=int, default=90,
                    help='headroom the group needs above the aim point. '
                         'MEASURED, not guessed: aug uncompensated climbs '
                         '6.9+10.8+12.3 = 30 counts over three rounds, which '
                         'is 47 px at K=1.54; the rest is margin.')
    ap.add_argument('--margin-px', type=int, default=35,
                    help='clear wall demanded around the group. 60 was a guess '
                         'and it refused a real 324x170 band; three rounds '
                         'climb 47 px measured, so 35 is margin, not slack.')
    ap.add_argument('--settle-tries', type=int, default=12,
                    help='nudges allowed to walk the aim onto clean wall. Each '
                         'is capped at 70 counts, so this IS the reach: 6 gave '
                         '420 counts and akm needed 425, which is how four of '
                         'five guns in one batch reported "no clean concrete" '
                         'about a wall their own survey had just located.')
    ap.add_argument('--no-teleport', action='store_true')
    a = ap.parse_args()

    arms = [s.strip() for s in a.arms.split(',') if s.strip()]
    if len(arms) < a.groups:
        arms += [arms[-1] if arms else 'off'] * (a.groups - len(arms))

    if not ensure_ready(label='the bullet-hole groups', range_name=None)['ok']:
        return 1
    if not a.no_teleport:
        teleport_to_wall()
        # ⚠ AFTER the teleport, not before, and this is the second time the
        # same ordering bit today. ensure_ready closes the spawner panel as its
        # fifth leg -- but the Jump School landing spot has a spawner terminal
        # next to it and comes up with the panel OPEN, and the panel is modal:
        # Tab is swallowed beneath it, so the rack cannot be read and every
        # later step fails naming something else. A teleport RESETS the world,
        # so every precondition that describes the world has to be re-taken on
        # the far side of it. (The first instance: teleporting before
        # ensure_ready, whose re-entry silently undid the teleport.)
        from control.spawner import SpawnerControl as _SC
        _sc = _SC()
        print(f'  spawner panel closed after the teleport: '
              f'{_sc.ensure_panel(False)}')

    from calibration.sweep import Rig
    from calibration import collect_timed as CT, rpm_store
    from control.inventory import InventoryControl
    from control.spawner import SpawnerControl
    from control.stock import ensure_weapon_in_hand

    stamp = time.strftime('%m%d_%H%M%S')
    out = os.path.join(OUT, f'run_{stamp}')
    os.makedirs(out, exist_ok=True)
    rig = Rig(a.sight, prefer_dxgi=False)
    grabber = None
    wd = WallDetector(climb_px=a.climb_px, margin_px=a.margin_px)
    rows = []
    curve = None
    scale = None
    try:
        ac, sc = InventoryControl(), SpawnerControl()
        if not ensure_weapon_in_hand(ac, sc, weapon=a.weapon):
            print('no weapon in hand')
            return 1
        if not CT.ensure_sight(ac, sc, 1, a.weapon, a.sight):
            return 1
        # ⚠ `want` IS NOT 'full'. It was hard-coded to it, and on the mg3 that
        # is the WRONG GUN: the mg3 has two automatic modes, config's
        # FIRE_MODE_FOR names the fast one 'high', and 'full' is the SLOW one.
        # So the burst was fired at ~660 rpm while `iv` below came from
        # rpm_store's 59.97 ms, which is the fast mode -- the hold is
        # 2.5 x 59.97 = 150 ms, and at 90.9 ms/round that covers t=0 and t=91
        # and nothing else. Measured twice on 2026-08-12: 75 -> 73, two rounds
        # for a three-round command, both times. Every other gun in the roster
        # defaults to 'full', so this line was invisible until the one gun it
        # was wrong about came up.
        want_mode = fire_mode_for(a.weapon)
        mode = rig.gun.ensure_fire_mode(a.weapon)
        if mode != want_mode:
            print(f'  fire mode reads {mode!r}, not {want_mode!r} — refusing')
            return 1
        if not rig.gun.ensure_ads():
            print('  could not confirm ADS')
            return 1

        # ⚠ The curve is built from the READBACK, never from what was asked
        # for: "I asked for a comp" and "the gun is wearing a comp" are two
        # sentences, and the gap between them is what this repository's second
        # cross-layer law is about. Only built when an ON arm is planned, so an
        # OFF-only run cannot fail on a curve it will never play.
        if 'on' in arms:
            from calibration.weapon_build import build_weapon
            lo = CT.read_loadout()
            cfg = CT.read_config(lo, a.weapon)
            scope_asset = (lo or {}).get('scope')
            curve = build_weapon(a.weapon, 'standing',
                                 dict(cfg or {}, scope=scope_asset))
            print(f'  curve for {cfg}: {len(curve.dy_s)} knots')
            # ⚠ THIS PRINTED AND CARRIED ON, and that is how a run labelled its
            # groups "compensation ON" while the firmware had nothing to play.
            # A spawned gun wears whatever the backpack autofits, and a kitted
            # cell usually has no fitted curve -- so `0 knots` is the COMMON
            # outcome, not an edge case, and an ON arm with 0 knots is an OFF
            # arm wearing the other one's label. Saying it out loud is not the
            # same as stopping.
            if not len(curve.dy_s):
                print(f'  REFUSING: {a.weapon} {cfg} has no fitted curve, so '
                      f'an "ON" arm would fire uncompensated under an ON '
                      f'label. Seed or fit the cell, or run --arms off,off.')
                return 1

        rec = (rpm_store.load() or {}).get(a.weapon) or {}
        iv = rec.get('interval_ms', 85.0)
        # ⚠ THE INTERVAL AND THE FIRE MODE MUST DESCRIBE THE SAME GUN, and on a
        # two-rate weapon rpm_store cannot say whether they do: its note field
        # is prose. The mg3's entry reads "asis prone" -- as-is, mode unrecorded
        # -- and 59.97 ms is 1000 rpm, which matches the FAST mode's nominal 990
        # by arithmetic and by nothing else. Say so out loud rather than let a
        # burst be timed by a number whose subject was never written down.
        if a.weapon in FIRE_MODE_FOR:
            print(f'  ⚠ {a.weapon} has two automatic rates. Firing '
                  f'{want_mode!r}; interval {iv:.2f} ms comes from rpm_store, '
                  f'which records mode as: {rec.get("note", "(nothing)")!r}. '
                  f'The ammo counter below is what checks the pairing.')
        iv /= 1000.0
        grabber = DXGISyncGrabber(rig.tracker.regions())
        centre = (SCREEN_W // 2, SCREEN_H // 2)
        meta = dict(weapon=a.weapon, sight=a.sight, fire_mode=mode,
                    fire_mode_wanted=want_mode, interval_ms=round(iv * 1000, 2),
                    rpm_note=rec.get('note'), rounds_asked=a.rounds,
                    arms=arms, posture='standing', stamp=stamp,
                    tool='calibration/hole_groups.py')

        retries = 0
        k = -1
        while k + 1 < a.groups + retries and retries <= 3:
            k += 1
            # k can run past the plan when a group is retried, and the
            # retry is the SAME arm -- clamping keeps it that way rather
            # than silently promoting a retry into the next condition.
            arm = arms[min(k, len(arms) - 1)]
            print(f'\n── group {k}: compensation {arm.upper()} ──')
            if arm == 'on':
                if curve is None:
                    print('  no curve was built — refusing to fire an ON arm')
                    return 1
                rig.arm(curve)
            elif not rig.fire.disarm():
                # disarm() is the only one of the pair that READS THE FIRMWARE
                # BACK, so an OFF arm is confirmed and an ON arm is asserted.
                print('  the firmware would not confirm compensation is off')
                return 1

            before = capture_screen()
            s = wd.survey(before)
            cv2.imwrite(os.path.join(out, f'g{k}_survey.png'),
                        wd.annotate(before, s))
            print(f'  wall survey: ok={s.ok}  {s.why[:100]}')
            if not s.ok:
                print('  REFUSING to fire: no surface that can record a group.')
                print('  (an empty diff and a perfect group are the same picture)')
                break

            if scale is None:
                scale = measure_scale(rig)
                print(f'  px/count measured: {scale[0]:.2f} yaw, '
                      f'{scale[1]:.2f} pitch')
            centre_in, before, frac, why = settle_aim(
                rig, wd, centre, scale, tries=a.settle_tries)
            print(f'  aim settled: {centre_in}  |  {why}')
            cv2.imwrite(os.path.join(out, f'g{k}_aimed.png'),
                        wd.annotate(before, wd.survey(before)))
            if not centre_in:
                print('  REFUSING: could not get the strip above the crosshair '
                      'onto clean concrete')
                break

            ammo0 = rig.fire.read_ammo()
            # ⚠ mag_size = rounds - 1, NOT rounds. fire_magazine_timed holds
            # for (mag_size - 1 + 1.5) intervals, so asking for 3 held 291 ms
            # at 83.1 ms/round and let a FOURTH round out (measured: 40 -> 36).
            # rounds-1 gives 2.5 intervals = 208 ms, which covers t=0, 83, 166
            # and stops before 249. The ammo counter is what checks it.
            rig.fire.fire_magazine_timed(grabber, max(1, a.rounds - 1), iv)
            # Smoke and muzzle flash outlast the burst by seconds, and they are
            # what drowned the first attempt's diff.
            time.sleep(3.0)
            ammo1 = rig.fire.read_ammo()
            after = capture_screen()
            # ⚠ SAVE THE CLEAN BEFORE, not just the annotated one. The first
            # run wrote only g{k}_aimed.png (with boxes drawn on it), so the
            # diff could not be recomputed offline and every threshold had to
            # be re-earned by firing again.
            cv2.imwrite(os.path.join(out, f'g{k}_before.png'), before)
            cv2.imwrite(os.path.join(out, f'g{k}_after.png'), after)
            fired = (ammo0 - ammo1) if (ammo0 is not None
                                        and ammo1 is not None) else None
            hit, npix, cen = confirm_shot(before, after, centre, tol_px=300)
            marks = find_holes(after, before, near=centre)
            print(f'  ammo {ammo0} -> {ammo1}  (rounds out: {fired})')
            print(f'  confirm_shot: {hit}  changed {npix} px near the crosshair'
                  f'  centroid {cen}')
            K = rig.K
            agree = (fired is not None and len(marks) == fired)
            print(f'  holes: {len(marks)}   rounds: {fired}   '
                  f'AGREE: {agree}')
            for j, (x, y, ar, _new) in enumerate(marks):
                gap = (marks[j - 1][1] - y) if j else None
                print(f'    shot {j + 1}: ({x:7.1f},{y:7.1f}) area {ar:3d}'
                      + ('' if gap is None else
                         f'   gap {gap:6.1f} px = {gap / K:6.2f} counts'))
            if len(marks) >= 3:
                g1 = (marks[0][1] - marks[1][1]) / K
                g2 = (marks[1][1] - marks[2][1]) / K
                print(f'    -> shot 1 recoil {g1:.2f} counts, '
                      f'shot 2 recoil {g2:.2f} counts, ratio {g1 / g2:.2f}')
            rows.append(dict(
                group=k, arm=arm, fired=fired, agree=agree,
                ammo_before=ammo0, ammo_after=ammo1,
                marks=[[round(x, 2), round(y, 2), int(ar)]
                       for x, y, ar, _n in marks],
                gaps_px=[round(marks[j - 1][1] - marks[j][1], 2)
                         for j in range(1, len(marks))],
                K=round(K, 4), px_per_count=[round(scale[0], 3),
                                             round(scale[1], 3)],
                wall_frac=round(frac, 3) if frac is not None else None))
            # ⚠ WRITTEN AFTER EVERY GROUP, NOT AT THE END. Three aug groups
            # fired on 2026-08-11 printed their numbers to a terminal and
            # reached no file; the frames survive and the conditions do not, so
            # the only way to learn what they measured is to re-detect offline
            # -- and whether compensation was on cannot be recovered from
            # pixels at all. A run that is idle-kicked halfway is the common
            # case here, so the write cannot wait for a clean exit.
            _save(out, meta, rows)
            if not marks:
                # ⚠ NO HOLES IS A STATEMENT ABOUT THE SURFACE, not about the
                # burst: the ammo counter already said the rounds went out. The
                # pixel test accepted asphalt once, and only a recorded hole
                # can prove a surface records holes. So move and try again
                # rather than booking a zero.
                print('  no holes on this surface — moving on and retrying')
                rig.view.turn(a.yaw_step, 0)
                time.sleep(0.6)
                retries += 1
                if retries <= 3:
                    k -= 1
            if k + 1 < a.groups:
                # Open loop on purpose: the next group only has to land on
                # UNMARKED wall, and the wall survey re-runs before it fires,
                # so an inexact yaw is caught by the thing that matters.
                rig.view.turn(a.yaw_step, 0)
                time.sleep(0.6)

        print('\n' + '=' * 62)
        for r in rows:
            m = r['marks']
            gaps = [(m[j - 1][1] - m[j][1]) / 1.5413 for j in range(1, len(m))]
            ok = (r['fired'] is not None and len(m) == r['fired'])
            print(f"group {r['group']} comp {r['arm']:>3}  rounds {r['fired']}  "
                  f"holes {len(m)}  {'AGREE' if ok else 'MISMATCH'}  "
                  f"per-shot counts: " + '  '.join(f'{g:.1f}' for g in gaps[:6]))
        print(f'\nOUT {out}')
    finally:
        if grabber is not None:
            grabber.close()
        rig.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
