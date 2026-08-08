"""Is the backpack deeper than the twelve rows the reader can see?

    pixi run python tools/probe_backpack_depth.py

Needs the game. Drops ONE item and photographs the panel either side of it.
Nothing is spawned and nothing is fired.

THE QUESTION, and why the logs cannot answer it
-----------------------------------------------
An unattended run kept printing

    [stock] dropping 9: comp_arx2, quickext_arx2, ...
    [!] the backpack is unchanged after dropping 9 — is the 附近 panel drawn?

and the backpack stayed at 12 rows all night, until the parts a cell needed
could not be produced and four cells failed in a row.

Two explanations fit that log exactly, and they call for opposite fixes:

  A. THE DRAGS DO NOT LAND. press/pointer.py reported the cursor releasing 23
     px below the target every time, with x exact, which reads like a drop
     that went to the wrong row or nowhere.

  B. THE DRAGS LAND AND THE LIST REFILLS. detector/tab_layout.INV_ROWS is 12
     -- "rows visible at 1440p before scrolling". If the pack holds more than
     twelve, dropping the visible twelve lets the next twelve scroll up, and
     `_view_sig` (which compares exactly those visible rows, as its own
     docstring says) reads the same signature it did before.

Nothing in the log separates them: both give "N dropped, still 12 rows".

THE EXPERIMENT
--------------
Drop exactly ONE item and read the panel again.

    under A: the row is still there. Twelve rows, unchanged.
    under B: that row is gone, everything below shifts up, and a row that was
             never visible appears at the bottom. Still twelve rows -- but the
             LAST one is new, and that is the whole tell.

One item, because twelve at a time is what made the two indistinguishable in
the first place. The count is not the evidence; the identity of the last row
is.
"""
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2

from control.evidence import full_frame
from control.session import ensure_ready
from control.inventory import InventoryControl
from control.stock import read_stock
from detector.tab_layout import INV_ROWS

OUT = os.path.join(ROOT, 'calibration', 'artifacts', 'stock')

# Sixteen DISTINCT parts, four more than the window. Distinct on purpose: with
# duplicates a row scrolling up looks identical to the row it replaced, which
# is exactly the ambiguity that hid this for a whole night.
FILL = ['red_dot', 'holo', 'laser', 'comp_ar', 'comp_smg', 'supp_ar',
        'supp_smg', 'flash_ar', 'vert_grip', 'tilted_grip', 'half_grip',
        'thumb_grip', 'tactical_stock', 'cheek_pad', 'ext_ar', 'ext_smg']


def rows_of(stock):
    """The visible inventory rows, as names, in order."""
    # Item declares key in __slots__; the `or '?'` stays because key is None
    # for an asset with no catalogue entry, and those rows still occupy one.
    return [(i.key or '?') if i is not None else '-'
            for i in stock.view.inventory]


def show(tag, rows):
    print(f'  {tag}:')
    for n, r in enumerate(rows):
        print(f'    {n:2d}  {r}')


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    os.makedirs(OUT, exist_ok=True)
    stamp = datetime.now().strftime('%m%d_%H%M%S')

    if not ensure_ready(label='the backpack depth probe', countdown_s=4)['ok']:
        print('[!] could not focus the game')
        return 1
    time.sleep(0.5)

    # There is no backpack outside a match, and "Tab would not open" is what
    # that looks like from here -- which is how the first attempt at this
    # probe failed, minutes after a halted night had left the game in the
    # lobby. Walk back in rather than reporting a screen problem.
    sys.path.insert(0, os.path.join(ROOT, 'calibration'))
    from calibration.range_session import get_session
    session = get_session('auto')
    ok, _ = session.ensure()
    if not ok:
        print('[!] could not get into the training range')
        return 1

    # Fill the pack past the window on purpose. A fresh spawn point hands out
    # an EMPTY backpack, and an empty one cannot answer the question -- the
    # first run of this probe read twelve dashes and had nothing to drop.
    #
    # The fill is itself half the evidence: SPAWN_N distinct parts go in, and
    # if the reader still says INV_ROWS then the window is a window.
    from control.spawner import SpawnerControl
    from control.stock import restock
    with SpawnerControl() as sc:
        if not restock(ac_probe := InventoryControl(verbose=False), sc,
                       FILL, backpack='backpack3', drop_unwanted=False):
            print('[!] could not stock the backpack')
        ac_probe.close()

    ac = InventoryControl(verbose=False)
    try:
        stock = read_stock(ac, close=False)
        if stock is None:
            print('[!] the Tab screen would not open')
            return 1

        before = rows_of(stock)
        named = [r for r in before if r not in ('-', '?')]
        print(f'\nasked the spawner for {len(FILL)} distinct parts')
        print(f'the reader sees {len(before)} row(s), {len(named)} named')
        if len(FILL) > len(before) == INV_ROWS:
            print(f'  -> {len(FILL)} went in, {INV_ROWS} come back. The read '
                  f'is a WINDOW, not the pack.')
        cv2.imwrite(os.path.join(OUT, f'{stamp}_before.png'), full_frame())
        print(f'\nINV_ROWS = {INV_ROWS} (what the reader can see)')
        print(f'read back {len(before)} row(s)\n')
        show('before', before)

        # Pick the row to drop: the FIRST one with a name, so the result is
        # readable. An unnamed row (ammo, a med kit) would leave "-" moving
        # around and prove nothing.
        target = None
        for item in stock.view.inventory:
            if item is not None and item.key:
                target = item
                break
        if target is None:
            print('\n[!] nothing named in the backpack to drop')
            return 1

        print(f'\ndropping exactly ONE: {target.key}\n')
        ac.discard(target)
        time.sleep(0.6)

        stock2 = read_stock(ac, close=False)
        if stock2 is None:
            print('[!] lost the Tab screen after the drop')
            return 1
        after = rows_of(stock2)
        cv2.imwrite(os.path.join(OUT, f'{stamp}_after.png'), full_frame())
        show('after', after)

        print('\n── what this says ──')
        if before == after:
            print('  The rows are IDENTICAL. The drop did not land — that is'
                  '\n  explanation A, and the fix is in the drag, not the'
                  '\n  reader.')
        else:
            gone = [r for r in before if before.count(r) > after.count(r)]
            fresh = [r for r in after if after.count(r) > before.count(r)]
            print(f'  rows before / after : {len(before)} / {len(after)}')
            print(f'  left the window     : {gone or "nothing"}')
            print(f'  scrolled into it    : {fresh or "nothing"}')
            if len(after) == len(before) and fresh:
                print('\n  Same COUNT, different CONTENT: the drop landed and a'
                      '\n  row that was never visible took its place. The pack'
                      '\n  is deeper than the reader can see — explanation B.'
                      '\n  `_view_sig` compares only these rows, so a tidy pass'
                      '\n  that drops all twelve reads "unchanged" while having'
                      '\n  worked perfectly.')
            elif len(after) < len(before):
                print('\n  The row count FELL, so the pack held no more than the'
                      '\n  window showed. The drop landed; the jam is elsewhere.')
    finally:
        try:
            ac.ensure_tab(False)
        except Exception:
            pass
        ac.close()

    print(f'\nframes -> {OUT}\\{stamp}_before.png / _after.png')
    return 0


if __name__ == '__main__':
    sys.exit(main())
