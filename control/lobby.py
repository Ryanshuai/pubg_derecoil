"""Drive the game between not-running, the lobby and a match.

    from control.lobby import LobbyControl
    lc = LobbyControl()
    if lc.ensure_in_match(launch=True)['ok']:
        ...                       # a round is up and drivable

The eyes are detector/lobby_detector.py; this is the hands. The split is the
package rule: that module classifies a frame and can do it on a stored PNG,
this one clicks and re-reads until the game agrees.

THE PROCESS IS THE FIRST STATE ON THE SAME AXIS, not a separate concern.
Everything below used to begin by assuming there was a game to click at, and
the assumption failed in the worst available direction: with the game closed
the desktop is bright, the letterbox probe reads bar_max=251, and classify()
answers FULLBLEED — which every policy here treats as a loading screen, i.e.
"click nothing and wait". A dead game therefore burned the full 300 s
ENTER_TIMEOUT and then reported that the game was slow. Measured 2026-08-07,
straight off a desktop capture.

So `state()` asks the process and the window before it asks the screen, and
the three answers it can give there each have a DIFFERENT recovery — which is
the whole reason they are three states and not one "cannot read the screen":

    NOT_RUNNING  no process              launch it
    NO_WINDOW    process, no window      wait (starting or dying); a second
                                         launch just raises Steam's own
                                         "already running" dialog
    MINIMIZED    window, iconified       raise_game(), and it is drivable a
                                         second later

⚠ MINIMIZED was the same lesson one state further along, and it cost 420 s
before anyone looked: IsWindowVisible is TRUE for an iconified window, so
game_hwnd() hands back a window nobody can capture, and the grab comes back
with the desktop in it — bar_max 251, ping_frac 0.000, FULLBLEED, "loading,
wait it out". The rule above ("ask the process before the screen") was right
and one question short.

Nothing here sleeps for a fixed duration waiting on the game. Every wait is a
poll on LobbyDetector, because none of these durations are constant:

  results -> lobby   the screen says "exit to lobby in 18 seconds" and does
                     that on its own; clicking EXIT only shortens it.
  lobby -> match     matchmaking plus loading. Varies by a lot -- an empty
                     training range comes up quickly, a populated mode can
                     take minutes.

So the timeouts below are ceilings for giving up, NOT expected durations.
Every call returns the measured `elapsed` and the states it passed through;
`--stats` on the CLI prints them, which is how the real numbers get filled in
here rather than guessed.

    OBSERVED DURATIONS -- fill in from tools/probe_lobby_transition.py runs
      results -> lobby   : (not yet measured)
      not running -> lobby : 68.4 s, n=1, 2026-08-07 cold start after a kill.
                           The legs, because the whole point is that this one
                           passes through four states and each was a guess:
                             URL sent          0.0
                             process exists   17.2   <-- longer than
                                                     RETRY_AFTER, see
                                                     LAUNCH_RETRY_AFTER
                             window exists    46.5   (29.3 s of NO_WINDOW)
                             window drawn     64.9   ...MINIMIZED
                             restored         66.3
                             lobby            68.4
                           So LAUNCH_TIMEOUT=420 is ~6x the observed. Left
                           alone at n=1, but it is no longer unbacked.

                           SECOND OBSERVATION, 2026-08-08, cold start from a
                           desktop with Steam already up: 59.7 s, 2 actions.
                             URL sent          0.0
                             no_window         2.1   <-- 8x faster than the
                                                     17.2 above; Steam was
                                                     already running, which
                                                     the first sample did not
                                                     record either way
                             fullbleed        19.8
                             no_window        21.4   <-- goes BACK. The window
                                                     is destroyed and remade
                                                     during startup, so
                                                     "window exists" is not
                                                     monotonic and a state
                                                     machine that latches on
                                                     it will hang
                             fullbleed        37.6
                             minimized        56.0
                             restored         57.3
                             lobby            59.7
                           n=2 now: 59.7 and 68.4. Both ended in MINIMIZED
                           needing raise_game, so that leg is not a fluke of
                           the first run.
      lobby -> not running : 1.5 s, one terminate(), no kill() escalation
                           needed (3 processes).
      minimized -> lobby : 1.4 s, ONE action (raise_game), 2026-08-07.
                           Against the 420 s this used to spend before
                           MINIMIZED was a state — the fix is not a speedup,
                           it is the difference between recovering and not.
      in_game -> lobby   : 7.0 .. 23.0 s over 3 runs, training range,
                           ESC -> LEAVE -> CONFIRM. The fast run had ESC
                           landing in 0.6 s and the dialog 0.8 s later; the
                           slow ones spent it in fullbleed after CONFIRM.
      lobby -> in_game   : 12.2 .. 27.4 s over 4 runs, one PLAY click
                           (lobby -> fullbleed 3.4 s, the rest is loading).
                           The 2026-08-08 run landed on the fast end again
                           (3.2 s to fullbleed, 12.2 s total) and is the first
                           one measured WITH the teleport riding along: the
                           whole lobby -> standing-on-the-lane sequence was
                           12.2 + 6 (the failed first teleport) + 3 (settle)
                           + 2.4 = ~24 s. See RANGE_SETTLE_S for the 9 s in
                           the middle, which is a detector fault and not the
                           game being slow.
      PLAY -> 1st change : 3.4 s, consistent
      error -> lobby     : 0.8 s, one OK click on the inactivity dialog
      mode tab click     : landed first try in 3/3, both directions

    THE SPREAD IS THE POINT. A 3x range on the same transition, same map,
    same client, is why none of these may become a sleep. Note also that the
    fast exit ran exactly to MAX_RETRIES; one more step and it would have
    reported failure while working correctly. See _pump on why the budget is
    per-state.

    The fullbleed stretch above is the loading screen, the one state
    lobby_detector still has no sample of. It is reachable on demand: grab
    during that window.

Actions are fired at most once per cooldown rather than on every poll: holding
the state does not mean the action failed, it usually means the game is still
working on it. Re-pressing F every 500 ms would queue up menu input.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import psutil

from config import (LOBBY_ERROR_OK_XY, LOBBY_EXIT_XY, LOBBY_LEAVE_CONFIRM_XY,
                    LOBBY_MENU_LEAVE_XY, LOBBY_PARK_XY, LOBBY_PLAY_XY,
                    LOBBY_RECONNECT_XY)
from detector.lobby_detector import (LobbyDetector, LobbyState,
                                     error_dialog_visible, reconnect_visible,
                                     leave_confirm_visible,
                                     leave_entry_confirmed, is_results_screen)
from detector.lobby_nav import (SAFE_MODE, SUB_TABS, confident, read_mode,
                                read_page, tab_xy)
from capture.cropper import capture_screen
from press.pointer import move_cursor
from control.driver import Driver
from control.focus import (ensure_focus, focus_keeper, game_hwnd,
                           game_minimized, game_pids, raise_game)

POLL = 0.5              # how often the screen is sampled

# Ceilings, not expectations. See the module docstring.
EXIT_TIMEOUT = 90.0     # results screen -> lobby (it self-exits in ~18 s)
ENTER_TIMEOUT = 300.0   # lobby -> in a match, matchmaking included
RETRY_AFTER = 15.0      # a stuck state this long -> fire the action again

MAX_RETRIES = 3         # then give up rather than mash the menu forever

# ── Launching and quitting ───────────────────────────────────────────────

# From steamapps/appmanifest_578080.acf. The URL is what the library's PLAY
# button sends, so it starts Steam first if Steam is not up, and it needs no
# window to be visible anywhere.
#
# ⚠ THE PLAY BUTTON IS NOT CLICKED, ON PURPOSE, and the measurement is here so
# nobody has to take that on faith. Captured 2026-08-07 by its green fill:
# centre (881, 508) absolute, inside a Steam window at (491, 76, 2736, 1287),
# i.e. +(390, 432) from the window origin. THE ABSOLUTE NUMBER IS WORTHLESS —
# that window is user-movable, user-resizable, and can be minimised to the
# tray while Steam runs perfectly well. The offset is the only stable half,
# and using it would mean finding and raising the Steam window first, which is
# strictly more work than the URL and fails in more places. There is no case
# where clicking wins: the URL covers Steam-not-running too.
STEAM_URL = 'steam://rungameid/578080'

# A guess, and the only ceiling in this file that is not backed by a run. PUBG
# cold-starts through Steam, an anti-cheat launcher and a splash before the
# lobby draws. Record the real spread in OBSERVED DURATIONS.
LAUNCH_TIMEOUT = 420.0
QUIT_TIMEOUT = 60.0     # terminate -> the process is gone
QUIT_GRACE = 10.0       # ...after which SIGKILL rather than wait out the above

# ⚠ RETRY_AFTER IS 15 s AND STEAM TAKES 17.2 s TO PRODUCE A PROCESS, so the
# generic cadence re-fires the launch BEFORE the first one has shown any sign
# of working. Measured on the very first real run (2026-08-07): two URLs sent,
# 0.0 s and 15.2 s, and the process only appeared at 17.2 s. That is not an
# edge case, it is the normal path — the state stays NOT_RUNNING for longer
# than the retry window, every time.
#
# It survived (Steam ignored the second URL), but "the second URL raises
# Steam's own already-running dialog, which this repository cannot see or
# dismiss" is exactly what ensure_running's docstring says must not happen.
# The guard was on the STATE, and the state cannot move fast enough to be one.
#
# 60 s is 3.5x the observed latency and still leaves room for a genuine retry
# inside LAUNCH_TIMEOUT if Steam really did drop the URL.
LAUNCH_RETRY_AFTER = 60.0

# "The game is up and drawing a screen this file can name." Deliberately NOT
# "the lobby": launching can land on DISCONNECTED or a stale ERROR dialog, and
# ensure_running's job ends at "there is something to drive" — clearing those
# is ensure_in_match's, which already knows how. FULLBLEED is excluded because
# it is what the whole multi-minute startup looks like, so accepting it would
# make ensure_running return while the game is still loading.
SETTLED = (LobbyState.LOBBY, LobbyState.LOBBY_MENU, LobbyState.IN_GAME,
           LobbyState.MENU, LobbyState.DISCONNECTED, LobbyState.ERROR)

# Every policy that drives the SCREEN is fatal on this: no click, key or focus
# grab can reach a process that does not exist, so polling on is just a slower
# way to say so. See the module docstring for what it used to cost.
NOT_RUNNING_IS_FATAL = {
    LobbyState.NOT_RUNNING: 'the game is not running — nothing on screen can '
                            'be clicked into a match. ensure_running(), or '
                            'ensure_in_match(launch=True)',
}

PARK_SETTLE = 0.35      # hover highlights fade; see LOBBY_PARK_XY in config
MODE_SETTLE = 0.6       # the sub bar re-renders. Not a matchmaking wait.
MODE_TRIES = 3

# ── Where the character stands, which is part of ENTERING ────────────────
#
# THE TELEPORT IS BOUND TO THE ENTRY, NOT TO A CHECKLIST. Everyone spawns at
# the main compound, and on a populated server that compound has people
# driving through it: being rammed mid-magazine costs the magazine and DOES
# NOT ANNOUNCE ITSELF -- the recoil trace just has someone else's physics in
# it, and every gate downstream still passes. The 200m lane is off to one
# side.
#
# ⚠ IT USED TO BE A LEG OF control.session.ensure_ready(), gated by a flag in
# THAT module, and the placement is what broke it. Only one of the three doors
# back into a match went through ensure_ready; the other two
# (calibration.range_session.AutoSession.enter -> LobbyControl directly, and a
# human re-entering) left the flag standing, so a later ensure_ready SKIPPED
# the teleport on a belief the re-entry had just falsified. A harvest evicted
# at 17 minutes came back at the spawn compound and fired the whole back half
# of a 45-minute run there. The repair at the time was a public forget_range()
# that every such caller had to remember to call -- i.e. the same bug with a
# manual step in front of it.
#
# Here, the module that MOVES the character is the module that knows it moved.
# There is nothing to remember and no second door: `ensure_in_match` is the
# only way into a match in this repository.
#
# ⚠ AND SINCE 2026-08-08 THERE IS NO FLAG AT ALL, not even a private one. The
# teleport fires on the entry event and never otherwise -- "already in a match"
# now MEANS "already placed", by the operator's rule. That drops the case the
# flag was there for (this process inherits a match somebody else entered), and
# the trade is deliberate: the check cost a map open/read/close on every fresh
# process, while the mover it guarded against is one this process never sees.
# Whoever DOES see such an entry teleports at that moment -- the same rule at
# the same event (calibration/range_session.py:ManualSession._place_on_lane).
RANGE_SETTLE_S = 3.0    # after a lobby->match transition, before RE-trying a
                        # teleport that just failed. Not on the happy path.
                        #
                        # ⚠ NOT MEASURED, and the reason it exists was RE-READ
                        # WRONG for a day. 2026-08-07 an unattended run clicked
                        # the 200m box four times with the player marker never
                        # moving, and the identical call issued by hand a
                        # minute later landed on the first attempt; that was
                        # written up as "after a transition the game drops the
                        # first inputs", the same shape as FOCUS_SETTLE_S.
                        #
                        # 2026-08-08, watching it happen with the log open, it
                        # is not that. `ensure_map(True)` sent NO M AT ALL --
                        # map_open() answered True on its first capture, on a
                        # game that had reached in_game 0.0 s earlier and had
                        # nobody press M. player_xy() was None in the same
                        # frame, so the signal that fired was the left panel's
                        # yellow selection border, not the marker. The four
                        # clicks went into the WORLD. Nine seconds later both
                        # halves read consistently and the same call worked.
                        #
                        # So the retry is recovering from a DETECTOR that lies
                        # for the first seconds of a match, not from input
                        # being dropped -- and this constant is a band-aid over
                        # that, honestly labelled. Cause not established: the
                        # spawn compound is full of yellow practice structures
                        # and the left strip is 500 px of whatever the camera
                        # happens to face, and there is a fade-in. Settling it
                        # is one probe: exit_to_lobby, re-enter, and sample
                        # panel_visible / player_xy / the yellow count every
                        # 200 ms across the transition. Measured RIGHT AFTER
                        # that run, in the same match with the map shut: 4
                        # loose yellow px against PANEL_YELLOW_MIN = 60.

# ⚠ THERE IS NO MODULE-LEVEL "WHERE IS THE CHARACTER" BELIEF HERE ANY MORE,
# and its absence is the design. `_PLACED` / `placed_at` / `forget_placement`
# were removed 2026-08-08 when the teleport was bound to the ENTRY EVENT alone
# (see _place_on_range). A flag exists to answer a question that is no longer
# asked: nothing decides anything from "has this process placed anyone yet".
#
# The two functions that read and cleared it are gone rather than left unread —
# a belief nobody acts on still gets read as authoritative by the next person
# to find it, and this one had already cost a 45-minute harvest once.


class LobbyControl(Driver):
    """Lobby <-> match transitions, driven by polling the detector."""

    def __init__(self, verbose=True):
        super().__init__()
        self.det = LobbyDetector()
        self.verbose = verbose

    def _log(self, msg):
        if self.verbose:
            print(f'[lobby] {msg}', flush=True)

    def close(self):
        super().close()
        self.det.close()


    # ── Reading ──

    def state(self):
        """Where the game is, process included. -> LobbyState

        THE PROCESS IS ASKED FIRST, and the order is not a style choice: the
        pixel classifier has an answer for the desktop and it is a confident,
        well-formed, wrong one (FULLBLEED — see the module docstring). A probe
        that cannot say "there is nothing here" has to be fenced off by one
        that can.

        Cheap questions first (measured 2026-08-07: 1.4 ms for the window,
        18.7 ms for the process table over 603 processes), and the expensive
        one only runs in the branch where the cheap one said no:

            window? -> no, process?   starting or dying; NO_WINDOW, wait
            window? -> no, no process NOT_RUNNING; launch it
            window? -> yes, iconic?   MINIMIZED; raise it
            otherwise                 the game is up; read the screen

        ⚠ MINIMIZED IS ASKED HERE AND NOT IN ONE POLICY'S act(), which is
        where it first landed. Every screen-reading probe in this repository
        answers for a minimized game — and answers about the DESKTOP, because
        IsWindowVisible is true for an iconified window, so game_hwnd() hands
        back a window nobody can capture. A bright wallpaper reads bar_max 251
        / ping_frac 0.000 -> FULLBLEED -> "loading, wait it out": measured
        2026-08-07 as 420 s spent on a game that was drivable in seconds.
        Fixing it inside `ensure_running` left the other three policies, and
        this method itself, still reporting the desktop as a loading screen.

        ⚠ IT REPORTS, IT DOES NOT RESTORE. raise_game() steals the foreground,
        and a reader that moves the window it was asked to describe is the
        cached-state failure control/CLAUDE.md bans. The act() branches below
        do the restoring; this says what is true.
        """
        if game_hwnd() is None:
            return (LobbyState.NO_WINDOW if game_pids()
                    else LobbyState.NOT_RUNNING)
        if game_minimized():
            return LobbyState.MINIMIZED
        return self.det.state()

    # ── Actions ──

    # ── Mode selection ──

    def _grab_parked(self, settle=PARK_SETTLE):
        """Full-screen grey frame with the cursor off the tab bars.

        Parking is mandatory, not tidiness: a hovered tab lights to the same
        brightness the selected one does. Reading a bar without parking makes
        wherever the mouse happens to rest look selected — and the read-back
        that verifies a click is precisely when the cursor is sitting on the
        tab just clicked.
        """
        move_cursor(LOBBY_PARK_XY)
        time.sleep(settle)
        return cv2.cvtColor(capture_screen(), cv2.COLOR_BGR2GRAY)

    def mode(self, gray=None):
        """What the lobby would start right now. -> (mode|None, margin).

        None when the sub bar is not readable, which covers both "not on the
        PLAY page" and "a dialog is over the bar". Never guesses.
        """
        if gray is None:
            gray = self._grab_parked()
        page, page_margin, _ = read_page(gray)
        if not confident(page, page_margin) or page != 'PLAY':
            return None, 0.0
        name, margin, _ = read_mode(gray)
        return (name if confident(name, margin) else None), margin

    def ensure_mode(self, want=SAFE_MODE, tries=MODE_TRIES):
        """Select a mode tab and prove it took. -> {'ok', 'mode', 'error'}

        Clicking is verified by reading the bar back, because a click that
        silently missed looks exactly like one that worked — the sub bar
        simply stays where it was.
        """
        if want not in SUB_TABS:
            return {'ok': False, 'mode': None,
                    'error': f'{want} is not a mode tab; expected {SUB_TABS}'}

        for attempt in range(1, tries + 1):
            gray = self._grab_parked()
            page, page_margin, _ = read_page(gray)
            if not confident(page, page_margin):
                return {'ok': False, 'mode': None,
                        'error': 'top bar unreadable — a dialog may be over '
                                 'it, or this is not the lobby'}
            if page != 'PLAY':
                return {'ok': False, 'mode': None,
                        'error': f'on the {page} page, not PLAY — no mode bar'}

            name, margin, labels = read_mode(gray)
            if not confident(name, margin):
                return {'ok': False, 'mode': None,
                        'error': f'mode bar unreadable (margin {margin:.1f}x)'}
            if name == want:
                return {'ok': True, 'mode': name, 'error': None}

            xy = tab_xy(labels, want)
            if xy is None:
                return {'ok': False, 'mode': name,
                        'error': f'{want} is not on the bar'}
            self._log(f'mode is {name}, clicking {want} at {xy} '
                      f'(attempt {attempt})')
            self.pointer.click_at(*xy)
            time.sleep(MODE_SETTLE)

        name, margin = self.mode()
        return {'ok': False, 'mode': name,
                'error': f'{tries} clicks did not move the mode bar off '
                         f'{name}'}

    # ── Actions ──

    def press_play(self, require_mode=SAFE_MODE):
        """Start a match by clicking the PLAY button.

        This used to press F, because the button draws an "F" hint and a
        keypress is immune to wherever the cursor happens to be. That reading
        of the screenshot was wrong: three F presses in a row, with the game
        verified frontmost, moved the lobby not at all. The hint is decoration.

        Cursor placement is exactly what has to be handled rather than avoided
        — the lobby has a real cursor and it is wherever it was left, which is
        usually not over PLAY. click_at moves it there first.

        THE BUTTON STARTS WHATEVER THE SUB BAR HAS SELECTED. It is one
        button for every mode, so on NORMAL this enters a real match — which
        per detector/CLAUDE.md cannot currently be left, since only the
        training range's ESC menu has been captured and leave_entry_confirmed()
        refuses to click LEAVE anywhere else. An unattended run that lands
        there is stuck. Hence the gate: the mode is selected and read back
        before the click, and an unverified mode means no click at all.

        Pass require_mode=None to click regardless — that is a deliberate
        choice to enter whatever is selected, not a default.
        """
        if require_mode:
            rec = self.ensure_mode(require_mode)
            if not rec['ok']:
                # ⚠ None, NOT a label. _pump counts any truthy label as an
                # attempt, so returning the sentence here made three refusals
                # report "3 attempts had no effect, stuck in lobby" with not
                # one click sent — a timing word for a state problem, which
                # is the exact failure _pump's own comment warns about.
                print(f'      [lobby] REFUSED to click PLAY: wanted '
                      f'{require_mode}, {rec["error"]}', flush=True)
                return None
        self.pointer.click_at(*LOBBY_PLAY_XY)
        return f'click PLAY {LOBBY_PLAY_XY}'

    def click_exit(self):
        self.pointer.click_at(*LOBBY_EXIT_XY)
        return f'click EXIT {LOBBY_EXIT_XY}'

    def press_esc(self):
        """Close (or open) the system menu.

        ⚠ THE `pico is None -> return None` GUARD IS GONE (2026-08-08), and so
        is the "Pico only" caveat it enforced: there is no second backend to be
        only-Pico against. A Pointer that exists has a device, so the guard was
        unreachable and the None it promised could never be returned.
        """
        from press.pico_mouse import HID_KEY_ESC
        self.pointer.pico.key(HID_KEY_ESC, 60)
        return 'ESC'

    def click_leave(self):
        """Click LEAVE TRAINING, but only after confirming that is what is
        under the cursor.

        EXIT TO DESKTOP is one 85 px pitch below it. Clicking this coordinate
        without the check would quit the game on any menu whose entries are
        ordered differently — which includes every real match, since none has
        been captured.
        """
        if not leave_entry_confirmed():
            self._log('LEAVE TRAINING is not at its expected position — '
                      'refusing to click (EXIT TO DESKTOP is one row below)')
            return None
        self.pointer.click_at(*LOBBY_MENU_LEAVE_XY)
        return f'click LEAVE TRAINING {LOBBY_MENU_LEAVE_XY}'

    def launch(self):
        """Ask Steam to start the game. Returns a label to log, or None.

        Fire-and-verify, like every other action here: this hands the request
        to Steam and returns immediately — Steam forks, so there is no pid to
        hold on to and no exit code that means "the game came up". The proof
        is `ensure_running` polling the process table and then the screen,
        which is the same proof that covers a hand-launched game.

        ⚠ GUARDED ON THE PROCESS TABLE BY ITS ONLY CALLER, not here. Sending
        the URL twice does not start two games — Steam refuses — but it does
        raise a "game already running" dialog over everything, and that dialog
        belongs to Steam, so nothing in this repository can see or dismiss it.
        `ensure_running` only fires this from NOT_RUNNING, never NO_WINDOW.
        """
        try:
            os.startfile(STEAM_URL)
        except OSError as exc:
            # Loud, not swallowed: no handler for steam:// means Steam is not
            # installed for this user, and every retry below would fail the
            # same way while the pump reported a timeout.
            self._log(f'could not hand {STEAM_URL} to Steam: {exc}')
            return None
        return f'launch {STEAM_URL}'

    def restore_window(self):
        """Un-iconify the game so the screen probes read the game again.

        Every policy here needs this branch, not just the one that launches:
        a firmware flash minimises the game (control/CLAUDE.md), and so does
        anything that takes the foreground on another monitor. The state it
        produces is indistinguishable from a loading screen to all six pixel
        probes, so without this the pump waits out its whole ceiling.

        raise_game() already does IsIconic -> SW_RESTORE and reports whether
        focus actually landed; the label is returned either way, because the
        restore is what _pump is counting attempts of and Windows refusing the
        FOREGROUND half does not mean the window is still iconified.
        """
        raise_game()
        return 'restore the minimized window (the screen being read is the ' \
               'desktop, not the game)'

    def quit_game(self, timeout=QUIT_TIMEOUT, grace=QUIT_GRACE):
        """L1 — Close the game. -> the same rec shape as the pumps.

        TERMINATES THE PROCESS; it does not click EXIT TO DESKTOP. The menu
        route was considered and dropped, for a reason that is the whole point
        of having this function: the screens where you most need to close the
        game are the ones with no working menu — a hung loading screen, a
        modal Steam dialog, an ESC menu this repository cannot read. A quit
        that only works when the game is healthy is not a recovery.

        The other half is that EXIT TO DESKTOP is the one menu entry that
        exists on BOTH captured menus at two different y (634 in a match, 949
        in the lobby — see config.py), so the blind-click failure mode is
        "quit the game" pointing at a menu that is not the one you think.
        Here that is the goal, which makes it the one place a wrong click
        would not announce itself.

        Escalates rather than waits: terminate(), and if the process is still
        there after `grace`, kill(). PUBG's anti-cheat can sit on a clean
        shutdown, and the caller asked for the game to be gone.
        """
        t0 = time.perf_counter()
        pids = game_pids()
        if not pids:
            return {'ok': True, 'elapsed': 0.0, 'states': ['not_running'],
                    'retries': 0, 'actions': 0, 'error': None}

        self._log(f'terminating {len(pids)} game process(es): {pids}')
        procs = []
        for pid in pids:
            try:
                p = psutil.Process(pid)
                p.terminate()
                procs.append(p)
            except psutil.Error:
                pass          # already gone is the outcome we are asking for

        killed = False
        while True:
            elapsed = time.perf_counter() - t0
            if not game_pids():
                return {'ok': True, 'elapsed': elapsed,
                        'states': ['not_running'], 'retries': 0,
                        'actions': 2 if killed else 1, 'error': None}
            if elapsed > timeout:
                return {'ok': False, 'elapsed': elapsed, 'states': [],
                        'retries': 0, 'actions': 2 if killed else 1,
                        'error': f'quit_game: still alive after '
                                 f'{timeout:.0f}s (pids {game_pids()})'}
            if elapsed >= grace and not killed:
                killed = True
                self._log(f'{elapsed:6.1f}s  >>> still alive, escalating to '
                          f'kill()')
                for p in procs:
                    try:
                        p.kill()
                    except psutil.Error:
                        pass
            time.sleep(POLL)

    def click_reconnect(self):
        """Rejoin after the server drops the session.

        Not the same screen as the inactivity dialog, and not the same click.
        This one is black with a RECONNECT button; that one is a dialog over
        the lobby with OK. Pressing PLAY at this screen does nothing, which is
        how it cost three retries and thirty seconds before it had a state.
        """
        if not reconnect_visible():
            return None
        self.pointer.click_at(*LOBBY_RECONNECT_XY)
        return f'click RECONNECT {LOBBY_RECONNECT_XY}'

    def dismiss_error(self):
        """Clear a modal ERROR dialog. Most often the inactivity logout, which
        drops the session and then blocks every recovery behind itself — an
        unattended campaign that idles once never gets going again."""
        if not error_dialog_visible():
            return None
        self.pointer.click_at(*LOBBY_ERROR_OK_XY)
        return f'click OK on an ERROR dialog {LOBBY_ERROR_OK_XY}'

    def click_leave_confirm(self):
        """Answer the "Do you want to exit training?" dialog.

        Gated on the dialog's title being on screen, for the same reason
        click_leave is: CANCEL sits 287 px to the right of CONFIRM, and this
        coordinate means nothing on any other screen.
        """
        if not leave_confirm_visible():
            return None
        self.pointer.click_at(*LOBBY_LEAVE_CONFIRM_XY)
        return f'click CONFIRM {LOBBY_LEAVE_CONFIRM_XY}'

    # ── The polling core ──

    def _pump(self, target, timeout, act, tag, fatal=None, need_focus=True):
        """Poll until `target`, firing `act` when the state stalls.

        `target` is one state or a tuple of them; `rec['state']` says which
        one arrived. A tuple is for policies whose goal is a CLASS of screen
        rather than one screen — `ensure_running` wants "the game is up and
        drawing something nameable" (SETTLED), and enumerating that is more
        honest than picking LOBBY and calling everything else a timeout.

        `act(state)` returns a label to log, or None to do nothing for that
        state. It is called at most once per RETRY_AFTER so a slow transition
        is not mistaken for a lost keypress.

        `fatal` is {state: why} — states where waiting cannot help, returned
        immediately instead of at the ceiling. The one that motivated it is
        NOT_RUNNING: no click, key or focus grab reaches a process that does
        not exist, and the old behaviour was to poll a desktop for 300 s and
        then blame the game for being slow.

        `need_focus=False` is for the pumps that run while there is nothing to
        focus. The foreground check below is a guard on DRIVING the game, and
        `ensure_running` spends most of its time before the game has a window
        at all — requiring it there would fail every launch on the first poll.

        ⚠ **`exit_to_lobby`, `enter_match` and `ensure_in_match` ARE the merged
        form — do not fold them further.** A duplicate-intent scan pairs the
        three of them on 15 shared rare tokens (`_pump`, `click_exit`,
        `click_reconnect`, `dismiss_error`, `press_esc`, `press_play`, the
        LobbyState members), and that overlap is the design, not a smell: this
        method is the machine and each of the three is a POLICY over it, six
        lines long, differing only in which state gets which click and what
        counts as arrival. Anything that unified the three `act` closures would
        have to take the state->action table as an argument, i.e. would
        reinvent the closures with worse names.
        """
        targets = target if isinstance(target, tuple) else (target,)
        fatal = fatal or {}
        t0 = time.perf_counter()
        # Two counters, because they answer different questions. `retries` is
        # ineffective attempts in the CURRENT state and drives the give-up
        # test; `actions` is everything fired all run and is what the caller
        # reports. Collapsing them made a successful three-step exit read as
        # "0 actions".
        seq, acted_at, retries, actions = [], -1e9, 0, 0
        last = None

        while True:
            elapsed = time.perf_counter() - t0
            state = self.state()
            if state is not last:
                seq.append(state.value)
                self._log(f'{elapsed:6.1f}s  {last.value if last else "-"} '
                          f'-> {state.value}')
                last = state
                acted_at = -1e9      # a real transition re-arms the action
                # ...and clears the budget, because MAX_RETRIES counts
                # *ineffective* attempts. It used to be a total across the
                # whole pump, so a chain that worked still exhausted it: an
                # inactivity dialog cleared on the first click (error ->
                # lobby) spent one third of the budget, leaving two attempts
                # to get from the lobby into a match, and the run gave up
                # while the loading screen was still loading. The error
                # message says "stuck in <state>", so the count has to mean
                # what that says.
                retries = 0

            if state in targets:
                return {'ok': True, 'elapsed': elapsed, 'states': seq,
                        'state': state, 'retries': retries,
                        'actions': actions, 'error': None}

            if state in fatal:
                return {'ok': False, 'elapsed': elapsed, 'states': seq,
                        'state': state, 'retries': retries,
                        'actions': actions,
                        'error': f'{tag}: {fatal[state]}'}

            if elapsed > timeout:
                return {'ok': False, 'elapsed': elapsed, 'states': seq,
                        'state': state, 'retries': retries,
                        'actions': actions,
                        'error': f'{tag}: still {state.value} after '
                                 f'{timeout:.0f}s'}

            # Losing the foreground here used to be fatal. It is recoverable:
            # take it back and carry on, since a keypress that went somewhere
            # else is exactly what the retry below is for. Bounded, so a run
            # that cannot hold focus still stops instead of spinning.
            if need_focus and not focus_keeper().ok(tag):
                return {'ok': False, 'elapsed': elapsed, 'states': seq,
                        'state': state, 'retries': retries,
                        'actions': actions,
                        'error': f'{tag}: lost the foreground and could not '
                                 f'take it back'}

            if elapsed - acted_at >= RETRY_AFTER:
                if retries >= MAX_RETRIES:
                    return {'ok': False, 'elapsed': elapsed, 'states': seq,
                            'retries': retries, 'actions': actions,
                            'error': f'{tag}: {MAX_RETRIES} attempts had no '
                                     f'effect, stuck in {state.value}'}
                label = act(state)
                if label:
                    acted_at = elapsed
                    retries += 1
                    actions += 1
                    self._log(f'{elapsed:6.1f}s  >>> {label} '
                              f'(attempt {retries})')

            time.sleep(POLL)

    # ── Transitions ──

    def ensure_running(self, timeout=LAUNCH_TIMEOUT):
        """L1 — Poll from "no game process" to a game that is up and drawing a
        nameable screen (SETTLED). -> rec['ok'], rec['state'].

        Idempotent and cheap when the game is already up: the first poll reads
        the state and returns, no launch, no click. That is what makes it safe
        to put in front of anything.

        ⚠ IT DOES NOT PROMISE THE LOBBY. It can land on DISCONNECTED or a
        stale ERROR dialog, which is a game that is up — clearing those is
        `ensure_in_match`'s job and it already knows how. Chain them, or use
        `ensure_in_match(launch=True)` which does.
        ⚠ IT DOES NOT PROMISE FOCUS either, and does not require it: for most
        of a cold start there is no window to focus. `ensure_focus()` after.

        NO_WINDOW deliberately fires nothing. A game with a process and no
        window is mid-startup or mid-shutdown, and the second Steam URL would
        raise Steam's own "already running" dialog over everything — a dialog
        this repository cannot see, let alone dismiss.

        ⚠ AND THAT STATE GUARD IS NOT ENOUGH ON ITS OWN, which the first real
        run proved: NOT_RUNNING outlives RETRY_AFTER (17.2 s to a process vs.
        a 15 s cadence), so the launch fires twice before the first one can
        possibly have shown an effect. Hence the second, action-local clock
        below — see LAUNCH_RETRY_AFTER. **An action whose effect takes longer
        to appear than the retry interval needs its own interval**; the state
        machine cannot express that, because the state is precisely what has
        not moved yet.
        """
        launched_at = [None]      # list so the closure can write to it

        def act(state):
            if state is LobbyState.NOT_RUNNING:
                now = time.perf_counter()
                if (launched_at[0] is not None
                        and now - launched_at[0] < LAUNCH_RETRY_AFTER):
                    return None   # in flight; Steam has not answered yet
                label = self.launch()
                if label:
                    launched_at[0] = now
                return label
            if state is LobbyState.MINIMIZED:
                return self.restore_window()
            return None          # NO_WINDOW / FULLBLEED: starting, just wait

        return self._pump(SETTLED, timeout, act, 'ensure_running',
                          need_focus=False)

    def restart_game(self, timeout=LAUNCH_TIMEOUT):
        """L2 — quit_game() then ensure_running(). For a game that is up but
        wedged: a loading screen that never resolves, a Steam-owned modal, an
        ESC menu on a screen with no matching template.

        Returns ensure_running's rec with the quit leg's elapsed folded in, so
        the caller reads one number for the whole restart.

        ⚠ IT DESTROYS EVERYTHING, further than exit_to_lobby does: the
        session, the rack, the backpack, the position, AND anything else that
        only existed in the running client. Nothing downstream is told.
        """
        gone = self.quit_game()
        if not gone['ok']:
            return gone
        rec = self.ensure_running(timeout)
        rec['elapsed'] += gone['elapsed']
        rec['states'] = gone['states'] + rec['states']
        return rec

    def exit_to_lobby(self, timeout=EXIT_TIMEOUT):
        """L1 — Poll out to the lobby from any in-match screen. Proved
        (LobbyState.LOBBY), retried on a budget — the mirror of enter_match,
        not a weaker thing.

        ⚠ IT DESTROYS THE SESSION. Rack, backpack and position do not
        survive the next entry, so this is a restart, not a reset.
        ⚠ A LEAVE that fails its glyph check returns None, which _pump does
        not count — the step then spins to the 90 s ceiling, not fast-fail.

        Clicking EXIT on the results screen is an accelerator only — it
        returns on its own — so a missed template costs ~18 s, never
        correctness. Leaving a live round is the opposite: it needs the ESC
        menu, and the LEAVE entry is click-gated on its own glyphs.
        """
        def act(state):
            # Asked before the state, not inside a branch: the confirm dialog
            # dims the ping overlay enough to classify as FULLBLEED, which is
            # also what a loading screen looks like — and the two want
            # opposite treatment (click it vs. touch nothing).
            # Before any of the probes below, because they all read a screen
            # that is the desktop while this is true.
            if state is LobbyState.MINIMIZED:
                return self.restore_window()
            done = (self.click_reconnect() or self.dismiss_error()
                    or self.click_leave_confirm())
            if done:
                return done
            if state is LobbyState.FULLBLEED and is_results_screen():
                return self.click_exit()
            if state is LobbyState.IN_GAME:
                return self.press_esc()      # open the menu
            if state is LobbyState.MENU:
                return self.click_leave()
            if state is LobbyState.LOBBY_MENU:
                # Already out of the round; the menu is just sitting over the
                # target. ESC closes it. NOT click_leave(): its glyph gate
                # scores 0.128 here (the fourth entry reads RESTART LOBBY, not
                # LEAVE TRAINING) and correctly refuses, which before this
                # branch existed meant spinning to the 90 s ceiling one ESC
                # away from done.
                return self.press_esc()
            return None                      # loading: nothing safe to click

        return self._pump(LobbyState.LOBBY, timeout, act, 'exit_to_lobby',
                          fatal=NOT_RUNNING_IS_FATAL)

    def enter_match(self, timeout=ENTER_TIMEOUT):
        """L1 (partial) — The lobby->match half of ensure_in_match. Same target,
        same proof (LobbyState.IN_GAME), same budget; the only difference is
        which screens it can START from.

        ⚠ IT DOES NOT PLACE THE CHARACTER, and that is now the bigger gap
        between the two. It returns with the character at the SPAWN COMPOUND,
        where a recoil trace can pick up someone else's physics without
        saying so. Its one caller is the `match` CLI action, reached by hand;
        anything unattended wants ensure_in_match.

        ⚠ NO RESULTS-SCREEN BRANCH. There it clicks nothing, polls the full
        ENTER_TIMEOUT and then blames the game.
        ⚠ press_play's mode REFUSAL is returned as a label, so _pump counts
        it as an attempt: three refusals read as "3 attempts had no effect".

        Clears a reconnect prompt or ERROR dialog first, for the same reason
        `ensure_in_match` does: both sit OVER the lobby, so the state still
        classifies as LOBBY and PLAY is sent — into a modal that swallows it.
        The symptom is not an error, it is this pump retrying a click that
        cannot land until ENTER_TIMEOUT runs out.

        This was the one of the three policies without that preamble
        (2026-08-06). Its only caller is the `match` CLI action, which is
        reached by hand precisely when something has already gone wrong — a
        firmware flash drops the game to the lobby, sometimes with a dialog up
        — so the missing clear was worst exactly where it was most likely.
        """
        def act(state):
            if state is LobbyState.MINIMIZED:
                return self.restore_window()
            done = self.click_reconnect() or self.dismiss_error()
            if done:
                return done
            if state is LobbyState.LOBBY:
                return self.press_play()
            if state in (LobbyState.MENU, LobbyState.LOBBY_MENU):
                return self.press_esc()      # close it, then PLAY is reachable
            return None

        return self._pump(LobbyState.IN_GAME, timeout, act, 'enter_match',
                          fatal=NOT_RUNNING_IS_FATAL)

    def _place_on_range(self, name, entered):
        """Teleport to a practice range, IF AND ONLY IF this call is what
        walked into it. -> rec, with 'skipped' set when nothing was driven.

        **THE TELEPORT IS BOUND TO THE ENTRY EVENT, AND TO NOTHING ELSE.**
        Entering resets the world and puts the character at the spawn, so the
        one moment the position is known to be wrong is the moment we arrive.
        Every other call finds a match already running and leaves it alone:
        being in the training range IS being placed, as far as this module is
        concerned.

        That is the operator's rule, stated 2026-08-08: 「每次进训练场的时候做
        一次那个地图的那个切换就行了，其他过程中不需要切换」. It is a rule about
        WHEN to drive, not a belief about where anyone is standing, which is
        why there is no flag behind it any more -- see the note where `_PLACED`
        used to be.

        ⚠ WHAT IT GIVES UP, PLAINLY: a process that attaches to a match
        somebody else entered does not teleport, so if the character is
        standing in the spawn compound it stays there. The previous version
        spent one map open/read/close per fresh process to rule that out. The
        cost of the check was paid on every run; the case it caught happens
        when a mover outside this process walked in -- and the one such mover
        this repository knows about (a human, via
        calibration.range_session.ManualSession) now teleports at the point it
        observes the entry, which is the same rule applied at the same event.

        ⚠ M IS A KEYPRESS, AND Tab AND THE SPAWNER PANEL BOTH SWALLOW
        KEYPRESSES (docs/game_quirks.md). That is the hazard control/map.py's
        docstring is about, and the reason the teleport once lived in
        session.ensure_ready() -- BEHIND the two legs that put those screens
        down. It is safe on this path for a reason that is a fact about the
        game rather than an ordering promise: a match that just finished
        LOADING has no modal screen up. Not "we closed them" -- THEY CANNOT BE
        OPEN. Binding the teleport to the entry event is what makes that the
        ONLY path, so the caveat the old `already in` branch carried (modals
        can be up there, and this does not check) no longer applies to
        anything.
        """
        if not entered:
            return {'ok': True, 'skipped': f'already in the match — the {name} '
                                           f'teleport goes with walking in',
                    'player': None, 'elapsed': 0.0, 'error': None}

        from control.map import MapControl
        with MapControl(verbose=self.verbose) as mc:
            got = mc.goto_range(name)
            if not got['ok']:
                # ⚠ UNCONDITIONAL, and it was made so on 2026-08-08 after a
                # version that only retried when the map had just been entered
                # -- reasoning about the CAUSE (a match too fresh to take
                # input) instead of the SYMPTOM. Measured the same day: two
                # runs hit the map_open false positive, got no retry, and
                # failed hard, while the run after each succeeded first try.
                #
                # Now that the teleport only ever runs on the entry path, the
                # branch it was gated on is gone -- but the retry stays
                # unconditional, because a first-seconds transient is exactly
                # what this path is made of.
                self._log(f'the {name} teleport did not land — letting the '
                          f'match settle and trying once more')
                time.sleep(RANGE_SETTLE_S)
                got = mc.goto_range(name)

        return got

    def ensure_in_match(self, timeout=ENTER_TIMEOUT, launch=False,
                        range_name='200m'):
        """L1 — Poll from any screen into a running match AND onto the practice
        lane. -> rec['ok'], rec['range'].

        Arrival is proved twice, by two different readbacks: LobbyState.IN_GAME
        for the match, and the PLAYER MARKER on the map for the position.

        `launch=True` extends "any screen" to "no game at all": ensure_running
        first, then the usual pump. Opt-in and defaulted OFF because starting
        the game is a several-MINUTE side effect on top of a call whose
        contract reads like seconds, and because an interactive caller who
        closed the game usually meant it. An unattended campaign wants it on.
        Without it, a dead game is reported as one — see NOT_RUNNING_IS_FATAL.

        `range_name=None` skips the teleport, for the two callers that mean it:
        control/map.py's own CLI (about to drive the map by hand) and anything
        probing the lobby transition itself.

        ⚠ IF IT HAD TO WALK BACK IN, THE REST OF THE WORLD RESET TOO — empty
        rack, empty backpack. Those are still yours to redo; nothing here
        measures them. The POSITION is the one that used to be in that list
        and is not any more, because it is the one whose loss was silent.

        One pump handles the whole chain: the results screen gets EXIT, the
        lobby gets F, an open ESC menu gets closed, loading gets waited out.
        Retries re-arm on every real state change, so a long matchmaking wait
        does not burn the budget.

        Note MENU is dismissed rather than used to leave: the caller asked to
        be *in* a match, and the round behind the menu already is one.
        """
        if launch and not self.state().running:
            rec = self.ensure_running()
            if not rec['ok']:
                return rec

        def act(state):
            if state is LobbyState.MINIMIZED:
                return self.restore_window()
            # A dropped session and an ERROR dialog both sit over the lobby
            # and swallow the PLAY click, so both are cleared first.
            done = self.click_reconnect() or self.dismiss_error()
            if done:
                return done
            if state is LobbyState.LOBBY:
                return self.press_play()
            if state in (LobbyState.MENU, LobbyState.LOBBY_MENU):
                # LOBBY_MENU belongs on this line for the same reason the
                # other two clears above it do: it sits OVER the lobby, the
                # letterbox still reads 0, and before it had a state of its
                # own this pump read LOBBY and fired PLAY into a modal that
                # swallowed it — every 15 s, to the 300 s ceiling.
                return self.press_esc()
            if state is LobbyState.FULLBLEED and is_results_screen():
                return self.click_exit()
            return None

        rec = self._pump(LobbyState.IN_GAME, timeout, act, 'ensure_in_match',
                         fatal=NOT_RUNNING_IS_FATAL)
        # ⚠ THE FIRST OBSERVED STATE, not `actions` and not `elapsed`. _pump
        # appends every state it passes through, starting with the one it found
        # — so states[0] != in_game is precisely "this call walked in", which
        # is precisely "the character is at the spawn". `actions > 0` is a
        # weaker spelling of the same thing that answers wrong on the path
        # where a modal was cleared over a match that was already running.
        #
        # ⚠ IT IS NOW THE WHOLE DECISION, not one half of it. _place_on_range
        # used to consult a process-level flag as well; since 2026-08-08 this
        # line IS the teleport rule, so getting it wrong no longer costs one
        # extra map toggle — it costs the teleport.
        entered = not rec['states'] or rec['states'][0] != LobbyState.IN_GAME.value
        # ⚠ REPORTED, because it is not only the teleport rule. A caller that
        # is MEASURING needs to know the match it is in is not the match it
        # started in: every constant that drifts per session drifted, and a
        # reading taken before the walk-back cannot be paired with one taken
        # after. 2026-08-08 paid for this once -- a K reading was taken, the
        # game fell back to the lobby, and pairing the two would have been the
        # exact confound MODEL.md sec.4 spends a section removing.
        rec['entered'] = entered
        if not rec['ok'] or not range_name:
            return rec

        rec['range'] = self._place_on_range(range_name, entered)
        if not rec['range']['ok']:
            # The match is up and the position is wrong, which is worse than
            # not being in a match at all: every gate downstream passes and the
            # magazines are fired in traffic. So the whole call fails.
            rec['ok'] = False
            rec['error'] = rec['range']['error']
        return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=('state', 'lobby', 'match', 'ensure',
                                       'mode', 'launch', 'quit', 'restart'),
                    help='state: print and exit. lobby: get to the lobby. '
                         'match: lobby -> match. ensure: anything -> match. '
                         'mode: select the --mode tab and verify it took. '
                         'launch: start the game and wait for a screen. '
                         'quit: terminate it. restart: quit then launch.')
    ap.add_argument('--launch', action='store_true',
                    help='for `ensure`: start the game first if it is not '
                         'running (several minutes; off by default).')
    ap.add_argument('--mode', default=SAFE_MODE, choices=SUB_TABS,
                    help=f'mode tab to select (default {SAFE_MODE}). Every '
                         f'other value can start a match that cannot '
                         f'currently be left.')
    ap.add_argument('--range', default='200m',
                    help='for `ensure`: the practice range to place the '
                         'character on after entering. Pass an empty string '
                         'to skip the teleport — for probing the lobby '
                         'transition itself, where the extra map open/close '
                         'is part of what is being timed.')
    ap.add_argument('--timeout', type=float, default=None)
    ap.add_argument('--countdown', type=int, default=5)
    args = ap.parse_args()

    with LobbyControl() as lc:
        if args.action == 'state':
            s = lc.state()
            extra = ''
            if s is LobbyState.ERROR:
                extra = ' (a modal dialog is up — OK must be clicked)'
            elif s is LobbyState.FULLBLEED and is_results_screen():
                extra = ' (results screen)'
            elif s is LobbyState.MENU:
                extra = (' (LEAVE TRAINING confirmed)'
                         if leave_entry_confirmed()
                         else ' (LEAVE entry NOT where expected)')
            elif s is LobbyState.LOBBY_MENU:
                extra = ' (ESC menu over the lobby — ESC closes it)'
            elif s is LobbyState.NO_WINDOW:
                extra = f' (pids {game_pids()}, no window yet)'
            elif s is LobbyState.MINIMIZED:
                extra = (' (iconified — every screen probe would be reading '
                         'the desktop; raise_game() fixes it)')
            print(f'{s.value}{extra}  playable={s.playable}')
            if s is LobbyState.LOBBY:
                m, margin = lc.mode()
                print(f'mode: {m or "unreadable"} (margin {margin:.0f}x)'
                      f'{"" if m == SAFE_MODE else "  <-- PLAY would NOT "
                        "start the training range"}')
            # Skipped when there is no game: both read the screen, and on a
            # desktop they report a confident FULLBLEED-shaped answer about a
            # game that is not there. That is the reading this file exists to
            # stop being taken at face value.
            if s.running:
                print(f'probes: {lc.det.probes()}')
                ok, msg = lc.det.selftest()
                print(f'selftest: {"ok" if ok else "PROBLEM"} — {msg}')
            return 0

        # The process actions run BEFORE the focus gate, because there is
        # nothing to focus: `launch` is called precisely when the game is not
        # up, and `quit`/`restart` are the recovery for a game that is up but
        # cannot be brought forward. Gating them on focus would make the three
        # actions unusable in the only situations that call for them.
        if args.action in ('launch', 'quit', 'restart'):
            fn = {'launch': lc.ensure_running, 'quit': lc.quit_game,
                  'restart': lc.restart_game}[args.action]
            rec = fn(**({'timeout': args.timeout} if args.timeout else {}))
        else:
            # ⚠ ABOVE THE FOCUS GATE, not folded into ensure_in_match's own
            # `launch=`. The gate below cannot pass while the game is closed,
            # so leaving the launch inside the call it guards would make
            # `--launch` unreachable in the one case it is for.
            if args.launch and not lc.state().running:
                rec = lc.ensure_running()
                if not rec['ok']:
                    print(f'\n{rec}')
                    return 1

            # ensure_focus, not a bare countdown: this CLI is what an
            # unattended campaign calls to recover, and a recovery step that
            # needs a human to click the game first is not a recovery. The
            # countdown survives only as ensure_focus's own fallback.
            if not ensure_focus(countdown_s=args.countdown,
                                label=args.action):
                print('[lobby] could not bring the game to the foreground')
                return 1

            if args.action == 'mode':
                rec = lc.ensure_mode(args.mode)
                print(f'\n{rec}')
                return 0 if rec['ok'] else 1

            fn = {'lobby': lc.exit_to_lobby, 'match': lc.enter_match,
                  'ensure': lc.ensure_in_match}[args.action]
            kw = {'timeout': args.timeout} if args.timeout else {}
            if args.action == 'ensure':
                kw['range_name'] = args.range or None
            rec = fn(**kw)

    print(f'\n{rec}')
    if rec.get('range'):
        r = rec['range']
        print(f'range: {r.get("skipped") or r.get("error") or "ok"}'
              f'  player={r.get("player")}')
    print(f'\nstates: {" -> ".join(rec["states"])}')
    print(f'elapsed: {rec["elapsed"]:.1f}s over {rec.get("actions", 0)} '
          f'action(s)')
    if rec['ok']:
        print('^ record this in the OBSERVED DURATIONS block at the top of '
              'this file')
    return 0 if rec['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
