"""TabWatch's state machine, offline. No game, no screen, no hardware.

    pixi run tab-watch

Two claims, both about control flow, both testable without pixels:

  tab_open moves ONLY because the anchor was looked at
  the guns are read ONCE, when the anchor says the panel shut

⚠ THE SECOND ONE IS ENFORCED AS A PROHIBITION, NOT AS A PROPERTY. The fake
grabber below counts every look at the weapon panel, and several cases assert
that count is ZERO while the panel is up. That is deliberate: "it reads the
right thing at the close" is also satisfied by schemes that read continuously
and keep the last answer, and those have been built here more than once. A
gate that only checks the published result cannot tell them apart. This one
fails the moment anything grabs the panel early, whatever it does with it.

⚠ THE FAKE SCREEN STAMPS EVERY FRAME so this can say WHICH moment was read
rather than merely that something was: each grab carries a generation counter
in one pixel and the fake detectors decode it. Without the stamp, reading the
panel at the wrong time and reading it at the right time produce the same
assertion.

The cases that matter are the ones that have gone wrong: a keypress the game
swallowed, a screen that changes with no keypress at all, and a panel read
when its tiles are not painted.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import numpy as np

from config import HUD_REGIONS, SCREEN_H, SCREEN_W, TAB_DRIFT_S, TAB_SETTLE_S
from control.tab_watch import TabWatch

# Where a grab writes (painted, generation). Inside gun_name_1 so the weapon
# detector, which only ever sees cut crops, can read the same stamp the
# attachment detector reads off the whole frame.
MY, MX = HUD_REGIONS['gun_name_1'][0], HUD_REGIONS['gun_name_1'][1]


class FakeState:
    def __init__(self):
        self.tab_open = False
        self.weapon_gt = ('', '')
        self.attachments = {}
        self.synced = 0

    def sync_weapons(self):
        self.synced += 1

    def set_attachments(self, slot, att):
        self.attachments[slot] = att


class FakeScreen:
    """What the screen currently shows, and how often it was looked at.

    `painted` is whether the twenty attachment tiles are drawn. It is NOT the
    same question as `open`: the anchor text stops being legible before the
    tiles stop being drawn, which is the fact the whole design rests on, and
    the two are scripted separately here so a case can put them out of step.
    """

    def __init__(self):
        self.open = False
        self.painted = True
        self.anchor_reads = 0
        self.grabs = 0            # looks at the WEAPON PANEL — the expensive one
        self.name_reads = 0
        self.att_reads = 0
        self.att_told = 'never called'   # what the attachment read was given
        self.gen = 0
        self.guns = {0: ('aug', 'm416')}
        self.kit = {0: {1: {'muzzle': 'comp'}, 2: {'muzzle': ''}}}

    def show(self, gen, guns, kit=None):
        """Change what a grab taken from now on comes back with."""
        self.gen = gen
        self.guns[gen] = guns
        self.kit[gen] = kit if kit is not None else {1: {'muzzle': f'g{gen}'},
                                                    2: {'muzzle': ''}}

    def type_det(self):
        s = self

        class D:
            def classify(_self, crops):
                s.anchor_reads += 1
                return s.open
        return D()

    def weapon_det(self):
        s = self

        class D:
            def classify(_self, crops):
                s.name_reads += 1
                return s.guns[int(crops['gun_name_1'][0, 0, 1])]
        return D()

    def att_det(self):
        s = self

        class D:
            # Takes a frame and the weapon names, like the real one. Naming the
            # guns is what narrows each slot's template bank, so a caller that
            # forgets to pass them reads every slot against all 55 templates and
            # gets a confident wrong answer -- which is what this recorded
            # argument exists to catch.
            def classify(_self, frame, weapons=None):
                s.att_told = weapons
                s.att_reads += 1
                return s.kit[int(frame[MY, MX, 1])]

            def any_drawn(_self, frame):
                return bool(frame[MY, MX, 0])
        return D()

    def grabber(self, counts=True):
        """counts=False for the anchor grabber, which is a different question.

        `grabs` is "how often did it look at the WEAPON PANEL", and several
        cases assert it is zero. Letting the 41x18 anchor check add into the
        same number would make those assertions unwritable — which is how an
        earlier version of this file asserted "opening does not read the panel"
        while it was reading it.
        """
        s = self

        class G:
            """A correctly-shaped frame stamped with the screen state.

            Shape matters: TabWatch slices screen coordinates out of what it
            gets back. A dict here would raise inside the grab's except, which
            would look exactly like a real grab failure -- it did, the first
            time this was written.
            """
            _buf = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)

            def grab(_self):
                if counts:
                    s.grabs += 1
                _self._buf[MY, MX] = (1 if s.painted else 0, s.gen, 0)
                return _self._buf

            def close(_self):
                pass
        return G()


def build():
    screen = FakeScreen()
    state = FakeState()
    w = TabWatch(state, {'tab_type': screen.type_det(),
                         'tab_weapon': screen.weapon_det(),
                         'tab_attachment': screen.att_det()},
                 verbose=False)
    w._type_grab = screen.grabber(counts=False)
    w._panel_grab = screen.grabber()
    return w, state, screen


FAILS = []


def check(name, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {name:<52} {got!r}'
          + ('' if ok else f'  != {want!r}'))
    if not ok:
        FAILS.append(name)


def run(w, seconds, step=0.011, t0=None):
    """Tick as the dispatcher would, in `step` increments of fake time."""
    t = t0 if t0 is not None else time.perf_counter()
    end = t + seconds
    while t < end:
        w.tick(t)
        t += step
    return t


print('\n=== a keypress alone changes nothing ===')
w, state, screen = build()
t = time.perf_counter()
w.on_key(t)                      # user pressed Tab; screen has NOT changed yet
check('open right after the key', w.open, False)
t = run(w, 0.05, t0=t)           # ticks, but the fake screen is still closed
check('open while the screen stays closed', w.open, False)
check('and the key itself read nothing', screen.grabs, 0)

print('\n=== it opens when the SCREEN opens, and reads nothing ===')
screen.open = True               # now the game actually drew it
t = run(w, 0.05, t0=t)
check('open once the screen shows it', w.open, True)
check('state.tab_open followed', state.tab_open, True)
check('opening did NOT read the panel', screen.grabs, 0)

print('\n=== and NOTHING is read for as long as it stays up ===')
# ⚠ THE PROHIBITION. Two seconds of ticks, a whole rummage through the
# inventory, and the weapon panel must not be looked at once. Anything that
# reads here is describing a moment the player is still changing.
before_anchor = screen.anchor_reads
t = run(w, 2.0, t0=t)
check('two seconds open, panel grabs', screen.grabs, 0)
check('nothing classified', screen.att_reads + screen.name_reads, 0)
check('weapon_gt untouched', state.weapon_gt, ('', ''))
# ⚠ AND THE ANCHOR IS NOT POLLED EITHER, which is a COST claim rather than a
# correctness one and is pinned because it is invisible in behaviour. A GDI
# grab is ~5 ms whatever its size, so checking the anchor on every 10 ms tick
# for as long as somebody leaves their inventory open is 52% of a core. An
# open panel costs the drift check and nothing else.
budget = int(2.0 / TAB_DRIFT_S) + 1
check(f'anchor checks in 2 seconds open (<= {budget})',
      screen.anchor_reads - before_anchor <= budget, True)

print('\n=== the read happens when the ANCHOR says shut ===')
# The anchor stops being legible before the tiles stop being drawn, so `open`
# goes False while `painted` is still True. That gap is the design.
screen.open = False
w.on_key(t)
t = run(w, 0.05, t0=t)
check('closed', w.open, False)
check('the panel was grabbed exactly once', screen.grabs, 1)
check('classified exactly once', screen.att_reads, 1)
check('weapon_gt published', state.weapon_gt, ('aug', 'm416'))
check('attachments published', state.attachments[1], {'muzzle': 'comp'})
# The names were read off THIS frame and were once discarded, leaving the slots
# to be matched blind against every template. That is how a UZI read as wearing
# a sniper cheek pad, and it feeds the recoil scale.
check('the weapon names reach the attachment read',
      screen.att_told, {1: 'aug', 2: 'm416'})

print('\n=== a change made INSIDE the panel is the one published ===')
# The whole reason you opened the panel. Reading at the close is what makes
# this automatic: there is no earlier answer to prefer.
w2, state2, screen2 = build()
screen2.open = True
screen2.show(1, ('m416', 'sks'), kit={1: {'muzzle': ''}, 2: {'muzzle': ''}})
w2._set_open(True)
t = run(w2, 0.5, t0=time.perf_counter())
screen2.show(2, ('m416', 'sks'), kit={1: {'muzzle': 'comp_ar'},
                                     2: {'muzzle': ''}})   # you fitted it
t = run(w2, 0.5, t0=t)
screen2.open = False
w2.on_key(t)
t = run(w2, 0.05, t0=t)
check('published what the gun wears NOW',
      state2.attachments[1], {'muzzle': 'comp_ar'})
check('and read the panel once, not twice', screen2.grabs, 1)

print('\n=== a SWALLOWED keypress must not move the flag ===')
w3, state3, screen3 = build()
screen3.open = True
w3._set_open(True)
t = time.perf_counter()
w3.on_key(t)                     # key sent... but the game ignores it
t = run(w3, TAB_SETTLE_S + 0.05, t0=t)
check('still open after the key was eaten', w3.open, True)
check('state agrees with the screen', state3.tab_open, screen3.open)
check('nothing was published', state3.weapon_gt, ('', ''))
check('and nothing was read', screen3.grabs, 0)

print('\n=== the screen can change with NO keypress (alt-tab, dialog) ===')
w4, state4, screen4 = build()
screen4.open = True
t = time.perf_counter()
t = run(w4, TAB_DRIFT_S + 0.05, t0=t)
check('drift check noticed it opened', w4.open, True)
screen4.open = False
t = run(w4, TAB_DRIFT_S + 0.05, t0=t)
check('drift check noticed it closed', w4.open, False)
check('and it still read the panel once', screen4.grabs, 1)

print('\n=== a close nobody announced finds the panel already gone ===')
# ⚠ THE HONEST FAILURE, AND IT IS THE COST OF THE DESIGN. With no key to
# watch, the close is found by the drift check up to TAB_DRIFT_S later, by
# which time the tiles are not drawn. `any_drawn` catches it: the kit is NOT
# published, because an unpainted tile reads '' out of classify() and that is
# the same '' an empty slot gives. A missed reading, not a wrong one.
#
# The NAMES still publish: the plates outlive the tiles, and it is only the
# tiles this can be wrong about.
w5, state5, screen5 = build()
screen5.open = True
screen5.show(1, ('vss', 'p90'))
w5._set_open(True)
screen5.open = False
screen5.painted = False          # by the time drift notices, it is gone
t = run(w5, TAB_DRIFT_S + 0.05, t0=time.perf_counter())
check('it did look', screen5.grabs, 1)
check('attachments NOT published', 1 in state5.attachments, False)
check('attachment read never even attempted', screen5.att_reads, 0)
check('but the weapon names still are', state5.weapon_gt, ('vss', 'p90'))

print('\n=== idle costs nothing but the drift check ===')
w6, state6, screen6 = build()
t = run(w6, 1.0, t0=time.perf_counter())
expected = int(1.0 / TAB_DRIFT_S) + 1
check(f'anchor reads in 1 idle second (<= {expected})',
      screen6.anchor_reads <= expected, True)
check('no panel grabs while closed', screen6.grabs, 0)

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
    sys.exit(1)
print('all ok')
