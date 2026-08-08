"""Reading the item-spawner panel: where the categories are, where a expanded
submenu's entries are, and whether a column changed.

Purely geometric, no OCR. The panel dims the scene behind it, so UI text
stands out as clean bright bands; a real category column is the one whose
bands are evenly spaced, which is what rejects the sensitivity sliders and the
bottom-right buttons without hardcoding where they sit.

Everything here reads a full-screen BGR frame and returns coordinates. Driving
the mouse to those coordinates is control/spawner.py; capturing
the whole menu to disk is tools/scrape_spawner.py. See calibration/artifacts/spawner/README.md
for the measurements behind every threshold.
"""
import cv2
import numpy as np

from detector.geometry import segments

# ── Panel geometry search window ─────────────────────────────────────────
# Generous bounds around where the spawner panel renders; only used to keep
# the scene outside the panel from polluting the projections.
PANEL_X0, PANEL_X1 = 380, 2700
PANEL_Y0, PANEL_Y1 = 150, 1350

TEXT_THRESH = 190      # panel text is near-white over a dimmed background
MIN_COL_PIX = 3        # bright px in a column before it counts as text
MIN_ROW_PIX = 3        # bright px in a row before it counts as text
COL_GAP = 80           # x gap that separates two columns
ROW_H_MIN, ROW_H_MAX = 8, 40
ROW_SPACING_TOL = 4.0  # px a row may deviate from the column's row pitch
MIN_ROWS = 3           # fewer than this and it is not a category list

# Where the panel draws its category columns, at 3440x1440. Used only when
# segmentation fails.
#
# Hard-coding these is defensible in a way hard-coding the ROWS would not be.
# A column's x span is panel chrome: it is the same whatever the game ships,
# and it was measured identically on every successful sync -- 529, 1029, 1529,
# a 500 px pitch, plus the sensitivity column that is not a clickable list and
# gets rejected on its rows anyway. The rows inside are the item catalogue,
# which grows with every content patch, so those stay discovered.
#
# Segmentation is still tried first. This is the floor under it, not a
# replacement: it is what stops a run dying because the character happened to
# be facing bright sand when the panel opened.
COLUMN_SPANS = ((520, 1010), (1020, 1510), (1520, 2010), (2020, 2510))
MERGED_COL_W = 900     # a "column" wider than this is a failed segmentation

CLICK_X_OFFSET = 45    # from the row's leftmost bright px (the ">" chevron)
                       # onto the first glyph of the label
BOX_LEFT_PAD = 20      # how far left of the chevron a column's box starts

# ── The measured layout ──────────────────────────────────────────────────
#
# Where the category rows and the column boxes ARE, as constants. This is the
# primary path: control/spawner.py clicks these numbers and never looks for a
# row. find_menu() below is the fallback, run only when clicking a measured
# coordinate has already failed.
#
# Provenance — two independent capture runs, which agreed to the pixel on all
# 21 categories:
#
#   calibration/artifacts/spawner/runs/20260801_205423   first run, 20/21
#   calibration/artifacts/spawner/runs/20260801_210656   clean run, 21/21
#
# tools/scrape_spawner.py merged them into calibration/artifacts/spawner/layout.json, which is
# kept as the record of the measurement; `pixi run spawner-plan` asserts these
# constants still reproduce it entry for entry. Measurements and method are in
# calibration/artifacts/spawner/README.md sections 2 and 2b.
#
# Why constants and not recognition. find_menu() has to separate UI text from
# terrain through a translucent panel, so what it can read depends on which way
# the player happens to be facing. Facing bright sand every column merged into
# one band; facing a red banner ITEMS and SENSITIVITY merged into an x range
# whose rows are not evenly pitched, the band was rejected as "not a list", and
# column 3 stopped existing as far as the tool was concerned -- four re-reads
# in a row, same answer, with the panel plainly on screen. The geometry did not
# move through any of that. Recognising it every time buys nothing and costs
# the run.
#
# What stays discovered, deliberately: WHICH categories exist and WHAT is
# inside them. Those are the item catalogue, and it grows with every content
# patch -- which is why COLUMN_ROWS is a floor for "the panel finished drawing"
# rather than a truth, and why spawn()/give_many() still count the entries on
# screen before clicking an index into them.
#
# Only a game update or a resolution change can move these. Re-run
# tools/scrape_spawner.py and update this block if one does.

LAYOUT_SCREEN = (3440, 1440)      # the resolution every number here was taken at

# Row centres of a COLLAPSED category column, top to bottom, shared by all
# three columns. Evenly pitched: 305 + (384/9)*(n-1) reproduces all ten to the
# pixel, and `pixi run spawner-plan` checks that it still does -- a typo in one
# row would otherwise click the row above or below it.
CATEGORY_Y = (305, 348, 390, 433, 476, 518, 561, 604, 646, 689)

# Each category column's panel box, x0..x1. 500 px pitch. The fourth column
# (SENSITIVITY) is sliders, not a clickable list, and is not addressable here.
COLUMN_BOX = {1: (529, 1004),     # WEAPONS
              2: (1029, 1504),    # ATTACHMENTS
              3: (1529, 2004)}    # ITEMS

# Rows per column in this build: 突击步枪..轻机枪 / 握把..瞄准镜 / 汽油桶..背包.
COLUMN_ROWS = {1: 10, 2: 5, 3: 6}

# Click x measured from the box's left edge: past the ">" chevron and onto the
# first glyph of the label. Identical on all three columns, and it is the same
# pixel the recognition path picks -- column_boxes() pads BOX_LEFT_PAD left of
# the chevron, find_menu() clicks CLICK_X_OFFSET right of it. Asserted, so the
# constant path and the fallback cannot drift apart into clicking two
# different places.
COLUMN_CLICK_DX = BOX_LEFT_PAD + CLICK_X_OFFSET      # 65


def category_point(col, row):
    """(x, y) to click category `row` (1-based) of column `col`.

    A measured constant, not a search: no frame, no panel, no game. Valid
    whenever nothing ABOVE this row in the SAME column is expanded, which is
    what visiting a column bottom-up guarantees -- see calibration/artifacts/spawner/README.md
    section 3b, and plan()'s ordering in control/spawner.py.
    """
    if col not in COLUMN_BOX:
        raise KeyError(f'column {col} is not in the measured layout '
                       f'({sorted(COLUMN_BOX)})')
    if not 1 <= row <= COLUMN_ROWS[col]:
        raise IndexError(f'col{col}_row{row:02d} is outside the measured '
                         f'layout ({COLUMN_ROWS[col]} rows in column {col})')
    return COLUMN_BOX[col][0] + COLUMN_CLICK_DX, CATEGORY_Y[row - 1]


def known_layout():
    """The measured layout, shaped like find_menu()/column_boxes() output.

    -> ({col: [{'row', 'y', 'click_x'}, ...]}, {col: (x0, x1)})

    Same shape as calibration/artifacts/spawner/layout.json, so a caller can take either. This
    one cannot fail: no file to be missing, no frame to be misread.
    """
    menu = {c: [{'row': r, 'y': CATEGORY_Y[r - 1],
                 'click_x': COLUMN_BOX[c][0] + COLUMN_CLICK_DX}
                for r in range(1, n + 1)]
            for c, n in COLUMN_ROWS.items()}
    return menu, dict(COLUMN_BOX)

# Submenu entries sit in bordered tiles, and on a long list those borders
# bridge the gaps between rows — at the category threshold the whole column
# projects as one unbroken band. The border is 200..202 grey against 238 for
# text, so a higher threshold separates them cleanly.
SUBMENU_THRESH = 215
SUBMENU_ROW_GAP = 6      # rejoins a narrow label split across scan lines
SUBMENU_CENTRE_TOL = 45  # entries are centred; categories are left-aligned

# Expanded vs collapsed is decided on how many text pixels changed inside the
# clicked column's box. Measured over a full 21-category run: a real expansion
# moves 489..21096 px (the floor is the last row of a column, whose submenu has
# two entries and nothing below it to push down), while an untouched column
# drifts by at most ~75. The run and both bounds are in calibration/artifacts/spawner/README.md
# §3 — the 60-line probe that produced them was deleted 2026-08-08, because
# re-running it can only reprint that table.
CHANGE_MIN = 200

# How far below its category row the submenu's first entry starts. Measured
# over 42 ground-truthed frames across both runs in calibration/artifacts/spawner/runs/ (the
# file name colN_rowMM_open.png IS the answer): +35..+37, never outside.
#
# This is what makes the panel readable WITHOUT the collapsed baseline. The
# same 42 frames also showed the category rows do not move when something
# expands -- the submenu draws over its neighbours rather than pushing them
# down -- so one collapsed reading of the categories stays valid forever, and
# a frame can say which node it is at on its own. See calibration/artifacts/spawner/README.md
# section 3b.
SUBMENU_OFFSET = 36
SUBMENU_OFFSET_TOL = 12

# Where a submenu's entries are DRAWN, so they can be clicked without looking
# for them first. Calibrated over the same 40 ground-truthed frames
# and the panel turns out to be rigid:
#
#   category centre -> first entry centre   44.25 px   sd 0.43
#   entry pitch                             50.70 px   sd 0.17
#   entry click x, from the box's left      237.00 px  sd 0.00
#
# That is why the spawner does not need a screenshot per click. Finding the
# entries with find_submenu_items() costs a frame each time and returns the
# same numbers these produce.
SUBMENU_ENTRY_DY = 44.25
SUBMENU_ENTRY_PITCH = 50.70
SUBMENU_CLICK_DX = 237


def entry_point(box, cat_y, index):
    """(x, y) to click submenu entry `index` (1-based) of a category at cat_y.

    `cat_y` must be the category's CURRENT y. Read from a collapsed sync it is
    correct as long as nothing above it in the same column is expanded -- which
    is exactly what visiting a column bottom-up guarantees.
    """
    return (int(box[0] + SUBMENU_CLICK_DX),
            int(round(cat_y + SUBMENU_ENTRY_DY + SUBMENU_ENTRY_PITCH * (index - 1))))

# Somewhere outside the panel to leave the cursor while a screenshot is taken:
# the row under the cursor draws a hover highlight, which would otherwise make
# a collapsed panel differ from the baseline.
PARK_XY = (200, 1380)


class MenuItem:
    """One clickable category row."""

    def __init__(self, col, idx, y, x0, x1, y0, y1):
        self.col = col          # column index, 1-based, left to right
        self.idx = idx          # row index within the column, 1-based
        self.y = y              # row centre
        self.x0, self.x1 = x0, x1   # bright-pixel extent of the row
        self.y0, self.y1 = y0, y1
        self.set_column_left(x0)

    def set_column_left(self, left):
        """Place the click point relative to the column's chevron column."""
        self.click_x = (left + CLICK_X_OFFSET if self.x1 - left >= 60
                        else (left + self.x1) // 2)

    @property
    def key(self):
        return f'col{self.col}_row{self.idx:02d}'

    def label_crop(self, img, pad=6):
        return img[max(0, self.y0 - pad):self.y1 + pad,
                   max(0, self.x0 - pad):self.x1 + pad].copy()

    def as_dict(self):
        return {'key': self.key, 'col': self.col, 'idx': self.idx,
                'click': [self.click_x, self.y],
                'bbox': [self.x0, self.y0, self.x1, self.y1]}


def bright_mask(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    m = np.zeros(g.shape, np.uint8)
    m[PANEL_Y0:PANEL_Y1, PANEL_X0:PANEL_X1] = (
        g[PANEL_Y0:PANEL_Y1, PANEL_X0:PANEL_X1] > TEXT_THRESH)
    return m


def _regular_run(centres):
    """Keep the longest evenly-spaced run — drops the panel title and any
    stray band that is not part of the list."""
    if len(centres) < MIN_ROWS:
        return []
    gaps = np.diff(centres)
    pitch = float(np.median(gaps))
    if pitch <= 0:
        return []

    best, cur = [], [0]
    for i, g in enumerate(gaps):
        if abs(g - pitch) <= ROW_SPACING_TOL:
            cur.append(i + 1)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [i + 1]
    if len(cur) > len(best):
        best = cur
    return [centres[i] for i in best] if len(best) >= MIN_ROWS else []



def find_menu(img, verbose=True):
    """Locate every clickable category row in a collapsed spawner panel.

    Returns {col_index: [MenuItem, ...]}; empty if the panel is not open.

    THE FALLBACK, not the driving path. control/spawner.py clicks
    category_point() and only comes here when a measured coordinate has
    already failed -- see the measured-layout block above for what recognition
    costs when the player is facing the wrong way. Its other job is
    tools/scrape_spawner.py, which is how the constants get re-measured after
    a game update.

    Columns are discovered from the image and, failing that, taken from the
    fixed layout — see COLUMN_SPANS for why that fallback is not a hack.
    Rows are always discovered, because rows are content and content moves.
    """
    mask = bright_mask(img)
    colsum = mask[PANEL_Y0:PANEL_Y1].sum(axis=0)
    columns = segments(colsum, MIN_COL_PIX, min_len=40, gap=COL_GAP)
    # One segment spanning most of the band is not a column, it is the
    # segmentation having failed: the panel is translucent, and with bright
    # terrain inside PANEL_Y0..PANEL_Y1 every x carries a few pixels over
    # TEXT_THRESH, so MIN_COL_PIX=3 out of 1200 rows is met everywhere and the
    # gaps disappear. Facing open sand it merged into a single (522, 2700).
    widest = max((b - a for a, b in columns), default=0)
    if not columns or widest > MERGED_COL_W:
        if verbose:
            print(f'  columns did not separate ({columns}) — falling back to '
                  f'the fixed panel layout')
        columns = list(COLUMN_SPANS)

    found = {}
    col_i = 0
    for x0, x1 in columns:
        sub = mask[:, x0:x1 + 1]
        bands = segments(sub.sum(axis=1), MIN_ROW_PIX,
                         min_len=ROW_H_MIN, max_len=ROW_H_MAX)
        centres = [(a + b) // 2 for a, b in bands]
        keep = set(_regular_run(centres))
        rows = [(a, b) for (a, b), c in zip(bands, centres) if c in keep]
        if len(rows) < MIN_ROWS:
            if verbose:
                print(f'  x {x0:4d}..{x1:4d}  {len(bands):2d} bands -> '
                      f'rejected (not an evenly spaced list)')
            continue

        col_i += 1
        items = []
        for i, (a, b) in enumerate(rows, 1):
            band = mask[a:b + 1, x0:x1 + 1]
            xs = np.where(band.any(axis=0))[0]
            if len(xs) == 0:
                continue
            items.append(MenuItem(col_i, i, (a + b) // 2,
                                  x0 + int(xs[0]), x0 + int(xs[-1]), a, b))
        # Every row's chevron sits at the same x; take the median so one row
        # bleeding left (a hover highlight, a stray bright pixel) cannot drag
        # the whole column's click point with it.
        if items:
            med = int(np.median([it.x0 for it in items]))
            for it in items:
                it.set_column_left(med)
        found[col_i] = items
        if verbose:
            pitch = np.median(np.diff([it.y for it in items])) if len(items) > 1 else 0
            print(f'  x {x0:4d}..{x1:4d}  column {col_i}: {len(items)} rows, '
                  f'pitch {pitch:.1f}px, y {items[0].y}..{items[-1].y}')
    return found


def column_boxes(menu, default_pitch=500, left_pad=BOX_LEFT_PAD, right_pad=45):
    """x range of each column's panel box, {col: (x0, x1)}.

    Only the inside of a box is dimmed by the overlay; the gaps between boxes
    show the live scene, which flickers across the text threshold and swamps
    any diff taken over the whole panel. Columns are evenly pitched, so the
    box width follows from the spacing of the column text.
    """
    starts = {c: int(np.median([it.x0 for it in items]))
              for c, items in menu.items() if items}
    if not starts:
        return {}
    xs = sorted(starts.values())
    pitch = float(np.median(np.diff(xs))) if len(xs) > 1 else default_pitch
    return {c: (int(s - left_pad), int(s + pitch - right_pad))
            for c, s in starts.items()}


def find_submenu_items(img, box):
    """Entries of the expanded submenu inside one column box, top to bottom.

    Returns [{'y','y0','y1','x0','x1','click_x'}, ...]. Centring is what
    separates an entry from a category header: headers hang off the left edge
    behind their chevron, entries are centred in the tile.
    """
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    x0, x1 = box
    sub = (g[:, x0:x1] > SUBMENU_THRESH).astype(np.uint8)
    centre = (x1 - x0) // 2

    out = []
    for a, b in segments(sub.sum(axis=1), MIN_ROW_PIX, min_len=ROW_H_MIN,
                         max_len=ROW_H_MAX, gap=SUBMENU_ROW_GAP):
        xs = np.where(sub[a:b + 1].any(axis=0))[0]
        if len(xs) == 0:
            continue
        lo, hi = int(xs[0]), int(xs[-1])
        if abs((lo + hi) // 2 - centre) > SUBMENU_CENTRE_TOL:
            continue           # a category header, not an entry
        out.append({'y': (a + b) // 2, 'y0': a, 'y1': b,
                    'x0': x0 + lo, 'x1': x0 + hi,
                    'click_x': x0 + centre})
    return out


def expansions(img, menu, boxes):
    """Which categories are expanded, read from this frame alone.

    -> [(col, row, entries), ...], normally empty or one long.

    `menu` and `boxes` must come from a COLLAPSED reading (find_menu /
    column_boxes). That is not a limitation in practice: the category rows do
    not move when a submenu opens, so one collapsed reading holds for the rest
    of the session. Re-reading find_menu on an expanded panel is what does not
    work -- the centred submenu rows join the category bands and the
    even-spacing filter then keeps whichever run is longest, which measured
    anywhere from 3 to 14 rows on a column that has 10.

    Two independent signals have to agree, which is why this does not
    false-positive on a collapsed panel:
      1. the column shows centred rows at all (categories hang off the left
         behind their chevron, entries are centred -- find_submenu_items)
      2. the first of them sits SUBMENU_OFFSET below one of the known
         category rows
    """
    out = []
    for col, box in sorted(boxes.items()):
        entries = find_submenu_items(img, box)
        if not entries:
            continue
        top = entries[0]['y0']
        best, best_d = None, None
        for i, it in enumerate(menu.get(col, []), 1):
            d = abs((top - it.y) - SUBMENU_OFFSET)
            if best_d is None or d < best_d:
                best, best_d = i, d
        # Centred rows that line up with no category are not a submenu. Left
        # unmatched deliberately: guessing the nearest row would turn a
        # misread into a click on the wrong category.
        if best is not None and best_d <= SUBMENU_OFFSET_TOL:
            out.append((col, best, entries))
    return out


def column_diff(mask_a, mask_b, box):
    """Text pixels that differ inside one column box."""
    x0, x1 = box
    return int((mask_a[:, x0:x1] ^ mask_b[:, x0:x1]).sum())


def changed_rows(mask_a, mask_b, box, min_px=3):
    """y range over which a column box changed, or None."""
    x0, x1 = box
    rows = (mask_a[:, x0:x1] ^ mask_b[:, x0:x1]).sum(axis=1)
    ys = np.where(rows > min_px)[0]
    if len(ys) == 0:
        return None
    return int(ys.min()), int(ys.max())


def annotate(img, menu):
    vis = img.copy()
    for col, items in menu.items():
        for it in items:
            cv2.rectangle(vis, (it.x0 - 4, it.y0 - 4), (it.x1 + 4, it.y1 + 4),
                          (0, 200, 255), 1)
            cv2.circle(vis, (it.click_x, it.y), 5, (0, 0, 255), -1)
            cv2.putText(vis, it.key, (it.x1 + 12, it.y + 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    return vis
