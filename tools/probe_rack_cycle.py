"""Can the rack be cycled? Spawn two guns, throw both away, spawn two more.

    pixi run python tools/probe_rack_cycle.py --rounds 3

Fires nothing and writes no curve. It exists because the weapon axis is a
LOOP over pairs and only one pass through it had ever run -- which is exactly
the pass where the rack starts empty and every assumption holds.

What is actually being tested:

  1. Does dragging from a weapon's name plate to 附近 take the WHOLE gun?
     at_gun() infers the grab point from the plate's box, which is measured,
     but "the plate is part of the draggable row" is an inference. The check
     is the plate reading empty afterwards.

  2. Does spawning two guns into an EMPTY rack fill both slots? give_many
     clicks each weapon once for this, on the rule that an empty rack takes
     the first gun into slot 1. With a full rack both would land in slot 2
     and the second would evict the first, so the drop has to work for the
     spawn to be right.

  3. Does the floor filling up change anything? Three rounds leaves six guns
     lying in 附近, and that list is the same one attachments are dragged out
     of. Nobody has run it that full.

Each round prints what the rack read after the spawn and after the drop, so a
failure names which of the three it was.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from control.focus import ensure_focus
from control.lobby import LobbyControl
from control.spawner import SpawnerControl

from control.inventory import InventoryControl
from control.stock import open_tab

PAIRS = [('aug', 'm416'), ('akm', 'scar'), ('groza', 'qbz'),
         ('g36c', 'k2'), ('ace32', 'famas')]


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--countdown', type=int, default=8)
    args = ap.parse_args()

    print('>>> Spawns and drops guns. Fires nothing, writes no curve.')
    if not ensure_focus(countdown_s=args.countdown, label='the rack probe'):
        print('[!] could not focus the game')
        return 1
    time.sleep(0.6)
    with LobbyControl() as lc:
        if not lc.ensure_in_match()['ok']:
            print('[!] not in a match')
            return 1
    time.sleep(1.0)

    sc = SpawnerControl()
    ac = InventoryControl(verbose=False)
    ok_all = True
    try:
        # Start from a known state, so round 1 is testing the spawn and not
        # whatever the last run left behind.
        if open_tab(ac, label='clearing the rack'):
            cleared = ac.clear_rack()
            ac.ensure_tab(False)
            print(f'  cleared {len(cleared)} gun(s) to start from empty'
                  + ('' if all(c['ok'] for c in cleared) else ' — SOME FAILED'))

        for i in range(args.rounds):
            pair = PAIRS[i % len(PAIRS)]
            print(f'\nround {i}: {", ".join(pair)}')
            if not sc.ensure_panel(True):
                print('  [!] panel would not open')
                ok_all = False
                break
            sc.sync()
            res = sc.give_many(list(pair), switch=False, weapon_times=1)
            sc.ensure_panel(False)
            print(f"  spawn: {res['clicks']} clicks"
                  + ('' if res['ok'] else f" — {res['error']}"))

            if not open_tab(ac, label='reading the rack'):
                print('  [!] Tab would not open')
                ok_all = False
                break
            got = ac.read_weapons()
            worn = ac.read_slots()
            for g in (1, 2):
                parts = {s: n for s, n in worn.get(g, {}).items() if n}
                print(f'    slot {g}: {got.get(g)!r}'
                      + (f'  wearing {parts}' if parts else '  bare'))
            filled = [got.get(g) for g in (1, 2)]
            if sorted(x for x in filled if x) != sorted(pair):
                print(f'  [!] rack holds {filled}, expected {list(pair)}')
                ok_all = False

            drops = ac.clear_rack()
            ac.ensure_tab(False)
            for d in drops:
                print(f"    dropped {d['was']!r} -> "
                      + ('gone' if d['ok'] else f"STILL THERE ({d['now']!r})"))
            if not all(d['ok'] for d in drops):
                ok_all = False
                print('  [!] the rack did not empty — stopping, because every '
                      'later round\n      would be spawning into a full rack')
                break
    finally:
        ac.close()

    print('\n' + ('rack cycling works: spawn 2, drop 2, repeat.'
                  if ok_all else
                  '[!] rack cycling is NOT reliable — see the failures above.'))
    return 0 if ok_all else 1


if __name__ == '__main__':
    sys.exit(main())
