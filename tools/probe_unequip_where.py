"""Take a part off a gun. Where does it actually go? Needs the game.

    pixi run python tools/probe_unequip_where.py --reps 3

THE QUESTION. calibration/collect_templates.py cannot photograph a part in
库存 unless it can put it there, and the step that should is failing:

    muzzle would not come off: slot moved 3.5, 库存 8->8
    muzzle would not come off: slot moved 0.5, 库存 8->8

The slot did not change, so nothing was taken off at all -- which is a
different failure from "it came off and went somewhere else", and the two need
opposite repairs. Three destinations exist and the log only ever watched two.

WHAT IS READ, at both ends of one gesture:

    the slot's crop        did anything leave the gun
    库存 row count         did it arrive in the backpack
    附近 row count         did it land on the floor instead

All three are counts or pixel differences. No template names anything, which
matters because the parts whose templates are missing are exactly the ones
this collector exists to photograph.

WHAT IS ALREADY ON RECORD, and why it is not enough. unequip's own docstring
says the DRAG lands on the floor rather than in 库存 -- "库存 +0, 附近 +1",
measured twice -- and that gesture='auto' therefore right-clicks when the
destination is 库存. So the drag is understood. What has never been measured
is the RIGHT CLICK in this direction: whether it takes the part off at all,
and where the game decides to put it when it does.

Both gestures are run here, against the same starting state, because "the
right click does nothing" and "the right click works and the caller is wrong
about when" look identical from one gesture alone.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import numpy as np

from collect_templates import CHANGE_MIN, cut                   # noqa: E402
from control.focus import ensure_focus                          # noqa: E402
from control.inventory import (InventoryControl, at_ground,     # noqa: E402
                               at_inv)
from control.spawner import SpawnerControl                      # noqa: E402
from detector.tab_items import TabItemDetector                  # noqa: E402
from range_session import get_session                           # noqa: E402

GUN, PART, SLOT = 'slr', 'comp_ar', 'muzzle'


def rows(view, panel):
    return sum(1 for i in getattr(view, panel) if i is not None)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--countdown', type=int, default=4)
    args = ap.parse_args()

    if not ensure_focus(countdown_s=args.countdown, label='the unequip probe'):
        return 1
    if not get_session('auto').ensure()[0]:
        print('[!] could not get into the training range')
        return 1

    sc = SpawnerControl(verbose=False)
    ac = InventoryControl(verbose=False)

    def give(key, weapon=False):
        if not sc.ensure_panel(True) or not sc.sync(need_cols=(1, 2, 3)):
            return False
        ok = (sc.give_weapon(key)['ok'] if weapon
              else sc.give_attachment(key)['ok'])
        sc.ensure_panel(False)
        time.sleep(0.7)
        return ok

    def state():
        f = ac.frame()
        view = ac.look(f)
        return (cut(f, f'att_{gun}_{SLOT}').copy(),
                rows(view, 'inventory'), rows(view, 'nearby'))

    print('=== setup ===')
    ac.ensure_tab(True)
    ac.clear_rack()
    if not give(GUN, weapon=True):
        print('[!] the spawner would not produce the host')
        return 1
    ac.ensure_tab(True)
    gun = ac.gun_slot()
    if gun is None:
        print('[!] no gun in the rack')
        return 1
    print(f'  host in rack slot {gun}')
    ac.held = None
    ac.hold(gun)
    ac.strip(gun, to=at_ground())

    results = {}
    for gesture in ('click', 'drag'):
        print(f'\n=== unequip by {gesture} ===')
        tally = []
        for i in range(args.reps):
            # Put one on, so there is something to take off. The slot is empty
            # after the previous iteration, so the spawn auto-fits it.
            if not give(PART):
                print(f'  {i}: no {PART}')
                continue
            ac.ensure_tab(True)
            time.sleep(0.5)
            before, inv0, gnd0 = state()

            ac.unequip(gun, SLOT, gesture=gesture)
            time.sleep(1.0)
            after, inv1, gnd1 = state()

            moved = float(np.abs(before.astype(np.float32)
                                 - after.astype(np.float32)).mean())
            came_off = moved >= CHANGE_MIN
            where = ('库存' if inv1 > inv0 else
                     '附近' if gnd1 > gnd0 else
                     'nowhere' if came_off else '-')
            verdict = (f'came off -> {where}' if came_off
                       else 'DID NOT COME OFF')
            tally.append(verdict)
            print(f'  {i}: slot moved {moved:6.1f}  库存 {inv0}->{inv1}  '
                  f'附近 {gnd0}->{gnd1}   {verdict}')
        results[gesture] = tally

    print('\n=== summary ===')
    for g, t in results.items():
        counts = {v: t.count(v) for v in set(t)}
        print(f'  {g:<6} ' + ', '.join(f'{v} x{n}'
                                       for v, n in sorted(counts.items())))
    print('\n  "DID NOT COME OFF" means the gesture never reached the slot.'
          '\n  "came off -> 附近" means it works but the destination is the'
          '\n  floor, which is what unequip\'s docstring records for the drag'
          '\n  and what collect_templates must not rely on.')
    try:
        ac.ensure_tab(False)
    finally:
        ac.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
