"""What exists in the game right now, and what fits on what.

Needed by the Tab-screen drag-and-drop: dragging an attachment onto a slot the
weapon does not have leaves the item mid-air / drops it back, so the target
must be known-good *before* the mouse moves.

Two independent things live here, kept apart on purpose because they have very
different confidence:

  ROSTER / ATTACHMENTS  — measured. Read straight off the training-range item
      spawner on 2026-08-01 (a scrape run on 2026-08-01, no longer on disk), which
      is by definition the live build's item list.

  SLOTS                 — measured, all 40. calibration/scan_compat.py:
      spawn, open Tab, read each slot's tile with SlotDetector. Captures in
      calibration/artifacts/compat/runs/20260802_155222/ (the first 30) and
      calibration/artifacts/runs/slot_scan/20260804_131048 + _131534 (the rest), so any row can
      be rechecked offline.

      The 2026-08-02 pass overturned two wiki/guess entries, both of the
      dangerous kind — a slot the table claimed and the weapon does not have,
      which makes a drag silently drop the part on the floor:
          ump45  had 'stock'   — no tile (ring 15.8 vs threshold 36)
          js9    had 'grip'    — no tile (ring 18.6)

      IT ALSO STOPPED AT 30 OF 40 AND WAS RECORDED AS COMPLETE. The ten it
      skipped were every shotgun and every bolt-action rifle, and fits() gates
      on SLOTS.get(weapon) — so those weapons rejected every attachment, and
      the three parts that only they can wear (choke, duckbill, bullet_loops)
      were reported by collect_templates as unwearable by anything alive. They
      are, not by coincidence, the only three attachments in this repository
      with no screen-solved template: the collector could not reach them. A
      hole in a compatibility table reads as a fact about the game. Filled
      2026-08-04.

  'scope' — HALF MEASURED NOW, and the halves are worth telling apart.

      SlotDetector cannot answer it for anyone: the scope position draws no
      tile, so an empty scope and an absent one are pixel-identical, and a
      weapon with an integral optic (VSS) renders it as weapon art. Every
      'scope' among the first thirty is therefore INFERRED.

      For the ten added on 2026-08-04 it was measured instead, and by letting
      the game answer rather than by dragging anything: PUBG bolts whatever
      the pack holds onto a gun the moment it spawns, so a sight sitting in
      the scope slot afterwards is the game confirming the slot exists.
      AttachmentDetector reads the CONTENT of that slot correctly even though
      SlotDetector cannot read its presence — it reads VSS's fixed PSO-1 as
      empty — so the whole check ran offline against the scan's stored Tab
      frames, at no cost in game time:

          kar98k m24 awm lynx   scope_6x mounted   mse 83   margin 8.3-9.1
          s12k dbs              red_dot mounted    mse 51   margin 15
          win94 s686 s1897 o12  nothing mounted    mse ~1900  margin 1.0

      The remaining four were settled the same day by making it a CONTROLLED
      test rather than an opportunistic one: four red dots put in the pack
      first, then each weapon spawned. o12 mounted one (mse 38, margin 17.2)
      and has the slot. win94, s686 and s1897 declined a sight that was
      demonstrably available — o12 came last and still found one — so for
      those three "no scope slot" is now a measurement and not an absence of
      evidence.

          o12                   red_dot mounted    mse   38   margin 17.2
          win94 s686 s1897      declined it        mse ~1800  margin  1.0

      win94 therefore has NO attachment slots at all, which is a real answer
      for a rifle with an integrated 2.7x.

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
    # calibration/artifacts/spawner/runs/20260801_210656/col1_row02_submenu.png — the scrape is
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

# DP-28, PP-19 Bizon, QBU, Mosin, R45 and P1911 were removed from the game in
# the June 2026 update (42.1). They used to live here in a VAULTED set beside
# their measured curves, gated by an is_live() that every selector called.
#
# All of it is gone as of 2026-08-04, on the reading that a half-present
# weapon misleads more than an absent one: ROSTER is what everything expands
# and plans against, so a name in this file reads as "the spawner can produce
# this" no matter what a second table says about it, and the second table is
# the part a reader skips. The curves went with them — they describe a gun
# that cannot be spawned, so nothing can check them and nothing can use them.
#
# ROSTER IS THE SPAWNER'S ROW ORDER, not just a list of names, which is the
# reason the deletion has to be all-or-nothing: control/spawner.py works out
# which row to click from a weapon's index among its LIVE classmates. A dead
# name left in here shifts every gun below it in that category by one row, and
# a mis-clicked row spawns the wrong gun rather than failing.

# ════════════════════════════════════════════════════════════
# Attachments currently in the game
#
# Measured: the spawner's 握把 / 弹匣 / 枪口 / 枪托 / 瞄准镜 categories, in
# spawner order. `asset` is the AttachmentDetector template stem, so a drag can
# be verified by classifying the slot afterwards; None means this repo has no
# template yet and the slot can only be checked as non-empty.
#
# A FEW STEMS ARE OURS, NOT THE GAME'S, and they are marked `# recovered`. The
# game added these attachments after the art dump this repo unpacked, so there
# is no shipped file to name them after — the only picture of them is the one
# calibration/solve_template.py recovers off the screen. The stem still has to start
# with the slot's prefix, because AttachmentDetector.SLOT_PREFIXES routes on it.
#
# THE ALTERNATIVE WAS LEAVING THEM None, AND None IS NOT NEUTRAL. A key with no
# asset is invisible to the template bank, so the icon does not read as unknown
# — the nearest neighbour wins instead, confidently: every 多倍率混合瞄具 in the
# corpus reads `scope_6x`, 10 for 10. Worse, `None` also blocked the repair:
# score_attachments builds its files by asset stem, so the three parts most in
# need of a recovered picture were the three it silently skipped.
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

# Keys this project has renamed, old -> current.
#
# THE STORED CAPTURES ARE NOT REWRITTEN. Eleven runs' manifests carry
# `angled_grip` labels, and every one of them is a picture of the right item --
# what changed is which name the item goes by here, not what was photographed.
# Editing ground truth to match a table is the move that turns a corpus into a
# record of the table; reading it through a rename does not.
#
# Anything resolving a label from disk should go through canonical().
RENAMED = {
    'angled_grip': 'tilted_grip',   # 41.1 swapped the part at that position
}


def canonical(key):
    """The current key for a possibly-renamed one. Unknown keys pass through."""
    return RENAMED.get(key, key)


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
    # ⚠ WAS `angled_grip` UNTIL 2026-08-04, AND THAT WAS THE WRONG ITEM.
    # Update 41.1 (2026-04-08) removed the Angled Foregrip and added the Tilted
    # Grip. The training range's list kept its length AND its label 斜向握把, so
    # nothing here noticed for four months -- the 库存 row prints the English
    # name and reads "Tilted Grip". The 0.809 recoil factor filed under
    # angled_grip is this part's, and the wiki contradiction it seemed to
    # produce was never real.
    #
    # LAST IN THE GRIP BLOCK ON PURPOSE: attachment_position() takes the spawner
    # index from the ORDER of this dict, so moving this line moves the click.
    # Entry 7 of 7, the crossbow quiver being the uncatalogued extra at 6.
    #
    # `asset` is OUR name -- it keys the template files, nothing reads it off
    # the client.
    'tilted_grip':    {'slot': 'grip', 'zh': '斜向握把',
                       'asset': 'Lower_TiltedGrip_C', 'classes': ('AR', 'DMR', 'SMG')},

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
                       'asset': 'Muzzle_Brake_Large_C',   # recovered
                       'classes': ('AR', 'DMR')},

    # ── 枪托 (stock) ──
    'heavy_stock':    {'slot': 'stock', 'zh': '重型枪托 (冲锋枪, 突击步枪, M249)',
                       'asset': 'Stock_Heavy_C',          # recovered
                       'classes': ('SMG', 'AR', 'LMG')},
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
                       'asset': 'Upper_Variable_C',       # recovered, NOT YET
                       # CAPTURED — the stem reserves the name so a run can
                       # fill it; until then there is no file and this key
                       # still reads as scope_6x.
                       'classes': ('AR', 'DMR', 'SMG', 'LMG', 'SR')},
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
    # Read off calibration/artifacts/tab_inventory_2.png, which has an Mk12 in slot 2: scope,
    # muzzle, grip and magazine boxes are drawn, the stock position is blank.
    'mk12':     {'slots': ('scope', 'muzzle', 'grip', 'magazine'), 'conf': 'measured'},
    'dragunov': {'slots': ('scope', 'muzzle', 'magazine', 'stock'), 'conf': 'measured'},

    # ── SMG ──
    # No stock slot: its tile is simply not drawn (ring 15.8 against a
    # threshold of 36, while its other three tiles read 96..120). The wiki
    # listed one. Scanned 2026-08-02.
    'ump45':    {'slots': ('scope', 'muzzle', 'grip', 'magazine'), 'conf': 'measured'},
    # calibration/artifacts/tab_inventory_2.png, slot 1: scope/muzzle/magazine/stock drawn, the
    # grip position blank — which is also what confirms an unowned slot is
    # simply not rendered rather than shifted away.
    'uzi':      {'slots': ('scope', 'muzzle', 'magazine', 'stock'), 'conf': 'measured'},
    'vector':   {'slots': _ALL, 'conf': 'measured'},
    'mp5k':     {'slots': _ALL, 'conf': 'measured'},
    # Suppressor-only muzzle, vertical-only grip.
    'tommy':    {'slots': ('scope', 'muzzle', 'grip', 'magazine'), 'conf': 'measured'},
    # Integral holo + laser + suppressor, none removable.
    # ⚠ THE p90 HAS NO ATTACHMENT SLOTS AT ALL. docs/p90_has_no_slots.png is the
    # att_1 strip cut from two night frames at identical coordinates:
    #
    #     mg3   ONE tile, with a red dot sitting in it   <- the region is right
    #     p90   no tile at all, the strip is empty
    #
    # The mg3 half is the positive control and it is the reason this is a
    # measurement rather than a squint: a blank strip only means "no slots" if
    # tiles are known to render there, and on the mg3 they do. The optic visible
    # on the p90's rail is drawn ON THE GUN, i.e. weapon art -- the same thing
    # the VSS does with its fixed PSO-1, per this file's header. Same answer
    # shape as win94 above: no slots, because the gun has an integrated optic.
    #
    # ⚠ AND IT REPLICATES ON AN INDEPENDENT FRAME SEVEN DAYS EARLIER.
    # docs/p90_no_slots_0802_scan.png is the P90 in slot 2 of the 2026-08-02
    # scan_compat capture: same blank strip. Two frames, two sessions, one
    # answer -- which is what this repository means by measured.
    #
    # Source frames, all kept:
    #   calibration/artifacts/nights/night_20260809_0546/fail_p90_standing_red_dot/tab.png
    #   calibration/artifacts/nights/night_20260809_0827/fail_mg3_standing_red_dot/tab.png
    #   calibration/artifacts/compat/runs/20260802_155222/p90.png
    #
    # On that frame the Red Dot Sight the night asked for is sitting in
    # VICINITY / Ground. `[!] scope should be red_dot, reads ''` is the honest
    # report of a part that had nowhere to go.
    #
    # ⚠ AND THE ENTRY THAT STOOD HERE WAS WRITTEN OFF THAT SAME ARTWORK. It
    # claimed "the evidence frame shows the gun WEARING a sight, and read_sight
    # came back `red_dot` off it" -- which is how `scope` got added to a weapon
    # that has no scope slot, and how a red dot ended up on the floor.
    #
    # ⚠ THE FOLLOW-UP CLAIM THAT read_sight IS FOOLED BY WEAPON ART IS WITHDRAWN.
    # I wrote it here before checking, and the check refutes it: on that exact
    # frame AttachmentDetector.read_slots({1: 'p90'}) answers None for all five
    # slots, blind or with the weapon named. The reader does NOT invent an optic
    # off the integral sight -- it declines, which is what the header says it
    # does for the VSS. Where the earlier `red_dot` reading came from is
    # unknown, and it is not in this frame.
    #
    # ⚠ SO THE p90 CANNOT BE MEASURED BY THIS PIPELINE AS IT STANDS, and that is
    # a bigger statement than "a cell failed". Its sight is neither `red_dot`
    # nor `iron`; it is an integral optic with no K in RECOIL_SIGHT_PROFILES.
    # Measuring the gun needs a K for that sight first (calibration/calibrate_k),
    # not another kitting attempt.
    #
    # ⚠ AND SlotDetector CANNOT COVER FOR THIS. On the same frame it answers
    # scope/grip/magazine = 'empty' where the truth is 'absent', because no tile
    # is drawn and existence is unreadable there. An 'empty' slot is an
    # INVITATION to drag, and a drag at a slot that does not exist is how
    # attachments -- and whole guns -- end up on the ground.
    # ⚠ AND THE OPERATOR CONFIRMED IT INDEPENDENTLY (2026-08-09):「p90 不能装
    # 瞄具，我确认过」. So this row has three sources that cannot have copied each
    # other -- a 08-02 scan frame, a 08-09 night frame, and somebody who plays
    # the game. That is the strongest confidence any row in this table has.
    'p90':      {'slots': (), 'conf': 'measured+confirmed'},
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

    # ── SR and SG ──
    #
    # THESE TEN WERE MISSING ENTIRELY until 2026-08-04, and the absence was not
    # neutral. fits() gates on SLOTS.get(weapon), so with no entry every part
    # read as incompatible with every shotgun and every bolt-action rifle —
    # which is precisely the set of weapons that can wear `choke`, `duckbill`
    # and `bullet_loops`. collect_templates concluded "no live weapon in ROSTER
    # can wear them" and skipped all three, so the three parts with no
    # screen-solved template were the three the collector could not reach. A
    # hole in a compatibility table reads as a fact about the game.
    #
    # 2026-08-02's scan covered 30 weapons; ROSTER has 40. Scanned the rest
    # 2026-08-04 (calibration/scan_compat.py --only ..., run
    # calibration/artifacts/runs/slot_scan/20260804_131048, 147s).
    #
    # `scope` IS DELIBERATELY ABSENT FROM EVERY ENTRY BELOW, and that is not
    # the same as "no sight fits". SlotDetector returns `unknown` there for
    # anyone: the scope position draws no tile at all, so an empty slot and an
    # absent one are pixel-identical (see detector/CLAUDE.md). The entries
    # above carry 'scope' on inference, from before this was understood; these
    # carry only what was measured. Settling it needs a sight fitted and read
    # back, per the calibrate-compat skill — until then a caller wanting to
    # mount an optic on one of these will be refused, which is the safe
    # direction: the unsafe one drops the part on the floor.
    'kar98k':   {'slots': ('scope', 'muzzle', 'stock'), 'conf': 'measured'},
    'm24':      {'slots': ('scope', 'muzzle', 'magazine', 'stock'),
                 'conf': 'measured'},
    'awm':      {'slots': ('scope', 'muzzle', 'magazine', 'stock'),
                 'conf': 'measured'},
    # Integrated 2.7x. No muzzle or magazine tile either, and it declined a
    # sight the pack was holding — see the scope note above.
    'win94':    {'slots': (), 'conf': 'measured'},
    # Nothing but the optic. Same shape as mg3, reached the other way round:
    # mg3's scope is inferred, this one was watched being mounted.
    'lynx':     {'slots': ('scope',), 'conf': 'measured'},
    's686':     {'slots': ('muzzle', 'stock'), 'conf': 'measured'},
    's12k':     {'slots': ('scope', 'muzzle', 'magazine'), 'conf': 'measured'},
    's1897':    {'slots': ('muzzle', 'stock'), 'conf': 'measured'},
    # grip, which no other shotgun has; the magazine tile read EMPTY rather
    # than filled, and an empty tile proves the slot exists just as well.
    'dbs':      {'slots': ('scope', 'grip', 'magazine'), 'conf': 'measured'},
    # Scanned separately (run 20260804_131534) — the first attempt died on a
    # Tab that would not open, and a guess was written in its place for about
    # four minutes before the re-scan refuted it. No tile anywhere, and then
    # a red dot mounted itself on it (mse 38, margin 17.2), so it has the one
    # slot that draws no tile and nothing else.
    'o12':      {'slots': ('scope',), 'conf': 'measured'},
}

# Per-weapon attachments that its class allows but this weapon rejects.
# The AUG entry is a *known conflict*: pubg.wiki.gg lists the AUG A3 only under
# Suppressor (AR, DMR, S12K) and not under Compensator or Flash Hider, while
# this repo's older weapon_attachments.WEAPON_SLOTS has had it as 'comp' since
# the recoil scales were calibrated. Left permissive here (no exclusion) and
# flagged by unverified() — one drag settles it.
EXCLUDE = {
    # MEASURED 2026-08-04, and the first entry in this table that ever was.
    # S686's muzzle slot is PRESENT and EMPTY (SlotDetector ring 61.1, edges
    # 0), a choke fits it 10 times out of 10 — and a duckbill spawned into the
    # backpack instead of onto the gun. The collector's own diagnosis is the
    # useful part: "per the autofit rule that means the slot was not empty
    # when it arrived. But the slot reads empty, so the autofit rule is what
    # does not hold here." The same duckbill fits an S12K 10/10, so it is the
    # WEAPON refusing the PART — which is exactly what this table is for, and
    # what none of it had been measured against.
    #
    # It also means PUBG's autofit is a usable compatibility ORACLE: a part
    # that lands on the gun proves the pairing, a part that goes to the pack
    # with the slot empty disproves it. That is one spawn per (weapon, part)
    # and no dragging, against the drag matrix this table's docstring assumes.
    #
    # s1897 is untested. Its attempt died on a rack-slot mix-up rather than a
    # refusal, and an inconclusive run is not a fact.
    's686':  {'duckbill'},
    'groza': {'comp_ar', 'flash_ar', 'brake_ar'},   # suppressor is its only muzzle
    'tommy': {'comp_smg', 'flash_smg'},             # suppressor only
    # CONFIRMED BY HAND 2026-08-04. Two harvest cells failed to fit it and both
    # recorded `reads ''` -- the part never landed at all. It was then tried in
    # the game by hand and the grip will not go on.
    #
    # ⚠ The cost recorded here used to be "tilted_grip is measured on the mp5k
    # alone (0.809), with no second weapon to repeat it on". THAT COST IS PAID:
    # the m762 measures 0.8169 ± 0.0210 (2026-08-05), 0.4σ from the mp5k. The
    # vector is still excluded; the cross-weapon repeat just came from a third
    # gun instead. It is the only grip in the set with one.
    'vector': {'tilted_grip'},
    # MEASURED 2026-08-05, and it RETRACTS what this table claimed the day
    # before. The retracted sentence, which sat right here: "the mp5k/scope_4x
    # strikes ... the slot read a red dot because the run had bolted one on and
    # mislabelled it. That pair was this project's bug and was cleared."
    #
    # It was not cleared. The strikes were real and the red dot was PUBG's
    # autofit, not a mislabelled run. What settles it is a POSITIVE CONTROL in
    # the same session 3.5 minutes apart -- same code path, same slot, same
    # starting condition (a red dot the spawner had autofitted):
    #
    #   aug  + scope_4x   red_dot -> Upper_ACOG_01_C   ok    (drag journal 489)
    #   mp5k + scope_4x   red_dot -> red_dot           MISS  (journal 490, 491)
    #
    # and the journal says the gesture was clean on both misses: cursor placed
    # on the first try, `grab±0` (zero offset from the intended point), plate
    # 889 -> 898 (the gun never left the rack). A clean gesture the game does
    # not accept is a refusal -- which is exactly why the journal records the
    # geometry next to the outcome instead of just the outcome.
    #
    # ⚠ SO "the slot reads the OLD part" IS A THIRD FAILURE SIGNATURE, next to
    # `reads ''` (never landed) and `reads '?'` (landed but unnameable). It is
    # the one that looks most like a bug in this project, and telling it apart
    # takes a same-session control -- not a re-reading of the logs, which is
    # what produced the retracted sentence.
    #
    # This is measurement-only; no wiki claim was consulted either way. One
    # hand-drag of a 4x onto an mp5k refutes it if it is wrong.
    'mp5k':  {'scope_4x'},
    # MEASURED 2026-08-09, and the game says it in TEXT, which is the strongest
    # kind of entry this table has (it is what the ONLY table is built from).
    #
    # Two night cells both recorded `magazine should be ext_smg, reads ''` --
    # the part never landed, the same signature that put vector/tilted_grip
    # here. What settles it rather than merely repeating it is the evidence
    # frame: the weapon panel reads **P90, 50 rounds, 5.7mm**, and the item
    # sitting unused in the inventory is labelled
    #
    #     Extended Mag (Handgun, SMG)
    #
    # The P90 is the only 5.7mm weapon in the game. The extended SMG magazine
    # names the classes it serves and the P90 is not among them, so this is not
    # an inference from two failures -- it is the game refusing a pairing it
    # never claimed.
    #
    # ⚠ "THE SLOT IS REAL" IS FALSE AND THIS ENTRY IS NOW DEAD WEIGHT, kept
    # only because the way it got here is worth more than the line. It read:
    #
    #     scan_compat measured `p90: ('magazine',)` by reading the tile, and
    #     that reading stands: the P90 draws a magazine slot.
    #
    # The scan's own capture refutes it. In
    # calibration/artifacts/compat/runs/20260802_155222/p90.png the P90 is in
    # slot TWO -- slot one holds an AUG (label 'AUG', 40 rounds, 5.56mm, four
    # tiles) -- and the P90's strip is blank, exactly as it is in the 2026-08-09
    # night frame seven days later. docs/p90_no_slots_0802_scan.png is that half.
    #
    # A file named p90.png whose measurement came off an AUG is root CLAUDE.md's
    # second law verbatim: the record described one object and the measurement
    # was taken from another, and nothing raised.
    #
    # ⚠ AND THE FALSE `magazine` DID NOT COME FROM THE AUG EITHER -- SlotDetector
    # answers `magazine: 'empty'` on the P90's blank strip, on BOTH frames. Its
    # existence test is a gradient on the tile's border ring, and with no tile
    # drawn it reads the weapon render behind it. 'empty' is the word that means
    # "drag here"; 'absent' is the word that means "do not". This is the one gun
    # where every slot gets the wrong one.
    #
    # With SLOTS['p90'] now empty, fits() rejects everything before this set is
    # ever consulted, so it can go the next time somebody is in here. The
    # 5.7mm-vs-SMG-magazine reasoning below was sound and is simply about a slot
    # that does not exist.
    'p90':   {'ext_smg', 'quickext_smg', 'quick_smg'},
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
    if not att or weapon not in ROSTER:
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
    print(f'{len(ROSTER)} weapons, {len(ATTACHMENTS)} attachments')
    print(f'unverified slot lists: {", ".join(unverified())}')
    for w in ROSTER:
        c = compatible(w)
        n = sum(len(v) for v in c.values())
        print(f'  {w:9s} {weapon_class(w):3s} {n:3d} fits  '
              f'slots={",".join(SLOTS[w]["slots"]) if w in SLOTS else "?"}')
