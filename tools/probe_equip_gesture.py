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

from control.inventory import InventoryControl
from control.focus import ensure_focus
from control.spawner import SpawnerControl
from press.pico_mouse import get_mouse

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

    # "the spawner would not come up" is what being in the lobby looks like
    # from here, and the range evicts on its own clock. Walk back in rather
    # than reporting a panel problem.
    from range_session import get_session
    session = get_session('auto')
    if not session.ensure()[0]:
        print('[!] could not get into the training range')
        return 1

    mouse = get_mouse()

    if not args.no_stock:
        print('=== stocking up ===')
        sc = SpawnerControl(verbose=False)
        if not sc.ensure_panel(True) or not sc.sync(need_cols=(1, 2, 3)):
            print('[!] spawner would not come up')
            return 1
        want = ['backpack3', GUN] + [PART_A] * 3 + [PART_B] * 3
        rec = sc.give_many(want, switch=True)
        if not rec['ok']:
            # The accordion: a category that will not expand because a
            # different one is. control/stock.restock retries exactly this way
            # -- drop the cached menu, re-read the layout, go again -- and this
            # probe had no such path, so one stuck panel meant zero spares and
            # four conditions reporting "out of spares" instead of a result.
            print(f"  [!] {rec['error']} — re-reading the layout and retrying")
            sc.menu = None
            sc.sync(need_cols=(1, 2, 3))
            rec = sc.give_many(want, switch=True)
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

    # 2x2, because the first run of this probe varied ONE of the two things
    # that matter. It compared right-click against drag and never called
    # hold(), so "drag lands 0 out of 4" was measured with the gun not in
    # hand -- and control/CLAUDE.md's rule that a right-click only reaches the
    # HELD weapon says holding is exactly the variable that was left out.
    # Either the rule is wrong (the right click landed 4/4 without holding) or
    # the drag was never given the condition it needed. One of those is true
    # and the old table cannot say which.
    results = {}
    for label, held in (('right-click', False), ('right-click', True),
                        ('drag', False), ('drag', True)):
        print(f'\n=== {label}, gun {"in hand" if held else "not held"} ===')
        ok_n, times = 0, []
        for i in range(args.reps):
            # Re-taken every repetition: equipping can put the gun away, and a
            # belief about what is in hand is the kind of state this repo has
            # been bitten by before.
            ac.held = None
            if held and not ac.hold(GUN_SLOT):
                print(f'  {i}: could not take gun{GUN_SLOT} in hand')
                continue
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
                # gesture='drag' forced. 'auto' picks the right click whenever
                # the gun is in hand, which in the held half of this table
                # would quietly measure the right click twice and report it as
                # the drag.
                rec = ac.equip(GUN_SLOT, SLOT, item, att=part, retries=0,
                               gesture='drag')
                landed = slot_now(ac)
                ok = bool(rec.get('ok')) and landed == want
            dt = time.perf_counter() - t0
            ok_n += ok
            times.append(dt)
            print(f'  {i}: {"ok  " if ok else "FAIL"} {dt:.2f}s  '
                  f'{part:8s} {before[:22]!r} -> {landed[:22]!r}')
        if times:
            results[(label, held)] = (ok_n, len(times),
                                      statistics.median(times))

    print('\n=== summary ===')
    print(f'  {"gesture":12s} {"held":>5s}  landed   median')
    for (label, held), (ok_n, n, med) in results.items():
        print(f'  {label:12s} {str(held):>5s}  {ok_n}/{n}      {med:.2f}s')
    print('\n  docs/game_quirks.md currently records right-click 4/4 and drag'
          '\n  0/4, both measured WITHOUT holding the gun. Whatever this says,'
          '\n  update that table and control/CLAUDE.md together — the rule'
          '\n  "装用右键、卸用拖拽" is derived from those two numbers.')
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
