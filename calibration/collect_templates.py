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
from detector.attachment_catalog import (ATTACHMENTS, ROSTER, SLOTS, fits,
                                         is_live)
from detector.cropper import win32_cap
from detector.tab_detector import TabTypeDetector
from detector.attachment_detector import SLOT_DETAIL_MIN, SLOT_NAMES
from detector.tab_items import ROW_DETAIL_MIN, tab_blocks
from detector.tab_layout import INV_ROWS, icon_box
from control.focus import ensure_focus, focus_keeper

from control.spawner import SpawnerControl
from control.inventory import InventoryControl, at_ground, at_inv
from harvest import BACKPACK
from range_session import get_session
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
        if not self.spawn(weapon, [], backpack):
            return False
        if not self.tab():
            print('    [!] the inventory would not open to strip the host')
            return False
        # to=at_ground(): the floor, by drag. unequip's default sends it to
        # 库存 by right click, which is the one destination that must not be
        # used here.
        rec = self.ac.strip(self.gun, to=at_ground())
        if rec.get('worn'):
            print(f'    host arrived wearing {", ".join(rec["worn"])} — '
                  f'stripped to the floor')
        left = [s for s, v in self.ac.read_slots(self.gun).items() if v]
        if left:
            print(f'    [!] still wearing {left} — every fit into those slots '
                  f'will be a swap and cannot be named')
            return False
        return True

    def spawn(self, weapon, keys, backpack):
        """Spawn a host weapon and/or a list of parts. One panel visit.

        Callers wanting a host for FITTING want bare_host(), which is this plus
        the strip that makes the gun actually bare.
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
            if self.sc.give_attachment(k)['ok']:
                continue
            # One re-read and one more try, the way control/stock.restock
            # handles the same thing: the usual cause is the accordion, a
            # category refusing to expand because a different one is, and a
            # fresh sync clears it. Without this a single stuck panel cost the
            # rest of the round's parts -- the loop used to break on the first
            # refusal.
            print(f'    [!] the spawner would not produce {k} — re-reading '
                  f'the layout and retrying once')
            self.sc.menu = None
            self.sc.sync(need_cols=(1, 2))
            if not self.sc.give_attachment(k)['ok']:
                print(f'    [!] {k} still would not spawn — carrying on '
                      f'without it')
        self.sc.ensure_panel(False)
        time.sleep(SPAWN_SETTLE_S)
        return ok

    def rows_of(self, keys):
        """WHICH ROWS this round's parts occupy — not which part is in which.

        -> [row, ...], or None if the count says something did not spawn.

        It used to return {row: key}, on the reasoning that "spawn order is row
        order: the list fills from the top with no gaps". The count part is
        true and is still used. The ORDER part is false: the game sorts 库存 by
        its own rule, and the first real run of this collector spawned
        angled_grip, brake_ar, cheek_pad, ext_ar, holo into rows holding holo,
        cheek_pad, ext_ar, brake_ar and a grip. Every one of 228 row crops was
        labelled with the wrong part.

        Which row holds which part is now an OUTPUT, of fit(): the game is
        asked to place each one and the slot it fills names it.
        """
        n = inv_rows(self.frame())
        base = n - len(keys)
        if base < 0:
            # A shortfall no longer costs the round. It used to: fewer rows
            # than parts meant the row->key mapping could not be built, so all
            # of them were dropped. That mapping is not built here any more --
            # fit() discovers it -- so the rows that ARE there can be
            # photographed and named, and whichever key never turns up in the
            # result is the one that did not spawn, identified by elimination
            # and without reading a template.
            #
            # Seen twice on live runs (a round asking for 5 and finding 4, and
            # tools/probe_autofit.py's condition C). The cause is still open:
            # NOT auto-fitting onto the gun, which that probe ruled out.
            print(f'    [!] 库存 shows {n} rows for {len(keys)} parts — '
                  f'{len(keys) - n} did not spawn. Collecting the {n} that '
                  f'did; the missing one names itself when the fits come back.')
            base = 0
        if base:
            print(f'    ({base} row(s) already in 库存; parts are {base}..{n-1})')
        return list(range(base, n))

    # ── fitting ──

    def fit(self, rows, keys):
        """Equip every part in 库存 and learn what each row held. -> (map, recs)

        `map` is {row: key}, DISCOVERED. `rows` only says which row indices
        hold this round's parts; `keys` is the set that was spawned. The
        pairing between them is what this works out, by right-clicking a row
        and seeing which slot the game filled — the catalogue then names the
        part, because a round's parts occupy different slots.

        That direction is the whole change. It used to take {row: key} as an
        INPUT, built on "spawn order is row order", and the first real run
        showed the game sorts 库存 its own way: round 1 spawned angled_grip,
        brake_ar, cheek_pad, ext_ar, holo and the rows held holo, cheek_pad,
        ext_ar, brake_ar, grip. Every label went on the wrong crop.

        Bottom row first: taking row i out shifts only the rows below it, so a
        descending pass leaves every queued source where it was.
        """
        # THE SCREEN FIRST. fit() is called straight after the rows sweep, and
        # every background in that sweep goes through turn(), whose first line
        # is ensure_inventory_closed() -- with Tab open the mouse drives a
        # cursor, not the view. So the panel is SHUT on the way in here, and
        # hold() will not open it either: it restores Tab only when Tab was
        # already up (`was_open`).
        #
        # Without this the right click lands in the WORLD, where it means
        # aim-down-sights: the whole picture is redrawn, all five slot crops
        # change at once, and inv_rows reads scenery. Measured exactly that,
        # every round of every run -- "5 slot(s) gained an icon, rows 12->0".
        # The old drag-based version had the same hole; it just also had the
        # 0/4 gesture on top, so this one never got a chance to show.
        # tools/probe_fit_smoke.py prints the Tab state at each step.
        if not self.tab():
            print('    [!] the inventory would not open for fitting')
            return {}, {}

        # In hand, once for the round. The 2x2 in docs/game_quirks.md says the
        # right click lands 5/5 whether or not the gun is held, so this is not
        # what makes it work -- but that was measured with ONE gun in the rack,
        # and which weapon an unheld right-click chooses when there are two has
        # never been tested. A round here can have a leftover in the other
        # slot. Holding removes the question for the price of one Tab cycle.
        self.ac.held = None
        # hold() closes Tab to press the number and reopens it, because it saw
        # it open. That ordering matters: the tab() above is what makes
        # `was_open` true.
        if not self.ac.hold(self.gun):
            print(f'    [!] could not take gun{self.gun} in hand — a right '
                  f'click may equip onto the other rack slot')

        want = {}
        for k in keys:
            want.setdefault(ATTACHMENTS[k]['slot'], []).append(k)
        found, recs = {}, {}
        for row in sorted(rows, reverse=True):
            rec = self.fit_row(row)
            key = None
            if rec['ok']:
                here = want.get(rec['slot']) or []
                if len(here) == 1:
                    key = here[0]
                    found[row] = key
                else:
                    # Two parts of this round want the same slot, so the slot
                    # cannot name which one landed. Not an error and not a
                    # label: the crop is kept, unlabelled, and plan_rounds is
                    # what should stop this happening.
                    rec['error'] = (f'{len(here)} of this round\'s parts are '
                                    f'{rec["slot"]}s ({", ".join(here)}) — the '
                                    f'slot cannot say which one this was')
            recs[row] = rec
            print(f'    row{row:2d} -> '
                  + (f'{rec["slot"]:<9}{key or "(ambiguous)":<16}ok'
                     if key else f'FAILED — {rec["error"]}'))
            if rec['fatal']:
                break
        return found, recs

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

    def slot_crops(self, frame):
        """{slot: crop} for every attachment slot, copied.

        Copied because the caller holds them across another grab, and the
        grabber reuses its buffers.
        """
        return {s: self.crop(frame, s).copy() for s in SLOT_NAMES}

    def fit_row(self, row, tries=2):
        """RIGHT-CLICK one 库存 row and report WHICH SLOT it filled.

        -> {'slot', 'row', 'ok', ...}. `slot` is the answer, not the argument.

        THE GESTURE. This was a drag from the row to a slot chosen from the
        catalogue, and docs/game_quirks.md has that exact direction measured at
        **0 landings out of 4**, against 4/4 for the right click. control/
        CLAUDE.md states it as a rule -- 装用右键、卸用拖拽. The collector was
        using the one gesture the repo had already recorded as not working, so
        no part was ever fitted and the `slots` half of every run came back
        empty. That is not a subtle failure and it survived because this file
        had never been run.

        THE DIRECTION OF IDENTITY. Right-clicking hands the part to the GAME,
        which puts it in the slot it belongs in -- and the catalogue says each
        of a round's parts wants a different slot. So the slot that gains an
        icon names the part, and the row it came from is learned rather than
        assumed. That kills the assumption underneath the old version, which
        was that 库存 fills in spawn order: it does not, the game sorts it, and
        every label in the first real run was attached to the wrong crop.

        Still verified by two independent facts, and still with no template
        consulted: a slot gained detail, AND 库存 lost exactly one row.
        Scenery can move pixels; it cannot take a row out of the list.

        DELIBERATELY NOT InventoryControl.ensure_kit, which is the one reason
        this loop stays hand-rolled: ensure_kit proves a slot by matching the
        part's ICON TEMPLATE, and this file exists to COLLECT that template. A
        part with no template yet would read as "cannot be proven", throwing
        away every capture of exactly the attachment the run was started for.
        """
        rec = {'slot': None, 'row': row, 'ok': False, 'error': None,
               'fatal': False, 'change': 0.0, 'detail': 0.0}
        for _ in range(tries):
            f0 = self.frame()
            before, n0 = self.slot_crops(f0), inv_rows(f0)
            # auto_equip, not equip: equip() takes a destination slot and
            # refuses without one, and the destination is precisely what is
            # being asked. The game places it; which slot lit up is the answer.
            if not self.ac.auto_equip(at_inv(row)):
                rec['error'] = f'row {row} is not a clickable source'
                rec['fatal'] = True
                return rec

            deadline = time.perf_counter() + FIT_TIMEOUT_S
            while True:
                f1 = self.frame(flush=2)
                after, n1 = self.slot_crops(f1), inv_rows(f1)
                # The slot whose picture CHANGED — not the one that crossed a
                # detail threshold. SLOT_DETAIL_MIN answers "is UI drawn in
                # this cell", and a gun in the rack draws every slot it owns
                # whether or not anything is in it: measured, an EMPTY muzzle
                # slot scores 728 against a floor of 100
                # (tools/probe_autofit.py). A gained-detail test therefore
                # never fires, and the first version of this method would have
                # failed every fit. CHANGE_MIN is the number that was measured
                # for this question: an icon landing in a 63x63 slot moves most
                # pixels most of the way, and scenery with the player standing
                # still moves nothing.
                moved = {s: change(before[s], after[s]) for s in SLOT_NAMES}
                gained = [s for s in SLOT_NAMES if moved[s] >= CHANGE_MIN]
                # Recorded on EVERY pass, not only on success. It used to be
                # written just inside the `ok` branch, so a failure reported
                # `change=0.0` -- which reads as "nothing moved" and is in fact
                # "nobody wrote the number down". The message underneath then
                # asserted that cause out loud. Same shape as the two other
                # messages this repo has had to correct for naming a reason
                # they had not checked.
                rec.update(moved={s: round(v, 1) for s, v in moved.items()},
                           rows=(n0, n1))
                if len(gained) == 1 and n1 == n0 - 1:
                    rec.update(slot=gained[0], ok=True,
                               change=moved[gained[0]],
                               detail=detail(after[gained[0]]))
                    return rec
                if time.perf_counter() >= deadline:
                    break
                time.sleep(FIT_POLL_S)

            if n1 != n0 or len(gained) > 1:
                rec['error'] = (
                    f'had an effect but not a nameable one — '
                    f'{len(gained)} slot(s) gained an icon ({gained}), '
                    f'rows {n0}->{n1}. Two slots filling at once means the '
                    f'part cannot be told from the one beside it.')
                rec['fatal'] = True
                return rec
        rec['error'] = (
            f'no single slot changed enough to name — per-slot movement '
            f'{rec.get("moved")}, threshold {CHANGE_MIN}, rows '
            f'{rec.get("rows")}. Says WHAT was measured rather than asserting '
            f'why: "the click did nothing" and "it swapped a part for one that '
            f'looks the same" are the same picture.')
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
            for row in rows:
                x0, y0, x1, y1 = icon_box(row, 'inventory')
                item = found[row] if row < len(found) else None
                # No key in the name and no label: at this moment nobody knows
                # which part sits in this row, and the game's own sort is why.
                # relabel() fills both in once the fits have said. The row and
                # the round are in the name so it can find them again.
                shots.append(self._shot(
                    frame[y0:y1, x0:x1],
                    f'row{row:02d}__{wname}__{tag}.png',
                    'rows', None, (y0, x0, y1 - y0, x1 - x0),
                    (item.key or '') if item else '', False, row=row))

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
        self.ac.strip(self.gun, to=at_ground())
        if self.ac.read_slots(self.gun).get(slot):
            print(f'    {key}: {slot} would not empty — cannot tell what an '
                  f'icon landing there is')
            return None
        n0 = inv_rows(self.frame())

        if not self.spawn(None, [key], False) or not self.tab():
            return None
        if not self.ac.read_slots(self.gun).get(slot):
            print(f'    {key}: spawned but {slot} is still empty — it did not '
                  f'arrive, or the catalogue has the wrong slot for it')
            return None
        if 'slots' in self.targets:
            shots += self.sweep(weapon, [key], [], angles, tag_n, 'f')

        # ── 2. a second copy, which the now-occupied slot sends to 库存 ──
        # The same part, so no ordering question and no second identity to
        # keep track of: whatever appears in that one row is `key`, because
        # `key` is the only thing that was asked for.
        if 'rows' in self.targets:
            if not self.tab():
                return shots
            n1 = inv_rows(self.frame())
            if not self.spawn(None, [key], False) or not self.tab():
                return shots
            n2 = inv_rows(self.frame())
            if n2 != n1 + 1:
                print(f'    {key}: 库存 went {n1}->{n2}, wanted one more row '
                      f'— the second copy did not arrive, so the row it would '
                      f'have filled is not it')
                return shots
            rows_shots = self.sweep(weapon, [], [n2 - 1], angles, tag_n, 'l')
            self.relabel(rows_shots, {n2 - 1: key})
            shots += rows_shots
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

        if spawn:
            # The HOST only. Its parts are spawned by one_part, one at a time,
            # each into a slot state it has arranged -- spawning them here as
            # well produced two copies of everything and left one_part looking
            # for a slot the bulk spawn had already filled.
            if weapon:
                if not self.bare_host(weapon, backpack):
                    return None
            elif not self.spawn(None, [] if fit else keys, backpack):
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
                shots += got
            return shots
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
            got = col.round(weapon, ks, fit, args.angles, i,
                            spawn=not args.as_is,
                            backpack=(i == 1 or re_entered) and bool(ks))
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
        try:
            session.close()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
