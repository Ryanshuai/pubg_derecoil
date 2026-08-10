"""Offline check of the stocktake bookkeeping — no game, no screen.

Feeds a hand-built TabView through Stock and asserts what it decides to spawn
and what it decides to throw away. The parts that touch the game (drag, spawn,
Tab toggling) are not covered here; this is only the arithmetic.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detector.tab_items import Item, TabView
from detector.attachment_catalog import ATTACHMENTS, SLOT_NAMES
from control.stock import Stock


def item(key, row, panel='inventory'):
    asset = ATTACHMENTS[key]['asset']
    it = Item(asset, ATTACHMENTS[key]['slot'], (0, 0), (panel, row), 10.0, 3.0)
    assert it.key == key, f'{key} -> {it.key}'
    return it


def view(inv_keys, worn=None, unknown=()):
    """inv_keys: [key or None] by row. worn: {gun: {slot: key}}."""
    inv = [item(k, i) if k else None for i, k in enumerate(inv_keys)]
    inv += [None] * (12 - len(inv))
    worn = worn or {}
    weapons = {g: {s: (item(worn.get(g, {}).get(s), 0, 'inventory')
                        if worn.get(g, {}).get(s) else None)
                   for s in SLOT_NAMES} for g in (1, 2)}
    return TabView(inv, [None] * 12, weapons, list(unknown))


def main():
    want = {'comp_ar', 'vert_grip', 'red_dot', 'ext_ar'}
    fails = 0

    def check(label, got, expect):
        nonlocal fails
        ok = got == expect
        fails += not ok
        print(f'  {"ok  " if ok else "FAIL"} {label}: {got!r}'
              f'{"" if ok else f"  expected {expect!r}"}')

    print('a stuffed backpack, three copies of one part, one part on the gun')
    s = Stock(view(['comp_ar', 'comp_ar', 'vert_grip', 'comp_ar', 'red_dot',
                    'half_grip'],
                   worn={2: {'magazine': 'ext_ar'}},
                   unknown=[('inventory', 6), ('inventory', 7)]),
              backpack=True)
    check('rows', s.rows, 8)
    check('unnamed rows left alone', s.unknown, 2)
    check('in_pack(comp_ar)', s.in_pack('comp_ar'), 3)
    check('ext_ar counted although worn', s.have('ext_ar'), 1)
    check('missing', sorted(s.missing(want)), [])
    check('duplicates', sorted(i.where for i in s.duplicates()),
          [('inventory', 1), ('inventory', 3)])
    check('unwanted', [i.key for i in s.unwanted(want)], ['half_grip'])

    print('\nan empty backpack')
    s = Stock(view([]), backpack=False)
    check('missing', sorted(s.missing(want)), sorted(want))
    check('duplicates', s.duplicates(), [])
    check('backpack', s.backpack, False)

    print('\nexactly right — nothing to do')
    s = Stock(view(sorted(want)), backpack=True)
    check('missing', s.missing(want), [])
    check('duplicates', s.duplicates(), [])
    check('unwanted', s.unwanted(want), [])
    print(f'\n{"all good" if not fails else str(fails) + " FAILED"}')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
