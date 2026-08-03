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
and 库存 does not grow round over round; parts then spawn into 库存, which
fills from the top with no gaps, so row N holds a known item. Fitting drags
bottom row first (pulling row i out shifts only rows below it) and is verified
by two independent facts: the slot's pixels changed and gained detail, AND 库存
lost exactly one row. Both are Laplacian/difference tests.

A retry only happens when nothing changed at all — the one case where the
source row is provably still the source row. A drag with any other effect ends
the round, because a second drag would fit whatever slid into that row and
mislabel every crop after it.

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
coordinate -> 库存 row -> slot) whose every hop is confirmed WITHOUT A
TEMPLATE, which is exactly what makes it non-circular for the templates being
collected. `plate` and `type` get no label at all, and the reasons are in
label_for() too.
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
from detector.attachment_catalog import (ATTACHMENTS, ROSTER, SLOTS, fits,
                                         is_live)
from detector.cropper import win32_cap
from detector.tab_detector import TabTypeDetector
from detector.attachment_detector import SLOT_DETAIL_MIN
from detector.tab_items import ROW_DETAIL_MIN, tab_blocks
from detector.tab_layout import INV_ROWS, icon_box
from control.focus import ensure_focus, focus_keeper

from control.spawner import SpawnerControl
from control.inventory import InventoryControl, at_inv, at_slot
from harvest import BACKPACK
from sweep import Rig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ROOT = os.path.join(ROOT, 'docs', 'attachments', 'runs')

TARGETS = ('slots', 'rows', 'plate', 'type')

TURN_COUNTS = 900               # yaw per step; lands on unrelated scenery
PITCH_STEPS = (0, -260, 260)    # sky, level, ground — the three that differ most
SETTLE_S = 0.45
SPAWN_SETTLE_S = 0.7
FIT_TIMEOUT_S = 0.8             # the part animates into the slot
FIT_POLL_S = 0.08

# A NAME PLATE IS DRAWN HERE — the band, not a floor.
#
# MEASURED (2026-08-03), white-text-mask pixels over gun_name_N, Tab up:
#
#   empty rack slot      0        6 of 6 samples, exactly zero
#   a gun is racked      679-901  13 samples
#
# One of those frames carries its own control: an akm round read 682 on slot 1
# and 0 on slot 2 — same frame, same scenery, one slot occupied and one not.
#
# It is a BAND for the same reason TAB_COUNT_MIN/MAX is one. With the panel up
# the backdrop is dimmed (blend_tab_background: blur(bg,k=41)*0.49), but the
# mask is only "near-white and achromatic", and over 293 Tab-SHUT frames this
# region saturates at 11250 — the whole crop — on 80 of them. Bright everywhere
# is scenery, not glyphs. A reading above the ceiling is refused rather than
# believed, exactly as the tab detector refuses sky.
#
# 200 sits ~3.4x under the lowest real plate and far above the zero floor; 4000
# is 4.4x over the highest real plate and well under saturation.
PLATE_INK_MIN = 200
PLATE_INK_MAX = 4000

# Mean |after-before| over a slot crop, grey levels, for "an icon appeared".
# An icon landing in an empty 63x63 slot moves most pixels most of the way;
# scenery a second apart with the player standing still moves nothing. Kept low
# because it is only half the verdict — the other half is 库存 losing exactly
# one row, which no amount of scenery can fake.
CHANGE_MIN = 6.0

BY_ASSET = {v['asset']: k for k, v in ATTACHMENTS.items() if v.get('asset')}


def cut(frame, region):
    """A HUD_REGIONS entry out of a screen-indexed frame."""
    y, x, h, w = HUD_REGIONS[region] if isinstance(region, str) else region
    return frame[y:y + h, x:x + w]


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
            if not is_live(w):
                continue
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
      --plates      one weapon per round, no parts
      slots         parts must physically fit, so one per slot on a host
      otherwise     parts only sit in 库存 — no compatibility to satisfy, and
                    the list holds INV_ROWS of them at a time
    """
    if plates:
        return [(w, [], False) for w in weapons], []
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

def detail(crop):
    """High-frequency energy — is UI drawn here, or is it the blurred world?

    Pixel variance does not separate them (blurred scenery is colourful); an
    icon's hard edges do. Thresholds and their measurements live in tab_items.
    """
    if crop is None or crop.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return float(cv2.Laplacian(gray, cv2.CV_32F).var())


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

    Which is why fit_one() must stay hand-rolled rather than call
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
        # Set per round by round(): the plate ink on the cleared rack, and
        # whether the spawn that followed was watched arriving. False here so a
        # caller that never runs a round (or a target that is not `plate`)
        # cannot accidentally claim ground truth.
        self.plate_ink0 = None
        self.plate_arrived = False

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

    def empty_rack(self):
        """Clear both rack slots and read the plate back as blank. -> ink|None

        None means the rack could not be proven empty, and the caller must not
        claim an arrival off the spawn that follows.

        Plain clear_rack, not the strip-then-drop harvest uses: a plates round
        fits nothing, so there is no part worth keeping out of the drop, and
        the gun leaves wearing the magazine the game fitted for itself.
        """
        if not self.tab():
            print('    [!] the inventory would not open to clear the rack')
            return None
        self.ac.clear_rack()
        ink = self.ac.plate_ink(self.gun, self.frame(flush=2))
        if ink >= PLATE_INK_MIN:
            print(f'    [!] rack cleared but the plate still reads {ink} ink '
                  f'(< {PLATE_INK_MIN} expected) — cannot call the next spawn '
                  f'an arrival')
            return None
        return ink

    def spawn(self, weapon, keys, backpack):
        """Host weapon first, then the parts in order.

        The weapon goes first for two reasons: a gun arriving with parts
        already in the backpack picks them up on the spot, which would empty
        the rows the drags below index into; and spawning it is what clears the
        last round, since a full rack evicts the old gun and its attachments.
        """
        if not self.sc.ensure_panel(True):
            print('    [!] the spawner panel would not open')
            return False
        # Columns 1-2 only: the backpack has fixed coordinates (SpawnerControl
        # .GEAR), so column 3 never has to be found.
        ok = self.sc.sync(need_cols=(1, 2))
        if ok and backpack and not self.sc.give_gear(BACKPACK).get('ok'):
            print('    [!] no backpack: parts have nowhere to spawn')
            ok = False
        if ok and weapon and not self.sc.give_weapon(weapon)['ok']:
            print(f'    [!] the spawner would not produce {weapon}')
            ok = False
        for k in (keys if ok else []):
            if not self.sc.give_attachment(k)['ok']:
                print(f'    [!] the spawner would not produce {k}')
                ok = False
                break
        self.sc.ensure_panel(False)
        time.sleep(SPAWN_SETTLE_S)
        return ok

    def rows_of(self, keys):
        """{row: key} for the parts loose in 库存, or None if the count is off.

        Spawn order is row order: the list fills from the top with no gaps, so
        these are the last len(keys) occupied rows.
        """
        n = inv_rows(self.frame())
        base = n - len(keys)
        if base < 0:
            print(f'    [!] 库存 shows {n} rows for {len(keys)} parts — '
                  f'something did not spawn. Skipping rather than mislabelling.')
            return None
        if base:
            print(f'    ({base} row(s) already in 库存; parts are {base}..{n-1})')
        return {base + i: k for i, k in enumerate(keys)}

    # ── fitting ──

    def fit(self, rows):
        """Drag every part out of 库存 onto its slot. {key: record}.

        Bottom row first: pulling row i out shifts only the rows below it, so
        descending order leaves every queued source where it was.
        """
        out = {}
        for row in sorted(rows, reverse=True):
            key = rows[row]
            out[key] = rec = self.fit_one(row, key)
            print(f'    row{row:2d} -> {ATTACHMENTS[key]["slot"]:<9}{key:<16}'
                  + ('ok' if rec['ok'] else f'FAILED — {rec["error"]}'))
            if rec['fatal']:
                break
        return out

    def fit_one(self, row, key, tries=2):
        """One drag, verified by pixels and by the row count.

        Two independent facts on purpose: the slot's crop changed and gained
        detail, and 库存 lost exactly one row. Either alone can be argued with
        — scenery moves, and a part can land on the floor — together they
        cannot.

        DELIBERATELY NOT InventoryControl.ensure_kit, and this is the one
        fitting loop in the repo that must stay hand-rolled. ensure_kit
        verifies a slot by matching the part's ICON TEMPLATE — and this file
        exists to COLLECT that template. Using it here is circular: a part
        with no template yet reads as "cannot be proven", so every capture of
        a new attachment would be thrown away for lacking the very thing it is
        being run to produce. Pixels and the row count need no template.
        """
        slot = ATTACHMENTS[key]['slot']
        rec = {'slot': slot, 'row': row, 'ok': False, 'error': None,
               'fatal': False}
        for attempt in range(tries):
            f0 = self.frame()
            before, n0 = self.crop(f0, slot).copy(), inv_rows(f0)
            # verify=False, not a reach into ac.pointer. Same gesture, but the
            # address vocabulary and _reject() still apply -- a drag onto a
            # slot this gun does not have is refused before the mouse moves,
            # and the raw-pointer version had no idea such a thing existed.
            self.ac.drag(at_inv(row), at_slot(self.gun, slot), verify=False)

            deadline = time.perf_counter() + FIT_TIMEOUT_S
            while True:
                f1 = self.frame(flush=2)
                after, n1 = self.crop(f1, slot), inv_rows(f1)
                rec.update(change=change(before, after), detail=detail(after))
                rec['ok'] = (rec['change'] >= CHANGE_MIN
                             and rec['detail'] >= SLOT_DETAIL_MIN
                             and n1 == n0 - 1)
                if rec['ok'] or time.perf_counter() >= deadline:
                    break
                time.sleep(FIT_POLL_S)
            if rec['ok']:
                return rec
            # Retrying is safe only while the screen is exactly as it was: then
            # the part never left and `row` still points at it. If anything
            # moved, a second drag would pick up whatever slid into that row.
            if n1 != n0 or rec['change'] >= CHANGE_MIN:
                rec['error'] = (f'had an effect but not the expected one '
                                f'(change={rec["change"]:.1f} '
                                f'detail={rec["detail"]:.0f} rows {n0}->{n1})')
                rec['fatal'] = True
                return rec
        rec['error'] = (f'nothing moved (change={rec["change"]:.1f} '
                        f'detail={rec["detail"]:.0f})')
        return rec

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

    def capture(self, weapon, keys, rows, tag, tag_n):
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
            for row, key in rows.items():
                x0, y0, x1, y1 = icon_box(row, 'inventory')
                item = found[row] if row < len(found) else None
                shots.append(self._shot(
                    frame[y0:y1, x0:x1],
                    f'{key}__row{row:02d}__{wname}__{tag}.png',
                    'rows', key, (y0, x0, y1 - y0, x1 - x0),
                    (item.key or '') if item else '', self._has(key), row=row))

        if 'plate' in self.targets and weapon:
            name = f'gun_name_{self.gun}'
            read = self.ac.read_weapons(frame)
            shots.append(self._shot(
                cut(frame, name), f'plate__{weapon}__{tag}.png',
                'plate', weapon, HUD_REGIONS[name],
                read.get(self.gun) or '', True,
                # The arrival evidence, recorded per capture. Zero here on a
                # rack that was emptied first means no weapon is drawn, so the
                # crop is of nothing and the label must not be believed --
                # which is the one thing give_weapon()'s ok cannot tell you.
                ink=self.ac.plate_ink(self.gun, frame)))

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
        self.write(f'panel__{wname}__r{tag_n}__{tag}.png', frame[y:y+h, x:x+w],
                   target='panel', weapon=wname, round=tag_n, labels=[])
        return shots

    @staticmethod
    def _has(key):
        return bool(ATTACHMENTS[key].get('asset'))

    # ── one round ──

    def sweep(self, weapon, keys, rows, angles, tag_n, pass_tag):
        shots = []
        for a in range(angles):
            pitch = PITCH_STEPS[a % len(PITCH_STEPS)]
            self.turn(TURN_COUNTS, pitch)
            got = self.capture(weapon, keys, rows, f'{pass_tag}bg{a}', tag_n)
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

    def round(self, weapon, keys, fit, angles, n, spawn=True, backpack=False):
        print(f'\n── round {n}: {weapon or "no weapon"} ── '
              + (', '.join(keys) or '(no parts)'))
        if not focus_keeper().ok(f'round {n}'):
            print('    [!] lost the foreground and could not take it back')
            return None

        # Empty the rack and read the plate blank BEFORE the spawn, so what
        # follows can be called an arrival rather than assumed to be one. Only
        # when a plate is actually being collected: it costs a Tab cycle and
        # two drops, and a round that photographs attachments wants the host
        # gun left exactly where it is.
        self.plate_ink0 = None
        if 'plate' in self.targets and weapon and spawn:
            self.plate_ink0 = self.empty_rack()

        if spawn and not self.spawn(weapon, keys, backpack):
            return None

        if self.plate_ink0 is not None:
            after = self.ac.plate_ink(self.gun, self.frame(flush=2)) \
                if self.tab() else 0
            self.plate_arrived = plate_arrived(self.plate_ink0, after)
            print(f'    plate ink {self.plate_ink0} -> {after}  '
                  + ('ARRIVED, labelled' if self.plate_arrived else
                     'NOT an arrival — captured without a label'))
        else:
            self.plate_arrived = False

        rows = {}
        if keys and spawn:
            if not self.tab():
                print('    [!] the inventory would not open')
                return None
            rows = self.rows_of(keys)
            if rows is None:
                return None
        shots = []
        if fit:
            # 库存 holds the parts only until they are fitted, so a rows pass
            # has to happen now. The yaw keeps advancing across both passes, so
            # the second sees different scenery rather than repeating this.
            if 'rows' in self.targets:
                shots += self.sweep(weapon, [], rows, angles, n, 'l')
            done = self.fit(rows)
            # Only what provably landed is photographed; a crop of nothing
            # labelled as a part is worse than no crop.
            keys = [k for k in keys if done.get(k, {}).get('ok')]
            rows = {}
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
                                      f'or plate,type with --plates)')
    ap.add_argument('--weapon', help='host weapon(s), comma separated. '
                                     'Default: the smallest covering set')
    ap.add_argument('--gun', type=int, default=2, choices=(1, 2),
                    help='which weapon slot to kit (default 2)')
    ap.add_argument('--angles', type=int, default=6, help='how many backgrounds')
    ap.add_argument('--plan', action='store_true',
                    help='print the plan and exit; no game needed')
    ap.add_argument('--out', default='')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    targets = tuple(t.strip() for t in
                    (args.targets or ('plate,type' if args.plates
                                      else 'slots')).split(',') if t.strip())
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        ap.error(f'not a target: {", ".join(unknown)} (have {", ".join(TARGETS)})')

    weapons = [w.strip() for w in (args.weapon or '').split(',') if w.strip()]
    if args.plates and not weapons:
        weapons = [w for w in ROSTER if is_live(w)]
    dead = [w for w in weapons if not is_live(w)]
    if dead:
        ap.error(f'not a live weapon: {", ".join(dead)}')

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
            print(f'  {i:2d}. {w or "-":<10}{", ".join(ks)}')
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

    shots = []
    try:
        for i, (weapon, ks, fit) in enumerate(rounds, 1):
            got = col.round(weapon, ks, fit, args.angles, i,
                            spawn=not args.as_is, backpack=i == 1 and bool(ks))
            if got is None:
                print(f'    [!] round {i} produced nothing; carrying on')
                continue
            shots += got
        run.facts.update(ts=datetime.now().isoformat(timespec='seconds'),
                         bad=report(shots))
        print(f'\n  {len(shots)} crops + manifest.json -> '
              f'{os.path.relpath(out_dir, ROOT)}')
    except KeyboardInterrupt:
        print('\ninterrupted')
    finally:
        # Every crop is already saved — add() writes the manifest each time, so
        # an interrupted run keeps everything it captured. This is the run's
        # own summary (`bad`, the rebuild queue) landing beside them.
        run.save()
        col.close()
        rig.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
