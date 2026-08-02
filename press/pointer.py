"""Cursor placement, clicks and drags on the game's UI screens.

Placement is always SetCursorPos: the spawner and Tab screens are UI, where
the game's cursor follows the system cursor. The *button* is a different
matter — it goes through the Pico as a real HID report, which the game sees
even under raw input. SendInput is the fallback when no Pico is attached, and
it is a genuinely worse one; see press/soft_mouse.py.

game_focused() lives here rather than in a detector because it guards every
one of these calls: driving the mouse while the game is not frontmost types
into whatever *is* frontmost.
"""
import ctypes
import time

import win32gui
import win32process

_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004

MOVE_WAIT = 0.12       # cursor settle before the button goes down

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


class _POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


GAME_EXES = ('tslgame',)     # PUBG ships as TslGame.exe


def _exe_of(hwnd):
    try:
        import psutil
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().lower()
    except Exception:
        return ''


def _is_game(hwnd):
    exe = _exe_of(hwnd)
    return any(exe.startswith(k) for k in GAME_EXES)


def game_focused():
    """Is PUBG the foreground window?

    Matched on the EXECUTABLE, never the title. The title-based version
    accepted any window whose caption contained "pubg" — which includes this
    repository open in an editor, so every focus guard here silently passed
    while the game sat in the background. That is the exact failure the guards
    exist to catch.
    """
    try:
        return _is_game(win32gui.GetForegroundWindow())
    except Exception:
        return False


def game_hwnd():
    """The game's main window, found by process rather than caption.

    Largest visible window belonging to the game process: PUBG owns several
    (splash, IME, hidden helpers) and only one of them is the one that takes
    input."""
    best, best_area = None, 0
    def _cb(hwnd, _):
        nonlocal best, best_area
        if not win32gui.IsWindowVisible(hwnd) or not _is_game(hwnd):
            return
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
        except Exception:
            return
        area = max(0, r - l) * max(0, b - t)
        if area > best_area:
            best, best_area = hwnd, area
    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return None
    return best


def raise_game(settle_s=0.8):
    """Put the game window in front. Returns whether it actually got focus.

    Every tool here is launched from a terminal, so at t=0 the terminal owns
    focus and the game does not — which means the very first `game_focused()`
    fails and the run aborts before doing anything. The established workaround
    is a countdown that asks a human to alt-tab (harvest.py --countdown), and
    that is the reason those runs are not truly unattended.

    Calling this first removes the human from the loop. Windows only lets the
    current foreground process hand focus away, so SetForegroundWindow can
    legitimately refuse — hence a bool rather than an exception, and hence the
    countdown staying as a fallback:

        if not game_focused():
            raise_game()
        if not game_focused():
            ...count down and let a human switch...

    Verifying with game_focused() afterwards is not optional. The call can
    return without error and still leave the window flashing in the taskbar
    instead of frontmost — which is exactly what a bare SetForegroundWindow
    does from here, and it cost a run that reported focused=True on its first
    line and then could not open the spawner panel.
    """
    import win32api
    import win32con
    import win32process

    hwnd = game_hwnd()
    if hwnd is None:
        return False

    # SetForegroundWindow on its own is refused: Windows only lets the process
    # that currently owns the foreground hand it over. Attaching our input
    # queue to the foreground thread borrows that right for the length of the
    # call, which is the standard way round it and needs no synthetic ALT
    # keypress — ALT is free-look in this game and would be seen by it.
    fg = win32gui.GetForegroundWindow()
    try:
        fg_tid = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    except Exception:
        fg_tid = 0
    my_tid = win32api.GetCurrentThreadId()
    attached = False
    try:
        if fg_tid and fg_tid != my_tid:
            win32process.AttachThreadInput(fg_tid, my_tid, True)
            attached = True
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        if attached:
            try:
                win32process.AttachThreadInput(fg_tid, my_tid, False)
            except Exception:
                pass
    time.sleep(settle_s)
    return game_focused()


# A run that keeps losing the foreground is not a run worth continuing: some
# other window is fighting for it, and every keypress in between went there.
MAX_REGAINS = 5


def ensure_focus(countdown_s=0, tries=3, label=''):
    """Take the foreground, verify it, and only then let the caller proceed.

    This is what makes a run unattended. Every tool here starts from a
    terminal, so at t=0 the terminal owns the foreground and the game does
    not — the guard fires and the run aborts having done nothing. The old
    answer was a countdown asking a human to alt-tab, which is precisely the
    human the harvest loop is supposed to remove.

    Windows may legitimately refuse to hand focus over, so this retries and
    then falls back to the countdown rather than pretending. Returns whether
    the game actually ended up frontmost — never assume it did.
    """
    if game_focused():
        return True
    for i in range(max(1, tries)):
        if raise_game():
            return True
        time.sleep(0.4)
    if countdown_s > 0:
        print(f"    [!] could not take focus{' for ' + label if label else ''}"
              f" — switch to the game within {countdown_s:.0f}s")
        for s in range(int(countdown_s), 0, -1):
            if game_focused():
                return True
            print(f"    starting in {s} ...", flush=True)
            time.sleep(1.0)
    return game_focused()


class FocusKeeper:
    """Re-takes the foreground mid-run, a bounded number of times.

    Losing focus mid-magazine is usually something else stealing it — an
    overlay, a notification — and taking it straight back is right. Losing it
    repeatedly is not: either something is contending, or a human is trying to
    get out. Both mean stop, so the regains are counted rather than infinite.

    To stop a run by hand, Ctrl-C the terminal. It will take focus back up to
    MAX_REGAINS times before giving up on its own.
    """

    def __init__(self, budget=MAX_REGAINS):
        self.budget = budget
        self.used = 0

    def ok(self, where=''):
        if game_focused():
            return True
        if self.used >= self.budget:
            print(f"    [!] focus lost again ({self.used} regains used) — "
                  f"stopping instead of fighting for it")
            return False
        self.used += 1
        print(f"    [!] lost focus{' at ' + where if where else ''} — "
              f"taking it back ({self.used}/{self.budget})")
        # Retried rather than attempted once: focus is most often lost *during*
        # a screen transition, and for a second or two around one the window is
        # not raisable at all. A single 0.8 s try lands inside that window and
        # calls a recoverable blip fatal — which is how leaving the training
        # range failed one click short of the lobby.
        if ensure_focus(tries=4):
            time.sleep(0.4)      # the game swallows the first frames after a
            return True          # foreground change; do not press into them
        print("    [!] could not get the foreground back")
        return False


_KEEPER = None


def focus_keeper():
    """Shared across the process, so the regain budget counts a *run* rather
    than one loop inside it. A tool that lost focus three times in its outer
    loop should not get five more inside the inner one."""
    global _KEEPER
    if _KEEPER is None:
        _KEEPER = FocusKeeper()
    return _KEEPER


def move_cursor(xy):
    """Absolute cursor placement. Used to park the cursor off a UI element
    before a screenshot — whatever is under it draws a hover highlight."""
    ctypes.windll.user32.SetCursorPos(int(xy[0]), int(xy[1]))


class Pointer:
    """Cursor placement + left click + drag."""

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
                print(f'[pointer] no Pico ({e}); falling back to SendInput')
        self.backend = 'pico' if self.pico else 'sendinput'
        print(f'[pointer] click backend = {self.backend}')

    def move_to(self, x, y):
        ctypes.windll.user32.SetCursorPos(int(x), int(y))

    def cursor_pos(self):
        pt = _POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def click_at(self, x, y, settle=MOVE_WAIT):
        self.move_to(x, y)
        time.sleep(settle)
        got = self.cursor_pos()
        if abs(got[0] - x) > 2 or abs(got[1] - y) > 2:
            print(f'[pointer] warning: cursor landed at {got}, wanted {(x, y)}')
        if self.pico:
            self.pico.click(0x01, 80)
            time.sleep(0.09)
        else:
            ctypes.windll.user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.06)
            ctypes.windll.user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

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
