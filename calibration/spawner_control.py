"""Hand another module a weapon or an attachment, via the training range's
item spawner.

    from calibration.spawner_control import SpawnerControl
    sc = SpawnerControl()
    sc.give_weapon('m416')          # M416 ends up in slot 2, selected
    sc.give_attachment('vert_grip') # 垂直握把 lands in 库存
    sc.give('vert_grip')            # dispatches on the key

Weapons and attachments are the same click through a different category, but
they land differently: a weapon goes into a weapon slot and can evict what was
there, an attachment always goes into the backpack.

Every weapon lands in **slot 2**, always, and that is what makes the interface
stateless. The spawner's rule is: empty weapon slots -> the gun goes to slot
1; full -> it goes to slot 2 and evicts what was there (onto the ground). So
clicking the same gun *twice* converges from every starting state:

    empty      1st -> slot 1,  2nd -> slot 2
    full       1st -> slot 2,  2nd -> slot 2 again
    half full  1st -> slot 2,  2nd -> slot 2 again

No need to read the current loadout, and no dependence on the half-full case
that has never been measured. The cost is one extra gun on the floor whenever
the slots were already full.

The panel must already be open — this cannot walk the character to a spawner.
sync() fails loudly if it is not on screen.
"""
import argparse
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from detector.attachment_catalog import ROSTER, ATTACHMENTS, is_live
from detector.spawner_detector import SpawnerDetector
from detector.spawner_layout import (CHANGE_MIN, PARK_XY, bright_mask,
                                     capture_screen, column_boxes,
                                     column_diff, find_menu,
                                     find_submenu_items)
from press.pico_mouse import HID_KEY_2
from press.pointer import Pointer, game_focused, ensure_focus, move_cursor

OPEN_WAIT = 0.45       # submenu slide-open animation
CLOSE_WAIT = 0.40


def shoot_parked(settle=0.10):
    """Screenshot with the cursor off the panel, so no row is hover-lit."""
    move_cursor(PARK_XY)
    time.sleep(settle)
    return capture_screen()

# Which category row each weapon class lives in, as (column, row) into the
# collapsed layout. Verified against the captured run: the per-class counts in
# ROSTER match what each category expands to (AR 13, DMR 7, SMG 8, LMG 2).
CATEGORY_OF_CLASS = {
    'AR':  (1, 1),    # 突击步枪
    'DMR': (1, 3),    # 射手步枪
    'SMG': (1, 5),    # 冲锋枪
    'LMG': (1, 10),   # 轻机枪
}

# Same for attachments. Verified entry by entry against the captured submenus:
# 弹匣 / 枪口 / 枪托 / 瞄准镜 match ATTACHMENTS' order exactly.
CATEGORY_OF_SLOT = {
    'grip':     (2, 1),   # 握把
    'magazine': (2, 2),   # 弹匣
    'muzzle':   (2, 3),   # 枪口
    'stock':    (2, 4),   # 枪托
    'scope':    (2, 5),   # 瞄准镜
}

# 握把 does not match: the spawner lists 箭袋 (十字弩) — the crossbow quiver —
# between 垂直握把 and 斜向握把, and ATTACHMENTS has no key for it because
# nothing on a firearm equips it. Everything after it in that category is
# therefore one position further down than the catalogue implies. Spliced in
# here rather than patched into the catalogue, which is about what fits on
# what, not about menu order.
SPAWNER_EXTRAS = {
    'grip': {6: '箭袋 (十字弩)'},   # 1-based position in the spawner's list
}

SPAWN_WAIT = 0.30     # after clicking an entry, before the next click
SWITCH_WAIT = 0.35    # after pressing 2


def weapon_position(key):
    """(category (col,row), 1-based index inside that category) for a weapon.

    The index is ROSTER's own ordering, which is the spawner's ordering — the
    catalogue was read straight off this menu.
    """
    entry = ROSTER.get(key)
    if entry is None:
        raise KeyError(f'unknown weapon {key!r}')
    if not is_live(key):
        raise ValueError(f'{key} is vaulted and no longer in the spawner')
    cls = entry[0]
    if cls not in CATEGORY_OF_CLASS:
        raise ValueError(f'{key} is class {cls}, which has no mapped category '
                         f'(only {", ".join(CATEGORY_OF_CLASS)} are catalogued)')
    peers = [k for k, v in ROSTER.items() if v[0] == cls and is_live(k)]
    return CATEGORY_OF_CLASS[cls], peers.index(key) + 1, len(peers)


def attachment_position(key):
    """(category (col,row), 1-based index, expected entry count) for an
    attachment, with the spawner's uncatalogued extras spliced in."""
    att = ATTACHMENTS.get(key)
    if att is None:
        raise KeyError(f'unknown attachment {key!r}')
    slot = att['slot']
    if slot not in CATEGORY_OF_SLOT:
        raise ValueError(f'{key} is in slot {slot}, which has no category')
    peers = [k for k, v in ATTACHMENTS.items() if v['slot'] == slot]
    index = peers.index(key) + 1
    extras = SPAWNER_EXTRAS.get(slot, {})
    for pos in sorted(extras):
        if index >= pos:
            index += 1
    return CATEGORY_OF_SLOT[slot], index, len(peers) + len(extras)


def position_of(key):
    """Dispatch a catalogue key to whichever table owns it."""
    if key in ROSTER:
        return weapon_position(key)
    if key in ATTACHMENTS:
        return attachment_position(key)
    raise KeyError(f'{key!r} is in neither ROSTER nor ATTACHMENTS')


def check_against_run(run_dir):
    """Compare every mapped category's entry count with a captured scrape.

    Offline — no game. `spawn()` makes the same comparison live and refuses to
    click on a mismatch, but by then a run is already half done; this answers
    the same question from docs/spawner/runs/<stamp>/ before anything moves.

    It is what found 箭袋 (十字弩): 握把 expanded to 7 entries where the
    catalogue accounted for 6, which had silently shifted 斜向握把 down a slot.
    """
    base = cv2.imread(os.path.join(run_dir, '00_baseline.png'))
    if base is None:
        print(f'no 00_baseline.png in {run_dir}')
        return 1
    boxes = column_boxes(find_menu(base, verbose=False))

    wanted = [(f'{cls:<9}', cat, len([k for k, v in ROSTER.items()
                                      if v[0] == cls and is_live(k)]))
              for cls, cat in CATEGORY_OF_CLASS.items()]
    wanted += [(f'{slot:<9}', cat,
                len([k for k, v in ATTACHMENTS.items() if v['slot'] == slot])
                + len(SPAWNER_EXTRAS.get(slot, {})))
               for slot, cat in CATEGORY_OF_SLOT.items()]

    bad = 0
    for label, (col, row), expect in wanted:
        shot = cv2.imread(os.path.join(run_dir, f'col{col}_row{row:02d}_open.png'))
        if shot is None:
            print(f'  {label} col{col}_row{row:02d}  no capture in this run')
            bad += 1
            continue
        n = len(find_submenu_items(shot, boxes[col]))
        ok = n == expect
        bad += not ok
        print(f'  {label} col{col}_row{row:02d}  spawner={n:2d}  '
              f'catalogue={expect:2d}  {"OK" if ok else "MISMATCH"}')
    print(f'\n{bad} categor{"y" if bad == 1 else "ies"} out of step — indices '
          f'derived from the catalogue are unsafe' if bad else
          '\nevery mapped category matches; indices are safe to click')
    return 1 if bad else 0


class SpawnerControl:
    """Click things in the item spawner on behalf of another module."""

    def __init__(self, backend='auto', verbose=True):
        self.pointer = Pointer(backend)
        self.screen = SpawnerDetector()
        self.verbose = verbose
        self.menu = None        # {col: [MenuItem]} of the collapsed panel
        self.boxes = None       # {col: (x0, x1)}
        self.base_mask = None

    def _log(self, msg):
        if self.verbose:
            print(f'[spawner] {msg}', flush=True)

    # ── Layout ──

    def sync(self):
        """Read the collapsed panel and cache its layout. False if not up."""
        if not game_focused():
            self._log('game is not the foreground window')
            return False
        base = shoot_parked(settle=0.20)
        if self.screen.ready and not self.screen.classify(base):
            self._log('not on the item-spawner screen '
                      f'(icon scores {self.screen.scores(base)})')
            return False
        menu = find_menu(base, verbose=False)
        if not menu:
            self._log('no category columns found')
            return False
        self.menu, self.boxes = menu, column_boxes(menu)
        self.base_mask = bright_mask(base)
        self._log(f'synced: {sum(len(v) for v in menu.values())} categories, '
                  f'boxes {self.boxes}')
        return True

    def _category(self, col, row):
        items = (self.menu or {}).get(col, [])
        if not 1 <= row <= len(items):
            raise IndexError(f'category col{col}_row{row:02d} does not exist '
                             f'({len(items)} rows in column {col})')
        return items[row - 1]

    def _is_expanded(self, shot, col):
        return column_diff(self.base_mask, bright_mask(shot),
                           self.boxes[col]) > CHANGE_MIN

    # ── Primitives ──

    def expand(self, col, row, retries=2):
        """Open a category. Returns its submenu entries, or [] on failure."""
        item = self._category(col, row)
        for attempt in range(retries + 1):
            shot = shoot_parked()
            if self._is_expanded(shot, col):
                entries = find_submenu_items(shot, self.boxes[col])
                if entries:
                    return entries
            self.pointer.click_at(item.click_x, item.y)
            time.sleep(OPEN_WAIT)
        self._log(f'col{col}_row{row:02d} would not expand')
        return []

    def collapse(self, col, row, retries=2):
        """Close a category and confirm the panel is back to the baseline."""
        item = self._category(col, row)
        for _ in range(retries + 1):
            if not self._is_expanded(shoot_parked(), col):
                return True
            self.pointer.click_at(item.click_x, item.y)
            time.sleep(CLOSE_WAIT)
        self._log(f'col{col}_row{row:02d} is stuck expanded — the cached '
                  f'coordinates are no longer valid, call sync() again')
        return False

    def spawn(self, col, row, index, times=1, expect=None):
        """Click entry #index (1-based) of a category, `times` times.

        Re-locates the entry before every click, so it does not matter whether
        the game leaves the submenu open after a spawn.

        `expect` is how many entries the catalogue says this category has. A
        mismatch means the game's list moved under the catalogue and every
        index derived from it is suspect — that fails rather than clicking
        whatever now happens to sit at that position.
        """
        if self.menu is None and not self.sync():
            return {'ok': False, 'error': 'not synced'}

        clicked = 0
        for _ in range(times):
            entries = self.expand(col, row)
            if not entries:
                self.collapse(col, row)
                return {'ok': False, 'error': 'expand failed',
                        'clicked': clicked}
            if expect is not None and len(entries) != expect:
                self.collapse(col, row)
                return {'ok': False, 'clicked': clicked, 'entries': len(entries),
                        'error': f'category shows {len(entries)} entries, the '
                                 f'catalogue says {expect} — indices are stale, '
                                 f're-scrape before trusting them'}
            if not 1 <= index <= len(entries):
                self.collapse(col, row)
                return {'ok': False, 'clicked': clicked,
                        'error': f'entry {index} out of range, category has '
                                 f'{len(entries)}'}
            e = entries[index - 1]
            self.pointer.click_at(e['click_x'], e['y'])
            clicked += 1
            time.sleep(SPAWN_WAIT)

        ok = self.collapse(col, row)
        return {'ok': ok, 'clicked': clicked,
                'error': None if ok else 'stuck expanded'}

    def switch_to_slot2(self):
        """Press 2. Selects whatever now sits in the second weapon slot."""
        mouse = self.pointer.pico
        if mouse is None:
            self._log('no Pico: cannot press 2 (SendInput has no key path)')
            return False
        mouse.key(HID_KEY_2, 60)
        time.sleep(SWITCH_WAIT)
        return True

    # ── The interface other modules call ──

    def give_weapon(self, key, switch=True):
        """Put weapon `key` in slot 2 and select it.

        Clicks the entry twice; see the module docstring for why that is
        state-independent. Returns a record with ok/clicked/error.
        """
        (col, row), index, expect = weapon_position(key)
        self._log(f'{key} -> col{col}_row{row:02d} entry {index}/{expect} '
                  f'({ROSTER[key][1]})')
        rec = self.spawn(col, row, index, times=2, expect=expect)
        rec.update(weapon=key, category=(col, row), index=index)
        if rec['ok'] and switch:
            rec['switched'] = self.switch_to_slot2()
        return rec

    def give_attachment(self, key, count=1):
        """Spawn attachment `key` into the backpack, `count` times.

        Only one click per copy: attachments do not evict anything, they land
        in the backpack. Getting one onto a gun from there is
        tools/attach_control.py's job — this only puts it within reach.
        """
        (col, row), index, expect = attachment_position(key)
        self._log(f'{key} -> col{col}_row{row:02d} entry {index}/{expect} '
                  f'({ATTACHMENTS[key]["zh"]})')
        rec = self.spawn(col, row, index, times=count, expect=expect)
        rec.update(attachment=key, category=(col, row), index=index)
        return rec

    def give(self, key, **kw):
        """Whichever of the two applies to `key`."""
        if key in ROSTER:
            return self.give_weapon(key, **kw)
        if key in ATTACHMENTS:
            return self.give_attachment(key, **kw)
        raise KeyError(f'{key!r} is in neither ROSTER nor ATTACHMENTS')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('weapon', nargs='?',
                    help='ROSTER or ATTACHMENTS key, e.g. m416 / vert_grip')
    ap.add_argument('--list', action='store_true',
                    help='print everything this can spawn, with its index')
    ap.add_argument('--check', nargs='?', const='latest', metavar='RUN_DIR',
                    help='offline: check every category count against a '
                         'captured scrape run (default: the newest)')
    ap.add_argument('--no-switch', action='store_true',
                    help='weapons: spawn but do not press 2')
    ap.add_argument('--count', type=int, default=1,
                    help='attachments: how many copies into the backpack')
    ap.add_argument('--countdown', type=int, default=5)
    ap.add_argument('--backend', default='auto',
                    choices=('auto', 'pico', 'sendinput'))
    args = ap.parse_args()

    if args.check:
        run = args.check
        if run == 'latest':
            runs = sorted(glob.glob(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'docs', 'spawner', 'runs', '*')))
            if not runs:
                print('no scrape runs under docs/spawner/runs/')
                return 1
            run = runs[-1]
        print(f'checking against {os.path.relpath(run)}\n')
        return check_against_run(run)

    if args.list:
        for cls, cat in CATEGORY_OF_CLASS.items():
            peers = [k for k, v in ROSTER.items() if v[0] == cls and is_live(k)]
            print(f'{cls}  col{cat[0]}_row{cat[1]:02d}')
            for i, k in enumerate(peers, 1):
                print(f'   {i:2d}. {k:<10} {ROSTER[k][1]}')
        for slot, cat in CATEGORY_OF_SLOT.items():
            print(f'{slot}  col{cat[0]}_row{cat[1]:02d}')
            extras = SPAWNER_EXTRAS.get(slot, {})
            i = 0
            for k, v in ATTACHMENTS.items():
                if v['slot'] != slot:
                    continue
                i += 1
                while i in extras:
                    print(f'   {i:2d}. {"(not catalogued)":<16} {extras[i]}')
                    i += 1
                print(f'   {i:2d}. {k:<16} {v["zh"]}')
        return 0

    if not args.weapon:
        ap.error('give a weapon or attachment key, or --list')

    (col, row), index, expect = position_of(args.weapon)
    label = (ROSTER[args.weapon][1] if args.weapon in ROSTER
             else ATTACHMENTS[args.weapon]['zh'])
    print(f'{args.weapon} = {label}, '
          f'category col{col}_row{row:02d}, entry {index} of {expect}')
    print('>>> Taking the foreground. The spawner panel must be OPEN and '
          'collapsed.')
    if not ensure_focus(countdown_s=args.countdown, label='the spawner'):
        print('[!] ABORT: could not focus the game.')
        return 1
    time.sleep(0.6)

    sc = SpawnerControl(args.backend)
    if not sc.sync():
        return 1
    if args.weapon in ROSTER:
        rec = sc.give_weapon(args.weapon, switch=not args.no_switch)
    else:
        rec = sc.give_attachment(args.weapon, count=args.count)
    print(f'\n{rec}')
    return 0 if rec['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())

