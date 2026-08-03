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

from detector.attachment_catalog import SLOTS as CATALOG_SLOTS, compatible, has_slot

# Muzzle vertical recoil multipliers
MUZZLE_FACTOR = {
    'Compensator': 0.85,
    'FlashHider': 0.95,
    'Suppressor': 1.0,
}

# Grip vertical recoil multipliers
GRIP_FACTOR = {
    'Foregrip': 0.85,       # Vertical Foregrip
    'HalfGrip': 0.92,
    'ThumbGrip': 0.95,
    'AngledForeGrip': 1.0,
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


def attachment_factor(gun_name, muzzle='', grip=''):
    """Current recoil factor based on equipped attachments.

    muzzle: attachment_detector class name or ''
    grip: attachment_detector class name or ''
    """
    slots = WEAPON_SLOTS.get(gun_name)
    if not slots:
        return 1.0
    f = 1.0
    # Muzzle
    if slots['muzzle'] == 'comp' and muzzle:
        for key, factor in MUZZLE_FACTOR.items():
            if key in muzzle:
                f *= factor
                break
    # Grip
    if slots['grip'] and grip:
        for key, factor in GRIP_FACTOR.items():
            if key in grip:
                f *= factor
                break
    return f
