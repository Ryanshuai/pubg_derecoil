"""What exists in the game right now, and what fits on what.

Needed by the Tab-screen drag-and-drop: dragging an attachment onto a slot the
weapon does not have leaves the item mid-air / drops it back, so the target
must be known-good *before* the mouse moves.

Two independent things live here, kept apart on purpose because they have very
different confidence:

  ROSTER / ATTACHMENTS  — measured. Read straight off the training-range item
      spawner on 2026-08-01 (temp_debug/spawner_scrape/20260801_210656), which
      is by definition the live build's item list.

  SLOTS                 — measured. All 30 live weapons scanned in game on
      2026-08-02 by calibration/scan_compat.py: spawn, open Tab, read each
      slot's tile with SlotDetector. Run and per-weapon captures in
      docs/compat/runs/20260802_155222/, so any row can be rechecked offline.

      It overturned two wiki/guess entries, both of the dangerous kind —
      a slot the table claimed and the weapon does not have, which makes a
      drag silently drop the part on the floor:
          ump45  had 'stock'   — no tile (ring 15.8 vs threshold 36)
          js9    had 'grip'    — no tile (ring 18.6)
      The other 28 agreed exactly, including all six that unverified() had
      flagged, and the AUG's slot list.

      ⚠ THE 'scope' ENTRIES ARE STILL INFERRED. That slot draws no tile at
      all, so presence is unreadable from a capture — an empty scope and an
      absent one are pixel-identical, and a weapon with an integral optic
      (VSS) renders it as part of the weapon art. SlotDetector returns
      'unknown' there and the scan cannot confirm or deny it. Resolve by
      fitting a sight; see the calibrate-compat skill.

  EXCLUDE / ONLY /      — inferred, still. Which attachments a PRESENT slot
  GRIP_ONLY               accepts cannot be read off a tile: Tommy Gun's
      muzzle takes a suppressor and refuses a compensator, and both leave an
      identical empty tile. These need drags, one per part.

Re-run after any patch: pixi run python calibration/scan_compat.py
"""

# ════════════════════════════════════════════════════════════
# Weapons currently in the game
#
# Measured: the spawner's 突击步枪 / 射手步枪 / 冲锋枪 / 轻机枪 categories.
# ════════════════════════════════════════════════════════════

ROSTER = {
    # key        class   spawner label
    'akm':      ('AR',  'AKM'),
    'm762':     ('AR',  'Beryl M762'),
    'g36c':     ('AR',  'G36C'),
    'm416':     ('AR',  'M416'),
    'm16':      ('AR',  'M16A4'),
    'scar':     ('AR',  'SCAR-L'),
    'mk47':     ('AR',  'Mk47 Mutant'),
    'qbz':      ('AR',  'QBZ'),
    'aug':      ('AR',  'AUG'),
    'groza':    ('AR',  'Groza'),
    'k2':       ('AR',  'K2'),
    'ace32':    ('AR',  'ACE32'),
    'famas':    ('AR',  'FAMAS'),

    'slr':      ('DMR', '自动装填步枪'),
    'mini14':   ('DMR', 'Mini14'),
    'sks':      ('DMR', 'SKS'),
    'vss':      ('DMR', 'VSS'),
    'mk14':     ('DMR', 'Mk14'),
    'mk12':     ('DMR', 'Mk12'),
    'dragunov': ('DMR', '德拉贡诺夫'),

    'tommy':    ('SMG', '汤姆逊冲锋枪'),
    'ump45':    ('SMG', 'UMP'),
    'uzi':      ('SMG', 'Micro UZI 冲锋枪'),
    'vector':   ('SMG', 'Vector'),
    'mp5k':     ('SMG', 'MP5K'),
    'p90':      ('SMG', 'P90'),
    'mp9':      ('SMG', 'MP9'),
    'js9':      ('SMG', 'JS9'),

    'm249':     ('LMG', 'M249'),
    'mg3':      ('LMG', 'MG3'),

    # 狙击步枪, col1_row02, in the order the submenu draws them. Read off
    # docs/spawner/runs/20260801_210656/col1_row02_submenu.png — the scrape is
    # the source of truth for what the game currently offers, and the order is
    # what give_many's entry index counts.
    'kar98k':   ('SR',  'Kar98k'),
    'm24':      ('SR',  'M24'),
    'awm':      ('SR',  'AWM'),
    'win94':    ('SR',  'Win94'),
    'lynx':     ('SR',  'Lynx AMR'),

    # 霰弹枪, col1_row04, same source, same ordering rule.
    's686':     ('SG',  'S686'),
    's12k':     ('SG',  'S12K'),
    's1897':    ('SG',  'S1897'),
    'dbs':      ('SG',  'DBS'),
    'o12':      ('SG',  'O12'),
}

# Permanently removed in the June 2026 update (42.1). Kept as a named set
# rather than deleted outright so the recoil curves measured for dp28/pp19 are
# not lost if Krafton brings them back through an event mode — nothing should
# ever *select* one, which is what `is_live()` is for.
VAULTED = {
    'dp28':   'LMG, removed 2026-06 (U42.1)',
    'pp19':   'SMG, removed 2026-06 (U42.1)',
    'qbu':    'DMR, removed 2026-06 (U42.1)',
    'mosin':  'SR,  removed 2026-06 (U42.1)',
    'r45':    'handgun, removed 2026-06 (U42.1)',
    'p1911':  'handgun, removed 2026-06 (U42.1)',
}

# ════════════════════════════════════════════════════════════
# Attachments currently in the game
#
# Measured: the spawner's 握把 / 弹匣 / 枪口 / 枪托 / 瞄准镜 categories, in
# spawner order. `asset` is the AttachmentDetector template stem, so a drag can
# be verified by classifying the slot afterwards; None means this repo has no
# template yet and the slot can only be checked as non-empty.
#
# `classes` is the weapon-class list the game itself prints in the item name —
# e.g. 消焰器 (突击步枪、精确射手步枪、O12、S12K). That parenthesis is the
# game's own compatibility statement and is the most trustworthy field here.
#
# THE ORDER IS LOAD-BEARING. Within each slot this is the spawner submenu's own
# order, and control/spawner.py counts through it to click entry N — so a
# new attachment goes where the game puts it, not at the end. Items the game
# lists but nothing here can equip (箭袋, the crossbow quiver, sits in 握把
# between 垂直握把 and 斜向握把) are NOT added here; they live in that module's
# SPAWNER_EXTRAS, which keeps this file about what fits on what. Check both
# against the captured menus with `control/spawner.py --check`.
# ════════════════════════════════════════════════════════════

ATTACHMENTS = {
    # ── 握把 (grip / lower rail) ──
    'half_grip':      {'slot': 'grip', 'zh': '半截式握把',
                       'asset': 'Lower_HalfGrip_C', 'classes': ('AR', 'DMR', 'SMG')},
    'laser':          {'slot': 'grip', 'zh': '激光瞄准器',
                       'asset': 'Lower_LaserPointer_C', 'classes': ('AR', 'DMR', 'SMG')},
    'light_grip':     {'slot': 'grip', 'zh': '轻型握把',
                       'asset': 'Lower_LightweightForeGrip_C', 'classes': ('AR', 'DMR', 'SMG')},
    'thumb_grip':     {'slot': 'grip', 'zh': '拇指握把',
                       'asset': 'Lower_ThumbGrip_C', 'classes': ('AR', 'DMR', 'SMG')},
    'vert_grip':      {'slot': 'grip', 'zh': '垂直握把',
                       'asset': 'Lower_Foregrip_C', 'classes': ('AR', 'DMR', 'SMG')},
    'angled_grip':    {'slot': 'grip', 'zh': '斜向握把',
                       'asset': 'Lower_AngledForeGrip_C', 'classes': ('AR', 'DMR', 'SMG')},

    # ── 弹匣 (magazine) ──
    'quickext_smg':   {'slot': 'magazine', 'zh': '加长快速弹匣 (手枪, 冲锋枪)',
                       'asset': 'Magazine_ExtendedQuickDraw_Medium_C', 'classes': ('SMG',)},
    'quickext_ar':    {'slot': 'magazine', 'zh': '加长快速弹匣 (突击步枪、精确射手步枪、M249、S12K)',
                       'asset': 'Magazine_ExtendedQuickDraw_Large_C', 'classes': ('AR', 'DMR', 'LMG')},
    'quickext_sr':    {'slot': 'magazine', 'zh': '加长快速弹匣 (精确射手步枪、狙击步枪)',
                       'asset': 'Magazine_ExtendedQuickDraw_SniperRifle_C', 'classes': ('DMR', 'SR')},
    'ext_smg':        {'slot': 'magazine', 'zh': '扩容弹匣 (手枪, 冲锋枪)',
                       'asset': 'Magazine_Extended_Medium_C', 'classes': ('SMG',)},
    'ext_ar':         {'slot': 'magazine', 'zh': '扩容弹匣 (突击步枪、精确射手步枪、M249、S12K)',
                       'asset': 'Magazine_Extended_Large_C', 'classes': ('AR', 'DMR', 'LMG')},
    'ext_sr':         {'slot': 'magazine', 'zh': '扩容弹匣 (精确射手步枪、狙击步枪)',
                       'asset': 'Magazine_Extended_SniperRifle_C', 'classes': ('DMR', 'SR')},
    'quick_smg':      {'slot': 'magazine', 'zh': '快速弹匣 (手枪, 冲锋枪)',
                       'asset': 'Magazine_QuickDraw_Medium_C', 'classes': ('SMG',)},
    'quick_ar':       {'slot': 'magazine', 'zh': '快速弹匣 (突击步枪、精确射手步枪、M249、S12K)',
                       'asset': 'Magazine_QuickDraw_Large_C', 'classes': ('AR', 'DMR', 'LMG')},
    'quick_sr':       {'slot': 'magazine', 'zh': '快速弹匣 (精确射手步枪、狙击步枪)',
                       'asset': 'Magazine_QuickDraw_SniperRifle_C', 'classes': ('DMR', 'SR')},

    # ── 枪口 (muzzle) ──
    'choke':          {'slot': 'muzzle', 'zh': '扼流圈 (霰弹枪)',
                       'asset': 'Muzzle_Choke_C', 'classes': ('SG',)},
    'duckbill':       {'slot': 'muzzle', 'zh': '鸭嘴枪口 (霰弹枪)',
                       'asset': 'Muzzle_Duckbill_C', 'classes': ('SG',)},
    'comp_smg':       {'slot': 'muzzle', 'zh': '枪口补偿器 (冲锋枪)',
                       'asset': 'Muzzle_Compensator_Medium_C', 'classes': ('SMG',)},
    'comp_ar':        {'slot': 'muzzle', 'zh': '后坐补偿器 (突击步枪、精确射手步枪、O12、S12K)',
                       'asset': 'Muzzle_Compensator_Large_C', 'classes': ('AR', 'DMR')},
    'comp_sr':        {'slot': 'muzzle', 'zh': '后坐补偿器 (精确射手步枪、狙击步枪)',
                       'asset': 'Muzzle_Compensator_SniperRifle_C', 'classes': ('DMR', 'SR')},
    'flash_smg':      {'slot': 'muzzle', 'zh': '消焰器 (冲锋枪)',
                       'asset': 'Muzzle_FlashHider_Medium_C', 'classes': ('SMG',)},
    'flash_ar':       {'slot': 'muzzle', 'zh': '消焰器 (突击步枪、精确射手步枪、O12、S12K)',
                       'asset': 'Muzzle_FlashHider_Large_C', 'classes': ('AR', 'DMR')},
    'flash_sr':       {'slot': 'muzzle', 'zh': '消焰器 (精确射手步枪、狙击步枪)',
                       'asset': 'Muzzle_FlashHider_SniperRifle_C', 'classes': ('DMR', 'SR')},
    'supp_smg':       {'slot': 'muzzle', 'zh': '消音器 (手枪, 冲锋枪)',
                       'asset': 'Muzzle_Suppressor_Medium_C', 'classes': ('SMG',)},
    'supp_ar':        {'slot': 'muzzle', 'zh': '消音器 (突击步枪、精确射手步枪、O12、S12K)',
                       'asset': 'Muzzle_Suppressor_Large_C', 'classes': ('AR', 'DMR')},
    'supp_sr':        {'slot': 'muzzle', 'zh': '消音器 (精确射手步枪、狙击步枪)',
                       'asset': 'Muzzle_Suppressor_SniperRifle_C', 'classes': ('DMR', 'SR')},
    # Added after this repo's template pack was built — no icon to match against.
    'brake_ar':       {'slot': 'muzzle', 'zh': '枪口制退器 (突击步枪、精确射手步枪、O12)',
                       'asset': None, 'classes': ('AR', 'DMR')},

    # ── 枪托 (stock) ──
    'heavy_stock':    {'slot': 'stock', 'zh': '重型枪托 (冲锋枪, 突击步枪, M249)',
                       'asset': None, 'classes': ('SMG', 'AR', 'LMG')},
    'tactical_stock': {'slot': 'stock', 'zh': '战术枪托 (冲锋枪, 突击步枪, M249)',
                       'asset': 'Stock_AR_Composite_C', 'classes': ('SMG', 'AR', 'LMG')},
    'bullet_loops':   {'slot': 'stock', 'zh': '子弹袋 (霰弹枪, 狙击步枪, Win94)',
                       'asset': 'Stock_SniperRifle_BulletLoops_C', 'classes': ('SG', 'SR')},
    'cheek_pad':      {'slot': 'stock', 'zh': '托腮板 (精确射手步枪、狙击步枪)',
                       'asset': 'Stock_SniperRifle_CheekPad_C', 'classes': ('DMR', 'SR')},
    'uzi_stock':      {'slot': 'stock', 'zh': '折叠式枪托 (蝎式手枪, Micro UZI 冲锋枪, MP9)',
                       'asset': 'Stock_UZI_C', 'classes': ('SMG',)},

    # ── 瞄准镜 (scope) ──
    'red_dot':        {'slot': 'scope', 'zh': '红点瞄准镜',
                       'asset': 'Upper_DotSight_01_C', 'classes': ('AR', 'DMR', 'SMG', 'LMG', 'SR', 'SG')},
    'holo':           {'slot': 'scope', 'zh': '全息瞄准镜',
                       'asset': 'Upper_Holosight_C', 'classes': ('AR', 'DMR', 'SMG', 'LMG', 'SR', 'SG')},
    'scope_2x':       {'slot': 'scope', 'zh': '2倍 瞄准镜',
                       'asset': 'Upper_Aimpoint_C', 'classes': ('AR', 'DMR', 'SMG', 'LMG', 'SR', 'SG')},
    'scope_3x':       {'slot': 'scope', 'zh': '3倍瞄准镜',
                       'asset': 'Upper_Scope3x_C', 'classes': ('AR', 'DMR', 'SMG', 'LMG', 'SR', 'SG')},
    'scope_4x':       {'slot': 'scope', 'zh': '4倍瞄准镜',
                       'asset': 'Upper_ACOG_01_C', 'classes': ('AR', 'DMR', 'SMG', 'LMG', 'SR', 'SG')},
    'scope_6x':       {'slot': 'scope', 'zh': '6倍瞄准镜',
                       'asset': 'Upper_Scope6x_C', 'classes': ('AR', 'DMR', 'SMG', 'LMG', 'SR', 'SG')},
    'scope_8x':       {'slot': 'scope', 'zh': '8倍瞄准镜',
                       'asset': 'Upper_CQBSS_C', 'classes': ('DMR', 'SR')},
    'scope_15x':      {'slot': 'scope', 'zh': '15倍瞄准镜',
                       'asset': 'Upper_PM2_01_C', 'classes': ('DMR', 'SR')},
    'variable':       {'slot': 'scope', 'zh': '多倍率混合瞄具',
                       'asset': None, 'classes': ('AR', 'DMR', 'SMG', 'LMG', 'SR')},
}

# ════════════════════════════════════════════════════════════
# Which slots each weapon actually has
#
# A weapon only renders the slots it owns, so this is what decides whether a
# drag has anywhere to land. 'scope' is omitted where the weapon has a fixed
# optic (VSS, P90).
#
# confidence:
#   'wiki'    — every slot backed by pubg.wiki.gg's compatibility lists
#   'guess'   — weapon absent from the wiki; slots inferred from its class and
#               from what the model shows. MUST be measured before trusting.
# ════════════════════════════════════════════════════════════

_ALL = ('scope', 'muzzle', 'grip', 'magazine', 'stock')

SLOTS = {
    # ── AR ──
    'akm':      {'slots': ('scope', 'muzzle', 'magazine'), 'conf': 'measured'},
    'm762':     {'slots': ('scope', 'muzzle', 'grip', 'magazine'), 'conf': 'measured'},
    'g36c':     {'slots': ('scope', 'muzzle', 'grip', 'magazine'), 'conf': 'measured'},
    'm416':     {'slots': _ALL, 'conf': 'measured'},
    'm16':      {'slots': ('scope', 'muzzle', 'magazine', 'stock'), 'conf': 'measured'},
    'scar':     {'slots': ('scope', 'muzzle', 'grip', 'magazine'), 'conf': 'measured'},
    'mk47':     {'slots': _ALL, 'conf': 'measured'},
    'qbz':      {'slots': ('scope', 'muzzle', 'grip', 'magazine'), 'conf': 'measured'},
    'aug':      {'slots': ('scope', 'muzzle', 'grip', 'magazine'), 'conf': 'measured'},
    'groza':    {'slots': ('scope', 'muzzle', 'magazine'), 'conf': 'measured'},
    'ace32':    {'slots': _ALL, 'conf': 'measured'},
    # K2's wiki page states 3 attachment points: sight, muzzle, magazine.
    'k2':       {'slots': ('scope', 'muzzle', 'magazine'), 'conf': 'measured'},
    'famas':    {'slots': ('scope', 'muzzle', 'magazine'), 'conf': 'measured'},

    # ── DMR ──
    'slr':      {'slots': ('scope', 'muzzle', 'magazine', 'stock'), 'conf': 'measured'},
    'mini14':   {'slots': ('scope', 'muzzle', 'magazine'), 'conf': 'measured'},
    'sks':      {'slots': _ALL, 'conf': 'measured'},
    'mk14':     {'slots': ('scope', 'muzzle', 'magazine', 'stock'), 'conf': 'measured'},
    # Fixed PSO-1 optic and integral suppressor; only mag + cheek pad are free.
    'vss':      {'slots': ('magazine', 'stock'), 'conf': 'measured'},
    # Read off docs/tab_inventory_2.png, which has an Mk12 in slot 2: scope,
    # muzzle, grip and magazine boxes are drawn, the stock position is blank.
    'mk12':     {'slots': ('scope', 'muzzle', 'grip', 'magazine'), 'conf': 'measured'},
    'dragunov': {'slots': ('scope', 'muzzle', 'magazine', 'stock'), 'conf': 'measured'},

    # ── SMG ──
    # No stock slot: its tile is simply not drawn (ring 15.8 against a
    # threshold of 36, while its other three tiles read 96..120). The wiki
    # listed one. Scanned 2026-08-02.
    'ump45':    {'slots': ('scope', 'muzzle', 'grip', 'magazine'), 'conf': 'measured'},
    # docs/tab_inventory_2.png, slot 1: scope/muzzle/magazine/stock drawn, the
    # grip position blank — which is also what confirms an unowned slot is
    # simply not rendered rather than shifted away.
    'uzi':      {'slots': ('scope', 'muzzle', 'magazine', 'stock'), 'conf': 'measured'},
    'vector':   {'slots': _ALL, 'conf': 'measured'},
    'mp5k':     {'slots': _ALL, 'conf': 'measured'},
    # Suppressor-only muzzle, vertical-only grip.
    'tommy':    {'slots': ('scope', 'muzzle', 'grip', 'magazine'), 'conf': 'measured'},
    # Integral holo + laser + suppressor, none removable.
    'p90':      {'slots': ('magazine',), 'conf': 'measured'},
    # Non-replaceable laser sight and silencer as standard.
    'mp9':      {'slots': ('scope', 'magazine', 'stock'), 'conf': 'measured'},
    # No grip slot (ring 18.6) and no stock (14.2). The grip was a guess,
    # since the wiki has no JS9 page at all. Scanned 2026-08-02.
    'js9':      {'slots': ('scope', 'muzzle', 'magazine'), 'conf': 'measured'},

    # ── LMG ──
    # The AR magazines name the M249 outright — 快速弹匣 (突击步枪、精确射手步
    # 枪、M249、S12K) — so it has a magazine slot the wiki's per-attachment
    # lists omit.
    'm249':     {'slots': ('scope', 'magazine', 'stock'), 'conf': 'measured'},
    'mg3':      {'slots': ('scope',), 'conf': 'measured'},
}

# Per-weapon attachments that its class allows but this weapon rejects.
# The AUG entry is a *known conflict*: pubg.wiki.gg lists the AUG A3 only under
# Suppressor (AR, DMR, S12K) and not under Compensator or Flash Hider, while
# this repo's older weapon_attachments.WEAPON_SLOTS has had it as 'comp' since
# the recoil scales were calibrated. Left permissive here (no exclusion) and
# flagged by unverified() — one drag settles it.
EXCLUDE = {
    'groza': {'comp_ar', 'flash_ar', 'brake_ar'},   # suppressor is its only muzzle
    'tommy': {'comp_smg', 'flash_smg'},             # suppressor only
}

# The other direction: an attachment whose class list is wider than the set of
# weapons that actually take it. The game prints these in the item name, which
# is where each of these comes from.
ONLY = {
    'uzi_stock': {'uzi', 'mp9'},   # 折叠式枪托 (蝎式手枪, Micro UZI 冲锋枪, MP9)
}

# Grips a weapon can take when it does not take all of them.
GRIP_ONLY = {
    'tommy': {'vert_grip'},        # vertical foregrip is the only one it fits
}


# ════════════════════════════════════════════════════════════
# Queries
# ════════════════════════════════════════════════════════════

def is_live(weapon):
    """False for weapons removed from the game."""
    return weapon in ROSTER and weapon not in VAULTED


def weapon_class(weapon):
    entry = ROSTER.get(weapon)
    return entry[0] if entry else None


def has_slot(weapon, slot):
    entry = SLOTS.get(weapon)
    return bool(entry) and slot in entry['slots']


def fits(weapon, att_key):
    """Can `weapon` equip attachment `att_key`?

    Three gates, cheapest first: the weapon must be in the game, must own the
    slot, and must be in the attachment's class list minus this weapon's
    exclusions.
    """
    att = ATTACHMENTS.get(att_key)
    if not att or not is_live(weapon):
        return False
    if not has_slot(weapon, att['slot']):
        return False
    if weapon_class(weapon) not in att['classes']:
        return False
    if att_key in EXCLUDE.get(weapon, ()):
        return False
    if att_key in ONLY and weapon not in ONLY[att_key]:
        return False
    if att['slot'] == 'grip' and weapon in GRIP_ONLY:
        return att_key in GRIP_ONLY[weapon]
    return True


def compatible(weapon):
    """{slot: [att_key, ...]} for everything this weapon can take."""
    out = {}
    for key, att in ATTACHMENTS.items():
        if fits(weapon, key):
            out.setdefault(att['slot'], []).append(key)
    return out


def unverified():
    """Weapons whose slot list is still a guess.

    'shot' outranks 'wiki': the slot boxes were read straight off a capture of
    that weapon equipped, which is the game itself answering.
    """
    return sorted(w for w, e in SLOTS.items() if e['conf'] == 'guess')


if __name__ == '__main__':
    live = [w for w in ROSTER if is_live(w)]
    print(f'{len(live)} live weapons, {len(ATTACHMENTS)} attachments, '
          f'{len(VAULTED)} vaulted')
    print(f'unverified slot lists: {", ".join(unverified())}')
    for w in live:
        c = compatible(w)
        n = sum(len(v) for v in c.values())
        print(f'  {w:9s} {weapon_class(w):3s} {n:3d} fits  '
              f'slots={",".join(SLOTS[w]["slots"]) if w in SLOTS else "?"}')
