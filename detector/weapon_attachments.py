"""Per-weapon recoil factors from the muzzle and grip that are fitted.

This table exists for one job: turning weapon_scales.json's calibrated numbers
into a scale for whatever is actually on the gun. It only models the two slots
that change vertical recoil. For the full picture — all five slots, every
attachment, and which weapons are still in the game — see
detector/attachment_catalog.py, which is the authority; anything here that
disagrees with it is this file being out of date.

Weapons removed from the game in the June 2026 update (42.1) are gone from
WEAPON_SLOTS: qbu, pp19, dp28. Their measured recoil curves are preserved under
"_vaulted" in press/weapon_scales.json rather than deleted.
Recoil factors are from PUBG Wiki (pubg.wiki.gg):
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

WEAPON_SLOTS = {
    # ── AR ─────────────────────────────────────────
    'akm':    {'muzzle': 'comp', 'grip': False},
    'm416':   {'muzzle': 'comp', 'grip': True},
    'scar':   {'muzzle': 'comp', 'grip': True},
    'm762':   {'muzzle': 'comp', 'grip': True},
    'aug':    {'muzzle': 'comp', 'grip': True},
    'qbz':    {'muzzle': 'comp', 'grip': True},
    'g36c':   {'muzzle': 'comp', 'grip': True},
    'm16':    {'muzzle': 'comp', 'grip': False},
    'mk47':   {'muzzle': 'comp', 'grip': True},
    'groza':  {'muzzle': 'supp_only', 'grip': False},
    'k2':     {'muzzle': 'comp', 'grip': False},
    'ace32':  {'muzzle': 'comp', 'grip': True},
    'famas':  {'muzzle': 'comp', 'grip': False},

    # ── SMG ────────────────────────────────────────
    'tommy':  {'muzzle': 'supp_only', 'grip': True},   # suppressor only + vertical only
    'uzi':    {'muzzle': 'comp', 'grip': False},
    'ump45':  {'muzzle': 'comp', 'grip': True},
    'vector': {'muzzle': 'comp', 'grip': True},
    'mp5k':   {'muzzle': 'comp', 'grip': True},
    'p90':    {'muzzle': None, 'grip': False},          # built-in suppressor, no attachments
    'mp9':    {'muzzle': 'comp', 'grip': False},
    'js9':    {'muzzle': 'comp', 'grip': True},

    # ── MG ─────────────────────────────────────────
    'm249':   {'muzzle': None, 'grip': False},
    'mg3':    {'muzzle': None, 'grip': False},

    # ── DMR ────────────────────────────────────────
    'vss':    {'muzzle': None, 'grip': False},          # built-in suppressor + scope
    'mk14':   {'muzzle': 'comp', 'grip': False},
    'sks':    {'muzzle': 'comp', 'grip': True},
    'mini14': {'muzzle': 'comp', 'grip': False},
    'slr':    {'muzzle': 'comp', 'grip': False},
    'mk12':   {'muzzle': 'comp', 'grip': True},         # lower rail, like the SKS
    'dragunov': {'muzzle': 'comp', 'grip': False},
}


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
