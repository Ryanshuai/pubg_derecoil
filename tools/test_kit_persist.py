"""Nothing may leave the tool silently disarmed. Offline: no game, no Pico.

    pixi run kit-persist

Two instances of one shape, a year apart in the table and identical in kind:
a KEYPRESS turns compensation off and nothing observable turns it back on.

    the F key    wiped both guns' kit, so the curve key fell to `bare`
    the win key  set stop_recoil, and nothing reliably cleared it

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

print('\n=== the right button re-arms, on the PRESS ===')
# ⚠ SAME FAMILY AS THE F KEY ABOVE: a key silently leaves the tool disarmed
# and nothing brings it back. `win` sets stop_recoil and clears nothing. Play
# log 2026-08-09 17:09:58 -- `win` press, then THIRTY-SIX SECONDS disarmed,
# until a Tab happened to be opened and closed.
#
# Every other re-arm in the table hangs off a key the player may simply not
# press (1, 2, a shift release). The right button is the one they cannot
# avoid: it is the key that means "I am about to shoot".
rights = [e for e in KEY_ACTION_TABLE if e['key'] == 'right']
check('exactly one right-button entry', len(rights), 1)
check('and it is on the PRESS', rights[0].get('event'), 'press')
check('it clears stop_recoil', ('stop_recoil', False) in rights[0]['state'],
      True)
check('and re-arms the firmware', rights[0].get('hw'),
      ['recoil_on', 'upload_pattern'])
# ⚠ THE PRESS EDGE IS THE POINT, NOT A DETAIL. Under release-only the whole
# HOLD ran disarmed, and a held right button is shoulder aim -- one of the
# three aiming states, not an edge case. With toggle ADS the two edges are
# ~50 ms apart; with a held one it is the entire engagement.
check('nothing re-arms on the right RELEASE (that was the bug)',
      any(e['key'] == 'right' and e.get('event') == 'release'
          for e in KEY_ACTION_TABLE), False)

print('\n=== win disarms, and the next right-click undoes it ===')
# End to end through the real dispatcher, because the assertions above are
# about a table and the failure was about a SEQUENCE. Dispatcher.__new__ skips
# the poller, the threads and the hardware.
from collections import deque, namedtuple                      # noqa: E402
from control.match import Dispatcher                           # noqa: E402

KeyEvent = namedtuple('KeyEvent', ['key', 'event', 'ts', 'held_keys'])


class _Tab:
    def on_key(self, ts):
        pass


d = Dispatcher.__new__(Dispatcher)
d.state = GameState()
d._detectors = {}
d._pending = deque()
d._hw = []
d._apply_hw = d._hw.append
d.tab = _Tab()
d._handle_key(KeyEvent('win', 'press', 0.0, frozenset()))
check('win disarmed it', d.state.stop_recoil, True)
d._handle_key(KeyEvent('right', 'press', 0.1, frozenset()))
check('the right-click press re-armed it', d.state.stop_recoil, False)
check('and pushed the curve with it', d._hw[-1], ['recoil_on',
                                                  'upload_pattern'])

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
