"""Weapon switching for the automated calibration sweep.

The sweep works through ~22 weapons x 3 postures, so something has to put each
one in the player's hands between cells. That is a `control/` job, and this
module is the thin seam that lets the sweep ask for it without knowing whether
a human or the item spawner answers.

    IMPLEMENTORS: subclass WeaponSwitcher and satisfy the contract on
    switch_to(). Read that contract before writing one — it is deliberately
    narrower than it used to be, and the narrowing is the point.

⚠ THIS FILE WAS A STANDING ADVERTISEMENT FOR A FUNCTION THAT ALREADY EXISTED.
Until 2026-08-07 `SpawnerSwitcher.switch_to` raised NotImplementedError with
the words "point --switcher at the loadout module once it lands". It had
landed: control.stock.ensure_weapon_in_hand does exactly this job, verifies it
against the ammo counter, and had four callers in tools/. Meanwhile sweep.py's
--switcher defaulted to `manual`, so `pixi run sweep` printed ">>> Equip AUG"
and WAITED SIXTY SECONDS FOR A HUMAN, once per weapon, in a project whose
entire purpose is unattended overnight calibration.

It is worth naming the shape, because it is the one this repo keeps paying
for: the ABC's docstring was BETTER WRITTEN than the real implementation's.
Five crisp post-conditions here; over in stock.py, a function that actually
works and says less about itself. An agent looking for "put a gun in hand"
reads declarations, finds this one first, and builds against the stub.

The contract below is now the SMALLER of the two claims rather than the
larger, because a promise nothing keeps is worse than no promise.
"""
import time
from abc import ABC, abstractmethod

from detector.attachment_catalog import ROSTER
from detector.weapon import WEAPON_RPM, ar, smg, mg


class WeaponSwitcher(ABC):
    """Puts a named weapon in the player's hands."""

    @abstractmethod
    def switch_to(self, weapon, timeout_s=60.0):
        """Equip `weapon` and return once it is out. -> bool

        Args:
            weapon: key of detector.weapon.WEAPON_RPM, e.g. 'aug', 'm416'.
            timeout_s: give up after this long.

        Returns True only when BOTH of these hold, each CONFIRMED against the
        screen rather than inferred from an action having been sent:
            - the named weapon is IN HAND, not merely in the rack
            - no inventory and no spawner panel is left open

        Returns False if the weapon cannot be produced; the sweep logs it and
        moves on to the next weapon rather than aborting the run.

        ⚠ WHAT IT DOES NOT PROMISE, and who owns each instead. This list used
        to be part of the promise above, and NEITHER IMPLEMENTATION HAS EVER
        DELIVERED ANY OF IT — ManualSwitcher only ever compared a weapon name.
        A contract that no implementor satisfies does not constrain
        implementors; it only misleads callers.

            posture       sweep.calibrate_combo calls rig.ensure_posture() per
                          cell, and must: posture is part of what the cell IS.
            not in ADS    same call. The posture icon only renders IN ADS, so
                          ensure_posture enters it deliberately — a switcher
                          that "guaranteed" not-ADS would be undone one line
                          later.
            full magazine FireDriver.top_up() before each magazine, which
                          returns the round count it actually observed.

        Attachments are not specified either. Whatever the spawner fits is
        fine as long as it is READ BACK — calibrate_combo reads the loadout off
        the Tab screen and folds the parts into the pattern analytically
        (compensator 0.85 x vertical grip 0.85, 0.7% against measurement), so
        the sweep only ever solves for the bare-weapon and posture terms.
        """

    def available(self):
        """Weapon names this switcher can produce. Default: all full-autos."""
        return sorted((ar | smg | mg) & set(WEAPON_RPM))

    def close(self):
        """Release any resources. Safe to call more than once."""


class ManualSwitcher(WeaponSwitcher):
    """Asks a human to swap the weapon, then verifies it actually changed.

    Verification matters more than the prompt: without it a mistyped or
    forgotten swap silently attributes one weapon's recoil to another, and
    every downstream number for that weapon is wrong with no visible symptom.

    ⚠ It confirms the NAME and nothing else — see the contract above for what
    that leaves to the caller. It is the fallback for a weapon the spawner
    cannot produce, not the default; --switcher spawner is.
    """

    def __init__(self, verify_fn, poll_s=4.0):
        """verify_fn() -> current weapon name ('' if unknown).

        sweep.py passes a Tab-based reader. Keep the poll interval slow; each
        call opens and closes the inventory.
        """
        self._verify = verify_fn
        self._poll_s = poll_s

    def switch_to(self, weapon, timeout_s=60.0):
        print(f"\n>>> Equip {weapon.upper()} (fully kitted), stand up, "
              f"leave ADS.")
        print(f"    Waiting up to {timeout_s:.0f}s; checking every "
              f"{self._poll_s:.0f}s ...", flush=True)
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            time.sleep(self._poll_s)
            cur = self._verify()
            if cur == weapon:
                print(f"    detected {weapon} — proceeding")
                return True
            if cur:
                print(f"    currently holding {cur!r}, still waiting ...",
                      flush=True)
        print(f"    timed out waiting for {weapon}")
        return False


class SpawnerSwitcher(WeaponSwitcher):
    """Spawns the weapon from the training-range panel and holds it.

    ⚠ THERE IS NO DRIVING CODE HERE, AND THERE MUST NOT BE. Every line of it
    is control.stock.ensure_weapon_in_hand, which was written for the probes
    and paid for its lessons there:

      - it checks WHICH gun, not merely that a gun is out. A rack left loaded
        by the previous cell satisfies "a weapon is in hand" and hands the
        sweep the wrong weapon — measured 2026-08-05, an hour of a failure
        that looked like the scene rather than the loadout.
      - it goes through ac.hold() rather than a bare 1/2 keypress, because
        those keys are SWALLOWED while Tab is up (docs/game_quirks.md).
      - it brackets the spawner with ensure_panel(True/False), because
        collapse_all() on a closed panel collapses nothing and reports
        nothing, and give_many then clicks from a stale layout.

    Writing any of that again here is how the second copy starts.

    THE OBJECTS ARE BUILT PER SWITCH, not held. InventoryControl's grabber
    keeps GDI objects open and this runs a handful of times per hour, so the
    lifetime that costs nothing is the short one. The Pico itself is a process
    singleton (press.pico_mouse.get_mouse), so there is no port to contend
    for with the live sweep.Rig — only the grabbers, and those are released
    in the finally.
    """

    def __init__(self, verbose=True):
        self.verbose = verbose

    def switch_to(self, weapon, timeout_s=60.0):
        # Imported here rather than at module scope so that importing this
        # file — which sweep.py does at startup, before it has taken the
        # foreground — does not build a detector stack or reach for the Pico.
        from control.inventory import InventoryControl
        from control.spawner import SpawnerControl
        from control.stock import ensure_weapon_in_hand

        # `timeout_s` is not forwarded and that is deliberate: the step this
        # wraps is not a wait. It opens a panel, clicks a known layout and
        # reads the rack back, each with its own bounded retries — there is no
        # single deadline to hand it, and passing one would suggest the caller
        # can trade time for success here. It cannot; ManualSwitcher is the
        # implementation where waiting longer helps.
        with SpawnerControl() as sc:
            ac = InventoryControl(verbose=False)
            try:
                slot = ensure_weapon_in_hand(ac, sc, weapon=weapon,
                                             verbose=self.verbose)
            finally:
                ac.close()
        return slot is not None

    def available(self):
        """The full-autos the spawner has a catalogue entry for.

        Narrower than the base class on purpose: the default answers "which
        weapons exist", and a switcher is asked "which can YOU produce". A
        weapon absent from ROSTER has no panel coordinates, so give_many would
        fail on it after the run had already committed to the cell.
        """
        return sorted(set(super().available()) & set(ROSTER))


def get_switcher(kind, verify_fn=None):
    if kind == 'manual':
        if verify_fn is None:
            raise ValueError("ManualSwitcher needs verify_fn")
        return ManualSwitcher(verify_fn)
    if kind == 'spawner':
        return SpawnerSwitcher()
    raise ValueError(f"unknown switcher {kind!r}")
