"""A pickup must not disarm the tool. Offline: no game, no screen, no Pico.

    pixi run kit-persist

⚠ THIS IS THE GATE FOR THE BIGGEST SINGLE REASON THE TOOL STOPPED
COMPENSATING MID-FIGHT. `('clear_attachments',)` hung off the F key, so every
pickup wiped both guns' scope, muzzle, grip and stock. Nothing re-read them --
attachments are only visible on the Tab panel and F does not open it -- so ONE
pickup dropped the curve key to `bare` and the compensation stayed off until
the player opened Tab by hand.

F is the most-pressed key in a real match (ammo, meds, armour, attachments)
and almost none of those presses change your gun.

Measured, play log 2026-08-09 (calibration/artifacts/robot/0809_141201.log):
30 bursts, `[armed]` printed ONCE, and four m416 bursts went down recorded as
`bare`.

The clear now hangs off an OBSERVED weapon-name change in
GameState.sync_weapons. Clearing on a keypress is a guess about what the world
did; clearing on a name change is a measurement of it, and the name is already
read 500 ms after every F.

⚠ BOTH DIRECTIONS ARE CHECKED, and the second is the one that keeps this
honest. A gate that only proves "the kit survives" is passed by deleting the
clear altogether -- and then a real weapon swap fires the old gun's curve,
which is the 1521-counts-against-895 failure this repository already paid for.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from config import KEY_ACTION_TABLE                        # noqa: E402
from detector.game_state import GameState                  # noqa: E402

FAILS = []
KIT = {'scope': 'Upper_DotSight_01_C',
       'muzzle': 'Muzzle_Compensator_Large_C',
       'grip': 'Lower_Foregrip_C',
       'stock': 'Stock_AR_Composite_C'}


def check(what, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {what:<52} {got!r}'
          + ('' if ok else f'  != {want!r}'))
    if not ok:
        FAILS.append(what)


def armed(state):
    """counts the firmware would be handed for gun 1 right now."""
    return round(sum(state.weapon_1.dy_s), 1)


def fitted():
    s = GameState()
    s.weapon_gt = ('m416', '')
    s.sync_weapons()
    s.set_attachments(1, KIT)
    return s


print('=== the F key no longer carries a wipe ===')
f_entry = next(e for e in KEY_ACTION_TABLE
               if e['key'] == 'f' and e.get('event', 'press') == 'press')
check('F does not clear attachments',
      any(i == ('clear_attachments',) for i in f_entry.get('state', [])), False)
check('F still drops the weapon GT so the read can win',
      ('weapon_gt', ('', '')) in f_entry.get('state', []), True)

print('\n=== a pickup that does not change the gun keeps the curve ===')
s = fitted()
before = armed(s)
check('the full kit is armed', before > 0, True)
# What an F press does now: GT cleared, then the +500 ms read comes back with
# the SAME name (you picked up ammo, not a gun).
s.weapon_gt = ('', '')
s.sync_weapons()
check('still armed after the pickup', armed(s), before)
s.weapon_pred = ('m416', '')
s.sync_weapons()
check('still armed after the name read confirms m416', armed(s), before)

print('\n=== but a real weapon swap still clears it ===')
# ⚠ THE ORDER IS THE REAL ONE AND IT IS LOAD-BEARING. `weapon_name` prefers
# weapon_gt over weapon_pred, so a pred that says `scar` while a stale gt still
# says `m416` changes nothing. Picking a gun up is an F: the key drops the GT
# first, and the +500 ms weapon_hud read then wins. Writing the pred alone
# passed the "kit survives" half and failed all five of these -- which is the
# gate doing its job on the test rather than on the code.
s2 = fitted()
s2.weapon_gt = ('', '')          # the F press
s2.sync_weapons()
s2.weapon_pred = ('scar', '')    # the read, 500 ms later
s2.sync_weapons()
check('muzzle forgotten', s2.weapon_1.muzzle, '')
check('scope forgotten', s2.weapon_1.scope, '')
check('grip forgotten', s2.weapon_1.grip, '')
check('stock forgotten', s2.weapon_1.butt, '')
check('and it is not still firing the m416 curve', armed(s2), 0)

print('\n=== clearing gun 1 does not touch gun 2 ===')
s3 = GameState()
s3.weapon_gt = ('m416', 'vector')
s3.sync_weapons()
s3.set_attachments(2, KIT)
kept = s3.weapon_2.muzzle
s3.weapon_gt = ('', '')
s3.sync_weapons()
s3.weapon_pred = ('scar', 'vector')
s3.sync_weapons()
check('gun 1 swapped, gun 2 keeps its muzzle', s3.weapon_2.muzzle, kept)

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
    sys.exit(1)
print('all ok')
