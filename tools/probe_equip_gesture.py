"""Right-click vs drag for fitting an attachment. Needs the game.

    pixi run python tools/probe_equip_gesture.py --reps 5

Two things to settle, both reported by reading the slot back rather than by
whether the gesture "went through":

  1. does right-clicking an item in 库存 equip it, and how reliably
  2. how the two compare on wall clock

Drag is the only gesture InventoryControl has ever used, and it costs a press,
ten interpolated moves and a settle at each end. If a right-click does the
same job it is one click, and every calibration run that kits a gun gets
shorter.

The known swap rules are exercised on purpose (they are why this can loop
without cleaning up in between):
  库存 -> gun : whatever the slot held goes back to 库存
  地面 -> gun : whatever the slot held drops to the 地面
"""
import argparse
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from control.inventory import InventoryControl, at_inv
from control.focus import ensure_focus
from control.spawner import SpawnerControl
from press.pico_mouse import HID_KEY_TAB, get_mouse

GUN = 'm416'
# Two DIFFERENT muzzles, alternated. Fitting the same part twice cannot be
# verified: the slot reads the same before and after, so a gesture that did
# nothing is indistinguishable from one that worked. Alternating makes every
# repetition a real assertion -- and exercises the swap rule, since the part
# coming off goes back to 库存 on its own.
PART_A = 'comp_ar'
PART_B = 'supp_ar'
ASSET = {'comp_ar': 'Muzzle_Compensator_Large_C',
         'supp_ar': 'Muzzle_Suppressor_Large_C'}
SLOT = 'muzzle'
GUN_SLOT = 2          # the spawner always lands a weapon in slot 2




def slot_now(ac):
    return ac.read_slots(GUN_SLOT).get(SLOT, '')


def clear_slot(ac):
    """Leave the slot empty, whatever it takes. -> bool"""
    if not slot_now(ac):
        return True
    ac.unequip(GUN_SLOT, SLOT, retries=1)
    return not slot_now(ac)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--reps', type=int, default=5)
    ap.add_argument('--countdown', type=int, default=5)
    ap.add_argument('--no-stock', action='store_true')
    args = ap.parse_args()

    if not ensure_focus(countdown_s=args.countdown, label='the equip probe'):
        return 1
    time.sleep(0.6)

    mouse = get_mouse()

    if not args.no_stock:
        print('=== stocking up ===')
        sc = SpawnerControl(verbose=False)
        if not sc.ensure_panel(True) or not sc.sync(need_cols=(1, 2, 3)):
            print('[!] spawner would not come up')
            return 1
        rec = sc.give_many(['backpack3', GUN] + [PART_A] * 3 + [PART_B] * 3,
                           switch=True)
        print(f"  ok={rec['ok']} clicks={rec['clicks']} err={rec['error']}")
        sc.ensure_panel(False)
        time.sleep(0.6)

    ac = InventoryControl(verbose=False)
    ac.guns[GUN_SLOT] = GUN
    if not ac.ensure_tab(True):
        print('[!] Tab would not open')
        return 1
    if not ac.sync():
        print('[!] InventoryControl would not sync')
        return 1

    view = ac.look()
    print(f'\nslot {GUN_SLOT}.{SLOT} currently: {slot_now(ac)!r}')
    print(f'库存: {[i.key for i in view.inventory if i][:10]}')
    print(f'附近: {[i.key for i in view.nearby if i][:10]}')

    results = {}
    for label in ('right-click', 'drag'):
        print(f'\n=== {label} ===')
        ok_n, times = 0, []
        for i in range(args.reps):
            # Empty the slot first, OUTSIDE the timer. '' -> comp_ar is an
            # assertion that cannot be satisfied by a gesture that did nothing,
            # which fitting the same part onto an occupied slot cannot say.
            if not clear_slot(ac):
                print(f'  {i}: slot would not empty (reads {slot_now(ac)!r})')
                continue
            part, want = PART_A, ASSET[PART_A]
            before = slot_now(ac)

            view = ac.look()
            item = view.find(part)
            if item is None:
                print(f'  {i}: no {part} on screen — out of spares?')
                break

            t0 = time.perf_counter()
            if label == 'right-click':
                x, y = ac.point_of(item)
                ac.pointer.right_click_at(x, y)
                deadline = time.perf_counter() + 1.05
                while slot_now(ac) != want and time.perf_counter() < deadline:
                    time.sleep(0.08)
                landed = slot_now(ac)
                ok = landed == want
            else:
                rec = ac.equip(GUN_SLOT, SLOT, item, att=part, retries=0)
                landed = slot_now(ac)
                ok = bool(rec.get('ok')) and landed == want
            dt = time.perf_counter() - t0
            ok_n += ok
            times.append(dt)
            print(f'  {i}: {"ok  " if ok else "FAIL"} {dt:.2f}s  '
                  f'{part:8s} {before[:22]!r} -> {landed[:22]!r}')
        if times:
            results[label] = (ok_n, len(times), statistics.median(times))

    print('\n=== summary ===')
    for label, (ok_n, n, med) in results.items():
        print(f'  {label:12s} {ok_n}/{n} landed   median {med:.2f}s')
    if len(results) == 2:
        rc = results['right-click'][2]
        dr = results['drag'][2]
        print(f'\n  right-click is {100 * (1 - rc / dr):.0f}% faster than drag')

    clear_slot(ac)
    ac.ensure_tab(False)
    ac.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
