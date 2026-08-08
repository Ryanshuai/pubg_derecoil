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
        # force means RESTART, and that has to LEAVE first. enter() is
        # ensure_in_match(), which returns straight away when a match is
        # already running -- so force=True on a healthy session used to cost
        # 0 s and change nothing, while reporting `re_entered=True`. The one
        # caller that wanted it wanted the SIDE EFFECT: re-entry is the only
        # thing that empties the backpack and the floor, and both saturate at
        # 12 rows, at which point the spawner silently stops delivering.
        if force and self.in_range():
            self.leave()
        ok = self.enter()
        if ok:
            self.mark_entered()
        return ok, ok

    def leave(self):
        """Get out of the range, so the next enter() is a real re-entry.

        Base class cannot: a manual session has no way to drive the menus.
        Overridden where there is one.
        """
        return False

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
        print("    Re-enter it. Anywhere in the range will do — the spawner is "
              "the comma menu, not a thing to stand next to.")
        print(f"    Waiting up to {timeout_s:.0f}s, checking every "
              f"{self._poll_s:.0f}s ...", flush=True)
        # ⚠ THE ONLY CALLER OF THIS FUNCTION IN THE REPOSITORY, and it should
        # stay that way. A human re-entering is the one mover no transition in
        # control/lobby.py can observe: AutoSession below goes through
        # ensure_in_match, which sees the game outside a match and re-places
        # the character itself. This class stands and WAITS for someone else to
        # do it, so it is the one place that has to say so.
        from control.lobby import forget_placement
        forget_placement()
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

    control/lobby.py does the driving: from the results screen, the
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
        from control.lobby import LobbyControl
        self._det = LobbyDetector()
        self._lc = LobbyControl(verbose=verbose)
        self._at_spawner = at_spawner_fn

    def in_range(self):
        """Playable, not merely IN_GAME. The loading screen and the ESC menu
        both look like a match to anything coarser, and input goes nowhere in
        either."""
        return bool(self._det.state().playable)      # a property, not a call

    def leave(self):
        """Walk the ESC menu out to the lobby. -> bool

        control/lobby.py refuses to click LEAVE unless the entry's glyphs
        match, because EXIT TO DESKTOP sits one pitch below it. A refusal here
        is the guard working, not a failure to route around.
        """
        r = self._lc.exit_to_lobby()
        if not r.get('ok'):
            print(f"    [!] could not leave the range: {r}")
        return bool(r.get('ok'))

    def enter(self, timeout_s=300.0):
        # ⚠ THROUGH ensure_ready, NOT ensure_in_match. This used to call the
        # lobby driver directly, and being in a match is only ONE of the five
        # things a magazine needs. The one it silently dropped is the range:
        # re-entry puts the character at the SPAWN COMPOUND, which this
        # class's own docstring says ("respawns wherever the range puts
        # you"), and nothing walked it back. A 45-minute harvest is evicted
        # at least twice, so the back half of every long run was fired in the
        # middle of a populated compound -- and being rammed mid-magazine
        # costs the magazine without announcing itself.
        #
        # ⚠ THERE USED TO BE A forget_range() ON THIS LINE and it is gone, not
        # forgotten. The belief moved into LobbyControl, whose ensure_in_match
        # teleports whenever ITS OWN pump found the game outside a match --
        # which is precisely the situation this method is called in. The old
        # arrangement needed the declaration because ensure_ready read the
        # state, saw `playable` (the eviction had already been recovered by
        # then on some paths), and skipped the teleport on a belief this
        # re-entry had just falsified. Nothing to declare now: the module that
        # walks the character is the module that knows it walked.
        from control.session import ensure_ready
        # countdown_s=0: the operator is not standing by mid-run, and the
        # countdown exists to give a human time to alt-tab away at the start.
        rec = ensure_ready(label='re-entering the range', countdown_s=0,
                           verbose=True, match_timeout=timeout_s)
        if not rec.get('ok'):
            print(f"    [!] could not get back into the range: "
                  f"failed at {rec.get('failed')}")
            return False
        print("    back in the range, on the lane")
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
