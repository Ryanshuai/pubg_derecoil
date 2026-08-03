"""When a spawned attachment lands in 库存, and when the game bolts it straight
onto the gun.

    pixi run python tools/probe_autofit.py --reps 3

Needs the game. Fires nothing.

THE QUESTION. calibration/collect_templates.py spawns a host weapon and then a
handful of parts, and counts on the parts arriving in 库存 so it can photograph
them there. Its first real run asked for five and found four:

    [!] 库存 shows 4 rows for 5 parts — something did not spawn.

One went somewhere else, and the obvious candidate is the gun: PUBG fits a
picked-up attachment on the spot when the slot is empty. If that is the rule,
the collector's premise is wrong in a way no ordering fixes -- its own
docstring already records the mirror failure, that spawning the weapon LAST
makes it "pick up the parts already in the backpack", so both orders lose parts
to the same mechanism from opposite ends.

WHAT IS MEASURED, and why it needs no template. Two numbers either side of one
spawn:

    inv_rows(frame)     how many rows 库存 is showing
    detail(slot crop)   Laplacian variance, i.e. "is something drawn here"

Neither reads an icon, so this says nothing about WHICH part arrived -- only
where it went, which is the whole question. +1 row means 库存; a slot going
from empty to drawn means the gun.

THREE CONDITIONS, one variable between them:

    A  no gun in the rack
    B  gun racked, the part's slot EMPTY
    C  gun racked, the part's slot ALREADY OCCUPIED

If B sends the part to the gun and C sends it to 库存, the rule is "auto-fit
into an empty slot" and the collector has to fill the slots it is not
photographing before it spawns anything.
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

from collect_templates import detail, inv_rows          # noqa: E402
from control.focus import ensure_focus                  # noqa: E402
from control.inventory import InventoryControl, at_inv  # noqa: E402
from control.spawner import SpawnerControl              # noqa: E402
from detector.attachment_detector import SLOT_DETAIL_MIN  # noqa: E402
from config import HUD_REGIONS                          # noqa: E402
from range_session import get_session                   # noqa: E402

GUN = 'sks'
PART = 'comp_ar'          # muzzle: sks has the slot, and spares are cheap
SLOT = 'muzzle'
OTHER = 'supp_ar'         # a second muzzle, to occupy the slot for condition C
GUN_SLOT = 1


def snap(ac, gun_slot):
    """(rows, slot detail) — neither reads a template."""
    f = ac.frame()
    y, x, h, w = HUD_REGIONS[f'att_{gun_slot}_{SLOT}']
    return inv_rows(f), detail(f[y:y + h, x:x + w])


def where(before, after):
    rows0, det0 = before
    rows1, det1 = after
    to_gun = det0 < SLOT_DETAIL_MIN <= det1
    if rows1 == rows0 + 1 and not to_gun:
        return 'INVENTORY'
    if to_gun and rows1 == rows0:
        return 'THE GUN'
    if to_gun and rows1 == rows0 + 1:
        return 'both?? (a row AND the slot filled)'
    if rows1 == rows0:
        return 'NOWHERE — the floor, or it never spawned'
    return f'unclear (rows {rows0}->{rows1}, detail {det0:.0f}->{det1:.0f})'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--countdown', type=int, default=4)
    args = ap.parse_args()

    if not ensure_focus(countdown_s=args.countdown, label='the autofit probe'):
        return 1
    if not get_session('auto').ensure()[0]:
        print('[!] could not get into the training range')
        return 1

    sc = SpawnerControl(verbose=False)
    ac = InventoryControl(verbose=False)

    def give(key):
        if not sc.ensure_panel(True) or not sc.sync(need_cols=(1, 2, 3)):
            return False
        ok = sc.give_attachment(key)['ok'] if key != GUN \
            else sc.give_weapon(key)['ok']
        sc.ensure_panel(False)
        time.sleep(0.7)
        return ok

    def gear(key):
        if not sc.ensure_panel(True) or not sc.sync(need_cols=(1, 2, 3)):
            return False
        ok = sc.give_gear(key).get('ok')
        sc.ensure_panel(False)
        time.sleep(0.7)
        return ok

    print('=== a backpack first, or everything lands on the floor ===')
    if not ac.ensure_tab(True) or not ac.sync():
        print('[!] Tab would not open')
        return 1
    gear('backpack3')

    out = {}
    for cond in ('A', 'B', 'C'):
        print(f'\n=== condition {cond} ===')
        hits = []
        for i in range(args.reps):
            # Set the condition up FRESH each repetition: the previous spawn
            # changed exactly the thing being varied.
            ac.ensure_tab(True)
            ac.clear_rack()
            if cond in ('B', 'C'):
                if not give(GUN):
                    print(f'  {i}: the spawner would not produce {GUN}')
                    continue
                ac.ensure_tab(True)
                ac.held = None
                ac.hold(GUN_SLOT)
            if cond == 'C':
                # Fill the slot, so the incoming part has nowhere to auto-fit.
                if not give(OTHER):
                    print(f'  {i}: no {OTHER}')
                    continue
                ac.ensure_tab(True)
                view = ac.look()
                item = view.find(OTHER)
                if item is not None:
                    ac.auto_equip(item.where)
                    time.sleep(0.6)
                if snap(ac, GUN_SLOT)[1] < SLOT_DETAIL_MIN:
                    print(f'  {i}: could not occupy the slot — skipping')
                    continue

            before = snap(ac, GUN_SLOT)
            ok = give(PART)
            ac.ensure_tab(True)
            time.sleep(0.4)
            after = snap(ac, GUN_SLOT)
            verdict = where(before, after) if ok else 'the spawner refused'
            hits.append(verdict)
            print(f'  {i}: rows {before[0]}->{after[0]}  '
                  f'slot detail {before[1]:6.0f}->{after[1]:6.0f}   {verdict}')
        out[cond] = hits

    print('\n=== summary ===')
    names = {'A': 'no gun in the rack',
             'B': 'gun racked, slot EMPTY',
             'C': 'gun racked, slot OCCUPIED'}
    for cond, hits in out.items():
        tally = {v: hits.count(v) for v in set(hits)}
        print(f'  {cond}  {names[cond]:<28} '
              + ', '.join(f'{v} x{n}' for v, n in sorted(tally.items())))
    print('\n  If B says THE GUN and C says INVENTORY, the rule is "auto-fit'
          '\n  into an empty slot" — and collect_templates has to fill every'
          '\n  slot it is not photographing before it spawns anything.')
    try:
        ac.ensure_tab(False)
    finally:
        ac.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
