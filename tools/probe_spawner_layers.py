"""Live check of the spawner's L0/L1/L2 split. Needs the game and the Pico.

    pixi run python tools/probe_spawner_layers.py

Four things the offline tests cannot answer, because they depend on how the
game reacts to clicks rather than on what a stored frame looks like:

  1. ensure_panel() opens the panel from anywhere (comma is a menu key)
  2. read() reports the right node while the panel is mid-sequence
  3. goto() -- and specifically whether this menu is an accordion. `path`
     comes back 'direct' (1 click) if opening one category closes the last,
     'cleared-first' (3) if not. Nothing assumes either.
  4. give_many() against the same list spawned one at a time -- the clicks
     and the wall clock

Spawns a few attachments into the backpack. Nothing is destroyed: attachments
evict nothing, and no weapon is spawned.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from control.focus import ensure_focus, game_focused
from control.spawner import SpawnerControl

# One per category, so the batch has something to reorder. All attachments:
# they land in the backpack and evict nothing.
KEYS = ['comp_ar', 'vert_grip', 'quickext_ar', 'red_dot', 'half_grip', 'comp_sr']


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--countdown', type=int, default=6)
    ap.add_argument('--skip-compare', action='store_true',
                    help='skip the one-at-a-time run used as the baseline')
    args = ap.parse_args()

    if not ensure_focus(countdown_s=args.countdown, label='the spawner probe'):
        print('[!] could not focus the game')
        return 1

    sc = SpawnerControl()

    print('\n=== 1. ensure_panel() ===')
    t0 = time.perf_counter()
    ok = sc.ensure_panel(True)
    print(f'  opened={ok}  in {time.perf_counter() - t0:.2f}s')
    if not ok:
        print('[!] panel would not open')
        return 1

    if not sc.sync(need_cols=(1, 2)):
        print('[!] sync failed')
        return 1

    print('\n=== 2. read() ===')
    print(f'  {sc.read()!r}')

    print('\n=== 3. goto() — is this menu an accordion? ===')
    paths = []
    for col, row in [(2, 1), (2, 3), (2, 5), (2, 3), (1, 1)]:
        t0 = time.perf_counter()
        rec = sc.goto(col, row)
        dt = time.perf_counter() - t0
        paths.append(rec['path'])
        print(f"  goto({col},{row}): ok={rec['ok']} clicks={rec['clicks']} "
              f"path={rec['path']:14s} entries={len(rec['entries']):2d}  {dt:.2f}s")
        print(f'      read back: {sc.read()!r}')
    sc.collapse_all()
    direct = paths.count('direct')
    print(f'\n  verdict: {direct}/{len(paths)} moves took ONE click')
    print('  -> accordion: opening a category closes the previous one'
          if direct >= len(paths) - 1 else
          '  -> NOT an accordion: the old category had to be closed first')

    print('\n=== 4. plan() ===')
    for s in sc.plan(KEYS):
        print(f"  {s['kind']:11s} {s['key']:14s} cat={str(s['category']):8s} "
              f"idx={str(s['index']):4s} x{s['times']}")

    one_by_one = None
    if not args.skip_compare:
        print('\n=== 5a. one at a time (the old path) ===')
        t0 = time.perf_counter()
        okc = 0
        for k in KEYS:
            r = sc.give_attachment(k)
            okc += bool(r.get('ok'))
        one_by_one = time.perf_counter() - t0
        print(f'  {okc}/{len(KEYS)} ok in {one_by_one:.1f}s')

    print('\n=== 5b. give_many() ===')
    t0 = time.perf_counter()
    rec = sc.give_many(KEYS)
    batch = time.perf_counter() - t0
    okc = sum(1 for s in rec['steps'] if s.get('ok'))
    print(f"  {okc}/{len(rec['steps'])} ok in {batch:.1f}s  "
          f"entry-clicks={rec['clicks']}  error={rec['error']}")

    if one_by_one:
        print(f'\n  one at a time {one_by_one:.1f}s -> batched {batch:.1f}s '
              f'({100 * (1 - batch / one_by_one):.0f}% faster)')

    print(f'\n  final: {sc.read()!r}   focused={game_focused()}')
    sc.ensure_panel(False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
