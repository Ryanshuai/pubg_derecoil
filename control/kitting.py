"""Put a named set of attachments on the gun, and prove it went on.

    from control.kitting import Kitter, want_for, stock_parts

    kit = Kitter(rig)
    kit.aim_at('m416')
    kit.apply(want_for('m416', 'ar', {'muzzle', 'grip'}), weapon='m416')

⚠ THIS FILE IS THE HALF OF A 3364-LINE MODULE THAT SURVIVED OF TWO DIFFERENT
JOBS. The other one was the bullet-bucket measurement loop — measure_cell,
harvest_weapon, the EMA writeback and the convergence bookkeeping — and it went
on 2026-08-08 when MODEL.md replaced the coordinate. What is left never knew
what a bullet bucket was: it answers "is this gun wearing what the cell says",
which is the same question under any model of recoil.

The split is why the deletion was possible at all. `Kitter` and `BACKPACK` had
six importers, and two of them — calibration/collect_templates.py and
tools/verify_kit.py — have nothing to do with recoil. Deleting harvest.py
wholesale would have broken the template-collection chain, which is what the
calibrate-template skill is built on.

WHAT THE KITTING LAYER IS FOR, in one paragraph, because it is the single
largest source of wasted runs in this project: PUBG auto-fits whatever the
backpack holds onto a gun the moment it arrives, so a slot nobody mentioned is
NOT empty — it holds whatever the last teardown left lying around. `want_for`
pins every controlled slot, filled or empty, and `Kitter.apply` reads each one
back. A cell labelled `bare` that quietly ran wearing a grip is not a
hypothetical; it is what this machinery exists to stop.
"""
import argparse
import contextlib
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from detector.attachment_catalog import fits, has_slot
import detector.weapon as weapon_mod
from detector.weapon import Weapon, WEAPON_RPM, can_full_guns

from control.session import ensure_ready
from control import spawner as spawner_mod
from control.spawner import SpawnerControl, ROSTER
from control.inventory import InventoryControl, slot_matches
from control.stock import open_tab, restock
from calibration.kit_facts import KitFacts
# ONE construction site for the fired Weapon; fit_curve.rebuild uses the same
# one. Two copies drifted and the scope fix reached only this file --
# see calibration/weapon_build.py.

# ── when a cell is allowed to write its own curve ───────────────────────────
#
# The thresholds come from 2026-08-06's ten cells, split by hand into the nine
# that deserved to ship and the one that did not:
#
#     shipped      relative spread 0.03% .. 1.10%   (at three magazines)
#     the bad one                   7.79%
#
# so 3% sits in a 7x gap. `spread` is the standard deviation of the accepted
# magazines' residuals divided by the curve total, which is what makes a
# 22-round VSS comparable with a 42-round AUG.
EMA_MIN_MAGS = 3            # before anything is written at all
EMA_SPREAD_MAX = 0.03       # of the curve total, across those magazines
EMA_RESID_MAX_FRAC = 0.50   # a residual this big is a fault, not a refinement
# ── converged means the residual is gone, not that the steps got small ──────
#
# It used to compare the size of an UPDATE against the cell's scatter, and that
# test is very nearly vacuous. An update is alpha x residual and the scatter is
# sigma, so "moved < scatter" is just
#
#     |residual| < sigma / alpha
#
# and with the VSS's measured sigma of 34.6 counts at alpha 0.167 that is
# |residual| < 207 -- 12% of its curve. Measured 2026-08-06: a 40-magazine run
# stopped after FOUR, declaring convergence at +55.1 counts with a standard
# error of 17, i.e. 3.2 sigma from zero.
#
# So ask the question convergence actually is: given how many magazines this
# cell has fired, can the residual still be told apart from zero? That is a
# t-test against the cell's own sem, it needs no tuning constant, and it gets
# STRICTER as evidence accumulates rather than weaker.
#
# The per-magazine noise this is measured against, for scale: VSS bare, 16
# magazines, curve frozen -- sd 34.6 counts (2.1% of the curve), lag-1
# autocorrelation -0.075, sd/MAD 0.94. Independent, near-normal, stationary
# within a session. That is what makes sem = sd/sqrt(k) the right ruler.
EMA_CONVERGED_Z = 2.0       # confidence multiplier on the cell's own sem
# ⚠ AND A TOLERANCE, because a t-test alone cannot say what convergence means.
# "|mean| < Z x sem" is the statement THERE IS NO EVIDENCE OF A RESIDUAL, and
# that is satisfied by measuring badly: replayed over 2026-08-06's cells it
# declared the night's worst cell converged -- +144 counts of residual with a
# sem of 98, t = 1.5 -- because its own noise hid it. What is wanted is
# EVIDENCE OF NO RESIDUAL, which is an equivalence test: the whole interval
# has to sit inside a band worth caring about.
#
#     |mean| + Z * sem  <  EMA_TOL_FRAC * curve
#
# so a big residual fails it AND a noisy cell fails it, and the only way to
# pass is to fire enough magazines to shrink sem.
#
# 1% of the curve, because 0.5% is the floor this loop cannot go below anyway:
# ALPHA_MAG_FLOOR = 0.10 leaves a permanent sigma*sqrt(a/(2-a)) of noise
# written into the curve, which at the VSS's measured sigma of 34.6 counts is
# 7.9 -- 0.5% of its 1676. A target under that is asking the loop to beat its
# own design. 1% is twice the floor and, at sigma = 2.1% of the curve, needs
# k > (2*2.1/1)^2 ~ 18 magazines. That is the real cost of convergence and it
# is why runs of 3 magazines never reached it.
EMA_TOL_FRAC = 0.01
# How far a write may sit from alpha x the reported residual before the loop
# is not applying what it measured. 30% covers smoothing and the shape term
# redistributing; the real failure was 7 against 16, i.e. 57% off.
EMA_APPLY_REL = 0.30
# Below this the cumulative test abstains.
#
# ⚠ PROVISIONAL, and the honest reason is written here rather than implied by a
# round number. The test compares what the curve moved against alpha x the
# reported residual, and it has an error of its own -- measured 2026-08-06 over
# the 7 VSS magazines that rebuild() would still accept, |error| ran 0.6 to
# 10.6 counts and CORRELATED WITH THE SIZE OF THE WRITE (r = 0.83), so it is
# proportional rather than a fixed offset. Where that proportionality comes
# from is not established; the sample is 7 and biased, because rebuild refuses
# any magazine whose pattern_counts no longer matches the curve, which after a
# few writes is most of them.
#
# 15 gave the test 1.5x headroom over its own error and it duly cried STALLED
# twice on healthy runs (+10 against +26, +18 against -20 -- the second of
# those was real). 60 is 6x the worst error seen. It costs sensitivity: a
# mechanism dropping HALF of every correction is only caught once the running
# total due passes 60 counts, which at alpha 0.10 is six magazines of a 100-
# count residual.
#
# What would replace this guess: many magazines against a FROZEN curve, which
# is the only way rebuild will accept enough of them to characterise its own
# error. That run has not been done.
EMA_APPLY_MIN = 60.0

HERE = os.path.dirname(os.path.abspath(__file__))
# Runs are measurements, not source: they land under docs/ with the rest of
# what this repo has measured, never next to the script that wrote them.
RUNS = os.path.join(os.path.dirname(HERE), 'docs', 'recoil', 'runs')

# Which part fills each slot under test, per weapon class. A class that has no
# part for a slot skips every config naming it, rather than silently measuring
# bare twice under two different labels.
#
# Overridable per slot with --parts muzzle=brake_ar,grip=tilted_grip, which is
# how a second part in the same slot gets measured against the first.
# THE STOCK SLOT USED TO NAME tactical_stock AND THAT MEASURED NOTHING.
# Three cells across two weapons: 0.9942±0.0080, 1.0113±0.0120, 1.0025±0.0078.
# An identity part, so the whole stock axis was measuring a part that does not
# move the number — and it did that while the game's own wiki claims -20.00%
# Recoil Pattern Scale for it, a ~25 sigma disagreement nobody had looked at.
#
# heavy_stock, which had never been measured at all, is 0.8346 +- 0.0100 on
# mp5k against a bare cell of 1009.9 +- 2.1 (5 magazines, cv 0.5%). Sixteen
# sigma from 1.0. So the SLOT is real and it was the PART that was inert, and
# the representative had to be the one that moves.
#
# It also multiplies, which is not something the other slots can be assumed to
# do: on this same mp5k, muzzle x stock came out pred 0.4952 / meas 0.5143,
# +1.4 sigma — multiplicative — while muzzle x grip on the same weapon is
# 8.5-12.8 sigma from multiplicative. "The slots do not multiply" is a
# property of the muzzle-grip edge, not of the model.
PART_FOR_CLASS = {
    'AR':  {'muzzle': 'comp_ar',  'grip': 'vert_grip', 'stock': 'heavy_stock'},
    'DMR': {'muzzle': 'comp_ar',  'grip': 'vert_grip', 'stock': 'heavy_stock'},
    'SMG': {'muzzle': 'comp_smg', 'grip': 'vert_grip', 'stock': 'heavy_stock'},
    # The M249 takes the AR magazine and a stock, but no compensator — the
    # AR comp lists 突击步枪/精确射手步枪/O12/S12K and not the M249.
    'LMG': {'muzzle': None,       'grip': 'vert_grip', 'stock': 'heavy_stock'},
}

# Every slot this tool controls. A config names the ones to FILL; the rest are
# forced empty, never left alone. PUBG auto-fits whatever the backpack holds
# onto a gun the moment it arrives, so an unmentioned slot is not empty — it is
# whatever the last strip left lying around. The first "bare" run came back
# wearing a cheek pad it was never asked for, and a cheek pad reduces recoil.
TEST_SLOTS = ('muzzle', 'grip', 'stock')

# Level 3, the largest. Capacity is the whole reason it is here — the parts
# for a full factorial plus the spares shuttling on and off the gun have to fit
# at once, and the panel's own 物品 N/200 counter is the backpack's.
BACKPACK = 'backpack3'

# How many times to look for a freshly spawned gun's NAME PLATE, and how long
# to wait between looks. The plate is drawn by the same UI that is still
# animating the pickup, so one look samples a screen in motion: the m249 cell
# read {1: None, 2: None} twice while both rack slots plainly held a gun
# wearing a sight, a magazine and a stock. Cheap — a successful read breaks out
# on the first pass and the retries cost nothing.
FIND_TRIES = 3
FIND_SETTLE_S = 0.4

# How many times to set a weapon up (stock the parts, spawn it, find it) before
# giving up on it. More than one only ever helps for ONE reason -- we were
# evicted from the range and the second attempt runs after re-entering -- so
# the loop refuses to spend the second attempt on anything else: if the session
# says we are still in the range, one failure is the answer.
SETUP_TRIES = 2

SETUP_SAYS = {
    'retry': 'not in the range any more — evicted mid-run, not a spawn '
             'failure. Re-entering and re-stocking.',
    'spawner': 'still in the range, so this really is the spawner or the '
               'readback — not an eviction.',
    'exhausted': 'evicted from the range and {n} attempts did not get '
                 '{weapon} back — stopping this weapon.',
    'no-session': 'setting {weapon} up failed and there is no session to ask '
                  'whether we are still in the range.',
}


def setup_verdict(in_range, attempt, tries=SETUP_TRIES):
    """Why did setting a weapon up fail, and is retrying worth anything?

    `in_range` is the session's answer, or None when there is no session.
    Pure, because the branch it replaces is the one that got 2026-08-06 wrong
    in the expensive direction: retrying a spawn that had nothing to do with
    the spawner, while the real cause was written on the screen.

    ⚠ Retrying is gated on `in_range` being FALSE, not on the attempt budget.
    A gate that only counted attempts would happily burn the second one on a
    genuine spawner failure -- another 20 s of panel clicks for the same
    answer -- and, worse, would report an eviction that never happened.
    """
    if in_range is None:
        return 'no-session'
    if in_range:
        return 'spawner'
    if attempt + 1 >= tries:
        return 'exhausted'
    return 'retry'

# Which magazines are admissible is decided by analysis.magazine_fault, and
# its gates (ADS_FRAC_MIN, HAND_COUNTS_MAX, OOR_FRAC_MAX, ROUNDS_TOL, Z_MAX)
# live there with it — they are properties of the measurement, not of this
# run's schedule.

# How far the fitted fire rate may sit from the one the curve is timed to
# before the cell re-times and refires. Deliberately tight: the error is a
# phase that grows with the bullet number, so 2% is nothing at bullet 5 and
# most of a bullet by round 40. Stays here: re-timing is a thing this run
# DOES, not a verdict on a magazine already fired.
RPM_TOL = 0.02

# The sight is pinned, not tested. Magnification is a different axis from
# recoil reduction: a scope does not damp the gun, it magnifies the view, so
# the compensation has to scale with it and the measurement's own K changes
# with it too (RECOIL_SIGHT_PROFILES). Mixing that into an attachment factorial
# would confound the two. Red dot is 1x, where counts and pixels agree.
#
# PINNED, BUT NO LONGER A CONSTANT. The paragraph above is still the reason
# the sight is not one of TEST_SLOTS — it belongs to a different axis and
# mixing it into an attachment factorial confounds two things. What it is not
# a reason for is being unable to measure that axis at all, and until
# 2026-08-04 this was a literal, so `--sight 4x` changed the MEASUREMENT
# profile (K, patch columns, keepout) while the gun kept wearing a red dot.
# Every number that came out of such a run was the 4x's K applied to the red
# dot's picture.
#
# The two now move together: --sight picks the profile, SIGHT_SCOPE picks the
# part, and --scope overrides only for someone who means to break the pairing.
#
# WHAT THE PAIR IS FOR. Whether a sight changes the WEAPON's recoil has never
# been measured — calibrate_k.py measured the geometry (counts to pixels),
# which is a different question. Run the same bare/muzzle pair at two sights
# and compare the FACTORS: the sight cancels in each ratio, so equal factors
# mean the sight is orthogonal to the muzzle, and unequal ones mean it is not.
# The state the PITCH is positioned in, always, whatever optic the magazine is
# fired through. See GunDriver.ensure_hip: the clamps are the character's, only
# counts-per-degree is the sight's, so doing the move in one fixed state means
# one measured travel serves every scope instead of one measurement per scope.
HIP = 'hipfire'

SIGHT_SCOPE = {
    'red_dot': 'red_dot',
    '2x': 'scope_2x',
    '3x': 'scope_3x',
    '4x': 'scope_4x',
    # Carried by the weapon, not fitted. VSS has no sight slot at all.
    'vss_pso1': None,
    'hipfire': None,
}
SCOPE_PART = 'red_dot'

# Weapons that cannot wear the pinned sight because they carry their own.
# The VSS has a fixed PSO-1 at 4x and no sight slot at all, so measuring it
# with the red dot's K reported a recoil of MINUS 482 counts over a magazine.
# config.RECOIL_SIGHT_PROFILES already had the right profile; nothing was
# choosing it.
SIGHT_FOR = {'vss': 'vss_pso1'}

# The magazine is pinned the other way: always fitted, never stripped. It
# changes capacity, not recoil, and capacity is free measurement — 39 rounds
# against 29 on the AUG. A curve measured long is trivially truncated for a
# player carrying the base magazine, whereas one measured short can never be
# extended.
#
# 扩容弹匣 (ext), not 加长快速弹匣 (quickext): the plain extended magazine is
# the one that holds the most. The quickdraw variant's faster reload is dead
# time between magazines, which is worth nothing next to a longer curve.
MAG_FOR_CLASS = {'AR': 'ext_ar', 'DMR': 'ext_ar', 'LMG': 'ext_ar',
                 'SMG': 'ext_smg'}


def parse_config(name):
    """A config name is the set of slots to FILL, joined by '+'.

    'bare' fills nothing; 'muzzle+grip+stock' fills all three. Any subset is
    legal, so one --configs spells out a full 2^N factorial or any fraction of
    one, and adding a slot to TEST_SLOTS needs no change here.

    Returns None for a name that mentions a slot this tool does not control.
    """
    if name == 'bare':
        return frozenset()
    if name == 'both':          # kept: the 2x2 runs already logged say 'both'
        return frozenset(('muzzle', 'grip'))
    slots = frozenset(p.strip() for p in name.split('+') if p.strip())
    return slots if slots <= frozenset(TEST_SLOTS) else None


def config_name(slots):
    """Canonical name for a slot set, so --resume matches across runs."""
    return '+'.join(s for s in TEST_SLOTS if s in slots) or 'bare'


def effective_config(weapon, cfg, parts):
    """The part of `cfg` this weapon can physically wear.

    PART_FOR_CLASS answers "does this CLASS have a part for the slot". It
    cannot answer "does this WEAPON have the slot", and the two are not the
    same question: 11 of the 23 full-auto guns have no lower rail at all --
    AKM, Groza, FAMAS, K2, UZI, Mk14, VSS, MP9, P90 and both LMGs -- while
    their classes obviously do.

    Asking for a grip on one of those is not a harder measurement, it is an
    impossible one. The old code spawned the gun, failed to kit it, and moved
    on without firing a shot: half the roster silently produced no data.
    Degrading the config instead still yields a curve, correctly labelled with
    what was actually on the gun.
    """
    keep = {s for s in parse_config(cfg)
            if parts.get(s) and has_slot(weapon, s)
            and fits(weapon, parts[s])}
    return config_name(frozenset(keep))


def supported_configs(weapon, configs):
    """The DISTINCT configs `weapon` can actually wear, order preserved.

    A planner asking a mixed roster for the same eight slot combinations gets
    eight cells for the m416 and one for the groza, without knowing anything
    about either. `effective_config` already answers the per-config half by
    degrading to what the gun has; this adds the de-duplication that turns a
    degraded list into a PLAN.

    ⚠ DE-DUPLICATION IS THE WHOLE POINT, not tidiness. Degradation is
    many-to-one: on a weapon with no lower rail, 'grip', 'muzzle+grip' and
    'grip+stock' all collapse onto configs it already has. Feeding those to a
    manifest writes rows whose ids collide, so one cell's result overwrites
    another's and the night reports fewer measurements than it made -- and
    feeding them to a loop that halts on consecutive failures spends the halt
    streak on a plan error rather than a rig fault.

    Lives here rather than in the planner because the question is "what can
    this weapon wear", which is what this module's catalogue helpers answer.
    harness/ is forbidden to import detector (layering rule 5) for exactly the
    reason that would have applied: a second opinion about the game's own
    facts, sitting beside the one it is supposed to be judging.
    """
    cls = ROSTER.get(weapon, (None,))[0]
    parts = PART_FOR_CLASS.get(cls, {})
    out, seen = [], set()
    for c in configs:
        if parse_config(c) is None:
            continue
        eff = effective_config(weapon, c, parts)
        if eff not in seen:
            seen.add(eff)
            out.append(eff)
    return out


def fixed_kit(weapon, cls):
    """Sight and magazine, filtered by what this weapon takes.

    Both are pinned rather than tested (see SCOPE_PART, MAG_FOR_CLASS), but
    pinned is not the same as universal -- the VSS carries its own scope and
    takes neither.
    """
    out = {}
    for slot, key in (('scope', SCOPE_PART), ('magazine', MAG_FOR_CLASS.get(cls))):
        if key and has_slot(weapon, slot) and fits(weapon, key):
            out[slot] = key
    return out


def want_for(weapon, cls, fill=frozenset()):
    """The full `want` dict for one config: pinned kit plus the test slots.

    `fill` names the TEST_SLOTS to fill; every other slot the weapon HAS is
    pinned to None, meaning it must end up EMPTY rather than be left alone.
    PUBG bolts whatever is in the backpack onto a gun the moment it arrives,
    so an unnamed slot holds whatever the last strip left lying around — the
    first BARE run this project measured was wearing a cheek pad, and a cheek
    pad reduces recoil.

    A slot the weapon does not have is left out entirely: naming it would ask
    the kitter to prove an absence it cannot see, since a slot that is not
    drawn reads exactly like one that is drawn empty.

    Factored out of harvest_weapon so the harness can ask for the same
    configuration without importing detector/ (layering rule 7) and without
    growing a second copy of the force-empty rule, which is the one that has
    already been got wrong once.
    """
    parts = PART_FOR_CLASS.get(cls, {})
    want = fixed_kit(weapon, cls)
    want.update({s: (parts.get(s) if s in fill else None)
                 for s in TEST_SLOTS if has_slot(weapon, s)})
    return want


class Kitter:
    """Puts a named set of attachments on the gun, and proves it landed.

    Parts are spawned once and then shuttled between the gun and the backpack.
    Spawning fresh ones per weapon would work too, but every spare in 库存 is
    one more thing find() can pick instead of the one meant.

    `restock` is called with the keys a config needs but cannot see, and is
    expected to put them in the backpack. Without one, a missing part is still
    a hard failure — the point of asking first is that it almost never is.
    """

    def __init__(self, rig, slot=2, verbose=False, restock=None):
        self.rig = rig
        self.slot = slot
        self.ac = InventoryControl(verbose=verbose)
        self.restock_fn = restock
        # The last config asked for, so clear_rack knows which slots hold
        # THIS RUN's parts and which hold whatever the spawner fitted by
        # itself. See clear_rack. None until the first apply(), and that is
        # the conservative state: it falls back to stripping everything.
        self.last_want = None
        # True while session() holds the Tab screen for a whole weapon,
        # so the per-method closes stand down. See session().
        self._session = False
        # Slots that would not take what they were asked for, from the last
        # apply(). The caller uses it to drop those slots and measure anyway
        # rather than losing the weapon's whole cell to one stale catalogue
        # entry, and to log the failure for a human to check.
        self.last_bad = []

    def close(self):
        try:
            self.ac.close()
        except Exception:
            pass

    def _open(self):
        return open_tab(self.ac, label='kitting')

    @contextlib.contextmanager
    def session(self):
        """Hold the Tab screen for a whole weapon. -> yields bool

        THE SCREEN IS A PRECONDITION, NOT AN ACTION. Every read in here needs
        the Tab screen up; none of them needs it OPENED. Stated by the
        operator on 2026-08-06: "只是有个 pre-request 说你要检测东西必须在 tab
        状态里" -- so the state is established once and declared, instead of
        each helper re-establishing it as though it were the only reader.

        ac.tab_up() already composes correctly when nested (it opens only if
        shut and closes only what it opened), so nothing here needs a refcount.
        What broke the nesting was the four explicit closes in this class,
        which shut a screen somebody upstream was holding. `_session` gates
        them; this holds it.

        Measured before it existed: 184 blocks of four or more consecutive Tab
        toggles with no gesture between them, 1477 key presses, 80% of every
        Tab press in the shared journal.
        """
        with self.ac.tab_up() as ok:
            self._session = True
            try:
                yield ok
            finally:
                self._session = False

    def find_gun(self, weapon):
        """Point this Kitter at whichever rack slot holds `weapon`. -> slot|None

        **The spawner does not promise a slot and never did.** From
        control/spawner.py: "an empty rack takes it into slot 1, anything else
        puts it in slot 2 ... A caller that needs the gun in a PARTICULAR slot
        decides how to get it there." This is that decision, and reading the
        rack back is the cheap half of it.

        It was a constant here for a long time, with the help text "the
        spawner always fills slot 2", which is true only while the rack is
        already occupied. That holds for every weapon after the first and
        fails on the first — and range re-entry empties the rack, so "the
        first" comes round again every eviction. The failure is silent in the
        worst way: read_slots() on an empty rack slot answers '' for every
        slot, so the run reports the parts it just fitted as missing and
        records a compatibility failure against the CATALOGUE. One night of
        that put `aug.magazine.ext_ar` on the suspect list three times, for a
        magazine the gun plainly takes.

        The tell, worth keeping: a magazine slot cannot legitimately read ''.
        A gun out of the spawner ARRIVES wearing one (see weapon_axis.py), so
        '' there is not a fitting failure, it is nothing being looked at.
        """
        if not self._open():
            return None
        racked, worn = {}, {}
        try:
            # Read up to FIND_TRIES times. The gun was spawned seconds ago and
            # the name plate is drawn by the same UI that is still animating
            # the pickup; a single look is a sample of a screen in motion.
            for attempt in range(FIND_TRIES):
                if not self.ac.sync():
                    print("      [!] the Tab screen would not sync — cannot "
                          "locate the gun")
                    return None
                racked = dict(self.ac.guns)
                worn = self.ac.read_slots()
                if any(k == weapon for k in racked.values()):
                    break
                if attempt + 1 < FIND_TRIES:
                    time.sleep(FIND_SETTLE_S)
        finally:
            # Only if nobody upstream is holding the screen. See
            # Kit.session(): a forced close here shuts a Tab screen the
            # caller opened for the whole weapon, and the next helper
            # reopens it -- which is the OCOCOC alternation measured at
            # 80% of every Tab press in the corpus.
            if not self._session:
                self.rig.gun.ensure_inventory_closed()

        for slot, key in sorted(racked.items()):
            if key == weapon:
                if slot != self.slot:
                    print(f"      {weapon} is in rack slot {slot}, not "
                          f"{self.slot} — kitting slot {slot}")
                self.slot = slot
                return slot

        # Not found, and there are TWO reasons for that which look identical
        # from `racked` alone. Saying the wrong one sends the morning to the
        # wrong module, so it is worked out rather than guessed.
        #
        # This message used to read "the spawn did not land, or it was
        # evicted", asserting two causes it had not checked. The m249 cell
        # proved it wrong twice in a row: `guns` came back {1: None, 2: None}
        # while `slots` showed BOTH rack slots wearing a sight, a magazine and
        # a stock -- and a slot cannot hold a magazine with no gun in it. The
        # spawn had landed, both times; the NAME PLATE would not read. The
        # second attempt then spawned a second m249 on top of the first.
        occupied = sorted(g for g, s in (worn or {}).items()
                          if any((s or {}).values()))
        if occupied:
            print(f"      [!] rack slot(s) {occupied} hold a gun wearing "
                  f"parts, but no name plate would read ({racked}). The spawn "
                  f"landed; the WEAPON NAME detector did not. Refusing rather "
                  f"than assuming it is the {weapon} — measuring the wrong gun "
                  f"under this label is worse than losing the cell.")
        else:
            # ⚠ This used to end "— the spawn did not land", which is the very
            # thing the paragraph above refuses to do: assert a cause it has
            # not checked. An empty rack ALSO reads exactly like this when the
            # game has evicted us and the spawner clicks went into a modal
            # (2026-08-06, the AFK kick). Both remaining causes are named, and
            # the caller settles it by asking the session which one it is.
            print(f"      [!] {weapon} is not in the rack, and both slots are "
                  f"empty ({racked}) — either the spawn did not land, or we "
                  f"are no longer in the range.")
        return None

    def strip(self):
        """Everything off, back into 库存. Must happen BEFORE the next weapon
        is spawned: a full weapon rack means the incoming gun evicts the old
        one onto the floor, and it takes its attachments with it."""
        if not self._open():
            return False
        try:
            self.ac.strip(self.slot)
        except Exception as e:
            print(f"      [!] strip failed: {e}")
            return False
        finally:
            # Only if nobody upstream is holding the screen. See
            # Kit.session(): a forced close here shuts a Tab screen the
            # caller opened for the whole weapon, and the next helper
            # reopens it -- which is the OCOCOC alternation measured at
            # 80% of every Tab press in the corpus.
            if not self._session:
                self.rig.gun.ensure_inventory_closed()
        return True

    def _swap_back(self, gun, weapon):
        """Click every backpack row onto `gun`, then the caller throws it.

        THE WHOLE TEARDOWN, and it needs no bookkeeping at all. After the
        fitting, the backpack holds exactly what the fitting displaced -- the
        spawner's own attachments, evicted one per slot when this run's parts
        went on. Clicking all of them back reverses that swap wholesale: the
        junk returns to the gun, and each one it lands on evicts THIS RUN's
        part into the backpack, which is where it has to be for the next
        weapon. One uniform pass, no per-slot state, no classification of what
        counts as junk.

        Specified by the operator on 2026-08-06 after four revisions of a
        version that tracked slots: "测完以后 ... 把包里现在所有东西都点一遍,
        点到枪上 ... 然后这时候再点序号一, 就把枪带着原始配件全扔了".

        ⚠ WHY THE EARLIER VERSIONS WERE WORSE, so nobody rebuilds one: they
        asked "which part is junk?" and answered it from stored state -- the
        slot's previous occupant, or the run's shopping list. Both are derived
        quantities that can be wrong or absent, and when they were, the code
        fell back silently. The backpack's CONTENTS answer the same question
        directly and cannot be stale: after fitting, whatever is in there is
        what came off. One of those revisions read an empty shopping list as
        "everything is junk" and threw the run's four parts on the floor.
        """
        try:
            from control.stock import read_stock
            stock = read_stock(self.ac, close=False)
            if stock is None:
                print(f"      [rack] gun{gun}: backpack unreadable — "
                      f"dropping the gun as it stands")
                return
            # ⚠ ONLY WHAT THIS GUN CAN TAKE. Clicking every backpack row was
            # the first cut, and the journal priced it: parts the weapon does
            # not accept still cost two attempts and ~2.4 s apiece, because
            # ensure_kit retries once before believing the readback.
            # `Magazine_ExtendedQuickDraw_Medium_C` -- an SMG magazine being
            # offered to ARs -- went 0 for 12. Duplicate right-clicks rose
            # from ~0% to 33% of all clicks over the evening, and the rise
            # starts when this function did, not when park() was turned off.
            #
            # fits() is the catalogue's answer to exactly this question and
            # costs nothing; asking it is not an optimisation, it is the
            # difference between disposing of junk and hammering a slot that
            # was never going to take it.
            # ⚠ AND NEVER A PART THIS RUN NEEDS. The premise above -- "after
            # the fitting, whatever is in the backpack is what came off" --
            # is FALSE at the start of an invocation and after any restock:
            # the pack then holds the freshly spawned parts under test, and
            # clicking those onto a gun that is about to be thrown throws
            # them. Seen live on 2026-08-07, first weapon of a run:
            #   [rack] gun1: swapped back comp_ar, ext_ar, flash_ar,
            #                red_dot, vert_grip
            # of which ext_ar, flash_ar, red_dot and vert_grip were the
            # config being measured. Same shape as the `unwanted(set())` bug
            # two hours earlier: an empty or inapplicable notion of "wanted"
            # read as "everything is junk".
            #
            # `parts` is the run's whole shopping list, set beside restock_fn;
            # last_want covers a config asking for something outside it. If
            # NEITHER is known this returns without touching the gun -- the
            # junk then rides out only if it was already worn, which is the
            # old behaviour and costs a tidy pass, not the run's supply.
            keep = set(getattr(self, 'parts', None) or ())
            keep |= {v for v in (self.last_want or {}).values() if v}
            if not keep:
                print(f"      [rack] gun{gun}: nothing known to be wanted yet "
                      f"— not loading the gun, it leaves as it stands")
                return
            rows = [it for it in stock.view.inventory
                    if it and it.key and it.key not in keep
                    and fits(weapon, it.key)]
            if not rows:
                print(f"      [rack] gun{gun}: backpack empty — nothing to "
                      f"swap back")
                return
            # ⚠ RE-READ BEFORE EVERY CLICK. The row list is positional, and
            # a row that leaves makes everything below it move up -- stock.py
            # says so in its own comment ("below scroll up as the ones above
            # are dropped, so tidy() simply repeats"). Clicking a list read
            # ONCE therefore aims the second gesture and every one after it at
            # a row that has already shifted: at nothing, or at the wrong
            # part. Reported on 2026-08-07: "每次背包里要先看一下再说拖动还是
            # 装上什么的,不然拖的都不对呢,对着空拖".
            #
            # It is also the repo's own standing rule for this shape --
            # "拖拽一次一验": reading back only after a whole burst disguises
            # a timing problem as a geometry one.
            #
            # Costs one frame per part, ~7 ms plus the detectors, against a
            # gesture that was going to miss.
            done = []
            for _ in range(len(rows)):
                fresh = read_stock(self.ac, close=False)
                if fresh is None:
                    break
                nxt = next((it for it in fresh.view.inventory
                            if it and it.key and it.key not in keep
                            and it.key not in done
                            and fits(weapon, it.key)), None)
                if nxt is None:
                    break
                if self.ac.equip(gun, nxt):
                    done.append(nxt.key)
                else:
                    # Not retried here: the row is re-read next pass anyway,
                    # and a part that will not go on is the caller's problem
                    # to see rather than this loop's to hide.
                    done.append(nxt.key)
            print(f"      [rack] gun{gun}: swapped back "
                  f"{', '.join(sorted(done)) or '(nothing)'}")
            # ⚠ AND THAT IS THE WHOLE TEARDOWN. Whatever the gun still wears
            # LEAVES ON THE GUN -- there is no unequip step here, on purpose.
            #
            # There was one (_reclaim_worn, 2026-08-07). Its argument was that
            # when the spawner's own attachment IS the part under test, no junk
            # can displace it, so it rides out and has to be re-bought. The
            # journal priced that argument and it does not survive: EVERY part
            # it pulled off the gun was dropped on the floor by the very next
            # weapon's stock_parts. Ten weapons in a row, same two keys:
            #
            #   [rack]  gun1: pulled back red_dot, vert_grip
            #   [stock] dropping 2: red_dot, vert_grip
            #
            # because the two steps disagree about what "wanted" means --
            # _swap_back keeps anything on the run's whole shopping list,
            # stock_parts keeps only what THIS config asked for. The set
            # difference made a round trip every weapon: two unequip drags to
            # get them into the backpack, then two more to put them on the
            # floor, to reach the state that throwing the gun reaches for free.
            # It never once saved a part.
            #
            # Re-buying from the spawner is a menu click. Four gestures in the
            # Tab screen is where this project loses runs. Reported as it
            # happened: "现在扔枪前还是有拆垃圾配件的行为 ... 逻辑有一点问题".
        except Exception as e:
            # ⚠ PRINTS. A silent fallback here is indistinguishable from the
            # feature not existing, which is how four revisions read.
            print(f"      [rack] gun{gun}: swap-back failed ({e})")

    def clear_rack(self):
        """Rack empty. THIS RUN's parts come back; the rest rides out. -> bool

        NOTHING IS UNEQUIPPED HERE. `_swap_back` clicks the junk in the
        backpack onto the gun, and each click evicts this run's part into the
        backpack for free -- that is the whole reclaim. Then the gun goes over
        the side wearing the junk. One gesture per part, in one direction.

        ⚠ THE DOCSTRING USED TO DESCRIBE A last_want-BASED STRIP and the body
        had not done that for some time. Corrected 2026-08-07, because a stale
        docstring here is exactly what makes the next person rebuild the
        unequip step -- which is what happened, twice.

        THIS USED TO STRIP EVERYTHING FIRST, and stripping everything is what
        made the surplus. Measured over the first full-roster night: every
        freshly spawned weapon arrives wearing the quickdraw extended magazine
        that nobody asked for, stripping put it in the backpack, and the
        backpack went from `ext_ar, quickext_arx3` to no ext_ar at all and
        `quickext_arx4` -- the g36c cell then failed twice with `magazine reads
        Magazine_ExtendedQuickDraw_Large_C`, a supply failure wearing the
        costume of a compatibility one. The fix at the time was to also drop
        the surplus on the floor afterwards, which is four to six drags a
        weapon plus the Tab session around them. A part that never enters the
        backpack needs none of that.

        ⚠ AND IT CLOSES THE OTHER HALF, the one control/CLAUDE.md names: PUBG
        auto-fits what it finds in the backpack onto the next gun, so surplus
        in the pack is not merely clutter, it is a BARE cell that quietly ran
        wearing a grip. Parts that leave on the gun cannot do that.

        ⚠ WITH last_want UNSET IT STILL STRIPS EVERYTHING, and that is the
        right fallback rather than an oversight: before the first apply() this
        object cannot say which parts are its own, and on that one call the
        racked gun may be a leftover wearing the run's only extended magazine.
        Guessing wrong there costs a part; guessing wrong the other way costs
        a silently contaminated cell.

        Cheap when there is nothing to do: an empty slot is skipped (unequip
        refuses them -- a gesture on an empty slot drops the whole gun), and a
        bare gun reclaims in zero gestures.
        """
        if not self._open():
            return False
        try:
            # survey(), not _read_guns(_frame()): the read declares what it
            # needs and the control layer decides how to get it. The bare
            # _frame() here was the last caller reaching past that -- it
            # happened to want park=True, which _read_guns needs, but only
            # by luck.
            racked = self.ac.survey('guns')['guns']
            for g in (1, 2):
                if racked.get(g) is not None:
                    self._swap_back(g, racked.get(g))
            # The gun is thrown wearing whatever _swap_back just put back
            # on it. Nothing is stripped and nothing is dragged to the
            # floor: the parts this run needs are already back in the
            # backpack, displaced by the very clicks that reloaded the gun.
            rec = self.ac.clear_rack()
            ok = bool(rec.get('ok', True))
            if rec.get('dropped'):
                print(f"      [rack] cleared {len(rec['dropped'])} gun(s), "
                      f"parts kept")
        except Exception as e:
            print(f"      [!] could not clear the rack: {e}")
            return False
        finally:
            # Only if nobody upstream is holding the screen. See
            # Kit.session(): a forced close here shuts a Tab screen the
            # caller opened for the whole weapon, and the next helper
            # reopens it -- which is the OCOCOC alternation measured at
            # 80% of every Tab press in the corpus.
            if not self._session:
                self.rig.gun.ensure_inventory_closed()
        return ok

    def apply(self, want, weapon=None):
        """want = {'scope': key or None, 'muzzle': ..., 'grip': ...}.

        Returns the slot readback, or None if any slot disagrees with what was
        asked for. A drag that silently did nothing would otherwise be recorded
        as a measurement of a configuration that never existed.

        The diffing, the drags and the readback are InventoryControl's
        (`ensure_kit`); what is left here is this run's policy — open the Tab
        screen through open_tab() so a lost foreground is named before any
        drag, and flatten the result into the (slot, key, why) tuples
        measure_cell's retry loop reads out of `last_bad`.

        `weapon` is the catalogue gate and worth passing: without it a fit can
        be planned onto a slot the gun does not have, and that part lands on
        the floor rather than failing.
        """
        self.last_bad = []
        # open_tab first, and not just because ensure_kit would open it too:
        # it also runs ac.sync(), which demands the foreground and parks the
        # cursor. "Opened but would not sync" is a real state and naming it is
        # the difference between a fixable failure and a cell that dies as an
        # unexplained "could not reach config".
        # ⚠ ONE SESSION FOR THE WHOLE KITTING. Without it this method opened
        # the screen, ensure_kit closed it for the spawner and reopened, and
        # the finally below closed it again -- the churn log named the pair as
        # `harvest.py:735 apply -> harvest.py:735 apply`, one function closing
        # and reopening its own screen 0.79 s apart.
        with self.session() as _held:
            if not _held and not self._open():
                return None
            return self._apply(want, weapon)

    def _apply(self, want, weapon):
        """The body of apply(), inside a held Tab session."""
        try:
            # Recorded BEFORE the result is known, not after: a config that
            # only half landed still left this run's parts on the gun, and
            # those are exactly the ones clear_rack must take back.
            self.last_want = dict(want)
            rec = self.ac.ensure_kit(self.slot, want, weapon=weapon,
                                     restock=self.restock_fn)
        except Exception as e:
            print(f"      [!] kitting failed: {e}")
            return None
        finally:
            # Only if nobody upstream is holding the screen. See
            # Kit.session(): a forced close here shuts a Tab screen the
            # caller opened for the whole weapon, and the next helper
            # reopens it -- which is the OCOCOC alternation measured at
            # 80% of every Tab press in the corpus.
            if not self._session:
                self.rig.gun.ensure_inventory_closed()

        # Two ways a slot can end up wrong, and measure_cell treats them the
        # same: drop that slot and measure the rest. `missing` is a part that
        # is nowhere on screen, `bad` is a slot whose readback disagrees.
        # ensure_kit reports missing PARTS -- a list of catalogue keys, not
        # (slot, key) pairs. This unpacked each one into two names and threw
        # `ValueError: too many values to unpack (expected 2)` on any string
        # longer or shorter than two characters, i.e. always.
        #
        # It never fired until a part became genuinely unobtainable, because
        # `missing` is empty whenever the restock hook can produce what was
        # asked for. The night that emptied the backpack of ext_smg found it
        # twice in a row, on mp5k and mp9, as a crash rather than as the
        # "cannot fit" message written here.
        #
        # The slot is recovered from `want`, which is the mapping that was
        # asked for in the first place.
        for key in (rec.get('missing') or []):
            slot_name = next((s for s, k in (want or {}).items() if k == key),
                             None)
            print(f"      [!] {key} not on screen — cannot fit"
                  + (f" ({slot_name})" if slot_name else ""))
            # verifiable=False: the part was never on screen, so nothing was
            # ever asked of the slot. That is a SUPPLY failure, and this file
            # has been bitten by one wearing a compatibility costume before
            # (see clear_rack). It must not become a strike.
            self.last_bad.append((slot_name, key, 'not on screen', False))
        for b in rec['bad']:
            print(f"      [!] {b['slot']} should be "
                  f"{b['key'] or 'empty'}, {b['why']}")
            # ⚠ `verifiable` TRAVELS WITH IT. kit_faults sets it False when the
            # readback could not JUDGE the slot -- AMBIGUOUS, or a part with no
            # icon template -- as opposed to reading a different part, which is
            # real evidence. Dropping it here is how "the templates cannot
            # separate these two magazines" became "this weapon will not take
            # ext_smg" in kit_facts.json: four strikes on vector.magazine in
            # one run, for a magazine the gun takes.
            self.last_bad.append((b['slot'], b['key'], b['why'],
                                  bool(b.get('verifiable', True))))
        if rec.get('error') and not self.last_bad:
            print(f"      [!] kitting failed: {rec['error']}")
            return None
        return rec['worn'] if rec['ok'] else None

    # The two vocabularies -- read_slots' asset names and the spawner's keys --
    # are bridged in control/inventory.py now, next to the catalogue that owns
    # both. Kept as a name here because tools/verify_kit.py prints with it.
    _matches = staticmethod(slot_matches)




# A write is worth making when the batch's per-bullet signal stands this far
# clear of its own error bar. 3 is one part noise to three parts signal, so a
# write moves the curve toward truth ~9 times out of 10 rather than dithering.
EMA_WRITE_SNR = 3.0
# Never hold more than this. A cell that cannot reach the SNR in eight
# magazines is one whose remaining error is AT the noise -- the shape gate
# above should have stopped it, and if it has not, holding forever measures
# nothing and writes nothing. Bounded so the failure is "wrote less than it
# could" rather than "burned the cell in silence".
EMA_WRITE_MAX_HOLD = 8


def note_fits(facts, weapon, want):
    """Every part in `want` is on the gun and read back. Clear its strikes.

    Only ever called after a readback agreed with the request — kit.apply()
    returns None otherwise — so this is a positive observation, not an
    assumption. `want` also carries slots pinned to None, meaning "must end up
    empty"; those are not observations of anything fitting and are skipped.
    """
    if facts is None:
        return
    for slot_name, key in (want or {}).items():
        if key and facts.failures(weapon, slot_name, key):
            facts.note_success(weapon, slot_name, key)
            print(f"    {weapon}.{slot_name} took {key} after all — "
                  f"strike cleared")


def stock_parts(sc, kit, keys, also=(), loose_only=False):
    """Get the backpack to hold exactly one of each of `keys`, and no junk.

    Reads first, then spawns only the shortfall — see control/stock.py. The
    spawner cannot be asked what you already own, so an unconditional spawn
    per range entry (there is one at the start and one after every eviction)
    stacks duplicates until the backpack is full and the next part has
    nowhere to land.

    Safe to call as often as it is useful. Once the pack is right it reads,
    says so, and clicks nothing.
    """
    # ⚠ UNWANTED PARTS ARE NOT DRAGGED TO THE FLOOR ANY MORE. They stay in the
    # backpack until the next teardown, where Kit._swap_back clicks every row
    # onto the gun and clear_rack throws the gun wearing them. One right click
    # apiece instead of one drag apiece, and the drag was the gesture measured
    # at roughly half success inside this collector.
    #
    # DUPLICATES ARE STILL DROPPED (that is tidy's `keep=1` pass, separate from
    # this switch), which is what bounds the backpack: without it a run would
    # accumulate a row per weapon and outgrow the gun.
    #
    # ⚠ AND THE GUN ONLY HAS FIVE SLOTS, so a teardown carries at most five
    # rows out. That is the ceiling on this: unwanted parts beyond five cannot
    # ride out and would sit in the pack, where PUBG auto-fits them onto the
    # next weapon. If a bare cell is ever seen wearing something, this is the
    # first place to look -- re-enable drop_unwanted and the surplus goes back
    # to the floor.
    # leave='as-found' is tab_up()'s contract: whoever opened the screen
    # closes it. restock's own default is 'shut', which was written for a
    # caller heading to the spawner or the range -- true of the CLI, false of
    # this one, whose next move is find_gun and then kitting, both of which
    # need the screen up. See control/stock.restock.
    # ⚠ THE FLOOR IS THE OVERFLOW NET, NOT THE DISPOSAL PATH. Kit._swap_back
    # disposes of junk the cheap way -- click it onto the gun that is about to
    # be thrown, one right click apiece instead of one drag apiece, and the
    # drag is the gesture measured at roughly half success inside this
    # collector. This pass exists for what that CANNOT carry.
    #
    # The ceiling is five: a gun has five slots, so one teardown takes at most
    # five rows out with it. Anything beyond sits in the backpack, and a part
    # left there is not merely clutter -- PUBG auto-fits what it finds onto
    # the next weapon, which is how a cell labelled `bare` ends up having been
    # fired wearing a grip. Asked for on 2026-08-07: "每轮新枪以后,加一步拖动
    # 多余配件到地上,上一轮可能剩下什么的".
    #
    # It runs after the spawner visit that brings in the new gun, so it sees
    # the pack in its final state for this weapon. It SHOULD usually find
    # nothing: if `[stock] dropping N` is routinely non-zero, _swap_back is
    # failing to dispose of what it should, and that is the thing to fix
    # rather than this.
    return restock(kit.ac, sc, keys, backpack=BACKPACK, also=also,
                   drop_unwanted=True, leave='as-found',
                   loose_only=loose_only)


def expand(spec, semi=False):
    """Weapon names from 'ar', 'smg', 'all', or explicit names.

    Full-auto only unless semi=True. A recoil *curve* is a per-bullet sequence
    fired at a fixed cadence; a weapon that cannot hold the trigger down has no
    such sequence to measure, so a semi-auto cell records how fast the harness
    happened to click. Named weapons are honoured either way — asking for one
    by name is a deliberate act.
    """
    groups = {}
    for key, (cls, _) in ROSTER.items():
        groups.setdefault(cls.lower(), []).append(key)
    groups['all'] = sorted(ROSTER)
    out, named = [], set()
    for tok in spec.split(','):
        tok = tok.strip()
        if not tok:
            continue
        if tok in groups:
            out.extend(sorted(groups[tok]))
        else:
            out.append(tok)
            named.add(tok)
    seen, uniq, dropped = set(), [], []
    for x in out:
        if x not in WEAPON_RPM or x not in ROSTER or x in seen:
            continue
        seen.add(x)
        if not semi and x not in can_full_guns and x not in named:
            dropped.append(x)
            continue
        uniq.append(x)
    if dropped:
        print(f"skipping {len(dropped)} semi-auto/burst weapon(s), no "
              f"full-auto curve to measure: {', '.join(dropped)}")
    return uniq

