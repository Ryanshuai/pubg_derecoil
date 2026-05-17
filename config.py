# ════════════════════════════════════════════════════════════
# Screen
# ════════════════════════════════════════════════════════════

SCREEN_W = 3440
SCREEN_H = 1440

# ════════════════════════════════════════════════════════════
# HUD regions — (y, x, h, w) for win32_cap / ring buffer
# ════════════════════════════════════════════════════════════

HUD_REGIONS = {
    # Gameplay HUD (bottom)
    'weapon_1':   (1345, 2808, 53, 206),   # slot1 (bottom, key 1)
    'weapon_2':   (1262, 2808, 53, 206),   # slot2 (top, key 2)
    'fire_mode':  (1317, 1626, 43, 56),
    'posture':    (1301, 1373, 66, 66),

    # Tab inventory
    'type':       (129, 937, 18, 41),      # "Type" text, Tab open indicator
    'gun_name_1': (123, 2275, 45, 250),
    'gun_name_2': (425, 2275, 45, 250),

    # Attachment slots (63×63 each)
    'att_1_scope':    (153, 2581, 63, 63),
    'att_1_muzzle':   (316, 2219, 63, 63),
    'att_1_grip':     (316, 2355, 63, 63),
    'att_1_magazine': (316, 2502, 63, 63),
    'att_1_stock':    (316, 2785, 63, 63),
    'att_2_scope':    (455, 2581, 63, 63),
    'att_2_muzzle':   (617, 2219, 63, 63),
    'att_2_grip':     (617, 2355, 63, 63),
    'att_2_magazine': (617, 2502, 63, 63),
    'att_2_stock':    (617, 2785, 63, 63),
}

# ════════════════════════════════════════════════════════════
# Key polling — VK codes for GetAsyncKeyState
# ════════════════════════════════════════════════════════════

POLL_VK_MAP = {
    0x09: 'tab',        # VK_TAB
    0x10: 'shift',      # VK_SHIFT
    0x12: 'alt',        # VK_MENU
    0x1B: 'esc',        # VK_ESCAPE
    0x26: 'up',         # VK_UP
    0x28: 'down',       # VK_DOWN
    0x5B: 'win',        # VK_LWIN
    0x78: 'f9',         # VK_F9
    0x7C: 'f13',        # VK_F13
    ord('1'): '1', ord('2'): '2', ord('5'): '5',
    ord('B'): 'b', ord('C'): 'c', ord('F'): 'f',
    ord('G'): 'g', ord('X'): 'x', ord('Z'): 'z',
}

# ════════════════════════════════════════════════════════════
# Key action table — key events → state changes + hardware
#
# event: 'press' or 'release'
# cond:  condition on GameState (None = always)
# state: list of (attr, value) to set on GameState
#        'toggle_X' = toggle boolean X
#        callable = call method on GameState
# hw:    list of hardware actions (Pico)
#        'recoil_off', 'recoil_on', 'upload_pattern', 'shutdown'
# ════════════════════════════════════════════════════════════

KEY_ACTION_TABLE = [
    # ── Weapon switch ──
    {'key': '1', 'event': 'press', 'cond': '!tab_open',
     'state': [('set_active_by_key', 1), ('stop_recoil', False)],
     'hw': ['recoil_on', 'upload_pattern']},

    {'key': '2', 'event': 'press', 'cond': '!tab_open',
     'state': [('set_active_by_key', 2), ('stop_recoil', False)],
     'hw': ['recoil_on', 'upload_pattern']},

    # ── Pickup ──
    # Clear GT + attachments (weapon name falls back to pred/existing, so it persists).
    {'key': 'f', 'event': 'press', 'cond': '!tab_open',
     'state': [('weapon_gt', ('', '')), ('highlight_gt', 0), ('clear_attachments',)],
     'hw': ['upload_pattern']},

    # ── Fire mode ──
    {'key': 'b', 'event': 'press', 'cond': '!tab_open'},

    # ── Posture keys ──
    {'key': 'c', 'event': 'press', 'cond': '!tab_open'},
    {'key': 'z', 'event': 'press', 'cond': '!tab_open'},

    # ── Stop recoil ──
    {'key': 'g', 'event': 'press', 'cond': '!tab_open',
     'state': [('stop_recoil', True), ('highlight_gt', 0)],
     'hw': ['recoil_off']},

    {'key': 'x', 'event': 'press', 'cond': '!tab_open',
     'state': [('stop_recoil', True), ('highlight_gt', 0)],
     'hw': ['recoil_off']},

    {'key': '5', 'event': 'press',
     'state': [('stop_recoil', True)],
     'hw': ['recoil_off']},

    # ── Tab ──
    # Toggle tab_open immediately, tab_type calibrates after
    {'key': 'tab', 'event': 'press',
     'state': [('stop_recoil', True), ('highlight_gt', 0), ('toggle_tab_open',)],
     'hw': ['recoil_off']},

    # ── Alt+Tab / Win ──
    {'key': ('alt', 'tab'), 'event': 'press',
     'state': [('stop_recoil', True)],
     'hw': ['recoil_off']},

    {'key': 'win', 'event': 'press',
     'state': [('stop_recoil', True)],
     'hw': ['recoil_off']},

    # ── Shift (sprint) ──
    {'key': 'shift', 'event': 'press', 'cond': '!tab_open',
     'state': [('stop_recoil', True)],
     'hw': ['recoil_off']},

    {'key': 'shift', 'event': 'release', 'cond': '!tab_open',
     'state': [('stop_recoil', False)],
     'hw': ['recoil_on', 'upload_pattern']},

    # ── Right click (ADS) ──
    {'key': 'right', 'event': 'release', 'cond': '!tab_open',
     'state': [('stop_recoil', False)],
     'hw': ['recoil_on', 'upload_pattern']},

    # ── Scale adjust ──
    {'key': 'up', 'event': 'press', 'cond': '!tab_open',
     'state': [('adjust_counts', +0.01)]},

    {'key': 'down', 'event': 'press', 'cond': '!tab_open',
     'state': [('adjust_counts', -0.01)]},

    # ── Aim toggle ──
    {'key': 'f9', 'event': 'press',
     'state': [('toggle_aim',)]},

    # ── Shutdown ──
    {'key': 'f13', 'event': 'press',
     'hw': ['shutdown']},
]

# ════════════════════════════════════════════════════════════
# Detection table — detectors triggered by key events
#
# key:     trigger key (or 'tab_close' for tab closing edge)
# event:   'press' or 'release'
# detect:  detector name
# regions: which HUD_REGIONS crops to pass
# delay:   ms offset from key timestamp (find nearest frame at key_ts + delay)
# cond:    condition on GameState
# result:  what state field the result writes to
# ════════════════════════════════════════════════════════════

DETECT_TABLE = [
    # ── Weapon HUD (DL classifier) ──
    {'key': '1', 'event': 'press', 'detect': 'weapon_hud',
     'regions': ['weapon_1', 'weapon_2'], 'delay': 500,
     'cond': '!tab_open', 'result': 'weapon_pred'},

    {'key': '2', 'event': 'press', 'detect': 'weapon_hud',
     'regions': ['weapon_1', 'weapon_2'], 'delay': 500,
     'cond': '!tab_open', 'result': 'weapon_pred'},

    {'key': 'f', 'event': 'press', 'detect': 'weapon_hud',
     'regions': ['weapon_1', 'weapon_2'], 'delay': 500,
     'cond': '!tab_open', 'result': 'weapon_pred'},

    # ── Fire mode ──
    {'key': '1', 'event': 'press', 'detect': 'fire_mode',
     'regions': ['fire_mode'], 'delay': 500,
     'cond': '!tab_open', 'result': 'fire_mode'},

    {'key': '2', 'event': 'press', 'detect': 'fire_mode',
     'regions': ['fire_mode'], 'delay': 500,
     'cond': '!tab_open', 'result': 'fire_mode'},

    {'key': 'b', 'event': 'press', 'detect': 'fire_mode',
     'regions': ['fire_mode'], 'delay': 500,
     'cond': '!tab_open', 'result': 'fire_mode'},

    # ── Highlight (CV algorithm, no GT) ──
    {'key': 'f', 'event': 'press', 'detect': 'highlight',
     'regions': ['weapon_1', 'weapon_2'], 'delay': 500,
     'cond': '!tab_open && !stop_recoil', 'result': 'highlight_pred'},

    {'key': 'right', 'event': 'release', 'detect': 'highlight',
     'regions': ['weapon_1', 'weapon_2'], 'delay': 350,
     'cond': '!tab_open && !stop_recoil', 'result': 'highlight_pred'},

    # ── Posture ──
    {'key': 'c', 'event': 'press', 'detect': 'posture',
     'regions': ['posture'], 'delay': 200,
     'cond': '!tab_open', 'result': 'posture'},

    {'key': 'z', 'event': 'press', 'detect': 'posture',
     'regions': ['posture'], 'delay': 200,
     'cond': '!tab_open', 'result': 'posture'},

    {'key': 'right', 'event': 'release', 'detect': 'posture',
     'regions': ['posture'], 'delay': 350,
     'cond': '!tab_open', 'result': 'posture'},

    # ── Tab ──
    # Tab: calibrate tab_open after UI settles (correct toggle if out of sync)
    {'key': 'tab', 'event': 'press', 'detect': 'tab_type',
     'regions': ['type'], 'delay': 300,
     'result': '_tab_calibrate'},

    # Tab closing: cond checked BEFORE toggle, so tab_open is still True
    # Step 1: read weapons + attachments from pre-press frame
    {'key': 'tab', 'event': 'press', 'detect': 'tab_weapon',
     'regions': ['gun_name_1', 'gun_name_2'], 'delay': -50,
     'cond': 'tab_open', 'result': 'weapon_gt'},

    {'key': 'tab', 'event': 'press', 'detect': 'tab_attachment',
     'regions': ['att_1_scope', 'att_1_muzzle', 'att_1_grip', 'att_1_magazine', 'att_1_stock',
                 'att_2_scope', 'att_2_muzzle', 'att_2_grip', 'att_2_magazine', 'att_2_stock'],
     'delay': -50,
     'cond': 'tab_open', 'result': 'attachments'},

    # Step 2: after Tab UI closes, refresh HUD state
    {'key': 'tab', 'event': 'press', 'detect': 'fire_mode',
     'regions': ['fire_mode'], 'delay': 300,
     'cond': 'tab_open', 'result': 'fire_mode'},

    {'key': 'tab', 'event': 'press', 'detect': 'highlight',
     'regions': ['weapon_1', 'weapon_2'], 'delay': 300,
     'cond': 'tab_open', 'result': 'highlight_pred'},
]

# ════════════════════════════════════════════════════════════
# Mismatch collection — save crops when GT != pred (independent)
# ════════════════════════════════════════════════════════════

MISMATCH_TABLE = [
    # Highlight: GT is the key itself (1→slot1 highlighted, 2→slot2)
    {'key': '1', 'event': 'press', 'detect': 'highlight',
     'regions': ['weapon_1', 'weapon_2'], 'delay': 500,
     'cond': '!tab_open', 'gt_field': 'highlight_gt', 'gt_value': 1},

    {'key': '2', 'event': 'press', 'detect': 'highlight',
     'regions': ['weapon_1', 'weapon_2'], 'delay': 500,
     'cond': '!tab_open', 'gt_field': 'highlight_gt', 'gt_value': 2},

    # Weapon HUD: GT from state.weapon_gt (set by Tab scan, stable)
    {'key': '1', 'event': 'press', 'detect': 'weapon_hud',
     'regions': ['weapon_1', 'weapon_2'], 'delay': 500,
     'cond': '!tab_open', 'gt_field': 'weapon_gt'},

    {'key': '2', 'event': 'press', 'detect': 'weapon_hud',
     'regions': ['weapon_1', 'weapon_2'], 'delay': 500,
     'cond': '!tab_open', 'gt_field': 'weapon_gt'},
]

# ════════════════════════════════════════════════════════════
# Tab pixel fast-check thresholds
# ════════════════════════════════════════════════════════════

TAB_PIXEL_THRESH = 200
TAB_COUNT_MIN = 150
TAB_COUNT_MAX = 400

# Mismatch polling (ms)
MISMATCH_POLL_INTERVAL = 500   # ms between mismatch polls
GT_SETTLE_TIME = 500           # ms wait after GT change before polling (HUD animation)

# ════════════════════════════════════════════════════════════
# Alpha blending (for highlight hypothesis test)
# ════════════════════════════════════════════════════════════

ALPHA_HL = 0.80     # highlighted weapon icon opacity
ALPHA_LO = 0.405    # non-highlighted weapon icon opacity

# ════════════════════════════════════════════════════════════
# Training / assets
# ════════════════════════════════════════════════════════════

ASSET_DIR = {
    'weapon':     'training_data/pubg_assets/Item/Weapon/Main',
    'attachment': 'training_data/pubg_assets/Item/Attachment',
    'tab_detect': 'training_data/pubg_assets/type',
    'fire_mode':  'training_data/pubg_assets/fire_mode',
}

HARD_CASE_CONF = (0.3, 0.5)

# ════════════════════════════════════════════════════════════
# Mouse / Pico
# ════════════════════════════════════════════════════════════

MOUSE_BACKEND = 'pico'
PICO_PORT = None
MOUSE_DPI = 2000
GAME_SENSITIVITY = 30
COUNTS_PER_RECOIL_UNIT = 0.4
COUNTS_PER_PIXEL = 0.5

# ════════════════════════════════════════════════════════════
# Debug / detection
# ════════════════════════════════════════════════════════════

DEBUG_HOT_RELOAD = False
CONF_BODY = 0.85
CONF_HEAD = 0.3
CONF_BODY_RECOIL = 0.9

# ════════════════════════════════════════════════════════════
# Legacy compat — used by dl_models/icon_layout.py, training code
# ════════════════════════════════════════════════════════════

WEAPON_HUD_1 = {
    'x1': 2808, 'x2': 3014, 'y1': 1336, 'y2': 1406, 'icon_offset_y': 9,
}
WEAPON_HUD_2 = {
    'x1': 2808, 'x2': 3014, 'y1': 1253, 'y2': 1323, 'icon_offset_y': 9,
}
IN_TAB = {
    'x1': 937, 'y1': 129, 'x2': 978, 'y2': 147,
}
FIRE_MODE = {
    'x1': 1626, 'y1': 1317, 'x2': 1682, 'y2': 1360,
}
POSTURE = {
    'x1': 1373, 'y1': 1301, 'x2': 1439, 'y2': 1367,
}
ATTACHMENT_SLOTS = {
    1: {
        'scope':    (2581, 153, 2644, 216),
        'muzzle':   (2219, 316, 2282, 379),
        'grip':     (2355, 316, 2418, 379),
        'magazine': (2502, 316, 2565, 379),
        'stock':    (2785, 316, 2848, 379),
    },
    2: {
        'scope':    (2581, 455, 2644, 518),
        'muzzle':   (2219, 617, 2282, 680),
        'grip':     (2355, 617, 2418, 680),
        'magazine': (2502, 617, 2565, 680),
        'stock':    (2785, 617, 2848, 680),
    },
}
GUN_NAME_1 = {'x1': 2275, 'x2': 2525, 'y1': 123, 'y2': 168}
GUN_NAME_2 = {'x1': 2275, 'x2': 2525, 'y1': 425, 'y2': 470}
ALPHA = {
    'weapon_highlighted': 0.80,
    'weapon_non_highlighted': 0.405,
}
