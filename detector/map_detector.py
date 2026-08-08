"""The training range map screen: is it up, and where is the player standing.

The eyes for control/map.py; the hands are there. Per the package rule this
runs on a stored PNG -- no game, no hardware:

    python detector/map_detector.py calibration/artifacts/map/map_400m.png
    python detector/map_detector.py --selftest

TWO INDEPENDENT SIGNALS say the map is up, and both are positive -- the map
draws something, rather than the world failing to. Measured across the three
stored frames:

    signal                              open    shut   after teleport (shut)
    left-panel selection border        320 px    0 px     0 px
    player marker on the map           445 px    0 px     0 px   (see below)

`map_open()` is their OR, which is what makes CLOSING verifiable: the map is
gone only when BOTH are, so a map that has been panned until the marker is
off-screen still reads open and still gets its M. Requiring only the marker
is what the first version did, and control/map.py's close step would have
returned success without sending a single keypress.

⚠ **THE MINIMAP DRAWS THE SAME MARKER.** The first version of this file said
map_open() had "one untested direction: no game frame has been checked for a
false positive" -- and the first live frame taken after writing that broke it,
reading map_open=True with the map shut, off a marker at (3222, 1227) in the
bottom-right minimap. Not an edge case: the minimap is up whenever the big map
is not, so the probe was true unconditionally. MINIMAP_BOX is blanked before
any search. Both frames are in calibration/artifacts/map/ and both are in selftest(), which is
the point: a corpus holding only the frame a probe was written against can
only ever agree with it.

⚠ **The marker's colour is what separates it from the highlight it stands
on.** The range highlight is TRANSLUCENT yellow over sand and never reaches
the marker's saturation; the marker is painted opaque on top. Strict mask
keeps only the marker (445 px vs the highlight's 0..7 px of border specks),
loose mask keeps both (484 vs 9992). That is why "did the teleport land" is
answerable at all -- after a jump the marker sits ON the box.

⚠ **The residual false-positive risk is not zero and not measured**: a
saturated yellow ~25 px disc somewhere else on screen outside MINIMAP_BOX
would still read as a marker, and the training range has not been swept for
one. The left-panel signal does not share that failure, which is the other
half of why the OR is worth having.
"""
import os
import sys
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from config import (MAP_LEFT_PANEL_W, MAP_RANGE_BOXES, MAP_RANGE_SPAWN,
                    MINIMAP_BOX, Rect)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# Both masks are channel differences, not absolute levels: the map is drawn
# over a blurred game world whose brightness moves with the time of day, and
# "yellow" survives that where "bright" does not. Same reasoning as
# ads_detector's relative measure.
#
# STRICT: the opaque marker (and the left panel's selection border, which is
# the same UI yellow). LOOSE: those plus the translucent range highlights.
_STRICT = dict(gb=90, gmin=170, rmin=150, bmax=110)
_LOOSE = dict(gb=55, gmin=120, rmin=120, bmax=255)

# The marker is a 23x24 disc, 445 px filled. Area alone does NOT identify it,
# and the first version of this file believed it did:
#
#   strict-mask blobs, whole screen      area   w x h
#     player marker            (1917,988) 445   23x24   <- wanted
#     left-panel selection box   (80,756) 167    4x49   <- inside area 100..2000
#     left-panel selection box   (80,830) 153    4x46   <- inside area 100..2000
#
# The training-range list down the left edge draws a yellow border around the
# selected entry, and its vertical strokes pass an area gate aimed at a disc.
# That run only got the right answer because the marker happened to be the
# LARGEST blob -- i.e. the gate was decorative, and would have handed back a
# menu border the moment the marker was missing, which is exactly the case
# at_range() has to answer False on.
#
# The side gate is what actually separates them (4 px vs 23), and it beats
# restricting the search to a map ROI: the map pans and zooms, the marker's
# size does not. Fill is a second, weaker check -- the strokes pass it too
# (0.85 vs the disc's 0.81), so it is here for hollow frames, not for these.
#
# Those same strokes then became the left-panel signal. Nothing was thrown
# away: the thing that broke one probe is the evidence under another.
MARKER_AREA_MIN = 100
MARKER_AREA_MAX = 2000     # anything bigger is not a 25 px disc
MARKER_SIDE = (15, 35)     # both w and h; the disc measures 23x24
MARKER_FILL_MIN = 0.6      # area / (w*h); the disc is 0.81

# The left panel's selection border, measured on calibration/artifacts/map/map_400m.png: two
# vertical strokes at x=80, 4x49 and 4x46, 320 px between them. Both stored
# frames with the map shut read 0 px in this strip.
PANEL_YELLOW_MIN = 60      # 5x under the measured 320, well over the 0 of both
                           # negative frames. One open frame is thin evidence
                           # for the floor; it is the count being ZERO when
                           # shut that carries this, not the margin.


class Blob(NamedTuple):
    area: int
    cx: float
    cy: float
    x: int
    y: int
    w: int
    h: int


def _yellow(frame, spec):
    b, g, r = (frame[:, :, i].astype(np.int16) for i in range(3))
    mask = (((g - b) > spec['gb']) & ((r - b) > spec['gb'])
            & (g > spec['gmin']) & (r > spec['rmin'])
            & (b < spec['bmax'])).astype(np.uint8)
    # Blanked here rather than in each caller, so nothing in this module can
    # find the minimap's copy of the marker by forgetting to.
    #
    # ⚠ highlight_box() sees this blanking too. That is harmless today (no
    # range highlight is drawn in the bottom-right corner) and would silently
    # truncate one that were -- if a range is ever added down there, measure
    # it with the blanking off.
    mask[MINIMAP_BOX.slice] = 0
    return mask


def _blobs(mask):
    """Connected components as Blob tuples, largest area first."""
    n, _, stats, cent = cv2.connectedComponentsWithStats(mask)
    out = [Blob(int(stats[i, 4]), float(cent[i][0]), float(cent[i][1]),
                int(stats[i, 0]), int(stats[i, 1]),
                int(stats[i, 2]), int(stats[i, 3]))
           for i in range(1, n)]
    return sorted(out, reverse=True)


def _rejected_by(b):
    """Which gate rejects this blob as the marker, or None if it passes.

    One definition, two readers: player_xy() acts on it and main() prints it.
    They were separate copies for one commit, which is one commit longer than
    a diagnostic that exists to explain a decision should ever disagree with
    the decision.
    """
    lo, hi = MARKER_SIDE
    if not MARKER_AREA_MIN <= b.area <= MARKER_AREA_MAX:
        return 'area'
    if not (lo <= b.w <= hi and lo <= b.h <= hi):
        return 'side'
    if b.area / float(b.w * b.h) < MARKER_FILL_MIN:
        return 'fill'
    return None


# ── The two open-signals ──

def panel_visible(frame):
    """Is the map's left-hand training-area list on screen. -> bool

    Independent of where the map is panned to, which the marker is not.
    """
    # _yellow returns 0/1, not 0/255 -- sum() is already a pixel count.
    mask = _yellow(frame, _STRICT)
    return int(mask[:, :MAP_LEFT_PANEL_W].sum()) >= PANEL_YELLOW_MIN


def player_xy(frame):
    """Where the player marker is, in screen pixels. -> (x, y) | None

    None means no marker on the big map -- which happens with the map shut,
    and ALSO with it open and panned away from the player. Those two are not
    separated here; map_open() is what separates them.
    """
    for b in _blobs(_yellow(frame, _STRICT)):
        if _rejected_by(b) is None:
            return (int(round(b.cx)), int(round(b.cy)))
    return None


def map_open(frame):
    """Is the map screen up. -> bool.  Either signal is enough; see the module
    note on why closing needs the OR and not the AND."""
    return panel_visible(frame) or player_xy(frame) is not None


# ── Where the player is, relative to a range ──

# A landed teleport puts the marker at the range's SPAWN POINT, which is drawn
# at the top edge of the highlight and slightly outside it: the first live run
# (2026-08-06) arrived at (1977, 450) against a box whose top is y=460. Ten
# pixels out.
#
# ⚠ THIS USED TO BE ONE PREDICATE WITH A PAD, AND THE PAD WAS WRONG. Padding
# the box by 20 px to admit the arrival point also widened it 20 px on the
# other three sides -- and the neighbouring practice area's outline starts
# about 10 px right of x1=1999. A character standing just inside the next lane
# read as "already standing in the 200m range", so goto_range() skipped the
# teleport and the round ran from the wrong place: the exact outcome the whole
# feature exists to prevent, reported as success.
#
# They are two different questions and now have two predicates:
#   near_spawn  did the jump land          a POINT, one measurement, radius
#   in_box      is the character in there  the highlight's own bounds, no pad
#
# ⚠ ONE measurement behind the spawn point and the radius. A second landing
# that misses by more than 40 px is not a bug in the caller, it is this number
# needing a second sample.
ARRIVAL_RADIUS = 40


def in_box(xy, name):
    """Is this point inside the range's highlight. -> bool. No padding."""
    if xy is None:
        return False
    return MAP_RANGE_BOXES[name].contains(xy)


def near_spawn(xy, name):
    """Is this point on the range's teleport arrival spot. -> bool"""
    if xy is None:
        return False
    sx, sy = MAP_RANGE_SPAWN[name]
    return (xy[0] - sx) ** 2 + (xy[1] - sy) ** 2 <= ARRIVAL_RADIUS ** 2


def at_range_xy(xy, name):
    """Is the player at this range, given an already-read marker. -> bool

    Takes the point rather than the frame so a caller that needs both the
    answer and the position pays for one full-frame pass instead of three.
    """
    return in_box(xy, name) or near_spawn(xy, name)


def at_range(frame, name):
    """Is the player standing at the named range. -> bool"""
    return at_range_xy(player_xy(frame), name)


def highlight_box(frame):
    """Measure a range highlight off the frame -> Rect | None.

    NOT used to drive anything -- the click target is a constant, for the same
    reason the spawner's is (detector/CLAUDE.md: no recognition on the driving
    path). This is the tool that produced that constant and the one that
    re-measures it after a patch moves the map.

    ⚠ **IT NEEDS A HOVERED FRAME, AND THE DRIVING PATH NEVER PRODUCES ONE.**
    The highlight is drawn because the cursor is over that range -- the
    reference frame has its preview card visible for the same reason. Every
    frame control/map.py reads is taken with the cursor parked off the map, so
    re-measuring means grabbing a frame BY HAND with the mouse on the range.
    Returns the largest highlight, singular, because only one is ever lit.
    """
    for b in _blobs(_yellow(frame, _LOOSE)):
        if b.area > MARKER_AREA_MAX:
            return Rect(b.y, b.x, b.h, b.w)
    return None


# ── Offline gate ──

# ⚠ docs/ is gitignored, like every other corpus in this repo -- these two
# frames live on the machine that captured them and a fresh clone has neither.
# selftest() says which one is missing rather than reporting a pass over an
# empty corpus. Re-grab REFERENCE with the map open and the cursor ON the 200m
# range (highlight_box needs the hover); grab NEGATIVE in a plain match.
REFERENCE = os.path.join(_ROOT, 'calibration', 'artifacts', 'map', 'map_400m.png')
NEGATIVE = os.path.join(_ROOT, 'calibration', 'artifacts', 'map', 'ingame_minimap.png')


def selftest():
    """Both directions of every gate. -> (ok, [lines])

    The one-sided version of this file passed its own inspection while the
    side gate did not exist, because a corpus of one frame with the marker
    present can only ever confirm. Every case below that starts 'no marker'
    or 'game frame' is the missing half.

    ⚠ Two cases keep the corpus itself honest, by requiring a frame to FAIL
    when a gate is switched off: a negative sample that passes for the wrong
    reason is worth nothing.
    """
    global MINIMAP_BOX
    frame = cv2.imread(REFERENCE)
    game = cv2.imread(NEGATIVE)
    for path, img in ((REFERENCE, frame), (NEGATIVE, game)):
        if img is None:
            return False, [f'{path} is missing']

    # Painting the marker out is the negative frame: same open map, same menu
    # borders down the left edge, no marker to find.
    blank = frame.copy()
    blank[970:1010, 1900:1936] = (60, 60, 60)

    # ...and here it is on the 200m arrival point, which is the state a landed
    # teleport leaves behind. Cut rather than drawn, so the colour is the
    # game's own and not a guess at it.
    sx, sy = MAP_RANGE_SPAWN['200m']
    moved = blank.copy()
    moved[sy - 12:sy + 13, sx - 12:sx + 13] = frame[976:1001, 1905:1930]

    # Just inside the NEXT lane -- 15 px right of the box, which the old
    # pad-by-20 predicate accepted as "already at the 200m range".
    _r = MAP_RANGE_BOXES['200m']
    next_lane = (_r.x1 + 15, (_r.y0 + _r.y1) // 2)

    saved, MINIMAP_BOX = MINIMAP_BOX, Rect(0, 0, 0, 0)
    try:
        unguarded = player_xy(game)
    finally:
        MINIMAP_BOX = saved
    mx0, my0, mx1, my1 = saved.x0, saved.y0, saved.x1, saved.y1

    cases = [
        # the reference frame, map open
        ('marker found on the reference', player_xy(frame) == (1917, 988)),
        ('panel signal sees an open map', panel_visible(frame) is True),
        ('map_open true with a marker', map_open(frame) is True),
        ('player at 400m is not at 200m', at_range(frame, '200m') is False),
        ('highlight measures back to the constant',
         highlight_box(frame) == MAP_RANGE_BOXES['200m']),

        # map open, marker unreadable -- the panned-away case
        ('no marker -> player_xy None', player_xy(blank) is None),
        ('no marker -> at_range False', at_range(blank, '200m') is False),
        ('...but map_open STAYS TRUE, so close still presses M',
         map_open(blank) is True),

        # a real game frame, map shut, minimap up
        ('game frame (map shut) -> map_open False', map_open(game) is False),
        ('game frame -> no panel', panel_visible(game) is False),
        ('game frame -> player_xy None', player_xy(game) is None),
        ('game frame -> at_range False', at_range(game, '200m') is False),
        ('game frame -> no highlight measured', highlight_box(game) is None),
        (f'...and without the ROI it WOULD read the minimap {unguarded}',
         unguarded is not None
         and mx0 <= unguarded[0] <= mx1 and my0 <= unguarded[1] <= my1),

        # arrival vs occupancy, the two questions ARRIVAL_PAD used to conflate
        ('marker on the spawn point reads at_range',
         at_range(moved, '200m') is True),
        ('...via near_spawn, since it is OUTSIDE the box',
         near_spawn(MAP_RANGE_SPAWN['200m'], '200m') is True
         and in_box(MAP_RANGE_SPAWN['200m'], '200m') is False),
        ('a point in the NEXT lane is not at 200m',
         at_range_xy(next_lane, '200m') is False),
        ('...which the old pad-by-20 predicate got wrong',
         next_lane[0] <= _r.x1 + 20),
    ]
    lines = [f'  {"ok  " if ok else "FAIL"}  {name}' for name, ok in cases]
    return all(ok for _, ok in cases), lines


def main():
    args = sys.argv[1:]
    if args and args[0] == '--selftest':
        ok, lines = selftest()
        print('\n'.join(lines))
        print('selftest:', 'ok' if ok else 'PROBLEM')
        return 0 if ok else 1

    path = args[0] if args else REFERENCE
    frame = cv2.imread(path)
    if frame is None:
        print(f'cannot read {path}')
        return 1

    strict = _blobs(_yellow(frame, _STRICT))
    xy = player_xy(frame)
    print(f'{path}  {frame.shape[1]}x{frame.shape[0]}')
    print(f'map_open      : {map_open(frame)}')
    print(f'  panel signal: {panel_visible(frame)}')
    print(f'  player_xy   : {xy}')
    for name in MAP_RANGE_BOXES:
        print(f'at_range({name!r})  : {at_range_xy(xy, name)}'
              f'   in_box={in_box(xy, name)} near_spawn={near_spawn(xy, name)}')
    print(f'largest highlight measured off this frame: {highlight_box(frame)}')

    # Every candidate with the gate that rejected it: a blob list without this
    # column is what let the left-panel borders look acceptable.
    print('\nstrict-mask blobs (marker candidates):')
    for b in strict[:6]:
        print(f'  area={b.area:6d} centre=({b.cx:.0f},{b.cy:.0f}) {b.w}x{b.h}'
              f'  fill={b.area / float(b.w * b.h):.2f}'
              f'  {_rejected_by(b) or "--> MARKER"}')
    print('loose-mask blobs (highlights):')
    for b in _blobs(_yellow(frame, _LOOSE))[:5]:
        print(f'  area={b.area:6d} centre=({b.cx:.0f},{b.cy:.0f}) {b.w}x{b.h}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
