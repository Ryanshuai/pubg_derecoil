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
                                         compatible, has_slot, weapon_class)

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

    Takes EITHER namespace: an asset class name as the detector read it off the
    tile ('Stock_SniperRifle_CheekPad_C'), or a catalogue key as the experiment
    asked for it ('cheek_pad'). Assets are translated; keys pass through.

    ⚠ AN UNRECOGNISED PART POISONS THE WHOLE ANSWER, on purpose. A part this
    build cannot name is a part whose contribution is unknown, and answering
    with the factor for "the kit minus that part" would be a confident wrong
    number -- exactly the failure these tables exist to end.

    ⚠ ACCEPTING BOTH IS NOT LENIENCY, IT IS THE BUG THIS FUNCTION CAUSED. It was
    added for the RUNTIME, where `Weapon.muzzle` holds an asset, and the
    calibration path feeds the same field from `read_config`, whose docstring
    says "as catalog keys". So every collection run on a gun wearing a muzzle,
    grip or stock started answering None -- NOT compensating, on the one path
    that exists to measure the compensation.

    Measured 2026-08-10, a vss cheek-pad run: `[attach] gun1.stock=
    Stock_SniperRifle_CheekPad_C` (the drag, in assets) then `fitted: {'stock':
    'cheek_pad'}` (the readback, in keys) then `a fitted part has no catalogue
    name (stock='cheek_pad')` -- and ZERO magazines fired, because the firmware
    had no pattern and collect_timed refuses to store a magazine whose y_comp is
    unknown. That refusal is the only reason this cost a run and not a corpus.

    It hid because today's other runs were all deliberately stripped to bare,
    where every field is '' and the loop never looks anything up. m416's 56
    kitted magazines predate the function.

    ⚠ THE TWO NAMESPACES CANNOT COLLIDE, which is what makes this safe rather
    than a guess: assets are `Upper_/Lower_/Muzzle_/Stock_/Magazine_`-prefixed
    class names ending `_C`, keys are lower_snake ('comp_ar', 'cheek_pad').
    Anything in neither table still returns None.
    """
    worn = {}
    for slot, part in (('muzzle', muzzle), ('grip', grip), ('stock', stock)):
        if not part:
            continue
        key = _ASSET_TO_KEY.get(part)
        if key is None and part in ATTACHMENTS:
            key = part                      # already a catalogue key
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


# ── the SMG grip+muzzle floor on the 'parts' tier ─────────────────────────
# Multiplying single-slot factors assumes the slots do not interact. On the two
# measured SMGs that fails in ONE DIRECTION -- the product is always too small,
# worst on the fullest kit -- and it fails on ONE EDGE. Modelling grip and
# muzzle as acting on the REDUCIBLE recoil only, with a floor nothing gets
# under, while the stock keeps multiplying:
#
#   R = [R_min + (R_bare - R_min) * PROD_{grip,muzzle} (R_j - R_min)
#                                 / (R_bare - R_min)] * PROD_{other} f_j
#
# WHICH SLOTS, fitted per subset on the 8 measured SMG cells, one parameter
# each. The three single-slot rows come out EXACTLY equal to pure
# multiplication, which is the arithmetic self-check: a lone slot inside the
# floor collapses to its own factor.
#
#   floor acts on          chi2   chi2/dof   R_min    mean|err|   worst
#   grip+muzzle           133.5     19.07    346.1      3.30%     6.81%
#   grip+muzzle+stock     162.1     23.16    240.4      3.19%     6.15%
#   grip+stock            367.7     52.53    531.4      5.27%    12.00%
#   muzzle+stock          494.1     70.58    251.1      6.19%    10.11%
#   any single slot       581.8     83.11       -       6.73%    14.50%
#   none (multiplied)     581.8     72.72       -       6.73%    14.50%
#
# ⚠ grip+muzzle IS ALSO WHAT A SEPARATE MEASUREMENT SAYS, and that is the only
# reason this is a slot list rather than a fitted knob: the per-edge coupling
# computed straight from the residuals has mp5k's grip x muzzle at +4.4 sigma
# while grip x stock is -2.0 and muzzle x stock is -0.7. The stock does not
# couple there, and that was known before this fit existed.
#
# ⚠ SMG ONLY. All 8 fitted cells are SMG. The AR side has exactly one two-slot
# cell (aug grip+muzzle) and on it the floor LOSES: 2.90% multiplied against
# 4.74% floored, and that is the SMG fit applied cold. R_min is a count, so it is
# not even the same claim across families -- 346 counts is 34% of the vector's
# bare recoil and 19% of the aug's.
#
# ⚠ AND THE TWO SMGs DO NOT AGREE ON THE SLOT LIST. Fitted alone, mp5k picks
# grip+muzzle (chi2 34.0) and vector picks grip+muzzle+stock (chi2 36.0). The
# shipped list is the better of the two POOLED, not a settled fact. Nor has
# anything converged: chi2/dof 19 against a noise floor of ~1, and the two
# criteria disagree (chi2 prefers grip+muzzle, mean |err| prefers all three by
# 0.11 points). 8 cells on 2 guns cannot resolve this, so treat the slot list
# as one fit with no reproduction behind it.
#
# ⚠ ONE ATTACHMENT DOES NOT CARRY ONE PROPERTY ACROSS GUNS, and it was checked
# rather than assumed. For each part, the R_min that would make both guns agree
# on the fraction of reducible recoil it removes: muzzle 647.2, grip 528.0,
# stock 535.2 -- all three ABOVE the smallest measured kit (426 counts), where
# the reducible part goes negative, and three different values besides. So
# comp_smg reading 0.5907 on the mp5k and 0.7197 on the vector is not one
# property against two baselines.
#
# ⚠ WHAT IT IS WORTH TODAY: NOTHING, AND THAT IS MEASURED, NOT ARGUED.
#   1. Zero reach. Both measured SMGs have exactly three single-slot parts and
#      all 2^3 combinations are already FIRED rows in `kits`, so tier 1 answers
#      first every time. `pixi run kit-floor` prints the count; it goes
#      non-zero the first time an SMG gets a 4th measured part -- which is also
#      the measurement that would settle the slot list above.
#   2. Even reached, it never touches the firmware: explain_factor is not on
#      the compensation path (see its docstring). Its live caller prices
#      imported Kava4 seeds.
#
# Reproduce every number above, including the subset scan and the
# cross-validation:  pixi run kit-floor
_SMG_FLOOR_COUNTS = 346.1
# Fitted jointly with the constant; changing one without refitting the other is
# meaningless. `pixi run kit-floor` refits and fails if they drift apart.
_FLOOR_SLOTS = ('grip', 'muzzle')
# Slot factors are relative, so the product path never needs a bare. The floor
# does -- it is an absolute count -- and the only bare available is the one
# implied by each single-slot row (counts / f). Rows from different runs imply
# different bares, and averaging across them would silently mix two baselines.
_BARE_AGREE = 0.02


def _bare_counts(gun_name, posture, worn):
    """Bare counts this gun's own single-slot rows agree on, or None.

    None when the rows disagree by more than `_BARE_AGREE`: that means they are
    not measurements against a common baseline, and an absolute-count model has
    nothing to anchor to. The product path does not care and stays available --
    CLAUDE.md's second law, two readings that disagree means one is wrong, so
    refuse rather than average.
    """
    per_weapon = _part_factors.get(gun_name)
    if not per_weapon:
        return None
    bares = []
    for slot, key in worn.items():
        row = _row(per_weapon, posture, f'{slot}={key}')
        if not row or not row.get('f') or row.get('counts') is None:
            return None
        bares.append(row['counts'] / row['f'])
    if not bares:
        return None
    lo, hi = min(bares), max(bares)
    if lo <= 0 or (hi - lo) / lo > _BARE_AGREE:
        return None
    return sum(bares) / len(bares)


def _floor_factor(gun_name, posture, worn, per_slot):
    """The kit factor with grip+muzzle floored, or None if this does not apply.

    None -- meaning "use the plain product" -- unless every condition holds:
    the gun is an SMG, BOTH floor slots are filled (one alone collapses to its
    own factor, so there would be nothing to gain and a bare to get wrong),
    every filled slot is measured ON THIS GUN (a wiki number carries no counts,
    so there is nothing to subtract a floor from), and the single-slot rows
    agree on a bare. Slots outside `_FLOOR_SLOTS` multiply, untouched.
    """
    if weapon_class(gun_name) != 'SMG':
        return None
    if not all(s in per_slot for s in _FLOOR_SLOTS):
        return None
    # ⚠ THIS ONE CANNOT BE MADE TO FAIL, and that is why it says so here.
    # `f is None` holds exactly when that slot has no row, which is exactly
    # when _bare_counts returns None -- both go through _row on the same key.
    # Mutation-tested: deleting this line leaves every case green. It stays as
    # a TYPE guard, not a policy one: without it a None would reach the
    # arithmetic below as a TypeError if the two ever stop agreeing.
    if any(f is None for f in per_slot.values()):
        return None
    bare = _bare_counts(gun_name, posture, worn)
    if bare is None:
        return None
    span = bare - _SMG_FLOOR_COUNTS
    if span <= 0:
        return None
    floored, rest = 1.0, 1.0
    for slot, f in per_slot.items():
        if slot in _FLOOR_SLOTS:
            share = (bare * f - _SMG_FLOOR_COUNTS) / span
            if share <= 0:
                return None      # a part at or under the floor: outside the model
            floored *= share
        else:
            rest *= f
    return (_SMG_FLOOR_COUNTS + span * floored) / bare * rest


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

    ⚠ THIS IS NOT HOW A KITTED GUN GETS COMPENSATED. Under plan A the firmware
    curve is looked up by the EXACT configuration and emitted with no factor
    applied at all ('scaled_by: NOTHING') -- detector/weapon.set_seq says so,
    and the ten-line factor path that used to follow it was deleted on
    2026-08-09 for being unreachable code that read like policy. The live
    caller of this function is tools/import_kava4.py, which prices an IMPORTED
    community pattern for a gun nobody has measured yet; `pixi run kit-factors`
    is the other. Read the tiers below as "how good is this seed", not as the
    compensation path.

    FOUR TIERS, best evidence first, and each one is a strictly weaker claim
    than the one above it:

      'kit'    this exact kit was fired on this gun in this posture. Coupling
               included, because the parts were all on the gun at the time.
      'parts_floor'
               SMG only, and only with BOTH grip and muzzle fitted: this gun's
               own single-part measurements, with those two combined through a
               floor instead of multiplied (the stock still multiplies).
               chi2 133.5 against 581.8 for the plain product over the 8
               measured SMG cells. See _SMG_FLOOR_COUNTS for which slots, why
               only SMGs, and the three things it is NOT allowed to claim.
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
            # SMG with both grip and muzzle on -- see _SMG_FLOOR_COUNTS for
            # what it buys and what it cannot claim. A separate `source`
            # because a recorded number has to say which path produced it, not
            # just how good it is.
            floored = _floor_factor(gun_name, posture, worn, per_slot)
            if floored is not None:
                return floored, 'parts_floor', per_slot
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
