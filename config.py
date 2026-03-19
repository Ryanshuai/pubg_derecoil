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
    'y1': 1318,
    'x2': 1682,
    'y2': 1361,
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
HARD_CASE_CONF = (0.3, 0.8)

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

# Mouse / sensitivity settings
MOUSE_DPI = 2000
GAME_SENSITIVITY = 50  # default
# Global recoil scale factor (1.0 = Kava4 default, adjust for your sensitivity)
# ↑/↓ arrow keys adjust by 0.05 in-game
COUNTS_PER_RECOIL_UNIT = 0.4


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
