"""Hand another module a weapon or an attachment, via the training range's
item spawner.

    from control.spawner import SpawnerControl
    with SpawnerControl() as sc:
        sc.give_many(['m416', 'comp_ar', 'red_dot', 'backpack3'])

**Say what you want. Nothing outside this file needs a column, a row, an entry
index or a screenshot.** That is the whole interface:

    give_many(keys)      everything on the list, in the fewest panel moves
    give(key)            one thing, dispatched on the key
    give_weapon / give_attachment / give_gear    if the kind is already known
    plan(keys)           what give_many would do, without touching the game
    ready()              is the panel on screen

Underneath, the panel is driven from MEASURED CONSTANTS -- category rows,
column boxes and the submenu entry grid all live in
detector/spawner_layout.py, taken off two capture runs that agreed to the
pixel. Recognising the panel every time is the fallback, not the path: it
reads UI text through a translucent overlay, so facing bright terrain the
weapons column simply does not exist and a run dies with the panel plainly on
screen. See docs/spawner/README.md section 2b.

Constants for the GEOMETRY, never for the STATE. Whether the panel is up and
which category is expanded is read fresh every time -- the game drops
connections, pops dialogs, and another agent shares this window. read()
answers both from ONE frame with no baseline to compare against, which is what
lets a sequence move sideways between categories instead of collapsing back to
the root between every item. Measurements in docs/spawner/README.md section
3b; `pixi run panel-state` checks it against 44 ground-truthed frames offline,
and `pixi run spawner-plan` checks the coordinate arithmetic with no frames at
all.

There is still a rescue surface for probes that must drive the panel by hand
(click_category / click_entry / read / goto / collapse_all / sync / spawn).
It is at the bottom of the class, and it is not the normal path.

Weapons and attachments are the same click through a different category, but
they land differently: a weapon goes into a weapon slot and can evict what was
there, an attachment always goes into the backpack.

**One click per item asked for, weapons included.** Where a gun ends up is the
game's rule, not this module's promise: an empty rack takes it into slot 1,
anything else puts it in slot 2 and evicts what was there onto the ground.

A caller that needs the gun in a PARTICULAR slot decides how to get it there —
read the rack back, or ask for a second click. That policy does not belong in
a default here, because it is not free: this used to click every weapon twice
so that empty / full / half-full all converged on slot 2, and since a full
rack is the normal case, the second click mostly just evicted the copy the
first one had placed, one gun on the floor per spawn.

The panel is a menu on the comma key, available anywhere in the training
range — there is no spawner object to walk up to. ensure_panel() presses it
and polls, because comma is a toggle and pressing it blind lands in the wrong
state half the time. sync() still fails loudly if the panel is not on screen.
"""
import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from detector.attachment_catalog import ROSTER, ATTACHMENTS, is_live
from detector.spawner_detector import SpawnerDetector
from detector.cropper import capture_screen
from detector.spawner_layout import (CHANGE_MIN, COLUMN_ROWS, LAYOUT_SCREEN,
                                     PARK_XY, bright_mask, column_boxes,
                                     entry_point, expansions, find_menu,
                                     find_submenu_items, known_layout)
from press.pico_mouse import HID_KEY_2, HID_KEY_COMMA
from press.pointer import Pointer, move_cursor
from control.focus import game_focused, ensure_focus

# The submenu's slide-open is watched, not waited out. Every screen in this
# repo that waits a fixed time for the game is wrong for the same reason --
# the duration is not a constant -- and this one is on the critical path of
# every calibration run, which reaches the spawner through here.
OPEN_TIMEOUT = 1.2     # give-up, not expected duration
OPEN_POLL = 0.03
OPEN_WAIT = 0.45       # only for L0 clicks, which verify nothing by definition
CLOSE_WAIT = 0.40


def shoot_parked(settle=0.10):
    """Screenshot with the cursor off the panel, so no row is hover-lit."""
    move_cursor(PARK_XY)
    time.sleep(settle)
    return capture_screen()


# ── goto()'s path log ────────────────────────────────────────────────────
# Is this menu an accordion (opening one category closes the last) or does it
# let several stand open? Nobody knows: every capture in docs/spawner/runs/
# expands from a collapsed panel, so the question has never been posed to the
# game. The answer decides whether a same-column category switch costs 1 click
# or 2, which is docs/refactor_plan.md section 2's whole cost table.
#
# WRITTEN BY EVERY goto(), ON BY DEFAULT. One appended line per transition, no
# caller does anything.
#
# WHAT IT DOES NOT COVER, stated because the first version of this comment
# claimed otherwise: give_many() -- the primary path, and the one every
# calibration run takes -- does NOT go through goto(). It expands through
# spawn()/_click_await, which has no `path` to record. So the sample here is
# the RESCUE surface plus deliberate probing, not the panel's whole traffic.
# The measurement it was built for is done anyway (see docs/game_quirks.md,
# and the answer turned out to be neither of the two candidates); what remains
# is a tripwire for that answer changing, and a tripwire on one of two paths is
# worth what it is worth.
#
# Cheap enough to leave on: one open-append-close of ~120 bytes against a
# transition that costs a click, a screenshot and a poll. Failures are
# swallowed -- a full disk must not take down a harvest run over telemetry.
GOTO_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'docs', 'spawner', 'goto_paths.jsonl')


def record_goto(rec, col, row, path=None):
    """Append one transition. Never raises."""
    try:
        os.makedirs(os.path.dirname(path or GOTO_LOG), exist_ok=True)
        with open(path or GOTO_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                'to': [col, row],
                'from': list(rec['from']) if rec.get('from') else None,
                'path': rec.get('path'),
                'clicks': rec.get('clicks'),
                'ok': bool(rec.get('ok')),
            }, ensure_ascii=False) + '\n')
    except Exception:
        pass

# Which category row each weapon class lives in, as (column, row) into the
# collapsed layout. Verified against the captured run: the per-class counts in
# ROSTER match what each category expands to (AR 13, DMR 7, SMG 8, LMG 2).
CATEGORY_OF_CLASS = {
    'AR':  (1, 1),    # 突击步枪
    'SR':  (1, 2),    # 狙击步枪 — Kar98k, M24, AWM, Win94, Lynx AMR
    'DMR': (1, 3),    # 射手步枪
    'SG':  (1, 4),    # 霰弹枪 — S686, S12K, S1897, DBS, O12
    'SMG': (1, 5),    # 冲锋枪
    'LMG': (1, 10),   # 轻机枪
}
# Column 1's other rows are not weapons this project drives: row 6 手枪,
# row 7 可投掷物品, row 8 近战, row 9 其他. Labels read off
# docs/spawner/runs/20260801_210656/col1_row*_label.png.

# Same for attachments. Verified entry by entry against the captured submenus:
# 弹匣 / 枪口 / 枪托 / 瞄准镜 match ATTACHMENTS' order exactly.
CATEGORY_OF_SLOT = {
    'grip':     (2, 1),   # 握把
    'magazine': (2, 2),   # 弹匣
    'muzzle':   (2, 3),   # 枪口
    'stock':    (2, 4),   # 枪托
    'scope':    (2, 5),   # 瞄准镜
}

# 握把 does not match: the spawner lists 箭袋 (十字弩) — the crossbow quiver —
# between 垂直握把 and 斜向握把, and ATTACHMENTS has no key for it because
# nothing on a firearm equips it. Everything after it in that category is
# therefore one position further down than the catalogue implies. Spliced in
# here rather than patched into the catalogue, which is about what fits on
# what, not about menu order.
SPAWNER_EXTRAS = {
    'grip': {6: '箭袋 (十字弩)'},   # 1-based position in the spawner's list
}

PANEL_WATCH_S = 3.0    # comma -> panel drawn; generous, it is a full screen
PANEL_SETTLE_S = 0.5
# Measured, not guessed, and it is the one wait here that could NOT come down.
# tools/probe_spawn_wait.py spawns N copies and then counts what reached the
# backpack, because clicking faster than the game accepts raises nothing --
# the click is eaten and the run reports ok=True having spawned less than it
# asked for. 0.30 delivered 5/5; 0.15 delivered 2/5 with ok=True.
SPAWN_WAIT = 0.30     # after clicking an entry, before the next click
SWITCH_WAIT = 0.35    # after pressing 2

# ── Gear (column 3) ──────────────────────────────────────────────────────
# Column 3 is 汽油桶 / 能量物品 / 治疗物品 / 头盔 / 防弹衣 / 背包. Only the
# backpack is modelled, because only the backpack is a PREREQUISITE: an
# attachment spawns into the backpack, so with no backpack there is nowhere
# for one to go.
#
# That failure is not clean. The parts do not refuse to spawn; they go
# somewhere else, the inventory rows shift under the drag targets, and every
# kitting step afterwards reads back a part nobody asked for -- a harvest run
# fitted a suppressor while being told to fit a compensator, then skipped
# seven configs in a row. Spawn a level 3 backpack before anything else and
# none of it happens.
#
# Positions, not coordinates. These used to be three hand-measured (x, y)
# pairs, because find_menu() could not be trusted to see column 3 at all --
# facing a red banner, ITEMS and SENSITIVITY merged into one x band whose rows
# are not evenly pitched, the band was rejected as "not a list", and the
# backpack column stopped existing four re-reads running. That is still true of
# recognition, and it is why this category is driven blind. But the coordinates
# now come from the same measured table as everything else
# (detector/spawner_layout.py), so there is one set of numbers to keep correct
# instead of two. `pixi run spawner-plan` asserts the derived points reproduce
# the three originally measured ones: (1594,518) -> (1766, 563/613/664).
GEAR_BOX_Y = (280, 720)      # y band of the ITEMS column, for the change check
GEAR = {
    'backpack1': {'zh': '背包 (1级)', 'category': (3, 6), 'index': 1},
    'backpack2': {'zh': '背包 (2级)', 'category': (3, 6), 'index': 2},
    'backpack3': {'zh': '背包 (3级)', 'category': (3, 6), 'index': 3},
}


# The measurement's own record, written by tools/scrape_spawner.py. NOT loaded
# at runtime any more -- detector/spawner_layout.py carries the same numbers as
# constants, and `pixi run spawner-plan` asserts the two still agree. Pass
# `layout=` to SpawnerControl to drive off a file instead, which is for the
# scrape tool and for a resolution this repo has not measured.
LAYOUT_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'docs', 'spawner', 'layout.json')


class Category:
    """One clickable category row, from the stored layout."""

    __slots__ = ('col', 'idx', 'y', 'click_x')

    def __init__(self, col, idx, y, click_x):
        self.col, self.idx, self.y, self.click_x = col, idx, y, click_x

    @property
    def key(self):
        return f'col{self.col}_row{self.idx:02d}'


def _as_categories(menu, boxes):
    return ({int(c): [Category(int(c), e['row'], e['y'], e['click_x'])
                      for e in entries]
             for c, entries in menu.items()},
            {int(c): tuple(b) for c, b in boxes.items()})


def builtin_layout():
    """({col: [Category]}, {col: (x0, x1)}) from the measured constants.

    THE PRIMARY PATH. No file, no frame, no game, and nothing that can fail:
    the panel's geometry was measured twice and did not move, so it is code.
    Provenance and the reasoning are in detector/spawner_layout.py's
    measured-layout block and docs/spawner/README.md section 2b.
    """
    return _as_categories(*known_layout())


def load_layout(path=LAYOUT_PATH):
    """The same table, read from a scrape file instead. -> ({col: [Category]}, boxes)

    An override, for tools/scrape_spawner.py and for a resolution nobody has
    measured yet. The normal path is builtin_layout(); a file can be missing,
    stale, or written by a run that read the panel while facing a red banner.
    """
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    return _as_categories(data['categories'], data['boxes'])


def weapon_position(key):
    """(category (col,row), 1-based index inside that category) for a weapon.

    The index is ROSTER's own ordering, which is the spawner's ordering — the
    catalogue was read straight off this menu.
    """
    entry = ROSTER.get(key)
    if entry is None:
        raise KeyError(f'unknown weapon {key!r}')
    if not is_live(key):
        raise ValueError(f'{key} is vaulted and no longer in the spawner')
    cls = entry[0]
    if cls not in CATEGORY_OF_CLASS:
        raise ValueError(f'{key} is class {cls}, which has no mapped category '
                         f'(only {", ".join(CATEGORY_OF_CLASS)} are catalogued)')
    peers = [k for k, v in ROSTER.items() if v[0] == cls and is_live(k)]
    return CATEGORY_OF_CLASS[cls], peers.index(key) + 1, len(peers)


def attachment_position(key):
    """(category (col,row), 1-based index, expected entry count) for an
    attachment, with the spawner's uncatalogued extras spliced in."""
    att = ATTACHMENTS.get(key)
    if att is None:
        raise KeyError(f'unknown attachment {key!r}')
    slot = att['slot']
    if slot not in CATEGORY_OF_SLOT:
        raise ValueError(f'{key} is in slot {slot}, which has no category')
    peers = [k for k, v in ATTACHMENTS.items() if v['slot'] == slot]
    index = peers.index(key) + 1
    extras = SPAWNER_EXTRAS.get(slot, {})
    for pos in sorted(extras):
        if index >= pos:
            index += 1
    return CATEGORY_OF_SLOT[slot], index, len(peers) + len(extras)


def position_of(key):
    """Dispatch a catalogue key to whichever table owns it."""
    if key in ROSTER:
        return weapon_position(key)
    if key in ATTACHMENTS:
        return attachment_position(key)
    if key in GEAR:
        raise ValueError(f'{key!r} is gear — its position is in GEAR, because '
                         f'it is driven blind from the root. See give_gear().')
    raise KeyError(f'{key!r} is in ROSTER, ATTACHMENTS nor GEAR')


def plan(keys, weapon_times=1):
    """Order a shopping list into the fewest panel moves. Clicks nothing.

    -> [{'key', 'kind', 'category', 'index', 'expect', 'times'}, ...]

    A pure function of the catalogue: no panel, no screenshot, no game, no
    SpawnerControl. Worth calling on its own — it says what a give_many() would
    do, checkable before anything touches the game.

    Two orderings, both of them measured rather than assumed:

    - Same category adjacent, so the panel opens it once instead of once per
      item. This is where the saving is -- every item after the first in a
      category costs one click and no category move.
    - Gear first. Gear is driven blind and needs the panel with nothing
      expanded (see GEAR), so it cannot run in the middle of a sequence.
    """
    want, order = {}, []
    for k in keys:
        if k not in want:
            order.append(k)
        want[k] = want.get(k, 0) + 1

    gear, items = [], []
    for k in order:
        if k in GEAR:
            gear.append({'key': k, 'kind': 'gear', 'category': None,
                         'index': GEAR[k]['index'], 'expect': None,
                         'times': want[k]})
            continue
        (col, row), index, expect = position_of(k)
        items.append({
            'key': k,
            'kind': 'weapon' if k in ROSTER else 'attachment',
            'category': (col, row), 'index': index, 'expect': expect,
            # One click per gun asked for. weapon_times=2 is available for a
            # caller that wants slot 2 regardless of what the rack held -- see
            # give_weapon -- but it costs an evicted gun on the floor every
            # time the rack was already full, which is most of the time, so it
            # is not the default.
            'times': want[k] * (weapon_times if k in ROSTER else 1),
        })
    # Same category adjacent; within a column, BOTTOM ROW FIRST. A submenu
    # pushes everything below it down, so visiting a column bottom-up means the
    # next category to click is always ABOVE whatever is open, and its measured
    # coordinate is still where it is drawn. Top-down would need a collapse
    # before every move -- and getting it wrong does not miss harmlessly, the
    # stale coordinate lands on a submenu entry and spawns something nobody
    # asked for. Measured on live_two_open.png: 7 entries pushed MAGAZINE from
    # y=349 to y=709. docs/spawner/README.md section 3b.
    items.sort(key=lambda s: (s['category'][0], -s['category'][1], s['index']))
    return gear + items


def _category_runs(steps):
    """plan() output as [(category, [step, ...])] — consecutive runs.

    plan() already put items sharing a category next to each other, so this is
    plain run-length grouping. Gear never joins a run: it is driven blind from
    the root, one trip each.
    """
    out = []
    for s in steps:
        cat = s['category']
        if out and cat is not None and out[-1][0] == cat:
            out[-1][1].append(s)
        else:
            out.append((cat, [s]))
    return out


def click_plan(steps, menu=None, boxes=None):
    """Turn plan() output into the exact click script give_many() will send.

    -> [{'act', 'key', 'kind', 'category', 'index', 'expect', 'blind', 'xy'}]

    `act` is one of:

        'root'    collapse everything first — gear is driven blind and the
                  only state its coordinates are valid in is the root
        'open'    click the category row
        'entry'   click a submenu entry. THIS is the click that spawns
        'close'   click the category row again, before moving on

    Called with no layout it is pure arithmetic over the measured constants at
    LAYOUT_SCREEN: no panel, no screenshot, no game, no hardware. give_many()
    passes its own table so that a recalibrated or overridden layout drives
    every click and not just some of them, but that table IS the constants on
    every normal run. `pixi run spawner-plan` therefore checks the real path
    rather than a model of it, offline.

    The entry grid is rigid — 44.25 px from a category to its first entry,
    50.70 px pitch, 237 px in from the box's left edge, sd < 0.5 px over 246
    measured entries — which is why nothing here needs a frame to be told what
    the arithmetic already knows.
    """
    if menu is None or boxes is None:
        menu, boxes = builtin_layout()

    def point(col, row):
        items = menu.get(col, [])
        if not 1 <= row <= len(items):
            raise IndexError(f'col{col}_row{row:02d} is not in this layout '
                             f'({len(items)} rows in column {col})')
        return items[row - 1].click_x, items[row - 1].y

    script = []
    runs = _category_runs(steps)
    for n, (cat, batch) in enumerate(runs, 1):
        gear = cat is None
        col, row = GEAR[batch[0]['key']]['category'] if gear else cat
        cx, cy = point(col, row)
        head = {'category': (col, row), 'blind': gear,
                'expect': batch[0]['expect']}

        def move(act, s, xy):
            return dict(head, act=act, key=s['key'], kind=s['kind'],
                        index=s['index'], xy=xy)

        if gear:
            script.append(move('root', batch[0], None))
        script.append(move('open', batch[0], (cx, cy)))
        for s in batch:
            xy = entry_point(boxes[col], cy, s['index'])
            for _ in range(s['times']):
                script.append(move('entry', s, xy))
        # Close it BEFORE MOVING ON, and only then: several open submenus
        # would run off the bottom of the panel, and gear's blind coordinates
        # are only valid from the root. After the LAST category there is
        # nothing to move on to — give_many closes the whole panel, which
        # resets the expansion anyway (measured: expand col1_row01, press
        # comma twice, the panel comes back `all collapsed`).
        #
        # The trailing click was not merely wasted. It is a LEFT CLICK at a
        # panel coordinate, and by the time it goes out the panel may already
        # be gone — a left click with no panel under it reaches the game, and
        # the game fires the weapon in hand. Two of them per spawn, which is
        # what a plates run looked like from the outside: guns arriving, then
        # two shots.
        if n < len(runs):
            script.append(move('close', batch[-1], (cx, cy)))
    return script


def check_against_run(run_dir):
    """Compare every mapped category's entry count with a captured scrape.

    Offline — no game. `spawn()` makes the same comparison live and refuses to
    click on a mismatch, but by then a run is already half done; this answers
    the same question from docs/spawner/runs/<stamp>/ before anything moves.

    It is what found 箭袋 (十字弩): 握把 expanded to 7 entries where the
    catalogue accounted for 6, which had silently shifted 斜向握把 down a slot.
    """
    base = cv2.imread(os.path.join(run_dir, '00_baseline.png'))
    if base is None:
        print(f'no 00_baseline.png in {run_dir}')
        return 1
    # Constants, not recognition: this is a count of what is INSIDE each
    # category, and the column boxes it needs are the measured ones. Reading
    # them back off the baseline made the check inherit the terrain problem it
    # exists to be independent of.
    _, boxes = known_layout()

    wanted = [(f'{cls:<9}', cat, len([k for k, v in ROSTER.items()
                                      if v[0] == cls and is_live(k)]))
              for cls, cat in CATEGORY_OF_CLASS.items()]
    wanted += [(f'{slot:<9}', cat,
                len([k for k, v in ATTACHMENTS.items() if v['slot'] == slot])
                + len(SPAWNER_EXTRAS.get(slot, {})))
               for slot, cat in CATEGORY_OF_SLOT.items()]

    bad = 0
    for label, (col, row), expect in wanted:
        shot = cv2.imread(os.path.join(run_dir, f'col{col}_row{row:02d}_open.png'))
        if shot is None:
            print(f'  {label} col{col}_row{row:02d}  no capture in this run')
            bad += 1
            continue
        n = len(find_submenu_items(shot, boxes[col]))
        ok = n == expect
        bad += not ok
        print(f'  {label} col{col}_row{row:02d}  spawner={n:2d}  '
              f'catalogue={expect:2d}  {"OK" if ok else "MISMATCH"}')
    print(f'\n{bad} categor{"y" if bad == 1 else "ies"} out of step — indices '
          f'derived from the catalogue are unsafe' if bad else
          '\nevery mapped category matches; indices are safe to click')
    return 1 if bad else 0


class PanelState:
    """Where the spawner panel is, read from one frame.

    Absolute, not relative: nothing in here was derived by comparing against a
    stored baseline, so it is just as valid three clicks into a sequence as it
    is at the root. That is the whole point -- the old reading was a diff
    against the collapsed baseline, which is why every action had to end by
    collapsing again.
    """

    __slots__ = ('open', 'expanded', 'entries', 'why', 'all')

    def __init__(self, open, expanded, entries, why, all=None):
        self.open = open            # is the spawner panel up at all
        self.expanded = expanded    # (col, row) of the FIRST one, or None
        self.entries = entries      # submenu entries of `expanded`
        self.why = why              # human-readable, for logs and failures
        # EVERY expanded node, because there can be more than one. Measured
        # 2026-08-03: the menu is MULTI-OPEN across columns — expanding
        # col2_row01 leaves col1_row01 open, 16/16 transitions.
        #
        # `expanded` and `entries` stay as the first one for the callers and
        # the 44-frame regression that predate this; `at()` and `entries_for()`
        # are the ones that got fixed.
        self.all = list(all if all is not None else
                        ([(expanded[0], expanded[1], entries)]
                         if expanded is not None else []))

    @property
    def collapsed(self):
        return self.open and not self.all

    def at(self, col, row):
        """Is (col,row) expanded? ANY of them, not just the first.

        This used to be `self.expanded == (col, row)`, and with two columns
        open it answered False for the second one — so goto() concluded its
        click had failed, collapsed everything, and clicked again. Measured
        cost before the fix: entering column 2 while column 1 was open took
        `path='via-root'` and 2 clicks, 8/8, while the reverse direction took
        1, 8/8. The asymmetry was never about the menu. It was this line
        reading a list through a single slot.
        """
        return any(c == col and r == row for c, r, _ in self.all)

    def entries_for(self, col, row):
        """That node's submenu entries — not the first node's.

        Same bug, quieter: `entries` belongs to whichever expansion came
        first, so a caller that opened column 2 and read entries while column
        1 was also open got column 1's list, with plausible indices.
        """
        for c, r, e in self.all:
            if c == col and r == row:
                return e
        return []

    def __repr__(self):
        if not self.open:
            return f'<panel closed: {self.why}>'
        if self.expanded is None:
            return '<panel open, all collapsed>'
        c, r = self.expanded
        return f'<panel open, col{c}_row{r:02d} expanded, {len(self.entries)} entries>'


class SpawnerControl:
    """Spawn what a caller asks for. It says WHAT; this works out the clicks.

        with SpawnerControl() as sc:
            sc.give_many(['m416', 'comp_ar', 'red_dot', 'backpack3'])

    The declarative surface is give_many / give / give_weapon /
    give_attachment / give_gear / plan / ready. Nothing else needs to be
    called, and nothing outside this file should be naming a column, a row or
    an entry index — the point of the measured constants is that working out
    where to click is this class's problem.

    The layout is loaded at construction and cannot fail: it is
    detector/spawner_layout.py's measured table, not a file and not a
    screenshot. The PANEL'S STATE is a different thing entirely and is never
    cached — read() takes a fresh frame every time, because the game drops
    connections, pops dialogs, and another agent shares this window.

    Below the declarative entries is a rescue surface (click_category /
    click_entry / read / goto / collapse_all / sync / spawn) for probes that
    have to drive the panel by hand. Marked as such where it is defined.
    """

    def __init__(self, backend='auto', verbose=True, layout=None):
        self.pointer = Pointer(backend)
        self.screen = SpawnerDetector()
        self.verbose = verbose
        self.base_mask = None
        self._recalibrated = False
        self.menu, self.boxes = builtin_layout()
        # Telemetry for the accordion question -- see GOTO_LOG. On by
        # default because a sample of real traffic is the whole point;
        # off for tests, which would otherwise write synthetic rows into
        # the evidence.
        self.log_paths = True
        if layout:
            # An explicit override: a scrape file, or a resolution nobody has
            # measured. Never the default -- a file can go missing, and a
            # missing file used to leave self.menu None and every give_* call
            # returning 'not synced' with the panel plainly on screen.
            try:
                self.menu, self.boxes = load_layout(layout)
                self._log(f'layout overridden from {layout}')
            except (OSError, ValueError, KeyError) as e:
                self._log(f'layout override {layout} is unusable ({e}) — '
                          f'keeping the measured constants')

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # Nothing to release: the Pointer is lazily built and the screen
        # grabber is shared. Deliberately does NOT close the panel -- callers
        # open it themselves and several of them keep it open across steps.
        return False

    def can_press(self):
        """Is there a Pico, i.e. can this send KEYS as well as clicks?

        Exists so a caller can check the precondition up front instead of
        discovering it four minutes in. Every panel here is opened by a
        keypress and SendInput has no key path, so the answer decides whether
        the run is possible at all -- but "which backend, and is it there" is
        this layer's question. calibration/ used to answer it by building a
        throwaway `Pointer(backend)` purely to read `.pico`, which imports
        press/ from a module that is not supposed to know devices exist, and
        reaches for the shared serial port before the driver that needs it.
        """
        return self.pointer.pico is not None

    def _log(self, msg):
        if self.verbose:
            print(f'[spawner] {msg}', flush=True)

    # ── Layout ──

    # col1 weapons, col2 attachments, col3 gear. Not enforced — a game update
    # that adds a category should be loud, not fatal — but a column that comes
    # back SHORT of this is worth another look before believing it. Only the
    # recognition fallback consults it; the measured table is this by
    # construction.
    EXPECTED_ROWS = dict(COLUMN_ROWS)

    def ready(self, need_cols=()):
        """Is the item-spawner panel on screen? -> bool

        The declarative form of the old `sync()`. It answers a question about
        the SCREEN and takes a fresh frame to do it; there is no longer any
        layout to synchronise, so nothing here is cached and calling it twice
        costs two screenshots and nothing else.

        A caller that only wants things spawned does not need this at all:
        give_many() and spawn() check for themselves.
        """
        return self.sync(need_cols=need_cols)

    def sync(self, need_cols=(), retries=3, recalibrate=False):
        """Confirm the panel is on screen. -> bool   (see ready())

        Kept under its old name because several callers use it as a gate
        before a give_*(). It no longer reads any coordinates: those are
        constants now, so the only screen reading left is the button-icon
        check that says the panel is up at all.

        That check is the reliable one. Recognising the category columns is
        not: the panel is translucent, so facing bright terrain the first four
        rows of the weapons column do not separate from the background and the
        whole run aborts with the panel plainly visible — which is exactly what
        happened on 2026-08-02 before this changed.

        recalibrate=True forces the recognition path, for
        tools/scrape_spawner.py. goto() also reaches for it, once, after a
        measured coordinate has already failed twice.
        """
        if recalibrate:
            return self._recalibrate(need_cols, retries)
        if self.menu is None:
            # A caller clearing the table to force a re-read; stocktake does
            # this between give_many() attempts. There is nothing to re-read
            # any more, so put the constants back -- this used to return False
            # here and the retry it was written to enable never ran.
            self._log('layout table was cleared — restoring the constants')
            self.menu, self.boxes = builtin_layout()
        missing = [c for c in need_cols if c not in self.menu]
        if missing:
            self._log(f'layout table has no column(s) {missing}')
            return False
        shot = None
        for attempt in range(retries + 1):
            shot = shoot_parked(settle=0.10)
            if self.screen.classify(shot):
                return True
            if attempt < retries:
                time.sleep(0.3)
        # Say WHICH failure this is. A different resolution fails the icon
        # check for a reason that has nothing to do with the panel being up,
        # and every coordinate in this module is wrong at the same time.
        size = None if shot is None else (shot.shape[1], shot.shape[0])
        if size is not None and size != tuple(LAYOUT_SCREEN):
            self._log(f'screen is {size[0]}x{size[1]}, but the layout was '
                      f'measured at {LAYOUT_SCREEN[0]}x{LAYOUT_SCREEN[1]} — '
                      f're-run tools/scrape_spawner.py at this resolution')
        else:
            self._log('not on the item-spawner screen')
        return False

    def _recalibrate(self, need_cols=(), retries=3):
        """THE FALLBACK: read the collapsed panel and replace the layout table.

        False if the panel is not up. Reached from sync(recalibrate=True) --
        tools/scrape_spawner.py, which is how the constants get re-measured --
        and once from goto(), after a measured coordinate has already failed
        twice. It is not on the normal path and must not become one: it is the
        recognition that facing bright terrain cannot see the weapons column.

        need_cols: the columns the caller is about to use. The panel draws its
        columns progressively, and the three button glyphs that prove it is
        open finish before the last column does — so a sync taken the moment
        the panel reports open comes back with two columns out of three about
        as often as not. That surfaced as "category col3_row06 does not exist
        (0 rows in column 3)" for a backpack plainly on screen.

        So the layout is re-read until it STOPS CHANGING, and until the columns
        actually wanted are there.

        Two things forced the "stops changing" part. A read taken too early can
        find no columns at all, and that used to abort the run outright -- the
        retry loop only ever ran when a *named* column was short, so a panel
        caught one frame into its draw was a hard failure with a plainly
        visible panel on screen. And a read can succeed while still being
        partial: consecutive runs synced 21, then 14, then 10 categories off
        the same unchanged panel. Neither is detectable from one look.

        Stability rather than an expected count, deliberately. The catalogue
        grows with the game, and a hard-coded total would turn every content
        patch into this same silent failure pointing the other way.
        """
        missing, seen = [], None
        for attempt in range(retries + 1):
            if not self._sync_once(quiet=attempt < retries):
                if attempt < retries:
                    time.sleep(0.4)
                    continue
                return False
            shape = {c: len(v) for c, v in self.menu.items()}
            missing = [c for c in need_cols
                       if len(self.menu.get(c, [])) < self.EXPECTED_ROWS.get(c, 1)]
            if not missing and shape == seen:
                return True
            if attempt < retries:
                if missing:
                    self._log(f'column(s) {missing} not drawn yet — re-reading')
                elif seen is not None and shape != seen:
                    self._log(f'layout still changing ({seen} -> {shape}) — '
                              f're-reading')
                seen = shape
                time.sleep(0.4)
                continue
            # Out of tries. A layout that never settled is still usable if the
            # columns this caller needs are present -- refusing here would
            # trade a real risk for a certain failure.
            if not missing:
                self._log(f'layout never settled, going with {shape}')
                return True
        self._log(f'gave up waiting for column(s) {missing}')
        return False

    def _sync_once(self, quiet=False):
        if not game_focused():
            self._log('game is not the foreground window')
            return False
        base = shoot_parked(settle=0.20)
        if self.screen.ready and not self.screen.classify(base):
            if not quiet:
                self._log('not on the item-spawner screen '
                          f'(icon scores {self.screen.scores(base)})')
            return False
        menu = find_menu(base, verbose=False)
        if not menu:
            if not quiet:
                self._log('no category columns found')
            return False
        self.menu, self.boxes = menu, column_boxes(menu)
        self.base_mask = bright_mask(base)
        self._log(f'synced: {sum(len(v) for v in menu.values())} categories, '
                  f'boxes {self.boxes}')
        return True

    def _category(self, col, row):
        """The Category record for a row. Measured, so it is always there.

        This used to raise for a row that was plainly on screen, because the
        table came from a half-drawn panel -- "category col3_row06 does not
        exist (0 rows in column 3)" for a backpack anyone could see. From
        constants that cannot happen; a raise here now means the caller asked
        for a row this build of the game does not have.
        """
        items = (self.menu or {}).get(col, [])
        if not 1 <= row <= len(items):
            raise IndexError(f'category col{col}_row{row:02d} does not exist '
                             f'({len(items)} rows in column {col})')
        return items[row - 1]

    # ════════════════════════════════════════════════════════════
    # L0 — primitives. Send a click. Read nothing, verify nothing.
    #
    # Everything from here to the L2 banner is the RESCUE SURFACE. It is
    # named with a leading underscore so that reading this file makes the
    # boundary obvious, and re-exposed under the old public names at the
    # bottom of the class for the probes in tools/ that drive the panel by
    # hand. A module that wants a gun spawned calls give_many().
    # ════════════════════════════════════════════════════════════

    def panel_open(self):
        """Is the item-spawner screen up? One parked screenshot, no keys.

        Also the in-range test: comma produces this panel only inside the
        training range, so "it opened" and "we are where we think we are" are
        the same observation.
        """
        return bool(self.screen.classify(shoot_parked(settle=0.10)))

    def ensure_panel(self, want=True, tries=3):
        """Comma toggles the item spawner. Poll until it is `want`. -> bool

        Works anywhere in the training range — the panel is a menu bound to a
        key, not a world object you have to stand next to.

        Pressing blind lands in the wrong state half the time, comma being a
        toggle, so this reads before and after every press.
        """
        mouse = self.pointer.pico
        if mouse is None:
            self._log('no Pico: cannot press comma (SendInput has no key path)')
            return False
        for _ in range(tries):
            if self.panel_open() == want:
                return True
            mouse.key(HID_KEY_COMMA, 60)
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < PANEL_WATCH_S:
                if bool(self.screen.classify(shoot_parked(settle=0.05))) == want:
                    time.sleep(PANEL_SETTLE_S)
                    return True
                time.sleep(0.08)
        return bool(self.screen.classify(shoot_parked(settle=0.10))) == want

    def _click_category(self, col, row, settle=OPEN_WAIT):
        """Click a category row. It TOGGLES; this says nothing about the result."""
        item = self._category(col, row)
        self.pointer.click_at(item.click_x, item.y)
        if settle:
            time.sleep(settle)

    def _click_await(self, col, row, done, timeout=OPEN_TIMEOUT):
        """Click a category and poll until `done(state)`, or time runs out.

        The reason goto() is not a chain of sleeps: the slide-open takes what
        it takes, and every calibration run pays for the difference.
        """
        self._click_category(col, row, settle=0.0)
        deadline = time.perf_counter() + timeout
        while True:
            st = self._read()
            if done(st) or time.perf_counter() >= deadline:
                return st
            time.sleep(OPEN_POLL)

    def _click_entry(self, entry):
        """Click one submenu entry, as handed back by read() or goto()."""
        self.pointer.click_at(entry['click_x'], entry['y'])
        time.sleep(SPAWN_WAIT)

    # ════════════════════════════════════════════════════════════
    # L1 — state. One frame in, the whole node out.
    # ════════════════════════════════════════════════════════════

    def _read(self, shot=None):
        """Where the panel is right now. -> PanelState

        Costs one screenshot and no clicks, and is valid at any point in a
        sequence -- there is no baseline to be stale.

        STATE, not geometry: this is the half that is never turned into a
        constant. The coordinates it compares against are fixed; whether the
        panel is up and which row is open is read fresh, every time.
        """
        if self.menu is None:
            return PanelState(False, None, [], 'not synced')
        if shot is None:
            # NOT shoot_parked(). Parking the cursor existed for the old mask
            # diff, where a hover highlight made a collapsed column differ from
            # its baseline. The submenu test is positional -- entries are
            # centred, categories are left-aligned -- and a hover changes
            # neither, so the park is pure latency here: one cursor move plus a
            # settle on every read, and goto() reads up to three times.
            shot = capture_screen()
        if self.screen.ready and not self.screen.classify(shot):
            return PanelState(False, None, [], 'not on the item-spawner screen')
        found = expansions(shot, self.menu, self.boxes)
        if not found:
            return PanelState(True, None, [], 'all collapsed')
        if len(found) > 1:
            # THE NORMAL CASE, measured 2026-08-03 — not the anomaly this
            # branch was written for. The 42 ground-truthed frames all expand
            # from a COLLAPSED panel, so none of them could ever have shown
            # two, and the comment here used to say "never seen" on that
            # basis. Kept as a debug line, demoted from a warning.
            self._log(f'{len(found)} expanded at once: '
                      f'{[(c, r) for c, r, _ in found]}')
        col, row, entries = found[0]
        return PanelState(True, (col, row), entries, 'expanded', all=found)

    # ════════════════════════════════════════════════════════════
    # L1 — transitions. Shortest path from wherever we happen to be.
    # ════════════════════════════════════════════════════════════

    def _goto(self, col, row):
        """Leave (col,row) expanded, and log the transition. See _walk_to."""
        rec = self._walk_to(col, row)
        if self.log_paths:
            record_goto(rec, col, row)
        return rec

    def _walk_to(self, col, row):
        """Leave (col,row) expanded, in as few clicks as the menu allows.

        -> {'ok', 'clicks', 'path', 'from', 'entries', 'error'}

        Deliberately does NOT assume whether this menu is an accordion (opening
        one category closes the last) or lets several stand open. That has
        never been measured -- every capture in docs/spawner/runs/ expands from
        a collapsed panel -- and guessing either way costs a click or a wrong
        state. So: click the target first and read back. On an accordion that
        is one click and done; if it is not, the read-back says so and this
        clears the way and retries.

        `path` records which it turned out to be, and `from` records WHAT WAS
        EXPANDED WHEN IT STARTED. Both are needed and the second was missing:

            path='direct' from a COLLAPSED panel proves nothing. One click was
            always going to be enough — there was nothing to close.

            path='direct' with ANOTHER category expanded is the whole answer.
            It means opening one closed the other, i.e. an accordion, i.e. the
            cost table's "1 click" for a same-column category switch.

        Logging the path alone would have produced a pile of rows that cannot
        distinguish those, which is a worse outcome than no data: it looks like
        evidence. record_goto() writes both; `pixi run goto-paths` splits on it.
        """
        st = self._read()
        if not st.open:
            return {'ok': False, 'clicks': 0, 'path': 'closed', 'from': None,
                    'entries': [], 'error': st.why}
        was = tuple(st.expanded) if st.expanded is not None else None
        ents = st.entries_for(col, row)
        if st.at(col, row) and ents:
            return {'ok': True, 'clicks': 0, 'path': 'already', 'from': was,
                    'entries': ents, 'error': None}

        # Is the measured coordinate for (col,row) still where that row is
        # drawn? It is, unless an expanded submenu ABOVE it in the SAME column
        # has pushed it down -- measured 7 entries = 360 px, enough that the
        # coordinate lands on a submenu entry instead. Clicking there does not
        # miss harmlessly, it SPAWNS something nobody asked for. So close the
        # blocker first; everything else can be clicked where it always was.
        clicks = 0
        if (st.expanded is not None and st.expanded[0] == col
                and st.expanded[1] < row):
            self._click_category(*st.expanded)
            clicks += 1

        self._click_category(col, row)
        clicks += 1
        st = self._read()
        ents = st.entries_for(col, row)
        if st.at(col, row) and ents:
            return {'ok': True, 'clicks': clicks, 'from': was,
                    'path': 'direct' if clicks == 1 else 'closed-blocker',
                    'entries': ents, 'error': None}

        # Whatever is in the way, the root is a state every coordinate is
        # valid in. One extra collapse beats guessing.
        self._collapse_all()
        self._click_category(col, row)
        clicks += 1
        st = self._read()
        ents = st.entries_for(col, row)
        if st.at(col, row) and ents:
            return {'ok': True, 'clicks': clicks, 'path': 'via-root',
                    'from': was, 'entries': ents, 'error': None}

        # Twice from a state every coordinate is valid in, and the row still
        # did not open. Either the panel is misbehaving or the MEASURED LAYOUT
        # no longer describes this build -- a patch that inserts a category
        # shifts every row below it, and the symptom is exactly this: a click
        # that lands between rows and does nothing.
        #
        # This is the one place recognition is worth its cost. It runs here and
        # only here: after the constants have already failed, at most once per
        # instance, so a genuinely broken panel cannot turn every goto() into
        # four screenshots.
        if not self._recalibrated:
            self._recalibrated = True
            self._log('measured coordinates did not open the row — reading the '
                      'panel off the screen once, as a fallback')
            self._collapse_all()
            if self._recalibrate(need_cols=(col,)):
                self._click_category(col, row)
                clicks += 1
                st = self._read()
                ents = st.entries_for(col, row)
        if st.at(col, row) and ents:
                    self._log('THE PANEL HAS MOVED: recognition works where '
                              'the measured layout does not. Re-run '
                              'tools/scrape_spawner.py and update the '
                              'constants in detector/spawner_layout.py')
                    return {'ok': True, 'clicks': clicks,
                            'path': 'recalibrated', 'from': was,
                            'entries': ents, 'error': None}

        return {'ok': False, 'clicks': clicks, 'path': 'failed',
                'from': was, 'entries': [],
                'error': f'col{col}_row{row:02d} would not expand '
                         f'(panel reads {st!r})'}

    def _collapse_all(self, retries=2):
        """Back to the root: nothing expanded. -> bool

        No longer on the critical path of every action -- this is the recovery
        route for when read() cannot make sense of the panel, and the state
        other tools expect to find the panel in when a run ends.
        """
        for _ in range(retries + 1):
            st = self._read()
            if not st.open:
                return False
            if st.expanded is None:
                return True
            self._click_category(*st.expanded)
        st = self._read()
        if st.expanded is not None:
            self._log(f'{st!r} — stuck expanded; the measured layout may no '
                      f'longer match this build, try sync(recalibrate=True)')
        return st.expanded is None

    # ── Compatibility: the old two-call shape, on top of the new one ──

    def expand(self, col, row, retries=2):
        """Open a category. Returns its submenu entries, or [] on failure."""
        return self._goto(col, row)['entries']

    def collapse(self, col, row, retries=2):
        """Close a category. Kept for callers that name the row explicitly.

        The old implementation diffed the column against the baseline mask
        taken at sync() and had to keep patching around that baseline drifting:
        the panel is translucent, so items landing on the ground changed the
        pixels *behind* column 2 and every category after the first spawn read
        as permanently expanded. Four "stuck expanded" warnings in a row, for
        menus that had closed perfectly.

        Reading the submenu directly has no baseline to drift, so the whole
        problem and its workaround are gone.
        """
        return self._collapse_all(retries=retries)

    def _spawn(self, col, row, index, times=1, expect=None, leave_open=False):
        """Click entry #index (1-based) of a category, `times` times.

        Re-locates the entry before every click, so it does not matter whether
        the game leaves the submenu open after a spawn.

        `expect` is how many entries the catalogue says this category has. A
        mismatch means the game's list moved under the catalogue and every
        index derived from it is suspect — that fails rather than clicking
        whatever now happens to sit at that position.

        `leave_open=True` skips the collapse at the end. That is where the
        saving is when several things are being spawned in a row — see
        give_many(), which is the reason this parameter exists. It defaults to
        False because a caller that is about to press a key or read the HUD
        wants the panel out of the way.
        """
        if self.menu is None:
            self.menu, self.boxes = builtin_layout()

        clicked, err = 0, None
        for _ in range(times):
            # goto() short-circuits to zero clicks when the category is
            # already the one showing, so the second copy of an item costs a
            # screenshot rather than a collapse-and-reopen. It also reads the
            # panel first, which is what refuses to click when the spawner is
            # not on screen -- there is no separate sync() gate here.
            rec = self._goto(col, row)
            if not rec['ok']:
                err = rec['error']
                break
            entries = rec['entries']
            if expect is not None and len(entries) != expect:
                err = (f'category shows {len(entries)} entries, the catalogue '
                       f'says {expect} — indices are stale, re-scrape before '
                       f'trusting them')
                break
            if not 1 <= index <= len(entries):
                err = (f'entry {index} out of range, category has '
                       f'{len(entries)}')
                break
            self._click_entry(entries[index - 1])
            clicked += 1

        ok = err is None
        if not leave_open:
            closed = self._collapse_all()
            if ok and not closed:
                ok, err = False, 'stuck expanded'
        return {'ok': ok, 'clicked': clicked, 'error': err}

    def switch_to_slot2(self):
        """Press 2. Selects whatever now sits in the second weapon slot."""
        mouse = self.pointer.pico
        if mouse is None:
            self._log('no Pico: cannot press 2 (SendInput has no key path)')
            return False
        mouse.key(HID_KEY_2, 60)
        time.sleep(SWITCH_WAIT)
        return True

    # ════════════════════════════════════════════════════════════
    # L2 — intent. THE interface other modules call. Say what you want.
    #
    # Nothing below takes a column, a row or an entry index. A caller names a
    # catalogue key and this file works out where that lives, in what order to
    # visit the panel, and which pixels to click -- from the measured
    # constants, without looking for anything.
    # ════════════════════════════════════════════════════════════

    def give_weapon(self, key, switch=True, times=1):
        """Spawn weapon `key`, once. Returns a record with ok/clicked/error.

        Which slot it lands in is the game's rule (see the module docstring),
        not something this decides. `switch=True` presses 2 afterwards, which
        is right whenever the rack was not empty — pass switch=False and read
        the rack yourself if that assumption is not safe here.

        `times` is for a caller that wants the gun in slot 2 from an unknown
        starting state and would rather pay a gun on the floor than a
        screenshot: two clicks converge there from any of the three states.
        """
        (col, row), index, expect = weapon_position(key)
        self._log(f'{key} -> col{col}_row{row:02d} entry {index}/{expect} '
                  f'({ROSTER[key][1]})')
        rec = self._spawn(col, row, index, times=times, expect=expect)
        rec.update(weapon=key, category=(col, row), index=index)
        if rec['ok'] and switch:
            rec['switched'] = self.switch_to_slot2()
        return rec

    def give_attachment(self, key, count=1):
        """Spawn attachment `key` into the backpack, `count` times.

        Only one click per copy: attachments do not evict anything, they land
        in the backpack. Getting one onto a gun from there is
        control/inventory.py's job — this only puts it within reach.
        """
        (col, row), index, expect = attachment_position(key)
        self._log(f'{key} -> col{col}_row{row:02d} entry {index}/{expect} '
                  f'({ATTACHMENTS[key]["zh"]})')
        rec = self._spawn(col, row, index, times=count, expect=expect)
        rec.update(attachment=key, category=(col, row), index=index)
        return rec

    def give_gear(self, key):
        """Spawn a piece of column-3 gear. -> {'ok', 'gear', 'error'}

        Driven BLIND: the category is clicked open and the entry clicked
        without ever recognising either. That is not laziness, it is the one
        column recognition cannot see — facing a red banner, ITEMS and
        SENSITIVITY merged into one x band that was rejected as "not a list",
        and column 3 stopped existing four re-reads running. The check that
        replaces it is that the column CHANGED between two frames half a
        second apart, which the background behind a translucent panel does not.

        Because it is blind, it only works from the root. give_many() puts a
        collapse in front of it and orders gear first for exactly that reason;
        this is a one-item give_many() and inherits both.
        """
        if key not in GEAR:
            raise KeyError(f'unknown gear {key!r}')
        rec = self.give_many([key], switch=False)
        return {'ok': rec['ok'], 'gear': key, 'error': rec['error']}

    def give(self, key, **kw):
        """Whichever of the three applies to `key`."""
        if key in ROSTER:
            return self.give_weapon(key, **kw)
        if key in ATTACHMENTS:
            return self.give_attachment(key, **kw)
        if key in GEAR:
            return self.give_gear(key, **kw)
        raise KeyError(f'{key!r} is in ROSTER, ATTACHMENTS nor GEAR')

    def plan(self, keys, weapon_times=1):
        """What give_many(keys) would do. Clicks nothing. See plan()."""
        return plan(keys, weapon_times=weapon_times)

    def script(self, keys, weapon_times=1):
        """Every click give_many(keys) would send. See click_plan().

        Offline and exact, which is what makes a shopping list reviewable
        before anything touches the game — and what `pixi run spawner-plan`
        checks.
        """
        return click_plan(plan(keys, weapon_times=weapon_times),
                          self.menu, self.boxes)

    def _open_category(self, mv):
        """Click a category open and prove it opened. -> error string or None."""
        col, row = mv['category']
        if mv['blind']:
            # Gear. Nothing to recognise here (see give_gear): the proof is
            # that the column changed between two frames, which the scene
            # behind a translucent panel does not do on its own.
            before = bright_mask(shoot_parked(settle=0.15))
            self.pointer.click_at(*mv['xy'])
            time.sleep(OPEN_WAIT)
            after = bright_mask(shoot_parked(settle=0.15))
            x0, x1 = self.boxes[col]
            y0, y1 = GEAR_BOX_Y
            changed = int(np.count_nonzero(
                before[y0:y1, x0:x1] != after[y0:y1, x0:x1]))
            if changed < CHANGE_MIN:
                return (f'col{col}_row{row:02d} did not open ({changed} px '
                        f'changed, need {CHANGE_MIN}) — is the panel up and '
                        f'collapsed?')
            return None

        # One look, to confirm the category really opened and that the
        # catalogue still agrees about how many entries it has.
        st = self._click_await(col, row,
                               lambda s: s.entries_for(col, row))
        ents = st.entries_for(col, row)
        if not ents:
            return f'col{col}_row{row:02d} would not expand ({st!r})'
        # entries_for, not st.entries. This guard is the thing that stops a
        # stale layout clicking whatever happens to sit at the old
        # coordinates, and with two columns open `st.entries` is the FIRST
        # column's list -- so it would have compared column 1's entry count
        # against column 2's expectation and refused a perfectly good panel,
        # or, with matching counts, passed while pointing at the wrong list.
        expect = mv.get('expect')
        if expect is not None and len(ents) != expect:
            return (f'col{col}_row{row:02d} shows {len(ents)} entries, '
                    f'the catalogue says {expect} — indices are stale, '
                    f're-scrape before trusting them')
        return None

    def give_many(self, keys, switch=True, weapon_times=1):
        """Spawn everything in `keys`, moving the panel as little as possible.

        -> {'ok', 'steps': [...], 'clicks', 'error'}

        THE entry point. A caller names catalogue keys in any order and this
        works out the rest: which category each lives in, what order to visit
        them so no coordinate goes stale, and where to click.

        The reason it exists rather than a loop over give(): each give_*()
        returns the panel to fully collapsed, so N items from N categories pay
        2N category clicks and 2N screenshots. Here the panel stays open across
        the whole list and collapses once at the end.

        It walks click_plan() literally, so what runs against the game is the
        same list `pixi run spawner-plan` checks offline. The verification is
        what is left over: every 'open' is proved to have opened before any
        entry under it is clicked, because an entry click that lands on a
        collapsed panel is not a no-op — it is a click on whatever is drawn
        there instead.
        """
        steps = plan(keys, weapon_times=weapon_times)
        if not steps:
            return {'ok': True, 'steps': [], 'clicks': 0, 'error': None}

        need = tuple(sorted({s['category'][0] for s in steps if s['category']}))
        # Open it ourselves. A caller that has to know "the panel must be up
        # first" is being told HOW, and the whole point of this entry point is
        # that it is only told WHAT. Leaving it to the caller also fails in the
        # least useful way: sync() answers 'not on the item-spawner screen',
        # give_many returns ok=False with clicks=0, and nothing says the fix is
        # one comma press. Idempotent -- ensure_panel reads before it presses,
        # so an already-open panel costs one screenshot.
        if not self.ensure_panel(True):
            return {'ok': False, 'steps': [], 'clicks': 0,
                    'error': 'the item-spawner panel would not open'}
        # Unconditional, unlike the old `if self.menu is None`: the layout is
        # a constant now and is never None, so that test never fired and the
        # panel-is-on-screen check went with it. This is the gate.
        if not self.sync(need_cols=need):
            return {'ok': False, 'steps': [], 'clicks': 0,
                    'error': 'not synced'}

        # Off THIS instance's table, not off the constants directly: a
        # recalibrated or overridden layout has to move the entry clicks too,
        # or the category would open in one place and be clicked in another.
        script = click_plan(steps, self.menu, self.boxes)
        out, order, clicks, err = {}, [], 0, None
        for i, mv in enumerate(script):
            key, act = mv['key'], mv['act']
            if key not in out:
                out[key] = {'ok': True, 'clicked': 0, 'error': None,
                            'key': key, 'kind': mv['kind']}
                order.append(key)

            if act == 'root':
                self._collapse_all()
            elif act == 'open':
                col, row = mv['category']
                names = dict.fromkeys(m['key'] for m in script[i:]
                                      if m['act'] == 'entry'
                                      and m['category'] == mv['category'])
                self._log(f'col{col}_row{row:02d} {mv["xy"]}: '
                          + ', '.join(names))
                err = self._open_category(mv)
                clicks += 1
            elif act == 'entry':
                # Clicked from geometry, not from a frame. The entry grid is
                # rigid (44.25 / 50.70 / 237, sd < 0.5 px over 246 measured
                # entries), so looking for each row would cost a screenshot to
                # be told what the arithmetic already knows.
                self.pointer.click_at(*mv['xy'])
                clicks += 1
                out[key]['clicked'] += 1
                time.sleep(SPAWN_WAIT)
            elif act == 'close':
                self.pointer.click_at(*mv['xy'])
                clicks += 1

            if err:
                out[key]['ok'], out[key]['error'] = False, err
                break

        # No _collapse_all() here. Closing the panel resets the expansion by
        # itself (measured), so collapsing first was a second click on the
        # same category row -- and that click is a LEFT click at a panel
        # coordinate, which reaches the game and fires the weapon if the panel
        # has already gone. Closing is the stronger operation; it is enough.
        #
        # AND THE PANEL COMES DOWN. It was opened here, so it is closed here —
        # a caller told only WHAT it wants should not be left holding a screen
        # it never asked to open.
        #
        # Collapsing the categories is not the same thing and used to be all
        # that happened, which left the item-spawner panel up. With it up the
        # Tab inventory will not open, so the NEXT thing the caller does fails
        # instead of this one: a plate collection run spawned its first pair,
        # returned ok, and then reported "the inventory would not open" for
        # every round after it. Measured directly — panel_open() reads True
        # after a give_many that reported success.
        #
        # Before switch_to_slot2, not after: that presses a number key, and
        # keys go to whatever screen is up.
        if not self.ensure_panel(False):
            err = err or 'the item-spawner panel would not close'
        if err is None and switch and any(s['kind'] == 'weapon' for s in steps):
            self.switch_to_slot2()
        return {'ok': err is None, 'steps': [out[k] for k in order],
                'clicks': clicks, 'error': err}

    # ── Rescue surface: L0/L1 under their public names ──────────────────
    #
    # NOT the normal path. A module that knows what it wants calls give_many()
    # and lets this file work out the clicks; these are here because a probe
    # sometimes has to drive the panel by hand —
    # tools/probe_spawner_layers.py walks goto()/read() to record which kind of
    # menu this is, tools/probe_submenu_hover.py needs a click with nothing
    # verified after it, and several callers still use sync() as a gate before
    # a give_*() (which is redundant now: give_many() gates itself).
    #
    # Aliases rather than the definitions, so that reading this class top to
    # bottom makes the boundary obvious: everything the panel is actually
    # driven by is spelled with a leading underscore.
    click_category = _click_category
    click_entry = _click_entry
    read = _read
    goto = _goto
    collapse_all = _collapse_all
    spawn = _spawn


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('keys', nargs='*',
                    help='ROSTER / ATTACHMENTS / GEAR keys, e.g. m416 comp_ar '
                         'backpack3')
    ap.add_argument('--list', action='store_true',
                    help='print everything this can spawn, with its index')
    ap.add_argument('--plan', action='store_true',
                    help='offline: print every click these keys turn into, '
                         'and stop. No game, no panel, no hardware')
    ap.add_argument('--check', nargs='?', const='latest', metavar='RUN_DIR',
                    help='offline: check every category count against a '
                         'captured scrape run (default: the newest)')
    ap.add_argument('--no-switch', action='store_true',
                    help='weapons: spawn but do not press 2')
    ap.add_argument('--count', type=int, default=1,
                    help='attachments: how many copies into the backpack')
    ap.add_argument('--countdown', type=int, default=5)
    ap.add_argument('--backend', default='auto',
                    choices=('auto', 'pico', 'sendinput'))
    args = ap.parse_args()

    if args.check:
        run = args.check
        if run == 'latest':
            runs = sorted(glob.glob(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'docs', 'spawner', 'runs', '*')))
            if not runs:
                print('no scrape runs under docs/spawner/runs/')
                return 1
            run = runs[-1]
        print(f'checking against {os.path.relpath(run)}\n')
        return check_against_run(run)

    if args.list:
        for cls, cat in CATEGORY_OF_CLASS.items():
            peers = [k for k, v in ROSTER.items() if v[0] == cls and is_live(k)]
            print(f'{cls}  col{cat[0]}_row{cat[1]:02d}')
            for i, k in enumerate(peers, 1):
                print(f'   {i:2d}. {k:<10} {ROSTER[k][1]}')
        for slot, cat in CATEGORY_OF_SLOT.items():
            print(f'{slot}  col{cat[0]}_row{cat[1]:02d}')
            extras = SPAWNER_EXTRAS.get(slot, {})
            i = 0
            for k, v in ATTACHMENTS.items():
                if v['slot'] != slot:
                    continue
                i += 1
                while i in extras:
                    print(f'   {i:2d}. {"(not catalogued)":<16} {extras[i]}')
                    i += 1
                print(f'   {i:2d}. {k:<16} {v["zh"]}')
        return 0

    if not args.keys:
        ap.error('give one or more catalogue keys, or --list')

    keys = [k for k in args.keys for _ in range(
        args.count if k in ATTACHMENTS else 1)]

    # Offline: the whole click sequence, from the measured constants, without
    # anything running. Same function give_many() walks.
    for mv in click_plan(plan(keys)):
        col, row = mv['category']
        print(f'  {mv["act"]:<6} col{col}_row{row:02d}  {str(mv["xy"]):<14} '
              f'{mv["key"]}' + ('  (blind)' if mv['blind'] else ''))
    if args.plan:
        return 0

    print('\n>>> Taking the foreground. The spawner panel must be OPEN and '
          'collapsed.')
    if not ensure_focus(countdown_s=args.countdown, label='the spawner'):
        print('[!] ABORT: could not focus the game.')
        return 1

    with SpawnerControl(args.backend) as sc:
        rec = sc.give_many(keys, switch=not args.no_switch)
    print(f'\n{rec}')
    return 0 if rec['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())

