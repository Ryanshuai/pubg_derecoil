"""Photograph 库存 row ICONS through several pitches, for a VLM to name and
an INTERSECTION to clean.

    pixi run inventory-batch --plan              # what the batches are; no game
    pixi run inventory-batch --batch 0           # spawn batch 0 and shoot it 4x
    pixi run inventory-batch --solve <stamp> --names a,b,c,...    # offline

WHAT CHANGED FROM THE SINGLE-FRAME VERSION, AND WHY
───────────────────────────────────────────────────
The row icon is COMPOSITED over a blurred, darkened photograph of the world.
That is not a detail: it is why the runtime reader fails. Measured 2026-08-10
on a real night frame (night_20260810_0835), the icon matcher's top-1 was
mostly CORRECT and rejected anyway --

    row 1  tilted_grip   Lower_TiltedGrip_C   mse 206   margin 1.05
    row 6  half_grip     Lower_HalfGrip_C     mse 340   margin 3.63
    row 12 flash_smg     Muzzle_FlashHider_Medium_C  mse 226  margin 1.02

against gates of mse 150 and margin 1.25. Every row read `unknown`, `tidy()`
found nothing droppable (an unrecognised row is never `unwanted`, by design),
the pack saturated its 13-row window, `restock` could no longer see what it
already held, and four cells failed in a row.

⚠ SO THE TEMPLATE HAS TO BE THE ICON AND NOT THE ICON PLUS A BACKGROUND. The
same trick the slot tiles use: hold the item still, MOVE THE WORLD, keep the
pixels that did not change. The background differs per pitch and the icon does
not, so the intersection IS the icon.

WHY PITCH AND NOT ANYTHING ELSE. The panel is a blur of what the camera sees.
Looking at the ground and looking at the horizon are the two most different
things behind it, which is exactly what makes the strict-equality intersection
bite. The operator's instruction, 2026-08-10: 「上下抬头几次，低头到地面，抬
头到水平线」.

⚠ THE VIEW MOVES WITH TAB SHUT, ALWAYS. With the panel up, raw counts land on
the CURSOR and the view does not move at all -- measured elsewhere in this repo
at 0.29 against a still-arm noise floor of 0.32, i.e. nothing. A capture loop
that turned with Tab open would produce four identical frames, and four
identical frames INTERSECT PERFECTLY: the failure would look like success and
ship a template with the background baked in. That is what BACKDROP_MOVE_MIN
is for.

⚠ THE MODEL SUPPLIES ROW ORDER, THE SPAWNER SUPPLIES THE SET, AND THEY MUST
AGREE. Unchanged from the single-frame version and still the whole point: the
program knows WHICH parts it spawned and not which row each landed in, because
the game inserts into its own sort order. A batch whose read set differs from
its spawned set is thrown away rather than relabelled.

⚠ AND NOW THE READS MUST AGREE WITH EACH OTHER TOO. Four frames of the same
unchanged list must name the same rows in the same order; if they do not, one
of them was misread and nothing here can say which.

⚠ ONE FRAME PER PITCH, CURSOR PARKED. A hovered row draws a tooltip over its
own neighbours.

WHERE THE TEMPLATES GO: data/templates/pubg_assets/Item/Inventory, its own
directory because an inventory ROW icon and a weapon SLOT tile are different
renderings of the same part and this repository has already paid for mixing
two renderings in one bank (`_read_row`'s 80x80 note).
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from detector.attachment_catalog import ATTACHMENTS
from detector.tab_layout import icon_box
from control.session import ensure_ready
from control.spawner import SpawnerControl
from control.inventory import InventoryControl
from control.kitting import BACKPACK
from capture.cropper import capture_screen
from calibration.sweep import Rig
from control.aim import CLAMP_OVERSHOOT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'calibration', 'artifacts', 'inventory_vlm')
# ⚠ ITS OWN BANK. A row icon and a slot tile are two renderings of one part,
# and `_read_row` already carries the bill for reading one with the other's
# geometry (0/10 at 63x63, 10/10 at 80x80, with `pixi run attachments` green
# throughout because it scored the path the game does not take).
TMPL_OUT = os.path.join(ROOT, 'data', 'templates', 'pubg_assets', 'Item',
                        'Inventory')

# Which pitch STOP to shoot each frame at. +1 is down (bare ground), -1 is up
# (open sky) -- the two hard clamps, i.e. the largest change the backdrop can
# possibly take, and it spans the whole height of the panel.
#
# ⚠ THE FIRST TWO ATTEMPTS BOTH UNDER-MOVED IT, and the numbers say why.
# 2026-08-10, pitches at fractions (0.00, 0.15, 0.30, 0.44) of the travel BELOW
# the midline -- the view demonstrably swept that range, rising 3615, 2410,
# 1205 and 80 counts -- and the backdrop moved:
#
#     neighbour pair              p0->p1   p1->p2   p2->p3
#     the strip this gate reads     8.87     3.58     2.76
#     the icon column itself       10.09     2.43     2.11
#     the whole screen             18.56     7.91     4.35
#
# against a floor of 10.0. Only the first step moved anything, because every
# one of those four looks at GROUND or at the horizon: further down is not a
# different world, it is more of the same one, and the panel's blur flattens
# what little is left.
#
# ⚠ AND ALL FOUR SAT IN THE BOTTOM HALF OF THE TRAVEL. `below_frac` measures
# DOWN from the midline and cannot go above it, so the sky -- the one backdrop
# that looks nothing like the others -- was never photographed. The clamps have
# no such limit: `home_to_clamp` shoves into the stop, which is open loop on
# purpose (there is nothing to measure at a stop, and the first version of the
# travel probe reported the game's entire pitch range as 13 counts trying).
#
# ⚠ YAW TOO, AND IT IS THE AXIS THE GATE'S NUMBER CAME FROM. The 19.9-22.8
# BACKDROP_MOVE_MIN is calibrated against was measured on a 600-count
# HORIZONTAL turn (`_nudge_backdrop`), which sweeps past entirely different
# scenery -- where looking further down only ever shows more of the same
# ground. Pitch gives the two extremes the operator asked for; yaw makes each
# pair differ even when both sit at the same stop.
#
# ⚠ FOUR SHOTS, TWO EXTREMES, AND THE REPEATS ARE NOT WASTE. Sky-then-sky is
# the same framing but never the same frame: the yaw has moved, and clouds
# drift and foliage sways on top of that. What it must never become is four
# shots of ONE view -- the case BACKDROP_MOVE_MIN exists to refuse.
#
# ⚠ PITCH IS ANCHORED, YAW ACCUMULATES, and the asymmetry is deliberate. Pitch
# is re-established against a hard stop every time, so both framings are the
# same two on every batch. Yaw has no stop to anchor on and needs none:
# nothing here measures the heading, it only has to be DIFFERENT, and a
# cumulative sweep guarantees that where returning to a fixed heading would
# guarantee the opposite.
#
# (pitch stop, yaw counts to turn BEFORE this shot). +1 is down into the bare
# ground, -1 is up into open sky.
SHOT_POSES = ((+1, 0), (-1, 700), (+1, 700), (-1, 700))

# The backdrop strip must actually CHANGE between shots, and this is the gate
# that says so. Without it the intersection cannot fail: two identical frames
# intersect to a perfect copy of themselves -- background included -- and
# every downstream check passes. The number is the slot pipeline's measured
# neighbour-to-neighbour backdrop movement (19.9 at three pitches, 22.8 at the
# sawtooth), halved, because that one was measured over a whole tile and this
# is over a strip.
BACKDROP_MOVE_MIN = 10.0
# A failed intersection is scatter; a real icon is one connected blob. Size is
# NOT the criterion -- an absolute pixel floor is simply wrong for a genuinely
# small part, which `uzi_stock` proved once already.
BLOB_MIN = 0.45
# ⚠ AND BLOB_MIN ALONE IS NOT ENOUGH, which the selftest found rather than a
# run: a single surviving pixel is ONE connected component holding ALL the
# survivors, so it scores a perfect 1.000 and sails through. That is exactly
# what a scattered intersection collapses to when the coincidences are rare.
#
# So the pair is "enough survived" AND "what survived is one shape". This half
# is deliberately far below any real icon rather than tuned near one -- the
# 2% of an 80x80 crop is 128 px, while `uzi_stock`, the smallest part that
# ever caused trouble, is hundreds. An absolute floor set near a real icon is
# the mistake this repository already made once (200 px, which called
# uzi_stock unfit).
KEEP_MIN_FRAC = 0.02

# ⚠ THIRTEEN, the operator's count, and it is now MEASURED rather than taken
# on trust -- see MAX_ROWS below. It disagreed with tab_layout.INV_ROWS (12)
# and the operator was right.
BATCH = 13
# ⚠ MEASURED, AND tab_layout.INV_ROWS IS WRONG. That constant says 12 with the
# comment "rows visible at 1440p before scrolling" and no measurement behind
# it. Batch 0's frame settles it: row 12 holds Choke (detail 3088) and rows 13
# and 14 are drawn, on-screen and EMPTY (0.5 / 0.8). So the list shows at least
# 13 -- the operator's number -- and 12 was cropping the last part off every
# batch. INV_ROWS is not changed from here: it also addresses drags and the
# panel-row count in control/, so it moves in its own change with its own gate.
MAX_ROWS = 15                   # 15 is where icon_box runs off a 1440 screen
ROW_DETAIL_MIN = 100.0          # occupied 678-5228, empty 0.5-0.8
CLEAR_TRIES = 4


def batches(size=BATCH):
    """Every catalogue key, in fixed chunks. -> [[key, ...], ...]

    Sorted by key so the batches are the same on every machine and every run:
    a batch that reshuffles cannot be re-shot after a bad read.
    """
    keys = sorted(ATTACHMENTS)
    return [keys[i:i + size] for i in range(0, len(keys), size)]


def rows_held(frame):
    """How many 库存 rows have something in them. TEMPLATE-FREE.

    Laplacian variance per icon box: measured occupied 678-5228, empty 0.5-0.8,
    so the gate at 100 sits in a gap three orders wide. No template is
    consulted, which is the point -- a part whose template is missing or stale
    is exactly the part this collector exists to photograph, and a
    template-based count would read it as an empty list.
    """
    n = 0
    for i in range(MAX_ROWS):
        x0, y0, x1, y1 = icon_box(i, 'inventory')
        if y1 > frame.shape[0] or x1 > frame.shape[1]:
            break
        g = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        if cv2.Laplacian(g, cv2.CV_64F).var() > ROW_DETAIL_MIN:
            n = i + 1
    return n


def clear_verified(ac, tries=CLEAR_TRIES):
    """Empty 库存 and PROVE it. -> bool

    ⚠ THE PROOF IS THE POINT, and it is the operator's correction: a batch
    cannot go into a list that still holds the last one. Two ways this bites,
    and neither announces itself:

        the list overflows   13 + 13 = 26 rows into a window that draws ~15,
                             so the tail of batch N+1 is never photographed
        nothing spawns       a FULL 库存 makes the spawner silently produce
                             NOTHING (docs/game_quirks.md). Three template runs
                             died that way, and the only sign in the log was
                             `no bare host gun` several steps later.

    `clear_inventory` drags, and drags land ~93% of the time -- so calling it
    once and moving on is a coin flip, not a clearing. This counts the rows
    back with the template-free judgement and retries until they are gone.
    """
    for attempt in range(1, tries + 1):
        ac.clear_inventory()
        held = rows_held(capture_screen())
        if not held:
            print(f'库存 empty (verified, {attempt} pass'
                  f'{"" if attempt == 1 else "es"})')
            return True
        print(f'[!] 库存 still holds {held} row(s) after pass {attempt}')
    print(f'[!] ABORT: 库存 would not clear in {tries} passes. Spawning into a '
          f'full list produces nothing and says nothing.')
    return False


def _rel(path):
    """`path` relative to the repo when it is inside it, else as given.

    ⚠ `os.path.relpath` RAISES across Windows drives, and the selftest works in
    a temp directory that may be on another one. A path print is not worth an
    exception on the line after the work succeeded.
    """
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return path


def _backdrop_strip(frame):
    """A slice of panel that holds NO row icon, for the did-the-world-move
    check. Derived from `icon_box` rather than typed, so a layout change moves
    the probe together with the thing it is probing."""
    x0, y0, x1, y1 = icon_box(0, 'inventory')
    _, _, _, yb1 = icon_box(MAX_ROWS - 1, 'inventory')
    w = x1 - x0
    return frame[y0:yb1, max(0, x0 - w):x0]


def backdrop_move(shots):
    """Smallest mean |difference| between NEIGHBOURING pitches, on panel that
    holds no icon. -> float

    NEIGHBOURING AND SMALLEST, not the mean over all pairs. The intersection is
    only as clean as its WEAKEST pair: two shots that happen to sit at the same
    angle contribute nothing however far the others moved, and an average hides
    exactly that.
    """
    if len(shots) < 2:
        return 0.0
    out = []
    for a, b in zip(shots, shots[1:]):
        da = _backdrop_strip(a['frame']).astype('int16')
        db = _backdrop_strip(b['frame']).astype('int16')
        out.append(float(abs(da - db).mean()))
    return min(out)


def shoot_pitches(rig, ac, stamp):
    """One full-screen frame per pose in SHOT_POSES. -> [dict] | None

    TAB SHUT TO TURN, TAB UP TO SHOOT, EVERY TIME. With the panel up the raw
    counts land on the cursor and the view does not move, so a loop that keeps
    Tab open photographs one pitch four times -- and four identical frames
    intersect to a perfect copy of the background. The cycle is the cost of the
    method, not an oversight.

    FULL SCREEN, NOT `ac.frame()`. That one is a BANDED grab: it cuts a strip
    while `icon_box()` returns full-screen coordinates, so cropping one with
    the other silently returns a different rectangle. The first run of the
    single-frame version photographed the nearby list on the floor and filed it
    as the backpack. `ac.frame()` is still called, for the cursor park it does.

    ⚠ NO `ensure_hip()` HERE, AND THAT IS NOT AN OVERSIGHT -- it was called and
    it FAILED every time, on 2026-08-10, before this paragraph existed. This
    collector runs `clear_rack()` and spawns only ATTACHMENTS, so the character
    holds NO WEAPON for the whole batch. AdsDetector answers by the ABSENCE of
    the hip crosshair, and an empty-handed character draws none -- so it reads
    "scoped" forever and `ensure_hip` can never confirm what it is asking for.
    calibration/CLAUDE.md records exactly this trap for `calibrate_k`.

    Nor is it needed: the thing `ensure_hip` protects against is measuring
    pitch through a scope's own sensitivity, and a character with no gun has no
    scope. The hip travel this anchors on was itself measured with no weapon
    (config.py: "hipfire ~0.50 (TPP, no weapon)"), so the ruler and the state
    already agree.
    """
    from calibration.collect_timed import HIP_SIGHT
    # ⚠ SIZED FROM THE MEASURED TRAVEL, NOT FROM CLAMP_PUSH, and the first two
    # batches of 2026-08-10 are why. `home_to_clamp` shoves a flat CLAMP_PUSH =
    # 4000, described in its own comment as "comfortably past the travel" --
    # true of the red dot's 3400 and HALF of hip fire's measured 8034. Pushing
    # up 4000 from the bottom stop lands in the MIDDLE of the range, so every
    # frame labelled `sky` was pointed at the horizon and the two "extremes"
    # were far closer together than the code claimed. The operator saw it on
    # the frames; no number here did. (Those two batches are kept: their
    # backdrops still moved 15.8 and 15.2 against a floor of 10, and re-shooting
    # a passing batch to satisfy a better method is tidying, not measuring.)
    #
    # ASKED ONCE, BEFORE ANYTHING MOVES, so a missing ruler costs no turns.
    travel = rig.view.travel('standing', sight=HIP_SIGHT)
    if not travel:
        print(f'[!] no stop-to-stop travel stored for {HIP_SIGHT}/standing -- '
              f'the shove would be a guess. Measure it once:\n'
              f'      pixi run python tools/probe_pitch_range.py '
              f'--sight {HIP_SIGHT} --postures standing')
        return None
    shove = int(travel * CLAMP_OVERSHOOT)

    shots = []
    for idx, (direction, yaw) in enumerate(SHOT_POSES):
        if not rig.gun.ensure_inventory_closed():
            print('[!] the inventory would not close, so the view cannot move')
            return None
        # ⚠ THE STOP IS THE ANCHOR, so every batch shoots the same two framings
        # rather than wherever open-loop turns happened to leave the view. It
        # is also why nothing is verified here: at a clamp the view stares at
        # bare ground or blank sky, where phase correlation has nothing to lock
        # onto -- `home_to_clamp`'s own docstring says measuring it is not
        # possible, and the frame that comes back is the evidence anyway.
        if yaw:
            # ⚠ BEFORE the shove, not after: the stop is what makes the pitch
            # repeatable, and a yaw issued afterwards would be the last thing
            # to touch the view. Yaw does not disturb pitch, so this order
            # costs nothing and keeps the anchor last.
            rig.view.turn(yaw, 0)
        rig.view.turn(0, direction * shove)
        if not rig.gun.ensure_inventory_open():
            print('[!] the inventory would not reopen after the turn')
            return None
        ac.frame()                    # parks the cursor; a hovered row draws
        time.sleep(0.3)               # a tooltip over its own neighbours
        frame = capture_screen()
        name = f'{stamp}__p{idx}__rows.png'
        cv2.imwrite(os.path.join(OUT, name), frame)
        where = 'ground' if direction > 0 else 'sky'
        print(f'  shot {idx} at the {where} stop, yaw +{yaw} -> {name}')
        shots.append({'idx': idx, 'frame': frame, 'name': name,
                      'stop': where, 'yaw': yaw})
    return shots


def largest_blob_frac(mask):
    """Biggest connected component as a fraction of the surviving pixels."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(
        (mask > 0).astype('uint8'), connectivity=8)
    if n <= 1:
        return 0.0
    areas = stats[1:, cv2.CC_STAT_AREA]
    return float(areas.max()) / float(areas.sum())


def solve(stamp, names, write=False):
    """Intersect one batch's per-pitch crops into icons. OFFLINE. -> int

    STRICT EQUALITY, and that is what makes it a measurement rather than a
    blend. A pixel survives only if every pitch drew it identically; anything
    the world touched differs somewhere and is dropped. An average would keep a
    dimmer copy of the background instead of dropping it.

    ⚠ THE SURVIVING MASK IS THE ALPHA, AND IT IS NOT RECOVERABLE FROM THE
    PIXELS. Dropped pixels are written as black, and the icon has a black
    OUTLINE -- measured on this bank, 74 px of `vert_grip` and 13 px of
    `variable` are kept-and-black, i.e. exactly the highest-contrast pixels a
    reader wants. So the mask goes in the file's fourth channel, which is what
    `AttachmentDetector._load_templates` reads (`img.shape[2] != 4` -> skipped
    outright). A three-channel bank is not a weaker bank, it is an unloadable
    one.
    """
    with open(os.path.join(OUT, f'{stamp}__meta.json'), encoding='utf-8') as f:
        meta = json.load(f)
    if not meta.get('counts_agree'):
        print(f'[!] {stamp} is VOID: {len(meta["spawned"])} spawned, '
              f'{meta["rows_held"]} rows held.')
        return 1
    moved = meta.get('backdrop_move', 0.0)
    if moved < BACKDROP_MOVE_MIN:
        print(f'[!] {stamp} is VOID: the backdrop moved {moved:.1f}, under '
              f'{BACKDROP_MOVE_MIN}. Nothing here can separate the icon from '
              f'the world, and the intersection would succeed anyway.')
        return 1
    if sorted(names) != sorted(meta['spawned']):
        print(f'[!] {stamp} is VOID: the names read do not match what was '
              f'spawned.')
        print(f'    read    {sorted(names)}')
        print(f'    spawned {sorted(meta["spawned"])}')
        return 1

    os.makedirs(TMPL_OUT, exist_ok=True)
    kept = 0
    for i, key in enumerate(names):
        cells = []
        for pitch in meta['pitches']:
            path = os.path.join(OUT,
                                f'{stamp}__p{pitch["idx"]}__row{i:02d}.png')
            img = cv2.imread(path)
            if img is None:
                break
            cells.append(img)
        if len(cells) != len(meta['pitches']):
            print(f'  {key:16} MISSING a pitch crop -- skipped')
            continue
        keep = np.logical_and.reduce([(c == cells[0]).all(axis=2)
                                      for c in cells[1:]])
        icon = np.dstack([cells[0], keep.astype('uint8') * 255])
        icon[~keep] = 0
        frac = float(keep.mean())
        blob = largest_blob_frac(keep.astype('uint8') * 255)
        out = os.path.join(TMPL_OUT, f'{key}.png')
        exists = os.path.exists(out)
        # ⚠ ONE DECISION, USED TWICE. This was two: the same conditions were
        # spelled once to pick the printed verdict and again on the write, and
        # a mutation that removed KEEP_MIN_FRAC from the FIRST copy printed
        # `ok` while the second copy quietly kept refusing. The selftest went
        # green on a gate that had been half deleted -- a criterion with two
        # authors reports on one of itself.
        if frac < KEEP_MIN_FRAC:
            usable, verdict = False, f'only {frac:.2%} survived -- refused'
        elif blob < BLOB_MIN:
            usable, verdict = False, 'SCATTER, not an icon -- refused'
        elif exists:
            usable, verdict = False, 'already on disk -- NOT overwritten'
        else:
            usable, verdict = True, 'ok'
        print(f'  {key:16} kept {frac:6.1%} of pixels, largest blob '
              f'{blob:6.1%}  {verdict}')
        # ONLY FILLS EMPTY SLOTS. Overwriting measured 1943 -> 1869 on the slot
        # bank: a template cut from a handful of frames does not beat one mined
        # from thousands, and the file keeps the same name either way.
        if usable and write:
            cv2.imwrite(out, icon)
            kept += 1
    if write:
        # ⚠ THE ROW ORDER EXISTED IN ZERO PLACES, which is the one thing a
        # machine can never check (root CLAUDE.md). The meta recorded the SET
        # that was spawned; which row each landed in came in on the command
        # line and went nowhere, so the four batches of 2026-08-10 could not be
        # re-solved without running the vision read again. It was recoverable
        # that day only because the icons were still on disk to match crops
        # against -- an accident, not a record.
        meta['names_read'] = list(names)
        with open(os.path.join(OUT, f'{stamp}__meta.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(meta, f, indent=1, ensure_ascii=False)
    print(f'\n{kept} icon(s) written to {_rel(TMPL_OUT)}'
          if write else '\n(nothing written -- pass --write)')
    return 0


def _selftest():
    """The intersection and its three refusals, on synthetic crops. OFFLINE.

    WHAT IT CAN PROVE: that a moving backdrop is removed and a still one is
    caught, that scatter is refused, and that a name list disagreeing with the
    spawn is refused. WHAT IT CANNOT: that the game draws the panel the way
    this assumes -- only a real batch says that, and `backdrop_move` on the
    real shots is the number that reports it.
    """
    import shutil
    import tempfile

    fails = []

    def check(label, got, want):
        ok = got == want
        print(f'  {"ok  " if ok else "FAIL"}  {label:<52} {got}')
        if not ok:
            fails.append(label)

    def bank():
        """The redirected TMPL_OUT, read through globals() every time -- the
        name is rebound below and a captured copy would point at the repo."""
        return globals()['TMPL_OUT']

    rng = np.random.default_rng(11)
    tmp = tempfile.mkdtemp(prefix='invvlm_')
    globals_backup = (globals()['OUT'], globals()['TMPL_OUT'])
    try:
        globals()['OUT'] = tmp
        globals()['TMPL_OUT'] = os.path.join(tmp, 'bank')

        h = w = 80
        # One solid square, the "icon", identical in every pitch.
        icon = np.zeros((h, w, 3), np.uint8)
        icon[20:60, 20:60] = (200, 180, 160)
        keys = ['comp_ar', 'vert_grip']

        def write_batch(stamp, n_pitch, moving, spawned, held=None,
                        stable=True, mark=None, palette=255, survivors=None):
            # `stable=False` means even the ICON differs per pitch, so nothing
            # survives but coincidence -- which is what a SCATTER looks like.
            # `palette` narrows the background so those coincidences actually
            # happen; a full 0-255 background gives an EMPTY intersection, and
            # empty is a different thing from scattered.
            pitches = []
            for p in range(n_pitch):
                for i in range(len(spawned)):
                    bg = (rng.integers(0, palette, (h, w, 3), dtype=np.uint8)
                          if moving else
                          np.full((h, w, 3), 77, np.uint8))
                    cell = bg.copy()
                    this_icon = icon if mark is None else mark
                    m = this_icon.any(axis=2)
                    if stable:
                        cell[m] = this_icon[m]
                    else:
                        cell[m] = (this_icon[m] + p) % 255
                    if survivors is not None:
                        # A CHOSEN number of identical pixels, placed apart so
                        # no two touch: the intersection then survives exactly
                        # `survivors` px in `survivors` components. Built
                        # rather than sampled, because "how many coincidences
                        # a random background happens to give" is not a knob.
                        flat = cell.reshape(-1, 3)
                        step = max(2, flat.shape[0] // max(1, survivors))
                        flat[::step][:survivors] = (9, 9, 9)
                    cv2.imwrite(os.path.join(
                        tmp, f'{stamp}__p{p}__row{i:02d}.png'), cell)
                pitches.append({'idx': p, 'stop': 'ground' if p % 2 else 'sky',
                                'yaw': 700 * p,
                                'shot': f'{stamp}__p{p}__rows.png'})
            rows = len(spawned) if held is None else held
            meta = {'stamp': stamp, 'spawned': list(spawned),
                    'rows_held': rows,
                    'counts_agree': rows == len(spawned),
                    'pitches': pitches,
                    'backdrop_move': 60.0 if moving else 0.0}
            with open(os.path.join(tmp, f'{stamp}__meta.json'), 'w',
                      encoding='utf-8') as f:
                json.dump(meta, f)

        # 1. THE GOOD CASE. A moving backdrop must leave the icon and nothing
        #    else -- and it must actually WRITE, or every refusal below is
        #    satisfied by a function that refuses everything.
        write_batch('good', 4, True, keys)
        check('a moving backdrop solves', solve('good', keys, write=True), 0)
        wrote = sorted(os.listdir(bank()))
        check('...and writes one icon per key', wrote,
              ['comp_ar.png', 'vert_grip.png'])
        got = cv2.imread(os.path.join(bank(), 'comp_ar.png'),
                         cv2.IMREAD_UNCHANGED)
        check('...with the icon square intact',
              bool((got[20:60, 20:60, :3] == icon[20:60, 20:60]).all()), True)
        check('...and the background gone',
              int(got[0:10, 0:10, :3].sum()), 0)
        # ⚠ THE FOURTH CHANNEL, checked on its own. A three-channel file loads
        # and looks correct in every check above, and AttachmentDetector drops
        # it on `img.shape[2] != 4` without a word -- an unloadable bank that
        # passes its own selftest.
        check('...as a 4-channel file', got.shape, (h, w, 4))
        check('...whose alpha is opaque exactly where the icon is',
              bool((got[:, :, 3] > 0).sum() == 40 * 40), True)
        # And the row ORDER is written back, so the batch can be re-solved
        # without running the vision read a second time.
        with open(os.path.join(tmp, 'good__meta.json'), encoding='utf-8') as f:
            check('...and the meta records which row was which',
                  json.load(f).get('names_read'), keys)

        # 2. ⚠ THE REFUSAL THAT MATTERS MOST. Four IDENTICAL frames intersect
        #    perfectly -- background included -- so the failure looks exactly
        #    like the success above. Only backdrop_move can tell them apart.
        write_batch('still', 4, False, keys)
        check('a backdrop that never moved is REFUSED',
              solve('still', keys, write=True), 1)

        # 3. Names that do not match the spawn: unrecoverable, never relabelled.
        write_batch('mismatch', 4, True, keys)
        check('names disagreeing with the spawn are REFUSED',
              solve('mismatch', ['comp_ar', 'holo'], write=True), 1)

        # 4. Row count disagreeing with the spawn count.
        write_batch('short', 4, True, keys, held=1)
        check('a row count that disagrees is REFUSED',
              solve('short', keys, write=True), 1)

        # 5. ⚠ SCATTER. Nothing is stable across the pitches, so the only
        #    survivors are coincidences -- and they are spread all over the
        #    crop rather than forming one shape. This is the case an absolute
        #    pixel-count floor gets WRONG (a genuinely small icon has few
        #    pixels too), which is why the criterion is the largest connected
        #    component.
        #
        #    ⚠ IT HAD TO BE ADDED: with only the solid-square fixture above,
        #    deleting the blob check left the selftest GREEN. A gate whose
        #    fixture cannot make it fire is not being tested.
        # ⚠ FRESH KEYS, and that took a second mutation round to find. With
        # the good batch's keys, the file already existed and the
        # DO-NOT-OVERWRITE guard refused first -- so deleting the blob check
        # changed nothing and the selftest stayed green. Two gates in series,
        # the outer one masking the inner one, is indistinguishable from the
        # inner one working.
        fresh = ['holo', 'laser']
        write_batch('scatter', 4, True, fresh, stable=False, palette=3)
        check('an intersection that is scatter is REFUSED',
              solve('scatter', fresh, write=True), 0)
        check('...by writing nothing', sorted(os.listdir(bank())),
              wrote)

        # 5b. ⚠ ONE SURVIVING PIXEL SCORES A PERFECT BLOB. It is one connected
        #     component holding every survivor, so `largest_blob_frac` is
        #     1.000 and only KEEP_MIN_FRAC can refuse it. This case exists to
        #     pin that half on its own -- the scatter case above fails BOTH
        #     criteria, so with two gates in a disjunction neither was tested.
        one = ['scope_2x', 'scope_3x']
        write_batch('onepx', 4, True, one, stable=False, survivors=1)
        check('a single surviving pixel is REFUSED (blob would say 1.000)',
              solve('onepx', one, write=True), 0)
        check('...by writing nothing', sorted(os.listdir(bank())),
              wrote)

        # 5c. ⚠ AND THE MIRROR: plenty survives, but spread all over. This one
        #     passes KEEP_MIN_FRAC and must be refused by BLOB_MIN, which is
        #     the half 5b cannot reach.
        spread = ['supp_ar', 'supp_smg']
        write_batch('spread', 4, True, spread, stable=False, survivors=640)
        check('many scattered survivors are REFUSED (count would say fine)',
              solve('spread', spread, write=True), 0)
        check('...by writing nothing too', sorted(os.listdir(bank())),
              wrote)

        # 6. Nothing after a refusal reached the bank.
        check('no refused batch wrote anything',
              sorted(os.listdir(bank())), wrote)

        # 7. ⚠ EXISTING FILES ARE NEVER OVERWRITTEN, and the second batch has
        #    to carry a DIFFERENT icon or the check passes under an overwrite
        #    too -- re-writing identical bytes is indistinguishable from not
        #    writing. That is exactly how this line first passed while the
        #    overwrite guard was deleted.
        other = np.zeros((h, w, 3), np.uint8)
        other[5:75, 5:75] = (11, 22, 33)
        before = open(os.path.join(bank(), 'comp_ar.png'), 'rb').read()
        write_batch('again', 4, True, keys, mark=other)
        solve('again', keys, write=True)
        after = open(os.path.join(bank(), 'comp_ar.png'), 'rb').read()
        check('a second solve does not overwrite', before == after, True)
    finally:
        globals()['OUT'], globals()['TMPL_OUT'] = globals_backup
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f'{len(fails)} FAILED: {", ".join(fails)}')
        return 1
    print('all ok')
    return 0


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--batch', type=int, help='which chunk (0-based)')
    ap.add_argument('--keys', help='explicit comma-separated keys instead')
    ap.add_argument('--plan', action='store_true', help='print and exit')
    ap.add_argument('--solve', metavar='STAMP',
                    help='intersect a captured batch offline. Needs --names.')
    ap.add_argument('--names',
                    help='comma-separated keys, IN ROW ORDER, as read off the '
                         'shots by a vision model. Checked against what was '
                         'spawned as a SET before anything is written.')
    ap.add_argument('--write', action='store_true',
                    help='--solve: actually write the icons')
    ap.add_argument('--selftest', action='store_true',
                    help='the intersection and its refusals, on '
                         'synthetic crops. No game, no hardware.')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if args.solve:
        if not args.names:
            ap.error('--solve needs --names')
        return solve(args.solve,
                     [k.strip() for k in args.names.split(',') if k.strip()],
                     write=args.write)

    chunks = batches()
    if args.plan:
        print(f'{len(ATTACHMENTS)} parts in {len(chunks)} batches of {BATCH} '
              f'(measured: the list draws 13 occupied rows)')
        for i, c in enumerate(chunks):
            print(f'  {i}: {" ".join(c)}')
        return 0

    keys = ([k.strip() for k in args.keys.split(',') if k.strip()]
            if args.keys else
            chunks[args.batch] if args.batch is not None else None)
    if not keys:
        ap.error('give --batch, --keys or --plan')
    unknown = [k for k in keys if k not in ATTACHMENTS]
    if unknown:
        ap.error(f'not in the catalogue: {", ".join(unknown)}')

    ready = ensure_ready(label='inventory batch', countdown_s=args.countdown)
    if not ready['ok']:
        print(f'[!] ABORT: not ready — failed at {ready["failed"]!r}')
        return 1

    stamp = time.strftime('%Y%m%d_%H%M%S')
    os.makedirs(OUT, exist_ok=True)
    rig = Rig('red_dot')
    sc, ac = SpawnerControl(verbose=False), InventoryControl(verbose=False)
    try:
        # A backpack first: without one the parts do not refuse to spawn, they
        # go somewhere else. Then clear, so the list holds THIS batch and
        # nothing left over -- the set cross-check depends on it.
        rig.gun.ensure_inventory_closed()
        sc.give_many([BACKPACK])
        # ⚠ `ac.is_tab_open()` ASKS whether the panel is up; it does not put it
        # up, and ensure_ready leaves Tab SHUT on purpose -- so polling it here
        # is correct-and-useless and the batch aborted before spawning
        # anything. `ensure_inventory_open` is the action.
        if not rig.gun.ensure_inventory_open():
            print('[!] the inventory would not open')
            return 1
        ac.clear_rack()
        if not clear_verified(ac):
            return 1

        rig.gun.ensure_inventory_closed()
        rec = sc.give_many(list(keys))
        if not rec.get('ok'):
            print(f'[!] the spawner refused: {rec.get("error")}')
        time.sleep(1.0)
        if not rig.gun.ensure_inventory_open():
            print('[!] the inventory would not reopen after spawning')
            return 1
        shots = shoot_pitches(rig, ac, stamp)
        if shots is None:
            return 1
        frame = shots[0]['frame']

        # ⚠ CROP WHAT IS OCCUPIED, not range(INV_ROWS). That constant is 12
        # and the list holds 13, so the last part of every batch lost its
        # crop -- silently, because a short list of crops looks exactly like a
        # short batch. `held` is the template-free count.
        held = rows_held(frame)
        for sh in shots:
            for i in range(held):
                x0, y0, x1, y1 = icon_box(i, 'inventory')
                cv2.imwrite(os.path.join(
                    OUT, f'{stamp}__p{sh["idx"]}__row{i:02d}.png'),
                    sh['frame'][y0:y1, x0:x1])
        # ⚠ THE COUNT IS A CROSS-CHECK, not a log line. Rows held and parts
        # spawned are two independent statements about the same batch: one
        # comes from the spawner's own record, the other from the screen. They
        # disagree when a part did not arrive, when the list was not empty
        # first, or when the batch overflowed the window -- and a disagreement
        # makes the row->key mapping unrecoverable.
        if held != len(keys):
            print(f'[!] {len(keys)} parts spawned but {held} rows are '
                  f'occupied — this batch is VOID. The names cannot be '
                  f'matched to the keys when the counts disagree.')
        meta = {'stamp': stamp, 'spawned': list(keys), 'rows_held': held,
                'counts_agree': held == len(keys),
                'pitches': [{'idx': sh['idx'], 'stop': sh['stop'],
                             'yaw': sh['yaw'], 'shot': sh['name']}
                            for sh in shots],
                'backdrop_move': backdrop_move(shots),
                'note': 'names are read from the shots by a vision model. The '
                        'set of names MUST equal `spawned`, and all pitches '
                        'MUST name the same rows, or the batch is void. Then '
                        '--solve intersects the per-pitch crops.'}
        with open(os.path.join(OUT, f'{stamp}__meta.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(meta, f, indent=1, ensure_ascii=False)
        print(f'\nbackdrop moved by {meta["backdrop_move"]:.1f} '
              f'(need {BACKDROP_MOVE_MIN}) between neighbouring pitches')
        if meta['backdrop_move'] < BACKDROP_MOVE_MIN:
            print(f'[!] THE WORLD DID NOT MOVE between shots. An intersection '
                  f'over identical frames returns the frame — background and '
                  f'all — and every check after it passes. This batch is VOID.')
        print(f'spawned {len(keys)}: {" ".join(keys)}')
        print(f'shots   {len(shots)} poses under {_rel(OUT)}')
        print(f'meta    {_rel(os.path.join(OUT, stamp + "__meta.json"))}')
        # ⚠ THE NEXT STEP IS A HUMAN-OR-MODEL READ, AND IT IS NOT OPTIONAL.
        # The crops on disk carry no identity: the game inserts into its own
        # sort order, so row 0 is whatever the game put there. Nothing may be
        # written to the bank until the names come back and match `spawned` as
        # a set.
        print(f'\nnext: read the names off the shots, then\n'
              f'      pixi run inventory-batch --solve {stamp} '
              f'--names <row0,row1,...> --write')
    finally:
        try:
            ac.close()
        except Exception:
            pass
        rig.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
