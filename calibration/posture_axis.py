"""The posture axis: one gun, three postures, nothing touched in between.

    pixi run python calibration/posture_axis.py --weapons m416,akm --mags 3
    pixi run python calibration/posture_axis.py --weapons ar,smg,mg --dry

WHY THIS IS NOT A harvest.py FLAG. harvest exists to CHOOSE a configuration
and prove the gun is wearing it: strip what the game auto-fitted, drag the
wanted parts on, read each slot back, retry the ones that silently did
nothing. Every one of those steps is a drag, and a drag that reports success
while landing nothing is the failure that dominates that script.

The posture factor does not need any of it, because it is a RATIO BETWEEN TWO
CELLS OF THE SAME GUN. Whatever the gun is wearing appears in the numerator
and the denominator and cancels exactly -- provided nothing touches it between
the two, which is arranged here by touching nothing at all. Measured on the
0803 run: three "bare" weapons, and every one of them had comp_ar / vert_grip
/ tactical_stock auto-fitted and then stripped off again before firing. Those
drags bought a label the ratio never reads.

So: spawn the pair, read back what the game put on them, fire each posture,
throw both guns away wearing everything. Zero drags. weapon_axis.py reached
the same shape from the other axis and its docstring makes the same argument;
this is that loop with the magazine swap removed (the magazine cancels here
too) and the posture loop added.

WHERE THE KIT DOES STILL MATTER, and it is exactly the question this run is
for. Posture does NOT multiply with the attachments: on the m416 the prone
factor is 0.489 bare and 0.594 kitted, 8.5 sigma apart (docs/recoil/runs/
posture_0802.jsonl). So

  * WITHIN one weapon the ratio is clean whatever it wears -- same gun, same
    kit, three cells.
  * ACROSS weapons it is only meaningful if the kits match. "Do the guns in a
    class share one factor" is looking for differences of maybe 5-15%, and an
    uncontrolled kit moves the number by 21%.

Only the SIGHT is spawned, because it is not decoration: it sets K and which
screen columns the view tracker may use. Everything else is left to the game,
which is what makes every gun comparable for free -- a weapon out of the
spawner wears the same default whatever this script does, so "default" IS a
controlled kit as long as the backpack is empty of parts.

IT EMPTIES ITSELF, so no tidy pass is needed to get there. PUBG auto-fits
whatever the pack holds the moment a gun arrives, and a gun thrown away keeps
everything it is wearing -- so leftovers from an earlier run are consumed by
the first batch and leave on it. Only that batch is contaminated, the readback
shows it, and kit_groups() names it at the end. Dragging them to the floor
first would cost a pass of exactly the gesture this script exists without.

Nothing is asserted about what landed. `kit_groups` reports which guns ended
up wearing the same thing, so a cross-weapon comparison can be restricted to
guns that are actually comparable rather than assuming they are.
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from detector.attachment_catalog import ROSTER
from detector.weapon import can_full_guns

from sweep import Rig, POSTURES
from control.focus import ensure_focus, focus_keeper
from control.lobby import LobbyControl
from control.spawner import SpawnerControl
from control.inventory import InventoryControl
from control.stock import restock
from harvest import BACKPACK, SIGHT_FOR, measure_cell, expand

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(os.path.dirname(HERE), 'docs', 'recoil', 'runs')

# The rack holds two, so the spawner is visited once per PAIR.
RACK = (1, 2)


def stock_sight(ac, sc, sight):
    """Two sights on hand, spawned fresh each batch. -> bool

    FRESH EACH BATCH is not an oversight. The sight is fitted to the gun and
    leaves on it when the gun is thrown away, so the pack is short again by
    the next batch and `loose_only=True` sees the real shortfall. Keeping one
    pair alive across batches would mean unfitting them, which is the gesture
    this script exists without.

    The sight and nothing else. Everything else a gun wears is whatever the
    game fits by default, which is the same for every gun and therefore
    already a controlled kit -- see the module docstring.

    drop_unwanted=False: leftovers in the pack are not tidied, they are worn
    away by the first batch and reported by kit_groups().
    """
    return restock(ac, sc, {sight}, backpack=BACKPACK, loose_only=True,
                   per=len(RACK), drop_unwanted=False)


def spawn_pair(sc, pair):
    """Both guns, one panel visit, ONE click each.

    weapon_times=1 is load-bearing: the rack has two slots and this fills
    both, so a second click per gun would have the second evict the first.
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


def read_rack(rig, ac):
    """Which gun is in which slot and what each is wearing. ONE Tab session.

    -> {slot: {'weapon': key, 'slots': {slot: name}}}, or None.

    Read, never chosen. The pair just spawned, so whatever ac.held last
    recorded is about a gun that is now on the floor; a stale True there makes
    the next hold() a no-op and every later gesture address the wrong gun.
    """
    ac.held = None
    with ac.tab_up() as up:
        if not up:
            return None
        lo = ac.loadout()
    rig.ensure_inventory_closed()
    if lo is None:
        return None
    return {g: {'weapon': lo['guns'].get(g), 'slots': lo['slots'].get(g, {})}
            for g in RACK}


def drop_pair(rig, ac):
    """Both guns on the floor, wearing everything. -> the drop records or None.

    No strip first, which is the one line that separates this from
    harvest.Kitter.clear_rack. There the parts are a scarce shared resource
    shuttled between guns, so letting one leave wearing the only ext_ar
    stranded it on the floor. Here every batch spawns its own set, so the
    parts leaving on the gun is the disposal, not a leak -- and it is one
    right-click per gun instead of a drag per occupied slot.
    """
    with ac.tab_up() as up:
        if not up:
            return None
        drops = ac.clear_rack()
    rig.ensure_inventory_closed()
    return drops


def kit_groups(cells):
    """Which measured guns are actually comparable. -> {class: {kit: [weapon]}}

    A cross-weapon posture comparison is only valid between guns wearing the
    same thing (see the module docstring: the kit moves the prone factor by
    21%, and the within-class differences being looked for are smaller than
    that). Reported rather than enforced -- a gun that came out wearing
    something else is still a good measurement of ITSELF.
    """
    out = defaultdict(lambda: defaultdict(list))
    for weapon, worn in cells:
        cls = (ROSTER.get(weapon) or ('?', None))[0]
        key = ', '.join(f'{s}={n}' for s, n in sorted(worn.items()) if n) or 'bare'
        if weapon not in out[cls][key]:
            out[cls][key].append(weapon)
    return out


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
    ap.add_argument('--postures', default=','.join(POSTURES))
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--mags', type=int, default=3)
    ap.add_argument('--semi', action='store_true')
    ap.add_argument('--countdown', type=int, default=8)
    ap.add_argument('--home', action='store_true',
                    help='re-home against the pitch clamp before every '
                         'magazine instead of returning to the cell '
                         'reference. The standing cell of every weapon in the '
                         '0803 run died after one magazine with "the '
                         'reference match has wrapped" — standing has the '
                         'largest recoil, so the view travels furthest and '
                         'the correlator wraps at 128 px. Homing returns to a '
                         'hard stop rather than to a running total.')
    ap.add_argument('--out', default='')
    ap.add_argument('--dry', action='store_true',
                    help='print the batches and the kit each gun will be '
                         'offered, then stop. Touches nothing.')
    args = ap.parse_args()

    weapons = [w for w in expand(args.weapons, semi=args.semi)
               if w in can_full_guns or args.semi]
    postures = [p.strip() for p in args.postures.split(',') if p.strip()]
    bad = [p for p in postures if p not in POSTURES]
    if bad:
        print(f'[!] unknown posture(s): {bad}  (one of {POSTURES})')
        return 1
    if not weapons:
        print('[!] no weapons selected')
        return 1
    if len(postures) < 2:
        print('[!] a posture FACTOR is a ratio between two cells of the same '
              'gun. One posture measures a recoil, not a factor — use '
              'weapon_axis.py for that.')
        return 1
    batches = [tuple(weapons[i:i + len(RACK)])
               for i in range(0, len(weapons), len(RACK))]

    print(f'weapons  : {len(weapons)} — {", ".join(weapons)}')
    print(f'batches  : {len(batches)} of {len(RACK)}')
    print(f'postures : {", ".join(postures)}   '
          f'({len(weapons) * len(postures)} cells, {args.mags} magazines each)')
    print(f'kit      : the game\'s default plus a {args.sight}. Nothing else is\n'
          '           spawned and nothing is dragged — the kit cancels within '
          'a\n           weapon, and across weapons the groups are reported '
          'at the end\n           so only comparable guns get compared.')
    for b in batches:
        print(f'           {", ".join(b)}')
    if args.dry:
        return 0

    out = args.out or os.path.join(
        RUNS, f'posture_axis_{datetime.now():%m%d_%H%M}.jsonl')
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    print(f'out      : {out}')
    print('\n[SHADOW MODE] nothing is written back to any curve — a posture '
          'cell\n              measures a ratio, it does not converge a '
          'curve.\n')

    if not ensure_focus(countdown_s=args.countdown, label='the posture axis'):
        print('[!] could not focus the game')
        return 1
    with LobbyControl() as lc:
        if not lc.ensure_in_match()['ok']:
            print('[!] not in a match and could not get into one')
            return 1
    time.sleep(1.0)

    rig = Rig(args.sight)
    rig.use_homing = args.home
    sc = SpawnerControl()
    ac = InventoryControl(verbose=False)
    log = open(out, 'a', encoding='utf-8')
    log.write(json.dumps({'type': 'header', 'sight': args.sight,
                          'postures': postures, 'mags': args.mags,
                          'axis': 'posture', 'homing': bool(args.home),
                          'ts': datetime.now().isoformat()}) + '\n')
    log.flush()

    measured = []
    try:
        for bi, pair in enumerate(batches, 1):
            print(f'\n[{bi}/{len(batches)}] {", ".join(pair)}')
            if not focus_keeper().ok(f'batch {bi}'):
                break
            # The sight BEFORE the guns: PUBG auto-fits whatever the backpack
            # already holds at the moment a weapon arrives, so one spawned
            # after the pair sits in 库存 and both guns fire on iron sights —
            # at a K this run was not built for.
            if not stock_sight(ac, sc, args.sight):
                print('  [!] could not stock the sights — skipping this batch '
                      'rather than measuring at the wrong K')
                continue
            if not spawn_pair(sc, pair):
                continue
            rack = read_rack(rig, ac)
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
                    print(f'  slot {slot}: reads {seen!r}, not one of {pair} '
                          f'— skipping rather than mislabelling')
                    continue
                fitted = {s: n for s, n in worn.items() if n}
                print(f'  slot {slot}: {seen} wearing '
                      + (', '.join(f'{s}={n}' for s, n in sorted(fitted.items()))
                         or 'nothing'))
                rig.set_sight(SIGHT_FOR.get(seen, args.sight))
                # ac.hold rather than a raw 1/2 so ac.held stays true, and
                # before the posture loop rather than inside it: the gun in
                # hand does not change when the character crouches.
                if not ac.hold(slot):
                    print(f'  slot {slot}: could not select the weapon')
                    continue
                got = 0
                for posture in postures:
                    print(f'    posture {posture}')
                    cell = measure_cell(rig, seen, posture, args.mags, slot,
                                        log, 'asis', worn, apply_ema=False,
                                        loadout=(seen, worn))
                    if cell is None:
                        print(f'    [!] {posture}: nothing measured')
                    else:
                        got += 1
                if got:
                    measured.append((seen, fitted))
                if got < len(postures):
                    print(f'    [!] {seen}: {got}/{len(postures)} postures '
                          f'measured — a factor needs the pair, so the '
                          f'missing one costs the ratio, not just a cell')

            drops = drop_pair(rig, ac)
            if drops is None:
                print('  [!] Tab would not open to clear the rack')
                break
            # clear_rack returns a BATCH record, so the per-gun drops are in
            # ['steps']; iterating the record itself walks its KEYS and every
            # d['was'] is a string index into a string. weapon_axis.py carried
            # the same line and the same crash.
            drops = drops['steps']
            for d in drops:
                print(f"  dropped {d['was']!r}"
                      + ('' if d['ok'] else
                         f" — STILL IN THE RACK ({d['now']!r})"))
            if not all(d['ok'] for d in drops):
                print('  [!] the rack did not empty. Stopping: the next batch '
                      'would spawn\n      into a full rack, where both guns '
                      'land in slot 2 and the second\n      evicts the first.')
                break
    finally:
        log.close()
        ac.close()
        rig.close()

    if measured:
        print('\nWHICH GUNS ARE COMPARABLE — same class, same kit. A posture '
              'factor\ncompared across two different kits inherits a ~21% '
              'offset (see the\nmodule docstring), so a within-class '
              'difference is only readable\ninside one of these groups.')
        for cls, groups in sorted(kit_groups(measured).items()):
            print(f'  {cls}:')
            for kit, ws in sorted(groups.items()):
                print(f'    {", ".join(sorted(ws)):<28} {kit}')
            if len(groups) > 1:
                print(f'    [!] {len(groups)} different kits in {cls} — the '
                      f'cross-weapon comparison does not span them')
    print(f'\n  raw -> {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
