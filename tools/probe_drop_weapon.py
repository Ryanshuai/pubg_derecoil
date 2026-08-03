"""Does the boxed 1/2 throw the whole gun on the floor? Which gesture? Needs the game.

    pixi run python tools/probe_drop_weapon.py --gesture click
    pixi run python tools/probe_drop_weapon.py --gesture drag

Two gestures reach the same boxed slot number at the left of the weapon row:
RIGHT-CLICK it, or drag it LEFT into the 附近 list and release. Both are timed
here because the drag is 1621 px of travel and the click is one press, and the
difference is paid once per weapon for a whole harvest run.

The claim being tested is not just "the gun
left the rack" but "it took its attachments with it" -- that is the entire
reason for dropping the whole weapon instead of stripping it first. Stripping
puts the parts back in the backpack, where PUBG's auto-fit bolts them onto the
next gun that arrives, which is how a run labelled BARE came back wearing a
foregrip nobody asked for.

So this checks three things after the drop:

  the rack slot is empty            the drag took the weapon
  库存 did NOT gain the parts       they left with it, rather than falling off
  附近 gained rows                  they landed on the floor

tools/test_locations.py covers the addressing offline; this is the half that
only the game can answer.
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

from control.inventory import InventoryControl
from control.focus import ensure_focus
from control.lobby import LobbyControl
from control.spawner import SpawnerControl

KIT = ['aug', 'comp_ar', 'vert_grip', 'quickext_ar', 'red_dot']


def survey(ac):
    """What is in the rack, on the guns, and loose in each list."""
    view = ac.look()
    return {
        'guns': dict(ac.guns),
        'slots': ac._slot_states(ac._frame()),
        'inv': [it.key for it in view.inventory if it is not None and it.key],
        'ground': [it.key for it in view.nearby if it is not None and it.key],
        # Row COUNTS, not keyed items. A dropped weapon occupies a row but
        # matches no attachment template, so its key is None and counting keys
        # reports the floor as empty -- which is exactly what the first run of
        # this said while the gun was plainly lying there (rows_nearby was 1).
        'rows_inv': view.rows('inventory'),
        'rows_ground': view.rows('nearby'),
    }


def show(tag, s):
    print(f'  {tag}')
    print(f'    rack   : {s["guns"]}')
    for g in (1, 2):
        worn = {k: v for k, v in s['slots'][g].items() if v}
        print(f'    gun{g}   : {worn if worn else "(bare / empty)"}')
    print(f'    库存   : {s["rows_inv"]} rows, parts {sorted(set(s["inv"]))}')
    print(f'    附近   : {s["rows_ground"]} rows, parts {sorted(set(s["ground"]))}')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--gun', type=int, default=1, choices=(1, 2))
    ap.add_argument('--gesture', default='auto',
                    choices=('auto', 'click', 'drag'),
                    help="'click' right-clicks the plate, 'drag' hauls it "
                         "1621px into 附近, 'auto' clicks then falls back")
    ap.add_argument('--countdown', type=int, default=5)
    args = ap.parse_args()

    if not ensure_focus(countdown_s=args.countdown, label='the gun-drop probe'):
        return 1

    with LobbyControl(verbose=False) as lc:
        if not lc.state().playable:
            print('not in the range — walking back in ...')
            rec = lc.ensure_in_match()
            if not rec['ok']:
                print(f'[!] {rec["error"]}')
                return 1
            time.sleep(1.0)

    ac = InventoryControl(verbose=True)
    try:
        if not ac.ensure_tab(True):
            print('[!] Tab would not open')
            return 1
        before = survey(ac)

        if before['guns'].get(args.gun) is None:
            print(f'gun{args.gun} slot is empty — spawning a kit first')
            ac.ensure_tab(False)
            time.sleep(0.4)
            sc = SpawnerControl(verbose=False)
            if not (sc.ensure_panel(True) and sc.sync(need_cols=(1, 2))):
                print('[!] spawner would not come up')
                return 1
            sc.give_many(KIT)
            sc.ensure_panel(False)
            time.sleep(0.9)
            if not ac.ensure_tab(True):
                return 1
            before = survey(ac)

        print('\n=== before ===')
        show('', before)

        print(f'\n=== drop_weapon({args.gun}, gesture={args.gesture!r}) ===')
        t0 = time.perf_counter()
        rec = ac.drop_weapon(args.gun, gesture=args.gesture)
        dt = time.perf_counter() - t0
        print(f'  -> ok={rec["ok"]}  via={rec.get("gesture")!r}  '
              f'was={rec["was"]!r}  now={rec["now"]!r}  {dt:.2f}s')
        if rec.get('error'):
            print(f'     error: {rec["error"]}')
        d = rec.get('drag') or {}
        if d.get('error'):
            print(f'     drag : {d["error"]}')

        time.sleep(0.5)
        after = survey(ac)
        print('\n=== after ===')
        show('', after)

        print('\n=== verdict ===')
        gone = after['guns'].get(args.gun) is None
        print(f'  rack slot emptied        : {gone}')

        parts_before = sorted(v for v in before['slots'][args.gun].values() if v)
        inv_gain = len(after['inv']) - len(before['inv'])
        ground_rows = after['rows_ground'] - before['rows_ground']
        print(f'  gun was wearing          : {len(parts_before)} parts')
        print(f'  库存 parts change        : {inv_gain:+d}  '
              f'(must be 0 — they leave WITH the gun, not into the pack)')
        print(f'  附近 rows change         : {ground_rows:+d}  '
              f'(the gun itself; it matches no attachment template)')

        ok = gone and inv_gain == 0 and ground_rows > 0
        print(f'\n  {"PASS" if ok else "FAIL"}')
        return 0 if ok else 1
    finally:
        ac.ensure_tab(False)
        ac.close()


if __name__ == '__main__':
    sys.exit(main())
