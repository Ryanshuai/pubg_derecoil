"""One part, one background, every step's Tab state printed.

    pixi run python tools/probe_fit_smoke.py

Needs the game. Takes under a minute.

WHY THIS EXISTS. collect_templates has now been started three times and
produced no usable data, and each failure cost a full run to see: the row
ordering, an unimported name, a detail threshold that answers a different
question, and now every fit reporting that all five slots changed at once. The
common factor is that the only way to find out was to spend forty minutes.

So this drives the REAL Collector -- not a reimplementation, which could pass
while the real one fails -- through exactly one part, and prints the two
numbers that say whether the screen is even the screen being read:

    tab_open()   is the inventory panel up
    inv_rows()   how many 库存 rows are drawn

If `tab_open` is False anywhere between taking the reference frame and reading
it back, nothing after that point is about the panel. The right click then
lands in the WORLD, where it means aim-down-sights -- which changes the entire
picture, hence "5 slots gained an icon, rows 12->0".
"""
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

from capture_run import CaptureRun                       # noqa: E402
from collect_templates import Collector, inv_rows        # noqa: E402
from control.focus import ensure_focus                   # noqa: E402
from control.inventory import InventoryControl, at_inv   # noqa: E402
from control.spawner import SpawnerControl               # noqa: E402
from range_session import get_session                    # noqa: E402
from sweep import Rig                                    # noqa: E402

GUN, PART, GUN_SLOT = 'sks', 'comp_ar', 1


def main():
    if not ensure_focus(countdown_s=4, label='the fit smoke test'):
        return 1
    if not get_session('auto').ensure()[0]:
        print('[!] could not get into the training range')
        return 1

    rig = Rig('red_dot')
    sc = SpawnerControl(verbose=False)
    ac = InventoryControl(verbose=False)
    run = CaptureRun.create('fit_smoke', note='one part, throwaway')
    col = Collector(rig, sc, ac, GUN_SLOT, run, ('slots', 'rows'))

    def show(tag):
        up = ac.tab_open()
        n = inv_rows(col.frame(flush=1)) if up else -1
        print(f'  {tag:<34} tab_open={str(up):<5} rows={n if n >= 0 else "-"}')
        return up

    try:
        print('\n=== setup ===')
        show('at the start')
        ac.ensure_tab(True)
        ac.clear_rack()
        # The same two steps round() takes, and in the same order: a BARE host
        # first, then the parts. Calling spawn() with both at once is what
        # round() used to do, and it is why the gun turned up wearing a 6x, a
        # suppressor, a magazine and a cheek pad -- a gun picks up whatever
        # fits as it arrives.
        col.bare_host(GUN, backpack=True)
        show('after bare_host')
        col.spawn(None, [PART], backpack=False)
        show('after the part spawns')

        print('\n=== what round() does before fit(): a rows sweep ===')
        if not col.tab():
            print('  [!] the inventory would not open')
            return 1
        rows = col.rows_of([PART])
        print(f'  rows_of -> {rows}')
        show('after rows_of')
        col.sweep(GUN, [], rows, 1, 0, 'l')     # ONE background, not six
        show('after the rows sweep')

        print('\n=== the two ways to name that row ===')
        # probe_equip_gesture right-clicks ac.point_of(item), where `item`
        # came from view.find(), and lands 10/10. fit_row right-clicks
        # ac.point_of(at_inv(row)), where `row` came from a COUNT, and lands
        # nothing. Either they are the same point and the gesture is not the
        # problem, or they are not and the count is wrong. Nothing else in the
        # difference is worth guessing about.
        ac.ensure_tab(True)
        view = ac.look()
        item = view.find(PART)
        print(f'  库存 as detected : '
              + ', '.join(f'{i.key}@{i.where}' for i in view.inventory if i))
        for r in rows:
            print(f'  at_inv({r})        -> {ac.point_of(at_inv(r))}')
        if item is None:
            print(f'  view.find({PART!r})  -> NOT FOUND — the detector cannot '
                  f'see it, so this comparison says nothing')
        else:
            print(f'  find({PART}).where -> {item.where}  '
                  f'-> {ac.point_of(item.where)}')

        # Is the target slot ALREADY wearing one? "the right click did nothing"
        # and "it swapped a comp_ar for an identical comp_ar" are the same
        # picture, and probe_equip_gesture guards against exactly that by
        # emptying the slot and alternating two different muzzles. If the gun
        # picked one up out of 库存 as it spawned -- which this file's own
        # docstring says happens -- then the fit had nothing to change.
        print('\n=== is the slot already occupied? ===')
        worn = ac.read_slots(GUN_SLOT)
        print(f'  read_slots(gun{GUN_SLOT}) : '
              + ', '.join(f'{s}={v or "-"}' for s, v in worn.items()))

        print('\n=== fit() ===')
        col.ac.held = None
        col.ac.hold(GUN_SLOT)
        show('after hold()')
        found, recs = col.fit(rows, [PART])
        show('after fit()')
        print(f'\n  found  : {found}')
        for row, r in recs.items():
            print(f'  row{row}: ok={r["ok"]} slot={r["slot"]} '
                  f'change={r["change"]:.1f} err={r["error"]}')

        print('\n── reading ──')
        print('  A False anywhere between "after the rows sweep" and "after')
        print('  fit()" means the right click went to the WORLD, not the')
        print('  panel. In the world a right click is aim-down-sights, which')
        print('  redraws everything — which is what "5 slots changed" is.')
    finally:
        try:
            ac.ensure_tab(False)
        except Exception:
            pass
        col.close()
        rig.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
