"""Staying inside the training range for longer than the training range allows.

The range evicts you after 20 minutes. A full harvest run is 45-50, so it will
be thrown out mid-run at least twice — and re-entry is not free: the backpack
is empty again, the weapon rack is empty again, and the character respawns
wherever the range puts you rather than in front of an item spawner.

    IMPLEMENTORS: subclass RangeSession. ManualSession below is the
    human-in-the-loop fallback and also documents, in code, exactly what
    "back in the range" has to mean.

Being kicked mid-magazine is survivable — the cell fails, --resume retakes it —
but it wastes a magazine and leaves the character somewhere unknown. So the
caller re-enters on a budget rather than waiting to be evicted, which is why
elapsed() exists separately from in_range().
"""
import time
from abc import ABC, abstractmethod

# The range allows 20 minutes. Recycling at 17 leaves room for one more weapon
# (about 3 minutes at 4 configs) without gambling on the last one finishing.
DEFAULT_BUDGET_S = 17 * 60


class RangeSession(ABC):
    """Knows whether we are in the training range, and can get us back in."""

    def __init__(self, budget_s=DEFAULT_BUDGET_S):
        self.budget_s = budget_s
        self._entered = time.time()

    @abstractmethod
    def in_range(self):
        """True when the character is in the training range and playable.

        Must be false while a loading screen, the lobby, or any menu is up —
        a caller that believes it is in the range will start pressing fire.
        """

    @abstractmethod
    def enter(self, timeout_s=300.0):
        """Get back into the training range. Returns True only when ALL hold:

            - the character is in the range, loaded and controllable
            - standing at an item spawner, close enough to open it with comma
            - no menu, no inventory, not in ADS
            - standing, not crouched or prone

        Returns False if it cannot; the caller stops rather than firing blind.

        The weapon rack and backpack are expected to be EMPTY afterwards.
        Whatever was carried does not survive a re-entry, so the caller
        re-stocks its parts and re-spawns its weapon from scratch.
        """

    def elapsed(self):
        return time.time() - self._entered

    def expiring(self):
        """True when there is not enough of the session left to trust."""
        return self.elapsed() > self.budget_s

    def mark_entered(self):
        self._entered = time.time()

    def ensure(self, force=False):
        """Re-enter if we have been thrown out, or are about to be.

        Returns (ok, re_entered) so the caller knows whether to re-stock.
        """
        if not force and not self.expiring() and self.in_range():
            return True, False
        ok = self.enter()
        if ok:
            self.mark_entered()
        return ok, ok

    def close(self):
        """Release any resources. Safe to call more than once."""


class ManualSession(RangeSession):
    """Asks a human to re-enter, then waits for the check to agree.

    Verification matters more than the prompt: a run that carries on believing
    it is in the range fires into a lobby and records the result.
    """

    def __init__(self, in_range_fn, budget_s=DEFAULT_BUDGET_S, poll_s=5.0):
        super().__init__(budget_s)
        self._in_range = in_range_fn
        self._poll_s = poll_s

    def in_range(self):
        return bool(self._in_range())

    def enter(self, timeout_s=300.0):
        print("\n>>> The training range session is over.")
        print("    Re-enter it, walk to an item spawner, and stand there.")
        print(f"    Waiting up to {timeout_s:.0f}s, checking every "
              f"{self._poll_s:.0f}s ...", flush=True)
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            time.sleep(self._poll_s)
            if self.in_range():
                print("    back in the range — continuing")
                return True
        print("    timed out waiting to get back in")
        return False


class AutoSession(RangeSession):
    """Drives the lobby back into a match, with no human involved.

    detector/lobby_control.py does the driving: from the results screen, the
    lobby, an open ESC menu or a loading screen, ensure_in_match() gets to a
    running round, polling the state rather than sleeping fixed durations.

    Two things it does NOT know, and both matter here:

      * WHICH mode it re-enters. press_play() is F on whatever the lobby has
        selected, so the lobby must be left on the training range. Nothing can
        verify that from the lobby screen — but the spawner check below
        catches it a moment later, because no other mode has an item spawner.
      * WHERE in the range it lands. Re-entry drops the character at the
        range's spawn point, and the caller needs it standing at an item
        spawner. at_spawner_fn is how that gets verified; without it this
        reports success while parked somewhere with nothing to spawn from.
    """

    def __init__(self, budget_s=DEFAULT_BUDGET_S, at_spawner_fn=None,
                 verbose=False):
        super().__init__(budget_s)
        from detector.lobby_detector import LobbyDetector
        from detector.lobby_control import LobbyControl
        self._det = LobbyDetector()
        self._lc = LobbyControl(verbose=verbose)
        self._at_spawner = at_spawner_fn

    def in_range(self):
        """Playable, not merely IN_GAME. The loading screen and the ESC menu
        both look like a match to anything coarser, and input goes nowhere in
        either."""
        return bool(self._det.state().playable)      # a property, not a call

    def enter(self, timeout_s=300.0):
        r = self._lc.ensure_in_match(timeout=timeout_s)
        if not r.get('ok'):
            print(f"    [!] could not get back into a match: {r}")
            return False
        print(f"    back in a match after {r.get('elapsed', 0):.0f}s")
        if self._at_spawner is None:
            return True
        if self._at_spawner():
            return True
        print("    [!] in a match, but the item spawner will not open. Either "
              "the lobby was set to a different mode, or the spawn point is "
              "not next to a spawner — walking there is not automated.")
        return False

    def close(self):
        for x in (self._det, self._lc):
            try:
                x.close()
            except Exception:
                pass


def get_session(kind, in_range_fn=None, budget_s=DEFAULT_BUDGET_S,
                verbose=False):
    """in_range_fn proves we are somewhere useful — for harvest that means
    "comma produces the item spawner", which is both the range test and the
    at-a-spawner test in one."""
    if kind == 'manual':
        if in_range_fn is None:
            raise ValueError("ManualSession needs in_range_fn")
        return ManualSession(in_range_fn, budget_s)
    if kind == 'auto':
        return AutoSession(budget_s, at_spawner_fn=in_range_fn,
                           verbose=verbose)
    raise ValueError(f"unknown session kind {kind!r}")
