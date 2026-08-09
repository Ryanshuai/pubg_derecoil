"""Does the compensation hold the crosshair? In PIXELS, and nothing is stored.

    pixi run python tools/probe_compensation.py --weapon p90
    pixi run python tools/probe_compensation.py --weapon m416 --mags 2

Fires the same gun twice -- once with the curve OFF and once with it ON -- and
prints how far the view travelled each time. Two numbers, one question.

⚠ IT EXISTS BECAUSE K IS FOR MEASURING, NOT FOR COMPENSATING, and the two got
tangled. `RECOIL_SIGHT_PROFILES` has no entry for a weapon with an integral
optic (the p90, and `iron` generally), so the calibration path refuses it --
correctly, because y_true in COUNTS needs K and a defaulted K is wrong by up to
3x while looking perfectly normal. None of that touches whether the firmware
can hold the gun down: the curve plays in counts the device already speaks, and
"did the crosshair stay put" is a question about the screen.

⚠ SO IT STORES NOTHING, DELIBERATELY. calibration/samples.py is MODEL.md's
store and every magazine in it must have a trustworthy y_true. A magazine
measured under a defaulted K does not, and the way a store gets poisoned is one
convenient exception at a time.

What it can and cannot say:

    can     the compensation moved the view this much less        (px)
    can     the worst excursion during the burst                  (px)
    cannot  how many counts of recoil there are                   (needs K)
    cannot  whether the curve is the RIGHT shape                  (needs a fit)

MEASURED 2026-08-09 -- p90, standing, bare, one magazine per arm, 50 rounds:

    comp OFF     ended  +862.8 px     worst  865.2 px
    comp ON      ended   -51.3 px     worst   97.2 px

⚠ READ THE SIGN, NOT THE PERCENTAGE. The summary line says the curve removed
106% of the travel, and 106 is not "better than all of it" -- it is an
OVERSHOOT. The magnitude falls 862.8 -> 51.3 (94% of the excursion) and the
remaining 6% is on the OTHER SIDE: this community seed pulls slightly past
zero. Worst excursion 865 -> 97 px, 8.9x.

⚠ The same two numbers imply a K, and that is exactly the claim this file must
not make. y_true = 862.8 px on the off arm, so the curve delivered
862.8 - (-51.3) = 914.1 px for its 627 counts -> 1.458 px/count. The optic was
measured properly hours later -- RECOIL_SIGHT_PROFILES['p90_integral']['K'], a
ratio against the red dot -- and this by-product came out about 7% under it:
one magazine per arm, assuming the firmware delivered every count it stored,
with the correlator's per-pair bias never subtracted. Right order of magnitude,
wrong number. A K comes from calibration/calibrate_k.py, not from a side effect.

⚠ AND THE FIRST VERSION OF THIS PARAGRAPH HAD THE UNITS UPSIDE DOWN. It wrote
1/K as "0.686 counts/px", compared it against RECOIL_K_DEFAULT_SCOPED (which is
px/count), and concluded the default was wrong by 2.7x. In one unit the default
is about 19% above RECOIL_SIGHT_PROFILES['p90_integral']['K'] -- still far
outside the 5% agreement gate, so refusing `--sight iron` on this gun was right
for the reason given. Only the factor was wrong, and it was a unit error.
"""
import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapon', required=True)
    ap.add_argument('--posture', default='standing')
    ap.add_argument('--sight', default='',
                    help="only picks the tracker's patch layout here; K is not "
                         "used for anything this prints. Default: the weapon's "
                         "own optic if it has one, else red_dot.")
    ap.add_argument('--mags', type=int, default=1,
                    help='magazines PER ARM')
    ap.add_argument('--countdown', type=int, default=5)
    a = ap.parse_args()

    from capture.cropper import DXGISyncGrabber
    from calibration.collect_timed import aim_and_scope, one_magazine
    from calibration.sweep import Rig
    from calibration.weapon_build import build_weapon
    from control.inventory import InventoryControl
    from control.session import ensure_ready
    from control.stock import ensure_weapon_in_hand
    from control.spawner import SpawnerControl
    from press.pico_mouse import other_agents

    busy = other_agents()
    if busy:
        print(f'[!] another agent holds the Pico: {busy} — refusing to take it')
        return 1

    rec = ensure_ready(label='compensation probe', countdown_s=a.countdown,
                       verbose=True)
    if not rec.get('ok'):
        print(f'[!] not ready: {rec}')
        return 1

    # ⚠ `iron` USED TO BE THE DEFAULT AND IT WAS THE WRONG WORD FOR THIS GUN.
    # `iron` means a scope slot that EXISTS and is EMPTY -- metal sights. The
    # p90 has no scope slot at all; what it looks through is bolted to the
    # weapon. The two are different optics with different K, and naming one
    # after the other is how the p90's curve ended up filed where the lookup
    # could not reach it. It happened to be harmless HERE (both resolve to the
    # default patch layout and this file prints no counts), but a default that
    # is only harmless by coincidence is a bug waiting for its first consumer.
    from control.kitting import SIGHT_FOR
    sight = a.sight or SIGHT_FOR.get(a.weapon, 'red_dot')
    print(f'  tracker patches for sight {sight!r}')
    rig = Rig(sight, prefer_dxgi=False)
    grabber = None
    try:
        # ⚠ Rig owns capture, the Pico and the detectors -- not the Tab
        # screen. InventoryControl is built here for the one thing this needs
        # it for, and closed before the burst so nothing holds a screen open
        # over the trigger.
        with InventoryControl() as ac, SpawnerControl() as sc:
            if not ensure_weapon_in_hand(ac, sc, weapon=a.weapon):
                print(f'[!] could not get a {a.weapon} in hand')
                return 1
        w = build_weapon(a.weapon, a.posture, {})
        if w is None or not len(getattr(w, 't_s', ())):
            print(f'[!] {a.weapon} has no curve on disk — nothing to play')
            return 1
        base_dy, base_dx = list(w.dy_s), list(w.dx_s)
        print(f'  curve: {len(base_dy)} knots, {sum(base_dy):.0f} counts')

        grabber = DXGISyncGrabber(rig.tracker.regions())
        out = {}
        for arm in (0.0, 1.0):
            travels, worsts = [], []
            for i in range(max(1, a.mags)):
                w.dy_s = [v * arm for v in base_dy]
                w.dx_s = [v * arm for v in base_dx]
                rig.arm(w)
                # ⚠ BOTH ARMS ARE PROVED BY READBACK, NOT BY INTENT, and the
                # OFF arm is the one that matters: if the disarm is dropped,
                # this probe fires a compensated magazine, labels it `comp
                # OFF`, and the whole comparison inverts while every number
                # printed still looks reasonable. disarm() confirms against the
                # firmware and returns False when it cannot -- including when
                # the firmware is too old to answer, which is a refusal on
                # purpose (control/fire.py).
                curve = rig.mouse.read_pattern() or []
                if arm > 0:
                    played = sum(k['dy'] for k in curve)
                    print(f'    firmware holds {len(curve)} knots, '
                          f'{played} counts')
                    if not played:
                        print('  [!] the ON arm uploaded nothing — refusing to '
                              'call an empty pattern "compensation"')
                        return 1
                else:
                    if not rig.fire.disarm():
                        print('  [!] the firmware would not confirm '
                              'compensation is OFF. Every number below would '
                              'be labelled backwards.')
                        return 1
                    curve = []
                mag_size, _ = rig.fire.top_up(weapon=a.weapon)
                if not mag_size:
                    print('  [!] no ammo counter — cannot size the burst')
                    return 1
                if not aim_and_scope(rig, a.posture):
                    print('  [!] could not aim')
                    return 1
                mag, _ = one_magazine(rig, grabber, a.weapon, mag_size,
                                      w.bullet_interval_s, curve, {},
                                      a.posture, note='compensation probe')
                # ⚠ PIXELS, STRAIGHT OFF THE TRACKER. dy_px is what the
                # correlator measured between consecutive frames; summing it is
                # where the view ended up, and the running maximum is how far
                # it got at its worst. Neither goes near K.
                y = np.cumsum(np.asarray(mag.dy_px, dtype=float))
                travels.append(float(y[-1]) if len(y) else float('nan'))
                worsts.append(float(np.max(np.abs(y))) if len(y) else float('nan'))
                print(f'    {"comp ON " if arm else "comp OFF"} mag {i}: '
                      f'{mag_size} rounds, ended {travels[-1]:+8.1f} px, '
                      f'worst {worsts[-1]:7.1f} px')
                rig.fire.wait_reload(expect=mag_size, weapon=a.weapon)
            out[arm] = (float(np.mean(travels)), float(np.mean(worsts)))

        off_end, off_worst = out[0.0]
        on_end, on_worst = out[1.0]
        print(f'\n  {"":<10}{"ended at":>12}{"worst":>12}')
        print(f'  {"comp OFF":<10}{off_end:>12.1f}{off_worst:>12.1f}')
        print(f'  {"comp ON":<10}{on_end:>12.1f}{on_worst:>12.1f}')
        if abs(off_end) > 1:
            print(f'\n  the curve removed {100 * (1 - on_end / off_end):.0f}% '
                  f'of where the view ended up')
        print('  NOTHING WAS STORED — a magazine measured under a defaulted K '
              'does not belong in the sample store.')
    finally:
        if grabber is not None:
            grabber.close()
        rig.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
