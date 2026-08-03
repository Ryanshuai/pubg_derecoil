"""The trigger comes back up, even when the burst dies. No game, no hardware.

    pixi run fire

fire_magazine() holds the fire button down for up to MAX_FIRE_S and grabs
frames the whole time. Since the frame source started raising FocusLost when
the game leaves the foreground (detector/cropper.ScreenBuffer), that loop has
an exception path through the middle of it — and the thing on the other side
of that path is a mouse button that is still held.

The failure is not a crashed run. It is a character firing into a window
nobody is watching, through the reload, into the next cell, while the
traceback scrolls past. So the release lives in a `finally`, and this is what
says so.

TESTED OFFLINE ON PURPOSE. Proving it on the real game means deliberately
stealing focus mid-burst, and if the guard is broken THAT run is the one that
leaves the trigger down. A fake frame source raises on cue, costs nothing, and
can be re-run after every edit.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import numpy as np

from control.fire import FireDriver, PREFIRE_FRAMES
from detector.cropper import FocusLost

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {name:<52} {got!r}'
          + ('' if ok else f'  != {want!r}'))
    if not ok:
        FAILS.append(name)


class Frames:
    """Hands out frames, then raises FocusLost on the nth grab."""

    def __init__(self, raise_at=None):
        self.raise_at = raise_at
        self.n = 0

    def grab(self):
        self.n += 1
        if self.raise_at is not None and self.n >= self.raise_at:
            raise FocusLost('game is no longer the foreground window')
        return {'ammo': np.zeros((48, 90, 3), np.uint8),
                'posture': np.zeros((40, 40, 3), np.uint8)}

    def flush(self, n=8):
        for _ in range(n):
            self.grab()


class Mouse:
    """Records the button traffic, which is the whole point."""

    def __init__(self):
        self.clicks = []

    def click(self, buttons=0, duration_ms=0):
        self.clicks.append(buttons)

    def held(self):
        """Is the fire button down, given everything sent so far?"""
        down = False
        for b in self.clicks:
            down = bool(b & 0x01)
        return down


class Tracker:
    def slice_frame(self, frame):
        return [np.zeros((8, 8), np.float32)]


class Gun:
    """Enough GunDriver to keep the burst loop happy."""

    def __init__(self):
        self.dumped = []

    def ads_signals(self, frame=None):
        return True, True

    def dump(self, tag):
        self.dumped.append(tag)


def driver(raise_at=None):
    frames = Frames(raise_at)
    mouse = Mouse()
    fd = FireDriver(frames, mouse, Tracker(), ammo_det=None, gun=Gun())
    return fd, frames, mouse


print('\n=== the trigger goes down, and comes back up ===')
# A burst that ends normally: no ammo detector, so the loop falls through to
# the flicker heuristic and stops on EMPTY_STATIC_S of a frozen counter.
fd, frames, mouse = driver()
rec, fire_s, steps, *_ = fd.fire_magazine()
check('a burst that finishes presses fire', 0x01 in mouse.clicks, True)
check('...and releases it', mouse.held(), False)
check('...releasing is the LAST thing sent', mouse.clicks[-1], 0x00)

print('\n=== a burst killed mid-flight still releases it ===')
# THE regression. Raise a few grabs into the loop, which is where a lost
# foreground actually lands: after the trigger is down and before any exit
# condition has been reached.
for at in (PREFIRE_FRAMES + 2, PREFIRE_FRAMES + 10, PREFIRE_FRAMES + 40):
    fd, frames, mouse = driver(raise_at=at)
    raised = False
    try:
        fd.fire_magazine()
    except FocusLost:
        raised = True
    check(f'FocusLost at grab {at} propagates', raised, True)
    check(f'   ...and the button is not still held', mouse.held(), False)
    check(f'   ...released exactly once', mouse.clicks.count(0x00), 1)

print('\n=== and if the frames die before the trigger, nothing is pressed ===')
# The pre-fire baseline grabs run BEFORE the click. Dying there must not leave
# a press behind either -- there was none to release.
fd, frames, mouse = driver(raise_at=1)
raised = False
try:
    fd.fire_magazine()
except FocusLost:
    raised = True
check('FocusLost before the trigger propagates', raised, True)
check('...and nothing was ever pressed', mouse.clicks, [])

print('\n=== wait_reload is the other loop that grabs ===')
fd, frames, mouse = driver(raise_at=2)
raised = False
try:
    fd.wait_reload()
except FocusLost:
    raised = True
# It presses nothing, so there is nothing to leak; what matters is that it
# does not swallow the exception and report a finished reload.
check('a lost foreground during the reload propagates', raised, True)

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
    sys.exit(1)
print('all ok')
