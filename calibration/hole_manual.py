"""Bullet-hole groups, aimed BY EYE. Four commands, one screenshot each.

    pixi run python calibration/hole_manual.py setup --weapon mg3
    pixi run python calibration/hole_manual.py aim --to 1450,880
    pixi run python calibration/hole_manual.py fire
    pixi run python calibration/hole_manual.py yaw --counts -260

WHY THE WALL DETECTOR IS NOT IN THIS FILE
-----------------------------------------
`detector/wall_detector.py` reads colour, brightness and local flatness, and
its own first paragraph says what that cannot do: it does not certify that
holes will appear. Measured 2026-08-12, five groups in one session:

    aim settled True, 0.80 of the box is flat concrete  ->  holes 0
    ...the same line, five times, three rounds out of the counter each time

Every one of those passed every pixel test and recorded nothing -- a slab far
enough away that the impact is under the 6 px area floor reads exactly like one
at 30 m, because distance is not among the things the criterion looks at. Two
more groups failed the other way (`0.57`, `0.68`) while `survey` was pointing
at a wall that was genuinely there.

⚠ SO THE OPERATOR'S CALL IS THE INSTRUMENT HERE, and that is not a retreat.
This repository already reached the same conclusion twice for the same reason:
`docs/ads_eyeball_labels.json` holds 60 ADS labels read off frames by eye,
because every automatic label available for that question came from one of the
detectors under test; and `calibration/capture_first_shot_holes.py` says it
outright -- nine successive automatic criteria for "which dark blob is the
hole" were each defeated by the same thing, and three frames were read
correctly by eye in one glance.

⚠ THE HOLES THEMSELVES STILL GO THROUGH `find_holes`, and that is not an
inconsistency. It was checked against a human read on the one clean group of
2026-08-12 (run_0812_214634) and returned the same three marks, in the same
order, that a person picks out of the picture. The wall question is the one
with no ground truth to check against; the hole question has the picture.

WHAT EACH COMMAND LEAVES ON DISK
--------------------------------
Every command writes `look.png` (raw) and `look_grid.png` (half scale, with a
labelled 200 px grid in ORIGINAL screen coordinates). Read the grid one, name a
point, pass it to `aim`.

⚠ THE GRID IS LOAD-BEARING. A coordinate named off an unlabelled screenshot is
a guess with a plausible-looking number attached, which is this repository's
most expensive shape. The overlay is what makes "the wall runs from x=900 to
x=1600" a reading instead of an impression.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from capture.cropper import capture_screen                          # noqa: E402
from config import SCREEN_H, SCREEN_W, fire_mode_for                # noqa: E402
from detector.wall_detector import confirm_shot, find_holes         # noqa: E402

OUT = os.path.join('calibration', 'artifacts', 'holes', 'manual')
STATE = os.path.join(OUT, 'state.json')
CENTRE = (SCREEN_W // 2, SCREEN_H // 2)


# ── the picture the operator actually reads ──

def write_look(frame, tag='look', extra=None):
    """Raw frame plus a half-scale copy carrying a labelled coordinate grid.

    Labels are ORIGINAL screen coordinates, not the downscaled ones, because
    every other command in this file takes original coordinates. A grid whose
    numbers mean something different from the numbers you type back is worse
    than no grid.
    """
    os.makedirs(OUT, exist_ok=True)
    raw = os.path.join(OUT, f'{tag}.png')
    cv2.imwrite(raw, frame)
    g = cv2.resize(frame, (SCREEN_W // 2, SCREEN_H // 2))
    for x in range(0, SCREEN_W, 100):
        major = x % 400 == 0
        cv2.line(g, (x // 2, 0), (x // 2, SCREEN_H // 2),
                 (0, 255, 255) if major else (60, 160, 160), 2 if major else 1)
        if major:
            cv2.putText(g, str(x), (x // 2 + 4, 26), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (0, 255, 255), 2)
    for y in range(0, SCREEN_H, 100):
        major = y % 200 == 0
        cv2.line(g, (0, y // 2), (SCREEN_W // 2, y // 2),
                 (0, 255, 255) if major else (60, 160, 160), 2 if major else 1)
        if major:
            cv2.putText(g, str(y), (6, y // 2 - 6), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (0, 255, 255), 2)
    cx, cy = CENTRE[0] // 2, CENTRE[1] // 2
    cv2.drawMarker(g, (cx, cy), (0, 0, 255), cv2.MARKER_CROSS, 60, 3)
    cv2.putText(g, 'CROSSHAIR 1720,720', (cx - 200, cy + 50),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    for pt, col, label in (extra or []):
        cv2.circle(g, (pt[0] // 2, pt[1] // 2), 18, col, 3)
        cv2.putText(g, label, (pt[0] // 2 + 22, pt[1] // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, col, 2)
    grid = os.path.join(OUT, f'{tag}_grid.png')
    cv2.imwrite(grid, g)
    print(f'  look  {raw}')
    print(f'  READ  {grid}')
    return raw, grid


def load_state():
    if not os.path.exists(STATE):
        print(f'[!] no {STATE} — run `setup --weapon <gun>` first')
        return None
    return json.load(open(STATE, encoding='utf-8'))


def save_state(st):
    os.makedirs(OUT, exist_ok=True)
    json.dump(st, open(STATE, 'w', encoding='utf-8'), indent=1,
              ensure_ascii=False)


def _shift(a_bgr, b_bgr, box=(150, 200, 1500, 950)):
    x, y, w, h = box
    a = cv2.cvtColor(a_bgr[y:y + h, x:x + w],
                     cv2.COLOR_BGR2GRAY).astype(np.float32)
    b = cv2.cvtColor(b_bgr[y:y + h, x:x + w],
                     cv2.COLOR_BGR2GRAY).astype(np.float32)
    (sx, sy), _ = cv2.phaseCorrelate(a, b,
                                     cv2.createHanningWindow((w, h), cv2.CV_32F))
    return sx, sy


def measure_scale(rig, probe=60):
    """px per count for yaw and pitch, measured here and now.

    ⚠ NOT A CONSTANT AND NOT CACHEABLE ACROSS SPOTS. Measured at Jump School on
    one night: -1.60, -1.66, -1.85, -1.91, -2.17 px/count, because it depends
    on how far the surface is. It is re-measured by `setup` and by `aim
    --remeasure`; the cached value is only good for the spot it was taken at.
    """
    f0 = capture_screen()
    rig.view.turn(probe, 0)
    time.sleep(0.45)
    f1 = capture_screen()
    rig.view.turn(0, probe)
    time.sleep(0.45)
    f2 = capture_screen()
    return _shift(f0, f1)[0] / probe, _shift(f1, f2)[1] / probe


def _settle_ammo(rig, want=None, timeout_s=6.0, hold_s=0.5):
    """The counter's value once it has stopped moving. -> int | None

    `None` from the detector is "no digits", which happens all through a reload
    animation -- so an unreadable poll is skipped, never counted as a value.
    `want` (the magazine size, if the caller knows it) is required as well as
    stillness: a counter that is briefly still at 53 during a belt reload is
    still and wrong.
    """
    end = time.perf_counter() + timeout_s
    last, since = None, time.perf_counter()
    while time.perf_counter() < end:
        v = rig.fire.read_ammo()
        if v is None:
            continue
        if v != last:
            last, since = v, time.perf_counter()
        elif time.perf_counter() - since >= hold_s:
            if want is None or v == want:
                return v
            print(f'  counter is steady at {v} but the magazine holds {want} '
                  f'— still filling')
            since = time.perf_counter()
    return None


def open_rig(sight='red_dot'):
    """⚠ EVERY command grabs focus first, because every command is a separate
    process and the terminal takes the foreground back between them. Without
    it the grabber raises FocusLost mid-command (measured: it did, on the
    second `fire`), and the half of the commands that do not grab frames would
    instead drive the mouse into whatever window IS in front."""
    from calibration.sweep import Rig
    from control.focus import ensure_focus
    if not ensure_focus(countdown_s=6):
        raise SystemExit('[!] could not bring the game to the foreground')
    return Rig(sight, prefer_dxgi=False)


# ── commands ──

def cmd_setup(a):
    from control.session import ensure_ready
    from control.inventory import InventoryControl
    from control.spawner import SpawnerControl
    from control.stock import ensure_weapon_in_hand
    from calibration import collect_timed as CT, rpm_store
    from calibration.hole_groups import teleport_to_wall

    if not ensure_ready(label='manual hole groups', range_name=None)['ok']:
        return 1
    if not a.no_teleport:
        teleport_to_wall()
        # The Jump School terminal comes up with the spawner panel OPEN, and
        # the panel swallows Tab -- so the rack cannot be read until it is shut.
        SpawnerControl().ensure_panel(False)

    ac, sc = InventoryControl(), SpawnerControl()
    if not ensure_weapon_in_hand(ac, sc, weapon=a.weapon):
        print('[!] no weapon in hand')
        return 1
    if not CT.ensure_sight(ac, sc, 1, a.weapon, a.sight):
        return 1

    rig = open_rig(a.sight)
    try:
        want = fire_mode_for(a.weapon)
        mode = rig.gun.ensure_fire_mode(a.weapon)
        if mode != want:
            print(f'[!] fire mode reads {mode!r}, not {want!r} — refusing')
            return 1
        if not rig.gun.ensure_ads():
            print('[!] could not confirm ADS')
            return 1
        rec = (rpm_store.load() or {}).get(a.weapon) or {}
        iv = rec.get('interval_ms')
        if not iv:
            print(f'[!] no measured interval for {a.weapon} — the burst has no '
                  f'duration without one')
            return 1
        sx, sy = measure_scale(rig)
        save_state(dict(weapon=a.weapon, sight=a.sight, fire_mode=mode,
                        interval_ms=iv, rpm_note=rec.get('note'),
                        px_per_count=[sx, sy], K=rig.K, arm='off',
                        stamp=time.strftime('%m%d_%H%M%S'), shot=0))
        print(f'  {a.weapon}  mode {mode!r}  interval {iv:.2f} ms  '
              f'K {rig.K:.4f}')
        print(f'  px/count: {sx:.2f} yaw, {sy:.2f} pitch')
        rig.flush(4)
        write_look(capture_screen())
        print('\nNow LOOK at the grid image and pick a point on clean wall '
              'with room ABOVE it, then:  aim --to X,Y')
    finally:
        rig.close()
    return 0


def cmd_aim(a):
    st = load_state()
    if st is None:
        return 1
    tx, ty = (int(v) for v in a.to.split(','))
    rig = open_rig(st['sight'])
    try:
        if a.remeasure:
            sx, sy = measure_scale(rig)
            st['px_per_count'] = [sx, sy]
            save_state(st)
            print(f'  px/count re-measured: {sx:.2f} yaw, {sy:.2f} pitch')
        sx, sy = st['px_per_count']
        if abs(sx) < 0.05 or abs(sy) < 0.05:
            print(f'[!] px/count is {sx:.3f},{sy:.3f} — the scene did not move '
                  f'when probed, so this cannot aim. Re-run with --remeasure '
                  f'while looking at something with texture.')
            return 1
        yaw = int(round((CENTRE[0] - tx) / sx))
        pitch = int(round((CENTRE[1] - ty) / sy))
        print(f'  bringing ({tx},{ty}) to the crosshair: '
              f'yaw {yaw:+d}, pitch {pitch:+d} counts')
        rig.view.turn(yaw, pitch)
        time.sleep(0.6)
        rig.flush(4)
        write_look(capture_screen())
        if not a.fire:
            print('\nCheck the crosshair is on wall with room above it. '
                  'Off? aim again. Good? fire')
    finally:
        rig.close()
    # ⚠ `--fire` EXISTS BECAUSE THE OPERATOR IS THE SLOW PART AND PUBG COUNTS
    # THAT. The idle timer ("You have been logged off due to inactivity") runs
    # on wall-clock with no input, and in this loop the gaps between commands
    # are exactly the minutes spent LOOKING at the last screenshot -- which is
    # the whole method. It kicked the session on 2026-08-12 between a `yaw` and
    # the next `aim`. Firing in the same command as the aim halves the number
    # of gaps; the look written just before the burst is still on disk, so a
    # bad aim is discarded afterwards rather than prevented beforehand.
    if a.fire:
        ns = argparse.Namespace(rounds=a.fire, arm=a.arm)
        return cmd_fire(ns)
    return 0


def cmd_yaw(a):
    st = load_state()
    if st is None:
        return 1
    rig = open_rig(st['sight'])
    try:
        rig.view.turn(a.counts, a.pitch)
        time.sleep(0.6)
        rig.flush(4)
        write_look(capture_screen())
    finally:
        rig.close()
    return 0


def cmd_fire(a):
    from capture.cropper import DXGISyncGrabber
    st = load_state()
    if st is None:
        return 1
    rig = open_rig(st['sight'])
    grabber = None
    try:
        if a.arm == 'off':
            if not rig.fire.disarm():
                # disarm() is the one of the pair that READS THE FIRMWARE BACK,
                # so an OFF arm is confirmed where an ON arm is only asserted.
                print('[!] the firmware would not confirm compensation is off')
                return 1
        else:
            from calibration.weapon_build import build_weapon
            from calibration import collect_timed as CT
            lo = CT.read_loadout()
            cfg = CT.read_config(lo, st['weapon'])
            curve = build_weapon(st['weapon'], 'standing',
                                 dict(cfg or {}, scope=(lo or {}).get('scope')))
            if not len(curve.dy_s):
                print(f'[!] {st["weapon"]} {cfg} has no fitted curve — an "on" '
                      f'arm would fire uncompensated under an "on" label')
                return 1
            print(f'  curve for {cfg}: {len(curve.dy_s)} knots')
            rig.arm(curve)

        if not rig.gun.ensure_ads():
            print('[!] could not confirm ADS')
            return 1
        # ⚠ DO NOT RELOAD FOR A THREE-ROUND GROUP. Nothing in this measurement
        # depends on the magazine being full, and the reload is what broke two
        # groups tonight: PUBG does not accept the trigger through the reload
        # animation (control/CLAUDE.md measured 2000 ms -> 0/4 clicks), so the
        # burst was swallowed and the counter's 53 -> 75 and 72 -> 75 were
        # booked as "-22" and "-3 rounds out".
        #
        # ⚠ AND GATING ON top_up()'s OWN RETURN DOES NOT FIX IT, which is the
        # part worth writing down: it reports the magazine size by READING THE
        # COUNTER, so mid-reload it reported 72 for a 75-round belt and a gate
        # keyed on that number agreed with it. Two readings taken from one
        # instrument during the event that is confusing it are not two sources.
        #
        # The reload only happens when the belt genuinely cannot cover the
        # burst, and then the counter still has to settle before anything is
        # read. top_up() RETURNS A PAIR (rounds, reload_s), not a count.
        have = _settle_ammo(rig)
        if have is None or have < a.rounds + 5:
            size, _reload_s = rig.fire.top_up()
            have = _settle_ammo(rig, want=size)
        rounds = have
        # ⚠ top_up() RETURNING IS NOT THE RELOAD FINISHING, and the gap is not
        # small. Measured here 2026-08-12: it came back, `before` was captured,
        # the counter read 53 of an mg3's 75 -- and the burst went out DURING
        # the reload animation, which PUBG does not accept input through
        # (control/CLAUDE.md measured 2000 ms -> 0/4 clicks). The counter then
        # read 53 -> 75 and the run booked "-22 rounds out". Nothing was fired,
        # nothing hit the wall, and confirm_shot said True because a belt
        # reload moves 52431 px near the crosshair.
        #
        # So wait for the number to STOP MOVING, and require it to reach the
        # magazine size, before anything else is read or written.
        if rounds is None:
            print('[!] the ammo counter never settled — refusing to fire into '
                  'a state nothing can describe')
            return 1
        grabber = DXGISyncGrabber(rig.tracker.regions())
        rig.flush(4)
        before = capture_screen()
        ammo0 = rounds
        # ⚠ mag_size = rounds - 1. fire_magazine_timed holds for
        # (mag_size - 1 + 1.5) intervals, so asking for 3 held 291 ms at
        # 83.1 ms/round and let a FOURTH round out. The ammo counter checks it.
        rig.fire.fire_magazine_timed(grabber, max(1, a.rounds - 1),
                                     st['interval_ms'] / 1000.0)
        time.sleep(3.0)          # smoke and flash outlast the burst by seconds
        ammo1 = rig.fire.read_ammo()
        after = capture_screen()
        fired = (ammo0 - ammo1) if None not in (ammo0, ammo1) else None

        st['shot'] = st.get('shot', 0) + 1
        n = st['shot']
        # ⚠ THE STAMP IS IN THE NAME BECAUSE `setup` RESETS THE COUNTER. It did,
        # tonight, and `g2_before/after` -- the near-wall group whose two holes
        # were read by eye -- was overwritten by the next session's second
        # group. The numbers survived in groups.jsonl; the frames did not, and
        # the frames are the part that cannot be re-derived.
        tag = f"{st['stamp']}_g{n}"
        save_state(st)
        os.makedirs(OUT, exist_ok=True)
        cv2.imwrite(os.path.join(OUT, f'{tag}_before.png'), before)
        cv2.imwrite(os.path.join(OUT, f'{tag}_after.png'), after)

        hit, npix, cen = confirm_shot(before, after, CENTRE, tol_px=300)
        marks = find_holes(after, before, near=CENTRE)
        K = st['K']
        print(f'  ammo {ammo0} -> {ammo1}  (rounds out: {fired})')
        print(f'  confirm_shot: {hit}  changed {npix} px  centroid {cen}')
        print(f'  holes: {len(marks)}   rounds: {fired}   '
              f'AGREE: {fired is not None and len(marks) == fired}')
        gaps = []
        for j, (x, y, ar, _new) in enumerate(marks):
            gap = (marks[j - 1][1] - y) if j else None
            if gap is not None:
                gaps.append(gap)
            print(f'    shot {j + 1}: ({x:7.1f},{y:7.1f}) area {ar:3d}'
                  + ('' if gap is None else
                     f'   gap {gap:6.1f} px = {gap / K:6.2f} counts'))
        if len(gaps) >= 2:
            print(f'    -> shot 1 {gaps[0] / K:.2f} counts, '
                  f'shot 2 {gaps[1] / K:.2f} counts, '
                  f'ratio {gaps[0] / gaps[1]:.2f}')

        rec = dict(group=n, arm=a.arm, rounds_asked=a.rounds, fired=fired,
                   ammo=[ammo0, ammo1], confirm_shot=bool(hit),
                   changed_px=int(npix),
                   marks=[[round(x, 2), round(y, 2), int(ar)]
                          for x, y, ar, _ in marks],
                   gaps_px=[round(g, 2) for g in gaps],
                   K=K, px_per_count=st['px_per_count'],
                   aimed_by='operator, from the grid overlay')
        path = os.path.join(OUT, 'groups.jsonl')
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({**{k: st[k] for k in
                                    ('weapon', 'sight', 'fire_mode',
                                     'interval_ms', 'stamp')}, **rec},
                                ensure_ascii=False) + '\n')
        print(f'  appended -> {path}')

        # A zoom the operator can count holes in, and disagree with the
        # detector on. Circles are what find_holes claimed, not ground truth.
        x0, y0 = CENTRE[0] - 170, CENTRE[1] - 200
        crop = after[y0:y0 + 320, x0:x0 + 340].copy()
        crop = cv2.resize(crop, None, fx=3, fy=3,
                          interpolation=cv2.INTER_NEAREST)
        for j, (x, y, _ar, _n) in enumerate(marks):
            cv2.circle(crop, (int((x - x0) * 3), int((y - y0) * 3)), 24,
                       (0, 0, 255), 2)
            cv2.putText(crop, str(j + 1),
                        (int((x - x0) * 3) + 28, int((y - y0) * 3)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
        zoom = os.path.join(OUT, f'{tag}_zoom.png')
        cv2.imwrite(zoom, crop)
        print(f'  COUNT {zoom}   (circles are the detector\'s claim, not truth)')
    finally:
        if grabber is not None:
            grabber.close()
        rig.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('setup')
    s.add_argument('--weapon', required=True)
    s.add_argument('--sight', default='red_dot')
    s.add_argument('--no-teleport', action='store_true')
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser('look')
    s.set_defaults(fn=lambda a: (write_look(capture_screen()), 0)[1])

    s = sub.add_parser('aim')
    s.add_argument('--to', required=True, help='X,Y on the grid image')
    s.add_argument('--remeasure', action='store_true')
    s.add_argument('--fire', type=int, default=0,
                   help='rounds to fire immediately after aiming, in the SAME '
                        'command — see the note in cmd_aim about the idle kick')
    s.add_argument('--arm', choices=('off', 'on'), default='off')
    s.set_defaults(fn=cmd_aim)

    s = sub.add_parser('yaw')
    s.add_argument('--counts', type=int, required=True)
    s.add_argument('--pitch', type=int, default=0)
    s.set_defaults(fn=cmd_yaw)

    s = sub.add_parser('fire')
    s.add_argument('--rounds', type=int, default=3)
    s.add_argument('--arm', choices=('off', 'on'), default='off')
    s.set_defaults(fn=cmd_fire)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == '__main__':
    raise SystemExit(main())
