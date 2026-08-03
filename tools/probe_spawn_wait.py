"""How fast can the spawner be clicked before items go missing? Needs the game.

    pixi run python tools/probe_spawn_wait.py --n 5

SPAWN_WAIT is the pause after clicking a submenu entry, and at 0.30 s it is
most of what give_many() spends: six parts means 1.8 s of pure sleeping.

The only honest test is counting what actually arrived. Clicking faster than
the game accepts does not raise an error — the click is simply eaten, and the
run continues believing it spawned something. So each setting spawns N copies
and then opens the backpack and counts them.
"""
import argparse
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

import control.spawner as spawner_mod
from control.inventory import InventoryControl
from control.focus import ensure_focus
from control.lobby import LobbyControl
from control.spawner import SpawnerControl
from press.pico_mouse import get_mouse

PART = 'comp_ar'
WAITS = [0.30, 0.15, 0.08, 0.04, 0.0]




def count(mouse, ac):
    """How many PART are in 库存 + 附近 right now."""
    if not ac.ensure_tab(True) or not ac.sync():
        return None
    v = ac.look()
    if v is None:
        return None
    return sum(1 for p in ('inventory', 'nearby')
               for it in getattr(v, p) if it is not None and it.key == PART)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--n', type=int, default=5, help='copies per setting')
    ap.add_argument('--countdown', type=int, default=4)
    args = ap.parse_args()

    if not ensure_focus(countdown_s=args.countdown, label='the spawn-wait probe'):
        return 1

    # A training-range session ends on its own, and a probe that stops there is
    # a probe that needs a human. Walking back is what LobbyControl is for.
    with LobbyControl(verbose=False) as lc:
        st = lc.state()
        if not st.playable:
            print(f'not in a match ({st.value}) — walking back in ...')
            rec = lc.ensure_in_match()
            if not rec['ok']:
                print(f'[!] could not get into the range: {rec["error"]}')
                return 1
            print(f'  in, after {rec["elapsed"]:.0f}s via {rec["states"]}')
            time.sleep(1.0)

    mouse = get_mouse()
    ac = InventoryControl(verbose=False)
    sc = SpawnerControl(verbose=False)
    original = spawner_mod.SPAWN_WAIT

    print(f'{"wait":>6}  {"asked":>5}  {"arrived":>7}  {"s/item":>7}')
    for w in WAITS:
        before = count(mouse, ac)
        if before is None:
            print('[!] could not read the backpack')
            return 1
        if not ac.ensure_tab(False):
            print('[!] Tab would not close')
            return 1
        time.sleep(0.3)

        spawner_mod.SPAWN_WAIT = w
        if not sc.ensure_panel(True) or not sc.sync(need_cols=(2,)):
            print('[!] spawner would not come up')
            break
        t0 = time.perf_counter()
        rec = sc.give_many([PART] * args.n, switch=False)
        dt = time.perf_counter() - t0
        sc.ensure_panel(False)
        time.sleep(0.4)

        after = count(mouse, ac)
        got = None if after is None else after - before
        flag = '' if got == args.n else '   <-- LOST' if got is not None else ''
        print(f'{w:6.2f}  {args.n:5d}  {str(got):>7}  {dt / args.n:7.2f}'
              f'   ok={rec["ok"]}{flag}')
        if got is not None and got < args.n:
            print(f'  -> {w} is too fast; the last setting that kept every '
                  f'item is the floor')
            break

    spawner_mod.SPAWN_WAIT = original
    ac.ensure_tab(False)
    ac.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
