"""The trigger comes back up, even when the burst dies. No game, no hardware.

    pixi run fire

fire_magazine() holds the fire button down for up to MAX_FIRE_S and grabs
frames the whole time. Since the frame source started raising FocusLost when
the game leaves the foreground (capture/cropper.ScreenBuffer), that loop has
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

import time

import numpy as np

import control.fire as fire_mod
from control.fire import FireDriver, PREFIRE_FRAMES
from capture.cropper import FocusLost

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

    def slice_weapon(self, frame):
        return None

    def slice_reticle(self, frame):
        # ⚠ None, not a black crop. This double exists to keep the burst loop
        # running, and a fabricated reticle would put a barrel reading into a
        # test that measures the fire guard. None is what find_reticle turns
        # into nan, i.e. "not measured" — the honest answer here.
        return None


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


print('\n=== wait_reload reads the NUMBER, and does not wait when it can ===')
# The reload used to end on "the ammo pixels stopped moving" plus a flat 1.8 s,
# because the counter refills partway through the animation and pixels cannot
# say how far in they are. Digits can. These check both halves of that: the
# digit path returns on the reading and sleeps nothing, and the pixel path is
# still there for a weapon whose counter does not read -- and SAYS SO.


class Ammo:
    """Hands back a scripted sequence of counter readings. None is not zero."""

    def __init__(self, seq):
        self.seq, self.i = list(seq), 0

    def classify(self, _crops):
        v = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return v


def reload_run(seq, expect, **patch):
    """-> (returned value, total seconds slept). The sleep total is the test:
    a digit-driven reload must not be paying a fixed settle on top."""
    fd, frames, mouse = driver()
    fd.ammo_det = None if seq is None else Ammo(seq)
    slept, real_sleep = [], time.sleep
    old = {k: getattr(fire_mod, k) for k in patch}
    try:
        time.sleep = lambda s=0, *a, **k: slept.append(s)
        for k, v in patch.items():
            setattr(fire_mod, k, v)
        return fd.wait_reload(expect), sum(slept)
    finally:
        time.sleep = real_sleep
        for k, v in old.items():
            setattr(fire_mod, k, v)


# Mid-animation the counter is unreadable, then it climbs, then it is full.
# RELOAD_CONFIRM = 2 means one lone parse is not a reading.
got, slept = reload_run([None, None, 0, 0, 12, 30, 30, 30], expect=30)
check('a counter that reaches the magazine size ends the reload',
      got is not None, True)
# Not zero any more: the counter comes back PARTWAY through the animation,
# so the digit path pays RELOAD_READY_SETTLE_S and nothing else. The point
# of the check is that it is not paying SETTLE_AFTER_RELOAD_S's 1.8 s
# guess on top -- that one is anchored to the end of FIRING, this one to a
# read number.
check('...and it pays only the ready settle', slept,
      fire_mod.RELOAD_READY_SETTLE_S)
check("...which is well under the pixel path's blind wait",
      slept < fire_mod.SETTLE_AFTER_RELOAD_S, True)

# top_up() does not know the size yet -- it is the thing that measures it -- so
# the rule relaxes to "above zero and no longer changing".
got, slept = reload_run([None, 30, 30, 30, 30, 30], expect=None,
                        RELOAD_HOLD_S=0.0)
check('with no expected size, a settled non-zero reading ends it',
      got is not None, True)

# Digits that read and never reach the size: a reload that did not happen. It
# must NOT fall through to the pixel path and call the frozen counter settled.
got, _ = reload_run([0] * 40, expect=30, RELOAD_TIMEOUT_S=0.25)
check('a counter stuck at 0 times out rather than settling', got, None)

# No counter at all: the pixel fallback, which still needs its settle because
# "the pixels stopped" says nothing about how far into the animation this is.
got, slept = reload_run(None, expect=30, RELOAD_MIN_S=0.05,
                        RELOAD_STATIC_S=0.01, RELOAD_TIMEOUT_S=2.0)
check('an unreadable counter still finishes on pixels', got is not None, True)
check('...and that path DOES pay the settle',
      slept >= fire_mod.SETTLE_AFTER_RELOAD_S, True)

# ── the rate a magazine's counter is allowed to set ─────────────────────────
#
# ⚠ EVERY NUMBER BELOW IS FROM THE NIGHT OF 2026-08-10, where this cost five
# guns (akm, g36c, k2, tommy, uzi) and every printed line looked reasonable.
# The counter reported a FULL magazine after a burst that moved 1823 counts,
# `max(1, 0)` rounded "nothing was measured" up to "one round went out", the
# 4.24 s hold became one 4242 ms interval, and the NEXT hold -- 42 x 4242 ms --
# blew up inside struct.pack four frames away in another layer.
from calibration.collect_timed import rate_refusal, RATE_MIN_ROUNDS   # noqa: E402

# THE FAILURE ITSELF. The magazine is full: the counter saw nothing.
why = rate_refusal(fired_n=0, mag_size=42, hold_s=4.24, interval_s=0.0998)
check('a counter that saw NOTHING go out sets no rate', why is not None, True)
check('...and it says the counter is the doubtful one, not the burst',
      'NOTHING' in (why or ''), True)

# A negative count is the same fact wearing a different sign, and the old
# `max(1, ...)` swallowed it just as quietly.
check('more rounds than the magazine holds is refused too',
      rate_refusal(-3, 42, 4.24, 0.0998) is not None, True)

# TOO FEW TO MEASURE. One miscounted round out of 3 is a 50% error in the
# interval, and that error would then size every following hold.
check('a handful of rounds is not a rate',
      rate_refusal(3, 42, 0.30, 0.0998) is not None, True)
check('...and the boundary is RATE_MIN_ROUNDS, not a hand-picked number',
      rate_refusal(RATE_MIN_ROUNDS - 1, 42, 1.0, 0.0998) is not None, True)

# A COUNTER THAT LIES HIGH RATHER THAN LOW: enough rounds to pass the first
# gate, an interval nowhere near the stored one.
check('a rate 40x off the stored one describes the counter, not the gun',
      rate_refusal(20, 42, 80.0, 0.0998) is not None, True)

# ⚠ AND THE OTHER SIDE, WHICH IS THE HALF THAT MATTERS. A gate that refuses
# everything is not a gate -- the whole point of this feedback is that a gun
# whose STORED rpm is wrong costs one short burst instead of every burst, and
# that only works if a real disagreement still gets through.
why = rate_refusal(fired_n=30, mag_size=42, hold_s=3.60, interval_s=0.0998)
check('a real, well-counted disagreement is still ACCEPTED', why, None)
check('...and it is a genuine correction, not a rounding',
      abs((3.60 / 29) / 0.0998 - 1) > 0.2, True)

# The mg3's two automatic modes are 1.50x apart -- the widest real gap on the
# roster -- so the band has to admit it or the gun can never be re-measured.
check("the mg3's 1.50x mode gap is inside the band",
      rate_refusal(40, 75, 0.0998 * 1.50 * 39, 0.0998), None)

# ⚠ THE SECOND GUARD, CALLED FOR REAL. The first version of these three lines
# asserted `(42-1+1.5) * 4.2422 > MAX_FIRE_S` -- arithmetic this file did
# itself, about a constant, touching `fire_magazine_timed` not at all. Deleting
# the guard from control/fire.py left the gate GREEN (verified 2026-08-10).
# A gate that re-derives its subject instead of running it is the shape this
# repository keeps paying for, and it appeared here inside the fix for another
# instance of it.
import control.fire as _fire                                          # noqa: E402


class _RecordingMouse:
    """Records the click instead of blocking it, so the SAME fake serves both
    sides: the refusal is "no click was issued", not "the fake exploded"."""

    def __init__(self):
        self.clicks = []

    def click(self, **kw):
        self.clicks.append(kw)
        return time.perf_counter()


class _NoFrames:
    """grab_timed() answers "no frame yet", so the loops spin on the clock and
    never need a screen."""

    n_missed = 0

    def grab_timed(self):
        return time.perf_counter(), None


def timed_run(mag_size, interval_s):
    """-> (error message or None, the clicks that were issued)"""
    mouse = _RecordingMouse()
    fd = _fire.FireDriver(_NoFrames(), mouse, None)
    fd.PREFIRE_S = 0.0
    fd.TAIL_S = 0.0
    try:
        fd.fire_magazine_timed(_NoFrames(), mag_size, interval_s)
        return None, mouse.clicks
    except ValueError as e:
        return str(e), mouse.clicks


# 42 rounds at the rate the broken counter derived: 178 s of trigger.
err, clicks = timed_run(42, 4.2422)
check('an impossible hold is refused', err is not None, True)
check('...BEFORE the trigger is pressed', clicks, [])
check('...naming the INTERVAL as the suspect, not the magazine',
      'INTERVAL' in (err or ''), True)
check('...and pointing at where that number came from',
      'rate_refusal' in (err or ''), True)

# ⚠ AND THE OTHER SIDE: a real magazine must still fire. Without this the guard
# could be `if True: raise` and every line above would still pass.
err, clicks = timed_run(10, 0.0998)
check('a real magazine is NOT refused', err, None)
check('...and it does press the trigger', len(clicks), 1)

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
    sys.exit(1)
print('all ok')
