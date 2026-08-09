import os
from typing import NamedTuple


# ════════════════════════════════════════════════════════════
# Rectangles — ONE type, ONE stored order. The corner form is DERIVED.
# ════════════════════════════════════════════════════════════
#
# ⚠ THERE USED TO BE TWO, AND THE SECOND ONE IS WHAT KEPT COSTING. Row-major
# `(y, x, h, w)` for anything win32_cap or the ring buffer takes, corner
# `(x0, y0, x1, y1)` for containment tests — each correct on its own, and
# together a coin-flip at every call site. The only thing saying which a value
# followed was its VARIABLE NAME: `LOBBY_BAR_ROI` announced it,
# `HUD_REGIONS['gun_name_1']` announced nothing. Measured 2026-08-08, on that
# exact key: `x1, y1, x2, y2 = HUD_REGIONS[k]` unpacks, slices, raises
# nothing, and describes a DIFFERENT rectangle. The crop came back empty and
# the investigation went looking at the template bank.
#
# Naming the two types was not enough, and it was not enough for a reason
# worth keeping: "which convention is this" is still a question you have to
# ask, and the answer still has to be remembered. Asked for in those terms:
# "全局只用一种吧，不然永远会晕."
#
# So: ONE class, ONE stored order, and the corner view is a PROPERTY rather
# than a second storage form. `r.x0` cannot disagree with `r.x` — it is
# computed from it. There is nothing left to get backwards.
#
# Row-major won on arithmetic, not taste: 76 of the 89 slice sites in the
# repository are `frame[y:y + h, x:x + w]`, and it is the argument order
# win32_cap already takes. detector/geometry.cut() stays as the one-liner for
# the simple case; its own docstring explains why it cannot be the whole
# answer (61 of those 76 need y/x/h/w for something else as well).
#
# A NamedTuple, so this costs no call site anything: it IS a tuple, every
# existing `y, x, h, w = REGION` keeps working verbatim, and `repr` prints the
# field names.
class Rect(NamedTuple):
    """A screen rectangle, stored row-major. Corners are derived.

    The ONLY rectangle type in this repository. `pixi run params` and the
    import-time check at the bottom of this file both enforce that.
    """
    y: int
    x: int
    h: int
    w: int

    # Corner view. Read-only on purpose: a settable corner would be a second
    # storage form wearing a property's clothes.
    @property
    def x0(self):
        return self.x

    @property
    def y0(self):
        return self.y

    @property
    def x1(self):
        return self.x + self.w

    @property
    def y1(self):
        return self.y + self.h

    @classmethod
    def corners(cls, x0, y0, x1, y1):
        """Build from corner points. For a measurement TAKEN that way — the
        conversion happens once, here, instead of at every reader."""
        return cls(y0, x0, y1 - y0, x1 - x0)

    def contains(self, xy):
        """Is this point inside. -> bool. Half-open, like the slice."""
        return (xy is not None
                and self.x0 <= xy[0] < self.x1 and self.y0 <= xy[1] < self.y1)

    @property
    def slice(self):
        """`frame[r.slice]` — for the sites that only want the sub-image."""
        return (slice(self.y, self.y + self.h), slice(self.x, self.x + self.w))


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
    # Rounds left in the magazine — both ends of the calibration cycle: static
    # count = mag empty, count jumping back up = reload done. Far more robust
    # than timing fire/reload per weapon. Fits three digits (100-round drums)
    # right-aligned; stops short of the spare-ammo symbol at x=1760.
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

# Wrapped AFTER the literal so the table stays readable as a table.
HUD_REGIONS = {k: Rect(*v) for k, v in HUD_REGIONS.items()}

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
# The cost of height is intra-patch gain error — the same (y/f)^2 as above,
# now within the patch. At f~1720 the edge at y=+-128 is off by 0.55%, ~0.2%
# averaged over the patch, negligible against the 5% effects being measured.
RECOIL_PATCH_H = 256
# +-128 px is ample at 1x and NOT ample through a scope: the sight magnifies
# the picture, so one bullet's kick covers 2-4x the pixels and measure_pair
# starts refusing readings it should have made.
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
# THE THREE AIM STATES are defined in detector/ads_detector.py: hip fire (腰射,
# button not touched), shoulder aim (肩射 / tactical aim, button HELD, third
# person, a state of its own), ADS (开镜, button tapped -- it is a toggle).
# `hipfire` below means the FIRST of those.
#
#   sight     K        R^2       CV     patches
#   hipfire   ~0.50    -         -      7     (TPP, no weapon; needs redo armed)
#
# ⚠ THAT PARENTHESIS IS LOAD-BEARING. control/aim.py positions the pitch from
# hip fire for every optic (goto_midline). It is NOT a reason to distrust the
# pitch work — that travel is in COMMANDED COUNTS and K enters only `predict`,
# a tolerance on "did the picture change", never the answer. It does invalidate
# any counts-to-pixels claim about hip fire: redo it armed first. Labels lie
# easily here — calibrate_k's `--ads` held the right button, which is SHOULDER
# AIM, so four runs carried the wrong state; and an ADS-verified red dot
# re-measured 1.29 against the 1.5474 that stood below AT THE TIME (the table
# now reads 1.5413; the 1.29 is what makes the point either way, since it is
# 16% off both).
#   red_dot   1.5413   -         0.3%   7   <- 2026-08-08, ONE correlation
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
    'hipfire': {'K': 0.50,   'mag': 1, 'keepout': RECOIL_KEEPOUT,
                'patch_xs': RECOIL_PATCH_XS},
    # ⚠ 1.5474 -> 1.5128 on 2026-08-08, and the OLD one was never cleanly
    # measured. Two things said K was high (the scale sweep's F-coefficient
    # slope, and compensation-OFF magazines reading 6.4% below compensation-ON
    # in the same cell, 5.3 sigma), so the four stored red_dot runs were
    # re-read. 76 of their 98 rows have their tracked patches disagreeing by a
    # median of 76%, and the 22 survivors still ranged 0.39..1.59 WITH the
    # patches agreeing to 0.3% -- because the correlator ALIASES BY EXACTLY
    # RECOIL_PATCH_H = 256 px past its unambiguous range, every patch the same
    # way, and `max_abs_frame` cannot see it (an aliased pair reports a SMALL
    # displacement). tools/audit_k.py, pixi run k-audit.
    #
    # Re-measured with the injection spread over 1.0 s instead of 0.15 so no
    # frame gap approaches 128 px, gun in hand, red dot read back, 32 trials
    # across two magnitudes and both directions:
    #
    #     K = 1.5128   R^2 0.99984   CV 1.1%   sem 0.003
    #     -240 1.5117  -120 1.5191  +120 1.5285  +240 1.5083
    #     up/down asymmetry 0.19%
    #
    # 1.5474 is 2.29% above it, which against sem 0.003 is 11 sigma.
    #
    # ⚠ AND THE ARCHIVE'S "6.32% up/down asymmetry" IS NOT REAL. It measures
    # 0.19% here. That archive figure came from an `up` arm of n=3, all from
    # one run, all needing un-aliasing first -- and a whole argument was built
    # on it (that 1.5474 was a blend of two directions and K_down was the right
    # one). The argument is void; the constant it argued for happened to move
    # the same way for a different reason.
    #
    # ⚠ THE STORED MAGAZINES KEEP 1.5474 and that is a different case from
    # comp_lag_s and fire_delay_ms. Those record what the MACHINE DID, so a
    # later constant would describe a different machine. K is a property of the
    # game that did not change -- only the estimate did -- so re-analysing the
    # store at 1.5128 is legitimate, and it is not done silently here. Doing it
    # takes the two arms from 6.41% (5.3 sigma) to 4.07% (3.4 sigma): a third
    # of the gap, right sign. The rest is still open.
    #
    # ⚠ READ THE NEXT PARAGRAPH BEFORE ACTING ON THIS ONE. Everything above is
    # the record AS IT STOOD, kept in order because each layer says what
    # overturned the one before it -- but 1.5128 was itself retired hours later
    # and re-analysing the store at it would now be re-analysing at a value
    # nothing holds. The live constant is the last one named in this block.
    # ⚠ 1.5128 -> 1.5413, and the reason retires BOTH earlier numbers. K was
    # measured two ways that disagreed by 9.4 sigma (1.5171 with duplicate
    # frames dropped, 1.5520 with them kept), and the only difference between
    # them is HOW MANY FRAME PAIRS share the same total motion -- dropping a
    # frame correlates k-1 against k+1, halving the pair count and doubling the
    # displacement per pair.
    #
    # tools/probe_correlator_bias.py puts the same 70 counts through ONE
    # correlation and through ~223 of them, alternating, both directions,
    # scoped, no click:
    #
    #     one-pair    n=16   K = 1.5413   sd 0.0043   sem 0.0011
    #     many-pairs  n=16   K = 1.6574   sd 0.0185   sem 0.0046
    #     +7.54%, 24.4 sigma
    #
    # THE CORRELATOR OVER-READS EVERY PAIR BY ABOUT 0.04 px AND IT ACCUMULATES.
    # So both stored numbers were accumulation artefacts and the unbiased one
    # is the single correlation: 1.5413.
    #
    # ⚠ THAT LAST PARAGRAPH IS TRUE ONLY AT delta ~ 0.6 px, WHICH IS WHERE THAT
    # PROBE'S SPREAD ARM HAPPENED TO SIT. --grid re-ran it as a (step size x
    # spread) sweep, twice, and the per-pair over-read is a CURVE that CHANGES
    # SIGN (MODEL.md, the constants table):
    #
    #     delta px      0     0.6     1.1    2.0-2.2    3.8-4.4     7-8
    #     b px/pair  +0.000  +0.047  +0.076   +0.039     -0.098    -0.21
    #                (still)                 (a burst's median is 2.0)
    #
    # Small motion over-reads, large motion under-reads, zero crossing at
    # delta ~ 3 px -- and a real magazine's pairs (p25 0.90 / median 2.00 /
    # p75 3.78) straddle it, so the doses largely cancel. Projected pair by
    # pair over all 272 stored magazines: -0.28% (run A), -0.81% (run B).
    # NOT +7.5%, not +1.2%, and NEGATIVE. Both candidate models -- fixed px
    # per pair, fixed fraction of delta -- are rejected at chi2/dof > 390.
    #
    # ⚠ THE VALUE STAYS 1.5413 BECAUSE IT DID NOT REPLICATE, NOT BECAUSE IT
    # DID. The two runs' burst-weighted effective K bracket it:
    #
    #     K_eff   run A 1.5416     run B 1.5250     stored 1.5413
    #     K_true  run A 1.5459     run B 1.5374     (one-pair extrapolation)
    #
    # Within a run the four step sizes (20/35/50/70) agree to +-0.1% and the
    # sem is 0.06%; BETWEEN runs K_true moves 0.55%, nine times that.
    #
    # ⚠ THAT IS NOT K DRIFTING. MODEL.md's "no drift" is a PREMISE, not a
    # finding:
    # y_true is a FIXED curve and nothing here drifts, because a curve fitted
    # today and played tomorrow presupposes exactly that. So two readings that
    # disagree mean AT LEAST ONE OF THEM IS WRONG, and the job is to find the
    # fault -- not to name the disagreement and write it into the spec. That
    # night alone produced four faults big enough to do it: the view driven
    # into the pitch clamp (sky reads as "it did not move"), a frame-grab loop
    # wedged for eight minutes, hip fire's non-flat count ruler with the arms
    # travelling different distances, and arm-vs-time confounded in the older
    # data. Until one of those is shown to produce 1%, changing K to either
    # reading has no grounds.
    #
    # ⚠ WHAT DID REPLICATE, AND IS THEREFORE USABLE: a static scene does not
    # drift (+0.00018 and +0.00001 px per pair), and b depends on delta ALONE
    # -- two cells sharing a delta at 2x the pair count and 2x the total agree
    # to 0.2..0.8 sigma across four such comparisons, in a test where K_true
    # cancels exactly. The SHAPE is established; the LEVEL is not.
    'red_dot': {'K': 1.5413, 'mag': 1, 'keepout': RECOIL_KEEPOUT,
                'patch_xs': RECOIL_PATCH_XS},
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
    # non-overlapping 128 patches — hence the overlap below.
    #
    # THE PLACEMENT IS NOT WHAT AILS THIS PROFILE. Recorded because these
    # coordinates are the obvious suspect, and have now been acquitted three
    # times over:
    #
    #  - `0 tracked samples` on four magazines was calibration/sweep.py, not
    #    geometry: FireDriver took the tracker BY VALUE at construction and
    #    set_sight() never updated it, so a stale 7-patch tracker asked a
    #    rebuilt 3-region frame for recoil_3..6, slice_frame() returned None
    #    and every frame was dropped. Only this profile has a different patch
    #    COUNT, so only the VSS broke. These columns had never been read.
    #  - Once read they look fine: 14 magazines at mean_mad 0.4-2.2, with
    #    n_low_gate 1 and n_out_of_range 1 across the lot
    #    (docs/recoil/runs/vss_after_fix_0804b.jsonl). mean_mad is how far the
    #    patches disagree WITH EACH OTHER, so a patch on the scope tube or two
    #    seeing the same pixels is precisely what it would show.
    #  - The appealing fix — 1265/1330 overlap by 63 px, so they vote together
    #    in the median measure_pair rejects outliers against, and the odd one
    #    out loses even when it is right — was tried and does not help.
    #    patch=96 at (1262, 1362, 1995), three disjoint windows still clear of
    #    the PSO-1 line:
    #
    #        128px overlapping   27.5% rejected/patch   mad 1.37   low_gate 1
    #         96px disjoint      24.4%                      1.62            9
    #
    #    Narrower windows fall under the texture gate nine times as often and
    #    the magazines stayed just as wild (cum_counts 770 / 37 / -148 in one
    #    cell). Reverted; ViewTracker still takes `patch` per profile, which is
    #    worth keeping.
    #
    # WHAT DOES TRACK IT IS HOW FAR THE VIEW MOVES BETWEEN FRAMES. Rejection
    # rate (per PATCH, so it divides by the profile's patch count) against
    # median px/frame, over every weapon fired on 2026-08-04:
    #
    #     vss     26.5%   4.48 px/frame        mp5k/mg3/m249  5-6%   0.1-0.5
    #     vector  13.5%   1.11                 ace32/m416/akm 2-3%   0.3-1.0
    #     ump45/aug 7%    0.6-0.8              r = +0.91 across ten weapons
    #
    # The VSS is the extreme on BOTH axes, at four times the next worst, and it
    # is the only 4x profile — the same view rotation drags the world four
    # times further across the screen. That is consistent with all three
    # geometry theories failing: where the patches sit was never the variable.
    #
    # NOT YET A MECHANISM. Why inter-frame displacement makes patches DISAGREE
    # (the test is |dy - median| > thresh, and a pure rotation should move them
    # all alike) is unexplained. Motion blur scaling with magnification is the
    # obvious guess and is exactly the kind this weapon has already killed
    # three of. Worth checking the same axis against the magazines lost to
    # fire-rate disagreement — vector is second worst on both.
    # ⚠ 1.875 -> 1.8283 (2026-08-09). THE OLD NUMBER WAS NEVER MEASURED: it
    # sat 0.8% off RECOIL_K_DEFAULT_SCOPED, which is what an unmeasured sight
    # falls back to, and nothing in the tree records a run that produced it.
    #
    # Measured the same way as p90_integral — a RATIO against the red dot in
    # the same calibrate_k flavour, because calibrate_k accumulates the
    # correlator's per-pair bias while red_dot's stored 1.5413 comes from the
    # one-pair probe. See the p90 block above for why a level is not
    # comparable and a ratio is.
    #
    #     counts   K vss   K red_dot   ratio    sem
    #         50  1.8948      1.5927  1.1897  0.47%
    #        100  1.8840      1.5929  1.1827  0.42%
    #        200  1.8686      1.5789  1.1835  0.58%
    #        300  1.8595      1.5548  1.1960  0.72%
    #
    #     1.5413 x 1.1862 = 1.8283
    #
    # ⚠ ALL FOUR AMOUNTS AGREE HERE (1.1827..1.1960) where the p90's 300 row
    # broke away, so the 50/100 restriction costs nothing on this sight and is
    # kept only so the two constants are derived the same way.
    #
    # ⚠ THE TWO SIDES USED DIFFERENT PATCH GEOMETRY and it cannot be helped:
    # this profile has 3 columns because four of the default seven land on the
    # PSO-1's tube, where they are fixed to the screen and read a clean zero.
    # The vss side was the cleaner of the two — per-patch spread 0.50%, "no
    # positional bias", against a red_dot run with one patch at std 0.25.
    #
    # ⚠ K IS NOT WHAT MAKES A CELL AGREE, and the vss is the measurement that
    # says so: re-analysing its magazines at the new K moved the arm
    # disagreement 19.7% -> 18.9%. K biases only the weak arms, where y_obs is
    # most of the answer, and 2.6% of that is worth ~15 counts. Firing BALANCED
    # arms — six magazines each at 0.5x and 1.0x — did not settle the cell
    # either: 6.5% over the gate's window. What it settled is what the number
    # MEANS. Most of the 19.7% was one arm holding one magazine; the 6.5% is a
    # real arm dependence, and only balanced arms tell those apart. Arms are
    # compared one-for-one whatever their count, so plan magazines per ARM
    # (calibration/CLAUDE.md).
    #
    # One run per side. Not replicated.
    'vss_pso1': {'K': 1.8283, 'mag': 4, 'keepout': 200,
                 'patch_xs': (1265, 1330, 2010)},
    # The p90's optic is part of the gun -- no scope slot, so nothing on screen
    # names it and detector.weapon.INTEGRAL_SIGHT does (2026-08-09). Before that
    # entry existed the curve was filed under `integral` while the lookup asked
    # for `iron`, so the p90 had NEVER PLAYED ITS CURVE and the only symptom was
    # `no fitted curve ... NOT compensating` -- a line the m416 prints for the
    # same slot, where it is true. The vss had the same fault.
    #
    # ⚠ MEASURED AS A RATIO, NOT AS A LEVEL, and that is the whole point.
    # calibrate_k drops duplicate frames, so it ACCUMULATES the correlator's
    # per-pair bias b(delta) -- which is why red_dot's stored 1.5413 comes from
    # the one-pair probe and NOT from calibrate_k. A number straight out of
    # calibrate_k is therefore not comparable with the entries above it. What IS
    # comparable is two calibrate_k runs divided by each other: the bias is
    # multiplicative and largely cancels, the same reason kit factors are
    # measured as ratios inside one run.
    #
    #     same command both sides: --ads --inject-s 1.0, 24 trials, 7 patches
    #     counts  px/frame   K p90   K red_dot   ratio   sem
    #         50      1.71  1.6197      1.5927  1.0170  0.24%
    #        100      3.05  1.6161      1.5929  1.0145  0.16%
    #        200      5.54  1.6070      1.5789  1.0178  0.56%
    #        300      8.37  1.6125      1.5548  1.0371  0.68%   <- see below
    #
    #     1.5413 x 1.0157 = 1.5656
    #
    # ⚠ THE 300 ROW IS EXCLUDED ON A STATED RULE, NOT BECAUSE IT DISAGREES.
    # b(delta) is a curve that changes sign near 3 px/pair, so a ratio only
    # cancels where both sides sit at the same delta -- and K is USED on a real
    # magazine, whose pairs run p25 0.90 / median 2.00 / p75 3.78 px. The 50 and
    # 100 rows bracket that median; 300 is at 8.4 px/frame, off the end of the
    # regime this constant is ever applied in. 200 is outside p75 and agrees
    # anyway, which is the check that the rule is not just fitting the answer.
    #
    # ⚠ ONE RUN PER SIDE. NOT REPLICATED. The red_dot block above spells out why
    # that matters (K_true moved 0.55% between two runs, nine times the
    # within-run sem), so treat 1.5656 as good enough to FIRE with, not as an
    # established constant. It survives on the compensated arm regardless: there
    # |y_obs| is ~36 counts against ~830, so a K error is divided by ~23.
    #
    # ⚠ patch_xs IS THE DEFAULT ON PURPOSE. Both runs flagged unstable patches
    # -- p90 at index 3,4 and red_dot at index 6 -- and DIFFERENT patches on the
    # two runs means world content, not the sight. Baking one run's bad patches
    # into the profile would record a transient as a permanent property.
    'p90_integral': {'K': 1.5656, 'mag': 1, 'keepout': RECOIL_KEEPOUT,
                     'patch_xs': RECOIL_PATCH_XS},
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
    # THE POSTURE ICON IS ONLY PAINTED WHILE THE SIGHT IS UP, and all three of
    # these fire when it usually is not: c and z are pressed before aiming, and
    # 350 ms after a right-button release the sight is often already down. A
    # one-shot read there returns None, set_posture discards it, and the STALE
    # posture survives into a compensation factor wrong by up to 2x (standing
    # 1.0 against prone 0.50). That is the vertical wander mid-burst.
    #
    # The delays are NOT the problem, which is worth writing down because they
    # were the obvious suspect and two rounds of work went at them. Measured
    # 2026-08-05 over six random viewpoints (tools/probe_posture_trace.py,
    # docs/posture/traces/20260805_094215) with a weapon out:
    #
    #   sight ALREADY up, key pressed at t=0   icon follows in  34..68 ms
    #   sight already up, any sample           readable 3786/3787
    #   sight DOWN, full 2000 ms window        readable       0, all 6 rounds
    #
    # So 200 ms clears the real latency three times over. What no delay can
    # clear is a frame with no icon painted on it — hence `retries`, which
    # re-reads until the sight comes up. Bounded: if it has not come up within
    # 1.0 s of a stance change nothing is being aimed, the stance does not
    # matter yet, and the next right-button event brings its own read.
    {'key': 'c', 'event': 'press', 'detect': 'posture',
     'regions': ['posture'], 'delay': 200, 'retry_ms': 100, 'retries': 10,
     'cond': '!tab_open', 'result': 'posture'},

    {'key': 'z', 'event': 'press', 'detect': 'posture',
     'regions': ['posture'], 'delay': 200, 'retry_ms': 100, 'retries': 10,
     'cond': '!tab_open', 'result': 'posture'},

    {'key': 'right', 'event': 'release', 'detect': 'posture',
     'regions': ['posture'], 'delay': 350, 'retry_ms': 100, 'retries': 10,
     'cond': '!tab_open', 'result': 'posture'},

    # ── Tab ──
    # Three entries used to live here and are now control/tab_watch.py:
    #
    #   tab_type      @ +300 ms  corrected the toggled tab_open
    #   tab_weapon    @ -50 ms   read the gun names off a buffered past frame
    #   tab_attachment@ -50 ms   ditto for the ten slots
    #
    # The negative delays worked, and they are exactly why every captured frame
    # had to carry the Tab regions (see FRAME_REGIONS): reaching back in time
    # means always having been looking. TabWatch keeps the reading fresh while
    # the panel is up instead, so the last one taken IS the final state when it
    # closes.
    #
    # What stays here reads the GAMEPLAY HUD, captured every frame anyway.
    # `cond: 'tab_open'` still means "this was the Tab CLOSING": the screen does
    # not go away for another 77-128 ms, so a measured tab_open is still True at
    # this instant, exactly as the toggled one was.
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

# What DECIDES, with the two above demoted to a pre-filter: the median HSV
# saturation of the BRIGHT pixels. The glyphs are pure white; the world is not.
#
# WHY THE COUNTS ARE NOT ENOUGH. One live viewpoint on 2026-08-05, Tab
# genuinely SHUT, the window over a tree against sky, read count 299 / floor 59
# where the real panel reads count 204 / floor 60 — the false case beating the
# true one on the very feature meant to catch it. The trunk supplies the dark
# floor and the sky between the branches supplies the bright count.
#
# TWO OTHER FEATURES WERE MEASURED FIRST AND BOTH FAILED ON THAT TREE, which is
# why this constant is a saturation and not something cleverer:
#
#   glyph template   held-out Tab-up scored 0.097 where shut reached 0.203 —
#                    a mask built at one backdrop does not transfer (the label
#                    carries 210 ink at one and 249 at another, 19% thicker)
#   Laplacian var    open 7615..25524, tree 13735..14275 — dead centre
#   median saturation  open 0.000 (all 48), shut 0.030..0.310, tree 0.089
#
# Measured over 48 Tab-up and 880 Tab-shut stored frames. The gate sits in an
# empty band: every open frame is EXACTLY 0.000 and no shut frame is under
# 0.030. Rerun `pixi run tab-open` after touching it.
TAB_TYPE_SAT_MAX = 0.015

# ── Tab anchor: is the inventory actually up? ─────────────────────────────
# The ink window above is NOT a safe answer to that on its own. It looks
# perfect on hand-picked negatives — lobby, results, ESC menu and plain
# gameplay all measure exactly 0 — and fails on the same ADS sky as above: of
# 96 sampled ADS captures, 13 carry ink, one lands inside 150..400, and nine
# measure exactly 738, which is 41x18, the whole crop saturated.
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
# WHY THE PADDING. The tile starts one pixel up and left of the interior, and
# only muzzle and grip could be measured that way: magazine and stock merge
# their connected component with a bright neighbour, giving 69x94 and 94x95.
# Two agreeing slots, not five.
#
# So HUD_REGIONS['att_*'] is 63x63 of tile INTERIOR, and the interior is flat.
# Anything measuring texture inside it — edges, std — reads the same for a
# tile that is empty and for no tile at all. The padding reaches the
# BACKGROUND OUTSIDE the tile, so the judgement becomes "is this patch
# brighter than what surrounds it". Presence is a contrast, and a contrast
# needs both sides.
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
# ⚠ ONE THRESHOLD PER JUDGEMENT. slot_contrast()'s window (TAB_SLOT_PAD = 16)
# stood here until 2026-08-07 and went with the function; what must not go is
# why it was dangerous. The presence threshold it ALSO carried lived here too
# and silently shadowed TAB_SLOT_PRESENT_MIN above — same name, defined later,
# so 36.0 became 6.0 and a G36C's absent stock (ring 8.3) read as present.
TAB_SLOT_NO_TILE = ('scope',)

# OCCUPANCY — Canny edges inside the interior. The tile is flat, an icon is
# not.  empty 0 (muzzle/grip/stock), 17 (magazine), 71 (scope: weapon render
# showing through); filled 202..885. Threshold 120 sits in that gap.
#
# ⚠ REPORTED, NOT JUDGED. "71 (scope: weapon render showing through)" above is
# the reason: on an AKM's MAGAZINE the same effect measures 395 with the gun
# stripped bare. slot_detector's docstring has what that cost.
TAB_SLOT_FILLED_EDGES = 120

# OCCUPANCY: the best template MSE in the tile. Filled means A PART IS
# RECOGNISED; the weapon's own render and the scenery behind the panel are then
# one case, "not a part", which needs a model of neither.
#
#   fitted tiles   p50 15.2   p90 40.3   p99 89.2     1685 captures
#   bare AKM mag        346.6                         run 20260804_211054
#   empty tiles    min 891    p50 2750                24 measurable of 281
TAB_SLOT_MATCH_MAX = 150

MISMATCH_POLL_INTERVAL = 500   # ms between mismatch polls
GT_SETTLE_TIME = 500           # ms after a GT change before polling (HUD animation)

# ════════════════════════════════════════════════════════════
# Spawner screen — the training range's item-spawner panel
#
# Identified by the three save-loadout / load-loadout / equip-lv3 button
# glyphs at the bottom right, which appear on no other screen. Measured over
# three captures with different scenes behind the panel (docs/spawner/
# README.md §4): the glyphs are achromatic (|max-min| over B,G,R <= 2
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
LOBBY_BAR_ROI = Rect(200, 3060, 1000, 140)
LOBBY_BAR_MAX = 8              # lobby measures exactly 0; in-game 78..255

# Net-debug overlay ("Ping: 43ms ..."), the one signal independent of the
# bars. Restricted to x >= LOBBY_IMAGE_X0 so it reads the lobby's own image
# rather than the black bar, which would make it a second letterbox probe.
# Bright-pixel fraction: lobby 0.021, in-game 0.099..0.112.
LOBBY_PING_ROI = Rect(0, 460, 26, 240)
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
# Tabs are FOUND by projection and NAMED by template — see detector/lobby_nav.
# The x windows below are the strips the projection runs over.

# (y, x, h, w). Starts at y=38, BELOW the yellow "new content" dots that sit
# at y 26..38 over PASS / CAREER / CUSTOMIZE / WORKSHOP. Including them puts
# 52 ink into four unselected tabs and five tabs read as selected at once.
LOBBY_TOP_BAR_ROI = Rect(38, 1050, 30, 1100)

# (y, x, h, w). Stops at x=2000 deliberately. Run it out to 2150 and a sixth
# "tab" appears at (2128,146) with more ink than CUSTOM — that is the daily
# BEGINNER TRAINING popup, whose left edge is x=1961.
#
# The top bar has the same problem and CANNOT be cropped out of it: on the
# Chinese client the labels are narrow enough that the green event icon lands
# at x 2091..2124, inside this ROI, and segments as an 8th run. Trimming the
# ROI to exclude it would cut into 商店 in one language or STORE in the other.
# That is why naming is by template now — an extra run matches nothing and is
# dropped, instead of shifting every name after it.
LOBBY_SUB_BAR_ROI = Rect(125, 1300, 40, 700)

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

# Naming gate: TM_CCOEFF_NORMED of a run's mask against the stored label masks
# in data/templates/pubg_assets/lobby/tabs/. Below it the run is nameless, which
# is the correct answer for the event icon and for a label in a language this
# repo has no template for. Measured by
# `calibration/build_lobby_tab_templates.py --verify`; set from that report, and
# rerun it after touching the templates.
LOBBY_TAB_TMPL_MIN = 0.55

# Horizontal slack when cutting a run's window to match against. The selected
# and unselected renderings of one label differ by a pixel or two in width
# (TRAINING measured 1757..1844 selected, 1758..1843 unselected), and
# matchTemplate needs the window to be at least as large as the template.
LOBBY_TAB_TMPL_PAD = 6

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
LOBBY_EXIT_TEXT_ROI = Rect(1350, 100, 26, 210)   # (y, x, h, w)
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
LOBBY_MENU_TITLE_ROI = Rect(150, 571, 66, 402)   # (y, x, h, w)
LOBBY_MENU_THRESH = 190
LOBBY_MENU_SEARCH = 24
LOBBY_MENU_MIN_SCORE = 0.55

# ESC IN THE LOBBY RAISES THE SAME MENU 315 px LOWER, and until 2026-08-07
# nothing here could see it. Same title, same glyphs, same x, same 85.3 px
# pitch — only the block is centred on the screen instead of sitting near the
# top, and the fourth entry reads RESTART LOBBY instead of LEAVE TRAINING:
#   title 465..529, then RESUME 607 / SETTINGS 693 / KEY GUIDE 778 /
#   RESTART LOBBY 863 / EXIT TO DESKTOP 949
# The SAME template scores 0.999 at both positions, so this is a second search
# window, not a second template.
#
# ⚠ WITHOUT IT THE STATE IS A LIE, and in the expensive direction: the lobby
# is letterboxed with or without the menu over it, so bar_max reads 0 and the
# frame classifies as LOBBY (measured on the capture: bar_max 0, ping_frac
# 0.000). press_play() then clicks PLAY into a modal that swallows it, which
# is the same shape as the ERROR and RECONNECT dialogs already handled here —
# a screen that sits OVER the lobby and eats the one click the pump retries.
LOBBY_MENU_TITLE_ROI_IN_LOBBY = Rect(465, 570, 66, 402)   # (y, x, h, w)

# ⚠ EXIT TO DESKTOP EXISTS ON BOTH MENUS, at two different y, and it is the
# only entry that does. LEAVE TRAINING / RESTART LOBBY are at least unique to
# their own screen; this one is the same word at 634 in a match and 949 in the
# lobby, so a coordinate that is right on one is a live quit-the-game click on
# the other. Nothing here clicks it — control/lobby.py quits by terminating
# the process, which works from screens that have no menu at all.

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
LOBBY_LEAVE_TEXT_ROI = Rect(527, 570, 46, 316)   # (y, x, h, w)
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
LOBBY_LEAVE_CONFIRM_TEXT_ROI = Rect(558, 1495, 65, 450)   # (y, x, h, w)
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
LOBBY_ERROR_TEXT_ROI = Rect(500, 1628, 65, 186)   # (y, x, h, w)
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
LOBBY_RECONNECT_TEXT_ROI = Rect(881, 1689, 23, 119)   # (y, x, h, w)
LOBBY_RECONNECT_MIN_SCORE = 0.55
LOBBY_RECONNECT_XY = (1730, 906)

# ── In-match map (M) ─────────────────────────────────────────────────────
# The training range map doubles as a teleporter: each practice area is drawn
# as a translucent yellow box, and clicking one moves the character there.
#
# WHY A RUN WANTS THIS. Spawning drops everyone at the main compound, which on
# a populated server has people driving through it. A measurement round that
# gets rammed loses the magazine and does not necessarily know it did. The
# 200m range is a lane off to the side of that.
#
# Boxes are (x0, y0, x1, y1) of the highlight's own bounds, measured off
# docs/map/map_400m.png by map_detector.highlight_box(), which is also how
# they get re-measured when a patch moves the map. The click target is the box
# centre -- a CONSTANT, not a per-run measurement, for the same reason the
# spawner's entry points are constants (detector/CLAUDE.md: nothing on the
# driving path may depend on recognising the thing it drives).
MAP_RANGE_BOXES = {
    '200m': (1937, 460, 1999, 622),
}

# Measured as corner points, so converted here rather than at every reader.
MAP_RANGE_BOXES = {k: Rect.corners(*v) for k, v in MAP_RANGE_BOXES.items()}

# ⚠ NAMED FIELDS, NOT INDICES, AND THIS LINE IS WHY. It read
# `((b.x0 + b.x1) // 2, (b.y0 + b.y1) // 2)` — correct corner arithmetic, on a
# value that had just stopped being corners. Rect(y=460, x=1937, h=162, w=62)
# put the 200m click point at (311, 999) instead of (1968, 541), and the
# teleport spent eight attempts clicking the far side of the map. It moved the
# character somewhere real, so it did not even fail cleanly.
#
# NOTHING CAUGHT IT. config's import-time ratchet checks that the constants are
# Rects, not that arithmetic ON them was updated; map_detector's 18-case
# selftest stayed green because it never reads this dict. A type that carries
# its field names does not help a reader who still writes indices — so do not
# write indices.
MAP_RANGE_XY = {name: ((b.x0 + b.x1) // 2, (b.y0 + b.y1) // 2)
                for name, b in MAP_RANGE_BOXES.items()}

# Where a landed teleport actually puts the marker: the range's spawn point,
# drawn at the top edge of the highlight and slightly OUTSIDE it. Measured on
# the first live run, 2026-08-06 -- ONE sample each.
#
# This is separate from the box on purpose. Padding the box until it admitted
# the arrival point also widened it toward the neighbouring lane, so a
# character standing next door read as "already at the 200m range" and the
# teleport was skipped. Arrival is a point question, occupancy is an area one.
MAP_RANGE_SPAWN = {
    '200m': (1977, 450),
}

# ⚠ THE MINIMAP DRAWS THE SAME PLAYER MARKER, and it is on screen whenever the
# big map is NOT. So "I can see a yellow disc" is true essentially always, and
# the first live frame taken after map_detector was written said map_open=True
# with the map shut — the marker it found was at (3222, 1227), in this corner.
#
# The two are mutually exclusive (opening the map hides the minimap), so
# excluding this rectangle turns the marker back into a map signal. Excluding
# it costs nothing: the big map's own UI ends around x=3000, so nothing it
# draws ever lands here.
#
# Measured off docs/map/ingame_minimap.png: the minimap occupies
# x[3050,3405) y[1050,1405). Padded by 20 px, then out to the screen edge.
#
# ⚠ _BOX, NOT _ROI. Every *_ROI in this file is (y, x, h, w) -- the row-major
# order detector/geometry.py's cut() exists to enforce, and getting it wrong
# does not raise, it silently returns a different rectangle. This one is corner
# points like MAP_RANGE_BOXES, because it is tested against and sliced with,
# never cut(). The suffix is the only thing that says which convention a
# constant follows, so it has to stay honest.
MINIMAP_BOX = Rect.corners(3030, 1030, SCREEN_W, SCREEN_H)

# Width of the strip holding the map's left-hand list of training areas. The
# selected entry is drawn with a yellow border -- two vertical strokes at
# x=80, 320 px of them -- and that is the map-open signal that does NOT move
# when the map is panned or zoomed. Both stored frames with the map shut read
# 0 px of yellow in this strip.
MAP_LEFT_PANEL_W = 420

# Hovering a range pops a preview card ~40 px down-right of the cursor. It
# does not cover the marker at any range measured so far, but the read-back
# that verifies a jump is by definition taken with the cursor sitting on the
# box just clicked -- the same trap LOBBY_PARK_XY exists for. Parked in the
# blurred game world left of the map, which draws no UI and no hover state.
MAP_PARK_XY = (450, 1200)

# ════════════════════════════════════════════════════════════
# Training / assets
# ════════════════════════════════════════════════════════════

# Only fire_mode is left, because it is the only surviving synthetic-training
# task. The 'weapon' entry pointed at a directory deleted on 2026-08-05 (see
# detector/weapon_hud_detector.py on why extracted art cannot be a template
# source), and 'attachment' / 'tab_detect' fed checkpoints nothing loaded.
# ⚠ AND `fire_mode` HAS NO READER EITHER, which is the part worth writing down
# rather than the path. `grep ASSET_DIR` finds exactly one other definition --
# detector/spawner_detector.py's own, an absolute join that is correct -- and
# zero readers of this one. So this dict survived the deletion of three of its
# four entries by being the kind of thing nobody greps.
#
# Its path was ALSO wrong from 2026-08-08: the templates moved to
# data/templates/ with the rest of the checked-in assets, and a dead constant
# is exactly what a path migration cannot fix, because nothing fails when it
# is missed. Corrected rather than deleted only because deleting a config entry
# is the one edit that a dynamic reader would survive silently; if the next
# person confirms nothing reads it, it should go.
ASSET_DIR = {
    'fire_mode':  'data/templates/pubg_assets/fire_mode',
}

HARD_CASE_CONF = (0.3, 0.5)

# ════════════════════════════════════════════════════════════
# Calibration artifacts — what the measurement layers wrote down
# ════════════════════════════════════════════════════════════

# ⚠ THESE FOUR LIVED IN press/ UNTIL 2026-08-08, AND NOTHING IN press/ EVER
# OPENED ONE. Every reader and writer is in detector/ or calibration/, reaching
# sideways through a '..' into a layer it does not own:
#
#     detector/weapon.py: os.path.join(dirname(__file__), '..', 'press', ...)
#
# A '..' in a path is the same smell as a '..' in an import -- it names a
# neighbour by POSITION instead of by contract, and it survives that neighbour
# being renamed without saying anything. (press/ was in fact reorganised the
# same day, absorbing protocol/ and firmware/; these paths would have kept
# resolving and kept being wrong about who owns what.)
#
# Ownership, now that position no longer implies it:
#
#     calibration/ WRITES them      build_kit_factors.py --write
#     detector/    READS them       weapon.py, weapon_attachments.py
#     press/       never touches them at all
#
# The paths live here because config.py is the one module every layer is
# already allowed to import -- tools/check_layering.py exempts it by name.

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

WEAPON_SCALES_PATH  = os.path.join(DATA_DIR, 'weapon_scales.json')
POSTURE_SCALES_PATH = os.path.join(DATA_DIR, 'posture_scales.json')
KIT_FACTORS_PATH    = os.path.join(DATA_DIR, 'kit_factors.json')
KIT_RECORDS_PATH    = os.path.join(DATA_DIR, 'kit_records.jsonl')

# The compensation curves the runtime loads on every weapon/attachment/posture
# change, and the templates the detectors load at import.
#
# ⚠ CURVES ARE NOT MEASUREMENTS, and the distinction is what this directory
# is for. Measurements go to calibration/artifacts/ -- gitignored, and
# regenerable by measuring again. Curves and templates are the ARTIFACTS the
# runtime loads: with no curve the tool simply does not compensate, and with
# no templates a fresh clone has no lobby chain, no weapon-name OCR and no
# posture.
#
# The curves lived under docs/ once, which is line 19 of .gitignore in its
# entirety, so they had no history at all. On 2026-08-08 a cleanup deleted
# docs/recoil/curves/ and every weapon's curve went with it -- 40 guns,
# unrecoverable, while `git status` stayed clean because git had never heard
# of the directory. There was nothing to revert to.
CURVES_DIR = os.path.join(DATA_DIR, 'curves')
TEMPLATES_DIR = os.path.join(DATA_DIR, 'templates')

# Which fire mode a weapon's stored curve DESCRIBES. Everything here is
# full-auto and comes out 'full'; the mg3 is the exception, because it has two
# automatic modes and they are 1.50x apart in cyclic rate.
#
# ⚠ IT LIVES HERE RATHER THAN ON GunDriver BECAUSE TWO LAYERS NEED THE SAME
# ANSWER, and they need it for two different reasons: control/ presses B until
# the HUD reads this, and calibration/samples.py decides from it which FILE a
# magazine belongs in. A weapon's ordinary mode files untagged; anything else
# gets its own file, so the two never pool and the runtime keeps reading the
# curve that describes the gun it will actually meet.
#
# ⚠ THE mg3 ENTRY IS THE ONE THAT HAS ALREADY COST SOMETHING. Its two stored
# rates -- 59.97 ms/round (2026-08-04, bullet detection) and 88-92 ms
# (2026-08-09, the traces' own autocorrelation) -- were each measured in
# whichever mode the gun happened to spawn in, and NEITHER magazine records
# which. `ensure_fire_mode` existed the whole time and no collection path ever
# called it. docs/game_quirks.md carries the full account.
FIRE_MODE_FOR = {'mg3': 'high'}


def fire_mode_for(weapon):
    """The fire mode this weapon's untagged curve and samples describe."""
    return FIRE_MODE_FOR.get(weapon, 'full')


# ════════════════════════════════════════════════════════════
# Cell names — ONE author, because three layers spell this string
# ════════════════════════════════════════════════════════════
#
# A cell is (weapon, attachments) and its name is the filename fragment both
# the sample store and the curve store use. THE GRAMMAR:
#
#     key   := 'bare' | pair ('_' pair)*        pairs sorted by slot
#     pair  := slot '-' part
#     slot  := an attachment slot: muzzle, grip, stock
#     part  := a catalogue key, WHICH MAY CONTAIN '_' AND NEVER '-'
#
#     {}                                              -> 'bare'
#     {'grip': 'vert_grip'}                           -> 'grip-vert_grip'
#     {'muzzle': 'comp_ar', 'grip': 'vert_grip'}      -> 'grip-vert_grip_muzzle-comp_ar'
#
# ⚠ IT LIVES HERE BECAUSE THE ALTERNATIVE WAS TWO AUTHORS, AND IT HAD TWO.
# calibration/samples.py and detector/weapon.py each defined config_key, byte
# for byte the same, and detector's carried the reason: "expressed here so
# detector/ does not import calibration/. If the two ever disagree, the curve a
# magazine was fitted from stops being findable by the runtime that has to fire
# it -- which is silent, because the lookup just misses." config is the module
# both layers already import, so the constraint that forced the copy is gone.
#
# ⚠ AND THE PART NAMES CONTAIN THE SEPARATOR, which is why parse_config_key
# exists rather than a str.split('_'). `grip-vert_grip` split on '_' reads back
# as {'grip': 'vert'} — a key that names a part nobody has, pointing at a file
# nobody wrote. Eleven measured cells could not be written from the CLI for
# exactly that reason (2026-08-09) and the naive parser was refusing them by
# round-trip, correctly, without anyone noticing the parser was the fault.


def fire_tag(weapon, fire_mode):
    """The name fragment for a fire mode. '' for this weapon's ORDINARY one.

    ⚠ THE BASELINE IS PER WEAPON (fire_mode_for), NOT THE LITERAL 'full', and
    the difference is the whole design. The mg3's ordinary mode is 'high' --
    that is what its curve is timed for and what the runtime will meet -- so
    'high' files untagged and the SLOW one gets `__fire-full`. A tag keyed on
    the literal 'full' would have pushed the mg3's real curve into a tagged
    file that nothing at runtime looks up, which is a worse state than the bug
    it was fixing.

    Everything else has exactly one automatic mode, so its tag is always '',
    and every magazine and curve on disk stays exactly where it is. None --
    "fired before this field existed", or "the HUD could not be read" -- also
    gives '', because that is where those already live and because the ordinary
    mode is the right thing to fall back to when nobody could look.

    ⚠ AND IT IS DRIVEN BY THE READBACK. samples.append passes `mag.fire_mode`,
    which is what the HUD said and not what the run asked for -- so a magazine
    fired in the wrong mode files itself under the wrong mode instead of
    pooling into the right one's numbers. This project has paid twice for a
    record that described the request.

    ⚠ IT LIVES IN config FOR THE SAME REASON config_key DOES: detector/ must
    not import calibration/, and until 2026-08-09 that meant the runtime curve
    lookup had no way to say the words. It keyed (weapon, config, posture,
    sight) with no fire mode, so the mg3 -- whose two automatic modes are 1.5x
    apart in cyclic rate -- played ONE curve for both of them.
    """
    if fire_mode in (None, '') or fire_mode == fire_mode_for(weapon):
        return ''
    return f'__fire-{fire_mode}'


def config_key(config):
    """A (weapon, attachments) cell's name. -> str

    Sorted, so {'muzzle':'comp_ar','grip':'vert'} and the same dict built in
    the other order land in the same file rather than two.
    """
    if not config:
        return 'bare'
    items = sorted((str(k), str(v)) for k, v in config.items() if v)
    return '_'.join(f'{k}-{v}' for k, v in items) or 'bare'


def parse_config_key(key):
    """The inverse of config_key. -> {slot: part} | None if it is not a key.

    ⚠ IT DOES NOT NEED THE SLOT NAMES, and that is what makes it safe. A pair
    always starts with `<slot>-`, and NO PART NAME CONTAINS '-', so a token
    holding a hyphen opens a new pair and every token after it belongs to that
    pair's part until the next hyphen appears. `grip-vert` + `grip` is
    {'grip': 'vert_grip'} without a table of legal slots to consult, so this
    cannot go stale when a slot is added.

    Returns None rather than guessing when the string is not a key at all, and
    every caller should still check `config_key(parse_config_key(k)) == k` --
    the round trip is the proof, and it costs one comparison.
    """
    if key in (None, '', 'bare'):
        return {}
    out, slot, parts = {}, None, []
    for tok in str(key).split('_'):
        if '-' in tok:
            if slot is not None:
                out[slot] = '_'.join(parts)
            slot, first = tok.split('-', 1)
            parts = [first]
            if not slot or not first:
                return None
        elif slot is None:
            return None                       # a part before any slot
        else:
            parts.append(tok)
    if slot is None:
        return None
    out[slot] = '_'.join(parts)
    return out


# ════════════════════════════════════════════════════════════
# Mouse / Pico
# ════════════════════════════════════════════════════════════

# MOUSE_BACKEND was here and is gone (2026-08-08). It chose between the Pico
# and a SendInput backend; PUBG reads the trigger and aiming off raw HID, so
# that backend's click/aim/recoil were no-ops on the only game this repo
# drives. Nothing ever set it to 'soft', Pointer rejected that backend on
# sight, and three error messages still recommended it to anyone without a
# Pico. press/pico_mouse.get_mouse() carries the full account.
PICO_PORT = None
MOUSE_DPI = 2000
GAME_SENSITIVITY = 30
COUNTS_PER_RECOIL_UNIT = 0.4
COUNTS_PER_PIXEL = 0.5

# ⚠ NOT A WIRE CONSTANT, which is why it is here and not in
# protocol/protocol.toml: the FIRMWARE HAS NEVER HEARD OF IT. This is
# when the PC schedules the curve relative to the click; the Pico just
# plays back what it was handed. It lived in press/pico_mouse.py AND
# a since-deleted soft_mouse module as two literal 13s, joined by "Match
# PicoMouse.RECOIL_FIRE_DELAY_MS" -- two backends for the same game,
# so a measurement that moved one had to be remembered into the other.
# Both now read this.

# How long after the click the pattern should START. Positive = later.
#
# This used to be RECOIL_LEAD_FRAC = 0.30, shifting the pattern 30% of a
# bullet interval EARLIER, on the reasoning that USB and frame latency put
# the compensation behind the recoil. Both the sign and the units were
# wrong, and it was never measured.
#
# Write P for the input-and-render delay (command issued -> that command
# visible on screen), C for the capture delay (visible -> we notice), W for
# the weapon's own trigger-to-round delay, T for the bullet interval.
#
# Measured on the AUG, 2026-08-02, red dot, training range:
#
#   tools/probe_input_latency.py   L = P + C     = 38 ms   (n=44, sd 4.8)
#   tools/probe_shot_latency.py    S = W + P + C = 51 ms   (n=36, sd 8.1)
#                                  W = S - L     = 13 ms
#
# S is taken from the AMMO COUNTER, not from the view starting to move.
# Both were recorded and the counter is the sound one: it changes as a
# step, so the first frame that shows it is the answer, while the recoil
# ramps in (0.9 counts in the first 7 ms of a bullet, 2.7 in the middle) so
# any motion threshold fires a frame or two late. The measured gap between
# them was one-sided -- 7 taps of 16 landed in the same frame and NOT ONE
# put the recoil first -- which is the shape of a detection bias, not of
# two events happening at different times. They are simultaneous.
#
# That simultaneity is load-bearing twice over. It is why fit_curve can
# anchor its bins on the first counter change and have the capture latency
# cancel out; and it is why the derivation below closes.
#
# The firmware schedules pattern point k at t_k after the click, and those
# counts reach the screen at t_k + P. Round k's recoil reaches the screen
# at W + P + k*T. Setting them equal:
#
#     t_k = W + k*T
#
# P and C are GONE. Neither the render pipeline nor the capture chain
# enters the offset at all -- only the weapon's own delay does. The same
# cancellation covers the spread: the firmware pours point k out over
# [t_k, t_k + T], which lands on screen over [W + k*T, W + (k+1)*T], and
# that is exactly the window the round's own recoil occupies.
#
# Two earlier values were wrong for two different reasons, and both looked
# like "the first shot is not compensated":
#
#   RECOIL_LEAD_FRAC = 0.30 shifted the pattern EARLIER by a fraction of an
#   interval. Wrong sign, wrong units, never measured.
#
#   36 ms came from S = 72, measured with a coarse motion threshold on the
#   ramping recoil. Re-measured off the counter it is 51, and W is 13.
#
# Milliseconds, not a fraction of the interval: USB transport, input
# sampling and a fire animation do not get faster because the gun does.
# ⚠ 21 WAS TRIED AND REVERTED, 2026-08-07 — and what it measured is worth
# more than the value. m416 bare, shadow, per-bullet residual:
#
#                  n    bullet 0        bullet 41       middle   |per-bullet|
#     13 ms       17   +8.9 +-1.25     +11.9 +-4.08     -1.04       497
#     21 ms       20   +1.5 +-0.91     +30.3 +-2.70     -0.33       527
#
# THE OFFSET TRADES THE FIRST BULLET AGAINST THE LAST. Moving it later
# nulls bullet 0 (4.8 sigma, and +1.5 +-0.91 is indistinguishable from
# zero) and makes bullet 41 WORSE by the same kind of margin (3.8 sigma).
# The middle does not care either way, which is what says this is phase
# and not amplitude.
#
# ⚠ THE SYMMETRIC STORY IS WRONG. "Starts early so it also ends early"
# predicts both ends move together; they move OPPOSITE. What fits: the
# pattern shifted later has its tail fall past the last round, and the
# firmware stops when firing stops, so that tail is never delivered at
# all. Bullet 41 does not get over-compensated, it gets truncated.
#
# So neither end is reachable by this constant alone, and 13 stays because
# it loses less: +7.4 gained on bullet 0 against +18.4 given up on 41.
# ⚠ THE ENDS USED TO BE BLAMED ON A "delivery gain" OF 75% AT BULLET 0
# AND 63% AT 39, AND THAT CLAIM IS WITHDRAWN. Both numbers came from an
# A/B round-alignment probe, and that technique is rejected outright
# (MODEL.md's ruled-out table): the instant is not recorded accurately,
# the two coordinates cannot be put in correspondence, and the ammo
# counter reads about five times in a 42-round magazine, so it cannot
# say which of forty-two rounds moved. Whatever the ends are, those two
# numbers are not evidence for it.
#
# ⚠ AND DO NOT RE-DERIVE THIS FROM S_recoil. probe_shot_latency reports
# "pattern start offset = S_recoil - L = 21" and that is the same coarse
# motion threshold recorded below as having produced the wrong 36. On the
# counter, W = S_ammo - L = 54.3 - 38 = 16.3, and 13 is inside L's own
# +-5.1 ms. Measured 2026-08-07, m416, 40 taps: paired gap median 0.0 ms,
# with 5 of 40 taps reading recoil 12-17 ms LATE and none early.
#
# ════════════════════════════════════════════════════════════════════════
# ⚠ 2026-08-08: EVERYTHING ABOVE IS THE BULLET-BIN COORDINATE, AND THE SIGN
# FLIPPED WHEN THE MODEL MOVED TO TIME.
# ════════════════════════════════════════════════════════════════════════
#
# All of it answers "when does round k's kick land relative to the click",
# because the curve was indexed by ROUND and had to be aligned to one. That
# question is gone. Under MODEL.md the curve IS y_true(t) -- the screen's
# displacement at time t after the click, MEASURED ON THE SCREEN -- so the
# recoil's own path to the photons is already baked into it and cancels.
#
# What does NOT cancel is the compensation's own path, which the recoil never
# travels: Pico -> USB report -> the game sampling input -> view rotation ->
# render -> present. To cancel a displacement that APPEARS at screen-time t,
# the counts have to be EMITTED at t - L. The offset is therefore -L, and it
# is a property of this machine and this display chain rather than of any gun.
#
# THE VALUE IS THE MEASURED OPTIMUM, NOT L, and those turned out to be two
# different numbers. Both were measured; here is each and why the second wins.
#
# L, tools/probe_input_latency.py, 40 trials, no weapon fired:
#     command -> the PRESENT time of the first frame showing it
#     mean 27.63 ms, sd 5.20, sem 0.82, observed frame interval T = 11.84 ms
#     L = mean - T/2 = 21.7 ms      L = min over 40 = 18.3 ms
#
# ⚠ AN EARLIER READING OF 45.6 ms WAS THE PROBE'S OWN BUG. It stamped
# `time.perf_counter()` at the moment its polling loop noticed, not the frame's
# present time -- so it carried the grab and the loop period (~18 ms of the
# 45.6), and it was on a DIFFERENT CLOCK from the samples' t, which is
# `present_s() - click_time`. Two numbers that cannot be subtracted from each
# other looked like they agreed.
#
# THE FIRED OPTIMUM, 25 magazines, ONE fitted curve of 943 counts, five offsets
# ROTATED PER MAGAZINE so the arms interleave in time. RMS of y_obs over the
# whole burst, which is the criterion the root CLAUDE.md law demands -- an
# endpoint hides the path, and here it moves the answer by 11 ms:
#
#     offset    -90    -70    -50    -30    -10
#     RMS      18.3   12.0    9.9    7.0   10.8   counts
#
# A clean bowl; a quadratic puts the minimum at -35.6 ms with the same answer
# when the -90 end point is dropped. At -30 the residual is 7.0 counts RMS =
# 11 px of drift over a 40-round burst, against 68-76 px at the old +13.
#
# ⚠ WHY -36 AND NOT -21, AND THE HONEST ANSWER IS THAT NOBODY KNOWS YET.
# This comment used to assert the gap "is the amplitude repair showing through",
# on the strength of a two-arm decomposition that put ~4.7% of the residual in
# AMPLITUDE -- and with an amplitude term the drift-minimising offset genuinely
# is not -L, so the story was coherent. It had not been computed.
#
# tools/probe_offset_decomposition.py computes it, on these same 25 magazines
# but using all five arms at once, which is what makes the split well-posed:
# F and F' are near-collinear within ONE burst shape (the failure that produced
# "+4.74% gain / +3.2 ms lag", refuted the moment it was fired), while the
# arm-to-arm difference is pure F' with a coefficient nobody had to fit.
#
#     F' coefficient vs commanded offset:  slope +0.92, intercept +22.4 ms
#     -> M (the true lag) = +24.3 ms, bootstrap 95% [+18.0, +28.4]
#     -> eps (amplitude)  = +1.62%,   bootstrap 95% [+0.60%, +3.01%]
#
# So the "missed lag" reading is REFUTED: M's interval contains the probe's
# 21.7 and excludes 36 comfortably, and the slope near 1 says the curve moves
# when it is told to. RECOIL_COMP_LAG_MS = 20 stands.
#
# ⚠ AND THE AMPLITUDE READING IS NOT ESTABLISHED EITHER, because the same fit
# fails to reproduce the sweep that produced it: eps = 1.62% predicts a
# minimum-RMS offset of -43.3 ms, and this data's own parabola minimum is
# -29.1. A decomposition that cannot re-derive its own optimum has not
# explained anything -- eps is the collinear direction and its per-arm
# estimates swing 0.55%..2.93% with no relation to the arm.
#
# FIRED 2026-08-08, --scale-sweep 0.90,1.00,1.10 rotated per magazine at a
# fixed -36, 15 magazines, mp5k bare red_dot standing, curve 968.6 counts:
#
#     scale    F coef     end y_obs     F' coef
#     x0.90   +0.0907    +78.4 counts   -12.6 ms
#     x1.00   -0.0024     -5.5 counts   -17.1 ms
#     x1.10   -0.0990    -89.7 counts   -18.5 ms
#
#     F coef vs scale: slope -0.948 (theory -1.000), zero at s0 = 0.9962
#     ->  eps = -0.38%.  NOT 4.7%, NOT 1.62%. ZERO.
#
# The offline decomposition's own 95% interval was [+0.60%, +3.01%] and does
# not contain the answer. That is the collinear direction failing exactly where
# the tool said it would, and it is the third time this residual has been
# decomposed offline and refuted by firing it.
#
# ⚠ SO THE AMPLITUDE DEFICIT IS GONE, AND THAT MAKES -36 WRONG. It was tuned
# against a 943-count curve when y_true measured ~950 -- a real ~0.8% shortfall
# then, which genuinely pulls the drift-minimising offset negative. The fit has
# since grown to 968.6 and the shortfall closed. What is left at -36 is a clean
# -17.1 ms of F', which is (M + D) exactly:
#
#     M = F' coef - D = -17.1 - (-36) = +18.9 ms
#
# A THIRD independent reading of the lag, and the FIRST one in the regime the
# compensation actually uses -- about 1 count per millisecond, where the probe
# throws a single 250-count impulse. It agrees with both of the others:
#
#     impulse probe, n=40         18.3 .. 21.7 ms
#     offset sweep, 5 arms        24.3 ms   bootstrap [18.0, 28.4]
#     scale sweep, in-regime      18.9 ms
#
# ⚠ THE "small-delta L may differ from large-impulse L" BACKLOG ITEM IS
# THEREFORE CLOSED. It could never have been closed by more impulse trials.
#
# With eps = 0 the drift-minimising offset and -L COINCIDE, because the only
# thing that separated them was the shortfall. So -36 over-leads by ~17 ms and
# the constant should be about -19.
#
# FIRED 2026-08-08, --fire-delay-sweep=-50,-36,-19,-5 rotated per magazine off
# ONE 968.6-count fit, 16 magazines, mp5k bare. Per-MAGAZINE whole-path RMS:
#
#     -50    6.7  12.0  16.6  28.3    mean 15.9
#     -36    5.4   8.3  17.4  22.7    mean 13.5
#     -19    5.7   6.5   6.6   8.5    mean  6.8
#      -5    5.8   6.3   6.8  11.9    mean  7.7
#
#     -36 vs -19   +6.6 counts  95% [-0.2, +13.7]   P(-19 better)  97%
#     -50 vs -19   +9.1         95% [+1.9, +17.2]   P             100%
#      -5 vs -19   +0.9         95% [-1.3,  +3.6]   P              71%
#
# ⚠ -19 IS NOT SEPARABLE FROM -5, and it is set anyway, because it is where two
# INDEPENDENT things intersect: the RMS optimum (parabola minimum -19.3) and -M
# from the two well-conditioned lag readings (18.3..21.7 STEP, 18.9 in-regime).
# Nothing measures M anywhere near 5. Half the interval, all of the support.
#
# ⚠ "STEP" USED TO READ "impulse" AND THAT WORD IS NOW LOAD-BEARING IN THE
# WRONG DIRECTION. The round-alignment technique called `impulse` -- put a spike
# on round k, see which round moves -- is REJECTED (MODEL.md's ruled-out table).
# This reading is a DIFFERENT TECHNIQUE that happens to have shared a word:
# tools/probe_input_latency.py sends one mouse.move() and times when the SCREEN
# starts moving. It never touches the ammo counter and has no round index in
# it, so nothing here inherits that rejection.
#
# ⚠ THE OVER-LEADING ARMS ARE ERRATIC, NOT MERELY BIASED, and that was not
# predicted: -50 and -36 spread 6.7..28.3 and 5.4..22.7 while -19 and -5 sit
# inside 5.7..8.5 and 5.8..11.9. Leading too far makes the residual sensitive
# to something that varies magazine to magazine. Unexplained; recorded because
# a mean would have hidden it and the spread is the larger effect.
#
# ⚠ AND THE MECHANISM PREDICTION MISSED, on a criterion that was set wrong.
# The claim was "F' coef = M + D, a line of slope 1 through -M, and any arm off
# it kills this" -- observed -19.4 / +7.4 / +4.9 / +16.0 against a predicted
# -31.1 / -17.1 / 0.0 / +13.9, slope 0.67 [0.34, 1.03]. But that coefficient is
# a DIFFERENCE OF MEANS, and this very file records "five magazines an arm
# cannot carry a difference of two means" -- the prediction was made at FOUR.
# Its interval on M, [18.4, 48.0], contains 18.9, so the mechanism is untested
# rather than refuted. The per-magazine RMS above is what is well-conditioned
# at n=4: a scalar per magazine, not a difference. Root CLAUDE.md, "判据必须能
# 看见它要管的那个维度", again -- this time the blind criterion was the one
# written to check the previous blind criterion.
#
# ⚠ AND THE INTERLEAVING EARNED ITS KEEP. A -46 arm fired in its own earlier
# run reads +31.4 counts at t=2.0 s where the interleaved -50 and -30 either
# side of it read -4.7 and +1.0. Thirty counts apart, on the same gun, the same
# lane, twenty minutes apart. Every offset comparison made across runs before
# this was unreliable, including the first sweep that put the optimum "near
# -60".
#
# ⚠ AND IT IS NOT "BETWEEN-SESSION DRIFT", which is what this comment called it
# until 2026-08-09. MODEL.md 2.3: two readings that disagree mean AT LEAST ONE
# IS WRONG, and "it drifted" is a placeholder for a measurement fault nobody
# has located -- naming it makes an open problem read as a closed one. The same
# shape has already produced one retraction here: nine runs' "2.7% step" turned
# out to be arm and run co-varying. What is measured above is the DISAGREEMENT
# and its size; the cure (interleave, never compare arms across runs) is what
# was tested, and it works whichever reading is the wrong one.
#
RECOIL_FIRE_DELAY_MS = -19

# How long an EMITTED count takes to become a photon: Pico USB report, the game
# sampling input, view rotation, render, present. The other half of the pair
# above, and the two are different numbers doing different jobs:
#
#   RECOIL_FIRE_DELAY_MS   shifts WHEN the firmware emits. CHOSEN -- it is the
#                          offset that minimises measured drift, and with an
#                          amplitude term in the residual that optimum is not -L.
#   RECOIL_COMP_LAG_MS     how late the emission APPEARS. MEASURED, and used by
#                          the ANALYSIS: y_true(t) = y_obs(t) + C(t - L).
#
# ⚠ THE ANALYSIS USED C(t) AND THAT IS A DIVERGENT LOOP, not just a bias. The
# fit consumes its own output, so writing C(t) makes
#
#     F_{n+1} = y_true + L * F_n'
#
# an iteration carrying a DERIVATIVE, with gain L*omega at frequency omega.
# Above 1/L (~8 Hz here) every round amplifies, and the 17 ms grid reaches
# 59 Hz. The root CLAUDE.md records the same shape costing 1.025^255.
#
# tools/probe_input_latency.py, 40 trials, present times, observed T = 11.84 ms:
#     mean 27.63 sd 5.20 sem 0.82  ->  L = 21.7 (mean - T/2),  L = 18.3 (min)
# 20 is between them and inside the sem of neither being ruled out.
#
# ⚠ Stored PER MAGAZINE (samples.Magazine.comp_lag_s), never read from here at
# analysis time. A constant read later describes the machine as it is now, and
# a magazine fired last week went through a different display chain.
RECOIL_COMP_LAG_MS = 20


# ════════════════════════════════════════════════════════════
# Debug / detection
# ════════════════════════════════════════════════════════════

# ⚠ ON, AND NO LONGER A DEBUG SETTING. Weapon.set_seq() re-reads the curves
# when they have changed on disk, so a calibration run that improves a curve
# reaches the game on the next weapon switch instead of on the next restart.
# Asked for on 2026-08-07 -- "每次开枪都是最新的曲线" -- after a night of
# --apply passes produced better curves that the live process never saw.
#
# It was off because the reload was unconditional and read EVERY json in
# docs/recoil/curves, including 991 timestamped backups: 163 ms on a path that
# runs at every weapon, attachment and posture change. Now backups are skipped
# (they are not curves, and they were also silently competing to BE the curve
# -- see load_curves) and a directory stat decides whether to parse anything:
#
#     load_curves        163 ms  ->  27 ms
#     the change check                3.1 ms, and only that when nothing moved
DEBUG_HOT_RELOAD = True
CONF_BODY = 0.85
CONF_HEAD = 0.3
CONF_BODY_RECOIL = 0.9

# ════════════════════════════════════════════════════════════
# Legacy compat — x1/y1/x2/y2 boxes. NOTHING READS THEM ANY MORE.
# ════════════════════════════════════════════════════════════
#
# The last consumer, dl_models/icon_layout.py, went on 2026-08-08 with the
# fire-mode CNN it synthesised training data for (2 answers in 859 crops — see
# detector/fire_mode_detector.py). So the two dicts below have zero readers,
# which is exactly what IN_TAB / GUN_NAME_1 / GUN_NAME_2 / ATTACHMENT_SLOTS /
# ALPHA were deleted for the day before. They are kept for a different reason:
# they are the ONLY surviving record of these two boxes in x1/y1/x2/y2 form,
# and the coordinates themselves are still live under HUD_REGIONS.
#
# ⚠ NEVER INFER FROM THEIR PRESENCE THAT SOMETHING USES THEM. That inference is
# what kept the dead entries alive: this header used to claim "used by
# dl_models/icon_layout.py, training code", true of FIRE_MODE and of nothing
# else, and unfalsifiable from anywhere outside dl_models/.
#
# Nothing measured was lost in that deletion. The four were FIELD-FOR-FIELD
# HUD_REGIONS['type'], ['gun_name_1'], ['gun_name_2'] and ['att_N_*'], and
# ALPHA was a third copy of two highlight opacities nothing read — the live
# judgement is HighlightDetector, scored as a PAIR (`pixi run highlight`).
#
# WEAPON_HUD_1/2 were NOT duplicates and are recorded rather than dropped: same
# x 2808..3014, but y 1336..1406 and 1253..1323 — 70 px tall against the
# current 53 — with an `icon_offset_y: 9` that has no equivalent in
# HUD_REGIONS. An older, taller crop of the same thing. If a weapon-HUD crop
# ever needs the icon's offset inside its box, that 9 is where it was measured.
FIRE_MODE = {
    'x1': 1626, 'y1': 1317, 'x2': 1682, 'y2': 1360,
}
# Kept alongside it deliberately: it is the same box as HUD_REGIONS['posture']
# in the other spelling, and the pair is the two HUD icons. Splitting them
# would leave the next reader wondering which convention the survivor is in.
POSTURE = {
    'x1': 1373, 'y1': 1301, 'x2': 1439, 'y2': 1367,
}


# ════════════════════════════════════════════════════════════
# The rectangle-convention ratchet
# ════════════════════════════════════════════════════════════
#
# ⚠ AT IMPORT, AND IT RAISES. Every process in this repository imports config,
# so there is no "remember to run the gate" step and no file it can stop
# covering. A rectangle in the wrong convention does not fail loudly on its
# own -- it returns a DIFFERENT rectangle, and the symptom shows up one layer
# away as "the detector cannot read this" (2026-08-08, HUD_REGIONS
# ['gun_name_1'] unpacked as x1/y1/x2/y2: the crop came back empty and the
# investigation went looking at the template bank).
#
# It checks the naming rule and the type together, because each catches what
# the other misses: a bare tuple named *_ROI passes any type check that only
# looks at declared Rois, and a Roi named *_BOX passes any name check.
def _check_rectangles():
    """Every rectangle in this file is a Rect, and there is no second type.

    ⚠ AT IMPORT, AND IT RAISES. Every process here imports config, so there is
    no "remember to run the gate" step and no file it can stop covering. A
    rectangle in the wrong form does not fail loudly on its own -- it returns a
    DIFFERENT rectangle, and the symptom lands one layer away as "the detector
    cannot read this" (2026-08-08, HUD_REGIONS['gun_name_1']).

    It checks the NAME as well as the type, because each catches what the other
    misses: a bare tuple called *_ROI passes any type-only check that iterates
    declared Rects, and a stray corner tuple called *_BOX passes any name-only
    check. The suffixes are historical -- both mean Rect now -- and they are
    kept as the redundant half rather than renamed, because renaming would
    touch the call sites this change is specifically not touching.
    """
    bad = []
    for _name, _val in list(globals().items()):
        if _name.startswith('_') or not isinstance(_val, tuple):
            continue
        if ('_ROI' in _name or '_BOX' in _name) and not isinstance(_val, Rect):
            bad.append(f'{_name} is a rectangle constant but a bare '
                       f'{type(_val).__name__} -- write Rect(y, x, h, w), or '
                       f'Rect.corners(x0, y0, x1, y1) if that is how it was '
                       f'measured')
    for _table in ('HUD_REGIONS', 'MAP_RANGE_BOXES'):
        for _k, _v in globals().get(_table, {}).items():
            if not isinstance(_v, Rect):
                bad.append(f'{_table}[{_k!r}] is a bare {type(_v).__name__} -- '
                           f'the table is wrapped just below its literal, so a '
                           f'new ENTRY needs no change; this means the wrapping '
                           f'line was removed')
    if bad:
        raise AssertionError(
            'config.py rectangles are broken:\n  ' + '\n  '.join(bad)
            + '\n\nThere is ONE rectangle type and ONE stored order '
              '(y, x, h, w).\nThe corner view is a PROPERTY '
              '(r.x0/.x1/.y0/.y1) so it cannot\ndisagree with the storage. '
              'Do not add a second type back.')


_check_rectangles()
