"""The weapon axis: every gun, one fixed kit, no attachment work at all.

    pixi run python calibration/weapon_axis.py --weapons ar --mags 5 --apply
    pixi run python calibration/weapon_axis.py --weapons aug,m416 --dry

Separating the axes is what makes this cheap. calibration/harvest.py exists to
answer TWO questions at once -- what each gun's recoil is, and what each
attachment does to it -- and it pays for the second on every cell of the
first: strip the old gun, plan drags, fit each part, read it back, retry the
ones that silently did nothing.

The attachment question is answered. Measured on the m416 2x2x2 factorial
(calibration/ortho_0802*.jsonl, analysed by analyse_factors.py), the slots
MULTIPLY:

    grip+stock          predicted 0.7392  measured 0.7428   +0.2 sigma
    muzzle+grip                   0.5772           0.5908   +0.9
    muzzle+stock                  0.7292           0.7237   -0.2
    muzzle+grip+stock             0.5578           0.5909   +1.3

so N slots cost N numbers, not 2^N curves, and a weapon measured at one fixed
kit converts to any other kit by multiplying. Which means the weapon axis
never has to change a kit -- it only has to KNOW the one it measured.

And knowing it is free: PUBG auto-fits whatever the backpack holds onto a gun
the moment it arrives. So the loop is spawn, read back what the game put on,
fire, discard. Nothing is dragged, and the failure mode that dominates
harvest -- a drag that lands nothing and reports a configuration that never
existed -- cannot happen, because nothing is asserted about the kit. It is
recorded.

Two guns per batch because the rack holds two, and one Tab session per batch
because everything Tab is needed for -- which gun is in which slot, what each
is wearing -- comes out of a single detection pass.

POSTURE IS NOT ON THIS AXIS. It does not multiply with the attachments: on the
m416 the crouching factor is 0.729 bare and 0.829 kitted, 4.6 sigma apart, and
prone is 8.5. Posture is measured per weapon, on the converged standing curve,
by a separate run.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from detector.weapon import can_full_guns

from sweep import Rig, POSTURES
from control.focus import ensure_focus, focus_keeper
from control.lobby import LobbyControl
from control.spawner import SpawnerControl
from control.inventory import InventoryControl
from control.stock import restock
from harvest import BACKPACK, SIGHT_FOR, measure_cell, expand

HERE = os.path.dirname(os.path.abspath(__file__))
# Runs are measurements, not source: they land under docs/ with the rest of
# what this repo has measured, never next to the script that wrote them.
RUNS = os.path.join(os.path.dirname(HERE), 'docs', 'recoil', 'runs')

# The rack holds two. Every spawner visit fills both, so the panel is opened
# once per PAIR rather than once per weapon.
RACK = (1, 2)

# Magazines, and the one place this axis does touch a slot.
#
# A gun out of the spawner arrives wearing 加长快速弹匣 (quickext) -- the game
# fits it, nobody asks for it. It holds the same rounds as 扩容弹匣 (ext) and
# reloads faster, which is worth nothing here: reload time is dead time
# between magazines and capacity is what decides how many bullets a curve can
# cover. So the pair is swapped onto ext for the measurement and swapped BACK
# before being thrown away.
#
# Swapping back is what makes it free. The guns leave wearing what they
# arrived in, the two ext magazines drop into 库存, and the next pair swaps
# onto the same two. Two are spawned once, at the start of the run, and never
# again.
#
# RIGHT-CLICK, never a drag. Measured 4/4 at 0.35 s against 0/4 at 1.70 s for
# the equivalent drag (tools/probe_equip_gesture.py) -- on this slot the drag
# does not land at all, which is what "magazine should be ext_ar, reads ''"
# was in every earlier run. Right-click reaches only the HELD weapon, so each
# gun is taken in hand first; Tab swallows 1/2, so ac.hold() closes and
# reopens it, and that cost is per weapon rather than per attachment.
MEASURE_MAG = 'ext_ar'
STOCK_MAG = 'quickext_ar'


def fit_mags(ac, key, guns=RACK, weapon=None):
    """Put `key` in both guns' magazine slots. Tab must already be up. -> bool

    ensure_kit rather than a hand-rolled equip loop, for the property that
    matters here: it checks the slot against `key`'s OWN icon template rather
    than "occupied by anything". The magazine slot is never empty, so a swap
    that did nothing leaves the old magazine sitting there and an
    occupied-by-something check passes on it.

    It also holds the gun before fitting, which is the difference between the
    right-click that lands 4/4 and the drag that landed 0/4
    (docs/game_quirks.md), and it re-reads between the two guns -- the
    magazine displaced by each swap arrives in 库存 as a NEW row, so a plan
    made once up front is stale by the second gun.
    """
    ok = True
    for g in guns:
        rec = ac.ensure_kit(g, {'magazine': key}, weapon=weapon)
        if rec['ok']:
            continue
        ok = False
        if rec['missing']:
            print(f'    [!] {key} not on screen — gun {g} keeps what it has')
        for b in rec['bad']:
            print(f"    [!] gun {g} magazine: {b['why']}")
        if rec['error'] and not rec['bad'] and not rec['missing']:
            print(f"    [!] gun {g} magazine: {rec['error']}")
    return ok


def prep_pair(rig, ac):
    """Magazines on, rack read — ONE Tab session, which is the whole point.

    -> {slot: {'weapon': key, 'slots': {slot: name}}}, or None.

    Everything Tab is needed for before firing happens here: swap both guns
    onto the measurement magazine, then read which gun is in which rack slot
    and what each ended up wearing. It used to be three separate open/close
    cycles. hold() still costs one Tab cycle per gun -- the number keys are
    swallowed while the screen is up -- but that is two, not six, and it is
    per weapon rather than per attachment.
    """
    # A pair just spawned into the rack, so whatever hold() last recorded is
    # about a gun that is now on the floor. hold() is a no-op when it believes
    # the weapon is already in hand, and a stale True there right-clicks the
    # magazine onto the wrong gun.
    ac.held = None
    # Take the first gun in hand BEFORE the screen goes up. hold() has to
    # close Tab to press a number key, so doing it here costs one press and
    # doing it inside costs a close and an open as well.
    ac.hold(RACK[0])
    with ac.tab_up() as up:
        if not up:
            return None
        fit_mags(ac, MEASURE_MAG)
        lo = ac.loadout()
    rig.ensure_inventory_closed()
    if lo is None:
        return None
    return {g: {'weapon': lo['guns'].get(g), 'slots': lo['slots'].get(g, {})}
            for g in RACK}


def finish_pair(rig, ac):
    """Magazines back, guns on the floor — the other single Tab session.

    -> the drop records, or None if the screen never came up.

    Swapping back BEFORE dropping is what keeps the two extended magazines
    alive: a discarded weapon keeps everything it is wearing, so a pair thrown
    away still holding them costs two per batch.
    """
    with ac.tab_up() as up:
        if not up:
            return None
        fit_mags(ac, STOCK_MAG)
        drops = ac.clear_rack()
    rig.ensure_inventory_closed()
    return drops


def spawn_pair(sc, pair):
    """Both guns, one panel visit, ONE click each.

    The `panel` parameter went with the Panel class in 5f. The body never
    read it, so nothing here broke -- but the call site kept passing a name
    that no longer exists in that scope, and every batch raised NameError on
    its first spawn.

    weapon_times=1 -- the default, and stated anyway because it is load-
    bearing here: the rack has two slots and this fills both, so a second
    click per gun would have the second gun evict the first.
    """
    if not sc.ensure_panel(True):
        print('  [!] spawner panel would not open')
        return False
    try:
        sc.sync()
        res = sc.give_many(list(pair), switch=False, weapon_times=1)
        if not res['ok']:
            print(f"  [!] spawner: {res['error']}")
        else:
            print(f"  spawned {', '.join(pair)} in {res['clicks']} clicks")
    finally:
        sc.ensure_panel(False)
    return res['ok']


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapons', default='ar')
    ap.add_argument('--posture', default='standing', choices=POSTURES)
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--mags', type=int, default=5)
    ap.add_argument('--apply', action='store_true',
                    help='EMA-update each curve as its cell is measured')
    ap.add_argument('--semi', action='store_true')
    ap.add_argument('--countdown', type=int, default=8)
    ap.add_argument('--out', default='')
    ap.add_argument('--dry', action='store_true',
                    help='print the batches and the kit each gun will be '
                         'offered, then stop. Touches nothing.')
    args = ap.parse_args()

    weapons = [w for w in expand(args.weapons, semi=args.semi)
               if w in can_full_guns or args.semi]
    if not weapons:
        print('[!] no weapons selected')
        return 1
    batches = [tuple(weapons[i:i + len(RACK)])
               for i in range(0, len(weapons), len(RACK))]

    print(f'weapons : {len(weapons)} — {", ".join(weapons)}')
    print(f'batches : {len(batches)} of {len(RACK)}')
    print(f'posture : {args.posture}   (the posture axis is a separate run)')
    print(f'kit     : whatever the game auto-fits, read back per gun, plus '
          f'{MEASURE_MAG}\n          for capacity (swapped back to '
          f'{STOCK_MAG} before the pair is dropped).\n          Nothing is '
          f'dragged on: the attachment factors multiply, so any\n          '
          f'kit converts — it only has to be RECORDED, not chosen.')
    for b in batches:
        print(f'          {", ".join(b)}')
    if args.dry:
        return 0

    out = args.out or os.path.join(
        RUNS, f'weapon_axis_{datetime.now():%m%d_%H%M}.jsonl')
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    print(f'out     : {out}')
    print('\n' + ('[EMA] each cell updates its own curve in place.\n'
                  if args.apply else
                  '[SHADOW MODE] nothing is written back.\n'))

    if not ensure_focus(countdown_s=args.countdown, label='the weapon axis'):
        print('[!] could not focus the game')
        return 1
    time.sleep(0.6)
    with LobbyControl() as lc:
        if not lc.ensure_in_match()['ok']:
            print('[!] not in a match and could not get into one')
            return 1
    time.sleep(1.0)

    rig = Rig(args.sight)
    sc = SpawnerControl()
    ac = InventoryControl(verbose=False)
    log = open(out, 'a', encoding='utf-8')
    log.write(json.dumps({'type': 'header', 'sight': args.sight,
                          'posture': args.posture, 'mags': args.mags,
                          'axis': 'weapon',
                          'ts': datetime.now().isoformat()}) + '\n')
    log.flush()

    try:
        # Two extended magazines, once, for the whole run. Every batch swaps
        # them on for the measurement and back off before the guns are thrown
        # away, so they return to 库存 each time and are never respawned.
        if not restock(ac, sc, {MEASURE_MAG}, backpack=BACKPACK,
                       loose_only=True, per=len(RACK), drop_unwanted=False):
            print('[!] could not stock the two extended magazines')
            return 1

        for bi, pair in enumerate(batches, 1):
            print(f'\n[{bi}/{len(batches)}] {", ".join(pair)}')
            if not focus_keeper().ok(f'batch {bi}'):
                break
            # No parts, no backpack, no drags. The attachment axis is already
            # measured and it MULTIPLIES, so a bare curve converts to any kit
            # by a factor -- there is nothing a kitted measurement would add
            # that a bare one plus the factors does not already give.
            #
            # Removing them removes a noise source as well as work. A run that
            # did stock parts had the game auto-fit the second gun with
            # Magazine_ExtendedQuickDraw_Large_C and Lower_Foregrip_C -- not
            # the ext_ar and vert_grip that were spawned for it, but leftovers
            # the backpack happened to still hold. The cell was labelled with
            # what was asked for and measured something else.
            if not spawn_pair(sc, pair):
                continue
            # Onto the plain extended magazine, then read back what the game
            # actually put on both guns. One Tab session for both. The two
            # magazines were spawned once at the start of the run and come
            # back into 库存 every time a pair is swapped off them.
            rack = prep_pair(rig, ac)
            if rack is None:
                print('  [!] could not read the rack')
                continue
            for slot in RACK:
                seen = rack[slot]['weapon']
                worn = rack[slot]['slots']
                if not seen:
                    print(f'  slot {slot}: no gun read — skipping')
                    continue
                if seen not in pair:
                    print(f'  slot {slot}: reads {seen!r}, not one of '
                          f'{pair} — skipping rather than mislabelling')
                    continue
                fitted = {s: n for s, n in worn.items() if n}
                print(f'  slot {slot}: {seen} wearing '
                      + (', '.join(f'{s}={n}' for s, n in sorted(fitted.items()))
                         or 'nothing'))
                rig.set_sight(SIGHT_FOR.get(seen, args.sight))
                # ac.hold rather than a raw 1/2, so ac.held stays true. It is
                # what equip(gesture='auto') consults, and a stale value there
                # turns the swap-back into a refusal ("right-click only
                # reaches the held weapon") or, worse, a right-click that
                # fits the magazine to the wrong gun.
                if not ac.hold(slot):
                    print(f'  slot {slot}: could not select the weapon')
                    continue
                cell = measure_cell(rig, seen, args.posture, args.mags, slot,
                                    log, 'auto', worn, apply_ema=args.apply,
                                    loadout=(seen, worn))
                if cell is None:
                    print(f'  slot {slot}: nothing measured')

            # Magazines back, then both guns on the floor wearing everything
            # else. One Tab session again.
            drops = finish_pair(rig, ac)
            if drops is None:
                print('  [!] Tab would not open to clear the rack')
                break
            for d in drops:
                print(f"  dropped {d['was']!r}"
                      + ('' if d['ok'] else
                         f" — STILL IN THE RACK ({d['now']!r})"))
            if not all(d['ok'] for d in drops):
                print('  [!] the rack did not empty. Stopping: the next '
                      'batch would spawn\n      into a full rack, where '
                      'both guns land in slot 2 and the second\n      '
                      'evicts the first.')
                break
    finally:
        log.close()
        ac.close()
        rig.close()
    print(f'\n  raw -> {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
