"""Does the cursor resting on a category row eat that submenu's FIRST entry?

    pixi run python tools/probe_submenu_hover.py

Opens a category, screenshots it twice -- once with the cursor still on the
row it just clicked, once with the cursor parked off the panel -- and reads
both. Nothing is spawned.

The failure this exists for: a weapon-axis run asked for col1_row01 (AR, 13
entries) and goto() reported "col1_row01 would not expand (panel reads
<panel open, col1_row02 expanded, 12 entries>)", three times over, on a panel
that plainly had the AR list open.

Twelve entries and one row of offset are the same fact. expansions() anchors
on entries[0] and maps it to whichever category sits SUBMENU_OFFSET above it,
so losing the TOP entry shifts the anchor down exactly one row pitch (43 px)
and the whole submenu is attributed to the row below. The same two frames from
docs/spawner/runs/ read 13 entries with the top at y=342, one pixel off the
predicted 341 -- so the detector is right and the live frame differed.

What differs is the cursor. Those frames came from tools/scrape_spawner.py,
which shoots through shoot_parked(); SpawnerControl.read() deliberately does
not, on the argument that the submenu test is positional and a hover changes
no position. It changes one: the hover-lit band on the category row sits
directly above the first entry, and find_submenu_items merges bands separated
by less than SUBMENU_ROW_GAP. A merged band is either too tall for
ROW_H_MAX or no longer centred -- the header hangs off the left -- and either
way the first entry is gone.

That is the hypothesis. This measures it, on three categories, twice each,
rather than fixing it on the strength of the reasoning.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))

import cv2

from detector.cropper import capture_screen
from detector.spawner_layout import (find_submenu_items, expansions,
                                     SUBMENU_OFFSET)
from control.focus import ensure_focus
from control.lobby import LobbyControl
from control.spawner import SpawnerControl, shoot_parked


OUT = os.path.join(ROOT, 'docs', 'spawner', 'hover')

# (column, row, what it is, how many entries it should have)
CASES = [(1, 1, 'AR', 13), (1, 3, 'DMR', 7), (2, 2, 'magazine', None)]
ROUNDS = 2


def report(tag, shot, sc, col, row, want):
    """One reading of one frame. -> (n_entries, mapped_row)"""
    box = sc.boxes[col]
    ents = find_submenu_items(shot, box)
    top = ents[0]['y0'] if ents else None
    predicted = sc.menu[col][row - 1].y + SUBMENU_OFFSET
    found = expansions(shot, sc.menu, sc.boxes)
    mapped = next(((c, r) for c, r, _ in found if c == col), None)
    flag = ''
    if want is not None and len(ents) != want:
        flag = f'   <-- {want - len(ents)} MISSING'
    if mapped is not None and mapped != (col, row):
        flag += '   <-- MISATTRIBUTED'
    print(f'    {tag:<8} entries {len(ents):2d}   top y0 {top}   '
          f'(predicted {predicted})   reads as {mapped}{flag}')
    return len(ents), mapped


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    if not ensure_focus(countdown_s=6, label='the submenu hover probe'):
        print('[!] could not focus the game')
        return 1
    time.sleep(0.7)
    with LobbyControl() as lc:
        if not lc.ensure_in_match()['ok']:
            print('[!] not in a match')
            return 1
    time.sleep(0.8)

    os.makedirs(OUT, exist_ok=True)
    sc = SpawnerControl()
    verdict = []
    try:
        if not sc.ensure_panel(True):
            print('[!] the spawner panel would not open')
            return 1
        if not sc.sync():
            print('[!] could not sync the panel')
            return 1
        for rnd in range(1, ROUNDS + 1):
            for col, row, name, want in CASES:
                sc.collapse_all()
                print(f'\n  [{rnd}] col{col}_row{row:02d}  {name}')
                # Click and let the slide-open finish, WITHOUT moving the
                # cursor: this is exactly the state read() sees.
                sc.click_category(col, row)
                hov = capture_screen()
                n_h, m_h = report('hovered', hov, sc, col, row, want)
                par = shoot_parked(settle=0.15)
                n_p, m_p = report('parked', par, sc, col, row, want)
                cv2.imwrite(os.path.join(
                    OUT, f'{rnd}_col{col}_row{row:02d}_hovered.png'), hov)
                cv2.imwrite(os.path.join(
                    OUT, f'{rnd}_col{col}_row{row:02d}_parked.png'), par)
                verdict.append((name, n_h, n_p, m_h, m_p, (col, row)))
        sc.collapse_all()
    finally:
        sc.ensure_panel(False)

    print('\n  ── verdict ──')
    eaten = [v for v in verdict if v[1] < v[2]]
    wrong_h = [v for v in verdict if v[3] != v[5]]
    wrong_p = [v for v in verdict if v[4] != v[5]]
    print(f'  hovered lost entries the parked shot kept : '
          f'{len(eaten)}/{len(verdict)}')
    print(f'  misattributed to the wrong category       : '
          f'hovered {len(wrong_h)}/{len(verdict)}, '
          f'parked {len(wrong_p)}/{len(verdict)}')
    if eaten and not wrong_p:
        print('\n  -> the hover is the cause. read() has to park.')
    elif not eaten and not wrong_h:
        print('\n  -> not reproduced. The hover is NOT it; look at what else '
              'differed\n     (terrain behind the translucent panel, items on '
              'the ground).')
    else:
        print('\n  -> mixed. Read the saved frames before changing anything.')
    print(f'\n  frames -> {os.path.relpath(OUT, ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
