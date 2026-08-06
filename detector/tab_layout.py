"""Tab-screen geometry — where to grab an attachment and where to drop it.

Reference capture: docs/tab_inventory.png (3440x1440, 2026-08-01).
Measured by temp_debug/probe_tab_layout.py (inventory rows, from the text
bands) and temp_debug/probe_tab_slots.py (slot boxes, from their borders).

The attachment slots themselves are NOT redefined here — they already exist as
config.HUD_REGIONS['att_*'] for the detector, and a second copy would be one
more thing to keep in sync. This module only turns them into click points.

Both lists are dynamic: they scroll, and 附近 only exists while something is on
the ground under you. A row index therefore means "the i-th row currently
visible", never a specific item — whatever drags from one must identify the
item by its icon first.
"""
import os

from config import (HUD_REGIONS, SCREEN_W, SCREEN_H,
                    TAB_SLOT_RING_PAD, TAB_SLOT_TILE,
                    TAB_SLOT_TILE_OFF)

# ════════════════════════════════════════════════════════════
# The two source lists — 附近 (ground) and 库存 (inventory)
#
# They share one row geometry: first centre at y=199, pitch 81.55 px, verified
# on both reference captures and on both panels.
#
# Do not measure a row from its text: a label wraps to two or three lines and
# is not centred within the row when it does, which reads as a 15 px error and
# a bogus 66 px pitch. The icon block is centred; the text is not.
#
# 附近 appearing does NOT shift 库存 — the two panels have fixed, independent
# x ranges (checked against docs/tab_inventory.png, where 附近 is empty).
# ════════════════════════════════════════════════════════════

ROW_Y_FIRST = 199               # centre of row 0, both panels
ROW_PITCH = 81.55
ROW_H = 82

# Icon box, calibrated against AttachmentDetector's own metric rather than
# eyeballed: temp_debug/calib_inv_icon.py sweeps centre and size over the 12
# known rows of docs/tab_inventory.png and keeps what classifies most of them.
# 80 px is the box that puts the artwork at the same relative size the weapon
# slots present it at, so the same 63x63 templates apply after a resize.
ICON_W = 80

# panel → (row x0, row x1, icon centre x)
PANELS = {
    'nearby':    (565, 880, 616),    # 附近 / 地面, only when loot is underfoot
    'inventory': (907, 1236, 974),   # 库存
}

INV_ROWS = 12                   # rows visible at 1440p before scrolling

# WHERE TO RELEASE A DRAG so the item lands in this panel. NOT a row point.
#
# Measured 2026-08-02 by holding a drag over each panel without releasing and
# screenshotting (tools/snap_on_key.py stamps the cursor in): the game draws a
# dashed border around whatever would accept the drop, and it is invisible any
# other way. The two shots are in docs/tab/runs/drop_point/.
#
# This exists because reusing a row point as a release point is wrong and
# fails quietly. `unequip()` released at the ICON-COLUMN centre of a computed
# row -- inside PANELS' detected bounds -- and the part landed on the FLOOR
# instead of in the pack, twice, cleanly reproduced. Nothing noticed for
# months because drag() verifies the SOURCE slot and never the destination:
# the slot emptied, so it passed.
#
# Caveat, stated rather than papered over: the exact boundary between "lands
# in the pack" and "lands on the floor" is NOT mapped. The isolation runs that
# would have mapped it were taken with both lists at their 12-row display cap,
# where row-count deltas mean nothing, so they were discarded. What IS
# established is that these two points work and that the old row-derived one
# did not.
DROP_XY = {
    'inventory': (1128, 611),
    'nearby':    (744, 570),
}

# Kept as names because callers read better with them than with PANELS[...].
INV_X0, INV_X1, INV_ICON_X = PANELS['inventory']
NEARBY_X0, NEARBY_X1, NEARBY_ICON_X = PANELS['nearby']

# Scroll bar, for detecting that a list has more rows than are shown.
INV_SCROLL_X = 897


def row_y(i):
    """Centre y of the i-th visible row, in either panel."""
    return int(round(ROW_Y_FIRST + ROW_PITCH * i))


def row_point(i, panel='inventory'):
    """Where to press the mouse down to pick up the i-th row's item."""
    return PANELS[panel][2], row_y(i)


def row_box(i, panel='inventory'):
    """(x0, y0, x1, y1) of the i-th row — the whole row, label included."""
    x0, x1, _ = PANELS[panel]
    y = row_y(i)
    return x0, y - ROW_H // 2, x1, y + ROW_H // 2


def icon_box(i, panel='inventory'):
    """(x0, y0, x1, y1) of just the i-th row's icon — what to template-match."""
    _, _, cx = PANELS[panel]
    y = row_y(i)
    half = ICON_W // 2
    return cx - half, y - half, cx + half, y + half


# ════════════════════════════════════════════════════════════
# Weapon attachment slots (right) — drag targets
#
# Derived from HUD_REGIONS so there is exactly one definition. Slot boxes are
# drawn only when the weapon owns the slot, but the coordinates are fixed:
# an unused slot is invisible, not moved.
# ════════════════════════════════════════════════════════════

def _centre(region):
    y, x, h, w = HUD_REGIONS[region]
    return x + w // 2, y + h // 2


ATT_SLOT_XY = {name: _centre(name) for name in HUD_REGIONS
               if name.startswith('att_')}


def att_slot_point(weapon_slot, slot):
    """Drop point for `slot` on weapon 1 or 2.

    weapon_slot: 1 (bottom HUD / key 1) or 2 (top HUD / key 2)
    slot: 'scope' | 'muzzle' | 'grip' | 'magazine' | 'stock'
    """
    return ATT_SLOT_XY[f'att_{weapon_slot}_{slot}']


# The boxed slot number ("1" / "2") drawn at the left end of each weapon row.
#
# This is the handle for the WEAPON itself: grabbing here and releasing over
# 附近 throws the gun on the floor still wearing everything. That matters for
# the weapon axis, which measures a pair of guns and then has to clear the
# rack -- stripping the parts off first would put them back in the backpack,
# where PUBG's auto-fit bolts them onto the next pair, and a run labelled BARE
# comes back wearing a foregrip nobody asked for.
#
# Measured off docs/tab_inventory.png: the box spans x 2216..2259, y 123..158
# on weapon 1, which is immediately left of HUD_REGIONS['gun_name_1'] (x from
# 2275) and shares its vertical band. The row pitch is 302 px, matching the
# 301 between att_1_muzzle (y=316) and att_2_muzzle (y=617) to within a pixel.
GUN_TAG_X = 2237
GUN_TAG_Y = (145, 447)          # weapon 1, weapon 2


def gun_tag_point(weapon_slot):
    """Where to grab the WEAPON in rack slot 1 or 2, parts and all."""
    return (GUN_TAG_X, GUN_TAG_Y[weapon_slot - 1])


# ════════════════════════════════════════════════════════════
# Character equipment slots (middle)
#
# Not drag targets for attachments — included because the backpack governs how
# much the spawner can hand out, and armour/helmet level changes what the
# training range gives you.
# ════════════════════════════════════════════════════════════

EQUIP_SLOTS = {
    'helmet':   (1328, 244, 1406, 322),
    'backpack': (1328, 566, 1406, 645),
    'vest':     (1328, 654, 1406, 733),
    'melee':    (1328, 742, 1406, 821),
}


def equip_region(name):
    """An equipment slot as (y, x, h, w) — the shape every grabber here takes.

    EQUIP_SLOTS is written as (x0, y0, x1, y1) because that is how the boxes
    were measured off a screenshot; nothing else in this repo uses that order.
    """
    x0, y0, x1, y1 = EQUIP_SLOTS[name]
    return y0, x0, y1 - y0, x1 - x0

# Cosmetic slots, right of the character. Listed so a drag never lands on one
# by accident; nothing here affects gameplay.
COSMETIC_SLOTS_X = (2032, 2110)
COSMETIC_SLOTS_Y = ((242, 321), (330, 409), (418, 497), (565, 644),
                    (653, 732), (741, 820), (985, 1064), (1161, 1240))

# Somewhere with no interactive element, to park the cursor for a clean
# screenshot (a hovered row draws a highlight and a hovered slot a tooltip).
PARK_XY = (200, 1380)


# ════════════════════════════════════════════════════════════
# Anchor — is the Tab screen even up?
#
# Everything above returns coordinates whether or not the inventory is
# showing. A caller that drags without checking drags across the live game
# world, where the same click means "shoot".
#
# The probe is the 类型 / Type column header, which renders only while the
# inventory is up — matched by glyph IoU, not by counting bright pixels. The
# reasoning, the numbers and the two traps (saturated sky; TM_CCORR_NORMED
# scoring negatives above positives) are in the Tab anchor block of config.py.
# Measured: open 0.922..1.000, closed 0.000..0.352 over 5 screens and 96 ADS
# frames, threshold 0.60.
#
# Templates are per language and live in training_data/pubg_assets/tab/.
# Rebuild them with tools/probe_tab_anchor.py --write after a game update or
# a language change.
# ════════════════════════════════════════════════════════════

_MASKS = None


def _masks():
    """{lang: bool mask}, loaded once. Missing files are skipped."""
    global _MASKS
    if _MASKS is None:
        import cv2
        from config import TAB_ANCHOR_LANGS
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                         'training_data', 'pubg_assets', 'tab')
        _MASKS = {}
        for lang in TAB_ANCHOR_LANGS:
            m = cv2.imread(os.path.join(d, f'type_header_{lang}.png'),
                           cv2.IMREAD_GRAYSCALE)
            if m is None:
                continue
            if m.ndim == 3:          # ultralytics patches imread
                m = m[:, :, 0]
            _MASKS[lang] = m > 0
    return _MASKS


def type_ink(frame):
    """Bright pixels in the header. Diagnostic only — see the block above
    for why this is not a safe open/closed judgement by itself."""
    import cv2
    from config import TAB_PIXEL_THRESH
    y, x, h, w = HUD_REGIONS['type']
    crop = frame[y:y + h, x:x + w]
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return int((crop > TAB_PIXEL_THRESH).sum())


def type_score(frame):
    """Best header IoU over the language templates. -> (score, lang)"""
    import cv2
    import numpy as np
    from config import TAB_ANCHOR_SEARCH, TAB_PIXEL_THRESH
    y, x, h, w = HUD_REGIONS['type']
    gray = frame
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    s = TAB_ANCHOR_SEARCH
    best, who = 0.0, None
    for lang, ref in _masks().items():
        n_ref = int(ref.sum())
        for dy in range(-s, s + 1):
            for dx in range(-s, s + 1):
                yy, xx = y + dy, x + dx
                if yy < 0 or xx < 0 or yy + h > gray.shape[0] or \
                        xx + w > gray.shape[1]:
                    continue
                cand = gray[yy:yy + h, xx:xx + w] > TAB_PIXEL_THRESH
                inter = int(np.logical_and(cand, ref).sum())
                union = int(cand.sum()) + n_ref - inter
                if union and inter / union > best:
                    best, who = inter / union, lang
    return best, who


# ════════════════════════════════════════════════════════════
# One element, two windows — on purpose, not as a workaround.
#
#   inner   HUD_REGIONS['att_*'], 63x63, hugs the icon.
#           Answers WHAT IS IN IT, by template match.
#   outer   slot_tile_box() + a ring margin.
#           Answers WHETHER IT EXISTS, by gradient along the border.
#
# The 63x63 deliberately stops short of the tile edge: it was cut for the
# template matcher, and border pixels are not part of any icon — feeding them
# into an MSE only adds a constant that varies with whatever is behind the
# panel. That crop is correct and must not be widened.
#
# It is also, for the same reason, unable to answer the other question. The
# tile is 66x66, so the interior lies entirely *inside* it and sees only flat
# fill — identical whether the tile is empty or was never drawn.
#
# Geometry only here. The judgements that turn these boxes into
# absent/empty/filled are detector/slot_detector.py.
# ════════════════════════════════════════════════════════════

SLOT_NAMES = ('scope', 'muzzle', 'grip', 'magazine', 'stock')


def slot_tile_box(gun, slot):
    """The drawn tile. -> (y, x, h, w).

    66x66 starting one pixel up and left of the interior, measured on a
    stripped M416's muzzle and grip, where an empty tile is a clean blob.
    magazine and stock could not be measured the same way — their connected
    component merges with a bright neighbour (69x94, 94x95) — so this is two
    agreeing slots, not five.
    """
    y, x, _, _ = HUD_REGIONS[f'att_{gun}_{slot}']
    o, t = TAB_SLOT_TILE_OFF, TAB_SLOT_TILE
    return (y + o, x + o, t, t)


def slot_window(gun, slot, pad=None):
    """Outer window a presence test needs. -> (y, x, h, w)

    ⚠ **零调用方，而且它不是死代码。** 2026-08-06 的可达性扫描把它报成死的、删掉了、
    又装了回来：它的调用方是**人或 agent 按 skill 跑的一段测量步骤**
    （`.claude/skills/calibrate-compat`、`calibrate-screen` 都把它写成「外框裁剪」这
    一步），而那种调用方不在任何 import 图里。删掉它不会让任何测试变红，只会让下一次
    跑那个 skill 的人调到一个不存在的函数。
    """
    p = TAB_SLOT_RING_PAD if pad is None else pad
    ty, tx, th, tw = slot_tile_box(gun, slot)
    return (ty - p, tx - p, th + 2 * p, tw + 2 * p)


def is_open(frame):
    """Is the Tab inventory up? frame: full-screen BGR.

    Falls back to the pixel test only when no template is on disk, so a fresh
    checkout without the assets degrades loudly-ish rather than always
    answering False. Build them: tools/probe_tab_anchor.py --write

    The fallback goes through TabTypeDetector rather than re-deriving the
    band here. It used to be `TAB_COUNT_MIN <= type_ink(frame) <=
    TAB_COUNT_MAX`, which is the same idea in different arithmetic — type_ink
    is a luma count, the detector counts the channel maximum — and it missed
    the dark-floor gate entirely, so on a scoped frame full of sky it answered
    "Tab is up" with nothing on screen. See tools/test_tab_open.py.
    """
    from config import TAB_ANCHOR_MIN_IOU
    if not _masks():
        from detector.tab_detector import TabTypeDetector
        y, x, h, w = HUD_REGIONS['type']
        return bool(TabTypeDetector().classify(frame[y:y + h, x:x + w]))
    return type_score(frame)[0] >= TAB_ANCHOR_MIN_IOU


def _assert_on_screen():
    pts = [row_point(0), row_point(INV_ROWS - 1), PARK_XY]
    pts += list(ATT_SLOT_XY.values())
    for x, y in pts:
        assert 0 <= x < SCREEN_W and 0 <= y < SCREEN_H, f'off-screen: {(x, y)}'


_assert_on_screen()


if __name__ == '__main__':
    print(f'inventory rows (x={INV_ICON_X}):')
    for i in range(INV_ROWS):
        print(f'  row{i:2d}  y={row_y(i):4d}  box={row_box(i)}')
    print('\nattachment slots:')
    for name in sorted(ATT_SLOT_XY):
        print(f'  {name:16s} {ATT_SLOT_XY[name]}')
    print('\nequipment slots:')
    for name, box in EQUIP_SLOTS.items():
        print(f'  {name:9s} {box}')
