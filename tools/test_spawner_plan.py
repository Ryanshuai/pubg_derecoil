"""The spawner's declarative surface, offline. No panel, no frame, no game.

    pixi run spawner-plan

A caller says `give_many(['m416', 'comp_ar', 'red_dot'])`. Everything between
that and the pixels clicked is arithmetic over MEASURED CONSTANTS -- category
rows, column boxes, the submenu entry grid -- so the whole path is checkable
with nothing running. That is the point of making them constants: the old
version worked the coordinates out by clustering bright pixels on a live
frame, which meant the only way to know where a run would click was to run it,
facing the right way, with the game up.

What is deliberately NOT here: whether the panel is open, which category is
expanded. Those are screen state, they are read fresh every time, and
`pixi run panel-state` checks them against 44 ground-truthed frames.

Every check below names the specific way it can go wrong.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from control.spawner import (CATEGORY_OF_CLASS, CATEGORY_OF_SLOT, GEAR,
                             builtin_layout, click_plan, plan, position_of,
                             weapon_position)
from detector.attachment_catalog import ATTACHMENTS, ROSTER
from detector.spawner_layout import (BOX_LEFT_PAD, CATEGORY_Y, CLICK_X_OFFSET,
                                     COLUMN_BOX, COLUMN_CLICK_DX, COLUMN_ROWS,
                                     SUBMENU_CLICK_DX, SUBMENU_ENTRY_DY,
                                     SUBMENU_ENTRY_PITCH, category_point,
                                     entry_point, known_layout)

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {name:<52} {got!r}'
          + ('' if ok else f'  != {want!r}'))
    if not ok:
        FAILS.append(name)


def acts(script):
    return [(m['act'], m['key']) for m in script]


def points(script, act):
    return [m['xy'] for m in script if m['act'] == act]


# ══════════════════════════════════════════════════════════════════════
print('\n=== the constants still ARE the measurement ===')
# docs/spawner/layout.json is what tools/scrape_spawner.py wrote from the two
# capture runs (20260801_205423 and 20260801_210656, which agreed to the
# pixel). The constants in detector/spawner_layout.py are a hand-copy of it,
# and a hand-copy is exactly the thing that silently rots: one mistyped y and
# every click in that column lands on the row above.
LAYOUT_JSON = os.path.join(ROOT, 'docs', 'spawner', 'layout.json')
if os.path.exists(LAYOUT_JSON):
    with open(LAYOUT_JSON, encoding='utf-8') as f:
        recorded = json.load(f)
    for c, entries in sorted(recorded['categories'].items()):
        col = int(c)
        check(f'col{col}: same number of rows as the scrape',
              COLUMN_ROWS.get(col), len(entries))
        for e in entries:
            check(f'col{col}_row{e["row"]:02d} click point',
                  category_point(col, e['row']), (e['click_x'], e['y']))
    for c, box in sorted(recorded['boxes'].items()):
        check(f'col{c} box', COLUMN_BOX.get(int(c)), tuple(box))
else:
    print(f'  SKIP  {LAYOUT_JSON} is gone — provenance unverifiable')

print('\n=== the rows are one evenly pitched ruler, not ten numbers ===')
# 305 + (384/9)*(n-1). A typo shows up as a row that is off the ruler; a whole
# column shifted by a game patch shows up as every row being off it. Neither
# is visible by eye in a list of ten integers.
for n, y in enumerate(CATEGORY_Y, 1):
    check(f'row {n:2d} sits on the 42.67 px pitch',
          y, round(305 + (384 / 9) * (n - 1)))
check('all three columns share the same rows',
      len({tuple(sorted(e['y'] for e in rows))
           for rows in known_layout()[0].values()
           if len(rows) == max(COLUMN_ROWS.values())}), 1)

print('\n=== the constant path and the fallback click the same pixel ===')
# find_menu() is the fallback when a measured coordinate fails (goto() reaches
# for it once). If the two ever disagreed about where a row's click point is,
# the fallback would "recover" by clicking somewhere else and the run would
# carry on spawning the wrong things. column_boxes() pads BOX_LEFT_PAD left of
# the chevron; find_menu() puts the click CLICK_X_OFFSET right of it.
check('COLUMN_CLICK_DX == box pad + chevron offset',
      COLUMN_CLICK_DX, BOX_LEFT_PAD + CLICK_X_OFFSET)
check('which is the 65 px measured off the panel', COLUMN_CLICK_DX, 65)

print('\n=== every catalogued category is a row that exists ===')
# CATEGORY_OF_CLASS / CATEGORY_OF_SLOT are hand-written (col, row) pairs. A
# pair pointing past the end of a column used to raise IndexError halfway
# through a run, with the panel plainly on screen.
for label, cat in list(CATEGORY_OF_CLASS.items()) + list(
        CATEGORY_OF_SLOT.items()):
    col, row = cat
    ok = col in COLUMN_ROWS and 1 <= row <= COLUMN_ROWS[col]
    check(f'{label:<9} col{col}_row{row:02d} is on the panel', ok, True)
for key, g in GEAR.items():
    col, row = g['category']
    check(f'{key} col{col}_row{row:02d} is on the panel',
          col in COLUMN_ROWS and 1 <= row <= COLUMN_ROWS[col], True)

print('\n=== a row off the end is refused, not clicked ===')
# Silently clamping would click the last row instead: a request for a category
# that does not exist would spawn from the one that does.
for name, args in [('column 4 (SENSITIVITY, not a list)', (4, 1)),
                   ('col2 has 5 rows, not 6', (2, 6)),
                   ('row 0 is not 1-based', (1, 0))]:
    try:
        got = category_point(*args)
    except (KeyError, IndexError) as e:
        got = type(e).__name__
    ok = got in ('KeyError', 'IndexError')
    print(f'  {"ok  " if ok else "FAIL"}  {name:<52} {got!r}')
    if not ok:
        FAILS.append(name)

# ══════════════════════════════════════════════════════════════════════
print('\n=== the gear coordinates, which were measured directly ===')
# These three (x, y) pairs were read off a screenshot and hard-coded in
# control/spawner.py long before the row table existed. They are now DERIVED
# from it, so they are the strongest tie between the arithmetic and a real
# measurement: if the derivation is wrong, these move.
check('backpack category (was 1594, 518)', category_point(3, 6), (1594, 518))
for key, want in [('backpack1', (1766, 563)), ('backpack2', (1766, 613)),
                  ('backpack3', (1766, 664))]:
    g = GEAR[key]
    got = entry_point(COLUMN_BOX[3], category_point(*g['category'])[1],
                      g['index'])
    ok = got[0] == want[0] and abs(got[1] - want[1]) <= 1
    print(f'  {"ok  " if ok else "FAIL"}  {key + " entry (was " + str(want) + ")":<52} '
          f'{got!r}')
    if not ok:
        FAILS.append(f'{key} entry point')

print('\n=== an entry click lands inside its own column ===')
# 237 px in from the box's left edge. Get the box or the offset wrong and the
# click lands in the NEXT column -- which is not an error, it is a spawn from
# whatever category happens to be open over there.
for col, (x0, x1) in sorted(COLUMN_BOX.items()):
    x = entry_point((x0, x1), CATEGORY_Y[0], 1)[0]
    check(f'col{col} entry x inside {x0}..{x1}', x0 < x < x1, True)
    check(f'col{col} entry x', x, x0 + SUBMENU_CLICK_DX)

print('\n=== the entry grid is the measured one ===')
# 44.25 to the first entry, 50.70 pitch, sd < 0.5 px over 246 entries.
cy = category_point(2, 1)[1]
check('first entry', entry_point(COLUMN_BOX[2], cy, 1)[1],
      round(cy + SUBMENU_ENTRY_DY))
check('third entry', entry_point(COLUMN_BOX[2], cy, 3)[1],
      round(cy + SUBMENU_ENTRY_DY + 2 * SUBMENU_ENTRY_PITCH))
check('entries never collide with the next category row',
      entry_point(COLUMN_BOX[2], cy, 1)[1] > CATEGORY_Y[1], True)

# ══════════════════════════════════════════════════════════════════════
print('\n=== plan(): gear first, because gear is driven blind ===')
# give_gear clicks its category without recognising it, so the only state its
# coordinates are valid in is the root. Ordered anywhere but first, it fires
# with another submenu open and clicks whatever that submenu drew there. And
# without a backpack an attachment has nowhere to spawn to -- a harvest run
# fitted a suppressor while being told to fit a compensator because of it.
steps = plan(['comp_ar', 'backpack3', 'm416'])
check('gear leads', [s['kind'] for s in steps][0], 'gear')

print('\n=== plan(): same column BOTTOM ROW FIRST ===')
# A submenu pushes every category BELOW it down, measured at
# docs/spawner/live_two_open.png: 7 open entries moved MAGAZINE from y=349 to
# y=709. Going bottom-up, the next category is always ABOVE what is open and
# its measured y is still valid. Top-down, the second click lands on a submenu
# entry -- which does not miss, it SPAWNS SOMETHING NOBODY ASKED FOR, and it
# looks like the click simply did not register.
rows = [s['category'][1] for s in plan(['vert_grip', 'comp_ar', 'red_dot'])
        if s['category'][0] == 2]
check('col2 rows descend', rows, sorted(rows, reverse=True))
check('and it really is more than one row', len(set(rows)) > 1, True)

print('\n=== plan(): the same category is visited once ===')
# The whole point of give_many over a loop of give(): three muzzles in one
# category cost ONE category click, not three. Interleaving them with another
# category must not undo that -- plan() sorts them back together.
script = click_plan(plan(['comp_ar', 'vert_grip', 'supp_ar', 'flash_ar']))
check('four items, two categories, two opens',
      [m['act'] for m in script].count('open'), 2)
check('and each is opened exactly once',
      len({m['category'] for m in script if m['act'] == 'open'}), 2)
# Consecutive, and in submenu order within the category -- the caller's order
# is not preserved and is not meant to be.
check('the three muzzles are consecutive, top-down',
      [m['key'] for m in script if m['act'] == 'entry'],
      ['comp_ar', 'flash_ar', 'supp_ar', 'vert_grip'])

print('\n=== duplicates repeat the ENTRY click, not the category ===')
script = click_plan(plan(['comp_ar', 'comp_ar']))
# One category run, so it is also the last one, so no close — see the shape
# check below for why the trailing one went.
check('acts', [m['act'] for m in script], ['open', 'entry', 'entry'])
check('both entry clicks are the same point',
      len(set(points(script, 'entry'))), 1)

print('\n=== weapon_times=2 buys a second click, and only for weapons ===')
# It converges the rack on slot 2 from any starting state, at the cost of a
# gun on the floor. Not the default -- see docs/spawner/README.md section 5.
one = click_plan(plan(['m416', 'comp_ar']))
two = click_plan(plan(['m416', 'comp_ar'], weapon_times=2))
check('one extra entry click in total',
      len([m for m in two if m['act'] == 'entry'])
      - len([m for m in one if m['act'] == 'entry']), 1)
check('the attachment is untouched',
      len([m for m in two if m['act'] == 'entry' and m['key'] == 'comp_ar']), 1)

# ══════════════════════════════════════════════════════════════════════
print('\n=== the click script for a real shopping list ===')
# The end-to-end claim: keys in, measured pixels out, nothing consulted.
script = click_plan(plan(['m416', 'comp_ar', 'backpack3']))
# The LAST category is not closed: give_many closes the whole panel next,
# which resets the expansion by itself (measured -- expand a category, press
# comma twice, it comes back `all collapsed`). The click that used to be here
# was a LEFT click at a panel coordinate sent after the panel could already be
# gone, and a left click with no panel under it fires the weapon in hand.
# Between categories it stays: several open submenus run off the bottom of the
# panel, and gear's blind coordinates are only valid from the root.
check('shape', acts(script),
      [('root', 'backpack3'), ('open', 'backpack3'), ('entry', 'backpack3'),
       ('close', 'backpack3'),
       ('open', 'm416'), ('entry', 'm416'), ('close', 'm416'),
       ('open', 'comp_ar'), ('entry', 'comp_ar')])
check('gear is the only blind move',
      {m['key'] for m in script if m['blind']}, {'backpack3'})

by_key = {}
for m in script:
    by_key.setdefault(m['key'], []).append(m)
for key, want_cat in [('backpack3', (3, 6)), ('comp_ar', CATEGORY_OF_SLOT['muzzle']),
                      ('m416', CATEGORY_OF_CLASS['AR'])]:
    ms = by_key[key]
    check(f'{key} category', ms[0]['category'], want_cat)
    check(f'{key} opens at the measured row point',
          [m['xy'] for m in ms if m['act'] == 'open'],
          [category_point(*want_cat)])
    idx = (GEAR[key]['index'] if key in GEAR else position_of(key)[1])
    check(f'{key} entry #{idx} at the measured grid point',
          [m['xy'] for m in ms if m['act'] == 'entry'],
          [entry_point(COLUMN_BOX[want_cat[0]],
                       category_point(*want_cat)[1], idx)])
    # comp_ar is the last run in this script, so it has no close at all.
    check(f'{key} closes where it opened, when it closes',
          [m['xy'] for m in ms if m['act'] == 'close'],
          [] if key == 'comp_ar' else [category_point(*want_cat)])

print('\n=== every click of every catalogued key is on the panel ===')
# A blanket sweep, because an index that runs off the end of a submenu does
# not raise: it clicks empty panel, or the row below the list, and the run
# reports ok=True having spawned nothing. The panel body ends around y=1350.
live = (list(ROSTER) + list(ATTACHMENTS) + list(GEAR))
bad = []
for k in live:
    for m in click_plan(plan([k])):
        if m['xy'] is None:
            continue
        x, y = m['xy']
        col = m['category'][0]
        if not (COLUMN_BOX[col][0] < x < COLUMN_BOX[col][1]
                and 150 < y < 1350):
            bad.append((k, m['act'], m['xy']))
check(f'{len(live)} keys, every click inside the panel', bad, [])

print('\n=== the catalogue indices agree with the plan ===')
# position_of() is what turns a key into an entry number; click_plan turns
# that number into a y. They must be reading the same catalogue.
for k in ('m416', 'comp_ar'):
    cat, idx, expect = position_of(k)
    step = plan([k])[0]
    check(f'{k} index', (step['category'], step['index'], step['expect']),
          (cat, idx, expect))
# ROSTER IS THE SPAWNER'S ROW ORDER, so a name the game no longer offers is
# not a harmless extra entry: weapon_position() numbers a gun by its index
# among its classmates, and a dead name shifts every gun below it by one row.
# The click then lands on its NEIGHBOUR and the run measures the wrong weapon
# while reporting ok.
#
# This used to be enforced by a VAULTED set and an is_live() filter. Both are
# gone as of 2026-08-04 — the six removed weapons were deleted outright rather
# than flagged — so the invariant is now simply that ROSTER holds nothing the
# spawner cannot produce, and the two ways to break it are checked here.
try:
    weapon_position('dp28')          # deleted from ROSTER in the U42.1 sweep
    got = 'planned it anyway'
except KeyError:
    got = 'refused'
check('a weapon not in ROSTER is refused, not indexed', got, 'refused')

# The other direction, which no lint can see: the counts here are transcribed
# from the spawner's own submenus (docs/spawner/runs/), so a name added to
# ROSTER without one being added to the game shows up as a length that no
# longer matches. Update these ONLY against a fresh screenshot.
SPAWNER_ROWS = {'AR': 13, 'SR': 5, 'DMR': 7, 'SG': 5, 'SMG': 8, 'LMG': 2}
for cls, n in sorted(SPAWNER_ROWS.items()):
    have = [k for k, v in ROSTER.items() if v[0] == cls]
    check(f'{cls} has {n} rows in the spawner', len(have), n)

print('\n=== an unknown key is a KeyError, not a click ===')
for name, k in [('never heard of it', 'ak47_but_wrong'), ('empty', '')]:
    try:
        plan([k])
        got = 'planned it anyway'
    except KeyError:
        got = 'KeyError'
    ok = got == 'KeyError'
    print(f'  {"ok  " if ok else "FAIL"}  {name:<52} {got!r}')
    if not ok:
        FAILS.append(name)

print('\n=== nothing asked for, nothing clicked ===')
check('empty list', click_plan(plan([])), [])

# ══════════════════════════════════════════════════════════════════════
print('\n=== the layout loads with no file and no screen ===')
# It used to come from docs/spawner/layout.json, and a missing file left
# self.menu None -- every give_* answered "not synced" with the panel plainly
# on screen. builtin_layout() has nothing to fail on.
menu, boxes = builtin_layout()
check('columns', sorted(menu), sorted(COLUMN_ROWS))
check('rows per column', {c: len(v) for c, v in menu.items()}, dict(COLUMN_ROWS))
check('boxes', boxes, dict(COLUMN_BOX))
check('a Category clicks the measured point',
      (menu[2][2].click_x, menu[2][2].y), category_point(2, 3))
check('and knows its own key', menu[2][2].key, 'col2_row03')

print('\n=== goto()\'s path log: the split, not the count ===')
# The log accumulates on real runs and gets read once, months later, to settle
# whether the menu is an accordion. If the classifier is wrong then, the data
# is already spent -- every transition it mislabels was a real click nobody is
# going to make again. So the buckets are pinned here, offline, now.

def classify(r):
    """-> the bucket this transition belongs in.

    THE BUCKETS DESCRIBE WHAT WAS OBSERVED, NOT WHY. They used to be named
    'ACCORDION' and 'MULTI-OPEN', on the assumption that the menu had to be
    one or the other. It is neither, and the first real sample said so: the
    panel keeps BOTH columns open at once, and what a transition costs
    depends on DIRECTION, not on any accordion discipline. A bucket named
    after a mechanism would have gone on asserting the wrong one while the
    counts underneath it were perfectly good.

    So: same-column-up, same-column-down and cross-column are separated,
    because that is the split the measurements actually landed on.

    ⚠ THIS LIVED IN A REPORTING SCRIPT UNTIL 2026-08-08, and that script was
    the one thing in tools/ another script imported. The report around it
    was deleted because its `main()` returned 0 unconditionally: it
    had a pixi task, it ran, and it could not fail. Mutation-testing every
    gate that day turned up exactly one that was green by construction, and
    it was that one. The classifier is real, so it moved to its only caller
    — the thing that pins it.
    """
    if not r.get('ok'):
        return 'failed'
    src, dst = r.get('from'), r.get('to')
    if r.get('path') == 'already':
        return 'already open — 0 clicks'
    if src is None:
        return 'from a collapsed panel'          # uninformative by construction
    if list(src) == list(dst or []):
        return 'already open — 0 clicks'
    same_col = dst and src[0] == dst[0]
    if not same_col:
        where = 'cross-column'
    elif src[1] < dst[1]:
        # The open one is ABOVE the target, so its submenu pushes the target's
        # row ~360 px down out from under the measured coordinate. Geometry.
        where = 'same column, DOWN (open one is above)'
    else:
        where = 'same column, UP'
    return f'{where}: {r.get("path")} ({r.get("clicks")} clicks)'



CASES = [
    # A one-click transition from a COLLAPSED panel is not evidence. There was
    # nothing to close. This is most of the traffic.
    ({'to': [1, 1], 'from': None, 'path': 'direct', 'ok': True},
     'from a collapsed panel'),
    # THE MEASURED FACT (2026-08-03): the panel keeps ONE expansion per column
    # and holds them across columns. So asking for a node that is already open
    # is free, and that is the multi-open payoff rather than an anomaly.
    ({'to': [2, 1], 'from': [2, 1], 'path': 'already', 'ok': True},
     'already open — 0 clicks'),
    # Same column, target ABOVE the open one: nothing is in the way, 1 click.
    ({'to': [2, 1], 'from': [2, 5], 'path': 'direct', 'clicks': 1, 'ok': True},
     'same column, UP: direct (1 clicks)'),
    # Same column, target BELOW the open one: its submenu pushes the target's
    # row ~360 px down out from under the measured coordinate. Geometry, and
    # it costs 2-3 clicks.
    ({'to': [2, 4], 'from': [2, 1], 'path': 'closed-blocker', 'clicks': 2,
      'ok': True},
     'same column, DOWN (open one is above): closed-blocker (2 clicks)'),
    ({'to': [2, 5], 'from': [2, 1], 'path': 'via-root', 'clicks': 3,
      'ok': True},
     'same column, DOWN (open one is above): via-root (3 clicks)'),
    ({'to': [2, 1], 'from': [1, 1], 'path': 'direct', 'clicks': 1, 'ok': True},
     'cross-column: direct (1 clicks)'),
    # A transition that failed says nothing about the menu, and letting it
    # into the means would quietly move them.
    ({'to': [1, 5], 'from': [2, 3], 'path': 'failed', 'ok': False}, 'failed'),
]
for row, want in CASES:
    check(f'{row["path"]:14} from {row["from"]}', classify(row), want)

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS[:8])}'
          + (' ...' if len(FAILS) > 8 else ''))
    sys.exit(1)
print('all ok')
