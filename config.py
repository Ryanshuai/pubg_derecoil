# Resolution: 3440x1440
SCREEN_W = 3440
SCREEN_H = 1440

# Attachment slot size (px) — all boxes are 67x67
SLOT_SIZE = 67

# Slot positions (x1, y1) at 3440x1440, detected via edge/line detection
# Bottom row: muzzle, grip, magazine, stock (left to right)
# Scope is above the weapon on its own row
SLOT_X1 = {
    'muzzle':   2217,
    'grip':     2353,
    'magazine': 2500,
    'stock':    2783,
    'scope':    2579,
}

GUN1_Y1 = {
    'bottom': 314,   # muzzle, grip, magazine, stock
    'scope':  151,
}

GUN2_Y1 = {
    'bottom': 615,   # muzzle, grip, magazine, stock
    'scope':  453,
}

Y_DIFF = 301  # gun2_y - gun1_y

# Laplacian variance threshold: empty slot < 19, has attachment > 318
LAPLACIAN_THRESHOLD = 100

# Posture icon (bottom-center HUD, left of health bar)
POSTURE = {
    'x1': 1373,
    'y1': 1301,
    'x2': 1439,
    'y2': 1367,
}

# Fire mode icon (bottom-center HUD, left of ammo count)
FIRE_MODE = {
    'x1': 1638,
    'y1': 1325,
    'x2': 1682,
    'y2': 1368,
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


# Alpha blending values for synthetic data generation
ALPHA = {
    'weapon_highlighted':     0.80,   # selected weapon icon
    'weapon_non_highlighted': 0.405,  # unselected weapon icon
    'posture':                0.75,   # posture icon
}


# ── Training asset paths ──────────────────────────────────
ASSET_DIR = {
    'weapon':     'training_data/pubg_assets/Item/Weapon/Main',
    'attachment':  'training_data/pubg_assets/Item/Attachment',
    'tab_detect':  'training_data/pubg_assets/type',
}

# ── VGG detector configs ────────────────────────────────────

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
# Recoil curve value → mouse counts conversion
# Tune this: bigger = more compensation
COUNTS_PER_RECOIL_UNIT = 60

# ── VGG detector configs (only for detectors not yet replaced) ──

# Model input sizes [H, W]
# gun_name: uses original crop size (53x206), no resize needed
MODEL_INPUT_SIZE = {
    'gun_name':   [53, 206],
    'fire_mode':  [32, 32],
}

# Classification labels (index 0 = background/empty)
MODEL_CLASSES = {
    'gun_name':   ['98k', 'ace32', 'akm', 'aug', 'awm', 'dbs', 'dp28', 'g36c', 'groza', 'k2',
                   'lynx', 'm16', 'm24', 'm249', 'm416', 'm762', 'mg3', 'mini14', 'mk12', 'mk14',
                   'mk47', 'mosin', 'mp5k', 'mp9', 'o12', 'p90', 'pp19', 'qbu', 'qbz', 's12k',
                   's1897', 's686', 'scar', 'sks', 'slr', 'tommy', 'ump45', 'uzi', 'vector',
                   'vss', 'win94'],
    'fire_mode':  ['burst2', 'burst3', 'full', 'high', 'single'],
}

# Gun name text position in Tab view (3440x1440)
GUN_NAME_1 = {
    'x1': 2275, 'x2': 2525,
    'y1': 123,  'y2': 168,
}
GUN_NAME_2 = {
    'x1': 2275, 'x2': 2525,
    'y1': 425,  'y2': 470,
}



BORDER_CROP = 2  # bevel border pixels to exclude
SLOT_INNER = SLOT_SIZE - 2 * BORDER_CROP  # 63

def get_slot_rects(gun_idx=1):
    """Return dict of {slot_name: (x1, y1, x2, y2)} for given gun (1 or 2).
    63×63 inner area (bevel border excluded)."""
    gun_y = GUN1_Y1 if gun_idx == 1 else GUN2_Y1
    c = BORDER_CROP
    rects = {}
    for slot_name, x1 in SLOT_X1.items():
        y1 = gun_y['scope'] if slot_name == 'scope' else gun_y['bottom']
        rects[slot_name] = (x1 + c, y1 + c, x1 + SLOT_SIZE - c, y1 + SLOT_SIZE - c)
    return rects

ATTACHMENT_SLOTS = {
    1: get_slot_rects(1),
    2: get_slot_rects(2),
}
