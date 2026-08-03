"""Right-click a fitted part off the gun: where does it go, and how fast? Needs the game.

    pixi run python tools/probe_unequip_gesture.py --gesture click
    pixi run python tools/probe_unequip_gesture.py --gesture drag

Both gestures reach the same attachment slot. The drag hauls it left into
库存; the right-click has no target at all -- the game picks, and the claim
under test is that it picks the BACKPACK.

That distinction is the whole point. `unequip(to=at_ground())` is a supported
call, and if right-click always lands in the pack then `to=` anything else has
to stay a drag. Getting this wrong would be quiet: the part leaves the slot
either way, so a check that only reads the slot back would pass while the
part sat on the floor, where PUBG's auto-fit cannot reach it and the next
`ensure_kit` would spawn a duplicate.

So this reads three things after the pull:

    the slot is empty            the gesture took the part off
    库存 gained exactly one      it went to the BACKPACK
    附近 gained nothing          ...and not to the floor
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

from control.focus import ensure_focus
from control.inventory import InventoryControl
from control.spawner import SpawnerControl
from detector.tab_layout import INV_ROWS

KIT = ['backpack3', 'm416', 'comp_ar', 'vert_grip', 'red_dot']


def survey(ac):
    view = ac.look()
    return {'inv': [it.key for it in view.inventory if it is not None],
            'inv_rows': view.rows('inventory'),
            'ground_rows': view.rows('nearby'),
            'slots': {g: {k: v for k, v in ac.read_slots(g).items() if v}
                      for g in (1, 2)}}


def show(tag, s):
    print(f'  {tag}')
    print(f'    库存 : {s["inv_rows"]} rows {s["inv"]}')
    print(f'    附近 : {s["ground_rows"]} rows')
    for g in (1, 2):
        print(f'    gun{g} : {s["slots"][g] or "(bare)"}')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--gun', type=int, default=1, choices=(1, 2))
    ap.add_argument('--slot', default='muzzle')
    ap.add_argument('--gesture', default='click',
                    choices=('auto', 'click', 'drag'))
    ap.add_argument('--to-xy', default=None, metavar='X,Y',
                    help='release at this exact screen point instead of the '
                         'computed one — for measuring a drop zone found by '
                         'holding a drag (see the calibrate-screen skill)')
    ap.add_argument('--countdown', type=int, default=3)
    args = ap.parse_args()

    if not ensure_focus(countdown_s=args.countdown, label='the unequip probe'):
        return 1
    time.sleep(0.8)

    ac = InventoryControl(verbose=True)
    try:
        if not ac.ensure_tab(True) or not ac.sync():
            print('[!] Tab would not come up')
            return 1
        if not ac.read_slots(args.gun).get(args.slot):
            print(f'gun{args.gun}.{args.slot} is empty — kitting one up first')
            ac.ensure_tab(False)
            time.sleep(0.4)
            sc = SpawnerControl(verbose=False)
            sc.give_many(KIT)
            sc.ensure_panel(False)
            time.sleep(0.9)
            if not ac.ensure_tab(True) or not ac.sync():
                return 1
            ac.hold(args.gun)
            ac.ensure_kit(args.gun, {'muzzle': 'comp_ar'}, weapon='m416')

        before = survey(ac)
        print('\n=== before ===')
        show('', before)
        was = ac.read_slots(args.gun).get(args.slot)
        if not was:
            print(f'[!] gun{args.gun}.{args.slot} still empty — nothing to pull')
            return 1

        if args.to_xy:
            # Raw drag to a measured point, bypassing point_of() entirely.
            # This is how a drop zone found by holding a drag gets confirmed:
            # the coordinate came off the screen, so nothing here may re-derive
            # it. See the calibrate-screen skill, "drop targets only exist
            # while a drag is in flight".
            from detector.tab_layout import att_slot_point
            x, y = (int(v) for v in args.to_xy.split(','))
            src = att_slot_point(args.gun, args.slot)
            print(f'\n=== raw drag {src} -> ({x}, {y}) ===')
            t0 = time.perf_counter()
            moved = ac.pointer.drag(src, (x, y))
            dt = time.perf_counter() - t0
            time.sleep(0.6)
            rec = {'ok': not ac.read_slots(args.gun).get(args.slot),
                   'gesture': f'drag->({x},{y})', 'cursor_ok': moved}
            print(f'  -> slot empty={rec["ok"]}  cursor landed={moved}  '
                  f'was={was!r}  {dt:.2f}s')
        else:
            print(f'\n=== unequip({args.gun}, {args.slot!r}, '
                  f'gesture={args.gesture!r}) ===')
            t0 = time.perf_counter()
            rec = ac.unequip(args.gun, args.slot, gesture=args.gesture)
            dt = time.perf_counter() - t0
            print(f'  -> ok={rec["ok"]}  via={rec.get("gesture")!r}  '
                  f'was={was!r}  {dt:.2f}s')
        if rec.get('error'):
            print(f'     error: {rec["error"]}')

        time.sleep(0.5)
        after = survey(ac)
        print('\n=== after ===')
        show('', after)

        from collections import Counter
        d_gnd = after['ground_rows'] - before['ground_rows']
        empty = not after['slots'][args.gun].get(args.slot)
        gained = Counter(after['inv']) - Counter(before['inv'])
        # ROW COUNTS LIE ONCE THE LIST IS FULL. 库存 shows 12 rows before it
        # scrolls, so a part that really did go into the pack leaves the count
        # at 12 and the naive `+1` check calls it a failure. That mis-read cost
        # two rounds of this probe: the list had silently filled up from the
        # earlier runs and every result after that was noise. Compare the
        # multiset of keys, and treat a full list as "cannot tell from here".
        full = before['inv_rows'] >= INV_ROWS
        in_pack = bool(gained) or (full and d_gnd == 0)
        print('\n=== verdict ===')
        print(f'  slot emptied             : {empty}')
        print(f'  库存 gained              : {dict(gained) or "nothing"}'
              f'{"   (list is FULL — count cannot grow)" if full else ""}')
        print(f'  附近 rows change         : {d_gnd:+d}  '
              f'(must be 0 — the floor is where it must NOT go)')
        ok = empty and in_pack and d_gnd == 0
        print(f'\n  {"PASS" if ok else "FAIL"}')
        return 0 if ok else 1
    finally:
        ac.close()


if __name__ == '__main__':
    sys.exit(main())
