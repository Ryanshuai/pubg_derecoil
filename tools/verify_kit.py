"""Fit a part, then show what the detector says AND what the screen shows.

    pixi run python tools/verify_kit.py --weapon m416 --parts stock=tactical_stock
    pixi run python tools/verify_kit.py --weapon m416 --parts muzzle=comp_ar,grip=vert_grip

Written because a measurement said the tactical stock does almost nothing to
vertical recoil (0.97), and "this part has no effect" and "this part was never
actually fitted" produce identical numbers. Only one of those is a finding.

The readback check inside harvest already rejects an empty slot and a wrong
asset, so those fail loudly. What it cannot catch is a template that reports
the expected name while the slot holds something else — a false positive for
exactly the thing being looked for. detector/CLAUDE.md records one: a drifted
thumb-grip template made Mk12's grip slot read as `laser`, in-catalogue and
confident.

So this writes a screenshot next to the readback. The detector's opinion and
the picture disagreeing is the whole point; a human glance settles it in a
second and nothing else can.
"""
import argparse
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))), 'calibration'))

import cv2
import numpy as np

from control.session import ensure_ready
from control import spawner as spawner_mod
from control.spawner import SpawnerControl
from control.stock import restock
from calibration.sweep import Rig
from control.kitting import Kitter, BACKPACK, SCOPE_PART

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'docs', 'kit_checks')


def shot(path):
    from PIL import ImageGrab
    img = cv2.cvtColor(np.array(ImageGrab.grab()), cv2.COLOR_RGB2BGR)
    cv2.imwrite(path, img)
    cv2.imwrite(path.replace('.png', '_half.png'),
                cv2.resize(img, (img.shape[1] // 2, img.shape[0] // 2)))
    return img


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapon', default='m416')
    ap.add_argument('--parts', default='stock=tactical_stock',
                    help='slot=part pairs, comma separated')
    ap.add_argument('--slot', type=int, default=2)
    ap.add_argument('--no-spawn', action='store_true')
    args = ap.parse_args()

    want = {'scope': SCOPE_PART}
    for pair in args.parts.split(','):
        s, _, k = pair.partition('=')
        s, k = s.strip(), k.strip()
        if not s:
            continue
        if k and k not in spawner_mod.ATTACHMENTS:
            print(f'[!] {k} is not spawnable')
            return 1
        want[s] = k or None
    for s in ('muzzle', 'grip', 'stock'):
        want.setdefault(s, None)

    os.makedirs(OUT, exist_ok=True)
    stamp = datetime.now().strftime('%m%d_%H%M%S')

    if not ensure_ready(label='the kit check', countdown_s=5)['ok']:
        print('[!] could not focus the game')
        return 1

    rig = Rig('red_dot')
    kit = Kitter(rig, slot=args.slot)
    sc = SpawnerControl(verbose=True)
    try:
        if not args.no_spawn:
            # Look in the backpack before clicking anything: the spawner will
            # happily hand out a fourth compensator, and every spare is one
            # more thing find() can pick instead of the one meant.
            restock(kit.ac, sc, {v for v in want.values() if v},
                    backpack=BACKPACK)
            if not sc.ensure_panel(True) or not sc.sync(need_cols=(1,)):
                print('[!] the spawner would not come up')
                return 1
            sc.give_weapon(args.weapon)
            sc.ensure_panel(False)
            time.sleep(0.6)

        print(f'\nasked for: {want}')
        got = kit.apply(want)
        print(f'read back: {got}')

        # Open the inventory again and photograph it: the slot row is where a
        # human can see what is actually bolted on.
        rig.gun.ensure_inventory_open()
        time.sleep(0.5)
        p = os.path.join(OUT, f'{args.weapon}_{stamp}.png')
        shot(p)
        slots = kit.ac.read_slots(args.slot) if kit.ac.sync() else {}
        rig.gun.ensure_inventory_closed()

        print(f'\nslots read from the Tab screen: {slots}')
        print(f'screenshot -> {p}')
        print('\nCompare the two by eye. The readback agreeing with the request')
        print('proves the templates are self-consistent, not that the part is')
        print('on the gun -- that is what the picture is for.')
        ok = got is not None
        for s, k in want.items():
            if k is None:
                continue
            cur = slots.get(s, '')
            print(f'  {s:<9}{k:<16}-> {cur or "<empty>"}'
                  f'{"" if Kitter._matches(cur, k) else "   <-- MISMATCH"}')
        return 0 if ok else 1
    except KeyboardInterrupt:
        print('\ninterrupted')
        return 1
    finally:
        kit.close()
        rig.close()


if __name__ == '__main__':
    sys.exit(main())
