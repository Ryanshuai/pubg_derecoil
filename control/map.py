"""Drive the training range's map screen. M opens it, and it is the teleporter.

    from control.map import MapControl
    with MapControl() as mc:
        mc.goto_range('200m')

The eyes are detector/map_detector.py; this is the hands, the same split the
rest of this package uses. It is a fourth modal screen alongside the spawner
panel (control/spawner.py), the inventory (control/inventory.py) and the ESC
menu (control/lobby.py), and it gets its own driver for the same reason they
each do -- not folded into LobbyControl, whose job is lobby <-> match.

WHY THE SEPARATE MODULE MATTERS, concretely. This was written inside
LobbyControl first, and the placement hid a real hole: M is a keypress, and
BOTH the inventory and the spawner panel swallow keypresses while they are up
(docs/game_quirks.md). The check for that is control/session.ensure_ready(),
which imports LobbyControl -- so from inside LobbyControl it was unreachable,
and goto_range verified only `IN_GAME`. With Tab up it would have reported
`open the map: N attempts had no effect`, i.e. said "the key did not work"
about a screen that was eating the key. Out here, ensure_ready() calls this
module and the precondition is simply true before it runs.

⚠ **M IS A TOGGLE**, which is why nothing here presses it blind: every step
reads the screen first and presses only if the reading disagrees. That is the
same rule spawner.ensure_panel() and inventory.ensure_tab() are written to --
this is the third instance of "toggle a modal screen open/closed and prove it
took", and the first two each paid for the lesson separately.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import MAP_PARK_XY, MAP_RANGE_BOXES, MAP_RANGE_XY
from capture.cropper import capture_screen
from detector.lobby_detector import LobbyDetector
from detector.map_detector import at_range_xy, map_open, player_xy
from press.pointer import move_cursor
from control.driver import Driver
from control.focus import focus_keeper

# Measured on the first live run, 2026-08-06 (see OBSERVED DURATIONS): every
# step landed on its first attempt, so the retry path has never fired.
MAP_POLL = 0.15         # nominal. The real tick is ~0.28 s: a full-screen
                        # capture is ~72 ms and the predicate ~55 ms before
                        # this sleep even starts. Still well under the 0.5 s
                        # LobbyControl polls transitions at.
MAP_RETRY_AFTER = 1.5   # a swallowed keypress is a known game behaviour (see
                        # docs/game_quirks.md), so M gets re-sent.
                        #
                        # ⚠ M IS A TOGGLE, so a re-send that was not needed
                        # UNDOES the step -- press M at an open map and it
                        # shuts. Measured margin is 3.4x (0.44 s to open
                        # against 1.5 s to re-fire). The click path has no
                        # such hazard: clicking a range twice is idempotent.
MAP_MAX_RETRIES = 4     # ⚠ THIS is what actually ends a stuck step, not
                        # MAP_TIMEOUT: 4 attempts x 1.5 s gives up at ~6 s.
                        # LobbyControl's MAX_RETRIES=3 was tuned against its
                        # own RETRY_AFTER=15 s, so reusing it here would have
                        # meant "give up after 4.5 s" while a comment claimed
                        # a 15 s ceiling with 19x of headroom.
MAP_TIMEOUT = 15.0      # outer ceiling, reached only if a step somehow keeps
                        # succeeding at firing but never at working.
MAP_SETTLE = 0.30       # after the cursor parks, before the verifying read:
                        # the hover preview card fades rather than cuts.
                        # ⚠ Never measured -- the fade was not timed, and no
                        # run has read a card that was still up.


def _rec(elapsed, tries, error=None, frame=None):
    """One step's outcome. `frame` is the frame the predicate accepted, kept
    so the caller can read it again instead of re-capturing."""
    return {'ok': error is None, 'elapsed': elapsed, 'tries': tries,
            'error': error, 'frame': frame}


class MapControl(Driver):
    """The map screen: open it, click a range to teleport, close it."""

    def __init__(self, verbose=True):
        super().__init__()
        self.verbose = verbose
        self.det = LobbyDetector()

    def _log(self, msg):
        if self.verbose:
            print(f'[map] {msg}', flush=True)

    def close(self):
        super().close()
        self.det.close()

    # ── Actions ──

    def press_map(self):
        """Toggle the map.

        ⚠ The "SendInput has no keyboard path" caveat and the `pico is None`
        guard under it both went with that backend (2026-08-08). See
        LobbyControl.press_esc for the same removal.
        """
        from press.pico_mouse import HID_KEY_M
        self.pointer.pico.key(HID_KEY_M, 60)
        return 'M'

    def _click_range(self, name):
        """Click a range on the open map, then get the cursor out of the way.

        Parking is part of the action, not tidiness: hovering a range pops a
        preview card, and the read that verifies this click is by definition
        taken with the cursor still on the thing just clicked.
        """
        xy = MAP_RANGE_XY[name]
        self.pointer.click_at(*xy)
        move_cursor(MAP_PARK_XY)
        time.sleep(MAP_SETTLE)
        return f'click {name} range {xy}'

    # ── The polling core ──

    def _await_frame(self, pred, timeout, act, tag):
        """Poll a screen predicate, re-firing `act` when it stalls.

        Deliberately NOT LobbyControl._pump. That polls a LobbyState — a
        classification of the whole screen, with a retry budget that re-arms
        on every real transition because a match transition passes through
        several states. This polls a boolean on a frame, where there are no
        intermediate states to re-arm on: the map is open or it is not.

        Merging them would mean parameterising the observation (`observe=`),
        the arrival test, the budget policy and the poll interval — four knobs
        to host two callers, which is two machines sharing a signature rather
        than one machine doing two jobs.

        `act` returning None means there is no way to fire it at all (no
        Pico); that is reported rather than retried, since spending the
        timeout on a path that does not exist tells the caller nothing.
        """
        t0 = time.perf_counter()
        acted_at, tries = -1e9, 0
        while True:
            elapsed = time.perf_counter() - t0
            frame = capture_screen()
            if pred(frame):
                return _rec(time.perf_counter() - t0, tries, frame=frame)
            if elapsed > timeout:
                return _rec(elapsed, tries,
                            f'{tag}: no change after {timeout:.0f}s')
            if not focus_keeper().ok(tag):
                return _rec(elapsed, tries,
                            f'{tag}: lost the foreground and could not take '
                            f'it back')
            if elapsed - acted_at >= MAP_RETRY_AFTER:
                if tries >= MAP_MAX_RETRIES:
                    return _rec(elapsed, tries,
                                f'{tag}: {MAP_MAX_RETRIES} attempts had no '
                                f'effect')
                label = act()
                if label is None:
                    return _rec(elapsed, tries,
                                f'{tag}: no way to send it (no Pico?)')
                acted_at, tries = elapsed, tries + 1
                self._log(f'{elapsed:6.1f}s  >>> {label} (attempt {tries})')
            time.sleep(MAP_POLL)

    def ensure_map(self, want=True, timeout=MAP_TIMEOUT):
        """Get the map to `want` and prove it. -> rec

        Reads before it presses, so calling this on a map already in the
        wanted state costs one capture and no keypress.

        ⚠ READ-BEFORE-PRESS IS RIGHT FOR A TOGGLE AND IT INHERITS THE READ'S
        LIES. Observed 2026-08-08, 0.0 s after the game reached in_game: this
        returned ok having sent NO M, because map_open() answered True on a
        screen with no map on it. `player_xy` was None in the same frame, so
        what fired was the left panel's yellow selection border. goto_range
        then spent all four of its attempts clicking the 200m box INTO THE
        WORLD, reported "4 attempts had no effect", and the same call nine
        seconds later worked on its first try.

        Nothing here is wrong -- the failure is loud, bounded, and recovered by
        LobbyControl.RANGE_SETTLE_S, whose comment carries the full trace. But
        the two halves of that OR are not equally trustworthy in the first
        seconds of a match, and a caller reading "proved it" should know which
        proof it got. `player_xy` disagreeing with `map_open` IS the tell, and
        goto_range already logs it ("player is at None").
        """
        pred = map_open if want else (lambda f: not map_open(f))
        return self._await_frame(pred, timeout, self.press_map,
                                 f'{"open" if want else "close"} the map')

    # ── The one thing this module is for ──

    def goto_point(self, xy, timeout=MAP_TIMEOUT):
        """L1 — Teleport by clicking a bare (x, y) on the map, not a named range.

        ⚠ THE MAP HAS TWO KINDS OF TELEPORT TARGET AND CONFIG ONLY KNOWS ONE.
        `MAP_RANGE_BOXES` holds translucent yellow RECTANGLES (the 200m lane is
        the single entry). There are also POINT targets drawn as small icons --
        the parachute at (1148, 476) is one, and clicking it lands you at Jump
        School, whose north face is the only large unbroken concrete slab found
        so far. MEASURED 2026-08-11 by clicking it; `goto_range` cannot reach it
        because there is no box to centre on.

        ⚠ AND IT CANNOT VERIFY ARRIVAL THE WAY goto_range DOES. That one reads
        the player marker back against a known box. Here the caller supplies a
        raw coordinate, so nothing knows where the marker SHOULD end up -- the
        return says the click was delivered and the map was put away, and it
        does NOT say you arrived. A caller that needs arrival proved has to
        prove it from the world (the wall survey in calibration/hole_groups.py
        is one way: no wall in view means no teleport happened).

        Kept next to goto_range rather than left as three lines in the caller
        because those three lines are M / click / park / M, and this layer owns
        every one of them -- calibration/ importing press.pointer to park a
        cursor is exactly the parallel-driver shape rule 6 exists to stop.

        The map is closed on every exit path, exceptions included.
        """
        state = self.det.state()
        if not state.playable:
            return {'ok': False, 'clicked': None, 'elapsed': 0.0,
                    'error': f'{state.value}, not a running match — the map '
                             f'only opens in one'}
        t0 = time.perf_counter()
        try:
            opened = self.ensure_map(True, timeout=timeout)
            if not opened.get('ok'):
                return {'ok': False, 'clicked': None,
                        'elapsed': time.perf_counter() - t0,
                        'error': f'the map would not open: {opened.get("error")}'}
            self.pointer.click_at(*xy)
            # Parking is part of the action, not tidiness: a hovered icon pops
            # a preview card, and it lands on top of whatever is read next.
            move_cursor(MAP_PARK_XY)
            time.sleep(MAP_SETTLE)
        finally:
            closed = self.ensure_map(False, timeout=timeout)
        return {'ok': bool(closed.get('ok')), 'clicked': tuple(xy),
                'elapsed': time.perf_counter() - t0,
                'error': None if closed.get('ok') else
                         f'the map would not close: {closed.get("error")}'}

    def goto_range(self, name='200m', timeout=MAP_TIMEOUT):
        """L1 — Teleport to a practice range and put the map away again. One step,
        proved by reading the PLAYER MARKER back (a missed click and an
        ignored one leave the same screen). ensure_ready() is the L2.

        ⚠ IT CHECKS `playable` AND NOTHING ELSE. M is a keypress, and Tab
        and the spawner panel both swallow keypresses — run it under either
        and it reports "N attempts had no effect" about a screen eating the
        key. The map itself is closed on every path, exceptions included.

        M -> click the range's highlight -> M. The middle step is a teleport:
        in the training range each practice area is drawn on the map as a
        translucent yellow box that moves you there when clicked.

        WHY A RUN WANTS IT. Everyone spawns at the main compound, and on a
        populated server that compound has people driving through it. Getting
        rammed mid-magazine costs the magazine and — worse — does not announce
        itself: the recoil trace just has someone else's physics in it. The
        200m lane is off to one side. control/session.ensure_ready() calls
        this for every training-range script.

        ⚠ **Arrival is verified by reading the player marker back, not by the
        click returning.** A click that missed the box and a click the game
        ignored both leave the map exactly as it was.

        Idempotent: with the map open it checks first, and a caller already
        standing at the range pays one map toggle and no click.

        ⚠ **The map is closed on every exit path, including exceptions.**
        Handing back an open map would give the caller a state where every
        later keypress and click goes to the map instead of the game, and
        nothing downstream tests for that. It is a `finally`, not a line
        before each `return`, because the version that was a line before each
        return did not cover the raising path at all.
        """
        if name not in MAP_RANGE_XY:
            return {'ok': False, 'steps': [], 'player': None, 'elapsed': 0.0,
                    'error': f'{name} is not a mapped range; have '
                             f'{sorted(MAP_RANGE_XY)}'}

        state = self.det.state()
        if not state.playable:
            return {'ok': False, 'steps': [], 'player': None, 'elapsed': 0.0,
                    'error': f'{state.value}, not a running match — the map '
                             f'only opens in one'}

        t0 = time.perf_counter()
        steps = []

        def step(tag, rec):
            steps.append({'step': tag, 'elapsed': round(rec['elapsed'], 2),
                          'tries': rec['tries'], 'error': rec['error']})
            return rec

        opened = step('open', self.ensure_map(True, timeout))
        if not opened['ok']:
            # No close attempt: the map never opened, so pressing M now would
            # OPEN it and leave behind exactly the state this guards against.
            return {'ok': False, 'steps': steps, 'player': None,
                    'elapsed': time.perf_counter() - t0,
                    'error': opened['error']}

        error, where = None, None
        try:
            move_cursor(MAP_PARK_XY)
            time.sleep(MAP_SETTLE)
            # One read answers both "where is the player" and "is that at the
            # range" -- at_range() would repeat the full-frame pass, and each
            # one is ~55 ms over 14.9 MB.
            where = player_xy(opened['frame'])
            # ⚠ NO MARKER MEANS NO MAP, AND A CLICK WITH NO MAP IS A GUNSHOT.
            #
            # ensure_map's docstring has recorded since 2026-08-08 that
            # map_open() can answer True on a screen with no map on it -- the
            # left panel's yellow selection border -- and that `player_xy`
            # coming back None in the SAME frame is the tell. It logged the
            # tell and clicked anyway, and called the outcome "loud, bounded
            # and recovered".
            #
            # It is not bounded. Measured the same day: two runs hit it, and
            # the eight clicks aimed at the 200m box landed in the world as
            # eight rounds out of the gun under test -- the ammo counter went
            # 40 -> 32 and the magazine that would have been fired next was
            # short. In a training range a stray left click is a shot; nothing
            # about that is confined to the map.
            #
            # So the disagreement is now the GATE it always was the evidence
            # for. Two witnesses to one state, and clicking needs both.
            if where is None:
                self._log('the map reads open but the player marker is not '
                          'there — re-opening rather than clicking, because a '
                          'click with no map under it goes into the world')
                opened = step('reopen', self.ensure_map(True, timeout))
                where = player_xy(opened['frame']) if opened['ok'] else None
            if where is None:
                # Set the error and fall through to the `finally`, rather than
                # raise: this function's contract is a record, and its callers
                # branch on `ok`. The map still gets closed either way.
                error = ('the map would not come up: map_open says yes and '
                         'the player marker says no, twice. Refusing to '
                         'click — with no map under them those clicks fire '
                         'the weapon.')
                step('marker', _rec(0.0, 2, error))
            elif at_range_xy(where, name):
                self._log(f'already at the {name} range ({where}) — no click')
                step('already-there', _rec(0.0, 0))
            else:
                self._log(f'player is at {where}, going to {name}')
                jumped = step('teleport', self._await_frame(
                    lambda f: at_range_xy(player_xy(f), name), timeout,
                    lambda: self._click_range(name), f'teleport to {name}'))
                error = jumped['error']
                if jumped['ok']:
                    # The frame that satisfied the gate, not a fresh capture:
                    # re-reading would report a position ~130 ms later than
                    # the one that actually passed.
                    where = player_xy(jumped['frame'])
        finally:
            closed = step('close', self.ensure_map(False, timeout))

        if not closed['ok']:
            error = (f'{error}; ALSO {closed["error"]}' if error else
                     f'arrived at {name} but {closed["error"]} — the map is '
                     f'still up and will swallow input')
        return {'ok': error is None, 'steps': steps, 'player': where,
                'elapsed': time.perf_counter() - t0, 'error': error}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=('state', 'goto', 'open', 'close'),
                    help='state: read and exit, touching nothing. '
                         'goto: teleport to --range. '
                         'open/close: just toggle the map.')
    ap.add_argument('--range', default='200m', choices=sorted(MAP_RANGE_BOXES),
                    help='practice range for `goto`')
    ap.add_argument('--timeout', type=float, default=MAP_TIMEOUT)
    ap.add_argument('--countdown', type=int, default=5)
    args = ap.parse_args()

    with MapControl() as mc:
        if args.action == 'state':
            frame = capture_screen()
            xy = player_xy(frame)
            print(f'map open : {map_open(frame)}')
            print(f'player   : {xy}')
            for name in MAP_RANGE_BOXES:
                print(f'at {name:>5} : {at_range_xy(xy, name)}')
            return 0

        # ensure_ready, not ensure_focus: M is a keypress and both Tab and the
        # spawner panel swallow keypresses. This CLI is run by hand exactly
        # when something is already off, so it should establish the state
        # rather than report that it was not established.
        from control.session import ensure_ready
        if not ensure_ready(label=f'map {args.action}',
                            countdown_s=args.countdown, range_name=None)['ok']:
            return 1

        if args.action in ('open', 'close'):
            rec = mc.ensure_map(args.action == 'open', args.timeout)
            print(f'\n{"ok" if rec["ok"] else rec["error"]} '
                  f'({rec["elapsed"]:.2f}s, {rec["tries"]} attempt(s))')
            return 0 if rec['ok'] else 1

        rec = mc.goto_range(args.range, args.timeout)
        for s in rec['steps']:
            print(f'  {s["step"]:<14} {s["elapsed"]:5.2f}s  '
                  f'{s["tries"]} attempt(s)  {s["error"] or "ok"}')
        print(f'\nplayer marker: {rec["player"]}')
        print(f'{"ok" if rec["ok"] else rec["error"]}  ({rec["elapsed"]:.1f}s)')
        if rec['ok']:
            print('^ record these in the OBSERVED DURATIONS block in this file')
        return 0 if rec['ok'] else 1


# OBSERVED DURATIONS -- fill in from live runs, the way control/lobby.py does.
#
#   goto_range('200m') : 2.3 s cold, 1.4 s already standing there. Steps:
#                        open 0.44 s, click -> arrived 0.78 s, close 0.43 s,
#                        each on its FIRST attempt. Marker moved
#                        (1913,983) -> (1977,450); the second run took the
#                        idempotent path and sent no click. ONE run, and it
#                        predates the rewrite that split this out of
#                        LobbyControl -- the numbers are the game's, not this
#                        code's, but nothing here has been re-timed since.
#
#   RE-TIMED 2026-08-08, now that ensure_in_match calls this on every entry.
#   Three calls in one session, and they are the three branches:
#
#     0.0 s after in_game   FAILED. 4 attempts, ~6 s, no M sent at all (see
#                           ensure_map's warning). Marker unreadable.
#     +3 s settle, cold     2.39 s. open 0.60 / teleport 0.80 / close 0.56,
#                           each first try. Marker (2036,1020) -> (1979,454),
#                           i.e. the spawn compound to the lane's spawn point.
#     already standing there 1.41 s. open / already-there / close, no click.
#
#   The cold number is within 0.1 s of the 2026-08-06 run, across the rewrite
#   AND the move back into LobbyControl. Whatever this costs, it is the game's.


if __name__ == '__main__':
    sys.exit(main())
