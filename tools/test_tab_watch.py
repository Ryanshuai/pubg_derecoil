"""TabWatch's state machine, offline. No game, no screen, no hardware.

    pixi run tab-watch

The point of TabWatch is that tab_open moves only because the screen was
looked at. That is a claim about control flow, so it is testable without
pixels: feed it a scripted screen and check what it does.

The cases that matter are the ones the toggle got wrong -- a keypress the game
swallowed, and a screen that changes with no keypress at all.
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

from config import SCREEN_H, SCREEN_W, TAB_DRIFT_S, TAB_REFRESH_S, TAB_SETTLE_S
from control.tab_watch import TabWatch


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
    """What the screen currently shows, and how often it was looked at."""

    def __init__(self):
        self.open = False
        self.guns = ('aug', 'm416')
        self.type_reads = 0
        self.panel_reads = 0

    def type_det(self):
        s = self
        class D:
            def classify(_self, crops):
                s.type_reads += 1
                return s.open
        return D()

    def weapon_det(self):
        s = self
        class D:
            def classify(_self, crops):
                s.panel_reads += 1
                return s.guns
        return D()

    def att_det(self):
        s = self
        class D:
            def classify(_self, crops):
                return {1: {'muzzle': 'comp'}, 2: {'muzzle': ''}}
        return D()


def build():
    screen = FakeScreen()
    state = FakeState()
    w = TabWatch(state, {'tab_type': screen.type_det(),
                         'tab_weapon': screen.weapon_det(),
                         'tab_attachment': screen.att_det()},
                 verbose=False)
    # The grabbers are never built: every detector above ignores its crops.
    w._type_grab = _Nothing()
    w._panel_grab = _Nothing()
    return w, state, screen


class _Nothing:
    """A grabber that returns a correctly-shaped blank.

    Shape matters: TabWatch slices screen coordinates out of what it gets
    back, and the detectors above ignore the pixels. A dict here would raise
    inside read_loadout's except, which would look exactly like a real read
    failure -- it did, the first time this was written.
    """
    _buf = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)

    def grab(self):
        return self._buf

    def close(self):
        pass


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
w.on_key(t)                     # user pressed Tab; screen has NOT changed yet
check('open right after the key', w.open, False)
t = run(w, 0.05, t0=t)           # ticks, but the fake screen is still closed
check('open while the screen stays closed', w.open, False)

print('\n=== it opens when the SCREEN opens, not before ===')
screen.open = True               # now the game actually drew it
t = run(w, 0.05, t0=t)
check('open once the screen shows it', w.open, True)
check('state.tab_open followed', state.tab_open, True)

print('\n=== while up, the loadout is kept fresh ===')
before = screen.panel_reads
t = run(w, TAB_REFRESH_S * 3.5, t0=t)
check('re-read the panel while open', screen.panel_reads > before, True)
check('weapon_gt published', state.weapon_gt, ('aug', 'm416'))
check('attachments published', state.attachments[1], {'muzzle': 'comp'})

print('\n=== a SWALLOWED keypress must not move the flag ===')
w2, state2, screen2 = build()
screen2.open = True
w2._set_open(True, time.perf_counter())
t = time.perf_counter()
w2.on_key(t)                     # key sent... but the game ignores it
t = run(w2, TAB_SETTLE_S + 0.05, t0=t)
check('still open after the key was eaten', w2.open, True)
check('state agrees with the screen', state2.tab_open, screen2.open)

print('\n=== the screen can change with NO keypress (alt-tab, dialog) ===')
w3, state3, screen3 = build()
screen3.open = True
t = time.perf_counter()
t = run(w3, TAB_DRIFT_S + 0.05, t0=t)
check('drift check noticed it opened', w3.open, True)
screen3.open = False
t = run(w3, TAB_DRIFT_S + 0.05, t0=t)
check('drift check noticed it closed', w3.open, False)

print('\n=== closing keeps the last reading taken while it was up ===')
w4, state4, screen4 = build()
screen4.open = True
w4._set_open(True, time.perf_counter())
t = run(w4, TAB_REFRESH_S * 2, t0=time.perf_counter())
screen4.guns = ('kar98k', 'ump45')      # user swapped a gun
t = run(w4, TAB_REFRESH_S * 2, t0=t)
check('fresh reading while open', state4.weapon_gt, ('kar98k', 'ump45'))
screen4.open = False                     # and now they close it
w4.on_key(t)                             # on_key reads once more, still up
t = run(w4, 0.05, t0=t)
check('closed', w4.open, False)
check('final loadout survives the close', state4.weapon_gt, ('kar98k', 'ump45'))

print('\n=== idle costs nothing but the drift check ===')
w5, state5, screen5 = build()
t = run(w5, 1.0, t0=time.perf_counter())
expected = int(1.0 / TAB_DRIFT_S) + 1
check(f'type reads in 1 idle second (<= {expected})',
      screen5.type_reads <= expected, True)
check('no panel reads while closed', screen5.panel_reads, 0)

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
    sys.exit(1)
print('all ok')
