"""The address system, offline. No game, no screen, no hardware.

    pixi run locations

control/inventory.py's locations are pure tuples and the functions over them are
pure too, so the whole vocabulary is testable with nothing running. It was
not tested, and that cost a working feature: at_gun() was added with a grab
point and a drop_weapon() built on it, but _reject() had never been taught
the new shape, so it answered "source ('gun', 1) is not a location" and every
gun drop was refused before the mouse moved.

One import-free loop would have caught it. This is that loop.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from control.inventory import (MOVES, InventoryControl, at_ground, at_gun,
                            at_inv, at_slot, batch, is_gun, is_slot, kind_of,
                            loc_str, move_info, parse_loc, step)
from detector.tab_layout import INV_ROWS, gun_tag_point, row_point

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {name:<46} {got!r}'
          + ('' if ok else f'  != {want!r}'))
    if not ok:
        FAILS.append(name)


reject = InventoryControl._reject

print('\n=== every address is a location _reject accepts ===')
# The gun-drop drag, spelled out: grab the boxed 1/2, release over 附近.
check('gun 1 -> ground', reject(at_gun(1), at_ground(), None), None)
check('gun 2 -> ground', reject(at_gun(2), at_ground(), None), None)
# The gun address is VALID on either side -- that is what this line has
# always been about, and it is the exact bug it guards: _reject once answered
# "source ('gun', 1) is not a location". It is asserted through the error
# MESSAGE now, because gun -> 库存 is not a move (you cannot put a rifle in
# the backpack) and MOVES refuses the pair. Refused for the right reason is
# the whole distinction.
_why = reject(at_gun(1), at_inv(), None)
check('gun address is understood on the source side',
      'is not a location' not in (_why or ''), True)
check('slot -> ground', reject(at_slot(1, 'muzzle'), at_ground(), None), None)
check('inv row -> slot', reject(at_inv(3), at_slot(2, 'scope'), None), None)
check('ground -> 库存', reject(at_ground(0), at_inv(), None), None)

print('\n=== and every bad one is still refused ===')
for name, args in [
    ('gun 3 does not exist', (at_gun(3), at_ground())),
    ('slot on gun 3', (at_slot(3, 'muzzle'), at_ground())),
    ('no such slot name', (at_slot(1, 'bayonet'), at_ground())),
    ('row past the end', (at_inv(INV_ROWS), at_ground())),
    ('same place twice', (at_inv(2), at_inv(2))),
    ('not a location at all', (('rucksack', 0), at_ground())),
]:
    got = reject(args[0], args[1], None)
    ok = got is not None
    print(f'  {"ok  " if ok else "FAIL"}  {name:<46} {got!r}')
    if not ok:
        FAILS.append(name)

print('\n=== gun vs slot are different addresses ===')
check('is_gun on a gun', is_gun(at_gun(1)), True)
check('is_gun on a slot', is_gun(at_slot(1, 'muzzle')), False)
check('is_slot on a gun', is_slot(at_gun(1)), False)
check('loc_str gun', loc_str(at_gun(1)), 'gun1')
check('loc_str slot', loc_str(at_slot(1, 'muzzle')), 'gun1.muzzle')
check('parse gun:1', parse_loc('gun:1'), at_gun(1))
check('parse slot:1:muzzle', parse_loc('slot:1:muzzle'), at_slot(1, 'muzzle'))
check('parse gun:1:muzzle (kept)', parse_loc('gun:1:muzzle'),
      at_slot(1, 'muzzle'))

print('\n=== the gesture goes where it was described to go ===')
# "点住那个一和二，然后向左拖，拖到 ground 下边那儿松手"
for g in (1, 2):
    sx, sy = gun_tag_point(g)
    dx, dy = row_point(0, 'nearby')
    check(f'gun{g} drag is leftward', dx < sx, True)
    check(f'gun{g} lands in the 附近 column', 565 <= dx <= 880, True)

print("\n=== MOVES: every move a public method makes must be in the table ===")
# THE POINT OF THIS BLOCK. at_gun() shipped with a grab point, a point_of()
# and a drop_weapon() built on it, and _reject() had never been told the
# shape -- so every gun drop was refused before the mouse moved, reported as
# "the drag failed", and stayed broken for a year. That was ONE missing
# branch in a validator nothing enumerated. Now the validator gates on MOVES,
# so the same hole is possible again in a new form: a pair the table has not
# heard of. This walks the pairs the methods actually build.
METHOD_MOVES = [
    ('equip from 库存',   at_inv(3),              at_slot(1, 'muzzle')),
    ('equip from 地面',   at_ground(2),           at_slot(2, 'grip')),
    ('unequip to 库存',   at_slot(1, 'muzzle'),   at_inv()),
    ('unequip to 地面',   at_slot(1, 'muzzle'),   at_ground()),
    ('discard from 库存', at_inv(0),              at_ground()),
    ('stow',              at_ground(0),           at_inv()),
    ('drop_weapon',       at_gun(1),              at_ground()),
    ('transfer, step 1',  at_slot(1, 'scope'),    at_inv()),
    ('transfer, step 2',  at_inv(0),              at_slot(2, 'scope')),
    ('slot to slot',      at_slot(1, 'scope'),    at_slot(2, 'scope')),
]
for name, src, dst in METHOD_MOVES:
    check(f'{name} is a known move', move_info(src, dst) is not None, True)
    check(f'{name} is not rejected', reject(src, dst, None), None)

print("\n=== ...and a pair that is NOT a move is refused, by name ===")
# Two perfectly good addresses and no such action. Without the MOVES gate this
# went out as a real 1600 px drag and came back a mystery.
for name, src, dst in [('gun into the backpack', at_gun(1), at_inv()),
                       ('gun onto a slot', at_gun(1), at_slot(2, 'muzzle')),
                       ('slot onto a gun', at_slot(1, 'muzzle'), at_gun(2))]:
    why = reject(src, dst, None)
    check(f'{name} refused', why is not None and 'MOVES' in why, True)

print("\n=== every MOVES entry says how it knows ===")
# attachment_catalog.SLOTS shipped as 22 wiki readings, 6 guesses and 2
# screenshot reads with 0 measured, all indistinguishable, and two wrong
# entries silently dropped attachments on the floor. A capability table that
# cannot say how it knows repeats that.
for pair, info in sorted(MOVES.items()):
    check(f'{pair[0]}->{pair[1]} has evidence',
          info.get('evidence') in ('measured', 'used', 'untested'), True)
    check(f'{pair[0]}->{pair[1]} names a gesture',
          info.get('gesture') in ('click', 'drag'), True)
    check(f'{pair[0]}->{pair[1]} says if it is verifiable',
          isinstance(info.get('verified'), bool), True)
check('kind_of on a slot', kind_of(at_slot(1, 'muzzle')), 'weapon')
check('kind_of on a bare panel', kind_of(at_inv()), 'inventory')
# transfer() must not silently take the untested route -- see its docstring.
check('slot->slot is still untested',
      MOVES[('weapon', 'weapon')]['evidence'], 'untested')
# The one that is 0/4 by drag. If this ever says 'drag', somebody has
# forgotten why hold() exists.
check('库存->槽位 lands by right-click, not drag',
      MOVES[('inventory', 'weapon')]['gesture'], 'click')

print("\n=== the two record shapes, and only two ===")
st = step(at_inv(0), at_slot(1, 'muzzle'), ok=True, verified=True, extra=9)
check('step has the seven keys', all(k in st for k in
      ('ok', 'verified', 'src', 'dst', 'checks', 'attempts', 'error')), True)
check('step keeps extras', st['extra'], 9)
check('batch of all-ok is ok', batch([st, dict(st)])['ok'], True)
check('one bad step sinks the batch',
      batch([st, step(error='no')])['ok'], False)
# A batch that never reached its first step has no failing step to point at,
# so all(...) over an empty list would call it a success.
check('an empty batch with an error is NOT ok',
      batch([], error='the Tab screen never came up')['ok'], False)
check('an empty batch with no error is ok', batch([])['ok'], True)

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
    sys.exit(1)
print('all ok')
