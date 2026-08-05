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

# Which of the above the capture thread grabs EVERY FRAME. The rest are the
# Tab screen's, and they are read on demand — see control/tab_watch.py.
#
# This split is not tidiness, it is 87% of the capture cost. DXGI takes ONE
# bounding box, the gameplay HUD sits at the bottom of the screen (y 1262..
# 1398) and the Tab panel at the top (y 123..680), so having both in one set
# stretches the box over everything between them:
#
#   gameplay only   5 regions   1641x136   0.22 Mpx   4.5% of screen  0.80 ms
#   + Tab regions  18 regions  2077x1275   2.65 Mpx  53.5% of screen  6.27 ms
#
# Measured 2026-08-02. That 5.46 ms was being paid 144 times a second for a
# panel that is not on screen, and it showed: the shipped loop measured 115
# fps against its own target_fps=144, with a ceiling of 160.
#
# detector/CLAUDE.md has forbidden exactly this since before it was true here.
FRAME_REGIONS = ('weapon_1', 'weapon_2', 'fire_mode', 'posture', 'ammo')
TAB_REGIONS = tuple(k for k in HUD_REGIONS if k not in FRAME_REGIONS)

# ── Tab screen watching (control/tab_watch.py) ──
# A GDI grab costs ~5 ms almost regardless of size (41x18 measures 5.2 ms,
# 629x557 measures 9.6 ms), so nothing here can run per tick: at the 10 ms
# dispatcher tick a single 'type' check would be 52% of a core. Everything
# below is therefore event-driven, with a slow check to catch drift.
TAB_SETTLE_S = 0.40     # after a Tab key, how long to keep watching for the
                        # screen to actually change. Measured: open lands in
                        # 28-38 ms, close in 77-128 ms. Generous, and it stops
                        # the moment it sees the change.
TAB_REFRESH_S = 0.10    # while the panel is up, how often to re-read the guns
                        # so that the last reading is never stale by more than
                        # this when it closes
TAB_DRIFT_S = 0.50      # re-check open/closed even with no key event: alt-tab,
                        # a disconnect dialog or another agent can move the
                        # screen out from under us, and a keypress-only scheme
                        # would never notice

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
# HEIGHT IS RANGE. measure_pair calls a reading out of range when it lands more
# than half a patch height from the prediction, so 256 buys +-128 px per frame
# pair. That is ample at 1x and it is NOT ample through a scope: the sight
# magnifies the picture, so one bullet's kick covers 2-4x the pixels, and the
# correlator starts refusing readings it should have made.
#
# Measured 2026-08-04, one bare magazine each, mp5k/m416 standing:
#
#     profile     patches   rejected/frame   mean_mad
#     red_dot        7        3.5-11.3%       0.3-0.9   (19 cells)
#     4x             5           21.8%          1.87
#     2x             3           22.0%          0.78
#     3x             3           25.1%          2.39
#     vss_pso1       3          ~25%             --     (long recorded unusable)
#
# The split is NOT the patch count — 4x has five and fails like 2x's three.
# It tracks MAGNIFICATION, which is what the range argument predicts. The VSS
# has been "unusable" in this repository for its own reasons for weeks; it is
# mag=4, and three other mag>=2 profiles reproduce its number exactly.
#
# So the magnified profiles get their own height. Per profile rather than
# globally: a taller patch costs correlation time and moves the band, and 1x
# has nineteen cells of evidence that it does not need it.
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

# `mag` is the optic's magnification, and it is NOT redundant with K.
#
# K is pixels of screen motion per mouse count. Angle per count is K divided
# by pixels-per-degree, and pixels-per-degree scales with magnification — so
# `mag / K` is what says how many counts a given VIEW ROTATION costs. The two
# differ by more than 3x between the red dot and a 4x, which is why anything
# that drives the view by an absolute number of counts (homing into the pitch
# stop, rising to level) has to scale by it. Pushing the red dot's 1770 counts
# while looking through the VSS's fixed 4x lifts the view barely a third of
# the way and leaves it pointed at the ground.
RECOIL_SIGHT_PROFILES = {
    'hipfire': {'K': 0.50,   'mag': 1, 'keepout': 330,
                'patch_xs': (980, 1120, 1260, 2050, 2240, 2430, 2620)},
    'red_dot': {'K': 1.5474, 'mag': 1, 'keepout': 330,
                'patch_xs': (980, 1120, 1260, 2050, 2240, 2430, 2620)},
    '2x':      {'K': 1.8254, 'mag': 2, 'keepout': 60,
                'patch_xs': (1390, 1530, 1850),
                'patch_h': 384},
    '3x':      {'K': 1.8802, 'mag': 3, 'keepout': 70,
                'patch_xs': (1380, 1520, 1820),
                'patch_h': 384},
    '4x':      {'K': 1.885,  'mag': 4, 'keepout': 70,
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
    #
    # NONE OF THIS IS WHY THE VSS NEVER PRODUCED A CELL, and the wrong guess is
    # recorded because these coordinates are the obvious suspect and are not
    # the culprit. Measured 2026-08-04: four VSS magazines, `0 tracked samples`
    # on every one. Not "few" — zero, so no patch was ever read and no property
    # of these columns could have mattered.
    #
    # The cause was in calibration/sweep.py: FireDriver took the tracker BY
    # VALUE at construction and set_sight() never updated it, so a stale
    # 7-patch tracker asked a freshly-rebuilt 3-region frame for recoil_3..6,
    # slice_frame() returned None and MagazineRecorder.push() dropped every
    # frame. Only this profile has a different patch COUNT, so only the VSS
    # broke. Fixed there; these coordinates were never exercised.
    #
    # THEY HAVE NOW BEEN READ, AND THEY LOOK FINE. The first VSS run after
    # that fix (docs/recoil/runs/vss_after_fix_0804b.jsonl, 14 magazines)
    # reports mean_mad 0.4-2.2 with n_low_gate 1 and n_out_of_range 1 across
    # the lot. mean_mad is how far the three patches disagree WITH EACH OTHER,
    # so bad placement — a patch on the scope tube, or two seeing the same
    # pixels — is precisely what it would show, and it does not.
    #
    # THE COUNT IS THE PROBLEM, NOT THE PLACEMENT, and that makes the
    # patch-size override above the FIX rather than the optimisation it is
    # written as. measure_pair rejects a patch by distance from the MEDIAN of
    # the patches (view_tracker.py: `np.abs(dys_a - med) > thresh`), and a
    # median over three is not robust — least of all when two of the three
    # overlap by 63 px and therefore vote together. The odd one out loses even
    # when it is the correct one.
    #
    # Measured across every weapon fired on 2026-08-04, as the share of
    # individual patch readings thrown away (n_rejected is per PATCH, so it
    # divides by the profile's patch count):
    #
    #     vss      3 patches   27%      <- this profile
    #     vector   7           14%
    #     ump45/aug/mp5k 7     6-7%
    #     m416     7            2.4%
    #     akm      7            1.7%
    #
    # Eleven times the m416. What survives is too sparse to be stable:
    # cum_counts over one cell's magazines ran 760/906/808 in the best case and
    # 740/141/-273 in the worst.
    #
    # So: three non-overlapping 96 px patches at 1262/1362/1995, which needs
    # ViewTracker to take the size per profile instead of globally. Until then
    # the VSS records data and none of it is trustworthy.
    # ⚠ THE 96px NON-OVERLAPPING VERSION WAS TRIED AND DOES NOT HELP. The
    # paragraph above proposes it and the reasoning is appealing — 1265/1330
    # overlap by 63 px, so they vote together in the median measure_pair
    # rejects outliers against, and the third patch loses every disagreement.
    # Measured 2026-08-04 with patch=96 at (1262, 1362, 1995), three disjoint
    # windows still clear of the PSO-1 line:
    #
    #                       rejected/patch   mean_mad   low_gate
    #     128px overlapping     27.5%          1.37         1
    #      96px disjoint        24.4%          1.62         9
    #
    # Rejection barely moved, patch disagreement got slightly worse, and the
    # narrower windows fall under the texture gate nine times as often. The
    # magazines stayed just as wild (cum_counts 770 / 37 / -148 in one cell).
    # Reverted; ViewTracker still takes `patch` per profile, which is worth
    # keeping — the plumbing was missing and now is not.
    #
    # WHAT DOES TRACK IT IS HOW FAR THE VIEW MOVES BETWEEN FRAMES. Rejection
    # rate against median px/frame (cum_px / n_frames, both recorded per
    # magazine), over every weapon fired on 2026-08-04:
    #
    #     vss     26.5%   4.48 px/frame        mp5k/mg3/m249  5-6%   0.1-0.5
    #     vector  13.5%   1.11                 ace32/m416/akm 2-3%   0.3-1.0
    #     ump45/aug 7%    0.6-0.8
    #
    #     r = +0.91 across ten weapons
    #
    # The VSS is the extreme on BOTH axes, at four times the next worst, and it
    # is the only 4x profile — the same view rotation drags the world four
    # times further across the screen. That is consistent with all three
    # geometry theories failing: where the patches sit was never the variable.
    #
    # NOT YET A MECHANISM. Inter-frame displacement is what correlates; why it
    # makes patches DISAGREE (the rejection test is |dy - median| > thresh, and
    # a pure rotation should move them all alike) is unexplained. Motion blur
    # scaling with magnification is the obvious guess and is exactly the kind
    # of guess this weapon has already killed three of. The same axis is worth
    # checking against the magazines lost to fire-rate disagreement — vector is
    # second worst on both.
    'vss_pso1': {'K': 1.875, 'mag': 4, 'keepout': 200,
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
    # tab_open is NOT set here. It used to be toggled on this keypress and
    # corrected by a detection 300 ms later, so for those 300 ms it was a
    # guess -- and a guess gates every `cond: '!tab_open'` below, including
    # whether recoil compensation runs. A swallowed Tab key (see
    # docs/game_quirks.md) left it inverted with nothing to notice.
    # control/tab_watch.py moves it only after looking at the screen.
    {'key': 'tab', 'event': 'press',
     'state': [('stop_recoil', True), ('highlight_gt', 0)],
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
    # Three entries used to live here and are now control/tab_watch.py:
    #
    #   tab_type      @ +300 ms  corrected the toggled tab_open
    #   tab_weapon    @ -50 ms   read the gun names off a buffered past frame
    #   tab_attachment@ -50 ms   ditto for the ten slots
    #
    # The negative delays worked, but they are why every captured frame had to
    # include the Tab regions: DXGI takes one bounding box, and reaching back
    # in time means always having been looking. That was 5.46 ms of every
    # frame. TabWatch keeps the reading fresh while the panel is up instead,
    # so the last one taken IS the final state when it closes.
    #
    # What stays here is what reads the GAMEPLAY HUD, which is captured every
    # frame anyway. `cond: 'tab_open'` still means "this was the Tab CLOSING":
    # the screen does not go away for another 77-128 ms, so a measured
    # tab_open is still True at this instant, exactly as the toggled one was.
    #
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


def _check_table_regions():
    """Every scheduled detection must name a region that is actually captured.

    ScreenCapture.get_crops() drops regions it does not have (`if r in frame`),
    so an entry naming one of the TAB_REGIONS would not raise — the detector
    would just be handed fewer crops and answer from them. AttachmentDetector
    reads a missing crop as an EMPTY SLOT, which is a real answer, so the
    result would be a confident "nothing equipped".

    That is one import-time loop against a whole class of silent wrong answers,
    so it runs on import rather than living in a test nobody runs.
    """
    bad = []
    for name, table in (('DETECT_TABLE', DETECT_TABLE),
                        ('MISMATCH_TABLE', MISMATCH_TABLE)):
        for entry in table:
            for r in entry.get('regions', []):
                if r not in FRAME_REGIONS:
                    bad.append(f'{name} {entry.get("key")!r}/'
                               f'{entry.get("detect")!r} wants {r!r}')
    if bad:
        raise ValueError(
            'these are not captured every frame, so they would arrive as '
            'missing crops:\n  ' + '\n  '.join(bad)
            + '\n\nEither add the region to FRAME_REGIONS (and pay for it on '
              'every frame — see the note there), or read it on demand the '
              'way control/tab_watch.py does.')


_check_table_regions()

# ════════════════════════════════════════════════════════════
# Tab pixel fast-check thresholds
# ════════════════════════════════════════════════════════════

TAB_PIXEL_THRESH = 200
TAB_COUNT_MIN = 150
TAB_COUNT_MAX = 400

# The count band alone is NOT enough, and the failure is not academic: the
# 'type' region sits over the training range's sky, and ADS magnifies a patch
# of near-threshold pale blue into it. On 868 stored ADS frames, 15 land inside
# 150..400 purely from sky. Every one of those reads as "inventory is up", and
# a dozen `cond: '!tab_open'` entries below gate on that — including whether
# recoil compensation runs at all. Aiming at the sky silently disarmed it.
#
# What separates them is not how many bright pixels there are but whether any
# DARK ones remain. 类型 / "Type" is near-white ink on the panel's dimmed
# backdrop, so the crop always keeps a dark floor; sky is uniformly bright.
# The 10th percentile of the per-pixel channel maximum, measured over
# docs/ads/runs (Tab shut) + docs/compat/runs, docs/runs, docs/tab_inventory*
# (Tab up), 960 shots at 3440x1440:
#
#   Tab up                       23 .. 91
#   Tab shut, inside the band   190 .. 199   (the sky frames)
#
# 150 sits in the middle of that 99-wide gap. Note the gap only exists ONCE
# the count band has passed — over all Tab-shut frames the floor runs 27..227,
# because a dark crop with no ink is dark too. The two tests are a conjunction,
# not alternatives. Scored by tools/test_tab_open.py (`pixi run tab-open`).
TAB_DARK_FLOOR_MAX = 150

# ── Tab anchor: is the inventory actually up? ─────────────────────────────
# The ink window above is NOT a safe answer to that on its own. It looks
# perfect on hand-picked negatives — lobby, results, ESC menu and plain
# gameplay all measure exactly 0 — and fails on real frames: of 96 sampled
# ADS captures, 13 carry ink and one lands inside 150..400. Nine measure
# exactly 738, which is 41x18, the whole crop saturated. HUD_REGIONS['type']
# sits over the training range's bright sky and ADS magnifies it into frame.
#
# A count cannot tell "the glyph is drawn" from "everything here is white".
# Glyph IoU can, and bounds that failure by construction: a saturated crop
# matches every template pixel but fills the union too, so it scores at most
# |template|/|crop| = 0.28 (zh) or 0.32 (en) however bright it gets.
#
# TM_CCORR_NORMED was tried first and inverted the problem — negatives
# 0.985..0.999 against positives 0.887..1.000. Do not go back to it.
#
# Measured, best-of-both-languages IoU:
#   open       0.922 .. 1.000   (3 captures, zh and en)
#   closed     0.000 .. 0.352   (5 screens + 96 ADS frames)
# Threshold 0.60 sits in a 0.571-wide gap.
TAB_ANCHOR_MIN_IOU = 0.60
TAB_ANCHOR_SEARCH = 8          # +- px searched around the nominal position

# THE HEADER IS NOT ALWAYS THE SAME GLYPHS: docs/tab_inventory*.png render
# 类型, docs/lobby/in_game_tab.png renders "Type" — same screen, same place,
# different client language. A single-language template scores the other at
# 0.27, below the brightest negative, so the inventory would read as closed
# forever after a language switch. One mask per language, score is the best
# of them. Rebuild with tools/probe_tab_anchor.py --write.
TAB_ANCHOR_LANGS = ('zh', 'en')

# ── Attachment slot: absent / empty / filled ──────────────────────────────
# Three states, and the two cheap judgements each separate a different pair.
# Getting this wrong is what makes a drag land nothing: dragging onto a slot
# the weapon does not have drops the item, and "the gun lacks that slot" is
# indistinguishable from "the part was rejected" if you only watch the mouse.
#
# PRESENCE — gradient along the tile's BORDER RING, nothing else. A slot the
# weapon has draws a pale tile whether or not anything is in it; a slot it
# lacks draws nothing at all. So look at the border and only the border:
#
#   absent    5.0 .. 26.0    (5 slots: UZI grip, Mk12 stock, G36C stock,
#                             VSS muzzle, VSS grip)
#   present  46.0 .. 172.7   (19 slots across 6 captures)
#   threshold 36, in a gap of 20
#
# MEASURE THE BORDER, NOT THE INTERIOR. The interior holds the icon, which is
# "乱七八糟" — arbitrary content that says nothing about whether the tile is
# there. Restricting to the ring makes the judgement almost independent of
# what is fitted: a stripped M416 reads 260/260/278/260 and a fully fitted one
# 260/260/318/260 on the same slots.
#
# GRADIENT, NOT CANNY. Canny with fixed hysteresis (40,120) returned exactly
# 0 for the VSS magazine — a real slot whose tile sits on bright sand at
# almost the same brightness. The border is there, just low-contrast, and
# hysteresis quantises it away. Sobel magnitude at the 90th percentile keeps
# it at 46.
#
# The earlier attempt measured inner-minus-ring contrast. It works for 4 slots
# (absent -0.2..1.7 vs present 10.7..42.5) and is kept as slot_contrast() for
# diagnosis, but it reads the fill rather than the border, so it inherits
# whatever is behind the panel.
TAB_SLOT_TILE = 66             # measured on a stripped M416's muzzle and grip
TAB_SLOT_TILE_OFF = -1         # tile origin, relative to the interior's
TAB_SLOT_RING_HALF = 3         # ring half-width about the border
TAB_SLOT_RING_PAD = 10         # window margin around the tile
TAB_SLOT_PRESENT_MIN = 36.0    # midpoint of 26.0 .. 46.0
#
# WHY THE PADDING, PRECISELY. The tile measures 66x66 and starts one pixel up
# and left of HUD_REGIONS['att_*'] — measured on a stripped M416's muzzle and
# grip, where an empty tile is a clean blob. (magazine and stock could not be
# measured the same way: their connected component merges with a bright
# neighbour, giving 69x94 and 94x95. Two agreeing slots, not five.)
#
# So HUD_REGIONS['att_*'] is 63x63 of tile INTERIOR, and the interior is flat.
# Anything measuring texture inside it — edges, std — reads the same for a
# tile that is empty and for no tile at all, because neither has any texture.
# The padding is not there to catch a border 16px away; it is there to reach
# the BACKGROUND OUTSIDE the tile, so the judgement becomes "is this patch
# brighter than what surrounds it" instead of "what does this patch look
# like". Presence is a contrast, and a contrast needs both sides.
#
# ⚠ SCOPE HAS NO TILE AT ALL, so none of this applies to it and it always
# returns 'unknown'. Confirmed by eye across three captures: an empty scope
# slot on an M416 draws nothing — just the backdrop and the weapon render —
# and a VSS, which has no scope slot, draws its integral PSO-1 in the same
# place because that optic is part of the weapon's own art. Empty-and-present
# is pixel-identical to absent, so this is a property of the UI, not a
# threshold to tune. It also breaks the occupancy test: the VSS reads 678
# interior edges with an empty slot, well past the 120 that means "filled".
# Scope presence has to come from a drag (see the calibrate-compat skill);
# scope CONTENTS come from AttachmentDetector, which reads the VSS as ''
# correctly. Nearly every weapon has one, so little is blocked — but never let
# 'unknown' collapse into 'absent'.
# Window for slot_contrast(), the superseded diagnostic. The presence
# threshold it used to carry lived here too and silently shadowed the ring
# one above — same name, defined later, so 36.0 became 6.0 and a G36C's
# absent stock (ring 8.3) read as present. Keep one threshold per judgement.
TAB_SLOT_PAD = 16
TAB_SLOT_NO_TILE = ('scope',)

# OCCUPANCY — Canny edges inside the interior. The tile is flat, an icon is
# not.  empty 0 (muzzle/grip/stock), 17 (magazine), 71 (scope: weapon render
# showing through); filled 202..885. Threshold 120 sits in that gap.
TAB_SLOT_FILLED_EDGES = 120

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

# ── Mode navigation ──────────────────────────────────────────────────────
# The lobby is two bars of tabs above the mode card:
#
#   top   PLAY  PASS  CAREER  CUSTOMIZE  HIDEOUT  WORKSHOP  STORE
#   sub   NORMAL  RANKED  ARCADE  TRAINING  CUSTOM      (only under PLAY)
#
# LOBBY_PLAY_XY starts whatever the sub bar has selected, so the sub bar is
# not a convenience — it is the guard that keeps an unattended run out of a
# real match. Per detector/CLAUDE.md a real round cannot currently be left:
# only the training range's ESC menu has been captured, so
# leave_entry_confirmed() refuses to click LEAVE anywhere else.
#
# Tabs are found by projection, not hardcoded — see detector/lobby_nav.py.
# The x windows below are the strips the projection runs over.

# (y, x, h, w). Starts at y=38, BELOW the yellow "new content" dots that sit
# at y 26..38 over PASS / CAREER / CUSTOMIZE / WORKSHOP. Including them puts
# 52 ink into four unselected tabs and five tabs read as selected at once.
LOBBY_TOP_BAR_ROI = (38, 1050, 30, 1100)

# (y, x, h, w). Stops at x=2000 deliberately. Run it out to 2150 and a sixth
# "tab" appears at (2128,146) with more ink than CUSTOM — that is the daily
# BEGINNER TRAINING popup, whose left edge is x=1961.
LOBBY_SUB_BAR_ROI = (125, 1300, 40, 700)

# Two working points, and the gap between them IS the selected/unselected
# signal. Selection is argmax of ink at SEL, not a threshold on it: the top
# bar's unselected tabs carry stray ink from nearby decorations.
LOBBY_TAB_FIND_THRESH = 95     # every tab shows up, selected or not
LOBBY_TAB_SEL_THRESH = 170     # only the selected tab survives
LOBBY_TAB_GAP = 14             # merges glyphs in a label, keeps labels apart
LOBBY_TAB_MIN_W = 20           # a label is wider than this; specks are not

# Measured on two live captures, cursor parked, at SEL_THRESH:
#   TRAINING selected   NORMAL 0    RANKED 0  ARCADE 0  TRAINING 896  CUSTOM 0
#   NORMAL selected     NORMAL 829  RANKED 0  ARCADE 0  TRAINING   0  CUSTOM 0
#   after clicking TRAINING         NORMAL 0  ..        TRAINING 891
# Unselected is exactly zero in every sample, so the margin is the selected
# ink itself: 828..896x. A threshold of 5 is three orders of magnitude of
# headroom, and exists to catch an ambiguous read, not to discriminate.
LOBBY_TAB_MIN_MARGIN = 5.0

# HOVER LOOKS EXACTLY LIKE SELECTION. A tab under the cursor lights to the
# same brightness the selected one does, so every capture feeding the probes
# above must park the cursor first — including the read-back that verifies a
# click landed, where the cursor is by definition sitting on the tab just
# clicked. Inside the left letterbox bar, which the lobby paints flat black.
# Not the right bar: LOBBY_BAR_ROI reads that one for the letterbox probe.
LOBBY_PARK_XY = (200, 700)

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
