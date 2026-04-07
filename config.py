from pynput import keyboard

# ── pynput special key → string name mapping ────────────
SPECIAL_KEYS = {
    keyboard.Key.tab: 'tab',
    keyboard.Key.esc: 'esc',
    keyboard.Key.up: 'up',
    keyboard.Key.down: 'down',
    keyboard.Key.f13: 'f13',
    keyboard.Key.alt_l: 'alt',
    keyboard.Key.alt_r: 'alt',
    keyboard.Key.cmd: 'win',
    keyboard.Key.cmd_r: 'win',
    keyboard.Key.f9: 'f9',
}

# Resolution: 3440x1440
SCREEN_W = 3440
SCREEN_H = 1440

# Posture icon (bottom-center HUD, left of health bar)
POSTURE = {
    'x1': 1373,
    'y1': 1301,
    'x2': 1439,
    'y2': 1367,
}

# Fire mode icon (bottom-center HUD, left of ammo count)
FIRE_MODE = {
    'x1': 1626,
    'y1': 1317,
    'x2': 1682,
    'y2': 1360,
}

# Weapon watermark HUD (bottom-right, normal gameplay, NOT Tab view)
# Verified by template matching on 3440x1440 screenshots
# Icon 53px, vertically centered in 70px slot, right-aligned to x2
WEAPON_HUD_1 = {  # main (bottom, selected)
    'x1': 2808, 'x2': 3014,
    'y1': 1336, 'y2': 1406,
    'icon_offset_y': 9,
}
WEAPON_HUD_2 = {  # secondary (top, unselected)
    'x1': 2808, 'x2': 3014,
    'y1': 1253, 'y2': 1323,
    'icon_offset_y': 9,
}


# Hard case mining: save crops when model confidence is in this range
HARD_CASE_CONF = (0.3, 0.5)

# Alpha blending values for synthetic data generation
ALPHA = {
    'weapon_highlighted':     0.80,   # selected weapon icon
    'weapon_non_highlighted': 0.405,  # unselected weapon icon
}


# ── Training asset paths ──────────────────────────────────
ASSET_DIR = {
    'weapon':     'training_data/pubg_assets/Item/Weapon/Main',
    'attachment':  'training_data/pubg_assets/Item/Attachment',
    'tab_detect':  'training_data/pubg_assets/type',
    'posture':     'training_data/pubg_assets/posture',
    'fire_mode':   'training_data/pubg_assets/fire_mode',
}

# In-tab detection: "Type" text region (white text, only visible in Tab view)
IN_TAB = {
    'x1': 937,
    'y1': 129,
    'x2': 978,
    'y2': 147,
}

# ── Key-triggered detection table ─────────────────────────
# keys:    which key presses trigger this detection
# detect:  which detector to call
# delay:   ms to wait for HUD to update before screenshot
# capture: 'region' = detector's own SLOT_RECT, 'fullscreen' = entire screen
DETECT_TABLE = [
    {'keys': ['1', '2'],           'detect': 'weapon_hud', 'delay': 200, 'capture': 'region'},
    {'keys': ['1', '2', 'b'],      'detect': 'fire_mode',  'delay': 200, 'capture': 'region'},
    {'keys': ['c', 'z', 'right_down'],  'detect': 'posture',    'delay': 200, 'capture': 'region'},
    {'keys': ['tab'],              'detect': 'tab_scan',   'delay': 0,   'capture': 'fullscreen'},
]

# ── Key-triggered immediate state updates ─────────────────
# key:    the key press
# state:  which state field to set
# value:  the value to assign
KEY_STATE_TABLE = [
    {'key': '1',           'state': 'active',      'value': 'weapon_1'},
    {'key': '2',           'state': 'active',      'value': 'weapon_2'},
    {'key': 'g',           'state': 'stop_recoil', 'value': True},
    {'key': '5',           'state': 'stop_recoil', 'value': True},
    {'key': 'f',           'state': 'gt_valid',    'value': False},
    {'key': 'up',          'state': 'counts',      'value': +0.01},
    {'key': 'down',        'state': 'counts',      'value': -0.01},
    {'key': 'right_down',  'state': 'stop_recoil', 'value': False},
    {'key': 'tab',         'state': 'stop_recoil', 'value': True},
    {'key': ('alt', 'tab'), 'state': 'stop_recoil', 'value': True},
    {'key': 'win',         'state': 'stop_recoil', 'value': True},
    {'key': 'f9',          'state': 'toggle_aim',  'value': True},
    # left_down/left_up removed — Pico detects left click directly via USB Host
]

# Mouse backend: 'pico' (hardware) or 'soft' (win32 SendInput)
MOUSE_BACKEND = 'pico'
# Pico HID Mouse serial port (None = auto-detect by VID:PID)
PICO_PORT = None  # auto-detect by VID:PID (0xCAFE:0x4001)

# Mouse / sensitivity settings
MOUSE_DPI = 2000
GAME_SENSITIVITY = 50  # default
# Global recoil scale factor (1.0 = Kava4 default, adjust for your sensitivity)
# ↑/↓ arrow keys adjust by 0.05 in-game
COUNTS_PER_RECOIL_UNIT = 0.4

# Aim assist: mouse counts per screen pixel (calibrate with sniper + ↑↓ keys)
COUNTS_PER_PIXEL = 0.5

# Debug: hot-reload weapon_scales.json and curve files on every set_seq()
DEBUG_HOT_RELOAD = False

# Human detection confidence thresholds
CONF_BODY = 0.85          # body detection (used by sniper)
CONF_HEAD = 0.3           # head detection
CONF_BODY_RECOIL = 0.9    # body detection for rifle aim assist (higher = less jitter)


# Gun name text position in Tab view (3440x1440)
GUN_NAME_1 = {
    'x1': 2275, 'x2': 2525,
    'y1': 123,  'y2': 168,
}
GUN_NAME_2 = {
    'x1': 2275, 'x2': 2525,
    'y1': 425,  'y2': 470,
}



# Attachment slot rects: 63×63 inner area (67×67 minus 2px bevel border each side)
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
