"""Cursor placement, clicks and drags on the game's UI screens — plus the
relative moves that aim, which are the same device and nothing like the same
gesture. See Pointer.move() vs Pointer.move_to().

Placement is always SetCursorPos: the spawner and Tab screens are UI, where
the game's cursor follows the system cursor. The *button* is a different
matter — it goes through the Pico as a real HID report, which the game sees
even under raw input. SendInput is the fallback when no Pico is attached, and
it is a genuinely worse one; see press/soft_mouse.py.

Focus is not handled here: taking and holding the game window is its own
closed loop, so it lives in control/focus.py. Call ensure_focus() from there
before driving anything through this module.
"""
import ctypes
import time

_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010

MOVE_WAIT = 0.12       # cursor settle before the button goes down

# A UI click is three waits, and every one of them is on the critical path of
# every calibration run: the spawner alone fires a dozen per kit.
#
# Measured 2026-08-02 (tools/probe_click_speed.py, right-click equips read back
# off the weapon slot). settle held at 5/5 all the way down to 0, hold_ms down
# to 10 ms, after down to 0.02 -- but after=0 lands 0/5, because the click is
# handed to the Pico asynchronously and returning immediately races the CDC
# write. The values here sit one notch above each measured floor.
#
# Only settle and after cost wall clock. pico.click() returns at once and the
# firmware times the hold itself, so hold_ms buys nothing back -- it is kept
# short only so `after` can be, without a click returning mid-press.
CLICK_SETTLE = 0.015   # cursor placed -> button down (was 0.12)
CLICK_HOLD_MS = 20     # button held down, per click (was 80)
CLICK_AFTER_S = 0.035  # after the release, before returning (was 0.09)

# ── Drag timings ─────────────────────────────────────────────────────────
# A drag is press -> travel -> release with the button held the whole way.
# The Pico cannot simply "hold": CMD_CLICK carries a duration. It is fully
# asynchronous though — the firmware only stores an end time and keeps the
# button down until then — so an arbitrary hold is one CMD_CLICK plus a re-arm
# every DRAG_REARM_S (each re-arm pushes inject_end_ms out, with no release in
# between), and the release is a CMD_CLICK of duration 0, which expires on the
# firmware's very next report.
DRAG_GRAB_WAIT = 0.12   # button down -> first move: the UI has to latch the
                        # item under the cursor before it will follow
DRAG_STEPS = 10         # interpolated positions. A single jump gets read as a
                        # click, and the target slot never lights up.
DRAG_STEP_WAIT = 0.016  # >= one frame at 144 Hz per step
DRAG_HOVER_WAIT = 0.14  # at the target, before the button comes up
DRAG_DROP_WAIT = 0.25   # after release, before the screen is read back
DRAG_HOLD_MS = 400      # Pico hold per arm; must exceed DRAG_REARM_S by a lot
DRAG_REARM_S = 0.15     # so a dropped CDC packet still leaves 250 ms of hold

# ── Getting hold of the device ────────────────────────────────────────────
PICO_RETRIES = 3         # A brief retry only covers the CDC port still closing
PICO_RETRY_S = 1.0       # behind a run of this tool. It is deliberately short:
                         # this Pico is shared with other agents, and a locked
                         # port usually means one of them is using it right now
                         # — that is a reason to get out of the way, not to
                         # wait longer and take it the moment they finish.


class NoPico(RuntimeError):
    """No Pico, so nothing the caller sends would reach the game as raw input."""


class _POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


def move_cursor(xy):
    """Absolute cursor placement. Used to park the cursor off a UI element
    before a screenshot — whatever is under it draws a hover highlight."""
    ctypes.windll.user32.SetCursorPos(int(xy[0]), int(xy[1]))


class Pointer:
    """Cursor placement, buttons, drags — and relative aiming moves.

    Two different mice live behind one object, and mixing them up is the
    mistake to avoid: move_to() is the SYSTEM CURSOR, which the UI screens
    follow, while move() is RAW MOTION, which turns the character's view. The
    game reads them off separate paths, which is why SendInput is good enough
    for the first and close to useless for the second.
    """

    def __init__(self, backend='auto'):
        self.pico = None
        if backend in ('auto', 'pico'):
            try:
                from press.pico_mouse import PicoMouse, get_mouse
                mouse = get_mouse()
                # get_mouse() honours config.MOUSE_BACKEND and can hand back a
                # SoftMouse, whose click()/press are no-ops (Pico-only
                # features). Taking it would leave every click silently doing
                # nothing while this still printed "backend = pico".
                if not isinstance(mouse, PicoMouse):
                    raise RuntimeError(f'{type(mouse).__name__} cannot click; '
                                       f'set config.MOUSE_BACKEND = "pico"')
                self.pico = mouse
            except Exception as e:
                if backend == 'pico':
                    raise
                # "unplugged" and "someone else has it" are not the same
                # problem and must not get the same answer. Falling back to
                # SendInput when ANOTHER AGENT holds the port means: I cannot
                # have the device, so I will drive the mouse of whoever does.
                # That is worse than failing -- park() alone moves the cursor,
                # and the run being disturbed is mid-magazine with no way to
                # tell that its numbers just went wrong.
                #
                # Seen 2026-08-03: a verify run started while a harvest held
                # COM10, fell back here, and went on to move the cursor and
                # try to toggle Tab under it.
                from press.pico_mouse import other_agents
                busy = other_agents()
                if busy:
                    raise RuntimeError(
                        f'the Pico is held by another agent ({busy}), and '
                        f'SendInput would drive the same mouse it is using. '
                        f'Refusing rather than interfering — wait for it, and '
                        f'do not kill it. ({e})')
                print(f'[pointer] no Pico ({e}); falling back to SendInput')
        self.backend = 'pico' if self.pico else 'sendinput'
        print(f'[pointer] click backend = {self.backend}')

    @classmethod
    def opened(cls, backend='auto', retries=PICO_RETRIES, retry_s=PICO_RETRY_S):
        """A Pointer with a Pico behind it, retried, and fatal if it never arrives.

        Two separate lessons. The retry: the CDC port stays locked for about a
        second after a previous run exits, so "busy" arrives as
        PermissionError and is indistinguishable from "unplugged" — one run
        died that way seconds after a successful one.

        The refusal: falling back to SendInput used to be a printed warning
        that the run then sailed straight past, into the operator prompt, ready
        to spend four minutes producing frames the game never acted on. A
        degraded backend is not a degraded run here, it is a worthless one, so
        it takes an explicit --backend sendinput to get it.

        Both lessons are the DEVICE's rather than any one caller's, which is
        why they live here and not in the tool that learned them: PUBG reads
        raw HID, so a synthetic right-click or view move is ignored no matter
        who sent it. Plain Pointer() stays the constructor for callers that
        genuinely tolerate SendInput — a UI click through SetCursorPos does
        land — and this is the one for callers that do not.
        """
        for i in range(retries):
            p = cls(backend)
            if p.pico or backend == 'sendinput':
                return p
            if i + 1 < retries:
                print(f'[pointer] no Pico yet — retrying in {retry_s:g}s '
                      f'({i + 1}/{retries - 1})', flush=True)
                time.sleep(retry_s)
        raise NoPico(
            f'no Pico after {retries} tries. The game reads raw input, so '
            f'a SendInput right-click is ignored and every "ads" frame would '
            f'be hip fire. If the port came back "access denied", something '
            f'else has it — this Pico is shared, so check whether another '
            f'agent is mid-run before taking it. Otherwise check the cable, '
            f'or pass --backend sendinput to capture without it anyway.')

    def move_to(self, x, y):
        ctypes.windll.user32.SetCursorPos(int(x), int(y))

    def move(self, dx, dy):
        """Relative motion — the mouse as an AIMING device, not as a cursor.

        Nothing to do with move_to(). That places the system cursor so a UI
        click lands on a widget; this rotates the character's view, and the
        game reads the two off different paths — which is why SetCursorPos is
        enough for the Tab screen and useless for turning.

        SendInput here is the same bad fallback it is for the button (PUBG
        takes raw HID for aiming), but it is press/'s bad fallback in ONE
        place. It used to be a ctypes.windll.user32.mouse_event copied into
        whichever tool needed to turn the view, which bypassed this whole
        layer and, worse, used the legacy API rather than the SendInput path
        in press/soft_mouse.py that carries the 64-bit INPUT alignment fix.
        """
        if not dx and not dy:
            return
        if self.pico:
            # The firmware accumulates the delta and drains it at 127/report,
            # so an arbitrarily large jump is safe to send in one packet.
            self.pico.move(int(dx), int(dy))
        else:
            # Private on purpose: _send_move is the struct that got the
            # alignment right. Re-deriving it here is how a second copy of
            # that bug gets born.
            from press.soft_mouse import _send_move
            _send_move(int(dx), int(dy))

    def click(self, buttons=0x01, hold_ms=CLICK_HOLD_MS, after=0.0):
        """Press and release wherever the cursor already is.

        Split out of click_at because not every button press is a UI click.
        ADS is the case that forced it: the right button toggles the sight,
        the cursor's position is irrelevant, and placing it first would be a
        SetCursorPos nobody asked for in the middle of a capture.

        `after` only means anything on the Pico path — see click_at, where the
        measurement behind it is written down.
        """
        if self.pico:
            self.pico.click(buttons, hold_ms)
            if after:
                time.sleep(after)
        else:
            down = (_MOUSEEVENTF_RIGHTDOWN if buttons & 0x02
                    else _MOUSEEVENTF_LEFTDOWN)
            up = (_MOUSEEVENTF_RIGHTUP if buttons & 0x02
                  else _MOUSEEVENTF_LEFTUP)
            ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
            time.sleep(hold_ms / 1000.0)
            ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)

    def cursor_pos(self):
        pt = _POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def click_at(self, x, y, settle=CLICK_SETTLE, buttons=0x01,
                 hold_ms=CLICK_HOLD_MS, after=CLICK_AFTER_S):
        """Click at (x, y). buttons=0x02 is the right button.

        Right-click is a real gesture on the Tab screen, not a curiosity: it
        equips the item under the cursor straight onto the gun, which is one
        click where a drag is a press-travel-release with a settle at each end.
        """
        self.move_to(x, y)
        time.sleep(settle)
        got = self.cursor_pos()
        if abs(got[0] - x) > 2 or abs(got[1] - y) > 2:
            print(f'[pointer] warning: cursor landed at {got}, wanted {(x, y)}')
        # This is click() plus the placement. The old SendInput branch capped
        # its hold at 60 ms; no caller has ever passed hold_ms, so at the
        # 20 ms default the cap never applied and dropping it changes nothing
        # any measurement here was taken with.
        self.click(buttons, hold_ms, after=after)

    def right_click_at(self, x, y, **kw):
        """Right-click — on the Tab screen, "equip this"."""
        kw.setdefault('buttons', 0x02)
        return self.click_at(x, y, **kw)

    # ── Drag ──

    def _press(self, buttons=0x01):
        if self.pico:
            self.pico.click(buttons, DRAG_HOLD_MS)
        else:
            ctypes.windll.user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)

    def _release(self, buttons=0x01):
        if self.pico:
            self.pico.click(buttons, 0)   # ends the hold on the next report
        else:
            ctypes.windll.user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def drag(self, src, dst, settle=MOVE_WAIT, steps=DRAG_STEPS,
             grab=DRAG_GRAB_WAIT, hover=DRAG_HOVER_WAIT,
             drop=DRAG_DROP_WAIT, buttons=0x01):
        """Press at `src`, travel to `dst`, release there.

        False means the cursor did not go where it was told — another process
        moved it, or the coordinate is off-screen. The button is always
        released, including on an exception, because a stuck-down button in a
        UI screen leaves an item glued to the cursor.

        A True return says only that the gesture was performed; whether the
        game accepted the drop is for the caller to read back off the screen.
        """
        sx, sy = int(src[0]), int(src[1])
        tx, ty = int(dst[0]), int(dst[1])

        self.move_to(sx, sy)
        time.sleep(settle)
        got = self.cursor_pos()
        if abs(got[0] - sx) > 2 or abs(got[1] - sy) > 2:
            print(f'[pointer] drag aborted before press: cursor at {got}, '
                  f'wanted {(sx, sy)}')
            return False

        self._press(buttons)
        last_arm = time.perf_counter()
        try:
            time.sleep(grab)
            for i in range(1, steps + 1):
                f = i / steps
                self.move_to(round(sx + (tx - sx) * f),
                             round(sy + (ty - sy) * f))
                time.sleep(DRAG_STEP_WAIT)
                now = time.perf_counter()
                if self.pico and now - last_arm >= DRAG_REARM_S:
                    self.pico.click(buttons, DRAG_HOLD_MS)
                    last_arm = now
            time.sleep(hover)
        finally:
            self._release(buttons)
        time.sleep(drop)

        got = self.cursor_pos()
        if abs(got[0] - tx) > 2 or abs(got[1] - ty) > 2:
            print(f'[pointer] drag released at {got}, not {(tx, ty)}')
            return False
        return True
