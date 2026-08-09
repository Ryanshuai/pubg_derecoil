"""Per-weapon recoil factors from the muzzle and grip that are fitted.

This table exists for one job: turning weapon_scales.json's calibrated numbers
into a scale for whatever is actually on the gun. It only models the two slots
that change vertical recoil. For the full picture — all five slots, every
attachment, and which weapons are still in the game — see
detector/attachment_catalog.py, which is the authority.

WHICH SLOTS A GUN HAS IS NO LONGER WRITTEN DOWN HERE. It is derived from that
catalogue, because "anything here that disagrees with it is out of date" was
true of two entries and nothing said so:

  js9  had grip=True   -- the catalogue measured no grip tile (ring 18.6) on
                          2026-08-02 and notes the wiki has no JS9 page at
                          all, so the True was a guess about a slot that does
                          not exist
  mp9  had muzzle='comp' -- its silencer is integral and not removable, so
                          there is no muzzle slot to put a compensator in

Both fed calibration_factor(), which divides the calibrated scale to recover a
bare-gun number. Dividing by an attachment that could not have been fitted
during calibration makes that number too big: js9 divided by 0.7225 instead of
0.85 and mp9 by 0.85 instead of 1.0, each **17.65% over-compensating** at
runtime. validate_attachments() had the mirror of the same fault -- it let a
phantom mp9 suppressor through and would not have cleared a phantom js9 grip.

Deriving costs one thing worth stating: js9 and mp9 now compensate 17.65% less
than they did before 2026-08-03. That is the correction, not a regression, but
their numbers are not comparable across that line.

Recoil factors are still from PUBG Wiki (pubg.wiki.gg):
  - Compensator: 0.85 vertical recoil
  - Vertical Foregrip: 0.85 vertical recoil
  - Suppressor: 1.0 (no recoil effect)
  - Other grips: not modeled (only vertical foregrip used as calibration baseline)

weapon_scales.json was calibrated in training ground with default attachments
(compensator + vertical grip where available). The calibration_factor below
represents what was equipped during calibration.

At runtime:
  effective_scale = base_scale * current_factor
  base_scale = weapon_scales[gun] / calibration_factor
"""

import json
import os

import config

from detector.attachment_catalog import (ATTACHMENTS, SLOTS as CATALOG_SLOTS,
                                         compatible, has_slot)

# Muzzle vertical recoil multipliers
MUZZLE_FACTOR = {
    'Compensator': 0.85,
    'FlashHider': 0.95,
    'Suppressor': 1.0,
}

# Grip vertical recoil multipliers. Wiki figures except where marked measured;
# this project's screen-observed residuals run consistently deeper than stated
# (Foregrip states -20% and measures 0.747..0.761), so the two do not mix.
#
# 'AngledForeGrip': 1.0 is gone because update 41.1 removed the part, not
# because it measured wrong -- see attachment_catalog's tilted_grip.
GRIP_FACTOR = {
    'Foregrip': 0.85,       # Vertical Foregrip
    'HalfGrip': 0.92,
    'ThumbGrip': 0.95,
    'TiltedGrip': 0.809,    # measured, mp5k — stated +12% vertical control
    'LightweightForeGrip': 1.0,
    'LaserPointer': 1.0,
}

COMP = 0.85    # shorthand for calibration_factor
GRIP = 0.85

# muzzle_type: 'comp' = can equip compensator, 'supp_only' = suppressor only, None = nothing
# grip: True = can equip vertical foregrip, False = cannot
# calibration_factor: what was equipped during training ground calibration
#   comp + grip → 0.85 * 0.85 = 0.7225
#   comp only  → 0.85
#   grip only  → 0.85 (tommy: suppressor + vertical)
#   nothing    → 1.0


def muzzle_kind(gun):
    """None | 'supp_only' | 'comp' -- can this gun wear a compensator?

    Three states because two questions stack: does the muzzle slot exist at
    all (vss, p90, m249, mg3, mp9 -- integral or absent), and if it does, does
    the gun accept anything other than a suppressor (groza and tommy do not).
    Both answers already live in attachment_catalog: the slot list is measured
    and the suppressor-only rule is its EXCLUDE table.
    """
    if not has_slot(gun, 'muzzle'):
        return None
    muzzles = compatible(gun).get('muzzle', ())
    return 'comp' if any(k.startswith('comp') for k in muzzles) else 'supp_only'


def takes_vertical_grip(gun):
    """Can this gun hold the vertical foregrip the calibration assumed?

    Not `has_slot(gun, 'grip')`: tommy has a grip slot that takes only the
    vertical, and GRIP_ONLY in the catalogue is what encodes that. Asking for
    the specific attachment gets both facts in one question.
    """
    return 'vert_grip' in compatible(gun).get('grip', ())


# Computed, never edited. Kept in this shape because calibration_factor() and
# validate_attachments() read it and because __main__ dumps it, but every row
# comes from the catalogue. Verified against the 30 hand-written rows this
# replaced: 28 identical, and the 2 that were not are the bug in the module
# docstring.
WEAPON_SLOTS = {gun: {'muzzle': muzzle_kind(gun), 'grip': takes_vertical_grip(gun)}
                for gun in CATALOG_SLOTS}

def calibration_factor(gun_name):
    """Factor that was applied during training ground calibration.

    Training ground equips compensator + vertical grip by default (where available).
    """
    slots = WEAPON_SLOTS.get(gun_name)
    if not slots:
        return 1.0
    f = 1.0
    if slots['muzzle'] == 'comp':
        f *= COMP
    if slots['grip']:
        f *= GRIP
    return f


def validate_attachments(gun_name, attachments):
    """Filter out attachments that the weapon cannot equip.

    attachments: dict {scope, muzzle, grip, magazine, stock} → class name or ''.
    Returns filtered copy.
    """
    slots = WEAPON_SLOTS.get(gun_name)
    if not slots:
        return attachments
    out = dict(attachments)
    # Muzzle check
    muzzle_type = slots['muzzle']
    if muzzle_type is None:
        out['muzzle'] = ''
    elif muzzle_type == 'supp_only':
        if out.get('muzzle') and 'Suppressor' not in out['muzzle']:
            out['muzzle'] = ''
    # Grip check
    if not slots['grip']:
        out['grip'] = ''
    return out


# ⚠ THE PATH AND THE FILE MUST MOVE IN THE SAME COMMIT. On 2026-08-08 the
# json went to data/ and this line stayed, and it cost nothing visible:
# _load_kit_factors() catches OSError and returns {}, so every gun quietly
# fell back to the wiki coefficients (median 34.7% off) with no error
# anywhere. `pixi run kit-factors` went 6/18 and was the only thing that
# said so — every FAIL printed 0.7225, which IS the wiki product.
_KIT_PATH = config.KIT_FACTORS_PATH
# Slot order must match calibration/build_kit_factors.RECOIL_SLOTS, because the key
# is built the same way on both sides and a mismatch would silently miss every
# row rather than raise.
_KIT_SLOTS = ('muzzle', 'grip', 'stock')


def _load_kit_factors():
    try:
        with open(_KIT_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_kit_file = _load_kit_factors()
# TWO TABLES OUT OF ONE MEASUREMENT SET, written together by
# calibration/build_kit_factors.py:
#   kits   every measured (weapon, posture, kit) -- coupling included, because
#          the kit was on the gun when it was fired
#   parts  the single-slot subset, i.e. what THIS part does on THIS gun
# `parts` is a projection of `kits`, never an independent number.
_kit_factors = _kit_file.get('kits', {})
_part_factors = _kit_file.get('parts', {})
# Asset class name -> catalogue key. The runtime carries what the DETECTOR
# read ('Lower_Foregrip_C'); the table is keyed by what the EXPERIMENT asked
# for ('vert_grip'). Those are two names for one part and the catalogue is the
# only place that knows it.
_ASSET_TO_KEY = {v['asset']: k for k, v in ATTACHMENTS.items() if v.get('asset')}


def worn_keys(muzzle, grip, stock):
    """{slot: catalogue key} for what is fitted, or None if a part is unknown.

    ⚠ AN UNRECOGNISED ASSET POISONS THE WHOLE ANSWER, on purpose. A part this
    build cannot name is a part whose contribution is unknown, and answering
    with the factor for "the kit minus that part" would be a confident wrong
    number -- exactly the failure these tables exist to end.
    """
    worn = {}
    for slot, asset in (('muzzle', muzzle), ('grip', grip), ('stock', stock)):
        if not asset:
            continue
        key = _ASSET_TO_KEY.get(asset)
        if key is None:
            return None
        worn[slot] = key
    return worn


def _row(per_weapon, posture, key):
    """A row for this posture, else standing, and never a derived one.

    Posture is a level of the table because the kit factor genuinely moves with
    it (measured 3.7-8.5 sigma, and with OPPOSITE SIGNS on two ARs). Falling
    back to standing keeps the old assumption -- no interaction -- rather than
    inventing one, and is never worse than the product path.

    ⚠ A DERIVED ROW IS A MISS. The table used to be filled out by multiplying
    per-slot factors for every combination nobody had fired, tagged
    src='derived' -- and that tag protected a reader of the FILE while the
    runtime read row['f'] either way, so 267 products were being used exactly
    like 28 measurements. The generator stopped emitting them on 2026-08-06
    ("配件表不要正交。全部死记住，因为可能有耦合"); this is here so an older
    kit_factors.json on disk cannot quietly put them back.
    """
    row = (per_weapon.get(posture, {}).get(key)
           or per_weapon.get('standing', {}).get(key))
    if not row or row.get('src') not in (None, 'measured'):
        return None
    return row


def part_factor(gun_name, slot, part_key, posture='standing'):
    """What this ONE part does on THIS gun, measured. None if nobody measured it.

    The second table. MUZZLE_FACTOR and GRIP_FACTOR answer the same question
    globally, and globally is the part that is wrong: comp_smg measures 0.5907
    on the mp5k and 0.7197 on the vector, 5.5 sigma apart, against one wiki
    number of 0.85 for both. Whole-gun scale cannot absorb that -- it is a
    property of the part on the gun, not of the gun.
    """
    per_weapon = _part_factors.get(gun_name)
    if not per_weapon:
        return None
    row = _row(per_weapon, posture, f'{slot}={part_key}')
    return row['f'] if row else None


def measured_kit_factor(gun_name, posture, muzzle='', grip='', stock=''):
    """The measured factor for this exact kit, or None if nobody measured it.

    None is not "no effect" -- it means fall back to the product, which the
    caller does. Returning 1.0 here would silently claim the parts do nothing.

    ⚠ AN UNRECOGNISED ASSET MAKES THE WHOLE LOOKUP MISS, on purpose. A part
    this build cannot name is a part whose contribution is unknown, and
    answering with the factor for "the kit minus that part" would be a
    confident wrong number -- exactly the failure this table exists to end.
    """
    per_weapon = _kit_factors.get(gun_name)
    if not per_weapon:
        return None
    worn = worn_keys(muzzle, grip, stock)
    if worn is None:
        return None
    kit = '+'.join(sorted(f'{s}={worn[s]}' for s in _KIT_SLOTS if s in worn))
    if not kit:
        return None                     # bare IS the denominator, always 1.0
    row = _row(per_weapon, posture, kit)
    return row['f'] if row else None


def attachment_factor(gun_name, muzzle='', grip='', stock='',
                      posture='standing'):
    """Current recoil factor based on equipped attachments.

    muzzle / grip / stock: attachment_detector class names, or ''
    """
    return explain_factor(gun_name, muzzle, grip, stock, posture)[0]


def explain_factor(gun_name, muzzle='', grip='', stock='',
                   posture='standing'):
    """-> (factor, source, detail). Same number, plus WHERE it came from.

    THREE TIERS, best evidence first, and each one is a strictly weaker claim
    than the one above it:

      'kit'    this exact kit was fired on this gun in this posture. Coupling
               included, because the parts were all on the gun at the time.
      'parts'  each part measured on THIS gun, multiplied. Assumes the slots
               do not interact -- which is false, measurably: on the mp5k the
               product of the measured singles lands 7.9% off the measured
               whole kit. It is still the best available answer for a kit
               nobody has fired, because the error it carries is coupling only.
      'wiki'   one global number per part for every gun in the game. Wrong in
               a second and larger way: comp_smg is 0.5907 on the mp5k and
               0.7197 on the vector, 5.5 sigma apart, and the wiki says 0.85
               for both. Only reached for a gun with no measurements at all.

    `detail` names the per-slot numbers on the 'parts' tier, so a run can
    record which parts it actually knew about.

    ⚠ THE WIKI TIER IGNORES `stock`, and that is deliberate rather than
    pending. There is no wiki stock coefficient worth multiplying in:
    tactical_stock states -20% and measures 1.00 +- 0.01 (~25 sigma), so the
    stated numbers for that slot have no demonstrated relationship to what
    this project measures. Stock is applied where it has been MEASURED and
    nowhere else.
    """
    got = measured_kit_factor(gun_name, posture, muzzle, grip, stock)
    if got is not None:
        return got, 'kit', {}

    # ── tier 2: this gun's own single-part measurements ──
    worn = worn_keys(muzzle, grip, stock)
    if worn:
        per_slot, any_measured = {}, False
        for slot in _KIT_SLOTS:
            if slot not in worn:
                continue
            f = part_factor(gun_name, slot, worn[slot], posture)
            if f is None:
                per_slot[slot] = None
            else:
                per_slot[slot], any_measured = f, True
        if any_measured:
            # Mixed on purpose when only some slots are measured: this gun's
            # own comp_smg at 0.5907 beside a wiki grip beats falling all the
            # way back to a wiki muzzle at 0.85, which is 44% off on that part
            # alone. `detail` carries the None so the mixture is visible rather
            # than being averaged into a single confident number.
            f = 1.0
            for slot, measured in per_slot.items():
                f *= measured if measured is not None else \
                    _wiki_slot(gun_name, slot, muzzle, grip)
            return f, 'parts', per_slot

    # ── tier 3: the global wiki coefficients ──
    return (_wiki_slot(gun_name, 'muzzle', muzzle, grip)
            * _wiki_slot(gun_name, 'grip', muzzle, grip)), 'wiki', {}


def _wiki_slot(gun_name, slot, muzzle, grip):
    """The global coefficient for one slot, or 1.0. See explain_factor tier 3
    for why this is the last resort rather than the model."""
    slots = WEAPON_SLOTS.get(gun_name)
    if not slots:
        return 1.0
    if slot == 'muzzle' and slots['muzzle'] == 'comp' and muzzle:
        for key, factor in MUZZLE_FACTOR.items():
            if key in muzzle:
                return factor
    if slot == 'grip' and slots['grip'] and grip:
        for key, factor in GRIP_FACTOR.items():
            if key in grip:
                return factor
    return 1.0
