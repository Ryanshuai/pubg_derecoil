"""Spawn one weapon and check its slot layout against the catalogue.

Live counterpart to `SlotDetector`: spawns the gun, optionally strips it,
opens Tab, and prints what the screen says next to what
`attachment_catalog.SLOTS` claims. Where they differ, the screen wins — that
is the whole point, since 0 of the catalogue's entries were measured in-game
before this.

    pixi run python tools/probe_slot_boxes.py m416 --strip
    pixi run python tools/probe_slot_boxes.py vss

`--strip` matters: spawning does NOT give a bare gun. PUBG auto-fits whatever
the backpack holds, so a fresh M416 arrives wearing five parts. Slot PRESENCE
is unaffected (it reads the tile border, not the contents), but the
empty/filled half of the answer is meaningless without it.

Offline equivalent, on a saved capture:
    pixi run python detector/slot_detector.py docs/compat/m416_slots.png
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))), 'calibration'))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

import cv2
import numpy as np

from detector.attachment_catalog import SLOTS, is_live
from detector.slot_detector import SlotDetector
from detector.tab_layout import SLOT_NAMES
from control.lobby import LobbyControl
from press.pointer import Pointer
from control.focus import ensure_focus
from tools.drive_screen import SCREENS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'docs', 'compat')

_DET = SlotDetector()


def toggle(ptr, screen, want_open, settle=None):
    """Press a screen's key until it is in the wanted state. -> bool"""
    if screen.is_up() == want_open:
        return True
    ptr.pico.key(screen.key, 60)
    time.sleep(settle or (screen.open_wait if want_open else screen.close_wait))
    return screen.is_up() == want_open


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('weapon', help='ROSTER key, e.g. m416')
    ap.add_argument('--slot', type=int, default=2, choices=(1, 2),
                    help='weapon slot the spawner drops into (always 2)')
    ap.add_argument('--strip', action='store_true',
                    help='empty every slot first. Spawning does NOT give a '
                         'bare gun: PUBG auto-fits whatever the backpack '
                         'holds, so a fresh M416 arrives wearing five parts.')
    ap.add_argument('--countdown', type=int, default=5)
    ap.add_argument('--backend', default='auto')
    args = ap.parse_args()

    if not is_live(args.weapon):
        print(f'{args.weapon} is not a live weapon')
        return 1

    if not ensure_focus(countdown_s=args.countdown, label='slot boxes'):
        print('could not focus the game')
        return 1

    with LobbyControl(args.backend) as lc:
        rec = lc.ensure_in_match()
    if not rec['ok']:
        print(f'not in a match: {rec["error"]}')
        return 1

    ptr = Pointer(args.backend)
    if ptr.pico is None:
        print('no Pico — cannot send keys')
        return 1

    spawner, tab = SCREENS['spawner'], SCREENS['tab']

    # Spawn it. SpawnerControl needs the panel already up and says so loudly.
    if not toggle(ptr, spawner, True):
        print('spawner would not open')
        return 1
    from control.spawner import SpawnerControl
    sc = SpawnerControl(args.backend)
    got = sc.give_weapon(args.weapon)
    print(f'give_weapon({args.weapon}) -> {got}')
    if not toggle(ptr, spawner, False):
        print('spawner would not close')
        return 1

    if not toggle(ptr, tab, True):
        print('tab would not open')
        return 1

    if args.strip:
        from control.inventory import InventoryControl
        ac = InventoryControl(args.backend)
        if ac.sync():
            before = ac.read_slots(args.slot)
            print(f'auto-fitted on spawn: {before}')
            for r in ac.strip(args.slot)['steps']:
                if not r['ok']:
                    print(f'  could not remove {r["src"]}: {r["error"]} '
                          f'-- an integral part cannot be taken off, which '
                          f'is itself a finding')
            print(f'after strip:          {ac.read_slots(args.slot)}')
        else:
            print('attach sync failed; measuring without stripping')

    frame = tab.shoot()
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f'{args.weapon}_slots.png')
    cv2.imwrite(path, frame)
    toggle(ptr, tab, False)

    expected = SLOTS.get(args.weapon, {})
    have = set(expected.get('slots', ()))
    conf = expected.get('conf', '?')
    print(f'\nwrote {path}')
    print(f'catalogue says: {sorted(have)}  (conf={conf})\n')
    scores = _DET.scores(frame, args.slot)
    print(f'{"slot":10} {"catalogue":10} {"read":9} {"ring":>7} {"edges":>6}')
    disagree = []
    for s in SLOT_NAMES:
        v = scores[s]
        cat = 'has' if s in have else '-'
        print(f'{s:10} {cat:10} {v["state"]:9} {v["ring"]:7.1f} '
              f'{v["edges"]:6d}')
        if v['state'] == 'unknown':
            continue
        if (v['state'] in ('empty', 'filled')) != (s in have):
            disagree.append((s, cat, v['state']))

    if disagree:
        print('\nCATALOGUE DISAGREES WITH THE SCREEN:')
        for s, cat, got in disagree:
            print(f'  {s}: catalogue {cat!r}, screen {got!r}')
        print("The screen wins — update attachment_catalog.SLOTS and set "
              "conf='measured'.")
    else:
        print('\nScreen agrees with the catalogue on every readable slot.')
    print("scope always reads 'unknown': that slot draws no tile. Resolve it "
          'with a drag, never by assuming absent.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
