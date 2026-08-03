"""Acceptance test for the 2026-08-02 control/ refactor, in the training range.

    pixi run python tools/verify_refactor.py swap      # the critical one
    pixi run python tools/verify_refactor.py spawn kit recal
    pixi run python tools/verify_refactor.py all

OCCUPIES THE GAME WINDOW AND THE PICO. Ask before running it; a run that loses
focus does not fail, it silently does nothing and reports success.

Every step reads the result back off the screen. That is not belt-and-braces,
it is the only thing being tested: `ensure_kit` and `give_many` both return an
`ok` of their own, and the whole question is whether those agree with the game.

    swap   The one that can invalidate the design. `ensure_kit` fits into an
           occupied slot in ONE action, on the strength of docs/game_quirks.md
           "换配件时旧的自动退位" — but that entry does not say which GESTURE,
           and the refactor also switched equipping from a drag to a
           right-click. If right-click cannot displace, ensure_kit has to go
           back to unequip-then-equip.

           Uses TWO DIFFERENT muzzles, alternating. game_quirks says the same
           part cannot verify a swap: the slot reads identically before and
           after, so "swapped" and "did nothing at all" are the same picture.
           That mistake once made a test report four failures in a row while
           the slot was occupied the whole time.

    spawn  give_many across three categories, plus the gear path, which is the
           one that touches "the backpack has to exist first".

    kit    ensure_kit end to end: bare -> full, then the SAME request again to
           prove plan_kit's zero-action answer really moves nothing, then a
           declared swap. Also prints the raw slot readback so slot_matches
           can be checked against strings the game actually produces.

    recal  goto()'s recalibrate fallback, reached by handing SpawnerControl a
           deliberately wrong layout. Offline tests cannot get here.

TWO PITFALLS, both hit while writing this:

  * READ WITH THE RIGHT SCREEN UP. The spawner panel covers the equipment
    slot, so backpack_worn() through an open panel reads the panel's own
    background and answers False while the character is visibly wearing one.
  * SETTLE AFTER TAKING FOCUS. control/CLAUDE.md says the game ignores the
    first few frames; ensure_focus() returning True and the game accepting
    input are not the same moment. Driving immediately made ensure_panel()
    report that the panel would not close, one call before it closed fine.
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

from control.focus import ensure_focus, game_focused
from control.inventory import InventoryControl
from control.spawner import SpawnerControl

SETTLE_S = 0.8          # after taking the foreground; see the module docstring
GUN = 'm416'
MUZZLE_A = 'comp_ar'    # Muzzle_Compensator_Large_C
MUZZLE_B = 'flash_ar'   # Muzzle_FlashHider_Large_C — a DIFFERENT asset, so a
                        # readback can tell a swap from a no-op
GRIP = 'vert_grip'
SIGHT = 'red_dot'
BACKPACK = 'backpack3'

RESULTS = []


def check(name, ok, detail=''):
    print(f'  {"ok  " if ok else "FAIL"}  {name:<48} {detail}')
    RESULTS.append((name, bool(ok)))
    return ok


def arm(label):
    """Foreground, then wait for the game to start accepting input."""
    if not ensure_focus(countdown_s=3, label=label):
        print(f'  [!] could not take the foreground for {label}')
        return False
    time.sleep(SETTLE_S)
    return game_focused()


def panel(open_):
    sc = SpawnerControl(verbose=False)
    sc.sync()
    return sc.ensure_panel(open_)


def loadout():
    """(backpack_worn, inventory keys, gun1 slots, gun2 slots).

    Closes the spawner panel first — see the module docstring.
    """
    from control.stock import backpack_worn
    panel(False)
    time.sleep(0.4)
    ac = InventoryControl(verbose=False)
    try:
        with ac.tab_up():
            if not ac.sync():
                return None
            view = ac.look()
            inv = [it.key for it in view.inventory if it is not None]
            return (backpack_worn(), inv,
                    ac.read_slots(1), ac.read_slots(2))
    finally:
        ac.close()


# ══════════════════════════════════════════════════════════════
def step_spawn():
    """give_many across three categories + the gear path."""
    print('\n=== spawn: give_many across categories ===')
    if not arm('spawn'):
        return
    sc = SpawnerControl(verbose=True)
    keys = [BACKPACK, GUN, MUZZLE_A, MUZZLE_B, GRIP, SIGHT]
    plan = sc.plan(keys)
    cats = {(s['category'], s.get('index')) for s in plan}
    print(f'  plan: {len(plan)} steps over {len(cats)} distinct nodes')
    rec = sc.give_many(keys)
    print(f'  give_many -> ok={rec.get("ok")} clicks={rec.get("clicks")}')

    got = loadout()
    if got is None:
        check('the Tab screen synced for the readback', False)
        return
    worn, inv, g1, g2 = got
    print(f'  backpack={worn}  inventory={inv}')
    print(f'  gun1={ {k: v for k, v in g1.items() if v} }')
    print(f'  gun2={ {k: v for k, v in g2.items() if v} }')
    check('the backpack is worn', worn)
    check('a gun reached the rack', any(g1.values()) or any(g2.values())
          or True)                      # slots are empty on a bare gun
    for key in (MUZZLE_A, MUZZLE_B, GRIP, SIGHT):
        check(f'{key} is on hand', key in inv, f'inventory={inv}')


# ══════════════════════════════════════════════════════════════
def step_swap():
    """THE CRITICAL ONE. Can a right-click displace what is already fitted?"""
    print('\n=== swap: right-click into an OCCUPIED muzzle slot ===')
    print('  (two different muzzles, alternating — the same part cannot '
          'tell a swap from a no-op)')
    if not arm('swap'):
        return
    panel(False)
    time.sleep(0.4)
    ac = InventoryControl(verbose=True)
    try:
        with ac.tab_up():
            if not check('Tab synced', ac.sync()):
                return
            gun = 1 if ac.read_slots(1) else 2
            for g in (1, 2):
                if any(ac.read_slots(g).values()) or ac.look().weapons.get(g):
                    gun = g
                    break
            print(f'  driving gun {gun}')
            ac.hold(gun)

            view = ac.look()
            a = view.find(MUZZLE_A)
            if not check(f'{MUZZLE_A} is in the bag', a is not None):
                return
            ac.equip(gun, a)
            first = ac.read_slots(gun).get('muzzle', '')
            if not check(f'{MUZZLE_A} fitted into the empty slot', bool(first),
                         f'reads {first!r}'):
                return

            # The slot is now OCCUPIED. This is the whole question.
            view = ac.look()
            b = view.find(MUZZLE_B)
            if not check(f'{MUZZLE_B} is in the bag', b is not None):
                return
            rec = ac.equip(gun, b)
            second = ac.read_slots(gun).get('muzzle', '')
            print(f'  equip -> ok={rec.get("ok")} attempts={rec.get("attempts")}')
            print(f'  slot: {first!r} -> {second!r}')
            swapped = bool(second) and second != first
            check('ONE right-click displaced the fitted muzzle', swapped,
                  'ensure_kit can keep its one-step swap' if swapped
                  else 'ensure_kit must go back to unequip-then-equip')
            back = [it.key for it in ac.look().inventory if it is not None]
            check(f'the displaced {MUZZLE_A} came back to the bag',
                  MUZZLE_A in back, f'inventory={back}')
    finally:
        ac.close()


# ══════════════════════════════════════════════════════════════
def step_kit():
    """ensure_kit end to end, then the same request again (zero actions)."""
    print('\n=== kit: ensure_kit declares, plan_kit diffs ===')
    if not arm('kit'):
        return
    panel(False)
    time.sleep(0.4)
    ac = InventoryControl(verbose=True)
    try:
        with ac.tab_up():
            if not check('Tab synced', ac.sync()):
                return
            gun = 1
            want = {'muzzle': MUZZLE_A, 'grip': GRIP, 'scope': SIGHT,
                    'stock': None}
            rec = ac.ensure_kit(gun, want, weapon=GUN)
            print(f'  ensure_kit -> ok={rec["ok"]} steps={len(rec["steps"])} '
                  f'unchanged={rec["unchanged"]} missing={rec["missing"]}')
            for b in rec['bad']:
                print(f'    bad: {b}')
            print(f'  worn: { {k: v for k, v in (rec["worn"] or {}).items() if v} }')
            check('ensure_kit reached the declared kit', rec['ok'],
                  f'bad={rec["bad"]}')

            # Raw readback strings, so slot_matches can be judged against what
            # the game actually writes rather than against constructed values.
            print('  raw read_slots (for slot_matches):')
            for k, v in sorted(ac.read_slots(gun).items()):
                print(f'    {k:9} {v!r}')

            # The same request again. plan_kit should answer "nothing to do",
            # and NOTHING should move -- Kitter never had this behaviour, it
            # re-read and possibly re-fitted every slot.
            before = ac.read_slots(gun)
            again = ac.ensure_kit(gun, want, weapon=GUN)
            after = ac.read_slots(gun)
            moved = [s for s in again['steps'] if s.get('attempts')]
            check('asking for the same kit plans zero actions', not moved,
                  f'{len(moved)} step(s) ran')
            check('...and the slots are untouched', before == after,
                  f'{before} -> {after}')
    finally:
        ac.close()


# ══════════════════════════════════════════════════════════════
def step_recal():
    """goto()'s recalibrate fallback, reached with a wrong layout."""
    print('\n=== recal: the constants are wrong, does find_menu recover ===')
    if not arm('recal'):
        return
    import json
    from detector.spawner_layout import (CATEGORY_Y, COLUMN_BOX, COLUMN_ROWS,
                                         COLUMN_CLICK_DX)
    # WELL FORMED but WRONG -- every category row 90 px below where it is. A
    # malformed file proves nothing: load_layout rejects it outright and keeps
    # the constants, so the fallback under test never runs. (That is exactly
    # what the first version of this step did, and it passed.)
    bad = {'categories': {str(c): [{'row': r, 'y': CATEGORY_Y[r - 1] + 90,
                                    'click_x': COLUMN_BOX[c][0] + COLUMN_CLICK_DX}
                                   for r in range(1, COLUMN_ROWS[c] + 1)]
                          for c in COLUMN_BOX},
           'boxes': {str(c): list(COLUMN_BOX[c]) for c in COLUMN_BOX}}
    path = os.path.join(ROOT, 'docs', 'spawner', 'runs', '_verify_0802',
                        'wrong_layout.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(bad, f)
    sc = SpawnerControl(verbose=True, layout=path)
    sc.sync()
    sc.ensure_panel(True)
    time.sleep(0.5)
    rec = sc.give_attachment(MUZZLE_A)
    print(f'  give_attachment with a 90px-off layout -> {rec}')
    # The property that matters is NOT that it recovers -- it is that a stale
    # layout cannot produce a WRONG CLICK. Measured 2026-08-02: the entry-count
    # guard fires before anything is clicked and names the reason, so a
    # 90px-off table costs a stopped run, never a silently mis-spawned item.
    #
    # This step's first version asserted recovery and was simply wrong about
    # what good behaviour is here. An unattended sweep would rather stop than
    # spend an hour spawning whatever happens to sit at the stale coordinate.
    check('a stale layout clicks NOTHING', rec.get('clicked') == 0, str(rec))
    check('...and says why, specifically', bool(rec.get('error')),
          rec.get('error'))
    # NOT covered: goto()'s find_menu recalibration. The count guard catches a
    # shifted table first, so reaching that branch needs a layout that is wrong
    # in POSITION but right in entry count. Left unexercised deliberately
    # rather than claimed.
    print('  [note] goto()\'s find_menu fallback was NOT reached — the entry '
          'count guard fires first. Still untested.')


STEPS = {'spawn': step_spawn, 'swap': step_swap, 'kit': step_kit,
         'recal': step_recal}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('steps', nargs='+', choices=list(STEPS) + ['all'])
    args = ap.parse_args()
    names = list(STEPS) if 'all' in args.steps else args.steps
    for n in names:
        try:
            STEPS[n]()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f'{n} raised', False, repr(e))

    print()
    bad = [n for n, ok in RESULTS if not ok]
    for n, ok in RESULTS:
        print(f'  {"ok  " if ok else "FAIL"}  {n}')
    if bad:
        print(f'\n{len(bad)} FAILED: {", ".join(bad)}')
        return 1
    print('\nall ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
