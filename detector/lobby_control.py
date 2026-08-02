"""Drive the game between the lobby and a match.

    from detector.lobby_control import LobbyControl
    lc = LobbyControl()
    if lc.ensure_in_match()['ok']:
        ...                       # a round is up and drivable

Lives next to lobby_detector rather than under calibration/ (where
spawner_control and attach_control sit): the two halves are one unit, and the
polling loop here is meaningless without the state machine over there.

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
      lobby -> in_game   : (not yet measured)
      F -> first change  : (not yet measured)

Actions are fired at most once per cooldown rather than on every poll: holding
the state does not mean the action failed, it usually means the game is still
working on it. Re-pressing F every 500 ms would queue up menu input.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (LOBBY_EXIT_XY, LOBBY_LEAVE_CONFIRM_XY,
                    LOBBY_MENU_LEAVE_XY, LOBBY_PLAY_XY)
from detector.lobby_detector import (LobbyDetector, LobbyState,
                                     leave_confirm_visible,
                                     leave_entry_confirmed, is_results_screen)
from press.pointer import Pointer, game_focused, ensure_focus, focus_keeper

POLL = 0.5              # how often the screen is sampled

# Ceilings, not expectations. See the module docstring.
EXIT_TIMEOUT = 90.0     # results screen -> lobby (it self-exits in ~18 s)
ENTER_TIMEOUT = 300.0   # lobby -> in a match, matchmaking included
RETRY_AFTER = 15.0      # a stuck state this long -> fire the action again

MAX_RETRIES = 3         # then give up rather than mash the menu forever


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

    def press_play(self):
        """Start a match by clicking the PLAY button.

        This used to press F, because the button draws an "F" hint and a
        keypress is immune to wherever the cursor happens to be. That reading
        of the screenshot was wrong: three F presses in a row, with the game
        verified frontmost, moved the lobby not at all. The hint is decoration.

        Cursor placement is exactly what has to be handled rather than avoided
        — the lobby has a real cursor and it is wherever it was left, which is
        usually not over PLAY. click_at moves it there first.
        """
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
        seq, acted_at, retries = [], -1e9, 0
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

            if state is target:
                return {'ok': True, 'elapsed': elapsed, 'states': seq,
                        'retries': retries, 'error': None}

            if elapsed > timeout:
                return {'ok': False, 'elapsed': elapsed, 'states': seq,
                        'retries': retries,
                        'error': f'{tag}: still {state.value} after '
                                 f'{timeout:.0f}s'}

            # Losing the foreground here used to be fatal. It is recoverable:
            # take it back and carry on, since a keypress that went somewhere
            # else is exactly what the retry below is for. Bounded, so a run
            # that cannot hold focus still stops instead of spinning.
            if not focus_keeper().ok(tag):
                return {'ok': False, 'elapsed': elapsed, 'states': seq,
                        'retries': retries,
                        'error': f'{tag}: lost the foreground and could not '
                                 f'take it back'}

            if elapsed - acted_at >= RETRY_AFTER:
                if retries >= MAX_RETRIES:
                    return {'ok': False, 'elapsed': elapsed, 'states': seq,
                            'retries': retries,
                            'error': f'{tag}: {MAX_RETRIES} attempts had no '
                                     f'effect, stuck in {state.value}'}
                label = act(state)
                if label:
                    acted_at = elapsed
                    retries += 1
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
            done = self.click_leave_confirm()
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
    ap.add_argument('action', choices=('state', 'lobby', 'match', 'ensure'),
                    help='state: print and exit. lobby: get to the lobby. '
                         'match: lobby -> match. ensure: anything -> match.')
    ap.add_argument('--timeout', type=float, default=None)
    ap.add_argument('--countdown', type=int, default=5)
    ap.add_argument('--backend', default='auto',
                    choices=('auto', 'pico', 'sendinput'))
    args = ap.parse_args()

    with LobbyControl(args.backend) as lc:
        if args.action == 'state':
            s = lc.state()
            extra = ''
            if s is LobbyState.FULLBLEED and is_results_screen():
                extra = ' (results screen)'
            elif s is LobbyState.MENU:
                extra = (' (LEAVE TRAINING confirmed)'
                         if leave_entry_confirmed()
                         else ' (LEAVE entry NOT where expected)')
            print(f'{s.value}{extra}  playable={s.playable}')
            print(f'probes: {lc.det.probes()}')
            ok, msg = lc.det.selftest()
            print(f'selftest: {"ok" if ok else "PROBLEM"} — {msg}')
            return 0

        print('>>> Bring the game to the foreground.')
        for s in range(args.countdown, 0, -1):
            print(f'    starting in {s} ...', flush=True)
            time.sleep(1.0)
        if not game_focused():
            print('[lobby] game is not the foreground window — aborting')
            return 1

        fn = {'lobby': lc.exit_to_lobby, 'match': lc.enter_match,
              'ensure': lc.ensure_in_match}[args.action]
        rec = fn(**({'timeout': args.timeout} if args.timeout else {}))

    print(f'\n{rec}')
    print(f'\nstates: {" -> ".join(rec["states"])}')
    print(f'elapsed: {rec["elapsed"]:.1f}s over {rec["retries"]} action(s)')
    if rec['ok']:
        print('^ record this in the OBSERVED DURATIONS block at the top of '
              'this file')
    return 0 if rec['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
