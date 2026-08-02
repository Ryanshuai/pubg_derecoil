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
    # Rounds left in the magazine. Automated training-range calibration uses
    # it for both ends of the cycle: the count going static means the mag is
    # empty, and it jumping back up means the reload finished — far more
    # robust than timing the fire/reload durations by weapon. Sized to fit
    # three digits (100-round drums) with the count right-aligned; stops
    # short of the spare-ammo symbol at x=1760.
    'ammo':       (1318, 1670, 48, 90),

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
# Recoil observation ROI — patches for measuring view rotation
#
# The patches sit on the screen's vertical centre line. A pitch rotation maps
# to a pure translation only at y = 0 (relative to the principal point): off
# the centre line the vertical gain grows as 1+(y/f)^2 and pitch leaks into
# the horizontal reading as x*y/f — 20%+ crosstalk at y=200, x=1300. On the
# centre line both vanish *independently of focal length*, so no FOV
# calibration is needed and phaseCorrelate's pure-translation assumption holds.
#
# x positions avoid the centre +-330 px (crosshair / iron sights) and all sit
# inside the existing DXGI bounding box (x 937..3014), so adding them to
# HUD_REGIONS does not enlarge the captured area.
#
# See docs/recoil_observer_design.md for the measurements behind each value.
# ════════════════════════════════════════════════════════════

RECOIL_PATCH = 128             # width. Sets nothing about range: recoil is
                               # vertical, so only the height has to cover it.
                               # Widening costs FFT time for nothing.
# Height sets the wrap limit (H/2), and the wrap limit is what decides which
# guns can be measured at all. One shot's recoil lands in a single frame, so
# the peak frame carries the whole per-bullet kick: a kitted AUG measured
# 21-54 px, but a BARE m762 is 80 px and a bare AKM 69 px — past the 64 px a
# 128 px patch allows, and the correlation peak wraps rather than failing.
# Guns come off the training-range spawner bare, so 128 could not measure them.
#
# 256 buys a 128 px limit for 3.6x the FFT time (0.16 -> 0.58 ms/pair, 2.0 s
# per magazine) — paid on a worker while the game reloads, so free in practice.
# Squaring it instead would cost 7.6x for the same limit.
#
# The cost of height is intra-patch gain error: pitch is a pure translation
# only on the centre line, growing as (y/f)^2 away from it. At f~1720 the
# patch edge at y=+-128 is off by 0.55%, ~0.2% averaged over the patch —
# negligible against the 5% effects being measured.
RECOIL_PATCH_H = 256
RECOIL_BAND_Y = SCREEN_H // 2 - RECOIL_PATCH_H // 2
RECOIL_KEEPOUT = 330           # half-width of the crosshair exclusion
RECOIL_PATCH_XS = (980, 1120, 1260, 2050, 2240, 2430, 2620)  # odd count: an
                               # even count lets 2 bad patches drag the median
RECOIL_CHANNEL = 1             # green; skips cvtColor, cuts the copy 3x

# Gradient-energy floor. Only rejects degenerate frames (loading screen,
# flashbang whiteout, full-screen UI) — measured accuracy is uncorrelated
# with this score all the way down to 0.5, so it is NOT a texture filter.
# Never gate on phaseCorrelate's response instead: it reads 0.95 on a flat
# patch whose displacement is completely wrong.
RECOIL_GATE_MIN = 0.1

# Outlier rejection across patches. The floor is required because MAD
# collapses to ~0 when patches agree, which would reject everything.
RECOIL_MAD_K = 3.0
RECOIL_MAD_FLOOR = 0.5

# ════════════════════════════════════════════════════════════
# Per-sight calibration — measured 2026-08-01 with
# calibration/calibrate_k.py, at general 30 / aim 50 / scope 50 and
# every per-scope slider at its default 50.
# RE-RUN AFTER CHANGING ANY SENSITIVITY SLIDER OR THE MOUSE DPI.
#
#   sight     K        R^2       CV     patches
#   hipfire   ~0.50    -         -      7     (TPP, no weapon; needs redo armed)
#   red_dot   1.5474   0.99999   0.3%   7
#   2x        1.8254   0.99948   2.0%   3
#   3x        1.8802   0.99977   1.4%   3
#   4x        1.88+-0.03         3 runs  5    (1.8827 / 1.8725 / 1.9000)
#
# 2x/3x/4x agree to within their own CV, so magnified scopes share one K.
#
# Measured inside the 4x scope, K is the same to 0.13% whether a patch sits
# 136 px or 406 px from the scope centre — the scope is a flat window, not a
# distorting lens, so patches may go anywhere inside the circle. The residual
# ~2.7% scatter is trial-to-trial (injection timing, frame pacing), identical
# across all five patches, and does not improve by moving them.
# 1x sits 18% lower because red-dot/holo/iron run off the "aim sensitivity"
# slider while 2x+ run off "scope sensitivity" times a per-scope multiplier.
#
# patch_xs is per-sight because the scope body blacks out most of the band.
# Those patches are fixed to the screen rather than the world, so they read
# zero and — being several of them — drag the median with them while MAD stays
# small. The blurred ring OUTSIDE the scope body is worse still: it renders at
# hip-fire FOV, so it moves at ~1/4 the in-scope rate.
#
# keepout is per-sight too: iron sights and red dots put the gun body across
# the centre, magnified scopes only a thin reticle.
# ════════════════════════════════════════════════════════════

RECOIL_SIGHT_PROFILES = {
    'hipfire': {'K': 0.50,   'keepout': 330,
                'patch_xs': (980, 1120, 1260, 2050, 2240, 2430, 2620)},
    'red_dot': {'K': 1.5474, 'keepout': 330,
                'patch_xs': (980, 1120, 1260, 2050, 2240, 2430, 2620)},
    '2x':      {'K': 1.8254, 'keepout': 60,
                'patch_xs': (1390, 1530, 1850)},
    '3x':      {'K': 1.8802, 'keepout': 70,
                'patch_xs': (1380, 1520, 1820)},
    '4x':      {'K': 1.885,  'keepout': 70,
                'patch_xs': (1250, 1390, 1520, 1800, 1930)},
    # VSS's fixed PSO-1 measures the same as a standard 4x (1.8855 / 1.8636 on
    # the two clean patches). Its patch set is different though: the PSO-1
    # reticle puts a full-width horizontal line plus a range ladder across the
    # observation band, and any patch overlapping those markings goes unstable
    # (measured std 0.67 vs 0.012 typical) because the screen-locked overlay
    # competes with real texture for the correlation peak.
    # The PSO-1 horizontal line spans x=1460..1990 and MUST be avoided
    # entirely. A patch overlapping it is a *conditional* failure: x=1900
    # measured clean against sand+grass (gate 3886, std 0.026) and then blew
    # up against rock (gate 1123, std 0.777) — strong texture outvotes the
    # line, weak texture lets the line win. So "tested clean once" is not
    # evidence; these three were verified on the weak-texture aim.
    # Only 1260..1460 and 1990..2150 are usable, which fits just two
    # non-overlapping 128 patches — hence the overlap below. Dropping to
    # patch=96 would give three clean non-overlapping ones at 1262/1362/1995
    # (range 3P/8=36px vs 5.6px/frame needed), but ViewTracker takes one
    # global patch size, so that needs a per-profile override first.
    'vss_pso1': {'K': 1.875, 'keepout': 200,
                 'patch_xs': (1265, 1330, 2010)},
}

# Untested sights fall back to the magnified-scope group.
RECOIL_K_DEFAULT_SCOPED = 1.86

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
    0x13: 'pause',      # VK_PAUSE
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
    {'key': 'pause', 'event': 'press',
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
# Spawner screen — the training range's item-spawner panel
#
# Identified by the three save-loadout / load-loadout / equip-lv3 button
# glyphs at the bottom right, which appear on no other screen. Measured over
# three captures with different scenes behind the panel (tools/
# probe_button_icons.py): the glyphs are achromatic (|max-min| over B,G,R <= 2
# on bright px), a flat ~221 grey rather than pure white, and their bright
# pixels are fully opaque — they shift by <= 6 grey levels across scenes,
# while the glyphs' dark parts are alpha-blended and shift by up to 86. Hence
# a binary mask of the bright pixels only; a grey-level template of the whole
# tile would track the scene.
#
# These are not in HUD_REGIONS: the check is for tools that drive the spawner,
# not something the per-frame capture loop needs.
# ════════════════════════════════════════════════════════════

SPAWNER_ICON_ANCHORS = ((2514, 988), (2514, 1081), (2514, 1174))  # x, y
SPAWNER_ICON_W = 70
SPAWNER_ICON_H = 77
SPAWNER_ICON_THRESH = 200      # cuts the opaque glyph out of the dim tile
SPAWNER_ICON_SEARCH = 24       # +- px searched around each anchor
SPAWNER_MIN_SCORE = 0.55       # positives 0.989..1.000, negatives 0.000

# ════════════════════════════════════════════════════════════
# Lobby screen — am I in the menus or in a match?
#
# The lobby renders 16:9 centred on the 21:9 screen and ALWAYS does; it has no
# widescreen mode. That makes the side bars the cheapest possible discriminator
# — pure black (max=0) in the lobby, and a match cannot produce a 140x1000
# block of exact zeros. Measured over lobby / in-game / in-game-with-Tab:
# bar max = 0 / 255 / 78.
#
# Use the RIGHT bar. The left one is not clean: an overlay (the friends-list
# badge) paints over it from x=61, so a left-bar probe measures whether that
# overlay happens to be showing.
#
# These are NOT in HUD_REGIONS. Per detector/CLAUDE.md the per-frame capture
# box must not be stretched for an event-driven check, and these ROIs sit far
# outside it (x 937..3014) — adding them would grow every frame's copy by 59%
# for a check that only needs 1-2 Hz. LobbyDetector owns its own grabber.
# ════════════════════════════════════════════════════════════

LOBBY_IMAGE_X0, LOBBY_IMAGE_X1 = 440, 2999   # the 16:9 image inside the bars

# (y, x, h, w). Right letterbox bar, clear of the lobby image.
LOBBY_BAR_ROI = (200, 3060, 1000, 140)
LOBBY_BAR_MAX = 8              # lobby measures exactly 0; in-game 78..255

# Net-debug overlay ("Ping: 43ms ..."), the one signal independent of the
# bars. Restricted to x >= LOBBY_IMAGE_X0 so it reads the lobby's own image
# rather than the black bar, which would make it a second letterbox probe.
# Bright-pixel fraction: lobby 0.021, in-game 0.099..0.112.
LOBBY_PING_ROI = (0, 460, 26, 240)
LOBBY_PING_THRESH = 180        # grey level counted as overlay text
LOBBY_PING_MIN_FRAC = 0.05

# PLAY button, measured off the lobby screenshot by its purple fill:
# x 520..978, y 1247..1327. The button draws an "F" hint, which this used to
# take at face value and press F instead of clicking — no cursor needed, no
# pointer backend involved. It does not work: three F presses with the game
# verified frontmost left the lobby exactly where it was. The lobby takes the
# click, so the cursor does have to be driven here.
LOBBY_PLAY_XY = (749, 1287)

# ── Results screen ───────────────────────────────────────────────────────
# The match-end screen is full-bleed with the ping overlay covered, so it
# reads as FULLBLEED exactly like a loading screen — but the two want opposite
# handling: click EXIT TO LOBBY on one, only wait on the other.
#
# Colour cannot tell them apart. The button's tan is the same tan as the
# training range's dirt: measured over the button's box, the results screen
# scores 0.297 and the training range 0.655. The glyphs are structure, so a
# binary text mask is the separable signal — 1.000 on the results screen and
# 0.000 on all three other captured states.
#
# Button box: x 80..347, y 1339..1387. Text band inside it:
LOBBY_EXIT_TEXT_ROI = (1350, 100, 26, 210)   # (y, x, h, w)
LOBBY_EXIT_THRESH = 170
LOBBY_EXIT_SEARCH = 20
LOBBY_EXIT_MIN_SCORE = 0.55    # positive 1.000, negatives 0.000
LOBBY_EXIT_XY = (213, 1363)    # button centre

# Clicking EXIT is only an accelerator: the results screen returns to the
# lobby on its own after ~18 s ("You will exit to lobby in 18 seconds"). So a
# failed template match costs time, never correctness — the poll for LOBBY
# gets there either way.

# ── System (ESC) menu ────────────────────────────────────────────────────
# The pause menu leaves everything the two probes look at intact: the scene is
# still full-bleed and the ping overlay still draws, so the bar/ping pair
# reads IN_GAME and would report playable=True. It is NOT playable — keys go
# to the menu, not the character, so anything driving the game here throws its
# whole run away silently.
#
# Title row measured by projection: y 150..215, x 571..972. The five entries
# below it are left-aligned at x=570 with a pitch of 85.25 px:
#   RESUME 293 / SETTINGS 378 / KEY GUIDE 464 / LEAVE TRAINING 549 /
#   EXIT TO DESKTOP 634
# Template scores: system menu 1.000, every other captured state <= 0.111.
LOBBY_MENU_TITLE_ROI = (150, 571, 66, 402)   # (y, x, h, w)
LOBBY_MENU_THRESH = 190
LOBBY_MENU_SEARCH = 24
LOBBY_MENU_MIN_SCORE = 0.55

# Only the training range's menu has been captured. A real match almost
# certainly renders a different fourth entry ("LEAVE MATCH" or similar), so
# the entry coordinates below are training-range-only until one is measured.
LOBBY_MENU_LEAVE_XY = (727, 549)     # LEAVE TRAINING
LOBBY_MENU_RESUME_XY = (648, 293)

# LEAVE TRAINING must be confirmed by its glyphs before it is ever clicked.
# EXIT TO DESKTOP sits ONE PITCH BELOW IT at y=634 — on a reordered menu, a
# blind click at y=549 would quit the game outright. Confusion measured
# against every entry in the captured menu: LEAVE TRAINING 1.000, and the
# worst impostor is EXIT TO DESKTOP at 0.152.
LOBBY_LEAVE_TEXT_ROI = (527, 570, 46, 316)   # (y, x, h, w)
LOBBY_LEAVE_MIN_SCORE = 0.55

# Clicking LEAVE TRAINING does not leave. It raises a centred CONFIRM / CANCEL
# dialog ("Do you want to exit training?") and the game sits there until it is
# answered — which looked, from the outside, exactly like the exit working and
# then the process losing focus one step short of the lobby.
#
# Gated on the dialog's own title rather than on the CONFIRM glyphs: every
# confirmation dialog in this game has a CONFIRM button, and only this one says
# LEAVE TRAINING across the middle of the screen. Distinct from the menu ENTRY
# of the same name at LOBBY_LEAVE_TEXT_ROI — different position, different size.
LOBBY_LEAVE_CONFIRM_TEXT_ROI = (558, 1495, 65, 450)   # (y, x, h, w)
LOBBY_LEAVE_CONFIRM_MIN_SCORE = 0.55
LOBBY_LEAVE_CONFIRM_XY = (1576, 878)   # CONFIRM; CANCEL is at x=1863

# "ERROR / You have been logged off due to inactivity." Sitting still in the
# lobby gets the session dropped, and the dialog then blocks everything —
# including re-entry, so an unattended campaign that idles once never recovers
# on its own. OK is its only button.
#
# Gated on the title, which is just the word ERROR, so this fires for any error
# dialog. That is deliberate: whatever the message, OK is the only thing to
# click, and being stuck is worse than dismissing something unexpected. It is
# logged loudly either way.
LOBBY_ERROR_TEXT_ROI = (500, 1628, 65, 186)   # (y, x, h, w)
LOBBY_ERROR_MIN_SCORE = 0.55
LOBBY_ERROR_OK_XY = (1709, 904)

# "ERROR / The service is not available at the moment." with a RECONNECT
# button — the session has been dropped by the server, usually following the
# inactivity logout above. A STATE, not a dialog: nothing works here, and
# pressing PLAY at it wastes three retries and thirty seconds.
#
# Nothing else distinguishes it. The screen is almost entirely black, so the
# letterbox probe reads 0 and classify() calls it LOBBY. Its ERROR title sits
# 38 px below the inactivity dialog's, close enough to be a coincidence worth
# not relying on, so the gate is the RECONNECT glyph, which is unique.
LOBBY_RECONNECT_TEXT_ROI = (881, 1689, 23, 119)   # (y, x, h, w)
LOBBY_RECONNECT_MIN_SCORE = 0.55
LOBBY_RECONNECT_XY = (1730, 906)

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
