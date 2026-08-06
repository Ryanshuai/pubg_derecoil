"""Spawn known items, turn on the spot, photograph what the Tab screen draws.

One Tab frame carries four things a detector must read, all behind the same
translucent panel and all changing with whatever is behind it:

    slots   the five attachment icons on the gun      detector/tab_items
    rows    the 库存 list's icons                      detector/tab_items
    plate   the weapon's name plate                   weapon_template_detector
    type    the 类型 marker that means "Tab is up"     detector/tab_detector

Turning and cycling Tab is what a run costs; cutting one more region out of a
frame already grabbed is free, so all four come from the same pass.

    pixi run python calibration/collect_templates.py --plan --all
    pixi run python calibration/collect_templates.py --all --targets slots,plate,type
    pixi run python calibration/collect_templates.py --slot grip --targets rows
    pixi run python calibration/collect_templates.py --plates
    pixi run python calibration/collect_templates.py --as-is --label muzzle=comp_ar

GROUND TRUTH IS SELF-SPECIFIED, AND NO TEMPLATE ESTABLISHES IT — the items
worth collecting are exactly the ones no template can name. Spawning the host
weapon evicts the old gun to the floor with its attachments, so it arrives bare
and 库存 does not grow round over round.

WHICH ROW HOLDS WHICH PART IS AN ANSWER, NOT AN ASSUMPTION. It used to be
assumed: "库存 fills from the top with no gaps, so row N holds a known item."
The count is right and is still used; the ORDER is not. The game sorts that
list its own way, and the first real run of this collector spawned angled_grip,
brake_ar, cheek_pad, ext_ar, holo into rows holding holo, cheek_pad, ext_ar,
brake_ar and a grip — 228 crops, every label on the wrong one.

So the rows are photographed UNLABELLED, and then each is RIGHT-CLICKED, which
hands it to the game to place. The slot that gains an icon names the part,
because a round's parts want different slots and the catalogue says which. The
row is learned on the way past. Two independent facts still confirm it and
neither reads a template: exactly one slot went from empty to drawn, AND 库存
lost exactly one row. Scenery can move pixels; it cannot take a row out of a
list.

Right-click and not a drag: 库存 -> gun measured 0 landings out of 4 by drag
against 4/4 by right click (docs/game_quirks.md, control/CLAUDE.md). The old
version dragged, so no part was ever fitted and every `slots` capture came back
empty.

A retry only happens when nothing changed at all — the one case where the
source row is provably still the source row. Any other effect ends the round,
because a second click would equip whatever slid into that row.

Turning happens with Tab CLOSED: with it open the mouse drives a cursor, not
the view, and every capture comes back identical.

Output: docs/attachments/runs/<stamp>/, a run in the shared CaptureRun format
(calibration/capture_run.py). `manifest.json`'s facts carry the `bad` list,
which is the work queue for the calibrate-template skill. Runs written before
2026-08-03 carry an index.json instead and read back through the same API.

The directory does not move under docs/runs/ for the same reason capture_ads
stays put: a run's path is already load-bearing elsewhere (see capture_run.py),
and the skill that consumes these is pointed at this one. What unifies is the
manifest.

WHICH LABELS ARE GROUND TRUTH, AND WHY THIS FILE HAS THE STRONGEST CLAIM IN
THE REPOSITORY — see label_for(). Short version: `slots` and `rows` are
LABEL_REQUESTED because the identity travels an addressing chain (spawner
coordinate -> 库存 -> the slot the game chose) whose every hop is confirmed
WITHOUT A TEMPLATE, which is exactly what makes it non-circular for the
templates being collected. `plate` is LABEL_REQUESTED only when a weapon was
watched ARRIVING on a cleared rack; `type` gets no label at all. Both reasons
are in label_for().
"""
import argparse
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from capture_run import CaptureRun, LABEL_REQUESTED
from config import (HUD_REGIONS, TAB_PIXEL_THRESH, TAB_COUNT_MIN,
                    TAB_COUNT_MAX)
from detector.attachment_catalog import ATTACHMENTS, ROSTER, SLOTS, fits
from detector.cropper import capture_screen, win32_cap
from detector.geometry import detail
from detector.tab_detector import TabTypeDetector
from detector.attachment_detector import SLOT_DETAIL_MIN, SLOT_NAMES
from detector.slot_detector import SlotDetector
from detector.tab_items import (ROW_DETAIL_MIN, inserted_row,
                                row_icons, tab_blocks)
from detector.tab_layout import INV_ROWS, icon_box
from control.focus import ensure_focus, focus_keeper

from control.spawner import SpawnerControl
from control.inventory import (InventoryControl, PLATE_INK_MIN,
                               at_ground, at_inv)
from harvest import BACKPACK
from range_session import get_session
from sweep import Rig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ROOT = os.path.join(ROOT, 'docs', 'attachments', 'runs')

TARGETS = ('slots', 'rows', 'plate', 'type')

# Not a target anyone asks for -- it is written alongside `slots` by
# paired_sweep. A backdrop crop is the SAME slot at the SAME angle with
# nothing in it, which is the only way to know what a filled crop was
# composited over. It carries no label: it is not a picture of an
# attachment, it is a picture of the absence of one.
BACKDROP = 'backdrop'

TURN_COUNTS = 900               # yaw per step; lands on unrelated scenery
PITCH_STEPS = (0, -260, 260)    # sky, level, ground — the three that differ most
SETTLE_S = 0.45
SPAWN_SETTLE_S = 0.7
FIT_TIMEOUT_S = 0.8             # the part animates into the slot
CLEAR_TRIES = 3                 # 库存 clearings before a part is given up on;
                                # clear_inventory already retries per row, so
                                # this covers the list refilling from below
FIT_POLL_S = 0.08

# The CEILING on a name plate's ink. The floor lives in control/inventory.py
# with plate_ink itself, because control acts on it; this one is only ever a
# sanity check here, so it stays here.
#
# It is a band for the same reason TAB_COUNT_MIN/MAX is one. With the panel up
# the backdrop is dimmed, but the mask is only "near-white and achromatic",
# and over 293 Tab-SHUT frames this region saturates at 11250 — the whole crop
# — on 80 of them. Bright everywhere is scenery, not glyphs, and a reading
# above the ceiling is refused rather than believed. 4000 is 4.4x over the
# highest real plate and well under saturation.
PLATE_INK_MAX = 4000

# How long to keep looking for the name plate before calling the rack empty.
# The panel fades in after every Tab cycle, so "no ink in this one frame" is
# not "no gun" -- it is usually "too early". Matches InventoryControl's
# GUN_SLOT_WATCH_S, which exists for the same reason and was measured there.
GUN_WATCH_S = 1.2

# Mean |after-before| over a slot crop, grey levels, for "an icon appeared".
# An icon landing in an empty 63x63 slot moves most pixels most of the way;
# scenery a second apart with the player standing still moves nothing. Kept low
# because it is only half the verdict — the other half is 库存 losing exactly
# one row, which no amount of scenery can fake.
CHANGE_MIN = 6.0

# ...and the winner has to DOMINATE, not merely clear the floor. brake_ar
# landed in the muzzle at 25.6 while the stock read 6.1 against a floor of 6.0,
# so a floor-only rule called it two slots at once and threw the part away. The
# real landing beat the runner-up four times over.
#
# Raising CHANGE_MIN instead would trade this false rejection for a false
# acceptance on some quieter icon. A ratio adapts: whatever the part, the slot
# it went into moves far more than the panel breathing next door.
CHANGE_MARGIN = 2.5

BY_ASSET = {v['asset']: k for k, v in ATTACHMENTS.items() if v.get('asset')}


def cut(frame, region):
    """A HUD_REGIONS entry out of a screen-indexed frame."""
    y, x, h, w = HUD_REGIONS[region] if isinstance(region, str) else region
    return frame[y:y + h, x:x + w]


def winner(moved):
    """The one slot a part landed in, or None. `moved` is {slot: change}.

    Two conditions, and the second is the one that matters: the top mover
    clears CHANGE_MIN, AND it beats the runner-up by CHANGE_MARGIN. A floor
    alone called brake_ar two slots at once -- muzzle 25.6, stock 6.1, floor
    6.0 -- and threw away a part that had landed perfectly well.
    """
    if not moved:
        return None
    order = sorted(moved.items(), key=lambda kv: -kv[1])
    top, second = order[0], (order[1][1] if len(order) > 1 else 0.0)
    if top[1] < CHANGE_MIN:
        return None
    return top[0] if top[1] >= CHANGE_MARGIN * max(second, 1e-6) else None


# ════════════════════════════════════════════════════════════
# Planning — pure, no game needed (--plan prints it)
# ════════════════════════════════════════════════════════════

def hosts_for(keys):
    """Cover `keys` with as few host weapons as possible.

    Greedy set cover; the catalogue is small enough that greedy and optimal
    agree. A weapon whose slot list is a guess (attachment_catalog.unverified())
    loses ties: a wrong slot list does not fail loudly, it drops the part on the
    floor and leaves a crop of an empty slot labelled as a part.
    """
    remaining, plan = set(keys), []
    while remaining:
        best, cover, rank = None, (), -1
        for w in ROSTER:
            cov = tuple(k for k in remaining if fits(w, k))
            r = 0 if SLOTS.get(w, {}).get('conf') == 'guess' else 1
            if (len(cov), r) > (len(cover), rank):
                best, cover, rank = w, cov, r
        if not cover:
            return plan, sorted(remaining)
        plan.append((best, sorted(cover)))
        remaining -= set(cover)
    return plan, []


def by_slot(keys):
    """Split into rounds wearable at once — one part per slot."""
    slots = {}
    for k in keys:
        slots.setdefault(ATTACHMENTS[k]['slot'], []).append(k)
    n = max((len(v) for v in slots.values()), default=0)
    return [[v[i] for v in slots.values() if i < len(v)] for i in range(n)]


def plan_rounds(targets, keys, weapons, plates):
    """[(weapon|None, [key, ...], fit), ...] plus the keys nothing can wear.

    Three shapes, decided by what the targets need:
      --plates      TWO weapons per round, no parts
      slots         parts must physically fit, so one per slot on a host
      otherwise     parts only sit in 库存 — no compatibility to satisfy, and
                    the list holds INV_ROWS of them at a time
    """
    if plates:
        # PAIRS. The rack holds two guns and both name plates are drawn in the
        # same frame, so one turn to a background yields two plates. Turning is
        # what a run spends its time on -- 30 weapons go from 30 sweeps to 15.
        # An odd roster leaves a round of one, which plate_pair handles: it
        # loops over whatever it was given.
        return [(list(weapons[i:i + 2]), [], False)
                for i in range(0, len(weapons), 2)], []
    if 'slots' in targets:
        if weapons:
            plan, left = [], set(keys)
            for w in weapons:
                cov = sorted(k for k in left if fits(w, k))
                if cov:
                    plan.append((w, cov))
                    left -= set(cov)
            unreachable = sorted(left)
        else:
            plan, unreachable = hosts_for(keys)
        return ([(w, lo, True) for w, ks in plan for lo in by_slot(ks)],
                unreachable)
    host = weapons[0] if weapons else None
    return ([(host, keys[i:i + INV_ROWS], False)
             for i in range(0, len(keys), INV_ROWS)], [])


def parse_label(text):
    """'muzzle=comp_ar,grip=vert_grip' -> ['comp_ar', 'vert_grip']"""
    keys = []
    for pair in text.split(','):
        _, _, k = pair.partition('=')
        k = k.strip()
        if not k:
            continue
        if k not in ATTACHMENTS:
            raise ValueError(f'{k!r} is not in the attachment catalogue')
        keys.append(k)
    return keys


# ════════════════════════════════════════════════════════════
# Template-free reads — nothing here may depend on a template
# ════════════════════════════════════════════════════════════

# `detail` is detector/geometry.detail — imported at the top of this file, and
# re-exported here because tools/ scripts import it from this module by name.
# It used to be defined here; five other copies of the same Laplacian read had
# drifted into four different guard sets before they were merged 2026-08-06.
# Thresholds still live with their callers, deliberately: see geometry.detail.


def change(a, b):
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean())


def inv_rows(frame):
    """Occupied 库存 rows, named or not. Not TabView.rows(): that runs the
    whole template bank to answer what the Laplacian already answers, and this
    is the reading every label rests on."""
    n = 0
    for i in range(INV_ROWS):
        x0, y0, x1, y1 = icon_box(i, 'inventory')
        if y1 > frame.shape[0] or x1 > frame.shape[1]:
            break
        if detail(frame[y0:y1, x0:x1]) >= ROW_DETAIL_MIN:
            n = i + 1
    return n


# ════════════════════════════════════════════════════════════
# What a crop is entitled to claim
# ════════════════════════════════════════════════════════════

KIND = 'attachments'     # see calibration/capture_run.py for the layout


def plate_arrived(before, after):
    """Did a weapon arrive in the rack between these two ink readings? -> bool

    `before` is taken on a CLEARED rack, `after` once the spawner has been
    asked for exactly one weapon. Both are white-text-mask pixel counts over
    the name plate — no template is consulted, which is the whole point: the
    plate OCR is the detector this evidence exists to label samples FOR.

    Neither reading alone would do. A plate that was already there stays there
    if the spawn silently produced nothing, so the zero is what makes the
    second number mean "arrived" rather than "is present". And the after has a
    CEILING as well as a floor, because the mask is only "near-white and
    achromatic": with nothing to dim it this region saturates on scenery.
    """
    return (before < PLATE_INK_MIN
            and PLATE_INK_MIN <= after <= PLATE_INK_MAX)


def label_for(target, key, slot, by, arrived=False):
    """The label a crop of `target` may carry. -> [] or [one label]

    capture_run.py's rule: a label exists only when someone looked, and
    `source` says who. Applied here target by target, because the four differ
    and the difference is the whole reason this file exists.

    `slots` and `rows` are LABEL_REQUESTED, and this is the one producer whose
    claim to that is airtight for the templates being collected. Nothing in the
    chain consults a template:

        the spawner clicks a MEASURED COORDINATE for a named item, and refuses
        when the category's entry count has drifted from the catalogue
                    ->  the part lands in 库存, which fills from the top with
                        no gaps, and rows_of() checks the count grew by exactly
                        the number spawned
                    ->  the drag is verified by the slot's pixels changing and
                        gaining Laplacian detail AND 库存 losing exactly one
                        row — two facts that cannot both be faked by scenery

    Which is why fit_row() must stay hand-rolled rather than call
    InventoryControl.ensure_kit: ensure_kit verifies by matching the part's
    ICON TEMPLATE, and a part with no template yet would read as "cannot be
    proven". Running that here would throw away every capture of exactly the
    new attachment the run was started to photograph.

    `plate` gets LABEL_REQUESTED only when `arrived` says a weapon was watched
    ARRIVING — see plate_arrived. Identity was never the doubtful part: the
    spawner clicked one measured coordinate for one named weapon. What
    give_weapon()'s ok means is "the click went to the right entry index", not
    "a gun appeared", and nothing read it back without the plate OCR, which is
    the detector under test. A spawn that silently produced nothing therefore
    left the PREVIOUS weapon in front of the camera under the new name — the
    exact shape of ADS run 20260802_015545, 40 frames of the wrong gun.

    THE TEST THAT WAS REJECTED, and why this one is different. A crop diff of
    the plate cannot work: the plate sits on the translucent Tab panel, so its
    pixels change between backgrounds for the same gun and "new weapon" is
    indistinguishable from "new scenery". That reasoning stands. This is not a
    diff between two similar images — it is INK COUNT ON A CLEARED RACK, zero
    against several hundred, and an empty slot draws no plate at all whatever
    is behind it. Measured: 0 on 6 empty samples, 679-901 on 13 occupied, with
    one frame carrying both at once.

    Without `arrived` the crop still lands on disk with its requested key in
    the filename and in the entry's facts, so a human in the loop can use it;
    it simply is not machine ground truth.

    `type` gets no label either, for two reasons that each suffice. There is no
    asset to name — it is an ink count, not an icon. And "the Tab screen is up"
    is established by tab_open(), which reads THIS VERY REGION; labelling the
    crop from that would be the circularity this format exists to prevent.

    `by` records who established it, and `--as-is` is the interesting value.
    There the operator names what is already on the gun and nothing is spawned
    or dragged. That still counts as REQUESTED: the failure mode add_fit warns
    about is an ACTION that silently did not happen, and --as-is performs no
    action. Its only risk is a person typing the wrong key — a non-circular
    reading by the one detector no template can taint, which is precisely what
    --as-is exists to supply. `by: operator` keeps it distinguishable.
    """
    # No key, no label — never a label that names nothing. The rows pass
    # photographs 库存 BEFORE anything is fitted, which is the only moment the
    # parts are visible and also the moment nobody knows which row is which,
    # so it writes key=None and relabel() fills it in afterwards. Without this
    # guard those crops would land carrying `asset: None` as REQUESTED ground
    # truth: a claim to know, attached to the one thing not yet known.
    if not key:
        return []
    if target == 'slots':
        return [{'slot': slot, 'asset': key, 'source': LABEL_REQUESTED,
                 'by': by}]
    if target == 'rows':
        return [{'slot': 'inventory', 'asset': key, 'source': LABEL_REQUESTED,
                 'by': by}]
    if target == 'plate' and arrived:
        return [{'slot': 'plate', 'asset': key, 'source': LABEL_REQUESTED,
                 'by': by}]
    return []


# ════════════════════════════════════════════════════════════
# Collector
# ════════════════════════════════════════════════════════════

class Collector:
    """Owns the screen, the spawner and the drags for one run."""

    def __init__(self, rig, sc, ac, gun, run, targets, by='spawn'):
        self.rig, self.sc, self.ac = rig, sc, ac
        self.gun, self.run, self.targets = gun, run, targets
        self.by = by
        self.panel_box = tab_blocks()['right']
        # Built here rather than reached for through `ac`, on the rule
        # GunDriver states for the same detector: stateless and pixel-only
        # (device=None loads no model), so every caller wants the same one and
        # constructing it is cheaper than an accessor. `ac.tab.ink(...)` would
        # be one more reach past a high-level object, which is the thing this
        # pass exists to remove.
        self.type_det = TabTypeDetector()
        # The one reader of a slot that does not match an icon; see one_part.
        self.slots = SlotDetector()
        # Set by plate_pair once both guns are watched onto a cleared rack.
        # False here so a caller that never runs one -- or a target that is not
        # `plate` -- cannot accidentally claim ground truth.
        self.plate_arrived = False
        # The order spawn()'s clicks actually went out in. Empty until one
        # runs, so a caller reading it first gets nothing rather than a stale
        # round's answer.
        self.spawn_order = []
        # WHY NOTHING WAS COLLECTED IS A RESULT TOO, and it belongs in the
        # manifest rather than only on stdout. A grip run photographed six
        # parts, failed all six, and saved `entries: []` with an empty `bad`
        # list -- a record indistinguishable from a run that had nothing to do.
        # The reasons had been printed, and the console was gone. Every miss
        # below carries the numbers that separate the repairs.
        self.misses = []

    def miss(self, key, why, frame=None, before=None, **facts):
        """Record and print one part that was not collected. -> None

        `frame` writes the whole Tab screen beside the run. A crop proves what
        the crop shows and nothing about why: six grip rounds reported a slot
        reading `absent` — the tile not drawn at all — and absent has two
        completely different causes, this weapon having no such slot and there
        being no weapon in that rack row. One full frame separates them, and
        the earlier guess at "the backpack is full" was settled the same way,
        by a panel screenshot that had been on disk the whole time.
        """
        for tag, img in (('shot', frame), ('shot_before', before)):
            if img is None:
                continue
            name = f'miss_{len(self.misses):02d}_{key}_{tag}.png'
            try:
                os.makedirs(self.run.path, exist_ok=True)
                cv2.imwrite(os.path.join(self.run.path, name), img)
                facts[tag] = name
            except Exception as e:                       # never lose the miss
                facts[tag] = f'unwritable: {e}'
        self.misses.append({'key': key, 'why': why, **facts})
        detail = '  '.join(f'{k}={v}' for k, v in facts.items())
        print(f'    {key}: {why}' + (f'   {detail}' if detail else ''))
        return None

    def close(self):
        try:
            self.ac.close()
        except Exception:
            pass

    # ── screen ──

    def frame(self, flush=3):
        """A Tab frame, settled — a hovered slot draws a tooltip straight over
        the icon being photographed, and the panel fades in.

        InventoryControl.frame() parks the cursor itself; the flush is this
        collector's own concern, because it is the only caller that needs the
        panel to have finished animating rather than merely to be up.
        """
        for _ in range(flush):
            f = self.ac.frame()
        return f

    def crop(self, frame, slot):
        return cut(frame, f'att_{self.gun}_{slot}')

    def take_off(self, slot, timeout=1.5, quiet=False, known_filled=False):
        """Unequip `slot` and watch where it lands. -> row index | None

        RETURNS THE ROW, and that is the point. The caller needs to put this
        part back on at the next background, and the obvious way -- ask the
        detector to find it in 库存 -- is the circularity this whole file
        exists to avoid: `brake_ar`, `heavy_stock` and `variable` have no icon
        in the catalogue at all, so a template search can never find them, and
        they are precisely the three worth collecting. paired_sweep used
        find(key) and brake_ar died on it every time.

        Counting works for any part, named or not: 库存 gained exactly one row,
        and inserted_row says which one by comparing pictures.

        Verified, not assumed -- this was `unequip(); sleep(0.3)`, the one
        fire-and-forget action in the flow and the one that failed.
        """
        f0 = self.frame()
        # IS THERE A GUN IN THAT ROW AT ALL. Whether the SLOT is safe to touch
        # is InventoryControl.unequip's own business now -- it refuses a slot
        # whose tile says there is nothing in it, because either gesture aimed
        # at an empty slot reaches the weapon row and drops the gun. That guard
        # was written here first and moved: every caller of unequip has the
        # same hazard, and a copy in one script only protects one script.
        #
        # What stays here is the question unequip cannot answer, because it is
        # about the RACK and not the slot: blurred scenery behind an empty
        # panel reads `empty` as convincingly as a real empty slot, so the tile
        # gate is only meaningful once the name plate says a gun is there.
        if self.ac.plate_ink(self.gun, f0) < PLATE_INK_MIN:
            if not quiet:
                print(f'    no gun in rack slot {self.gun} — nothing to take '
                      f'{slot} off')
            return None
        before, n0 = self.crop(f0, slot).copy(), inv_rows(f0)
        rows0 = row_icons(f0, n0)
        rec = self.ac.unequip(self.gun, slot)
        if rec.get('slot_state') and not known_filled:
            if not quiet:
                print(f'    {slot}: {rec["error"]} — nothing was clicked')
            return None
        deadline = time.perf_counter() + timeout
        while True:
            f1 = self.frame(flush=2)
            n1 = inv_rows(f1)
            if change(before, self.crop(f1, slot)) >= CHANGE_MIN \
                    and n1 == n0 + 1:
                return inserted_row(rows0, row_icons(f1, n1))
            if time.perf_counter() >= deadline:
                # quiet=True is for "empty it if anything is there" -- nothing
                # coming off an already-empty slot is the expected case, not a
                # failure, and the two are indistinguishable from here.
                if not quiet:
                    print(f'    {slot} would not come off: slot moved '
                          f'{change(before, self.crop(f1, slot)):.1f}, 库存 '
                          f'{n0}->{n1}')
                return None
            time.sleep(FIT_POLL_S)

    def slot_crops(self, frame):
        """{slot: crop} for every attachment slot, copied.

        Copied because the caller holds them across another grab and the
        grabber reuses its buffers.
        """
        return {s: self.crop(frame, s).copy() for s in SLOT_NAMES}

    def tab(self):
        return bool(self.rig.ensure_inventory_open()) and bool(self.ac.sync())

    def turn(self, yaw, pitch):
        """Change what shows through the panel. Tab must be shut.

        With Tab up the mouse drives a cursor, not the view, and every capture
        comes back identical.
        """
        self.rig.ensure_inventory_closed()
        self.rig.view.turn(yaw, pitch, settle_s=SETTLE_S)

    def write(self, name, crop, **facts):
        """One crop into the run. Returns the entry the manifest now holds.

        The crop and everything known about it land in one call, which is the
        point of going through CaptureRun: there used to be a `write()` that
        saved the pixels and a separate `_shot()` that built the record, and
        nothing tied the two together — a panel__*.png was written with no
        record at all, and still is not in any index of the old runs.
        """
        return self.run.add(crop, name, **facts)

    # ── spawning ──

    def bare_host(self, weapon, backpack):
        """Get `weapon` into the rack wearing NOTHING. -> bool

        Three steps, in an order that is the whole point:

          1. spawn the gun. It does not arrive bare -- it picks up whatever
             fits out of the backpack as it appears, which is this file's
             oldest recorded quirk and was measured again on the smoke run: an
             sks asked for with one comp_ar came out wearing a 6x scope, a
             suppressor, an extended magazine and a cheek pad.
          2. strip it TO THE FLOOR, not into 库存. Into 库存 would leave those
             parts sitting in the list this round is about to index, and worse,
             a stripped suppressor and a requested comp_ar are both muzzles --
             so the slot that fills would no longer name which one landed.
          3. only then spawn the round's parts, into a 库存 that holds nothing
             else.

        After this every slot is empty, so every fit is a filling rather than a
        swap: the crop always changes, and 库存 always loses exactly one row.
        Both halves of fit_row's check become true by construction instead of
        being loosened until they pass.
        """
        # THE RACK EMPTY FIRST, so "which slot holds a gun" has one answer.
        # gun_slot() returns the FIRST rack slot that draws its boxes, and with
        # two guns racked that is whichever came earlier -- not this round's
        # host. Round 8 swapped slr for ump45, the slr stayed in slot 1, and
        # every read and every unequip for the next three rounds pointed at the
        # slr's empty muzzle: "would not come off: slot moved 3.5, 库存 8->8",
        # which is exactly what an empty slot looks like.
        if not self.tab():
            print('    [!] the inventory would not open to clear the rack')
            return False
        self.ac.clear_rack()

        if not self.spawn(weapon, [], backpack):
            return False
        if not self.tab():
            print('    [!] the inventory would not open to strip the host')
            return False
        # Where it actually landed, before anything reads a slot off it. --gun
        # is a starting guess: an empty rack takes the first gun into slot 1,
        # and re-entering the range empties the rack.
        g = self.ac.gun_slot()
        if g is None:
            print(f'    [!] no gun in either rack slot after spawning '
                  f'{weapon}')
            return False
        # AND THE PLATE HAS TO HAVE INK ON IT. gun_slot() answers "which row
        # draws its boxes", which is one frame's worth of gradient and can be
        # satisfied by the blurred scenery behind an EMPTY panel -- so can
        # SlotDetector, which called that same empty panel's muzzle and stock
        # `empty`. read_slots went further and named four attachments on a rack
        # with no gun in it, and the round then photographed nothing for six
        # parts running while every log line looked healthy.
        #
        # The name plate does not have that failure mode: 0 ink on an empty
        # row, 679-901 with a gun, in 6 and 13 measured samples, including one
        # frame carrying both. `plate_arrived`'s band is the same fact.
        ink = self.ac.plate_ink(g, self.frame(flush=2))
        if ink < PLATE_INK_MIN:
            print(f'    [!] rack slot {g} draws boxes but its name plate has '
                  f'{ink} ink — there is no gun there. Nothing after this '
                  f'would be a reading of {weapon}.')
            return False
        if g != self.gun:
            print(f'    the gun is in rack slot {g}, not {self.gun} — '
                  f'reading slot {g}')
        self.gun = g
        # to=at_ground(): the floor, by drag. unequip's default sends it to
        # 库存 by right click, which is the one destination that must not be
        # used here.
        rec = self.ac.strip(self.gun, to=at_ground())
        if rec.get('worn'):
            print(f'    host arrived wearing {", ".join(rec["worn"])} — '
                  f'stripped to the floor')
        left = [s for s, v in self.ac.read_slots(self.gun).items() if v]
        if left:
            # A warning, not a refusal. This used to fail the whole round, and
            # a stuck MAGAZINE then cost a run that was only collecting a
            # muzzle. Only the slot a part is going into has to be empty, and
            # one_part checks exactly that slot for exactly that part.
            #
            # Some of these may not be strippable at all -- a gun arrives with
            # a magazine and that one has never been watched coming off. Not
            # claimed either way here; it is simply not this method's business.
            print(f'    still wearing {left} after the strip — fine unless a '
                  f'part is collected for one of those slots')
        return True

    def spawn(self, weapon, keys, backpack):
        """Spawn host weapon(s) and/or a list of parts. One panel visit.

        `weapon` may be a list. Into an EMPTY rack the first gun spawned takes
        row 1 and the second takes row 2, so two guns in one visit still have
        known addresses -- see plate_pair, which is the reason this accepts
        more than one.

        Callers wanting a host for FITTING want bare_host(), which is this plus
        the strip that makes the gun actually bare.
        """
        weapons = ([weapon] if isinstance(weapon, str)
                   else list(weapon or []))
        want = ([BACKPACK] if backpack else []) + weapons + list(keys)
        if not want:
            return True
        # ONE CALL, and it decides the route. give_many groups by category so
        # a category opens once instead of once per item, orders the visit so
        # no coordinate goes stale behind an expanded submenu, and proves each
        # category actually opened before clicking anything under it.
        #
        # This used to be a loop over give_weapon()/give_attachment(). Each of
        # those returns the panel to fully collapsed, so two ARs paid two
        # category trips -- and the second one landed on the category and
        # never reached the gun. The entry point that avoids exactly this has
        # existed the whole time; the loop simply did not use it.
        #
        # `spawn_order` is the order the clicks actually went out in, which is
        # NOT the order asked for: plan() sorts within a column bottom-up and
        # by entry index. Anything relying on "first spawned" -- plate_pair
        # does, for which rack row a gun lands in -- has to read this rather
        # than the request.
        # switch=False: give_many's default presses 2 afterwards, which is for
        # a caller that wants slot 2 in hand without reading the rack. Nothing
        # here wants that -- bare_host and plate_pair both read which row the
        # gun landed in, and a key press that changes what is in hand is one
        # more thing happening between a spawn and the frame that judges it.
        rec = self.sc.give_many(want, switch=False)
        if not rec['ok']:
            # One re-read and one more try, which is what the per-item loop
            # did: the usual cause is a category refusing to expand because a
            # different one is, and a fresh sync clears it.
            print(f"    [!] spawner: {rec['error']} — re-reading and retrying")
            self.sc.menu = None
            rec = self.sc.give_many(want, switch=False)
        self.spawn_order = [s['key'] for s in rec['steps']
                            if s['kind'] == 'weapon' and s['clicked']]
        missed = [s['key'] for s in rec['steps'] if not s['clicked']]
        if missed:
            print(f"    [!] never clicked: {', '.join(missed)}")
        time.sleep(SPAWN_SETTLE_S)
        return rec['ok']

    # ── fitting ──

    def relabel(self, shots, found):
        """Attach the discovered row->key mapping to captures already taken.

        The rows pass photographs 库存 before anything is fitted, which is the
        only moment the parts are there to photograph — and the moment nobody
        yet knows which row is which. So those entries go down with the row
        recorded and no label, and this fills them in once the fits have said.

        A row the fits could not name keeps NO label rather than a guess. That
        is the same rule as everywhere else here: an unlabelled crop is still
        useful to a human, a wrongly labelled one poisons the template it is
        used to fit.
        """
        n = 0
        for s in shots:
            if s.get('target') != 'rows':
                continue
            key = found.get(s.get('row'))
            if key is None:
                continue
            s['key'] = key
            s['capture_key'] = key
            s['labels'] = label_for('rows', key, None, self.by)
            s['ok'] = bool(s.get('read')) and s['read'] == key
            n += 1
        if n:
            print(f'    labelled {n} row crop(s) from what the fits revealed')

    def paired_sweep(self, weapon, key, slot, angles, tag_n, row):
        """Per background, the same slot EMPTY and then FILLED. -> [entries]

        WHY PAIRS AND NOT JUST THE FILLED CROP. A composited crop is not the
        icon -- it is the icon blended over whatever was behind the panel:

            c = a*icon + (1-a)*backdrop

        Two unknowns per pixel, `a` and `icon`. Storing `c` as the template
        bakes in one particular backdrop, and it then scores badly against any
        other scene, which is the whole reason blend_attachment exists.

        With the EMPTY slot at the same instant, `backdrop` is measured rather
        than modelled, and each background gives one equation per pixel. Two
        backgrounds are enough to solve; six make it a least-squares fit with
        the antialiasing averaged out.

        The pairing has to be at the SAME angle, which is why this exists
        instead of two passes of sweep(). sweep() advances the yaw between
        backgrounds on purpose, so an empty pass and a filled pass would be
        photographing different scenes and could not be subtracted.

        Two right clicks per background and no drags: equipping is the
        measured 4/4 gesture, and unequipping to 库存 is a right click too
        (unequip's own 'auto'), so the pair costs about 0.35 s on top of the
        turn.
        """
        shots = []
        for a in range(angles):
            pitch = PITCH_STEPS[a % len(PITCH_STEPS)]
            self.turn(TURN_COUNTS, pitch)
            if not self.tab():
                self.miss(key, 'the inventory would not open mid-sweep',
                          slot=slot, background=a, kept=len(shots) // 2)
                break
            tag = f'p{a}'

            # EMPTY first: this frame IS the backdrop for the pair. Held in
            # memory, NOT written yet -- a backdrop is only meaningful next to
            # the filled crop it explains, and writing it first left an orphan
            # on disk for every part whose equip then failed. `shots.pop()`
            # takes an entry out of the list; the file and its manifest row
            # are already saved by then.
            f0 = self.frame()
            before = self.crop(f0, slot).copy()

            # The row is KNOWN -- take_off returned it. Not find(key), which
            # is a template search and cannot see a part whose icon is the
            # thing being collected.
            if row is None or not self.ac.auto_equip(at_inv(row)):
                self.miss(key, 'no known 库存 row to equip at this background '
                               '— the part is not where the last unequip put '
                               'it', slot=slot, background=a, row=row,
                          kept=len(shots) // 2)
                break
            time.sleep(FIT_TIMEOUT_S)

            f1 = self.frame(flush=2)
            after = self.crop(f1, slot)
            if change(before, after) < CHANGE_MIN:
                self.miss(key, 'the slot did not change — the equip did not '
                               'land, so this pair is not a pair', frame=f1,
                          slot=slot, background=a,
                          moved=round(change(before, after), 1),
                          kept=len(shots) // 2)
                break
            # Both, now that they are a pair.
            region = HUD_REGIONS[f'att_{self.gun}_{slot}']
            shots.append(self._shot(
                before, f'{key}__{slot}__{weapon or "none"}__{tag}bg.png',
                'backdrop', key, region, '', False, slot=slot, angle=a))
            shots.append(self._shot(
                after, f'{key}__{slot}__{weapon or "none"}__{tag}fg.png',
                'slots', key, region,
                BY_ASSET.get(self.ac.read_slots(self.gun).get(slot, ''), ''),
                self._has(key), slot=slot, angle=a))

            # Back to 库存 for the next background. Right click, per
            # unequip('auto') -- the drag lands on the floor instead.
            row = self.take_off(slot, known_filled=True)
            self.turn(0, -pitch)
        return shots

    # ── capturing ──

    def _shot(self, crop, name, target, key, region, read, has_tmpl,
              slot=None, **extra):
        """Save one crop with its record and its label. -> the manifest entry.

        `read` is what the CURRENT detector made of it and `ok` is whether that
        agreed — the coverage question report() answers. Neither is the label:
        the label comes from how the crop was PRODUCED (label_for), which is
        the only account of it that a broken template cannot corrupt.
        """
        return self.write(name, crop, target=target, key=key,
                          region=list(region), read=read,
                          has_template=has_tmpl, ok=has_tmpl and read == key,
                          labels=label_for(target, key, slot, self.by,
                                           arrived=self.plate_arrived),
                          **({} if slot is None else {'slot': slot}), **extra)

    def capture(self, weapon, keys, rows, tag, tag_n, row_key=None):
        """One background. Returns the manifest entries it added.

        Every target checks whether it has anything to photograph, so the same
        call works before fitting (rows full, slots blank) and after (the
        reverse) with no phase flag.
        """
        if not self.tab():
            print('    [!] the inventory would not open')
            return []
        # Naming the gun narrows each slot's template bank to what it can hold,
        # which is what separates 消音器(冲锋枪) from 消音器(突击步枪). Set by
        # hand, not read: three plates cannot be read, and this must work then.
        self.ac.guns = {1: None, 2: None,
                        self.gun: weapon if weapon in ROSTER else None}
        frame = self.frame(flush=4)
        shots, wname = [], weapon or 'none'

        if 'slots' in self.targets and keys:
            read = self.ac.read_slots(self.gun)
            for key in keys:
                slot = ATTACHMENTS[key]['slot']
                region = HUD_REGIONS[f'att_{self.gun}_{slot}']
                cell = self.crop(frame, slot)
                if detail(cell) < SLOT_DETAIL_MIN:
                    continue            # not fitted yet, or the slot is bare
                shots.append(self._shot(
                    cell, f'{key}__{slot}__{wname}__{tag}.png',
                    'slots', key, region,
                    BY_ASSET.get(read.get(slot, ''), ''), self._has(key),
                    slot=slot))

        if 'rows' in self.targets and rows:
            found = self.ac.look(frame).inventory
            for row in rows:
                x0, y0, x1, y1 = icon_box(row, 'inventory')
                item = found[row] if row < len(found) else None
                # No key in the name and no label: at this moment nobody knows
                # which part sits in this row, and the game's own sort is why.
                # relabel() fills both in once the fits have said.
                #
                # THE ROUND IS IN THE NAME BECAUSE ROW 0 IS NOT ONE THING. Each
                # round racks a different part, so `row00 at lbg0` names a
                # different picture every round. Without `r{n}` the second
                # round overwrote the first round's file and left its manifest
                # entry pointing at the new pixels under the old key — silently,
                # since both are real row crops of real parts. Seven runs on
                # disk carry the damage; CaptureRun.conflicts() finds it and
                # labelled() refuses to hand any of it out.
                # THE PART IS IN THE NAME, and the comment above is why it
                # has to be. Adding the round fixed collisions BETWEEN rounds
                # and left the ones WITHIN one: each part of a round is staged
                # alone into an empty 库存, so each lands at row 0, so every
                # part of round 1 wrote `row00__sks__r1__lbg0.png`. Three keys
                # per file, 70 of 272 captures in the 2026-08-04 run.
                #
                # `row_key` is the caller's, not a reading: the rows pass is
                # per-part and knows which part it just staged (see the block
                # that calls sweep with one row). Where it is not known the
                # name falls back to the row, and CaptureRun.add now refuses a
                # repeat outright rather than overwriting.
                stem = f'row{row:02d}__{wname}__r{tag_n}'
                if row_key:
                    stem += f'__{row_key}'
                shots.append(self._shot(
                    frame[y0:y1, x0:x1], f'{stem}__{tag}.png',
                    'rows', None, (y0, x0, y1 - y0, x1 - x0),
                    (item.key or '') if item else '', False, row=row))

        if 'type' in self.targets:
            # Its own grab: 类型 sits at y=129, just above the block TabGrabber
            # copies, and widening that block for 18x41 px would cost every
            # frame of every run that uses it.
            cell = win32_cap(HUD_REGIONS['type'])
            # TabTypeDetector.ink, not a count computed here. This used to be
            # cvtColor(BGR2GRAY) -- a luma average -- compared against
            # TAB_COUNT_MIN/MAX, which are measured on the CHANNEL MAXIMUM.
            # Averaging three channels to find white ink dilutes it, so every
            # count came out low against bounds that assumed otherwise and the
            # `ok` flag below was wrong in one direction. The detector owns the
            # number now; there is one definition of it.
            ink = self.type_det.ink(cell)
            s = self._shot(cell, f'type__{wname}__{tag}.png',
                           'type', 'type', HUD_REGIONS['type'], str(ink), True,
                           ink=ink)
            # In place, so the manifest holds the ink-band verdict rather than
            # the key-vs-read one _shot computes for the icon targets. The
            # entry is the run's own dict, so this reaches the file on the next
            # save() — which the next add() does.
            s['ok'] = TAB_COUNT_MIN <= ink <= TAB_COUNT_MAX
            shots.append(s)

        # The whole weapon panel: the detector's opinion and the picture
        # disagreeing is the thing worth catching, and a human glance settles
        # it in a second. See tools/verify_kit.py. Recorded as a capture with
        # no labels — it is for a human, and nothing established what is in it.
        y, x, h, w = self.panel_box
        # Same naming rule as the row crops above and for the same reason: a
        # round photographs its parts one at a time, so round+background does
        # not identify a capture. This one carries no labels, so a collision
        # cost no ground truth — but it left 270 manifest entries in the
        # 2026-08-04 run pointing at pixels from a later part, which is a lie
        # about what a human is looking at. Found by CaptureRun.add's
        # overwrite guard the first time it ran.
        stem = f'panel__{wname}__r{tag_n}'
        if row_key:
            stem += f'__{row_key}'
        self.write(f'{stem}__{tag}.png', frame[y:y+h, x:x+w],
                   target='panel', weapon=wname, round=tag_n,
                   key=row_key or None, labels=[])
        return shots

    @staticmethod
    def _has(key):
        return bool(ATTACHMENTS[key].get('asset'))

    # ── one round ──

    def sweep(self, weapon, keys, rows, angles, tag_n, pass_tag,
              row_key=None):
        shots = []
        for a in range(angles):
            pitch = PITCH_STEPS[a % len(PITCH_STEPS)]
            self.turn(TURN_COUNTS, pitch)
            got = self.capture(weapon, keys, rows, f'{pass_tag}bg{a}',
                               tag_n, row_key=row_key)
            by = {}
            for s in got:
                h, n = by.get(s['target'], (0, 0))
                by[s['target']] = (h + bool(s['ok']), n + 1)
            print(f'    {pass_tag}bg{a}: ' + ('  '.join(
                f'{t} {h}/{n}' for t, (h, n) in sorted(by.items()))
                or 'nothing captured'))
            shots += got
            self.turn(0, -pitch)        # undo the pitch, keep the yaw
        return shots

    def plate_pair(self, weapons, angles, tag_n):
        """Two weapons' NAME PLATES over many backgrounds. -> [entries]

        TWO AT A TIME BECAUSE THE RACK HOLDS TWO. Both plates are drawn in the
        same frame -- one akm capture reads 682 ink on row 1 and 0 on row 2 at
        the same instant -- so a background that has been turned to costs one
        turn and yields two plates. Turning is what a run spends its time on;
        a second crop out of a frame already grabbed is free. 30 weapons: 15
        rounds instead of 30.

        SPAWNED ONCE, NOT PER BACKGROUND. What is behind the panel changes
        when the view turns; what is IN THE RACK does not. So the round racks
        both guns at the start and then only ever does

            Tab off -> turn -> Tab on -> two crops

        for every background after that. The first version cleared the rack and
        re-spawned both guns at every angle, which bought nothing and cost a
        spawner round trip and two drops per background -- roughly ten times
        the work for an identical set of crops.

        WHICH ROW HOLDS WHICH GUN IS WATCHED, NOT ASSUMED. The guns go in one
        at a time and the plate ink says where each landed -- 0 on an empty
        row, 679-901 with a gun. Reading the plate to find out would be the
        circularity this file exists to avoid: these are the templates that
        would be doing the reading.

        NO PAIRED BACKDROP, and that is deliberate rather than an omission.
        The icons need one because a slot icon is alpha-blended over the scene
        and cannot be recovered without it. A name plate is read through a
        NEAR-WHITE, ACHROMATIC MASK instead (TabWeaponDetector._white_text_mask,
        the same mask plate_ink counts), so the scene behind it is thresholded
        away rather than mixed in. Pairing would mean emptying the rack at each
        angle, which is exactly the drop-and-respawn this method stopped doing,
        and the two passes cannot be aligned afterwards -- turn() is open-loop,
        which is why paired_sweep pairs within one angle instead of across two
        passes. What backgrounds buy here is the mask's robustness against
        bright scenery leaking into it, and that needs many scenes, not pairs.
        """
        shots = []
        if not self.tab():
            return self.miss(','.join(weapons), 'the inventory would not open')

        # ── both rows emptied by RIGHT CLICK, then both guns racked ──
        #
        # THE ORDER IS THE ADDRESS: into an empty rack, the first gun spawned
        # takes row 1 and the second takes row 2. That is what makes one
        # spawner trip enough for two guns -- with the rule, nothing has to be
        # read between them, and reading between them is the only alternative
        # (the plate itself cannot be asked; these are its templates).
        #
        # The rule is USED, and its precondition and its result are both still
        # checked, because a rule that silently did not apply would mislabel
        # every crop in the round:
        #
        #   before   both rows read 0 ink, or there is no "empty rack" for the
        #            order to be defined against
        #   after    both rows read a plate, or fewer than two guns arrived and
        #            nothing here can say which one is missing
        self.ac.clear_rack()
        f = self.frame()
        inks = {g: self.ac.plate_ink(g, f) for g in (1, 2)}
        blank = {g: self.ac.plate_ink(g, f) for g in (1, 2)}
        if any(v >= PLATE_INK_MIN for v in blank.values()):
            return self.miss(','.join(weapons), 'the rack would not clear, so '
                                                'first-spawned-is-row-1 has no '
                                                'empty rack to count from',
                             ink=blank, frame=f)
        if not self.spawn(weapons, [], False) or not self.tab():
            return self.miss(','.join(weapons),
                             'the spawner would not produce the pair')
        f = self.frame()
        inks = {g: self.ac.plate_ink(g, f) for g in (1, 2)}
        # plate_arrived and not `>= PLATE_INK_MIN`: it carries the CEILING too.
        # The mask is only "near-white and achromatic", so with nothing to dim
        # it this region saturates on bright scenery, and a reading above the
        # band is refused rather than believed.
        rows = [g for g in (1, 2) if plate_arrived(blank[g], inks[g])]
        if len(rows) != len(weapons):
            return self.miss(','.join(weapons),
                             f'{len(rows)} of {len(weapons)} guns reached the '
                             f'rack — with one missing, the order cannot say '
                             f'which row belongs to which weapon',
                             ink=inks, blank=blank, frame=f)
        # THE ORDER CLICKED, not the order asked for. give_many sorts within a
        # column bottom-up and by entry index, so `weapons` and the click
        # sequence routinely differ -- akm and m762 share the AR category and
        # come out in index order whichever way round they were named. Zipping
        # the request against the rows would then label every crop in the round
        # with the other gun's name, and nothing downstream could tell.
        order = [w for w in self.spawn_order if w in weapons]
        if len(order) != len(weapons):
            return self.miss(','.join(weapons),
                             f'the spawner clicked {order}, not all of '
                             f'{weapons} — cannot map guns to rows',
                             ink=inks, frame=f)
        landed = dict(zip(order, rows))
        print('    ' + ', '.join(f'{w}=row{g}' for w, g in landed.items()))

        # Every crop below is of a gun watched onto a rack that read 0 ink and
        # then read a plate. That IS the arrival label_for() insists on before
        # it will call a plate crop ground truth, and it is established once
        # here rather than re-argued per background -- nothing between the
        # backgrounds touches the rack.
        self.plate_arrived = True

        for a in range(angles):
            pitch = PITCH_STEPS[a % len(PITCH_STEPS)]
            self.turn(TURN_COUNTS, pitch)       # turn() closes Tab to do it
            if not self.tab():
                self.miss(','.join(weapons), 'the inventory would not reopen '
                                             'after turning', background=a)
                break
            f = self.frame()
            read = self.ac.read_weapons(f)
            gone = [w for w, g in landed.items()
                    if self.ac.plate_ink(g, f) < PLATE_INK_MIN]
            if gone:
                # A gun cannot leave the rack by itself. If one has, every crop
                # after it would be of an empty row under this weapon's name.
                self.miss(','.join(gone), 'the rack lost a gun mid-sweep — '
                                          'stopping rather than photographing '
                                          'an empty row under its name',
                          background=a, frame=f)
                break
            for w, g in landed.items():
                shots.append(self._shot(
                    cut(f, f'gun_name_{g}'), f'{w}__plate{g}__bg{a}.png',
                    'plate', w, HUD_REGIONS[f'gun_name_{g}'],
                    read.get(g) or '', True, slot='plate', angle=a, row=g,
                    ink=self.ac.plate_ink(g, f)))
            hit = sum(1 for w, g in landed.items() if read.get(g) == w)
            print(f'    bg{a}: {hit}/{len(landed)} read correctly')
            self.turn(0, -pitch)
        return shots

    def one_part(self, weapon, key, angles, tag_n):
        """Collect ONE attachment, in both renderings. -> [entries] or None

        The identity of every crop rests on one fact: exactly one thing was
        spawned, so whatever appeared is that thing. No template is read and no
        row ordering is assumed -- the two mechanisms that produced 228
        mislabelled crops and a run of empty slot captures.

        Both states are arranged rather than hoped for, using the measured
        auto-fit rule (tools/probe_autofit.py, 3/3 each way):

            with the slot ALREADY FULL  the part lands in 库存
            with the slot EMPTY         the part lands on the gun

        so `rows` is photographed with a blocker in the slot, and `slots` after
        equipping. `rows` first, because 库存 only holds it until it is fitted.
        """
        slot = ATTACHMENTS[key]['slot']
        shots = []
        if not self.tab():
            return None

        # ── 1. an empty slot, so the first copy auto-fits onto the gun ──
        # To the floor, not 库存: a part sitting in the list would be a second
        # row, and this whole method rests on there being exactly one.
        # IN HAND before anything is spawned. probe_autofit measured "empty
        # slot -> the part lands on the gun" 3/3 with the weapon HELD, and the
        # first run of this method got 库存 instead with an empty slot and the
        # gun merely racked. One variable between them. So the auto-fit looks
        # like the right click: it reaches the weapon in hand, not whichever
        # one is in the rack.
        # THE GUN HAS TO STILL BE THERE AFTER EVERY STEP, and which step loses
        # it is the whole question. bare_host ends with a plate carrying ink,
        # and by the time the part is spawned the plate reads 0 with the weapon
        # panel not drawn at all — so something between them empties the rack,
        # and the three candidates need three different repairs. Asking after
        # each one costs a name-plate crop.
        # WATCHED, not grabbed once. hold() closes Tab and opens it again, and
        # the panel FADES IN -- a single grab lands before the plate is drawn
        # and reads 0 ink, which is indistinguishable from the gun being gone.
        # The first version did exactly that and reported 22 phantom losses in
        # one run, 14 of them straight after hold(). gun_slot() carries the
        # same watch for the same reason; this is that lesson, repeated.
        def still_here(step, timeout=GUN_WATCH_S, **extra):
            deadline = time.perf_counter() + timeout
            while True:
                ink = self.ac.plate_ink(self.gun, self.frame(flush=2))
                if ink >= PLATE_INK_MIN:
                    return True
                if time.perf_counter() >= deadline:
                    # WHICH of the three it is, recorded rather than guessed.
                    # "plate reads 0" has been logged 74 times across 11 runs
                    # and never once said whether the gun moved rack slots,
                    # fell on the floor, or was simply not being looked at —
                    # and the repairs are different for each. The poll above
                    # already rules out the fade-in, so the answer is one of:
                    #
                    #   other slot has ink  -> self.gun is stale, not a loss
                    #   Tab is shut         -> the plate region is game world
                    #   neither             -> the gun really is gone, and the
                    #                          full screen shows where to
                    #
                    # The full screen matters: frame() is a banded grab, so
                    # whether the Tab panel was even up is invisible in it.
                    f = self.frame(flush=2)
                    both = {g: self.ac.plate_ink(g, f) for g in (1, 2)}
                    try:
                        tab = bool(self.ac.tab_open())
                    except Exception as e:
                        tab = f'unreadable: {e}'
                    self.miss(key,
                              f'the gun left rack slot {self.gun} during '
                              f'"{step}"', frame=capture_screen(), plate=ink,
                              step=step, watched_s=timeout,
                              plate_both=both, tab_open=tab, **extra)
                    return False
                time.sleep(FIT_POLL_S)

        self.ac.held = None
        if not self.ac.hold(self.gun):
            return self.miss(key, f'could not take gun{self.gun} in hand')
        if not still_here('hold'):
            return None
        # EVERYTHING OFF, TO THE FLOOR. strip() picks its slots from the TILES
        # now, not from read_slots -- a part with no template used to be
        # invisible to it and stay on the gun, which is every part this file
        # exists to collect.
        #
        # The floor and not 库存: a part sitting in the list would be a second
        # row, and the identity of everything below rests on there being
        # exactly one.
        # The record is KEPT, not discarded. `strip` loses the host gun often
        # enough to be the collector's largest single failure (74 across 11
        # runs, 2026-08-04) and every one of those misses said only "the gun
        # left rack slot N" — which slot's unequip preceded it, what gesture
        # it used and what it read back were all thrown away here.
        strip_rec = self.ac.strip(self.gun, to=at_ground())
        if not still_here('strip', strip=strip_rec):
            return None
        # NOTHING TOUCHES A SLOT THAT DOES NOT READ `filled` -- in ANY gesture.
        #
        # The version before this one dragged the target slot blind, on the
        # reasoning that "a drag from an empty slot picks up nothing" while a
        # right click reaches the weapon underneath. That reasoning was never
        # measured, and it is wrong: 11 of one run's 35 misses were the host
        # gun disappearing at exactly this step. A drag that starts on an empty
        # slot takes hold of the weapon row instead, and dragging a weapon left
        # is drop_weapon's other measured gesture -- 1/1, the whole gun on the
        # floor. Same trap as the right click, one gesture along.
        #
        # So the rule is about the SLOT, not the gesture: if the tile does not
        # say something is in it, leave it alone. take_off carries the same
        # gate and does the clearing when there is something to clear.
        #
        # `scope` reads `unknown` forever (no tile is drawn there), so it is
        # never cleared here. That is a real limit, not a fix: a sight the
        # spawn auto-fitted stays on, the next one goes to 库存, and the
        # landing check below reports it with the row count. Nine sights need
        # a mechanism that does not exist yet; photographing them against an
        # unknown slot state would be worse than not photographing them.
        f0 = self.frame()
        if self.slots.classify(f0, self.gun).get(slot) == 'filled':
            self.take_off(slot, timeout=0.6, quiet=True, known_filled=True)
            if not still_here('clear the slot'):
                return None
            f0 = self.frame()

        # AND IT HAS TO HAVE WORKED. `absent` is not a pass: this weapon does
        # not have the slot the catalogue says it does, and the part would land
        # somewhere else and be photographed under the wrong name.
        #
        # `unknown` IS a pass, and only scope ever returns it. Nothing can be
        # read there, so the round proceeds and the landing check below is what
        # judges it -- reporting a part that went to 库存 instead, with the row
        # count, rather than photographing the wrong thing.
        state = self.slots.classify(f0, self.gun).get(slot)
        if state in ('filled', 'absent'):
            return self.miss(
                key,
                'the slot would not empty — the part in it did not come off'
                if state == 'filled' else
                f'{weapon} draws no {slot} tile — the catalogue says it has '
                f'one, and the screen wins',
                frame=f0, slot=slot, state=state,
                scores=self.slots.scores(f0, self.gun)[slot])

        n0 = inv_rows(f0)
        slots0 = self.slot_crops(f0)
        states0 = self.slots.classify(f0, self.gun)

        if not self.spawn(None, [key], False) or not self.tab():
            return self.miss(key, 'the spawner would not produce it, or Tab '
                                  'would not open afterwards')
        # WHICH SLOT WENT FROM NOT-FILLED TO FILLED. Not "does read_slots name
        # something there": that matches the part's ICON TEMPLATE, and for
        # brake_ar, heavy_stock and variable the catalogue has no asset at all,
        # so it could only ever answer "empty" and those three would never be
        # collectable. Same circularity, one method along.
        #
        # The tile transition is ABSOLUTE -- each end read on its own frame --
        # where the frame difference below is RELATIVE, and the difference is
        # not academic. A thumb_grip landed in the grip slot with the tile
        # reading filled at 411 edges, while the muzzle crop had moved 49.6
        # against the grip's 17.1 (the previous part leaving it), so the
        # difference-based winner called it a muzzle and threw the part away.
        # Scenery, a tooltip, or the slot next door emptying all move pixels;
        # none of them make a tile read filled.
        #
        # The difference survives as the FALLBACK, for `scope` alone. That
        # position draws no tile, so its state is `unknown` at both ends and no
        # transition can ever be seen there.
        f1 = self.frame()
        moved = {s: change(slots0[s], self.crop(f1, s)) for s in SLOT_NAMES}
        states1 = self.slots.classify(f1, self.gun)
        gained = [s for s in SLOT_NAMES
                  if states1[s] == 'filled' and states0[s] != 'filled']
        if gained:
            landed = gained if len(gained) == 1 else []
        elif states1.get(slot) == 'unknown':
            landed = [winner(moved)] if winner(moved) else []
        else:
            landed = []
        if landed != [slot]:
            # REPORT, do not assert. "it did not arrive" and "the catalogue has
            # the wrong slot" and "it went to 库存 instead" are three different
            # repairs, and the difference is visible right here: the row count
            # says whether anything spawned at all, and the other slots say
            # whether it landed somewhere else.
            n1 = inv_rows(f1)
            after = states1.get(slot)
            if len(gained) > 1:
                # Two tiles filled from one spawn. Nothing here can say which
                # one is this part, and naming either is how 228 crops went out
                # under the wrong labels.
                why = (f'{len(gained)} slots filled at once ({gained}) — one '
                       f'spawn cannot have landed in two, so the round before '
                       f'this one left something half-done')
            elif n1 > n0 and not landed:
                # The autofit rule says 库存 means the slot was occupied. That
                # is a claim about the slot, so the slot is read -- by tile
                # geometry, the same instrument that gated this above. If it
                # says `empty`, the rule is what is wrong, not the strip, and
                # `slot_now` in the manifest is the evidence either way.
                why = ('it went to 库存 instead. Per the autofit rule that '
                       'means the slot was not empty when it arrived'
                       + (', and the slot does read filled — the strip did not '
                          'take' if after == 'filled' else
                          f'. But the slot reads {after}, so the autofit rule '
                          f'is what does not hold here'))
            elif not landed:
                why = ('nothing moved anywhere. The spawner click reported ok, '
                       'which only means it went to the right entry index')
            else:
                why = (f'it landed in {landed}, not {slot}. The catalogue and '
                       f'the game disagree about this part\'s slot; nothing is '
                       f'collected rather than mislabelled')
            return self.miss(key, why, frame=f1, before=f0, slot=slot,
                             landed=landed or None,
                             inv=f'{n0}->{n1}', gun=self.gun,
                             # The one number that says whether a gun is in
                             # that row at all. Read at BOTH ends, because a
                             # rack that empties between them is a different
                             # bug from one that was empty to begin with.
                             plate=(self.ac.plate_ink(self.gun, f0),
                                    self.ac.plate_ink(self.gun, f1)),
                             # BOTH rack rows. `absent` on the row being read
                             # means "no tile drawn", and a gun sitting in the
                             # OTHER row is one of the two things that causes
                             # it -- the one a single row can never show.
                             rack={g: self.slots.classify(f1, g)
                                   for g in (1, 2)},
                             scores=self.slots.scores(f1, self.gun)[slot],
                             moved={s: round(v, 1) for s, v in moved.items()})
        # ── 2. the slot rendering, paired so the template can be SOLVED ──
        # One composited crop cannot separate the icon from what was behind
        # it; see paired_sweep. The part goes back to 库存 first because the
        # pairing starts from an empty slot and equips from the list at each
        # background.
        if 'slots' in self.targets:
            row = self.take_off(slot, known_filled=True)
            if row is None:
                self.miss(key, 'could not get it off the gun to start the '
                               'pairing', slot=slot)
                return shots
            shots += self.paired_sweep(weapon, key, slot, angles, tag_n, row)

        # ── 3. the 库存 rendering, from where paired_sweep left it ──
        # No second copy is spawned. paired_sweep ends on an unequip, so the
        # part is ALREADY the newest row in 库存 -- and spawning another would
        # find the slot empty and auto-fit it onto the gun instead, which is
        # exactly what happened: "库存 went 1->1, wanted one more row".
        #
        # Which row it is comes from watching it arrive: photograph 库存 with
        # the part on the gun, unequip, and the row that changed is it. The
        # game inserts into its own sort order, so the newest row is not the
        # last one -- assuming it was is what produced seven photographs of the
        # same leftover under seven different names.
        if 'rows' in self.targets:
            if not self.tab():
                return shots
            # `row` is where take_off last put it -- known, not searched.
            # auto_equip_key(key) was a template lookup, and a part with no
            # icon in the catalogue can never be found that way. Those three
            # are the whole reason this collector exists.
            if row is None or not self.ac.auto_equip(at_inv(row)):
                self.miss(key, 'no known 库存 row to stage the row capture',
                          row=row)
                return shots
            time.sleep(FIT_TIMEOUT_S)
            row = self.take_off(slot, known_filled=True)
            if row is None:
                self.miss(key, 'it would not come back off for the 库存 pass',
                          slot=slot)
                return shots
            rows_shots = self.sweep(weapon, [], [row], angles, tag_n, 'l',
                                    row_key=key)
            self.relabel(rows_shots, {row: key})
            shots += rows_shots
        return shots

    def rows_only(self, keys, angles, tag_n, backpack=False):
        """Photograph each part's 库存 row without ever fitting it. -> shots

        WHY THIS IS NOT one_part's rows pass. That one reaches a row by
        equipping the part and taking it back off, so every row it collects is
        gated on SlotDetector naming what sits in the slot — and the parts
        whose templates are too weak to be named are exactly the parts a row
        capture is needed for. Run 20260805_005551 is the circle in one line:
        quick_smg scored 192 against a gate of 150, so the uzi's magazine slot
        read `empty` with the uzi's own magazine still in it, `strip` skipped
        it, and the spawned part was bounced into 库存 and abandoned. 11 of 12
        parts died that way, all of them parts with no row variant.

        NOTHING HERE CONSULTS A TEMPLATE. `inv_rows` is Laplacian only, and
        identity comes from having spawned exactly one thing into an empty
        list — the same rule one_part rests on, minus the gun that made the
        rule need a detector.

        THE RACK IS EMPTIED TOO, not just 库存: the autofit rule puts a part
        straight onto a racked gun whose slot is free (3/3, tools/probe_autofit
        .py), which would leave 库存 empty and nothing to photograph.
        """
        shots = []
        for k in keys:
            if not self.tab():
                print(f'    {k}: the inventory would not open')
                continue
            # AN EMPTY 库存 IS THE ENTIRE IDENTITY CLAIM, so it is verified
            # rather than assumed. clear_inventory drags rows to the floor and
            # a drag can fail to land; run 20260805_010546 lost its last five
            # parts to exactly that, each arriving into a list that still held
            # the previous one ("rows=2"). The drag itself is fixed (see
            # Pointer.place), and this stays because the cost of checking is
            # one Laplacian pass and the cost of not checking is a whole part.
            self.ac.clear_rack()
            for attempt in range(1, CLEAR_TRIES + 1):
                self.ac.clear_inventory()
                if inv_rows(self.frame(flush=2)) == 0:
                    break
                print(f'    {k}: 库存 did not empty (attempt {attempt})')
            else:
                self.miss(k, '库存 would not empty, so a spawned part cannot '
                             'be named by being the only row in it')
                continue
            if not self.spawn(None, [k], backpack):
                self.miss(k, 'the spawner would not produce it')
                continue
            if not self.tab():
                print(f'    {k}: the inventory would not reopen after spawning')
                continue
            held = inv_rows(self.frame(flush=4))
            # Exactly one, or the row cannot be named. Zero means it landed
            # somewhere else (a gun left in the rack ate it); more than one
            # means the list was not empty and the game's own sort decides
            # which row is which — the guess that sent 228 crops out under the
            # wrong names.
            if held != 1:
                self.miss(k, 'staging left something other than exactly one '
                             'row in 库存, so no row can be named', rows=held)
                continue
            got = self.sweep(None, [], [0], angles, tag_n, 'l', row_key=k)
            self.relabel(got, {0: k})
            shots += got
        return shots

    def round(self, weapon, keys, fit, angles, n, spawn=True, backpack=False):
        label = ', '.join(weapon) if isinstance(weapon, list) else weapon
        print(f'\n── round {n}: {label or "no weapon"} ── '
              + (', '.join(keys) or '(no parts)'))
        if not focus_keeper().ok(f'round {n}'):
            return self.miss(f'round {n}', 'lost the foreground and could not '
                                           'take it back')

        # PLATES ARE THEIR OWN SHAPE and share nothing below: no parts to fit,
        # no host to keep bare, and the rack is cleared at EVERY background
        # rather than once, because an empty rack is what a plate is paired
        # against. plate_pair does its own spawning for the same reason -- the
        # gun has to arrive after the backdrop is already photographed.
        if isinstance(weapon, list):
            return self.plate_pair(weapon, angles, n)

        if spawn:
            # The HOST only. Its parts are spawned by one_part, one at a time,
            # each into a slot state it has arranged -- spawning them here as
            # well produced two copies of everything and left one_part looking
            # for a slot the bulk spawn had already filled.
            if weapon:
                if not self.bare_host(weapon, backpack):
                    return self.miss(weapon, 'no bare host gun to hang this '
                                             'round\'s parts on')
            # rows_only stages one part at a time into a 库存 it empties first,
            # so a bulk spawn here would defeat it exactly as it defeats the
            # fitting path: several parts in the list at once and the row->key
            # mapping becomes the game's sort order, which is not spawn order.
            elif not self.spawn(None, [] if (fit or 'rows' in self.targets)
                                else keys, backpack):
                return self.miss(', '.join(keys),
                                 'the spawner would not produce this round')

        # No rows_of here any more. It answered "which rows hold this round's
        # parts", and one_part does not need the answer: it puts exactly one
        # part in 库存 at a time and knows which row that is because it counted
        # before and after. Left in place it just complained about a 库存 that
        # is empty at this point by design -- "0 rows for 1 parts".
        rows = []
        shots = []
        if fit:
            # ONE PART AT A TIME, and the identity comes from having spawned
            # exactly one thing.
            #
            # Nothing here asks a detector what an icon is, and nothing depends
            # on the order 库存 chose. Both were tried and both failed: spawn
            # order is not row order (the game sorts the list, and 228 crops
            # went out under the wrong names), and a run that identifies parts
            # by reading them is using the detector it exists to test.
            #
            # The auto-fit rule makes this work, and it is why the rule was
            # worth measuring (tools/probe_autofit.py):
            #
            #   slot EMPTY    -> the part goes straight ONTO THE GUN   3/3
            #   slot OCCUPIED -> the part goes to 库存                 3/3
            #   no gun racked -> 库存                                  3/3
            #
            # So each part is spawned twice, into a deliberately chosen state:
            # once with the slot blocked, which puts it in 库存 as the ONLY row
            # there, to photograph `rows`; then equipped, which puts it in the
            # slot, to photograph `slots`. One row and one slot, one part in
            # play, no ordering question to get wrong.
            for k in keys:
                got = self.one_part(weapon, k, angles, n)
                if got is None:
                    print(f'    {k}: not collected')
                    continue
                if not got:
                    self.miss(k, 'collected nothing, without a reason above — '
                                 'a target may be off, or a sweep returned '
                                 'empty', targets=sorted(self.targets))
                shots += got
            return shots
        # Rows without fitting. `rows` above is [] on this path and always was
        # — the sweep below photographs nothing, which is why `--targets rows`
        # on its own returned 0 crops for as long as it has existed.
        if 'rows' in self.targets:
            return shots + self.rows_only(keys, angles, n, backpack)
        return shots + self.sweep(weapon, keys, rows, angles, n, 'f')


# ════════════════════════════════════════════════════════════
# Reporting
# ════════════════════════════════════════════════════════════

def report(shots):
    """What the current detectors made of targets whose identity is known.

    Returns the rebuild queue, which lands in the run's facts — that list is
    what the calibrate-template skill is pointed at.
    """
    print('\n' + '=' * 70)
    print('COVERAGE — what the CURRENT detectors make of known targets')
    print('=' * 70)
    bad = []
    for target in TARGETS:
        rows = [s for s in shots if s['target'] == target]
        if not rows:
            continue
        if target == 'type':
            ink = [s['ink'] for s in rows]
            n_ok = sum(s['ok'] for s in rows)
            print(f'\ntype — 类型 ink over {len(rows)} frames: '
                  f'{min(ink)}..{max(ink)}, config says '
                  f'{TAB_COUNT_MIN}..{TAB_COUNT_MAX}, {n_ok}/{len(rows)} inside')
            if n_ok < len(rows):
                print('  <-- Tab detection fails OPEN: the screen reads as '
                      '"not up" and the caller silently does nothing.')
                bad.append({'target': 'type', 'key': 'type',
                            'why': 'ink outside TAB_COUNT_MIN/MAX',
                            'measured': [min(ink), max(ink)],
                            'config': [TAB_COUNT_MIN, TAB_COUNT_MAX],
                            'files': [s['capture'] for s in rows
                                      if not s['ok']]})
            continue

        print(f'\n{target}')
        groups = {}
        for s in rows:
            groups.setdefault(s['key'], []).append(s)
        for key, got in sorted(groups.items()):
            hits = sum(s['ok'] for s in got)
            others = sorted({s['read'] or '<nothing>' for s in got
                             if not s['ok']})
            why = ('' if hits == len(got) else
                   'no template in this repo' if not got[0]['has_template'] else
                   'never recognised' if hits == 0 else
                   'recognised on some backgrounds only')
            print(f'  {key:<16}{hits:>4}/{len(got):<3}  {", ".join(others)}'
                  + (f'   <-- {why}' if why else ''))
            if why:
                bad.append({'target': target, 'key': key, 'why': why,
                            'read_as': others, 'region': got[0]['region'],
                            'files': [s['capture'] for s in got]})
    print()
    if bad:
        print(f'{len(bad)} target(s) need rebuilding — manifest.json lists '
              f'them under facts.bad with the crops already cut. Hand the run '
              f'directory to the calibrate-template skill.')
    else:
        print('Every target read correctly against every background.')
    return bad


# ════════════════════════════════════════════════════════════

def main():
    try:            # item names are Chinese; a cp1252 console dies on 倍
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--slot', help='every attachment for this slot')
    ap.add_argument('--keys', help='explicit comma-separated catalogue keys')
    ap.add_argument('--all', action='store_true', help='every attachment')
    ap.add_argument('--plates', action='store_true',
                    help='name-plate sweep: one round per weapon, no parts')
    ap.add_argument('--as-is', action='store_true',
                    help='photograph what is on the gun now, spawn nothing; '
                         'needs --label')
    ap.add_argument('--label', help='--as-is: slot=key pairs naming what is on')
    ap.add_argument('--targets', help=f'{",".join(TARGETS)} (default slots, '
                                      f'or plate with --plates)')
    ap.add_argument('--weapon', help='host weapon(s), comma separated. '
                                     'Default: the smallest covering set')
    ap.add_argument('--gun', type=int, default=2, choices=(1, 2),
                    help='which weapon slot to kit (default 2)')
    ap.add_argument('--angles', type=int, default=10,
                    help='how many backgrounds. MEASURED, not guessed: '
                         'calibration/solve_template.py --stability holds one '
                         'capture out of the solve and reconstructs it, and '
                         'on comp_ar over 16 backgrounds the error plateaus '
                         'at k>=9 (0.27-0.41 grey levels) after falling from '
                         '1.69 at k=2. Ten sits at the start of that plateau; '
                         'sixteen buys nothing.')
    ap.add_argument('--plan', action='store_true',
                    help='print the plan and exit; no game needed')
    ap.add_argument('--out', default='')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    targets = tuple(t.strip() for t in
                    # `plate` alone, not plate,type: plate_pair photographs the
                    # rack and nothing else, because the 类型 marker is not
                    # paired against anything and rides along free on the parts
                    # runs, which grab a whole frame anyway.
                    (args.targets or ('plate' if args.plates
                                      else 'slots')).split(',') if t.strip())
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        ap.error(f'not a target: {", ".join(unknown)} (have {", ".join(TARGETS)})')

    weapons = [w.strip() for w in (args.weapon or '').split(',') if w.strip()]
    if args.plates and not weapons:
        weapons = list(ROSTER)
    dead = [w for w in weapons if w not in ROSTER]
    if dead:
        ap.error(f'the spawner cannot produce: {", ".join(dead)}')

    if args.as_is:
        if not args.label:
            ap.error('--as-is needs --label to say what is fitted; labels read '
                     'off the templates being tested would be circular')
        keys = parse_label(args.label)
    elif args.keys:
        keys = [k.strip() for k in args.keys.split(',') if k.strip()]
    elif args.slot:
        keys = sorted(k for k, v in ATTACHMENTS.items() if v['slot'] == args.slot)
    elif args.all:
        keys = list(ATTACHMENTS)
    elif args.plates:
        keys = []
    else:
        ap.error('give --slot, --keys, --all, --plates or --as-is')
    unknown = [k for k in keys if k not in ATTACHMENTS]
    if unknown:
        ap.error(f'not in the catalogue: {", ".join(unknown)}')

    if args.as_is:
        rounds, unreachable = [(weapons[0] if weapons else None, keys, False)], []
    else:
        rounds, unreachable = plan_rounds(targets, keys, weapons, args.plates)
    passes = 2 if ('rows' in targets and 'slots' in targets) else 1
    print(f'targets  : {", ".join(targets)}')
    print(f'rounds   : {len(rounds)} x {args.angles} backgrounds'
          + (f' x {passes} passes' if passes > 1 else ''))
    if unreachable:
        print(f'skipped  : {", ".join(unreachable)} — '
              + ('none of the hosts you named can wear them' if weapons
                 else 'no live weapon in ROSTER can wear them'))
    if args.plan:
        for i, (w, ks, _) in enumerate(rounds, 1):
            name = ' + '.join(w) if isinstance(w, list) else (w or '-')
            print(f'  {i:2d}. {name:<22}{", ".join(ks)}')
        return 0
    if not rounds:
        ap.error('nothing to collect')

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    # path=, so --out keeps working and the run keeps this root — see the
    # module docstring for why it does not move under docs/runs/.
    run = CaptureRun.create(KIND, stamp=stamp,
                            path=args.out or os.path.join(OUT_ROOT, stamp),
                            note=('--as-is, operator-labelled' if args.as_is
                                  else ''),
                            facts={'gun': args.gun, 'angles': args.angles,
                                   'targets': list(targets),
                                   'unreachable': unreachable})
    out_dir = run.path
    print(f'out      : {os.path.relpath(out_dir, ROOT)}\n')

    print('>>> Stand at an item spawner with room to turn all the way round.')
    if not ensure_focus(countdown_s=args.countdown, label='template collection'):
        print('[!] ABORT: game not focused and could not take the foreground.')
        return 1

    rig = Rig('red_dot')
    if not hasattr(rig.mouse, 'key'):
        print('[!] ABORT: no Pico — Tab and the spawner key cannot be pressed.')
        rig.close()
        return 1
    # `by` is what label_for() writes into every label. --as-is spawns nothing
    # and drags nothing: the operator named what is already on the gun, and
    # that is the only account of it there is.
    col = Collector(rig, SpawnerControl(verbose=False),
                    InventoryControl(verbose=False), args.gun, run, targets,
                    by='operator' if args.as_is else 'spawn')

    # The training range evicts at 20 minutes and a full collection runs far
    # longer than that -- 12 rounds x 6 backgrounds x 2 passes, or 30 weapons.
    # Without this the run is thrown into the lobby somewhere in the middle and
    # every round after it photographs a menu, or dies on a spawner that will
    # not open. harvest re-enters between weapons for the same reason; this is
    # the same call at the same granularity.
    #
    # Re-entry is a RESTART: the rack and the backpack come back empty, so the
    # round after one has to spawn its own parts again. `backpack` below is
    # already keyed off "does this round have parts", so a re-entered round
    # asks for the backpack too rather than dropping its parts on the floor.
    session = get_session('auto')

    shots = []
    try:
        for i, (weapon, ks, fit) in enumerate(rounds, 1):
            ok, re_entered = session.ensure()
            if not ok:
                print('[!] could not get back into the training range — '
                      'stopping rather than photographing a lobby')
                break
            if re_entered:
                print('    re-entered the range — the rack and pack are empty '
                      'again')
            elif ks and (i == 1 or col.misses):
                # A FULL 库存 STOPS THE SPAWNER, silently. The pack fills up
                # over a run because every part that fails to fit stays in it,
                # and at 12 rows nothing else arrives -- give_attachment still
                # reports ok, because that only means the click found the right
                # entry. Three runs photographed an empty rack this way.
                #
                # Cleared before round 1 (the pack survives from whatever ran
                # last) and after any round that missed, which is the cheapest
                # signal that something is accumulating. A re-entry has already
                # emptied it.
                #
                # ONLY WHEN THIS ROUND SPAWNS PARTS (`ks`). Clearing is up to
                # twelve LEFT-BUTTON DRAGS, and a plates round never touches
                # the backpack -- it racks two guns and turns. Those drags were
                # going out anyway, doing nothing, and a left button pressed
                # while the Tab screen is not actually up reaches the game and
                # fires the weapon in hand.
                rec = col.ac.clear_inventory() if col.tab() else None
                if rec and rec.get('rows_left'):
                    print(f"    [!] 库存 still holds {rec['rows_left']} row(s) "
                          f"— {rec.get('error')}")
                elif rec:
                    print('    库存 emptied to the floor')
            # THE BACKPACK IS ASKED FOR EVERY ROUND THAT SPAWNS PARTS, not
            # only the first. An attachment with no backpack does not refuse
            # to spawn — it goes somewhere else, the 库存 rows shift under the
            # drag targets, and every step afterwards reads back a part nobody
            # asked for (control/spawner.py says the same thing about harvest).
            # `give_many` folds a backpack that is already worn into nothing,
            # so asking again is a no-op, while assuming it survived N rounds
            # of dropping things on the floor is a guess that costs a whole
            # round when it is wrong.
            got = col.round(weapon, ks, fit, args.angles, i,
                            spawn=not args.as_is, backpack=bool(ks))
            if got is None:
                print(f'    [!] round {i} produced nothing; carrying on')
                continue
            shots += got
        run.facts.update(ts=datetime.now().isoformat(timespec='seconds'),
                         bad=report(shots), misses=col.misses)
        print(f'\n  {len(shots)} crops + manifest.json -> '
              f'{os.path.relpath(out_dir, ROOT)}')
        if col.misses:
            print(f'  {len(col.misses)} part(s) collected nothing — '
                  f'facts.misses says why, per part, with the numbers')
    except KeyboardInterrupt:
        print('\ninterrupted')
    finally:
        # Every crop is already saved — add() writes the manifest each time, so
        # an interrupted run keeps everything it captured. This is the run's
        # own summary (`bad`, the rebuild queue) landing beside them.
        #
        # `misses` is set here and not only on the success path: an interrupted
        # run is exactly the one whose console is most likely to be lost, and
        # the reasons are the whole reason to look at it afterwards.
        run.facts.setdefault('misses', col.misses)
        run.save()
        col.close()
        rig.close()
        try:
            session.close()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
