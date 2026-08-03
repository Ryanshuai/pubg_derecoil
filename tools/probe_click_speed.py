"""How fast can a UI click be and still register? Needs the game.

    pixi run python tools/probe_click_speed.py --reps 6

Pointer.click_at spends three fixed waits per click — cursor settle, button
hold, post-release — and nothing else in this repo has ever measured them.
They are on the critical path of everything: the spawner fires a dozen clicks
to kit one gun, and each carries all three.

The test click is a right-click that equips an attachment, because its effect
is READABLE: the slot goes from empty to the part. A click that did not
register is not a slow click, it is a missing one, and only reading the slot
back can tell the difference.

Sweeps one wait at a time, keeping the fastest value that landed every single
repetition. Prints a settings dict to paste into press/pointer.py.
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
from control.focus import ensure_focus, focus_keeper
from control.spawner import SpawnerControl
from press.pico_mouse import get_mouse

PART = 'comp_ar'
SLOT = 'muzzle'
GUN = 2

SWEEPS = [
    ('settle',  [0.12, 0.06, 0.03, 0.015, 0.0]),   # cursor placed -> press
    ('hold_ms', [80, 50, 30, 20, 10]),             # button held
    ('after',   [0.09, 0.05, 0.02, 0.0]),          # release -> return
]




def slot_now(ac):
    return ac.read_slots(GUN).get(SLOT, '')


def one_click(ac, kw):
    """Empty the slot (untimed), then right-click the part. -> (ok, seconds)"""
    if slot_now(ac):
        ac.unequip(GUN, SLOT, retries=1)
        if slot_now(ac):
            return None, 0.0          # could not reset; do not score it
    view = ac.look()
    item = view.find(PART) if view else None
    if item is None:
        return None, 0.0
    x, y = ac.point_of(item)

    t0 = time.perf_counter()
    ac.pointer.right_click_at(x, y, **kw)
    deadline = time.perf_counter() + 1.0
    while not slot_now(ac) and time.perf_counter() < deadline:
        time.sleep(0.05)
    dt = time.perf_counter() - t0
    return bool(slot_now(ac)), dt


def run(ac, kw, reps):
    ok_n, n, times = 0, 0, []
    for _ in range(reps):
        ok, dt = one_click(ac, kw)
        if ok is None:
            continue
        ok_n += ok
        n += 1
        times.append(dt)
    return ok_n, n, (statistics.median(times) if times else float('nan'))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--reps', type=int, default=6)
    ap.add_argument('--countdown', type=int, default=4)
    args = ap.parse_args()

    if not ensure_focus(countdown_s=args.countdown, label='the click probe'):
        return 1

    mouse = get_mouse()
    ac = InventoryControl(verbose=False)

    # Top up: each repetition consumes nothing, but a failed unequip can drop
    # a part on the floor and out of reach.
    ac.ensure_tab(False)
    time.sleep(0.3)
    sc = SpawnerControl(verbose=False)
    if sc.ensure_panel(True) and sc.sync(need_cols=(2,)):
        sc.give_many([PART] * 4, switch=False)
    sc.ensure_panel(False)
    time.sleep(0.5)

    if not ac.ensure_tab(True) or not ac.sync():
        print('[!] Tab would not come up')
        return 1
    ac.hold(GUN)
    ac.sync()

    kw = {}
    ok_n, n, med = run(ac, kw, args.reps)
    print(f'\nbaseline (shipped): {ok_n}/{n} landed, median {med:.3f}s')
    if n == 0 or ok_n == 0:
        print('[!] the shipped settings do not land — stopping')
        return 1
    base = med

    keeper = focus_keeper()
    for name, values in SWEEPS:
        print(f'\n=== {name} ===')
        kept = None
        for v in values:
            if not keeper.ok(f'{name}={v}'):
                return 1
            kw[name] = v
            ok_n, n, med = run(ac, kw, args.reps)
            flag = 'OK  ' if n and ok_n == n else 'FAIL'
            print(f'  {name}={v!s:>6}  {flag}  {ok_n}/{n}  median {med:.3f}s')
            if n and ok_n == n:
                kept = v
            else:
                break
        if kept is None:
            kw.pop(name, None)
            print(f'  -> keeping the shipped {name}')
        else:
            kw[name] = kept
            print(f'  -> fastest that always landed: {name}={kept}')

    print(f'\n=== combined {kw} ===')
    ok_n, n, med = run(ac, kw, max(args.reps, 8))
    print(f'  {ok_n}/{n} landed, median {med:.3f}s  '
          f'(baseline {base:.3f}s, {100 * (1 - med / base):.0f}% faster)')

    ac.ensure_tab(False)
    ac.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
