"""Where the rounds actually LAND. The one measurement outside the loop's own chain.

    pixi run python calibration/probe_hole_pattern.py --weapon m416
    pixi run python calibration/probe_hole_pattern.py --weapon m416 --both

WHY THIS EXISTS
---------------
Every number the calibration loop produces comes off ONE chain:

    patch correlation -> view displacement -> / K -> binned by a clock
      -> compared against the curve  ->  residual, sum|e|, wander, floor, ratio

Eleven hypotheses about why the loop stalls were raised and falsified on
2026-08-07, and every one of them was about a link INSIDE that chain, judged
using the chain's own output. A stable bias anywhere in it produces numbers
that are self-consistent and wrong, and nothing downstream can see it.

Bullet holes are outside it. They are a physical record: no tracker, no K, no
binning, no clock. The vertical spread of one magazine's holes IS the residual
-- not a proxy for it -- and a human can read the picture directly.

⚠ IT MUST BE ABLE TO SAY "NOTHING IS HERE". Firing into the sky leaves no
holes, and an empty diff looks exactly like perfect compensation (every round
through the same point).

⚠ AND THIS PARAGRAPH USED TO CLAIM THAT CHECK EXISTED WHEN IT DID NOT. It read
"the baseline is checked for a surface first". Nothing checked the baseline:
the only guards were on the RESULT -- too few marks, or a span over half the
screen -- and both run long AFTER the two frames have been registered against
whatever happened to be in a hard-coded rectangle. A sentence describing a gate
that does not exist is worse than no sentence, because it is why nobody writes
the gate; this repository has paid for that shape before.

The check is real now and it is `find_band`: the registration ROI is SEARCHED
for, and the phase-correlation response is both the choice and the evidence. A
rigid textured surface present in both frames scores; sky, water and wind-blown
grass cannot, and a frame where nothing scores is REFUSED. Measured against the
rectangle it replaced, over seven stored pairs: it never loses, and where the
character had walked away from the wall it reads 0.97 where the constant read
0.44.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:                                   # cp1252 cannot encode the ⚠ this prints
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import config                                                    # noqa: E402
from calibration.sweep import Rig                                # noqa: E402
from capture.cropper import capture_screen                       # noqa: E402
from control.session import ensure_ready                         # noqa: E402

OUT_DIR = os.path.join(ROOT, 'calibration', 'artifacts', 'holes')

# A hole darkens the wall. 18 is well clear of frame-to-frame noise on a still
# scene (measured floor ~4) and well under the 40+ a real mark makes.
DIFF_MIN = 18
# Marks smaller than this are muzzle flash residue and compression noise.
BLOB_MIN_PX = 6
# Below this many marks the picture is not a group -- most likely the rounds
# went into the sky or past the wall. Refuse rather than report a tight group.
MARKS_MIN = 6

# ⚠ READ OFF config, NOT TYPED. A second copy of the screen size here is
# exactly the VALUE-scan shape the root CLAUDE.md is about: the constant has
# one author and this would be a transcription of it that nothing checks.
SCENE_H, SCENE_W = config.SCREEN_H, config.SCREEN_W


# ── single-round mode: the hole, measured against the RETICLE ───────────────
#
# ⚠ THE WHOLE-SCREEN DIFF CANNOT DO THIS, and three live runs proved it before
# the reason was looked at rather than guessed. `recenter()` returns the view to
# within a few pixels, not to the pixel — measured here at dx +3.8, dy +7.5 —
# and a 1 px shift lifts EVERY edge in the scene over the darkening threshold.
# The result was 6000-9000 "marks" per shot at every threshold from 18 to 80.
# Raising the gate does not help: the residue is edges, and edges are strong.
#
# So two changes, and the second is the one that matters:
#   1. register the two frames on a band of the wall first (phase correlation)
#   2. measure the hole RELATIVE TO THE RED DOT, not to the screen
# (2) makes the answer independent of how well (1) did: the reticle and the
# hole are in the same frame, so a view that drifted moves both together.
WALL_BAND = (300, 1100, 400, 1400)   # y0, y1, x0, x1 — surface, clear of the gun
# Registration response. 0.936 measured on the Jump School wall; a band that is
# sky, or a moving scene, cannot reach this, and that is the point — this ROI is
# the one scene-specific constant here and this number is what checks it.
# ⚠ 0.55 WAS TOO STRICT AND IT REJECTED GOOD SHOTS. Measured across one tilt
# sweep: response ran 0.94 on flat concrete but 0.37-0.44 once grass and sand
# were in the band -- and the hole was found, at the same offset, in every one
# of those. The gate is here to catch registering against SKY, not to demand a
# wall, so it sits under what a live ground scene actually scores.
ALIGN_RESP_MIN = 0.30
AIM_WIN = 200                        # search box half-width around the reticle
# ⚠ THE HOLE IS SMALL AND COMPACT. IT IS NOT THE BIGGEST THING NEAR THE DOT, and
# believing otherwise cost most of a session. `max(area)` inside the window kept
# picking a **5290 px** blob that a marked-up screenshot showed to be the RED DOT
# SIGHT'S OWN RING -- a thin curve with a huge bounding box, sweeping across the
# wall as the weapon idles, and CONNECTED to the hole in the diff so connected
# components could not tell them apart. Raising the floor to 1800 to chase it
# then rejected every real shot.
#
# Measured, three ADS rounds at different pitches, same session:
#
#     area  897 / 1111 / 556 px      fill 0.56 / 0.50 / 0.58
#     offset from the dot   dx +40 / +36 / +24   dy +85 / +90 / +90
#
# So: a few hundred px, and FILLED. The ring runs 0.13-0.31 fill; gun-body edges
# and registration residue are thin. Fill is what separates them, not size.
HOLE_AREA_MIN, HOLE_AREA_MAX = 150, 2500
HOLE_FILL_MIN = 0.35                 # area / bounding-box area
# How far below the dot a hole has to be before it is believed. The ring's lower
# arc sits at ~+96 px and the measured impacts at +85..+99, so this cannot
# separate those two -- it only rejects the UPPER arc and anything level with
# the dot. Keep it small; the discrimination that matters is the sign.
HOLE_BELOW_MIN = 20.0


def reticle_xy(frame):
    """The red dot, as (x, y). -> None if it is not on the screen.

    The optic's dot is the only saturated red in a desert scene, and it is what
    the shot was aimed at — so it is the origin the hole should be reported
    against. Screen centre would be a guess about where the game draws it.
    """
    # ⚠ RED **OR** GREEN. The red dot is red; the 3x/4x reticles are a green
    # chevron, and asking only for red made every scoped shot come back "no red
    # dot on the screen — not in ADS", which is a sentence about the optic
    # dressed up as a sentence about the player's state.
    b, g, r = (frame[:, :, i].astype(np.int16) for i in range(3))
    red = (((r - np.maximum(b, g) > 60) & (r > 120))
           | ((g - np.maximum(b, r) > 60) & (g > 120))).astype(np.uint8)
    h, w = red.shape
    red[:h // 4] = 0
    red[3 * h // 4:] = 0
    red[:, :w // 3] = 0
    red[:, 2 * w // 3:] = 0
    n, _lab, stats, cent = cv2.connectedComponentsWithStats(red, 8)
    best, at = 0, None
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] > best:
            best, at = stats[i, cv2.CC_STAT_AREA], (float(cent[i][0]),
                                                    float(cent[i][1]))
    return at if best >= 4 else None


# Where the weapon and the HUD live on screen. Tiles overlapping these are not
# offered to the search — and the gun is the dangerous one, not the HUD. It is
# RIGID and nearly stationary in screen space, so registering on it scores
# beautifully and returns a shift of ~0, which leaves the world unaligned while
# reporting a high response. A band chosen by score alone would pick it.
BAND_EXCLUDE = ((820, 1440, 1350, 2150),     # the weapon model in ADS
                (0, 130, 0, 3440),           # net/compass strip
                (1250, 1440, 0, 3440),       # ammo + hint bar
                (400, 1440, 2900, 3440))     # right-hand HUD and minimap
BAND_TILE = (420, 700)                       # h, w of a candidate band
BAND_STRIDE = (210, 350)


def _overlaps(band, box):
    y0, y1, x0, x1 = band
    by0, by1, bx0, bx1 = box
    return not (y1 <= by0 or y0 >= by1 or x1 <= bx0 or x0 >= bx1)


def find_band(before, after, floor=None):
    """FIND the surface to register on, by measuring candidates. -> (band, resp)

    ⚠ THIS REPLACES A HARD-CODED RECTANGLE, AND THE RECTANGLE WAS THE BUG. A
    constant band describes ONE viewpoint: `WALL_BAND` was measured facing the
    Jump School wall and scored 0.94 there, and the moment the character stood
    somewhere else the same rectangle covered sand and sea and scored 0.47 —
    with nothing saying so. Every shot in that run was then registered against
    the ocean and the marks that came back were residue.
    ⚠ AND THE FILE HEADER ALREADY CLAIMED THIS EXISTED: "the baseline is checked
    for a surface first". It did not. The only checks were on the RESULT — too
    few marks, or a span over half the screen — both of which run long after the
    frames have been registered against whatever was in the box.

    The response IS the check: a rigid, textured surface present in BOTH frames
    scores; sky, water and wind-blown grass cannot. So "is there a surface here"
    and "which part of the screen is it" are one measurement, not two.
    """
    if floor is None:
        floor = ALIGN_RESP_MIN
    g0 = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY).astype(np.float32)
    g1 = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY).astype(np.float32)
    h, w = g0.shape
    th, tw = BAND_TILE
    sy, sx = BAND_STRIDE
    win = cv2.createHanningWindow((tw, th), cv2.CV_32F)
    best = (None, -1.0)
    for y0 in range(0, h - th + 1, sy):
        for x0 in range(0, w - tw + 1, sx):
            band = (y0, y0 + th, x0, x0 + tw)
            if any(_overlaps(band, b) for b in BAND_EXCLUDE):
                continue
            a0 = g0[y0:y0 + th, x0:x0 + tw]
            # A flat patch registers against anything. Texture first, cheaply.
            if float(a0.std()) < 8.0:
                continue
            _, resp = cv2.phaseCorrelate(a0, g1[y0:y0 + th, x0:x0 + tw], win)
            if resp > best[1]:
                best = (band, float(resp))
    if best[0] is None or best[1] < floor:
        return None, best[1]
    return best


def align(before, after, band=WALL_BAND):
    """Shift `after` onto `before` using a rigid band of the scene.

    -> (aligned_gray, (dx, dy), response). The response is the gate: a band that
    is not a rigid textured surface cannot score on it, so this refuses to
    register against sky or foliage instead of quietly returning a shift.
    """
    g0 = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY).astype(np.float32)
    g1 = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY).astype(np.float32)
    y0, y1, x0, x1 = band
    win = cv2.createHanningWindow((x1 - x0, y1 - y0), cv2.CV_32F)
    (dx, dy), resp = cv2.phaseCorrelate(g0[y0:y1, x0:x1], g1[y0:y1, x0:x1], win)
    m = np.float32([[1, 0, -dx], [0, 1, -dy]])
    warped = cv2.warpAffine(g1, m, (g1.shape[1], g1.shape[0]),
                            flags=cv2.INTER_LINEAR)
    return g0, warped, (dx, dy), float(resp)


def hole_vs_reticle(before, after):
    """Where one round went, relative to where it was aimed.

    -> ((dx, dy), info) or (None, why). dy > 0 means the hole is BELOW the dot.
    """
    # ⚠ THE DOT COMES FROM THE **BEFORE** FRAME, and that is not a detail.
    # The optic rides the weapon model, so the dot sways on screen -- 14 px
    # between one before/after pair measured here, which is larger than the
    # whole effect this probe is chasing. The round left while the view was at
    # the BEFORE position, so that is where it was aimed.
    #
    # It also makes the answer invariant to how well `recenter()` did: the hole
    # is reported in the before frame's coordinates (that is what `align` buys)
    # and so is the dot, so a view that came back a few pixels off moves the
    # aim point and the impact point together and cancels.
    dot = reticle_xy(before)
    if dot is None:
        return None, 'no red dot on the screen — not in ADS, or not a red dot'
    # ⚠ THE BAND IS FOUND, NOT NAMED. See `find_band`: a fixed rectangle is a
    # statement about one viewpoint, and this probe is run from wherever the
    # character happens to be standing.
    band, resp = find_band(before, after)
    if band is None:
        return None, (f'nothing in the frame registers as a rigid surface '
                      f'(best response {resp:.2f} < {ALIGN_RESP_MIN}) — facing '
                      f'sky, water or moving foliage, or the view moved too far')
    g0, g1, (sx, sy), resp = align(before, after, band)
    dark = np.clip(g0 - g1, 0, 255).astype(np.uint8)
    mask = np.zeros_like(dark)
    cx, cy = int(dot[0]), int(dot[1])
    mask[max(cy - AIM_WIN, 0):cy + AIM_WIN,
         max(cx - AIM_WIN, 0):cx + AIM_WIN] = 255
    _, m = cv2.threshold(cv2.bitwise_and(dark, mask), DIFF_MIN, 255,
                         cv2.THRESH_BINARY)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, _lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    cands = []
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        bw = int(stats[i, cv2.CC_STAT_WIDTH]) * int(stats[i, cv2.CC_STAT_HEIGHT])
        if not (HOLE_AREA_MIN <= area <= HOLE_AREA_MAX
                and bw and area / bw >= HOLE_FILL_MIN):
            continue
        # ⚠ BELOW THE AIM, ALWAYS. The bore sits UNDER the sight and the rifle is
        # zeroed at 100 m, so at any range short of that the round lands low --
        # a candidate above the dot cannot be a hole, whatever it scores.
        #
        # This is the rule that broke the tie, and the tie was costing every
        # reading. Eight shots came back +95.4 -98.9 +95.6 +96.2 +98.6 -96.0
        # -94.8 +86.0: the MAGNITUDE pinned at 95-99 and only the SIGN moving.
        # That is the red dot's own RING, whose top and bottom arcs sit
        # symmetrically about the dot and survive the opening as compact blobs.
        # Picking `max(area)` chose whichever arc the sway had thickened.
        if float(cent[i][1]) <= dot[1] + HOLE_BELOW_MIN:
            continue
        # ⚠ AND IT MUST BE ON THE WORLD, NOT ON THE GUN. `fill` does not rule
        # the weapon out: the strip between the optic's lower rim and the
        # receiver is a filled sliver, 1140 px at fill 0.85, and it was being
        # returned as the hole on a frame a marked-up screenshot showed to have
        # NO hole in it at all -- the character was facing a dirt bank, where an
        # impact leaves no readable mark. Without this the probe invents a
        # reading wherever the surface cannot give one, which is the failure
        # this whole file exists to avoid.
        by0, by1 = int(stats[i, cv2.CC_STAT_TOP]), int(stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT])
        bx0, bx1 = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH])
        if _overlaps((by0, by1, bx0, bx1), BAND_EXCLUDE[0]):
            continue
        cands.append((area, float(cent[i][0]), float(cent[i][1])))
    if not cands:
        # Say how close the best thing came, so "nothing there" and "the floor
        # is set wrong" are different sentences rather than the same one.
        near = max([stats[i, cv2.CC_STAT_AREA] for i in range(1, n)] or [0])
        return None, (f'no mark of {HOLE_AREA_MIN}-{HOLE_AREA_MAX} px within '
                      f'{AIM_WIN} px of the dot (biggest was {int(near)} px) — '
                      f'the round missed the wall, or the wall is out of the '
                      f'search box')
    area, hx, hy = max(cands)
    return (hx - dot[0], hy - dot[1]), {
        'area': int(area), 'dot': dot, 'hole': (hx, hy), 'band': band,
        'shift': (sx, sy), 'resp': resp, 'others': len(cands) - 1}


def marks(before, after):
    """Hole centroids as (x, y), from the darkening between two frames."""
    a = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY).astype(np.int16)
    b = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY).astype(np.int16)
    darker = np.clip(a - b, 0, 255).astype(np.uint8)
    _, m = cv2.threshold(darker, DIFF_MIN, 255, cv2.THRESH_BINARY)
    n, _lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= BLOB_MIN_PX:
            out.append((float(cent[i][0]), float(cent[i][1])))
    return out, darker


def one_burst(rig, comp_on):
    """-> (marks, before, after). Fires one magazine and reads the wall.

    No round count: `fire_magazine()` empties whatever is loaded and counts
    it itself. A `mag_size` argument sat here unread until the move into
    calibration/ put the file under `pixi run params` (2026-08-08).
    """
    if not comp_on:
        rig.fire.disarm()
    rig.gun.ensure_ads()
    rig.flush(4)
    # ⚠ capture_screen(), NOT rig.grab(). The Rig's ScreenBuffer is BANDED --
    # `_regions()` is the tracker patches plus the HUD windows -- so `grab()`
    # returns {name: crop} and `full()` blits those few bands onto an otherwise
    # BLACK canvas. The wall is in neither. This probe reads a surface the rig
    # has no region for, which is the same thing that makes it evidence: it is
    # the one measurement outside the loop's own chain, so it cannot share the
    # loop's frame source either.
    #
    # It broke silently when capture/ moved out of detector/ on 2026-08-08 and
    # grab() started returning the dict -- a dict has .copy(), so the old line
    # kept working right up to cv2.cvtColor. Nothing ran this probe in between:
    # it needs the game, so no gate covers it.
    before = capture_screen()
    rig.fire.fire_magazine()
    time.sleep(0.5)
    # Put the view back where it started so the holes are in the baseline's
    # frame of reference. Without this the camera has moved and the diff
    # measures the pan, not the group.
    rig.view.recenter()
    rig.flush(4)
    after = capture_screen()
    return marks(before, after), before, after


# Between shots the aim is walked sideways so every round lands on VIRGIN wall.
# ⚠ WITHOUT THIS THE PROBE MEASURES ONE SHOT AND THEN REFUSES FOREVER, and the
# refusal reads like a miss. Measured: the first round on clean concrete darkens
# 5290 px, and a second round landing on top of that decal adds only 200-370 px
# of NEW darkening -- under the floor, so `no mark ... biggest was 306 px`. The
# rounds were hitting the wall the whole time.
# ⚠ NEGATIVE, i.e. LEFT, and that is geometry rather than taste. In ADS the
# reticle sits at x~1720 and the Jump School wall ends at x~2050, so stepping
# RIGHT walks off the wall in two shots -- which is what happened: shot 1 read,
# shots 2-6 reported `biggest was 300-800 px` and read like misses. There is
# ~1500 px of wall to the LEFT of the aim.
YAW_STEP_COUNTS = -260               # ~170 px at the red dot's K, > a decal wide
YAW_HOME_EVERY = 6                   # walk back before running off the wall
PITCH_TOL_PX = 4.0


def pitch_sign(rig, counts=200):
    """px of on-screen wall movement per DOWNWARD count. Measured, not assumed.

    ⚠ IT IS MEASURED BECAUSE THE ALTERNATIVE IS A SIGN CONVENTION, and this file
    has three of them in reach (the curve's, the correlator's, the pointer's).
    A re-aim loop that pushes the wrong way does not oscillate visibly — it
    walks the view into the pitch clamp and every later shot goes over the wall,
    which is what the previous version did while printing "view will not come
    back (+30 counts off)".
    """
    rig.flush(3)
    a = capture_screen()
    rig.view.turn(0, counts)
    time.sleep(0.25)
    rig.flush(3)
    b = capture_screen()
    _g0, _g1, (_dx, dy), _resp = align(a, b)
    rig.view.turn(0, -counts)          # put it back; this was a probe, not a move
    time.sleep(0.25)
    return dy / float(counts)


def reaim(rig, drift_px, sign, yaw_counts):
    """Step sideways onto clean wall and undo this shot's vertical drift."""
    pitch = 0
    if sign and abs(drift_px) > PITCH_TOL_PX:
        pitch = int(round(-drift_px / sign))
    rig.view.turn(yaw_counts, pitch)
    time.sleep(0.3)


def one_single(rig, comp_on, save_as=None):
    """-> (mark|None, why). ONE round, and the wall says where it went.

    ⚠ EVERY CLICK IS A FIRST SHOT, and that is the whole reason this exists
    beside `one_burst`. A magazine's holes are a group with no order in them --
    nothing on the wall says which hole was round 1 -- so the opening round,
    the one the compensation handles worst, is the one a burst cannot report.

    The view is recentred BEFORE the baseline as well as after the shot, so
    both frames describe the same aim. `one_burst` only does it after, which is
    enough for a spread and not enough for a POSITION.
    """
    rig.flush(4)
    before = capture_screen()
    rec = rig.fire.fire_once()
    if not rec['ok']:
        return None, rec.get('error') or 'the shot did not land as one round'
    time.sleep(0.35)
    rig.flush(4)
    after = capture_screen()
    # ⚠ KEEP THE FRAMES. This mode used to throw its own evidence away, so a
    # reading that came back wrong could only be investigated by FIRING AGAIN --
    # which needs the game, a wall, and a session that has not been idle-kicked.
    # Two of those cost half an hour each tonight.
    if save_as:
        os.makedirs(OUT_DIR, exist_ok=True)
        cv2.imwrite(os.path.join(OUT_DIR, f'{save_as}_before.png'), before)
        cv2.imwrite(os.path.join(OUT_DIR, f'{save_as}_after.png'), after)
    return hole_vs_reticle(before, after)


def singles(rig, n, comp_on, label):
    """Fire n single rounds and report where each one landed. -> [(x, y), ...]

    ⚠ THE REFERENCE IS TAKEN HERE, ONCE, AND WITHOUT IT THIS MEASURES NOTHING.
    `recenter()` is closed-loop against a reference patch, and with none set it
    prints "no reference ... going with that" and returns having moved nothing.
    The first run of this mode did exactly that: the recoil left the view where
    it landed, so the before/after diff was the whole scene and every shot came
    back as ~9000 marks. **A recentre that silently does nothing and a view
    that never moved look identical in the frame.**

    One reference for the whole arm, not one per shot: the point is that every
    round is fired from the SAME aim, so the wall's own coordinates are the
    measurement. Re-taking it per shot would re-anchor to wherever the last
    round left the view.
    """
    got = []
    print(f'  {label}:')
    # ⚠ DISARM ONLY, NEVER RE-ARM, so the caller must run every ON shot before
    # any OFF shot. `arm()` rebuilds and re-uploads the pattern; doing that
    # between two measurements meant to differ in one thing adds a second.
    if not comp_on:
        rig.fire.disarm()
    rig.gun.ensure_ads()
    sign = pitch_sign(rig)
    print(f'    a downward count moves the wall {sign:+.3f} px on screen')
    for i in range(n):
        p, why = one_single(rig, comp_on,
                            save_as=f'{"on" if comp_on else "off"}{i}')
        # Walk on regardless of whether the shot could be read: a refused shot
        # still put a decal on the wall, so staying put would guarantee the
        # next one is unreadable too.
        step = YAW_STEP_COUNTS
        if (i + 1) % YAW_HOME_EVERY == 0:
            step = -YAW_STEP_COUNTS * (YAW_HOME_EVERY - 1)
        reaim(rig, (why or {}).get('shift', (0, 0))[1] if p is not None else 0,
              sign, step)
        if p is None:
            print(f'    shot {i + 1}: SKIPPED — {why}')
            continue
        got.append(p)
        print(f'    shot {i + 1}: hole - dot   dx {p[0]:+7.1f}  dy {p[1]:+7.1f}'
              f'   ({why["area"]} px, shift {why["shift"][0]:+.1f},'
              f'{why["shift"][1]:+.1f}, resp {why["resp"]:.2f},'
              f' {why["others"]} other)')
        # ⚠ RELOADING DROPS ADS, so re-assert it rather than assume. Measured
        # this session: with the re-assert missing, the ADS flag alternated
        # True/False shot by shot and half the rounds went from the HIP -- a
        # different recoil regime reported as the same measurement.
        rig.fire.top_up()
        rig.gun.ensure_ads()
    if len(got) >= 2:
        ys = np.array([p[1] for p in got])
        xs = np.array([p[0] for p in got])
        print(f'    -> n={len(got)}  y mean {ys.mean():7.1f}  sd {ys.std(ddof=1):5.1f}'
              f'   x mean {xs.mean():7.1f}  sd {xs.std(ddof=1):5.1f}')
    return got


def report(name, pts):
    if len(pts) < MARKS_MIN:
        print(f'  {name}: only {len(pts)} mark(s) — REFUSING to call that a '
              f'group.\n    Either the rounds missed the surface or there is '
              f'no surface. Face a wall\n    at a fixed range and re-run; an '
              f'empty diff and perfect compensation\n    look identical here.')
        return None
    y = np.array([p[1] for p in pts])
    x = np.array([p[0] for p in pts])
    # ⚠ THE OTHER HALF OF THE GUARD ABOVE, and it was missing. "REFUSING when
    # there are too few marks" only covers the empty end; a diff that covers
    # the WHOLE SCREEN passes it easily and reports a magnificent group.
    #
    # Measured 2026-08-10: ensure_ready put the character on the 200m lane
    # looking STRAIGHT UP -- recenter() had no reference for the cell, so the
    # view was never brought level -- and the magazine went into the sky. The
    # clouds moved between the two frames and the diff reported 1385 marks,
    # 1437 px vertical, 3437 px horizontal, then printed a compensation verdict
    # off it. Every number in that line was arithmetically correct.
    #
    # A magazine's group is a fraction of the frame. Anything spanning most of
    # it is the scene, not the rounds.
    if (y.max() - y.min()) > 0.5 * SCENE_H or (x.max() - x.min()) > 0.5 * SCENE_W:
        print(f'  {name}: {len(pts)} marks spanning '
              f'{y.max() - y.min():.0f} x {x.max() - x.min():.0f} px — '
              f'REFUSING.\n    That is the SCENE changing, not a group: '
              f'moving clouds, a turned view, a\n    reticle. Aim at a WALL '
              f'and hold the view still.')
        return None
    print(f'  {name}: {len(pts)} marks   vertical spread '
          f'{y.max() - y.min():6.1f} px (sd {y.std():5.1f})   '
          f'horizontal {x.max() - x.min():6.1f} px')
    return float(y.max() - y.min())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', default='m416')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--both', action='store_true',
                    help='fire a second magazine with the compensation OFF, '
                         'so the two pictures can be compared side by side')
    ap.add_argument('--singles', type=int, default=0, metavar='N',
                    help='fire N SINGLE rounds per arm instead of a magazine. '
                         'Every click is a first shot, so this is the only '
                         'mode that can say anything about the opening round')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    print('>>> FACE A WALL at a fixed range. The holes are the measurement.')
    if not ensure_ready(label='the hole-pattern probe',
                        countdown_s=args.countdown)['ok']:
        return 1

    # ⚠ THE GUN IS NOT THE OPERATOR'S JOB, AND SAYING IT WAS COST TWO RUNS.
    # This used to print "gun held and loaded" and trust it. But ENTERING THE
    # TRAINING RANGE EMPTIES THE RACK, and ensure_ready() re-enters whenever it
    # finds the game back in the lobby -- which it did, twice on 2026-08-10,
    # once after a firmware flash and once after an ERROR dialog. Both times a
    # gun placed by hand a minute earlier was gone by the time this line ran,
    # and the only symptom was `no ammo counter`, which reads like an empty
    # magazine. Every other probe in this repo calls this; this one did not.
    #
    # ensure_weapon_in_hand proves the hold by the AMMO COUNTER, and it proves
    # WHICH gun by the name plate -- a rack left loaded by the previous run
    # satisfies "a weapon is out" while being the wrong weapon.
    from control.inventory import InventoryControl
    from control.spawner import SpawnerControl
    from control.stock import ensure_weapon_in_hand
    with SpawnerControl() as sc:
        ac = InventoryControl(verbose=False)
        slot = ensure_weapon_in_hand(ac, sc, weapon=args.weapon)
        if slot is None:
            print(f'[!] REFUSING: could not get a {args.weapon} into the rack.')
            return 2
        # ⚠ AND THE OPTIC IS A SECOND STATEMENT. calibration/CLAUDE.md: 「我要了
        # 红点」和「它戴着红点」是两句话 -- a spawned gun wears whatever the
        # backpack could autofit. Rig(args.sight) assumes an optic; this is what
        # makes the assumption true rather than hoped for.
        ac.ensure_kit(slot, {'scope': args.sight})

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%m%d_%H%M%S')
    rig = Rig(args.sight)
    try:
        rounds, _ = rig.fire.top_up()
        if not rounds:
            print('[!] REFUSING: no ammo counter — nothing held, or empty.')
            return 2
        print(f'    {rounds} rounds\n')

        if args.singles:
            # ⚠ SINGLE FIRE IS A REQUIREMENT, NOT A CONVENIENCE. A 30 ms click
            # in full auto gives one round most of the time, and the times it
            # gives two are not noise -- they are a two-round measurement
            # reported as a one-round one. `fire_once` counts either way and
            # refuses; this is what stops it having to refuse constantly.
            fm = rig.gun.ensure_fire_mode(args.weapon, want='single')
            print(f'    fire mode -> {fm}')
            on_pts = singles(rig, args.singles, True, 'compensation ON ')
            off_pts = singles(rig, args.singles, False, 'compensation OFF')
            print(f'\n  -> {OUT_DIR}')
            if len(on_pts) >= 2 and len(off_pts) >= 2:
                yon = np.array([p[1] for p in on_pts])
                yoff = np.array([p[1] for p in off_pts])
                d = yon.mean() - yoff.mean()
                # sd of the DIFFERENCE of two means, so the number carries what
                # it is worth. Three shots an arm cannot settle anything; it can
                # say whether the effect is bigger than the scatter.
                se = float(np.hypot(yon.std(ddof=1) / np.sqrt(len(yon)),
                                    yoff.std(ddof=1) / np.sqrt(len(yoff))))
                print(f'\n  first-shot hole, ON minus OFF:  {d:+.1f} px '
                      f'(se {se:.1f}, {abs(d) / max(se, 1e-9):.1f} sigma)')
                print('  positive = the compensated first round lands LOWER, '
                      'i.e. it is being\n  pushed down before the recoil it is '
                      'meant to cancel has happened.')
                print('  ⚠ The ABSOLUTE position is not the measurement — the '
                      'muzzle sits below the\n  camera and the gun is zeroed at '
                      '100 m, so both arms carry the same\n  unknown offset. '
                      'Only the DIFFERENCE cancels it.')
            else:
                print('\n  not enough clean single shots to compare — '
                      'see the SKIPPED lines above')
            return 0

        (pts, dark), before, after = one_burst(rig, True)
        cv2.imwrite(os.path.join(OUT_DIR, f'{args.weapon}_{stamp}_comp_on.png'),
                    after)
        cv2.imwrite(os.path.join(OUT_DIR,
                                 f'{args.weapon}_{stamp}_comp_on_diff.png'),
                    dark)
        on = report('compensation ON ', pts)

        off = None
        if args.both:
            rig.fire.top_up()
            (pts2, dark2), _b2, after2 = one_burst(rig, False)
            cv2.imwrite(os.path.join(OUT_DIR,
                                     f'{args.weapon}_{stamp}_comp_off.png'),
                        after2)
            cv2.imwrite(os.path.join(OUT_DIR,
                                     f'{args.weapon}_{stamp}_comp_off_diff.png'),
                        dark2)
            off = report('compensation OFF', pts2)
    finally:
        rig.close()

    print(f'\n  -> {OUT_DIR}')
    if on is not None and off is not None:
        print(f'\n  The compensation takes the vertical spread from '
              f'{off:.0f} px to {on:.0f} px '
              f'({100 * (1 - on / max(off, 1e-9)):.0f}% of the climb removed).')
        print('  That number owes nothing to the view tracker, K, the bullet '
              'binning or the\n  clock — which is the point.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
