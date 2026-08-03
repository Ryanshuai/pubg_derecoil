"""How fast can the Tab screen be dragged and still land? Needs the game.

    pixi run python tools/probe_drag_speed.py --reps 6

Every calibration run fits attachments through InventoryControl, so the drag
gesture's timing is on all of their critical paths. The numbers in
press/pointer.py were chosen to be safe, never measured -- a full gesture
costs 0.54 s of fixed sleeps, and a human does the same drag in about 0.2.

Method: fit an attachment onto the gun and pull it back off, over and over,
with one timing parameter dialled down at a time. EVERY repetition is verified
by reading the slot back (that is InventoryControl.equip/unequip's own check, the
same one the calibration runs rely on), because a drag that silently does not
land is exactly the failure worth catching -- the item goes back where it came
from, or onto the floor, and nothing downstream notices.

Reports, per setting: landed / attempted, and the median seconds per drag. A
setting is only usable if it landed every time.
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
from press.pico_mouse import HID_KEY_TAB, get_mouse

# Sweeps, coarse first. Each is (kwarg, [values]) applied on top of the
# defaults, one parameter at a time so a failure names its own cause.
SWEEPS = [
    ('settle', [0.12, 0.06, 0.03, 0.01]),   # cursor placed -> button down
    ('grab',   [0.12, 0.06, 0.03, 0.01]),   # button down -> first move
    ('steps',  [10, 6, 3, 2]),              # interpolated positions on the way
    ('hover',  [0.14, 0.07, 0.03, 0.01]),   # at target -> button up
]

GUN = 'm416'
PART = 'comp_ar'        # muzzle, fits the m416
SLOT = 'muzzle'
GUN_SLOT = 2            # the spawner always lands a weapon in slot 2




def one_cycle(ac):
    """Fit the part, then pull it off. Both read back. -> (ok, seconds, err)

    The look() that locates the part is deliberately OUTSIDE the timer: it is
    the same cost whatever the gesture timing is, and including it would hide
    the thing being measured.
    """
    view = ac.look()
    if view is None:
        return False, 0.0, 'could not read the Tab screen'
    item = view.find(PART)
    if item is None:
        return False, 0.0, f'{PART} is not on screen (backpack empty?)'

    t0 = time.perf_counter()
    up = ac.equip(GUN_SLOT, SLOT, item, att=PART, retries=0)
    if not up.get('ok'):
        return False, time.perf_counter() - t0, up.get('error') or 'equip failed'
    down = ac.unequip(GUN_SLOT, SLOT, retries=0)
    dt = time.perf_counter() - t0
    if not down.get('ok'):
        return False, dt, down.get('error') or 'unequip failed'
    return True, dt, None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--reps', type=int, default=6)
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    if not ensure_focus(countdown_s=args.countdown, label='the drag probe'):
        print('[!] could not focus the game')
        return 1
    time.sleep(0.6)

    print('=== stocking up ===')
    sc = SpawnerControl(verbose=False)
    if not sc.ensure_panel(True) or not sc.sync(need_cols=(1, 2, 3)):
        print('[!] spawner would not come up')
        return 1
    rec = sc.give_many(['backpack3', GUN, PART, PART, PART], switch=True)
    print(f"  give_many: ok={rec['ok']} clicks={rec['clicks']} err={rec['error']}")
    sc.ensure_panel(False)
    time.sleep(0.5)

    mouse = get_mouse()
    ac = InventoryControl(verbose=False)
    if not ac.ensure_tab(True):
        print('[!] Tab would not open')
        return 1
    if not ac.sync():
        print('[!] InventoryControl would not sync')
        return 1
    # Naming the gun makes equip() refuse a slot this weapon does not have,
    # before the mouse moves.
    ac.guns[GUN_SLOT] = GUN

    print(f'\n=== baseline ({args.reps} cycles, shipped timing) ===')
    base_ok, base_t = 0, []
    for i in range(args.reps):
        ok, dt, err = one_cycle(ac)
        base_ok += ok
        base_t.append(dt)
        print(f'  {i}: {"ok " if ok else "FAIL"} {dt:.2f}s'
              + (f'  {err}' if err else ''))
    if not base_t or base_ok == 0:
        print('[!] even the shipped timing does not land — stopping. Is the '
              'part in the backpack and the gun in slot 1?')
        return 1
    base_med = statistics.median(base_t)
    print(f'  baseline: {base_ok}/{args.reps} landed, median {base_med:.2f}s')

    best = {}
    keeper = focus_keeper()
    for name, values in SWEEPS:
        print(f'\n=== {name} ===')
        for v in values:
            if not keeper.ok(f'sweep {name}={v}'):
                return 1
            ac.timing[name] = v
            ok_n, times = 0, []
            for _ in range(args.reps):
                ok, dt, _err = one_cycle(ac)
                ok_n += ok
                times.append(dt)
            med = statistics.median(times)
            flag = 'OK  ' if ok_n == args.reps else 'FAIL'
            print(f'  {name}={v!s:>5}  {flag}  {ok_n}/{args.reps} landed  '
                  f'median {med:.2f}s')
            if ok_n == args.reps:
                best[name] = (v, med)
            else:
                break          # slower settings above it already passed
        # keep the fastest that held, for the next parameter's baseline
        ac.timing[name] = best.get(name, (values[0], 0))[0]

    print('\n=== fastest setting that landed every time ===')
    for name, (v, med) in best.items():
        print(f'  {name:8s} {v!s:>5}   (median cycle {med:.2f}s)')
    print(f'  combined: {ac.timing}')

    print(f'\n=== combined, {args.reps} cycles ===')
    ok_n, times = 0, []
    for i in range(args.reps):
        ok, dt, err = one_cycle(ac)
        ok_n += ok
        times.append(dt)
        print(f'  {i}: {"ok " if ok else "FAIL"} {dt:.2f}s'
              + (f'  {err}' if err else ''))
    med = statistics.median(times)
    print(f'  {ok_n}/{args.reps} landed, median {med:.2f}s '
          f'(baseline {base_med:.2f}s, {100 * (1 - med / base_med):.0f}% faster)')

    ac.ensure_tab(False)
    ac.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
