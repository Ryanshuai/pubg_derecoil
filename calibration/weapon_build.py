"""Turning a run record's attachments into the Weapon that fired it.

ONE construction site, and that is the whole point of the file. There used to
be two -- calibration/harvest.build_weapon and calibration/fit_curve.rebuild --
and they drifted in the way this repo keeps paying for: the SCOPE fix of
2026-08-05 went into the first and was missed in the second.

What that cost, found 2026-08-06: every `--apply` on a magnified sight silently
did nothing. rebuild() re-derived the curve without the scope factor, got 656.6
counts against the 2626.6 the run had actually fired -- exactly 4.00x, the VSS's
PSO-1 -- and correctly refused with "curve changed since the run". The refusal
was right and invisible: ema_update() passes verbose=False, which muted the one
line that said why, so five magazines of a --apply run wrote nothing at all and
printed the SHADOW-mode hint at the end.

So the guard worked, the second implementation was the fault, and the fix is to
stop having a second implementation.
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import detector.weapon as weapon_mod                      # noqa: E402
from detector.weapon import Weapon                        # noqa: E402

def build_weapon(weapon, posture, att, rpm=None):
    """A Weapon carrying the current curve, scale and bullet interval.

    Re-reads the measured fire rates first, because detector.weapon caches the
    table at import and a cell that re-times has to rebuild on the new rate
    rather than on the one it just replaced.

    `rpm` overrides that rate IN MEMORY ONLY, and exists for exactly one
    caller: the mid-cell re-time. A rate from a single magazine is not yet a
    fact -- it is one measurement, and the failure mode it most resembles (a
    missed last transition, which reads as a faster gun) also produces exactly
    one odd magazine. The rest of the cell still has to fire on SOMETHING, and
    a fresh measurement beats a stale table, so it is used and not stored. The
    store happens at the end of the cell, from magazines that AGREE.
    """
    weapon_mod.WEAPON_RPM.update(weapon_mod.load_measured_rpm())
    if rpm:
        weapon_mod.WEAPON_RPM[weapon] = rpm
    w = Weapon()
    w.set('name', weapon)
    w.set('posture', posture)
    w.set('muzzle', (att or {}).get('muzzle', ''))
    w.set('grip', (att or {}).get('grip', ''))
    # ⚠ THE STOCK WAS NEVER PASSED, and it is the third slot every recoil
    # lookup in this repository takes: attachment_factor(gun, muzzle, grip,
    # stock, posture). So for as long as this function has existed, a gun
    # wearing a composite stock was compensated as though it had none -- and
    # under MODEL.md's plan A it is worse than a factor being missed, because
    # the stock is part of the CONFIG KEY: without it the key comes out
    # `grip-vert_grip_muzzle-comp_ar` and misses a curve stored under
    # `grip-vert_grip_muzzle-comp_ar_stock-tactical_stock` entirely.
    #
    # Same shape as `build_weapon` never setting `scope`, found 2026-08-05,
    # which cost every magnification above 1x its compensation.
    #
    # `butt` is the name Weapon.set answers to; `stock` is what the slot is
    # called everywhere else, and both spellings are accepted here so a caller
    # reading the catalogue does not have to know that.
    w.set('butt', (att or {}).get('stock', (att or {}).get('butt', '')))
    # THE SIGHT SCALES THE CURVE, and leaving it out meant every run above 1x
    # fired the RED DOT's compensation. PUBG scales ADS sensitivity with
    # magnification, so a count rotates the view roughly 1/mag as far and
    # cancelling the same angular recoil needs roughly mag times the counts.
    # Weapon.set_seq multiplies by scope_factor for exactly this; nothing set
    # it, so it stayed 1.
    #
    # What that cost, measured 2026-08-05 on one gun in one afternoon:
    #
    #   aug bare, red dot, 6 magazines   true recoil 1812 counts, residual +4%
    #   aug bare, 4x,      1 magazine    true recoil 6347 counts, residual +265%
    #                                    -> ratio 3.50, and the view ran 2692
    #                                       counts past a reference that holds 68
    #
    # Every 4x cell died that way, and so did every vss cell all day: the VSS
    # carries an integral PSO-1 that _SCOPE_TO_MAG treats as 4x, its curve
    # covers 324 counts over a 22-round magazine against a measured 1058 —
    # ratio 3.26 — and that was read as "the curve was never fitted" through
    # eight failed attempts. It was fitted. It was being fired at a quarter
    # strength.
    #
    # From the READBACK, not from `--sight`: what the gun is wearing is what
    # the compensation has to match, and the two disagree whenever a fit
    # silently missed.
    w.set('scope', (att or {}).get('scope', ''))
    w.set_seq()
    return w
