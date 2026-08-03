"""Drive the game between the lobby and a match.

    from control.lobby import LobbyControl
    lc = LobbyControl()
    if lc.ensure_in_match()['ok']:
        ...                       # a round is up and drivable

The eyes are detector/lobby_detector.py; this is the hands. The split is the
package rule: that module classifies a frame and can do it on a stored PNG,
this one clicks and re-reads until the game agrees.

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
      in_game -> lobby   : 7.0 .. 23.0 s over 3 runs, training range,
                           ESC -> LEAVE -> CONFIRM. The fast run had ESC
                           landing in 0.6 s and the dialog 0.8 s later; the
                           slow ones spent it in fullbleed after CONFIRM.
      lobby -> in_game   : 12.2 .. 27.4 s over 3 runs, one PLAY click
                           (lobby -> fullbleed 3.4 s, the rest is loading)
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

from config import (LOBBY_ERROR_OK_XY, LOBBY_EXIT_XY, LOBBY_LEAVE_CONFIRM_XY,
                    LOBBY_MENU_LEAVE_XY, LOBBY_PARK_XY, LOBBY_PLAY_XY,
                    LOBBY_RECONNECT_XY)
from detector.lobby_detector import (LobbyDetector, LobbyState,
                                     error_dialog_visible, reconnect_visible,
                                     leave_confirm_visible,
                                     leave_entry_confirmed, is_results_screen)
from detector.lobby_nav import (SAFE_MODE, SUB_TABS, confident, read_mode,
                                read_page, tab_xy)
from detector.cropper import capture_screen
from press.pointer import Pointer, move_cursor
from control.focus import ensure_focus, focus_keeper

POLL = 0.5              # how often the screen is sampled

# Ceilings, not expectations. See the module docstring.
EXIT_TIMEOUT = 90.0     # results screen -> lobby (it self-exits in ~18 s)
ENTER_TIMEOUT = 300.0   # lobby -> in a match, matchmaking included
RETRY_AFTER = 15.0      # a stuck state this long -> fire the action again

MAX_RETRIES = 3         # then give up rather than mash the menu forever

PARK_SETTLE = 0.35      # hover highlights fade; see LOBBY_PARK_XY in config
MODE_SETTLE = 0.6       # the sub bar re-renders. Not a matchmaking wait.
MODE_TRIES = 3


class LobbyControl:
    """Lobby <-> match transitions, driven by polling the detector."""

    def __init__(self, backend='auto', verbose=True):
        self.det = LobbyDetector()
        self.verbose = verbose
        self._pointer = None
        self._backend = backend

    # Built lazily: constructing a Pointer opens the Pico, and a caller that
    # only ever reads state should not be holding the serial port that
    # harvest.py or robot.py may want.
    @property
    def pointer(self):
        if self._pointer is None:
            self._pointer = Pointer(self._backend)
        return self._pointer

    def _log(self, msg):
        if self.verbose:
            print(f'[lobby] {msg}', flush=True)

    def close(self):
        self.det.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── Reading ──

    def state(self):
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
                return (f'REFUSED to click PLAY: wanted {require_mode}, '
                        f'{rec["error"]}')
        self.pointer.click_at(*LOBBY_PLAY_XY)
        return f'click PLAY {LOBBY_PLAY_XY}'

    def click_exit(self):
        self.pointer.click_at(*LOBBY_EXIT_XY)
        return f'click EXIT {LOBBY_EXIT_XY}'

    def press_esc(self):
        """Close (or open) the system menu. Pico only — no key path without."""
        if self.pointer.pico is None:
            return None
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

    def _pump(self, target, timeout, act, tag):
        """Poll until `target`, firing `act` when the state stalls.

        `act(state)` returns a label to log, or None to do nothing for that
        state. It is called at most once per RETRY_AFTER so a slow transition
        is not mistaken for a lost keypress.
        """
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

            if state is target:
                return {'ok': True, 'elapsed': elapsed, 'states': seq,
                        'retries': retries, 'actions': actions, 'error': None}

            if elapsed > timeout:
                return {'ok': False, 'elapsed': elapsed, 'states': seq,
                        'retries': retries, 'actions': actions,
                        'error': f'{tag}: still {state.value} after '
                                 f'{timeout:.0f}s'}

            # Losing the foreground here used to be fatal. It is recoverable:
            # take it back and carry on, since a keypress that went somewhere
            # else is exactly what the retry below is for. Bounded, so a run
            # that cannot hold focus still stops instead of spinning.
            if not focus_keeper().ok(tag):
                return {'ok': False, 'elapsed': elapsed, 'states': seq,
                        'retries': retries, 'actions': actions,
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

    def exit_to_lobby(self, timeout=EXIT_TIMEOUT):
        """Get back to the lobby from wherever the match left us.

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
            return None                      # loading: nothing safe to click

        return self._pump(LobbyState.LOBBY, timeout, act, 'exit_to_lobby')

    def enter_match(self, timeout=ENTER_TIMEOUT):
        """From the lobby into a running match."""
        def act(state):
            if state is LobbyState.LOBBY:
                return self.press_play()
            if state is LobbyState.MENU:
                return self.press_esc()      # close it; the round is running
            return None

        return self._pump(LobbyState.IN_GAME, timeout, act, 'enter_match')

    def ensure_in_match(self, timeout=ENTER_TIMEOUT):
        """Whatever is on screen, end up in a running, drivable match.

        One pump handles the whole chain: the results screen gets EXIT, the
        lobby gets F, an open ESC menu gets closed, loading gets waited out.
        Retries re-arm on every real state change, so a long matchmaking wait
        does not burn the budget.

        Note MENU is dismissed rather than used to leave: the caller asked to
        be *in* a match, and the round behind the menu already is one.
        """
        def act(state):
            # A dropped session and an ERROR dialog both sit over the lobby
            # and swallow the PLAY click, so both are cleared first.
            done = self.click_reconnect() or self.dismiss_error()
            if done:
                return done
            if state is LobbyState.LOBBY:
                return self.press_play()
            if state is LobbyState.MENU:
                return self.press_esc()
            if state is LobbyState.FULLBLEED and is_results_screen():
                return self.click_exit()
            return None

        return self._pump(LobbyState.IN_GAME, timeout, act, 'ensure_in_match')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=('state', 'lobby', 'match', 'ensure',
                                       'mode'),
                    help='state: print and exit. lobby: get to the lobby. '
                         'match: lobby -> match. ensure: anything -> match. '
                         'mode: select the --mode tab and verify it took.')
    ap.add_argument('--mode', default=SAFE_MODE, choices=SUB_TABS,
                    help=f'mode tab to select (default {SAFE_MODE}). Every '
                         f'other value can start a match that cannot '
                         f'currently be left.')
    ap.add_argument('--timeout', type=float, default=None)
    ap.add_argument('--countdown', type=int, default=5)
    ap.add_argument('--backend', default='auto',
                    choices=('auto', 'pico', 'sendinput'))
    args = ap.parse_args()

    with LobbyControl(args.backend) as lc:
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
            print(f'{s.value}{extra}  playable={s.playable}')
            if s is LobbyState.LOBBY:
                m, margin = lc.mode()
                print(f'mode: {m or "unreadable"} (margin {margin:.0f}x)'
                      f'{"" if m == SAFE_MODE else "  <-- PLAY would NOT "
                        "start the training range"}')
            print(f'probes: {lc.det.probes()}')
            ok, msg = lc.det.selftest()
            print(f'selftest: {"ok" if ok else "PROBLEM"} — {msg}')
            return 0

        # ensure_focus, not a bare countdown: this CLI is what an unattended
        # campaign calls to recover, and a recovery step that needs a human to
        # click the game first is not a recovery. The countdown survives only
        # as ensure_focus's own fallback.
        if not ensure_focus(countdown_s=args.countdown, label=args.action):
            print('[lobby] could not bring the game to the foreground')
            return 1

        if args.action == 'mode':
            rec = lc.ensure_mode(args.mode)
            print(f'\n{rec}')
            return 0 if rec['ok'] else 1

        fn = {'lobby': lc.exit_to_lobby, 'match': lc.enter_match,
              'ensure': lc.ensure_in_match}[args.action]
        rec = fn(**({'timeout': args.timeout} if args.timeout else {}))

    print(f'\n{rec}')
    print(f'\nstates: {" -> ".join(rec["states"])}')
    print(f'elapsed: {rec["elapsed"]:.1f}s over {rec.get("actions", 0)} '
          f'action(s)')
    if rec['ok']:
        print('^ record this in the OBSERVED DURATIONS block at the top of '
              'this file')
    return 0 if rec['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
