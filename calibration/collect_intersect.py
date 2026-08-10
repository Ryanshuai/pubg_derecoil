"""Build an attachment template by INTERSECTION, from two racked guns at once.

    pixi run collect-intersect --group smg
    pixi run collect-intersect --all --plan        # no game needed
    pixi run collect-intersect --group scope --max-frames 60

THE IDEA, and why it replaces the alpha solve. A slot crop is the icon
composited over two things that are not the icon:

    the SCENE behind the translucent panel   changes when the view turns
    the HOST GUN's own hardware              changes when the host changes

The old flow modelled the first (`c = a*icon + (1-a)*backdrop`, solved from an
empty/filled pair per background) and had no handle at all on the second: the
rail does not move with the view, so its `db` is zero and no sweep separates
it. It shipped as part of the template, stable and confident and wrong.

This flow models neither. It photographs the SAME part on TWO GUNS AT ONCE and
keeps only the pixels that are byte-identical, then folds in another pair from
a different view, and another, until nothing changes:

    template = INTERSECT over (gun A, gun B) x (every view)

A scene pixel differs between views. A rail pixel differs between guns. Only
the attachment is the same in all of them. Nothing is modelled, nothing is
inverted, and there is no alpha to get wrong -- the thing that survives is the
thing that was always there.

⚠ EXACT EQUALITY, per channel. A pixel whose R matches and whose B does not is
a pixel the two views disagree about. Swept on the old corpus, every tolerance
from 0 to 30 grey levels scored identically, so the loose ones were keeping
pixels that contributed nothing -- and 0 is the only value that cannot carry a
pixel nobody agreed on.

THE PROCEDURE. One round per (one part per slot), and the round is a cycle:

  1. THROW EVERYTHING AWAY -- both guns, wearing whatever they wear, and all
     of 库存. Verified empty.
  2. spawn this round's parts, TWICE each. The rack is empty, so they go to
     库存 (measured 3/3 in docs/game_quirks.md; 4 x 13 with zero strays today).
  3. READ THE NAMES. This is where the labels come from, while the parts are
     still in the list and have touched nothing.
  4. spawn the two GUNS. A weapon arriving picks up what it can wear from the
     backpack, so each gun collects one copy of each part.
  5. verify by SUBTRACTION: a part that is no longer in 库存 went onto a gun;
     one that fully left (both copies) is on both guns, which is the pair this
     flow needs. No template is consulted anywhere in that sentence.
  6. shoot both racks' tiles in ONE frame, intersect, turn the view, repeat.
     Stop when an intersection changes nothing. Then round again at (1).

⚠ NOTHING IS EVER TAKEN OFF A GUN, and that is the operator's rule: 不要扔
配件了,要扔就扔枪. Every gesture aimed at a weapon SLOT can reach the weapon
row underneath when the slot turns out to be empty, and then the gun is on the
floor -- 74 measured losses, and twice more this week. Dropping the whole
weapon cannot fail that way, because the weapon row is what it aims at. Four
methods died with that rule (`clear_slots`, its fillers, `shed`, `drop`); they
existed only to get a part off a gun.

⚠ AND THE ROUND TAKES WHAT IT FINDS. An earlier version demanded that 库存
hold exactly what was requested and abandoned the whole group otherwise, which
threw away a racked, cleared pair three runs running over one part that had not
arrived. What a round contains is whatever the screen says it contains.

⚠ SLOTS ONLY. The 库存 row rendering is not collected here at all.

⚠ THE VIEW ALTERNATES GROUND / LEVEL, not sky/ground: nose down to the dirt,
then back to level, with a yaw step every time so the scene is new as well.
Ground and level are what the operator asked for; the sky is a flat wash that
moves few pixels and earns little.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from config import HUD_REGIONS
from detector.attachment_catalog import ATTACHMENTS, fits
from detector.geometry import cut, detail
from calibration.sweep import Rig
from control.focus import focus_keeper
from control.session import ensure_ready
from control.spawner import SpawnerControl
from capture.cropper import capture_screen
from detector.row_name_detector import RowNameDetector
from detector.tab_layout import SLOT_NAMES, icon_box
from control.inventory import InventoryControl, PLATE_INK_MIN, at_inv
from control.kitting import BACKPACK

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMPL_DIR = os.path.join(ROOT, 'data', 'templates', 'pubg_assets',
                        'Item', 'Attachment')
# A NEW TAG BESIDE `.solved`, not over it. The two flows have to be scoreable
# against each other before either is deleted, and a template that overwrote
# its predecessor could only be compared from git.
TAG = 'xsect'
OUT_ROOT = os.path.join(ROOT, 'calibration', 'artifacts', 'intersect')

# WHICH HOSTS CARRY WHICH FAMILY, assigned by the operator 2026-08-09. Two guns
# per group because two are what a rack needs; where the family has only one
# weapon that can wear its parts, the same weapon is racked twice.
#
# ⚠ THAT COSTS NOTHING, AND THIS FILE USED TO CLAIM IT COST A RAIL. The claim
# was that a repeated weapon gives "a second crop but not a second rail", i.e.
# that the host's own hardware would ride along in the template. It is refuted
# by the measurement in `converge`: two IDENTICAL s1897s intersect down to the
# same ~30 px as two different guns, so what separates the two pictures is the
# ROW, not the weapon. The operator put it plainly -- 枪肯定不是变量. Hence one
# template per (part, RACK) and no penalty for `sr` and `sg` repeating a gun.
GROUPS = {
    'smg':   (('mp5k', 'vector'), ('muzzle', 'grip', 'magazine', 'stock')),
    'ar':    (('m416', 'ace32'), ('muzzle', 'grip', 'magazine', 'stock')),
    'sr':    (('awm', 'awm'), ('muzzle', 'magazine', 'stock')),
    'sg':    (('s1897', 's1897'), ('muzzle', 'stock')),
    'scope': (('lynx', 'awm'), ('scope',)),
    # ⚠ ONE PART, ITS OWN GROUP, AND WITHOUT IT THE PLAN COVERS 40 OF 41.
    # `uzi_stock` (the game calls it "Folding Stock (Skorpion, Micro UZI,
    # MP9)") fits only uzi and mp9, and neither is a host anywhere above -- so
    # it fell through every group while every gate stayed green, because no
    # gate asked whether the groups COVER the catalogue. `--plan` prints the
    # uncovered set now, and this line is what makes it empty.
    'smg2':  (('uzi', 'mp9'), ('stock',)),
}

# ⚠ HOW MANY 库存 ROWS ONE SPAWNER TRIP FILLS, and it is NOT the number of
# slots. Those two were conflated for a while: the slot count caps how many
# parts can be WORN at once (one per slot), the backpack caps how many can be
# CARRIED. One trip loads as many as the list holds and the waves then draw
# from it, so the panel opens ~8 times over a full run instead of ~26.
#
# 12 not 13: the list draws 13 rows (measured, see collect_rows_vlm) and every
# part is loaded in PAIRS -- one copy for each rack -- so the usable figure is
# the largest even number under it.
LOAD_ROWS = 12

# Ground, then level. Relative counts, applied and then undone, so `level` is
# wherever the round started -- good enough by the operator's own call ("大概
# 就行"), and the absolute alternative (walking into the pitch clamp, see
# control/aim.py) costs a probe per background for a backdrop that only has to
# be DIFFERENT.
# ⚠ FIVE TIMES the value the old collector used, because the Tab screen
# SCALES the view: the same counts move the world far less with the panel up
# than without it. 260 was carried over from legacy_collect_templates and the
# operator watched two runs go by without the view ever reaching the ground.
# There is no measurement behind the factor of 5 -- it is what the operator
# called for after watching it, and the per-frame `bg moved` figure printed by
# `converge` is what says whether it is enough.
PITCH_GROUND = 260 * 5
PITCH_STEPS = (PITCH_GROUND, 0)
TURN_COUNTS = 900               # yaw per view; lands on unrelated scenery
SETTLE_S = 0.45
SPAWN_SETTLE_S = 0.8
# How long to keep looking for a name plate before calling the row empty. The
# panel fades in after every Tab cycle; matches InventoryControl.GUN_SLOT_WATCH_S,
# which exists for the same reason and was measured there.
PLATE_WATCH_S = 1.5
FIT_SETTLE_S = 0.8
# ⚠ NOT AN ABSOLUTE 'is there something here' THRESHOLD -- an empty tile
# clears any such threshold on its own border. This is the floor on a
# DIFFERENCE between two frames of the same tile, which is the only
# template-free reading of occupancy this file has. legacy's value.
CHANGE_MIN = 6.0
SLOT_DETAIL_MIN = 40.0          # only for 'is anything drawn at all'
# The intersection is monotone -- it can only ever remove pixels -- so it
# cannot oscillate and this is not a convergence tolerance. It is a cap on a
# run that is not converging because something else is wrong (the view is not
# moving, the panel is not up), and it is REPORTED when hit, because a template
# cut short is not the same object as a converged one.
MAX_FRAMES = 40
# Below this a template is not weak, it is a DIFFERENT OBJECT: the run failed
# to converge on an icon and converged on scattered dots instead. Measured
# outcomes are ~950 px (a same-rack run) and ~30-35 px (the failed cross-rack
# one), so this sits in the empty space between two results rather than being
# tuned. `write(install=True)` refuses anything under it.
MIN_INSTALL_PX = 200
# Laplacian variance that says a 库存 row holds something. Measured occupied
# 678-5228, empty 0.5-0.8 -- the gate sits in a gap three orders wide.
ROW_DETAIL_MIN = 100.0
# How many verified passes at emptying 库存 before giving up on it. `clear_inventory`
# drags, and drags land ~93% of the time, so one call is a coin flip rather
# than a clearing -- the count is read back each pass with the Laplacian gate.
CLEAR_TRIES = 4


def intersect(acc, *crops):
    """Keep only pixels every input agrees on, byte for byte. -> BGR uint8

    Disagreement is zeroed rather than averaged: a template must not carry a
    value that nothing observed. `acc` of None starts from the first crop.
    """
    out = None
    for c in crops:
        if out is None:
            out = c.copy()
            continue
        if out.shape != c.shape:
            return None
        out[np.any(out != c, axis=2)] = 0
    if acc is None:
        return out
    if acc.shape != out.shape:
        return None
    merged = acc.copy()
    merged[np.any(acc != out, axis=2)] = 0
    return merged


def change(a, b):
    """Mean absolute difference between two crops. -> float

    The template-free "did this tile change" reading. None or a shape mismatch
    is 0.0: nothing was seen to move, which is what the callers act on.
    """
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean())


def alive(tmpl):
    """How many pixels survive. The convergence signal and the quality one."""
    return int(np.any(tmpl != 0, axis=2).sum()) if tmpl is not None else 0


def by_slot(keys):
    """Split into batches wearable at once -- one part per slot.

    ⚠ RAGGED ON PURPOSE. The muzzle list runs out before the grip list does,
    and the batches after that simply carry fewer slots. An exhausted slot is
    not an error and is not photographed; it is left empty and the batch goes
    ahead with what it has.
    """
    per = {}
    for k in keys:
        per.setdefault(ATTACHMENTS[k]['slot'], []).append(k)
    n = max((len(v) for v in per.values()), default=0)
    return [{s: v[i] for s, v in per.items() if i < len(v)} for i in range(n)]


def loads_of(waves, rows=LOAD_ROWS):
    """Group the waves into spawner trips. -> [[wave, ...], ...]

    ⚠ THE TRIP AND THE WAVE ARE DIFFERENT UNITS, and treating them as one is
    what made an earlier version open the spawner panel once per wave. A wave
    is bounded by the SLOTS (a gun wears one part per slot); a trip is bounded
    by the 库存 ROWS. Nothing links them, so a trip carries as many whole
    waves as fit and the waves draw from what is already in the backpack.

    Whole waves only: splitting one across two trips would leave half a wave's
    parts sitting in the backpack while the gun is photographed, and a part
    left in the backpack is a part PUBG bolts onto the next gun to arrive.
    A single wave larger than the load is carried alone rather than dropped.
    """
    out, cur, used = [], [], 0
    for w in waves:
        need = len(w) * 2                       # one copy per rack
        if cur and used + need > rows:
            out.append(cur)
            cur, used = [], 0
        cur.append(w)
        used += need
    if cur:
        out.append(cur)
    return out


# ⚠ EVERY PART BELONGS TO EXACTLY ONE GROUP, and the order below is what
# decides which. Without an assignment step each group simply takes everything
# its hosts can wear, and seven parts (half_grip, laser, light_grip,
# thumb_grip, vert_grip, heavy_stock, tactical_stock) get photographed twice --
# once on an SMG, once on an AR -- with the second run silently OVERWRITING the
# first. Not a wasted wave: a template whose host depends on iteration order,
# which is the shape of the law this repo pays for most often (the record must
# describe the thing that was measured).
#
# The order is FIXED, and `smg2` goes LAST despite being the most constrained
# pair. It exists to rescue ONE part -- `uzi_stock`, which only uzi and mp9
# can wear -- and nothing else can claim that one, so last costs it nothing
# there while stopping it from hoovering up `heavy_stock` and
# `tactical_stock`: those would then be measured on a pair chosen by
# iteration order rather than by intent, 3 waves where 1 will do.
GROUP_ORDER = ('scope', 'sg', 'sr', 'smg', 'ar', 'smg2')


def assign():
    """Which group measures which part. -> {group: [key, ...]}

    ⚠ A part no pair can host appears in NO list, and `--plan` prints that set
    rather than leaving it to be noticed. `uzi_stock` sat in exactly that hole
    while every gate was green.
    """
    out, taken = {g: [] for g in GROUPS}, set()
    for g in GROUP_ORDER:
        hosts, slots = GROUPS[g]
        for k, v in sorted(ATTACHMENTS.items()):
            if k in taken or v['slot'] not in slots:
                continue
            if all(fits(w, k) for w in hosts):
                out[g].append(k)
                taken.add(k)
    return out


def plan_group(name):
    """-> (hosts, [batch, ...], skipped). Pure; --plan prints it."""
    hosts, slots = GROUPS[name]
    mine = set(assign()[name])
    keys, skipped = [], []
    for k, v in sorted(ATTACHMENTS.items()):
        if v['slot'] not in slots:
            continue
        if k in mine:
            keys.append(k)
        elif all(fits(w, k) for w in hosts):
            continue                    # another group owns it; not a failure
        elif any(fits(w, k) for w in hosts):
            # One host can wear it and the other cannot, so there is no second
            # crop and no intersection -- collecting it here would produce a
            # template from a single gun under a flow whose whole claim is two.
            skipped.append(k)
    return hosts, by_slot(keys), skipped


class Collector:
    def __init__(self, rig, sc, ac, run_dir, max_frames):
        self.rig, self.sc, self.ac = rig, sc, ac
        # The name bank. Built offline from four stored frames; see
        # detector/row_name_detector.py for why the text and not the icon.
        self.names = RowNameDetector()
        # Which weapon the screen says is in each rack this round. Read in
        # rack_and_fit, stamped into the log, never taken from the request.
        self.hosts_seen = {}
        self.run_dir, self.max_frames = run_dir, max_frames
        # Every crop that goes into an intersection, kept. Driving the game is
        # what a run costs; the pixels are free and the intersection cannot be
        # undone.
        self.raw_dir = os.path.join(run_dir, 'raw')
        os.makedirs(self.raw_dir, exist_ok=True)
        self.log = []

    # ── screen ──

    def frame(self, flush=3):
        """A settled Tab frame, OWNED BY THE CALLER.

        ⚠ `.copy()` IS THE WHOLE POINT OF THIS WRAPPER. The grabber reuses one
        buffer, and `cut` returns a VIEW into it -- so two crops taken a spawn
        apart are the same pixels, and every difference between them is
        exactly 0.0. That is what `tile moved 0.0 / 0.0` was: not a threshold
        too high, not a part that failed to arrive, but a comparison of a
        frame with itself. legacy_collect_templates copies at each call site
        (`self.crop(f0, slot).copy()`); copying once here means no call site
        can forget.
        """
        f = None
        for _ in range(flush):
            f = self.ac.frame()
        return None if f is None else f.copy()

    def tab(self):
        """Get the Tab screen UP. -> bool

        ⚠ `ac.is_tab_open()` ASKS whether it is up; it does not put it up. The
        first version of this method polled that three times and returned
        False, always -- ensure_ready leaves Tab shut on purpose, so the answer
        was correct every time and the collector skipped every group without
        collecting one crop. `ensure_inventory_open` is the action; `sync()` is
        what makes the panel's contents readable afterwards.
        """
        return bool(self.rig.gun.ensure_inventory_open()) and bool(self.ac.sync())

    def plate(self, gun):
        """Name-plate ink for a rack row, read PROPERLY. -> int

        Two things have to be true before this number means anything, and each
        one has cost a run today:

          the Tab screen is UP        with it down that region is game world,
                                      and the ink reads 0 -- which is
                                      indistinguishable from an empty rack.
                                      `give_many` closes Tab, so anything
                                      called after a spawn must reopen it.
          the panel has FADED IN      a single grab lands before the plate is
                                      drawn and also reads 0. Polled, not
                                      grabbed once; legacy logged 22 phantom
                                      losses learning this.

        Returns 0 when Tab will not open, which the callers treat as "cannot
        say" rather than "no gun".
        """
        if not self.tab():
            return 0
        deadline = time.perf_counter() + PLATE_WATCH_S
        while True:
            ink = self.ac.plate_ink(gun, self.frame(flush=2))
            if ink >= PLATE_INK_MIN or time.perf_counter() >= deadline:
                return ink
            time.sleep(0.08)

    def crop(self, frame, gun, slot):
        """One weapon slot's tile out of a full-screen frame.

        ⚠ TWO FUNCTIONS NAMED `cut` EXIST AND THEY TAKE DIFFERENT THINGS.
        `detector.geometry.cut` takes the (y, x, h, w) tuple; the private one
        in legacy_collect_templates also accepts the HUD_REGIONS KEY and looks
        it up. Writing the legacy call against the geometry import raises
        `too many values to unpack` -- loudly, this time, but the same pair of
        spellings could just as easily have returned the wrong rectangle.
        """
        region = HUD_REGIONS.get(f'att_{gun}_{slot}')
        return None if region is None else cut(frame, region)

    def turn(self, yaw, pitch):
        self.rig.gun.ensure_inventory_closed()
        self.rig.view.turn(yaw, pitch, settle_s=SETTLE_S)

    def spawn(self, fn, *a, **kw):
        """Run a spawner call with Tab SHUT. -> whatever fn returns

        ⚠ THE PANEL WILL NOT OPEN OVER THE TAB SCREEN, and it does not say so.
        With Tab up the mouse drives a cursor instead of the view and the
        spawner's key never reaches the game, so the call returns its usual
        "the click went to the right entry index" and nothing arrives. Both
        racking and fitting were written the wrong way round here -- Tab opened
        first, then the spawn -- and the run reported `plate ink 0` on both
        rows with no error anywhere. legacy_collect_templates spawns first and
        opens Tab afterwards for exactly this reason.
        """
        self.rig.gun.ensure_inventory_closed()
        return fn(*a, **kw)

    # ── steps ──
    def rows_held(self, frame=None):
        """How many 库存 rows have SOMETHING in them. TEMPLATE-FREE. -> int

        ⚠ IT EXISTS TO SPLIT ONE ANSWER INTO TWO. `rows_by_name` returning
        nothing means "no row was NAMED", which covers both "the list is
        empty" and "the list is full of things I cannot read" -- opposite
        situations with opposite fixes, printed identically. Laplacian
        variance per icon box: measured occupied 678-5228, empty 0.5-0.8, so
        the gate at 100 sits in a gap three orders wide and consults no
        template.
        """
        f = capture_screen() if frame is None else frame
        n = 0
        for i in range(15):
            x0, y0, x1, y1 = icon_box(i, 'inventory')
            if y1 > f.shape[0] or x1 > f.shape[1]:
                break
            g = cv2.cvtColor(f[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
            if cv2.Laplacian(g, cv2.CV_64F).var() > ROW_DETAIL_MIN:
                n = i + 1
        return n

    def rows_by_name(self, frame=None):
        """What 库存 is holding, read off the printed NAMES. -> {row: key}

        ⚠ NOT `TabView.find()`, AND THAT IS THE WHOLE REASON THE NAME BANK
        EXISTS. `find` matches the ICON, and the parts this collector exists
        to photograph are exactly the parts whose icon template is missing or
        stale -- so the lookup that tells it what it is holding is the lookup
        it cannot make. The label is text, it is not composited, and the bank
        was bootstrapped from a reading that needed no bank.
        """
        return self.names.classify(
            capture_screen() if frame is None else frame, 'inventory')

    def reset(self):
        """Floor everything: both guns (wearing whatever) and all of 库存.

        ⚠ THIS REPLACED FOUR METHODS, and the operator's instruction is the
        whole reason: 不要扔配件了,要扔就扔枪. `clear_slots` (fill a slot so
        the right-click is safe), `shed`, `drop` (strip a wave) and the
        filler machinery all existed for ONE purpose -- getting a part OFF a
        gun -- and every one of them had to aim a gesture at a weapon slot.
        A gesture at a slot that turns out to be empty reaches the WEAPON ROW
        underneath and throws the gun out: 74 measured losses, and again this
        week. Dropping the whole weapon has no such failure mode, because the
        weapon row is what it is aiming at.

        The cost is two gun spawns per round. What it buys is the deletion of
        an entire class of failure, plus the four bodies of code that existed
        to work around it.
        """
        if not self.tab():
            print('    [!] the inventory would not open to reset')
            return False
        self.ac.clear_rack()
        for attempt in range(1, CLEAR_TRIES + 1):
            self.ac.clear_inventory()
            held = self.rows_held()
            if not held:
                return True
            print(f'    [!] 库存 still holds {held} row(s) after pass {attempt}')
        # ⚠ NOT FATAL, because the next step is measured rather than assumed.
        # A leftover row is only a problem if it ends up on a gun, and the
        # round reads back WHAT ACTUALLY LANDED -- so a stray part shows up as
        # an extra name rather than as a mislabelled crop.
        print('    [!] 库存 would not clear; the round will report whatever '
              'actually lands rather than assuming')
        return True

    def stock(self, keys):
        """Spawn each key TWICE and report WHAT IS ACTUALLY IN 库存.

        -> {key: count}

        ⚠ IT REPORTS, IT DOES NOT DEMAND. The first version required the list
        to equal the request and aborted the whole group when it did not --
        which threw away a racked, cleared pair three runs running for the
        sake of a part that had simply not arrived. What a round contains is
        whatever the SCREEN says it contains; that is this repo's own law (the
        record describes the thing that was measured), and it is also the
        robust choice, because a missing part is just a part for a later round.

        ⚠ THE RACK MUST BE EMPTY WHEN THIS RUNS. With no gun on it a spawned
        attachment goes to 库存 -- measured 3/3 in docs/game_quirks.md and
        again today, 4 batches x 13 parts with zero strays. With two guns
        racked the same spawn puts the part ON THE FLOOR (measured this
        session, twice, named off the 附近 list). That is why `reset` comes
        first and why the guns are racked afterwards.
        """
        # ⚠ KEEP WHAT THE SPAWNER SAID. Discarding it made "the game did not
        # deliver" and "we never asked properly" print the same sentence --
        # `short of two copies`, over a spawner that had refused every click
        # because the panel would not open. A shortfall has three possible
        # authors and this method must name which.
        # ⚠ `give_many`, NOT `give_attachment`. The single-item call is an L1:
        # its own docstring says "on a panel the caller opens", and this
        # collector never opened one -- so every click was refused with `not
        # on the item-spawner screen` and the run reported "short of two
        # copies", which reads as the GAME not delivering. `give_many` is the
        # L2 that opens the panel, orders the clicks by category and closes it
        # again, and it folds the duplicate keys into `times=2` by itself,
        # which is exactly the two copies this flow wants.
        #
        # switch=False: that flag presses 1/2 to select a spawned WEAPON, and
        # there is no weapon in this list.
        rec = self.spawn(self.sc.give_many, list(keys) + list(keys),
                         switch=False) or {}
        refused = ([f'{rec.get("error") or "no reason given"}']
                   if not rec.get('ok') else [])
        time.sleep(FIT_SETTLE_S)
        if not self.tab():
            print('    [!] the inventory would not open after stocking')
            return {}
        frame = capture_screen()
        named = self.rows_by_name(frame)
        got = {}
        for k in named.values():
            got[k] = got.get(k, 0) + 1
        n = self.rows_held(frame)
        print(f'    库存: {n} row(s), named ' +
              ('  '.join(f'{k}x{c}' for k, c in sorted(got.items())) or 'none'))
        missing = [k for k in keys if got.get(k, 0) < 2]
        if missing:
            if refused:
                print(f'        the SPAWNER refused: {"; ".join(refused)}')
            floor = sorted(self.names.classify(frame, 'nearby').values())
            on_floor = sorted(set(missing) & set(floor))
            if on_floor:
                print(f'        on the FLOOR, not in the list: '
                      f'{", ".join(on_floor)} — a spawned attachment falls '
                      f'when there is nowhere to put it (no backpack, or '
                      f'库存 full)')
            if n > len(named):
                print(f'        {n - len(named)} occupied row(s) have no name '
                      f'in the bank — in the list, unidentifiable')
            rest = sorted(set(missing) - set(floor))
            if rest and not refused:
                print(f'        {", ".join(rest)}: in none of the three '
                      f'places this run can see')
            print(f'        short of two copies: {", ".join(missing)} — those '
                  f'slots go unphotographed this round')
        return got

    def rack(self, hosts):
        """Put both guns on the rack, ONCE PER GROUP. -> bool

        ⚠ ONCE, not once per round, and nothing right-clicks a weapon after
        this. An earlier version opened every round by throwing both guns on
        the floor; the operator watched it and said so plainly -- 不要右键点枪
        呀,右键点枪把枪扔地上了呀. There is no need: a slot already wearing
        something takes the next part by REPLACEMENT, and the part it evicts
        goes back to 库存 ("不必先卸再装,一步到位", docs/game_quirks.md). So
        the rack is built once and every round after that only touches
        backpack ROWS.
        """
        rec = (self.spawn(self.sc.give_many, [hosts[0]], weapon_times=2)
               if hosts[0] == hosts[1] else
               self.spawn(self.sc.give_many, list(hosts)))
        if not rec.get('ok'):
            print(f'    [!] the spawner would not produce {hosts}: '
                  f'{rec.get("error")}')
            return False
        time.sleep(SPAWN_SETTLE_S)
        if not self.tab():
            print('    [!] the inventory would not open after racking')
            return False
        # ⚠ WHICH GUN IS IN WHICH RACK IS READ, NOT ASSUMED. `give_many` is
        # handed [mp5k, vector] and this used to print "rack 1: mp5k" straight
        # from that list -- the request, not the screen. Templates are stored
        # PER RACK, so two guns arriving the other way round would label every
        # r1 template with the wrong host and nothing would say a word.
        seen = ((self.ac.loadout() or {}).get('guns') or {})
        for i in (1, 2):
            ink = self.plate(i)
            print(f'    rack {i}: {seen.get(i) or "?"}, plate ink {ink}')
            if ink < PLATE_INK_MIN:
                print(f'    [!] rack {i} reads empty — one crop cannot be '
                      f'intersected')
                return False
        if sorted(x for x in seen.values() if x) != sorted(hosts):
            print(f'    [!] asked for {sorted(hosts)}, rack reads '
                  f'{sorted(str(x) for x in seen.values())} — refusing, a '
                  f'per-rack template has to know its host')
            return False
        self.hosts_seen = {i: seen.get(i) for i in (1, 2)}
        return True

    def equip(self, stocked):
        """Right-click backpack rows onto BOTH guns. -> {slot: key} photographed

        ⚠ PRESS 1, RIGHT-CLICK, PRESS 2, RIGHT-CLICK -- per part, and it is the
        operator's sequence. Right-click fits onto whichever weapon is IN HAND,
        so without the hold there is no statement about which gun got it: both
        copies can land on the same one while every step reports success.

        ⚠ THE GESTURE GOES AT A BACKPACK ROW AND NEVER AT A GUN. That is the
        whole safety argument. A right-click on a weapon SLOT reaches the
        weapon row underneath when the slot is empty and throws the gun out (74
        measured losses); a right-click on a LIST ROW has no weapon underneath
        it. Replacement means the slot never has to be emptied first.

        ⚠ THE ROW IS RE-FOUND BEFORE EVERY SINGLE CLICK. Equipping removes a
        row and everything below slides up -- and on a swap the EVICTED part
        inserts a row, which moves things too. `right_click_equip`'s own
        recorded failure is exactly this: "装对了,然后又装一次,装错了,然后说
        找不到,把枪扔了".

        VERIFIED WITHOUT A TEMPLATE, two readings that have to agree: this
        key's count in 库存 fell by two, and a tile changed on both guns.
        """
        took = {}
        for key in sorted(stocked):
            slot = ATTACHMENTS[key]['slot']
            before_f = self.frame()
            before_n = self.count_named().get(key, 0)
            placed = 0
            for gun in (1, 2):
                rows = [r for r, k in sorted(self.rows_by_name().items())
                        if k == key]
                if not rows:
                    print(f'    [!] {key}: no 库存 row holds it any more — '
                          f'cannot fit gun{gun}')
                    break
                self.ac.hold(gun)
                # verify=False: that readback is SlotDetector, which decides
                # `filled` by RECOGNISING a template -- and the parts this
                # collector exists to photograph are the ones with no template.
                # The two readings below are what is paid instead.
                self.ac.right_click_equip(gun, slot, at_inv(rows[0]),
                                          att=key, verify=False)
                placed += 1
            after_n = self.count_named().get(key, 0)
            gone = before_n - after_n
            after_f = self.frame()
            # ⚠ WHICH tile moved, not "did the expected tile move". The slot
            # comes from the catalogue, which is a claim; asking only about the
            # expected tile agrees with the claim and would photograph the
            # wrong rectangle under the right name.
            moved = {s: [change(self.crop(before_f, g, s),
                                self.crop(after_f, g, s)) for g in (1, 2)]
                     for s in SLOT_NAMES}
            hot = sorted(s for s, v in moved.items()
                         if all(m >= CHANGE_MIN for m in v))
            if gone < 2 or placed < 2:
                print(f'    {key:15} {gone} of 2 copies left 库存 after '
                      f'{placed} click(s) — not photographed')
                continue
            if hot != [slot]:
                print(f'    {key:15} left 库存, but the tiles that moved are '
                      f'{hot or "none"}, not [{slot}] — cannot attribute the '
                      f'crop, skipping')
                continue
            took[slot] = key
            print(f'    {key:15} -> {slot} on both guns '
                  f'(tile moved {moved[slot][0]:.1f} / {moved[slot][1]:.1f})')
        return took

    def count_named(self):
        """{key: how many 库存 rows hold it}, by NAME."""
        out = {}
        for k in self.rows_by_name().values():
            out[k] = out.get(k, 0) + 1
        return out

    def converge(self, batch, early_stop=True):
        """Shoot, intersect, turn, until nothing changes.

        -> {(key, rack): template}

        ⚠ EACH RACK ACCUMULATES ON ITS OWN. The first version intersected
        rack 1 with rack 2 in the same frame; that is measured and it does not
        work. The two rows sit at different heights, so the scene showing
        through their translucent tiles differs and the same part on the same
        gun does not produce the same pixels in both:

            each rack against itself, over views   ~950 px, an icon
            the two racks against each other         30 px, scattered dots
            staged (each alone, then the two)        30 px, identical to above

        Two IDENTICAL s1897s produced that, so the weapon was never the
        variable -- the row is. Hence one template per (part, RACK).

        ⚠ AND SWAPPING THE TWO GUNS BETWEEN ROWS DOES NOT HELP, which is worth
        stating because it sounds like it should. It was built (`swap_racks`,
        since deleted) on the premise that moving a gun to the other row would
        separate the gun from the row. It does not: the gun's own hardware
        travels WITH it, so both rows end up holding the same weapon's
        rendering. What removes a host's hardware is a DIFFERENT GUN IN THE
        SAME ROW -- measured, awm@r1 1534 ∩ lynx@r1 1601 -> 1045 px with the
        barrel gone.
        """
        acc = {(s, g): None for s in batch for g in (1, 2)}
        history = {(s, g): [] for s in batch for g in (1, 2)}
        done, i = set(), 0
        # A slot this batch is NOT filling, so whatever shows there is panel
        # and scene only. Falls back to a filled one rather than skipping the
        # check -- a moving icon still proves the frame changed.
        probe_slot = next((s for s in ('scope', 'stock', 'grip', 'magazine',
                                       'muzzle') if s not in batch),
                          next(iter(batch)))
        prev_f_probe = None
        # ⚠ `early_stop=False` KEEPS SHOOTING PAST CONVERGENCE, on purpose.
        # Driving the game is the expensive part and the raw pairs are free;
        # a run that stops the moment the cross-gun intersection stalls has
        # collected far too few frames to answer what the SAME-GUN
        # intersection would have converged to. One padded run lets every
        # variant be compared offline for ever after.
        want = len(batch) * 2
        while i < self.max_frames and (len(done) < want or not early_stop):
            # ⚠ A HUMAN MUST BE ABLE TO TAKE THE MACHINE BACK. This loop holds
            # the foreground and turns the view; `max_frames` bounds it in
            # frames, not in the operator's patience, and clicking away does
            # not stop it -- 2026-08-08 one of these sat on the cursor for
            # eight minutes. focus_keeper() goes False once the foreground has
            # been taken MAX_REGAINS times, so alt-tabbing away ends the batch
            # instead of fighting it. Whatever converged so far is kept.
            if not focus_keeper().ok(f'xsect {"+".join(sorted(batch))}'):
                print('    [!] the foreground was taken — stopping this batch')
                break
            # ⚠ TURN FIRST, THEN SHOOT. The first version shot the frame and
            # THEN turned, and undid the pitch in the same breath -- so every
            # capture was taken at level and the ground half of the sawtooth
            # never once reached a photograph. Only the yaw was ever varying,
            # which is why three parts "converged" in two frames.
            pitch = PITCH_STEPS[i % len(PITCH_STEPS)]
            self.turn(TURN_COUNTS, pitch)
            if not self.tab():
                print('    [!] the inventory would not open mid-sweep')
                break
            f = self.frame()
            # ⚠ DID THE VIEW ACTUALLY MOVE. Every claim this file makes rests
            # on consecutive frames showing DIFFERENT scenery, and twice now a
            # run has been read as "converged" when the truth was that nothing
            # behind the panel had changed. An empty corner of the panel is
            # the cheapest witness: it holds no icon, so anything that moves
            # there is the scene.
            moved_bg = change(prev_f_probe, self.crop(f, 1, probe_slot))
            prev_f_probe = self.crop(f, 1, probe_slot)
            print(f'    frame {i + 1:2d}  pitch {pitch:+5d}  bg moved '
                  f'{moved_bg:6.2f}' + ('   ⚠ THE VIEW DID NOT MOVE'
                                        if i and moved_bg < CHANGE_MIN else ''))
            for slot, key in batch.items():
                c1, c2 = self.crop(f, 1, slot), self.crop(f, 2, slot)
                if c1 is None or c2 is None:
                    continue
                # ⚠ THE RAW PAIR IS KEPT, EVERY FRAME. The intersection is
                # lossy and one-way: once it has run there is no way to ask
                # "were these two crops even aligned?" of the result. The
                # first sg run converged to 35 of 3969 pixels and the question
                # could not be answered at all, because nothing but the
                # template had been written. HUD_REGIONS puts the two racks
                # 301px apart for four slots and 302 for `scope`, so a
                # one-pixel misalignment is not hypothetical.
                cv2.imwrite(os.path.join(self.raw_dir,
                                         f'{key}__{i:02d}__g1.png'), c1)
                cv2.imwrite(os.path.join(self.raw_dir,
                                         f'{key}__{i:02d}__g2.png'), c2)
                for gun, c in ((1, c1), (2, c2)):
                    if (slot, gun) in done:
                        continue
                    before = acc[(slot, gun)]
                    merged = intersect(before, c)
                    if merged is None:
                        continue
                    acc[(slot, gun)] = merged
                    n = alive(merged)
                    history[(slot, gun)].append(n)
                    # Byte equality, not "the count stopped falling": two
                    # different pixels could in principle be removed and added
                    # in one step if the operation were not monotone, and
                    # relying on the count would be a weaker statement than
                    # the one available.
                    if before is not None and np.array_equal(before, merged):
                        done.add((slot, gun))
                        print(f'    {key:14} rack {gun} converged at frame '
                              f'{i + 1}, {n} px  ('
                              f'{" ".join(map(str, history[(slot, gun)][-6:]))})')
            # Back to level so the next `pitch` is measured from the same
            # place -- these are relative counts, not absolute positions.
            if pitch:
                self.turn(0, -pitch)
            i += 1
        for (slot, gun) in sorted(acc):
            if (slot, gun) not in done:
                print(f'    [!] {batch[slot]} rack {gun}: NOT converged after '
                      f'{i} frames, {alive(acc[(slot, gun)])} px still moving '
                      f'— capped, and a capped template is not a converged one')
        self.log.append({'batch': {s: k for s, k in batch.items()},
                         'hosts_seen': dict(self.hosts_seen),
                         'frames': i,
                         'converged': [f'{batch[s]}@r{g}' for s, g in sorted(done)],
                         'history': {f'{batch[s]}@r{g}': h
                                     for (s, g), h in history.items()}})
        return {(batch[s], g): a for (s, g), a in acc.items() if a is not None}

def write(templates, run_dir, install=False):
    """One BGRA per (part, RACK). -> how many were written

    ⚠ THE RUN DIRECTORY BY DEFAULT, THE LIVE BANK ONLY ON `--install`, and
    that split was bought with 30 misreads. An earlier version wrote straight
    into `data/templates/.../Attachment/`, so three templates from the failed
    cross-rack experiment -- 30, 35 and 35 opaque pixels, scattered dots --
    became variants the live AttachmentDetector loads on every frame. `pixi
    run attachments` went 2060 -> 2030 and stayed there, and nothing connected
    the two: the collector had printed success and exited days earlier.

    ⚠ AND `install` REFUSES THE THIN ONES. A template under MIN_INSTALL_PX is
    not a weak template, it is a different object -- the intersection did not
    converge on an icon, it converged on noise, and noise matches everything a
    little. The number is set below the ~950 px a same-rack run produces and
    far above the ~35 that failure produces, so it separates the two outcomes
    this flow actually has rather than being a tuned threshold.

    ⚠ THE RACK IS IN THE FILENAME AND THAT IS NOT COSMETIC. Row 1 and row 2
    produce genuinely different pictures of the same part -- measured, ~950
    surviving pixels each and only 30 in common -- so they are two templates,
    not two takes of one. Writing both to `{asset}.{TAG}.png` would have the
    second silently overwrite the first, and the survivor would be whichever
    rack happened to sort last: a template with no record of which row it
    describes, matched against both.
    """
    out = TMPL_DIR if install else os.path.join(run_dir, 'templates')
    os.makedirs(out, exist_ok=True)
    n, thin = 0, []
    for (key, rack), bgr in sorted(templates.items()):
        asset = ATTACHMENTS[key].get('asset')
        if not asset:
            print(f'  {key}: no asset name in the catalogue, not written')
            continue
        a = (np.any(bgr != 0, axis=2) * 255).astype(np.uint8)
        px = int(a.astype(bool).sum())
        if install and px < MIN_INSTALL_PX:
            thin.append(f'{key}@r{rack} ({px} px)')
            continue
        dst = os.path.join(out,
                           f'Item_Attach_Weapon_{asset}.{TAG}_r{rack}.png')
        cv2.imwrite(dst, np.dstack([bgr, a]))
        print(f'  {key:15} rack {rack}  {px:5d} px -> '
              f'{os.path.basename(dst)}')
        n += 1
    if thin:
        print(f'  [!] NOT installed, under {MIN_INSTALL_PX} px — the '
              f'intersection did not converge on an icon: {", ".join(thin)}')
    print(f'  -> {os.path.relpath(out, ROOT)}'
          + ('' if install else '   (--install writes the live bank; run '
                                '`pixi run attachments` after)'))
    return n


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--group', action='append', choices=sorted(GROUPS),
                    help='which family; repeatable')
    ap.add_argument('--all', action='store_true', help='every group')
    ap.add_argument('--plan', action='store_true',
                    help='print the plan and exit; no game needed')
    ap.add_argument('--max-frames', type=int, default=MAX_FRAMES)
    ap.add_argument('--full', action='store_true',
                    help='shoot every frame up to --max-frames even after the '
                         'intersection stops changing, so the raw pairs can '
                         'answer questions this run was not asking')
    ap.add_argument('--install', action='store_true',
                    help='write the LIVE template bank, not just the run dir')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    groups = sorted(GROUPS) if args.all else (args.group or [])
    if not groups:
        ap.error('give --group or --all')

    plans = [(g,) + plan_group(g) for g in groups]
    total = sum(len(b) for _g, _h, b, _s in plans)
    trips = sum(len(loads_of(b)) for _g, _h, b, _s in plans)
    print(f'{len(plans)} group(s), {total} wave(s) in {trips} spawner trip(s), '
          f'up to {args.max_frames} frames each')
    for g, hosts, batches, skipped in plans:
        print(f'\n  {g}  hosts {" + ".join(hosts)}'
              + ('   (same weapon twice — fine; the ROW is the variable, not '
                 'the gun)' if hosts[0] == hosts[1] else
                 '   (two weapons — needed only for the scope slot)'))
        w = 0
        for t, trip in enumerate(loads_of(batches), 1):
            print(f'    load {t}: {len(trip) and sum(len(x) for x in trip)} '
                  f'part(s) x2 = {sum(len(x) for x in trip) * 2} rows')
            for b in trip:
                w += 1
                print(f'      wave {w:2d}. '
                      + '  '.join(f'{s}={k}' for s, k in sorted(b.items())))
        if skipped:
            print(f'    skipped (only one host can wear them): '
                  f'{", ".join(skipped)}')
    # ⚠ WHAT NO GROUP COVERS, printed every time. `uzi_stock` fell through all
    # five original groups -- it fits only uzi and mp9, neither of which was a
    # host -- and nothing said a word, because every gate asked "is this plan
    # self-consistent" and none asked "does it cover the catalogue". A plan
    # that silently omits a part looks exactly like a plan that includes it.
    planned = {k for _g, _h, bs, _s in plans for b in bs for k in b.values()}
    if args.all:
        missed = sorted(set(ATTACHMENTS) - planned)
        print(f'\ncoverage: {len(planned)}/{len(ATTACHMENTS)} parts planned'
              + (f'   ⚠ NO GROUP COVERS: {", ".join(missed)}' if missed else ''))
    if args.plan:
        return 0

    ready = ensure_ready(label='intersect collection', countdown_s=args.countdown)
    if not ready['ok']:
        print(f'[!] ABORT: not ready — failed at {ready["failed"]!r}')
        return 1

    stamp = time.strftime('%Y%m%d_%H%M%S')
    run_dir = os.path.join(OUT_ROOT, stamp)
    os.makedirs(run_dir, exist_ok=True)
    rig = Rig('red_dot')
    sc, ac = SpawnerControl(verbose=False), InventoryControl(verbose=False)
    col = Collector(rig, sc, ac, run_dir, args.max_frames)
    templates = {}
    try:
        for g, hosts, batches, _skipped in plans:
            print(f'\n══ {g}: {" + ".join(hosts)} ══')
            # ⚠ SET UP ONCE PER GROUP. Clearing the rack is the only thing
            # here that right-clicks a weapon, and it happens once, before any
            # gun matters. Every round after this touches backpack ROWS only.
            if not col.reset():
                continue
            # The backpack goes on AFTER the clearing: one that spawned into
            # 库存 rather than onto the character gets thrown on the floor by
            # `clear_inventory`, and then every part stocked afterwards has
            # nowhere to go and falls on the floor too -- which printed as
            # "库存 empty, guns bare" and reads as a broken spawner.
            col.spawn(sc.give_many, [BACKPACK])
            # ⚠ ROUND 1'S PARTS GO IN *BEFORE* THE GUNS ARRIVE, and the reason
            # is a documented blind spot rather than tidiness. A spawned gun is
            # NOT bare -- it wears factory attachments -- and s1897 ships with
            # bullet_loops in the very slot this group tests. Fitting a part
            # identical to the one already worn swaps it out and straight back
            # in, so 库存 loses a row and gains one: net zero. game_quirks
            # states it outright -- 装同一个配件无法验证,「换成功」和「什么都
            # 没发生」分不开 -- and this collector's count check walked into it,
            # reporting `0 of 2 copies left 库存` for a part that a `loadout()`
            # readback showed sitting on BOTH guns.
            #
            # A weapon arriving picks up what it can wear from the backpack, so
            # stocking first means the tested slots are filled with OUR parts
            # from the start. Every later round then swaps a known part for a
            # DIFFERENT known part, which is exactly the disambiguation
            # game_quirks recommends (换两个不同的配件交替).
            first = sorted(batches[0].values()) if batches else []
            if first:
                print(f'    pre-stocking round 1 so the guns arrive wearing '
                      f'ours, not the factory\'s: {" ".join(first)}')
                col.stock(first)
            if not col.rack(hosts):
                continue
            for i, wave in enumerate(batches, 1):
                keys = sorted(wave.values())
                print(f'\n── {g} round {i}/{len(batches)}: '
                      f'{" ".join(keys)}')
                stocked = col.stock(keys) if i > 1 else col.count_named()
                if not stocked:
                    print('    nothing to fit this round')
                    continue
                # ⚠ NO UNEQUIP BETWEEN ROUNDS. Round 2 right-clicks the next
                # part for the same slot and the game REPLACES, sending the
                # incumbent back to 库存 ("不必先卸再装,一步到位"). Stripping
                # first is what used to aim a gesture at a weapon slot, and an
                # empty slot passes that gesture through to the weapon row.
                took = col.equip(stocked)
                if took:
                    templates.update(col.converge(
                        took, early_stop=not args.full))
                else:
                    print('    no part reached both guns — nothing to '
                          'photograph, moving on')

    except KeyboardInterrupt:
        print('\ninterrupted')
    finally:
        with open(os.path.join(run_dir, 'log.json'), 'w', encoding='utf-8') as fh:
            json.dump(col.log, fh, indent=1, ensure_ascii=False)
        # ⚠ LEAVE THE SCREEN DOWN. A crash or a Ctrl-C mid-sweep ends with the
        # inventory UP, and an open Tab swallows the next run's keys -- so the
        # failure this run had shows up as a spawner that "would not open" in
        # the NEXT one. Best effort: the game may already be gone.
        try:
            rig.gun.ensure_inventory_closed()
        except Exception as e:
            print(f'[!] could not put the inventory away: {e}')
        try:
            ac.close()
        except Exception:
            pass
        rig.close()
    print(f'\n{len(templates)} template(s):')
    write(templates, run_dir, install=args.install)
    print(f'log -> {os.path.relpath(os.path.join(run_dir, "log.json"), ROOT)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
