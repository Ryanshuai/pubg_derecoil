"""Taking and holding the game's foreground window.

This is control, not press: it is a closed loop — take the foreground, read
back whether it worked, retry on a budget — and every step of it needs to know
the game exists. `press` only knows about devices.

It guards every other driver in this package: moving the mouse or sending a
key while the game is not frontmost types into whatever *is* frontmost, so
`ensure_focus()` comes before anything that drives the game, and
`focus_keeper()` re-checks during a long run.
"""
import time

import psutil
import win32gui
import win32process

GAME_EXES = ('tslgame',)     # PUBG ships as TslGame.exe


def window_info(hwnd):
    """Who owns this window. -> {'pid', 'exe', 'title'}, all filled on failure.

    THE one place that walks hwnd -> pid -> process name. There were four,
    and they differed only in **which of the three fields they threw away**:
    `_exe_of` kept the exe, `focus_trace._describe` wanted the pid too so it
    re-walked the whole chain, `focus_trace.list_windows` wanted the exe for
    filtering, and `calibration/state.py` wanted all three and grew a second
    `game_focused()` around them — same name as the one here, different return
    type. A caller that read one and used the other got a truthy 3-tuple where
    it expected a bool.

    `exe` is lowercased because every consumer compares it against GAME_EXES;
    `title` is not, because it is only ever printed.

    Never raises. A window can die between being enumerated and being asked
    about, and the answer to "who owns this now" is then genuinely nothing —
    but the shape stays, so callers do not each need a try block.
    """
    out = {'pid': 0, 'exe': '', 'title': ''}
    try:
        out['title'] = win32gui.GetWindowText(hwnd) or ''
    except Exception:
        pass
    # The `except` stays and the import does not. psutil.Process(pid) really
    # does raise for a process this one may not inspect (NoSuchProcess,
    # AccessDenied) and an unreadable window is a normal answer here — but a
    # MISSING psutil is a broken environment, and hiding that inside the same
    # handler makes every window on the machine report exe='' with no way to
    # tell "cannot inspect it" from "the library is not installed".
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        out['pid'] = pid
        out['exe'] = psutil.Process(pid).name().lower()
    except Exception:
        pass
    return out


def _exe_of(hwnd):
    return window_info(hwnd)['exe']


def _is_game(hwnd):
    exe = _exe_of(hwnd)
    return any(exe.startswith(k) for k in GAME_EXES)


def foreground():
    """What is frontmost right now. -> (is_the_game, exe, title)

    The diagnostic counterpart to `game_focused()`, which answers the same
    question with a bool. Both exist because the two questions are different:
    a guard wants "may I drive", a report wants "then what DID have focus".
    Before this, the report version lived in calibration/state.py under the
    name `game_focused` — see window_info on what that cost.
    """
    try:
        info = window_info(win32gui.GetForegroundWindow())
    except Exception:
        return False, '?', '?'
    return (any(info['exe'].startswith(k) for k in GAME_EXES),
            info['exe'] or '?', info['title'] or '?')


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


def game_minimized():
    """The game has a window and it is iconified. -> True / False / None.

    None means there is no window to ask about -- NOT "it is fine". The three
    answers are the three different situations control/CLAUDE.md's table
    already separates, and collapsing the last two is what this exists to stop.

    ⚠ `IsWindowVisible` IS TRUE FOR A MINIMIZED WINDOW. Only IsIconic (or the
    -32000 rect Windows parks it at) says so, which is why game_hwnd() happily
    returns a window nobody can screenshot. Everything downstream then reads
    the DESKTOP and classifies it: measured 2026-08-07, a minimized game gave
    bar_max 251 / ping_frac 0.000 -> FULLBLEED, and ensure_running sat in its
    poll loop for 420 s waiting for a "loading screen" to finish -- the exact
    failure control/CLAUDE.md documents for a game that is not running at all.
    The rule there ("ask the process before you ask the screen") was right and
    one state short: a minimized window is a process that is up and a screen
    that is not the game.
    """
    hwnd = game_hwnd()
    if not hwnd:
        return None
    try:
        if win32gui.IsIconic(hwnd):
            return True
        l, t, r, b = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    # Belt and braces: Windows parks minimized windows near -32000, and a
    # window whose rect is off every monitor cannot be captured either way.
    return l < -30000 or t < -30000


def game_pids():
    """Every live game process, by name. -> [pid, ...]

    The process-table counterpart to `game_hwnd()`, and the two disagree
    exactly where it matters: during startup and during shutdown the game HAS
    a process and does NOT yet (or no longer) have a drivable window. Reading
    only the window collapses those onto "not installed", and the recoveries
    are opposites — wait a few minutes vs. launch it.

    ⚠ ORDER MATTERS TO THE CALLER, not to correctness. Measured 2026-08-07:
    game_hwnd() 1.4 ms, this 18.7 ms over 603 processes. So the polling loop
    in control/lobby.py asks for the window first and only comes here when
    that answers None, which is the rare branch.

    Never raises: processes die between being listed and being named, and
    "it went away" is the answer, not an error.
    """
    out = []
    try:
        for p in psutil.process_iter(['pid', 'name']):
            name = (p.info.get('name') or '').lower()
            if any(name.startswith(k) for k in GAME_EXES):
                out.append(p.info['pid'])
    except Exception:
        pass
    return out


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

# The game ignores input for the first frames after a foreground change, so
# every caller has to wait after taking it. That is the kind of thing a caller
# forgets exactly once and then debugs as "the panel would not open", and
# refactor_plan lists it among the traps that caught its own author.
#
# It lived in 25 call sites and had drifted to five values (0.4 in
# FocusKeeper.ok, 0.5, 0.6, 0.7, 0.8 in various probes) with nothing measured
# behind any of them. 0.6 is the one control/CLAUDE.md documents and the one
# most sites used. It is now here and applied by ensure_focus itself.
FOCUS_SETTLE_S = 0.6


def _take_focus(countdown_s, tries, label):
    """The attempt itself, without the settle. -> bool"""
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


def ensure_focus(countdown_s=0, tries=3, label='', settle_s=FOCUS_SETTLE_S):
    """L1 — Take the foreground and prove it took, then settle. -> bool.
    Guarantees exactly one thing: the game's EXE is frontmost. It is the
    first of ensure_ready()'s five legs, never a substitute for them.

    ⚠ FRONTMOST IS NOT PLAYABLE. The lobby, the loading screen, the ESC menu
    and the results screen all satisfy this and all swallow input —
    probe_pitch_range drove three postures into the lobby on a True here.

    This is what makes a run unattended. Every tool here starts from a
    terminal, so at t=0 the terminal owns the foreground and the game does
    not — the guard fires and the run aborts having done nothing. The old
    answer was a countdown asking a human to alt-tab, which is precisely the
    human the harvest loop is supposed to remove.

    Windows may legitimately refuse to hand focus over, so this retries and
    then falls back to the countdown rather than pretending. Returns whether
    the game actually ended up frontmost — never assume it did.

    On a successful TAKE it sleeps settle_s first (see the constant). It does
    not sleep when the game was already frontmost: there was no foreground
    change, so there are no swallowed frames to wait out. Pass settle_s=0 to
    opt out — but the caller then owns the wait, which is the arrangement this
    replaced.
    """
    if game_focused():
        return True
    if not _take_focus(countdown_s, tries, label):
        return False
    if settle_s:
        time.sleep(settle_s)
    return True


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
            return True          # ensure_focus settles; see FOCUS_SETTLE_S
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
