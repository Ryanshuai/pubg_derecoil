"""TabWatch's state machine, offline. No game, no screen, no hardware.

    pixi run tab-watch

Two claims, both about control flow, both testable without pixels:

  tab_open moves ONLY because the anchor was looked at
  the guns are read ONCE, on the keypress that closes the panel

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
swallowed, a screen that changes with no keypress at all, a panel read when
its tiles are not painted, and -- the reason the trigger moved to the keypress
-- a panel read when the anchor said shut, which saved six frames of grass.
"""
import glob
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
from control.tab_watch import SHOT_DIR as REAL_SHOT_DIR, TabWatch

# ⚠ NOT control.tab_watch.SHOT_DIR. This suite drives TabWatch with a SYNTHETIC
# screen -- a zeroed buffer with one stamped pixel -- so every frame it saves is
# black and describes nothing. Seventeen of them landed in the real directory
# within minutes of the feature existing, and from the outside they are
# indistinguishable from a capture of the game: same name, same shape, same
# folder. The operator opened it, found it full of black frames, and reasonably
# concluded the capture was broken.
#
# A directory whose whole purpose is to answer "what did it actually see"
# cannot hold anything that was never seen. The frames still get written --
# here -- because a save path nobody exercises is one that breaks quietly.
SHOT_DIR = os.path.join(ROOT, 'calibration', 'artifacts', 'robot',
                        'tab_selftest')

# Where a grab writes (painted, generation). Inside gun_name_1 so the weapon
# detector, which only ever sees cut crops, can read the same stamp the
# attachment detector reads off the whole frame.
MY, MX = HUD_REGIONS['gun_name_1'][0], HUD_REGIONS['gun_name_1'][1]
ay, ax, ah, aw = HUD_REGIONS['type']


class FakeState:
    def __init__(self):
        self.tab_open = False
        self.weapon_gt = ('', '')
        self.weapon_pred = ('', '')
        self.attachments = {}
        self.synced = 0

    @property
    def weapon_name(self):
        """Effective names: GT then pred, as the real GameState resolves them.

        Modelled rather than stubbed because a kit is only published for a gun
        this answers for, and the fallback to `pred` is the half that matters:
        a gun already named by the HUD detector keeps its kit even when the Tab
        plate is unreadable. A stub returning weapon_gt alone would pass every
        case here and drop the kit in the one that counts.
        """
        return tuple(g or p for g, p in zip(self.weapon_gt, self.weapon_pred))

    def sync_weapons(self):
        self.synced += 1

    def set_attachments(self, slot, att):
        self.attachments[slot] = att


class FakeScreen:
    """What the screen currently shows, and how often it was looked at.

    `painted` is whether the twenty attachment tiles are drawn. It is NOT the
    same question as `open` -- the tiles land some milliseconds after the panel
    becomes legible -- so the two are scripted separately and a case can put
    them out of step.
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
                # ⚠ The anchor grabber returns a DICT, like the real
                # RegionGrabber, because the saved frame now carries the
                # anchor strip beside the panel and _compose has to cut it out
                # of whatever comes back. A bare array here would have made
                # _compose's dict branch untested and the real one the only
                # one that runs.
                return _self._buf if counts else {'type': _self._buf[
                    ay:ay + ah, ax:ax + aw]}

            def close(_self):
                pass
        return G()


def build():
    screen = FakeScreen()
    state = FakeState()
    w = TabWatch(state, {'tab_type': screen.type_det(),
                         'tab_weapon': screen.weapon_det(),
                         'tab_attachment': screen.att_det()},
                 verbose=False, shot_dir=SHOT_DIR)
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
# ⚠ IT DOES SNAP — both presses do, unconditionally, and the file says which.
# What it does not do is CLASSIFY: there is no panel to read yet, and the
# picture is kept because "an opening frame with a panel still in it" is how a
# close that never registered would show itself.
check('the key snapped one frame', screen.grabs, 1)
check('and classified nothing', screen.att_reads + screen.name_reads, 0)

print('\n=== it opens when the SCREEN opens, and reads nothing ===')
screen.open = True               # now the game actually drew it
t = run(w, 0.05, t0=t)
check('open once the screen shows it', w.open, True)
check('state.tab_open followed', state.tab_open, True)
check('opening classified nothing', screen.att_reads + screen.name_reads, 0)

print('\n=== and NOTHING is read for as long as it stays up ===')
# ⚠ THE PROHIBITION. Two seconds of ticks, a whole rummage through the
# inventory, and the weapon panel must not be looked at once. Anything that
# reads here is describing a moment the player is still changing.
before_anchor, before_grabs = screen.anchor_reads, screen.grabs
t = run(w, 2.0, t0=t)
check('two seconds open, panel grabs', screen.grabs - before_grabs, 0)
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

print('\n=== the read happens on the KEYPRESS, while the panel is up ===')
# ⚠ NOT when the anchor says shut. Six frames saved at that moment on
# 2026-08-09 were all pure game world: by then the panel is GONE, not fading.
# The press leads the close by 77-128 ms, and that margin is the design.
at_key = screen.grabs
w.on_key(t)                      # the closing press: panel still on screen
check('the panel was grabbed on the KEY', screen.grabs - at_key, 1)
after_key = screen.grabs
screen.open = False              # ...and the game takes it down afterwards
t = run(w, 0.05, t0=t)
check('closed', w.open, False)
check('the close itself grabbed nothing more', screen.grabs - after_key, 0)
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
# ⚠ IT DID READ, and that is correct rather than a leak: the press is a
# TRIGGER, not an answer. The panel was up, so the reading describes it -- and
# when the game eats the key the panel is still up and the reading is still
# true. What must not move is the FLAG.
check('the panel was read, because it was up', screen3.grabs, 1)
check('names published from a panel that really was there',
      state3.weapon_gt, ('aug', 'm416'))

print('\n=== the screen can change with NO keypress (alt-tab, dialog) ===')
w4, state4, screen4 = build()
screen4.open = True
t = time.perf_counter()
t = run(w4, TAB_DRIFT_S + 0.05, t0=t)
check('drift check noticed it opened', w4.open, True)
screen4.open = False
t = run(w4, TAB_DRIFT_S + 0.05, t0=t)
check('drift check noticed it closed', w4.open, False)
# ⚠ And read nothing on the way, in EITHER direction. Neither transition had a
# keypress, so neither had a moment at which the panel was known to be up.
check('and looked at the panel neither time', screen4.grabs, 0)

print('\n=== a close NOBODY announced reads nothing at all ===')
# ⚠ THE HONEST FAILURE, AND IT IS THE COST OF THE DESIGN. Alt-tab, a
# disconnect dialog, another agent: there is no keypress, so there is no
# moment at which the panel was known to be up, and by the time the drift
# check notices it is gone. Reading THEN is what produced six frames of grass.
# tab_open still follows the screen; the loadout keeps what it last knew.
w5, state5, screen5 = build()
screen5.open = True
screen5.show(1, ('vss', 'p90'))
w5._set_open(True)
screen5.open = False
screen5.painted = False          # by the time drift notices, it is gone
t = run(w5, TAB_DRIFT_S + 0.05, t0=time.perf_counter())
check('the drift check noticed', w5.open, False)
check('and looked at nothing', screen5.grabs, 0)   # no press, no snap
check('nothing published', state5.weapon_gt, ('', ''))

print('\n=== an unpainted panel still refuses to publish a kit ===')
# The guard stays even though the read now happens while the panel is up:
# `any_drawn` is what separates "this gun wears nothing" from "the tiles are
# not drawn yet", and classify() gives both the same ''. ⚠ It cannot do more
# than that -- it asks whether there is DETAIL in the tile rings, and bare
# grass and timber score far above its 46. It is a within-panel guard, never
# evidence that a panel is there.
w5b, state5b, screen5b = build()
screen5b.open = True
screen5b.show(1, ('vss', 'p90'))
screen5b.painted = False
w5b._set_open(True)
w5b.on_key(time.perf_counter())
check('the panel WAS snapped and read', screen5b.grabs, 1)
check('attachments NOT published', 1 in state5b.attachments, False)
check('attachment read never even attempted', screen5b.att_reads, 0)
check('but the weapon names still are', state5b.weapon_gt, ('vss', 'p90'))

print('\n=== a gun nothing can NAME gets no kit ===')
# ⚠ MEASURED IN A PLAY LOG, 2026-08-09. Both name plates came back blank for a
# whole session, and the slot templates are narrowed BY the weapon name -- so
# every tile was matched against all 55, and a blind match does not fail, it
# answers: `muzzle-choke` (a shotgun part) on one gun, `stock-cheek_pad` (a
# sniper part) on the other, published and keyed into the curve store.
#
# The names publishing while the kit does not is the honest split: the plate
# read is its own evidence, the slot read is only as good as the name.
w7, state7, screen7 = build()
screen7.open = True
screen7.show(1, ('', ''), kit={1: {'muzzle': 'choke'},
                               2: {'stock': 'cheek_pad'}})
w7._set_open(True)
screen7.open = False
w7.on_key(time.perf_counter())
t = run(w7, 0.05, t0=time.perf_counter())
check('the slots WERE read', screen7.att_reads, 1)
check('but no kit was published for gun 1', 1 in state7.attachments, False)
check('nor for gun 2', 2 in state7.attachments, False)

# ...and a gun the HUD already named keeps its kit, because the name it is
# checked against is the EFFECTIVE one. Dropping the kit whenever the Tab
# plate is unreadable would throw away a reading that was never in doubt.
w8, state8, screen8 = build()
state8.weapon_pred = ('m416', '')
screen8.open = True
screen8.show(1, ('', ''), kit={1: {'muzzle': 'comp_ar'}, 2: {'stock': 'x'}})
w8._set_open(True)
w8.on_key(time.perf_counter())   # the closing press is what reads
screen8.open = False
t = run(w8, 0.05, t0=time.perf_counter())
check('gun 1 is named by the HUD, so its kit publishes',
      state8.attachments.get(1), {'muzzle': 'comp_ar'})
check('gun 2 is still nameless, so its kit does not',
      2 in state8.attachments, False)

print('\n=== the close leaves its frame on disk, in ITS OWN directory ===')
# The save is the operator's only way to ask "what did it look like", so a
# broken save path has to fail HERE rather than during a match. Looking at the
# directory before and after is also what pins it to a directory that is not
# the one real play writes to.
before = set(glob.glob(os.path.join(SHOT_DIR, '*.png')))
w9, state9, screen9 = build()
# Tab HELD: press opens it, release closes it. Both edges arrive, and the
# names have to say which edge saw what — that pairing is the only thing a
# frame on disk can be matched against a line in the log by.
screen9.open = False
w9.on_key(time.perf_counter(), 'press')
screen9.open = True
run(w9, 0.05, t0=time.perf_counter())
w9.on_key(time.perf_counter(), 'release')
screen9.open = False
run(w9, 0.05, t0=time.perf_counter())
after = set(glob.glob(os.path.join(SHOT_DIR, '*.png')))
new = sorted(os.path.basename(f) for f in after - before)
check('one frame per edge, both edges', len(new), 2)
check('and the name says which edge, and what it found',
      [n.split('_')[-1] for n in new], ['press-shut.png', 'release-open.png'])
check('and NOT into the directory real play writes to',
      os.path.abspath(SHOT_DIR) != os.path.abspath(REAL_SHOT_DIR), True)

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
