"""Weapon switching for the automated calibration sweep.

The sweep needs to work through ~22 weapons x 3 postures. Producing a weapon
from the training-range item spawner is UI automation with its own failure
modes, and is being built separately — so it sits behind this interface.

    IMPLEMENTORS: subclass WeaponSwitcher and satisfy the contract on
    switch_to(). ManualSwitcher below is the human-in-the-loop fallback and
    also documents, in code, exactly what "switched" has to mean.

The caller (auto_calibrate.py) does NOT configure attachments. Whatever the
spawner gives is fine as long as it is consistent — attachments are read back
via Tab and folded into the pattern analytically (compensator 0.85 x vertical
grip 0.85, verified to 0.7% against measurement), so the sweep only ever needs
to solve for the bare-weapon and posture terms.
"""
import time
from abc import ABC, abstractmethod

from detector.weapon import WEAPON_RPM, ar, smg, mg


class WeaponSwitcher(ABC):
    """Puts a named weapon in the player's hands."""

    @abstractmethod
    def switch_to(self, weapon, timeout_s=60.0):
        """Equip `weapon` and return once it is ready to fire.

        Args:
            weapon: key of detector.weapon.WEAPON_RPM, e.g. 'aug', 'm416'.
            timeout_s: give up after this long.

        Returns True only when ALL of these hold:
            - the weapon is IN HAND, not merely in the inventory
            - its magazine is full
            - no menu / inventory is open
            - the player is NOT in ADS (the caller enters ADS itself, and
              toggling from an unknown state would land in the wrong one)
            - the player is standing (the caller drives posture from there)

        Returns False if the weapon cannot be produced; the sweep will log it
        and move on rather than abort.

        Attachments are not specified — fully kitted is expected and the
        caller reads back what is actually equipped.
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
    """

    def __init__(self, verify_fn, poll_s=4.0):
        """verify_fn() -> current weapon name ('' if unknown).

        auto_calibrate passes a Tab-based reader. Keep the poll interval slow;
        each call opens and closes the inventory.
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
    """Drives the training-range item spawner UI.

    NOT IMPLEMENTED — owned by the weapon-loadout module. Stubbed here so the
    sweep can be wired against the real interface today and swap the
    implementation in without touching calibration code.
    """

    def switch_to(self, weapon, timeout_s=60.0):
        raise NotImplementedError(
            "SpawnerSwitcher is not implemented yet. Use ManualSwitcher, or "
            "point --switcher at the loadout module once it lands.")


def get_switcher(kind, verify_fn=None):
    if kind == 'manual':
        if verify_fn is None:
            raise ValueError("ManualSwitcher needs verify_fn")
        return ManualSwitcher(verify_fn)
    if kind == 'spawner':
        return SpawnerSwitcher()
    raise ValueError(f"unknown switcher {kind!r}")
