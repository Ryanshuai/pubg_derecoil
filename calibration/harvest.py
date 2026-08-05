"""Unattended recoil-curve harvesting in the training range.

Produces a weapon from the item spawner, dresses it, fires magazines, measures
what the current curve failed to cancel, and moves on. Nothing here needs a
human once it starts.

    python calibration/harvest.py --weapons ar --configs bare,both
    python calibration/harvest.py --weapons aug,m416 --mags 3 --resume
    python calibration/harvest.py --weapons ar --configs bare,muzzle,grip,both

Two questions are being answered at once, and the second is why the attachment
configs exist at all:

  1. What is each weapon's real per-bullet recoil? The residual left by the
     current curve, measured per bullet, IS the correction — calibration/
     fit_curve.py turns a run of this into a new curve.

  2. Is the attachment model true? detector/weapon_attachments.py asserts a
     compensator is 0.85 and a half grip 0.92 on EVERY weapon, that the two
     multiply with no interaction, and that angled and lightweight grips do
     nothing at all. None of that has ever been measured. Firing the same gun
     bare / muzzle-only / grip-only / both is a 2x2 factorial: the model holds
     only if R(both)/R(bare) equals R(muzzle)/R(bare) x R(grip)/R(bare).

Results go to JSONL. By default nothing is written back to any curve — the fit
is a separate step, see fit_curve.py. With --apply each cell EMA-updates its
own curve the moment it is measured, so the next pass measures a FRESH
residual against what this one wrote and repeated passes converge.

State is never assumed, only verified. Every toggle in this game is a toggle:
comma opens AND closes the spawner, Tab opens AND closes the inventory, right
click enters AND leaves ADS. Pressing one blind lands in the wrong state half
the time, and the failure is silent — a whole run mislabelled rather than an
error. So each is paired with a detector and watched until it agrees.
"""
import argparse
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

import rpm_store
from analysis import (analyse, interval_from_span, magazine_fault,  # offline
                      ROUNDS_TOL)
from sweep import (Rig, ensure_focus, focus_keeper,
                   POSTURES, HEADROOM_WARN_FRAC)
from control import spawner as spawner_mod
from control.spawner import SpawnerControl, ROSTER
from control.inventory import InventoryControl, slot_matches
from detector.cropper import FocusLost
from range_session import get_session, DEFAULT_BUDGET_S
from control.stock import open_tab, restock
from kit_facts import KitFacts
from fit_curve import ema_update

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
            self.rig.ensure_inventory_closed()

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
            print(f"      [!] {weapon} is not in the rack, and both slots are "
                  f"empty ({racked}) — the spawn did not land.")
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
            self.rig.ensure_inventory_closed()
        return True

    def clear_rack(self):
        """Both rack slots empty, their parts back in the backpack. -> bool

        STRIP THEN DROP, in that order. InventoryControl.drop_weapon throws the
        gun wearing everything it had on, which is the right default everywhere
        else and wrong here: the parts this run uses are spawned once and
        shuttled between guns, so a gun that leaves wearing the only plain
        extended magazine takes it to the floor, where nothing goes to fetch it.

        MEASURED, over the first full-roster night. The rack was never cleared
        at all -- strip() took the parts off ONE slot and the other gun kept
        sitting there holding the plain ext_ar. Meanwhile every freshly spawned
        weapon arrives wearing the QUICKDRAW extended magazine, which the game
        fits by itself and nobody asks for. So each cell leaked one ext_ar to
        the floor and gained one quickext_ar: the backpack went from `ext_ar,
        quickext_arx3` to no ext_ar at all and `quickext_arx4`, and the g36c
        cell failed twice with `magazine reads Magazine_ExtendedQuickDraw_
        Large_C` -- not a compatibility problem, a supply one.

        Cheap when there is nothing to do: an empty slot is skipped, and a bare
        gun strips in zero drags.
        """
        if not self._open():
            return False
        try:
            racked = self.ac._read_guns(self.ac._frame())
            for g in (1, 2):
                if racked.get(g) is None:
                    continue
                self.ac.strip(g)
            rec = self.ac.clear_rack()
            ok = bool(rec.get('ok', True))
            if rec.get('dropped'):
                print(f"      [rack] cleared {len(rec['dropped'])} gun(s), "
                      f"parts kept")
        except Exception as e:
            print(f"      [!] could not clear the rack: {e}")
            return False
        finally:
            self.rig.ensure_inventory_closed()
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
        if not self._open():
            return None
        try:
            rec = self.ac.ensure_kit(self.slot, want, weapon=weapon,
                                     restock=self.restock_fn)
        except Exception as e:
            print(f"      [!] kitting failed: {e}")
            return None
        finally:
            self.rig.ensure_inventory_closed()

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
            self.last_bad.append((slot_name, key, 'not on screen'))
        for b in rec['bad']:
            print(f"      [!] {b['slot']} should be "
                  f"{b['key'] or 'empty'}, {b['why']}")
            self.last_bad.append((b['slot'], b['key'], b['why']))
        if rec.get('error') and not self.last_bad:
            print(f"      [!] kitting failed: {rec['error']}")
            return None
        return rec['worn'] if rec['ok'] else None

    # The two vocabularies -- read_slots' asset names and the spawner's keys --
    # are bridged in control/inventory.py now, next to the catalogue that owns
    # both. Kept as a name here because tools/verify_kit.py prints with it.
    _matches = staticmethod(slot_matches)


def build_weapon(weapon, posture, att, rpm=None):
    """A Weapon carrying the current curve, scale and bullet interval.

    Re-reads the measured fire rates first, because detector.weapon caches the
    table at import and a cell that re-times has to rebuild on the new rate
    rather than on the one it just replaced.

    `rpm` overrides that rate IN MEMORY ONLY, and exists for exactly one
    caller: the mid-cell re-time. A rate from a single magazine is not yet a
    fact -- it is one measurement, and the failure mode it most resembles (a
    missed last transition, which reads as a faster gun) also produces exactly
    one odd magazine. The rest of the cell still has to fire on SOMETHING, and
    a fresh measurement beats a stale table, so it is used and not stored. The
    store happens at the end of the cell, from magazines that AGREE.
    """
    weapon_mod.WEAPON_RPM.update(weapon_mod.load_measured_rpm())
    if rpm:
        weapon_mod.WEAPON_RPM[weapon] = rpm
    w = Weapon()
    w.set('name', weapon)
    w.set('posture', posture)
    w.set('muzzle', (att or {}).get('muzzle', ''))
    w.set('grip', (att or {}).get('grip', ''))
    w.set_seq()
    return w


def measure_cell(rig, weapon, posture, mags, slot, log, cfg_name, want,
                 apply_ema=False, loadout=None):
    """Fire `mags` magazines and record what the curve did not cancel.

    `loadout` is (weapon_read, {slot: name}) from a Tab session the caller has
    already had. Pass it when there is one: opening Tab costs a close, a
    detection pass and a reopen, and the weapon axis reads exactly this for
    both guns in one session immediately before calling here — so without it
    the batch pays two extra Tab cycles to learn what it just measured.
    """
    if loadout is not None:
        gun_seen, att = loadout
    else:
        gun_seen, att = rig.read_loadout(slot=slot)
    if gun_seen is None:
        print("      [!] inventory would not open — cannot read attachments")
        return None
    if gun_seen and gun_seen != weapon:
        print(f"      [!] expected {weapon}, inventory says {gun_seen!r}")
        return None

    w = build_weapon(weapon, posture, att)
    if not len(w.t_s):
        print(f"      [!] no curve for {weapon}")
        return None
    pattern_counts = float(np.sum(w.dy_s))
    if pattern_counts <= 0:
        # A curve of the right length whose entries are all zero. It passes the
        # len() check above and then every percentage below divides by it.
        print(f"      [!] {weapon}'s curve sums to zero — there is nothing to "
              f"measure a residual against. Needs a starting curve.")
        return None

    rig.arm(w)
    time.sleep(0.3)

    # Top up FIRST, then take the aim — never the other way round. Both halves
    # of that ordering, and the reason a full magazine still needs the press,
    # are in FireDriver.top_up().
    mag_size, reload_s = rig.top_up()
    if reload_s is None:
        # Was thrown away here for as long as this line existed, so a stalled
        # reload was measured as a full magazine and the cell reported nothing
        # wrong. The other three wait_reload() calls in this file have always
        # checked it.
        print("      [!] top-up never finished — this cell's magazines may "
              "run short")
    print(f"      magazine holds {mag_size if mag_size else '?'} rounds")

    if not rig.ensure_posture(posture):
        print(f"      [!] could not reach posture {posture}")
        return None

    # Full auto, verified. Several guns spawn in single fire — the Mk14 and
    # the DMRs do — and holding the trigger in single mode fires exactly one
    # round, which then gets analysed as a magazine. The MG3 is worse: both
    # its modes are automatic, at 660 and 990 rounds a minute, and only one of
    # them matches the interval the curve is timed to.
    mode = rig.ensure_fire_mode(weapon)
    want_mode = rig.FIRE_MODE_FOR.get(weapon, 'full')
    if mode is None:
        print(f"      [!] fire mode unreadable — firing anyway, but if this "
              f"gun spawned in single fire the cell is worthless")
    elif mode != want_mode:
        print(f"      [!] {weapon} is in {mode!r} and would not cycle to "
              f"{want_mode!r} — skipping rather than measuring single fire")
        return None

    # The measurable band is where the character can see texture, and that
    # moves with the posture — prone looks lower, so the band that was mapped
    # standing puts the aim somewhere else entirely. Re-mapped whenever the
    # posture changes, cached otherwise. Only used when homing is on.
    if getattr(rig, 'band_posture', None) != posture:
        rig.pitch_centre = 0
        rig.band_posture = posture

    rig.flush(6)
    if rig.use_homing:
        # Level, from the bottom stop, using the stored per-posture offset.
        # Falls back to the old ground-to-sky scan only where none is stored.
        if not rig.goto_level(posture):
            rig.goto_pitch_centre()
    rig.set_reference()

    # Everything rebuild() reads except `mags` and `pattern_counts`, which
    # change with every EMA step.
    base_rec = {'type': 'cell', 'weapon': weapon, 'config': cfg_name,
                'posture': posture, 'attachments': att, 'want': want,
                'sight': rig.sight, 'K': rig.K, 'scale': w.scale,
                'magazine_size': mag_size}

    rows = []
    seen_resid = []      # accepted residuals, for the robust scale
    retimed = False      # the fire rate is corrected at most once per cell
    # Why each magazine that did NOT survive was dropped. Kept because `rows`
    # is the survivors, and any rate computed from survivors alone is a rate
    # among survivors -- "the tracker held for 95% of the rounds" is true and
    # useless if it is measured over the magazines the tracker held. The
    # harness's track_alive_frac needs the denominator, so the denominator is
    # recorded rather than inferred.
    discarded = []
    # The rate the re-time switched to, if it did. Held in memory for the rest
    # of the cell and threaded through every rebuild below -- build_weapon
    # re-reads the stored table on each call, so without carrying it the EMA
    # rebuild would silently put the cell back on the old rate mid-magazine.
    provisional_rpm = None
    for i in range(mags):
        if not focus_keeper().ok(f'mag {i}'):
            break
        if i > 0:
            if not rig.ensure_ads():
                print("      [!] could not re-enter ADS after reload")
                break
            back = rig.reaim()
            if back:
                print(f"        re-aimed {back:+d} counts"
                      f"{' above the stop' if rig.use_homing else ''}")
            # A magazine fired from an unknown position is not noisy data, it
            # is wrong data that looks fine — at the pitch clamp the view
            # barely moves and the weapon measures unusually mild. Stop the
            # cell instead of recording it.
            if rig.tracking_lost:
                print("        [!] view position is no longer known — "
                      "abandoning the rest of this cell")
                break
        fire_start = time.perf_counter()
        try:
            (rec, fire_s, steps, fire_end, first_shot,
             ads_frac) = rig.fire_magazine()
        except FocusLost:
            # See sweep.calibrate_combo: the frames after a lost foreground
            # are a frozen picture, so the magazine is gone and the cell is
            # not. A harvest run is 45-50 minutes and the terminal takes the
            # focus back on its own, so this is the difference between losing
            # one magazine and losing the run.
            print("        [!] lost the foreground mid-magazine — discarded")
            discarded.append('lost the foreground')
            if not focus_keeper().ok(f'{weapon} mag {i}'):
                break
            rig.flush(6)
            continue
        if steps == 0:
            print("        no rounds fired (still reloading?) — skipped")
            discarded.append('no rounds fired')
            time.sleep(1.5)
            continue
        # What the counter says this gun's fire rate actually is. Fitted before
        # anything is analysed, because every bin edge below is laid out on
        # this interval and a wrong one does not add noise, it adds a phase
        # error that grows with the bullet number.
        trace = getattr(rec, 'ammo_trace', [])
        # Two endpoints and the magazine size, not a per-round fit. The counter
        # reads 40/40 standing still and 37% while firing, so the fit had five
        # values to work with; the endpoints need no OCR at all. See
        # sweep.interval_from_span for the validation against published rates.
        iv, iv_rounds = interval_from_span(first_shot, fire_end, mag_size)
        # Analysed BEFORE the re-timing decision, because a discarded magazine
        # still moved the view and that has to be paid back. The first version
        # skipped analyse() on the re-time path and left pending_pitch at zero,
        # so the next reaim() looked for a reference a whole magazine's worth of
        # climb away, declared the view's position unknown, and abandoned the
        # cell -- one magazine after correctly discovering the AUG fires at 720
        # rpm rather than the table's 680.
        # NOT `trace` — that name is taken sixty lines up by the AMMO trace,
        # and shadowing it here fed a MagazineResult to the list comprehension
        # that builds ammo_trace for the cell record. It only fires on the
        # re-timing branch, so three factorials passed and the VSS crashed on
        # the first magazine that disagreed about its rate.
        view_trace = rec.finish()
        a = analyse(view_trace, rig.K, w.bullet_interval_s, fire_end,
                    n_bullets=mag_size, first_shot_ts=first_shot)
        if a is None:
            # SAID OUT LOUD. This was the one discard path that printed
            # nothing, and it is the one that swallows a whole weapon: the VSS
            # produced 0 of 9 cells on 2026-08-04 with three magazines fired
            # per cell, every posture reached, the rack read, the sight
            # switched -- and not one line between "magazine holds 22 rounds"
            # and "nothing measured". A run that fires for a minute and
            # reports nothing at all is indistinguishable from one that never
            # tried.
            #
            # THE TRACE LENGTH IS THE DIAGNOSIS, so it is printed. analyse()
            # gives up immediately on `len(ts) < 2` (calibration/analysis.py),
            # i.e. the view tracker returned almost nothing -- which is a
            # completely different fault from "the trace is there but the shot
            # times could not be fitted", and the two want opposite fixes.
            #
            # The VSS is the standing suspect for the first. It is the only
            # weapon on a 3-patch sight profile, and two of those three columns
            # (1265, 1330) sit 65 px apart where the red dot's seven are
            # 140-190 apart -- so it is nearer two independent looks at the
            # world than three. Its integral PSO-1 is what squeezed them.
            print(f"        [!] analyse() could not read this magazine "
                  f"({steps} rounds, {fire_s:.2f}s, "
                  f"{len(rig.tracker.xs)} patch columns, "
                  f"{len(getattr(view_trace, 'ts', ()))} tracked samples) "
                  f"— discarded")
            discarded.append('analyse() returned nothing')
            continue
        rig.pending_pitch += a['view_drift_counts']
        # Once per cell. If the re-timed rate is itself wrong the next magazine
        # exceeds the tolerance again, and a cell that re-times on every
        # magazine burns the whole run without ever measuring anything.
        if (iv and not retimed
                and abs(iv - w.bullet_interval_s) / w.bullet_interval_s > RPM_TOL):
            retimed = True
            rpm = 60.0 / iv
            print(f"        [!] fire rate is {rpm:.0f} rpm, not the "
                  f"{60.0 / w.bullet_interval_s:.0f} the curve is timed to "
                  f"({iv_rounds} rounds)")
            # USED, not stored. This is one magazine, and the failure mode it
            # most resembles -- a missed last transition, which shortens the
            # span and reads as a FASTER gun -- also produces exactly one odd
            # magazine. Storing here is what put 737.9 rpm in the AUG's file
            # from a single 81.32 ms magazine while the four either side of it
            # read 82.73-83.39. The rate is written at the end of the cell,
            # from magazines that agree; see the block after this loop.
            provisional_rpm = rpm
            # Re-time and refire. This magazine was compensated on the wrong
            # grid, so it is not a measurement of anything -- by its last
            # round the pulses were whole bullets away from the shots they
            # were meant to cancel. Correcting and dropping it costs one
            # magazine; keeping it would write that phase error into the curve
            # as if it were recoil.
            print(f"            re-timed — this magazine is discarded, "
                  f"the rest of the cell fires on {rpm:.0f} rpm")
            # Not a fault: this magazine was fired on the wrong grid and
            # dropping it is the correction working. Recorded separately so
            # the harness does not read a successful re-time as a lost
            # magazine and dock the cell for it.
            discarded.append(f're-timed to {rpm:.0f} rpm')
            w = build_weapon(weapon, posture, att, rpm=provisional_rpm)
            pattern_counts = float(np.sum(w.dy_s))
            rig.arm(w)
            time.sleep(0.2)
            if rig.wait_reload() is None:
                print("        [!] auto-reload never finished — stopping")
                break
            continue
        a.update(mag=i, fire_s=round(fire_s, 2), ammo_steps=steps,
                 fps=round(rec.effective_fps(), 1), ads_frac=ads_frac,
                 ads_icon_frac=round(getattr(rec, 'ads_icon_frac', float('nan')), 3),
                 ads_cross_frac=round(getattr(rec, 'ads_cross_frac', float('nan')), 3),
                 measured_interval_ms=(round(1000 * iv, 2) if iv else None),
                 measured_rpm=(round(60.0 / iv, 1) if iv else None),
                 interval_fit_rounds=iv_rounds,
                 # Deliberately absent, not zero. A two-endpoint interval has
                 # no residual by construction, and the 0.0 that used to be
                 # written here was a fabricated pass: rpm_store's quality
                 # gate compared it against 12 ms and let every rate through,
                 # including the single 81.32 ms magazine that became the
                 # AUG's stored 737.9 rpm. The real check is agreement between
                 # magazines, and it happens once, after the loop.
                 interval_fit_resid_ms=None,
                 # Kept raw so a fit that comes back empty can be diagnosed
                 # without another trip into the game.
                 ammo_trace=[(round(t - fire_start, 4), n) for t, n in trace],
                 shot_delay_ms=(round(1000 * (first_shot - fire_start), 1)
                                if first_shot else None))

        bad = magazine_fault(a, pattern_counts, mag_size, ads_frac, seen_resid)
        if bad:
            print(f"        mag {i}: DISCARDED — {bad}")
            discarded.append(bad)
            if 'ADS' in bad:
                # The crosshair is what decided. The icon is printed alongside
                # because a persistent gap between them is how a detector
                # drifting after a patch would first show itself.
                print(f"            crosshair {a['ads_cross_frac']:.0%} "
                      f"(decides) / posture icon {a['ads_icon_frac']:.0%}")
            if rig.wait_reload() is None:
                print("        [!] auto-reload never finished — stopping cell")
                break
            continue
        seen_resid.append(a['cum_counts'])

        rows.append(a)

        # EMA, one step per MAGAZINE — not one per cell. The residual just
        # measured is truth minus the curve that fired it, so applying alpha
        # of it now means the NEXT magazine is fired by a better curve and
        # measures a genuinely fresh residual. Batching five magazines and
        # applying once wastes four of them: they all measure the same stale
        # curve, and the only thing the extra four buy is a smaller error bar
        # on a correction that could have been converging all along.
        if apply_ema and posture == 'standing':
            one = dict(base_rec, mags=[a], n_mags=1,
                       pattern_counts=pattern_counts)
            rep = ema_update(one, f"harvest {cfg_name} mag {i}, "
                                  f"{datetime.now():%Y-%m-%d}", verbose=False)
            if rep:
                # Re-read what was just written and fly it from here on. The
                # provisional rate goes back on: build_weapon re-reads the
                # STORED table every call, so an EMA rebuild after a re-time
                # would otherwise put the rest of the cell back on the rate the
                # re-time replaced -- silently, and mid-cell.
                w = build_weapon(weapon, posture, att, rpm=provisional_rpm)
                pattern_counts = float(np.sum(w.dy_s))
                rig.arm(w)
                time.sleep(0.2)
                print(f"          curve {rep['curve_total_before']:.0f} -> "
                      f"{rep['curve_total_after']:.0f} counts, in effect for "
                      f"mag {i+1}")
        # How much of the trackable band above the aim this burst used up.
        # Recoil only pushes up, so this is the number that decides whether
        # the magazine was measured or merely watched: past the top of the
        # band the tracker recovers a fraction of the real motion and the
        # recoil reads low, with nothing else in the record to say so.
        band = getattr(rig, 'pitch_band', None)
        if rig.use_homing and band:
            head = band[1] - rig.pitch_centre
            peak = float(np.max(np.cumsum(a['per_bullet_counts'])))
            if head > 0 and peak > head * HEADROOM_WARN_FRAC:
                print(f"        [!] the view rose {peak:.0f} counts into "
                      f"{head} of trackable headroom "
                      f"({100*peak/head:.0f}%) — this magazine finished near "
                      f"the edge of the band and reads low")
        print(f"        mag {i}: {fire_s:.2f}s  residual "
              f"{a['cum_counts']:+8.1f} ({100*a['cum_counts']/pattern_counts:+6.1f}%)"
              f"  oor={a['n_out_of_range']} hand={a['human_counts']:+.0f}"
              f"/{a['human_abs_counts']:.0f}"
              f"  shot+{a['shot_delay_ms'] or float('nan'):.0f}ms")
        # The canary. Both signals answer the same question, so a persistent
        # gap means one of them has drifted -- which is how the posture icon's
        # unreliability during fire was found in the first place, and how a
        # patch moving the crosshair would show up before it corrupted a run.
        gap = a['ads_cross_frac'] - a['ads_icon_frac']
        if gap == gap and abs(gap) > 0.20:
            print(f"            [ads] crosshair {a['ads_cross_frac']:.0%} vs "
                  f"posture icon {a['ads_icon_frac']:.0%} — the crosshair was "
                  f"believed")
        if rig.wait_reload() is None:
            print("        [!] auto-reload never finished — stopping cell")
            break

    if not rows:
        return None

    # ── The fire rate is stored HERE, and only if the magazines agree ──
    #
    # analysis.interval_from_span asks for exactly this and no caller did it:
    # "a missed LAST change shortens the span and reads as a faster gun ... it
    # shows up as a rate that disagrees between magazines of the same cell, so
    # the caller should require agreement before storing one."
    #
    # Storing from the single re-time magazine instead put 737.9 rpm in the
    # AUG's file (81.32 ms) while the four magazines either side of it read
    # 82.73-83.39 ms -- and the stored `fit_resid_ms` was 0.0, because a
    # two-endpoint interval has no residual by construction, so rpm_store's
    # own quality gate had nothing to bite on. What it records now is the
    # spread between magazines, which is a real number that can be too large.
    ivs = [r['measured_interval_ms'] / 1000.0 for r in rows
           if r.get('measured_interval_ms')]
    if len(ivs) >= 2:
        spread_ms = 1000.0 * float(np.std(ivs))
        mean_iv = float(np.mean(ivs))
        if spread_ms > rpm_store.AGREE_MS:
            print(f"      [!] the magazines disagree about the fire rate by "
                  f"{spread_ms:.2f} ms "
                  f"({', '.join(f'{60/v:.0f}' for v in ivs)} rpm) — not "
                  f"stored. One of them missed a counter transition.")
        else:
            rpm, wrote, why = rpm_store.record(
                weapon, mean_iv, sum(len(r['per_bullet_counts']) for r in rows),
                spread_ms,
                note=f'{cfg_name} {posture}, {len(ivs)} magazines agreeing '
                     f'to {spread_ms:.2f} ms')
            print(f"      fire rate {rpm:.1f} rpm from {len(ivs)} magazines "
                  f"(spread {spread_ms:.2f} ms)"
                  + ('' if wrote else f" — not stored: {why}"))
    elif provisional_rpm:
        print(f"      [!] re-timed to {provisional_rpm:.0f} rpm but only "
              f"{len(ivs)} magazine produced a rate — nothing to agree with, "
              f"so nothing was stored")

    # Magazines that fired a different number of rounds are not repeats of the
    # same measurement and averaging them is not noise reduction, it is a
    # wrong answer with a big error bar. A short magazine carries less recoil
    # AND less compensation, so its residual is not comparable — one of them
    # in the bare m416 cell moved the mean 85 counts and took the cell from
    # 2% spread to 10%, which propagated into every ratio taken against it.
    lens = [len(r['per_bullet_counts']) for r in rows]
    # The counter's reading outranks the median: with three magazines and one
    # of them short, the median can BE the short one. What the game said the
    # magazine holds cannot.
    keep = mag_size if mag_size else int(np.median(lens))
    odd = [n for n in lens if abs(n - keep) > ROUNDS_TOL]
    if odd and len(lens) - len(odd) >= 1:
        print(f"        dropping {len(odd)} magazine(s) that fired {odd} "
              f"rounds against a median of {keep}")
        rows = [r for r, n in zip(rows, lens) if abs(n - keep) <= ROUNDS_TOL]

    cc = np.array([r['cum_counts'] for r in rows])

    # The gun's own recoil is compensation + residual, but only over the
    # rounds that actually fired. pattern_counts covers the whole curve, and
    # the two are not the same window: a magazine shorter than the curve never
    # fires its tail, and adding compensation that was never applied inflates
    # the answer. analyse() already trims to the burst, so the bin count IS the
    # round count.
    # Same length as the residual, and for the same reason: the magazine says
    # how many rounds there are. The curve may be shorter -- those rounds fire
    # uncompensated, which is a real and measurable thing -- but the bins have
    # to line up or comp and residual describe different shots.
    curve_bins = w.curve_bullets()
    nb = max(curve_bins, mag_size or 0)
    comp, _ = w.comp_bins(nb)
    fired = mag_size or int(np.median([len(r['per_bullet_counts'])
                                       for r in rows]))
    comp_fired = float(comp[:fired].sum())
    # Rounds past the end of the curve get no compensation at all, which shows
    # up as a spike in the last bins rather than as noise. Worth naming.
    uncovered = max(0, fired - curve_bins)
    rec = {
        'type': 'cell', 'weapon': weapon, 'config': cfg_name, 'want': want,
        'posture': posture, 'sight': rig.sight, 'K': rig.K,
        'fire_mode': mode,
        # Where the view was pointed, when homing established it. The
        # measurable band moves with what the character faces and the reading
        # moves with the band, so two cells are only comparable if both say.
        'pitch_centre': getattr(rig, 'pitch_centre', 0) or None,
        'pitch_band': list(getattr(rig, 'pitch_band', ()) or ()) or None,
        'attachments': att, 'scale': w.scale,
        'posture_factor': w.get_posture_factor(),
        'pattern_counts': pattern_counts, 'n_mags': len(rows),
        # The denominator. `mags` below is the survivors, so without this
        # every rate derived from the record is a rate among survivors:
        # "the tracker held for 95% of rounds" reads the same whether one
        # magazine was thrown away or four. `mags_asked` is what the cell set
        # out to fire, which is not len(rows) + len(discarded) when the loop
        # broke early — and "never fired" is its own failure, not a bad one.
        'mags_asked': mags,
        'mags_discarded': list(discarded),
        'residual_counts_mean': float(cc.mean()),
        'residual_counts_std': float(cc.std()),
        'residual_pct': float(100 * cc.mean() / pattern_counts),
        # The quantity every downstream comparison is actually about.
        'true_counts': float(comp_fired + cc.mean()),
        'comp_over_fired': comp_fired,
        'bullets_fired': fired,
        # What the HUD counter said the magazine held, before a round left it.
        # None means the counter could not be read, which is worth telling
        # apart from "the magazine was empty".
        'magazine_size': mag_size,
        'bullets_in_curve': curve_bins,
        'bullets_uncompensated': uncovered,
        'mags': rows,
        'ts': datetime.now().isoformat(timespec='seconds'),
    }
    log.write(json.dumps(rec) + '\n')
    log.flush()
    note = (f"  [{uncovered} rounds past the end of the curve — no "
            f"compensation, expect a tail spike]" if uncovered else '')
    print(f"      => true recoil {rec['true_counts']:.1f} counts over {fired} "
          f"rounds (residual {cc.mean():+.1f} +- {cc.std():.1f}){note}")
    return rec


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


def harvest_weapon(rig, kit, sc, weapon, configs, postures, mags,
                   slot, log, done, want_parts=(), facts=None,
                   apply_ema=False, base_sight='red_dot', bare_mags=0):
    cls = ROSTER.get(weapon, (None,))[0]
    parts = PART_FOR_CLASS.get(cls, {})

    # Ask for what this gun can wear, not what its class can. Two configs can
    # collapse onto the same effective one -- on a grip-less gun bare and
    # grip are the same measurement -- so they are deduped rather than fired
    # twice under two names.
    todo, seen = [], set()
    for c in configs:
        eff = effective_config(weapon, c, parts)
        if eff != c:
            missing = sorted(parse_config(c) - parse_config(eff))
            why = '/'.join(f'{s} ({"no slot" if not has_slot(weapon, s) else "no part"})'
                           for s in missing)
            print(f"    {weapon} cannot take {why} — measuring {c} as {eff}")
        if eff in seen:
            continue
        seen.add(eff)
        if any((weapon, eff, p) not in done for p in postures):
            todo.append(eff)
    if not todo:
        print(f"  nothing to do for {weapon}")
        return []

    # The optic decides K and where the tracker may look, and one weapon
    # brings its own — see SIGHT_FOR.
    rig.set_sight(SIGHT_FOR.get(weapon, base_sight))

    # Clear the whole rack, not just this run's slot. Leaving the other gun in
    # place and letting the next spawn evict it loses that gun's parts to the
    # floor -- eviction only fires when the rack is FULL, and an evicted gun
    # leaves wearing everything it had on. See Kitter.clear_rack for the
    # measured cost of not doing this.
    kit.clear_rack()

    # Then take stock, with everything loose and nothing hidden on a gun. This
    # is the cheapest place in the run to notice a duplicate — the strip has
    # just put the last weapon's parts back, so the pack is at its fullest and
    # every copy is visible as a row rather than as something worn.
    #
    # want_parts is catalogue KEYS; `parts` above is {slot: key} for this
    # class. Handing the latter to a stocktake feeds it slot names, which
    # match nothing, so every real part reads as surplus and goes on the
    # floor — which is exactly what the first run of this did.
    # ONE panel visit for the shortfall AND the gun. They used to be two: the
    # parts went in through stock_parts, the panel closed, and then it reopened
    # and re-synced for a single give_weapon. give_many orders the whole list
    # by category and keeps the panel open across it, so a gun in column 1 and
    # four parts in column 2 cost one open, one sync and one collapse.
    if not stock_parts(sc, kit, want_parts, also=(weapon,),
                       loose_only=True):
        print(f"  [!] could not stock the parts or produce {weapon}")
        return []

    # Where the gun actually landed, read rather than assumed — see
    # Kitter.find_gun. Before anything is fitted, because every slot read
    # after this points at whatever this resolves to.
    if kit.find_gun(weapon) is None:
        return []

    out = []
    for cfg in todo:
        fill = parse_config(cfg)
        want = want_for(weapon, cls, fill)
        print(f"    config {cfg}: {want}")
        if kit.apply(want, weapon=weapon) is not None:
            # It went on. Clear any strike against it — KitFacts.note_success
            # exists for this and had no callers, so every strike this run
            # ever recorded was permanent, including the ones whose cause was
            # not compatibility at all. `aug.magazine.ext_ar` reached three
            # strikes that way, all of them from reading an empty rack slot,
            # for a magazine the gun takes. A strike that no success can clear
            # is not evidence, it is a rumour.
            note_fits(facts, weapon, want)
        else:
            # Drop whatever refused and measure the rest. A stale catalogue
            # entry should cost one slot, not this weapon's entire cell — the
            # old code skipped here and half the roster produced no data at
            # all. The failure is logged for a human to check; nothing is
            # auto-corrected, see kit_facts.py.
            bad = list(kit.last_bad)
            for slot_name, key, why in bad:
                if key:
                    n = facts.note_failure(weapon, slot_name, key, note=why)
                    print(f"    [!] {weapon}.{slot_name} would not take "
                          f"{key} ({why}) — {n} failure(s) on record")
            drop = {s for s, k, _ in bad if k}
            fill2 = fill - drop
            cfg2 = config_name(fill2)
            if drop and cfg2 != cfg:
                want = {k: v for k, v in want.items() if k not in drop}
                print(f"    retrying {weapon} as {cfg2} without {'/'.join(sorted(drop))}")
                if kit.apply(want, weapon=weapon) is None:
                    print(f"    [!] {cfg2} failed too — skipping {weapon}")
                    continue
                note_fits(facts, weapon, want)
                cfg, fill = cfg2, fill2
            else:
                print(f"    [!] could not reach config {cfg} — skipping")
                continue
        for posture in postures:
            if (weapon, cfg, posture) in done:
                print(f"      posture {posture}: already in the log, skipping")
                continue
            # THE BARE CELL IS WORTH MORE MAGAZINES THAN ANY OTHER, because
            # its error is COMMON-MODE. Every single-slot factor divides by
            # it, and the multiplicativity test multiplies by it with weight
            # (n-1), so an error there slides every verdict in the run the
            # same way -- and the spread AMONG the verdicts cannot see it.
            #
            # Measured: ortho_0802c and 0802d are the same m416 factorial run
            # twice. All four multiplicativity gaps flipped sign together,
            # -6.0/-6.2/-5.6/-9.9% against +0.5/+2.2/-0.8/+5.9%. c's bare cell
            # read 8% low with a 5.9% sem while d's had 1.6%; that single cell
            # is the entire difference between the two verdicts.
            #
            # Splitting magazines evenly across cells is therefore the wrong
            # allocation, not a neutral one. --bare-mags buys the whole run's
            # precision in one place.
            n = bare_mags if (cfg == 'bare' and bare_mags) else mags
            print(f"      posture {posture}"
                  + (f'  ({n} magazines — this is the cell every ratio '
                     f'divides by)' if n != mags else ''))
            r = measure_cell(rig, weapon, posture, n, kit.slot, log, cfg,
                             want, apply_ema=apply_ema)
            if r:
                out.append(r)
                done.add((weapon, cfg, posture))
                # Close the loop here, not in a separate pass over the JSONL.
                # The residual just measured is exactly truth-minus-curve, so
                # applying alpha of it IS the EMA step; the next pass then
                # measures a fresh residual against what this wrote.

    return out


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
    return restock(kit.ac, sc, keys, backpack=BACKPACK, also=also,
                   loose_only=loose_only)


def load_done(path):
    """Cells already in the log, as (weapon, config, posture).

    The posture belongs in the key. Without it, one recorded posture marked
    the whole config done and --resume skipped the other two silently -- a
    stale two-magazine m416/bare/standing cell from an earlier day cost the
    bare arm of a posture factorial, and nothing said so.
    """
    done = set()
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get('type') == 'cell':
                done.add((r['weapon'], r['config'],
                          r.get('posture', 'standing')))
    return done


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


def report(rows):
    if not rows:
        print("\nnothing harvested")
        return
    by = {}
    for r in rows:
        by.setdefault(r['weapon'], {})[r['config']] = r['true_counts']
    names = sorted({c for cells in by.values() for c in cells},
                   key=lambda n: (len(parse_config(n) or ()), n))
    w0 = max(8, max(len(n) for n in names) + 1)
    rule = 9 + w0 * len(names)

    print("\n" + "=" * rule)
    print("TRUE RECOIL PER CONFIG (counts over one magazine)")
    print("=" * rule)
    print(f"{'weapon':<9}" + ''.join(f'{n:>{w0}}' for n in names))
    print("-" * rule)
    for w in sorted(by):
        c = by[w]
        print(f"{w:<9}" + ''.join(
            f"{c[n]:>{w0}.0f}" if c.get(n) else f"{'-':>{w0}}" for n in names))

    print("\nRATIO TO BARE — a weapon-independent factor shows the same "
          "column everywhere")
    print("-" * rule)
    print(f"{'weapon':<9}" + ''.join(f'{n:>{w0}}' for n in names))
    for w in sorted(by):
        c, b = by[w], by[w].get('bare')
        if not b:
            continue
        print(f"{w:<9}" + ''.join(
            f"{c[n]/b:>{w0}.3f}" if c.get(n) else f"{'-':>{w0}}"
            for n in names))

    # Multiplicativity: does a combination equal the product of its parts?
    # This is the whole reason to prefer factors over a curve per combination —
    # if it holds, N slots cost N measurements instead of 2^N.
    combos = [n for n in names if len(parse_config(n) or ()) > 1]
    if combos:
        print("\nIS IT MULTIPLICATIVE?  predicted = product of the single-slot "
              "ratios")
        print("-" * 58)
        print(f"{'weapon':<9}{'config':<20}{'predicted':>11}{'measured':>10}"
              f"{'gap':>8}   verdict")
        for w in sorted(by):
            c, b = by[w], by[w].get('bare')
            if not b:
                continue
            for n in combos:
                if not c.get(n):
                    continue
                singles = [config_name(frozenset((s,)))
                           for s in parse_config(n)]
                if any(not c.get(s) for s in singles):
                    continue
                pred = 1.0
                for s in singles:
                    pred *= c[s] / b
                meas = c[n] / b
                gap = 100 * (meas / pred - 1)
                verdict = 'yes' if abs(gap) < 3 else f'NO'
                print(f"{w:<9}{n:<20}{pred:>11.3f}{meas:>10.3f}"
                      f"{gap:>7.1f}%   {verdict}")
        print("\n  gap is what a multiplicative model would get wrong. Under "
              "3% is\n  inside the game's own per-shot randomness at 3 "
              "magazines a cell.")


def main():
    global SCOPE_PART      # paired to --sight below
    # Item names are Chinese and the spawner logs them. Redirected to a file
    # or a pipe, Windows hands Python cp1252 rather than the console's own
    # code page, and the first 突击步枪 kills the run several minutes in --
    # after the backpack has been spawned and the gun kitted.
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapons', default='ar')
    ap.add_argument('--configs', default='bare,both')
    ap.add_argument('--postures', default='standing')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--scope', default='',
                    help='the sight actually FITTED. Defaults to whatever '
                         'SIGHT_SCOPE pairs with --sight, which is what keeps '
                         'the measurement profile and the gun in agreement. '
                         'Pass it only to break that pairing deliberately.')
    ap.add_argument('--parts', default='',
                    help='swap which part fills a slot, e.g. '
                         'muzzle=brake_ar,grip=tilted_grip. This is how a '
                         'second part in the same slot gets measured against '
                         'the first.')
    ap.add_argument('--home', action='store_true',
                    help='re-home against the pitch clamp before every '
                         'magazine instead of returning to the cell reference. '
                         'Drift-proof but obtrusive: mapping the measurable '
                         'band sweeps the view ground-to-sky per posture.')
    ap.add_argument('--apply', action='store_true',
                    help='EMA-update each curve right after its cell instead '
                         'of leaving the JSONL for fit_curve.py. The next pass '
                         'then measures a fresh residual against the curve '
                         'this one wrote, which is what makes repeated runs '
                         'converge. Backups are kept per write.')
    ap.add_argument('--semi', action='store_true',
                    help='include semi-auto and burst weapons, which have no '
                         'full-auto curve to measure')
    ap.add_argument('--mags', type=int, default=3)
    ap.add_argument('--bare-mags', type=int, default=0,
                    help='magazines for the BARE cell only (default: same as '
                         '--mags). Worth raising whenever the run computes '
                         'factors: bare is the denominator of every one of '
                         'them and enters the multiplicativity test with '
                         'weight (n-1), so its error is common-mode — it '
                         'slides every verdict the same way and the spread '
                         'among the verdicts cannot see it. Two runs of the '
                         'same m416 factorial had all four gaps flip sign '
                         'together on the strength of one noisy bare cell.')
    ap.add_argument('--slot', type=int, default=2,
                    help='rack slot to start from. Only a starting guess — '
                         'the slot the gun actually landed in is read back '
                         'per weapon (Kitter.find_gun), because an empty rack '
                         'takes the first gun into slot 1 and a re-entry '
                         'empties the rack.')
    ap.add_argument('--out', default='')
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--countdown', type=int, default=6)
    ap.add_argument('--session', default='auto', choices=('manual', 'auto'),
                    help="how to get back in when the range evicts us; 'auto' "
                         "drives the lobby via control/lobby.py")
    ap.add_argument('--budget', type=float, default=DEFAULT_BUDGET_S,
                    help='seconds before re-entering pre-emptively')
    args = ap.parse_args()

    for pair in args.parts.split(','):
        if not pair.strip():
            continue
        slot, _, key = pair.partition('=')
        slot, key = slot.strip(), key.strip()
        if slot not in TEST_SLOTS or key not in spawner_mod.ATTACHMENTS:
            print(f"[!] --parts {pair!r}: slot must be one of {TEST_SLOTS} "
                  f"and the part must be spawnable")
            return 1
        for cls, table in PART_FOR_CLASS.items():
            if table.get(slot):        # leave classes that have no such slot
                table[slot] = key
        print(f"parts    : {slot} = {key} (overridden)")

    weapons = expand(args.weapons, semi=args.semi)
    # Canonicalised so 'grip+muzzle' and 'muzzle+grip' are one cell, and so
    # --resume matches cells logged by an earlier run.
    configs, bad = [], []
    for c in (c.strip() for c in args.configs.split(',')):
        if not c:
            continue
        slots = parse_config(c)
        if slots is None:
            bad.append(c)
        elif config_name(slots) not in configs:
            configs.append(config_name(slots))
    postures = [p.strip() for p in args.postures.split(',') if p.strip()]
    bad += [p for p in postures if p not in POSTURES]
    if bad:
        print(f"[!] unknown: {bad}  (slots are {TEST_SLOTS}, joined by '+')")
        return 1
    if not weapons:
        print("[!] no weapons selected")
        return 1

    out = args.out or os.path.join(
        RUNS, f"harvest_{args.sight}_{datetime.now().strftime('%m%d_%H%M')}.jsonl")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    done = load_done(out) if args.resume else set()

    # What each gun will actually be measured wearing. Printed before anything
    # spawns, because the answer is not "what you asked for": half the roster
    # has no lower rail, and a run that discovers that one gun at a time
    # discovers it by failing to kit and firing nothing.
    plan = {}
    for w in weapons:
        cls = ROSTER.get(w, (None,))[0]
        table = PART_FOR_CLASS.get(cls, {})
        got = []
        for c in configs:
            eff = effective_config(w, c, table)
            if eff not in got:
                got.append(eff)
        plan[w] = got
    groups = {}
    for w, got in plan.items():
        groups.setdefault(tuple(got), []).append(w)

    print(f"weapons  : {len(weapons)} — {', '.join(weapons)}")
    print(f"configs  : {', '.join(configs)}")
    print("as built :")
    for got, ws in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        note = '' if list(got) == configs else '   <- degraded'
        print(f"           {', '.join(got):<24} {len(ws):2d}  "
              f"{', '.join(sorted(ws))}{note}")
    print(f"postures : {', '.join(postures)}")
    print(f"out      : {out}")
    print(f"est.     : ~{len(weapons)*len(configs)*len(postures)*args.mags*9/60:.0f}"
          f" min of firing, plus spawner and inventory work")
    print("\n" + ("[EMA] each cell updates its own curve in place, "
                  "backups kept.\n" if args.apply else
                  "[SHADOW MODE] nothing is written back to any curve.\n"))

    # PAIR THE FITTED SIGHT TO THE MEASUREMENT PROFILE, and do it before
    # `parts` is built below — that set is what restock puts in the pack,
    # and reading SCOPE_PART after it meant the run wore a red dot while
    # measuring through the 4x's K. Every count would have been wrong by
    # that ratio, and the log said `wearing 'scope_4x'` while it happened.
    # A mismatch is not a subtle error: it applies one sight's K to another
    # sight's picture, and every count in the run is wrong by that ratio.
    if args.scope:
        SCOPE_PART = args.scope or None
        if SIGHT_SCOPE.get(args.sight, SCOPE_PART) != SCOPE_PART:
            print(f"  [!] --sight {args.sight} normally wears "
                  f"{SIGHT_SCOPE.get(args.sight)!r}; you asked for "
                  f"{SCOPE_PART!r}. The K and the picture will not match.")
    elif args.sight in SIGHT_SCOPE:
        SCOPE_PART = SIGHT_SCOPE[args.sight]
    else:
        print(f"  [!] no sight part known for profile {args.sight!r} — "
              f"leaving the scope slot to {SCOPE_PART!r}")
    print(f"  sight profile {args.sight}, wearing {SCOPE_PART!r}")

    # What the run needs on hand, and — just as importantly — what it does
    # not: anything else nameable in 库存 is surplus from an earlier run, and
    # every spare is one more thing find() can pick instead of the one meant.
    # Only the slots some config actually fills are stocked.
    wanted_slots = frozenset().union(*(parse_config(c) for c in configs)) \
        if configs else frozenset()
    parts = {SCOPE_PART}
    for w in weapons:
        cls = ROSTER.get(w, (None,))[0]
        table = PART_FOR_CLASS.get(cls, {})
        parts.update(x for x in
                     [table.get(s) for s in wanted_slots] +
                     [MAG_FOR_CLASS.get(cls)] if x)

    rig = Rig(args.sight)
    rig.use_homing = args.home
    sc = SpawnerControl()
    kit = Kitter(rig, slot=args.slot)
    # A config that cannot see a part it needs asks for that part, rather than
    # dying as "not on screen — cannot fit". The run-wide set goes along so the
    # top-up still knows what counts as junk.
    kit.restock_fn = lambda need: stock_parts(sc, kit, set(need) | parts)
    print(f"grabber  : {type(rig.grabber).__name__}  K={rig.K:.4f}  "
          f"{len(rig.tracker.xs)} patches {rig.tracker.patch}x"
          f"{rig.tracker.patch_h}  wrap {rig.tracker.patch_h/2:.0f} px")
    if not rig.mouse.human_available():
        print("  [!] the Pico is not reporting hand movement — old firmware. "
              "Any aim correction during a burst will be booked as recoil.")

    # Position does not matter for the spawner — comma opens the panel from
    # anywhere in the training range (docs/game_quirks.md). What the aim has to
    # satisfy is the recoil measurement: phaseCorrelate needs texture to lock
    # onto, and a patch of empty sky reads zero displacement no matter how hard
    # the gun kicks.
    print("\n>>> Face something with texture — the recoil is measured off it.")
    if not ensure_focus(countdown_s=args.countdown, label='the harvest'):
        print("[!] ABORT: game not focused, and could not take the "
              "foreground. Is PUBG running?")
        rig.close()
        return 1
    time.sleep(0.6)     # the game ignores input for a few frames after a
                        # foreground change; the first comma would be eaten

    # "Are we in the training range?" has exactly one honest answer here: the
    # item spawner opens. Nothing else tells the range apart from any other
    # match, and the spawner is what the run needs anyway — so the in-range
    # test and the at-a-spawner test are the same press.
    def at_spawner():
        # The in-range test and the at-a-spawner test are one press: comma
        # produces this panel only inside the training range.
        ok = sc.ensure_panel(True)
        sc.ensure_panel(False)
        return ok

    session = get_session(args.session, in_range_fn=at_spawner,
                          budget_s=args.budget, verbose=False)

    ok, _ = session.ensure()
    if ok and not at_spawner():
        print("[!] in a match, but the item spawner will not open. Either the "
              "lobby was on a different mode, or this is not a spawn point "
              "next to a spawner — walking there is not automated.")
        ok = False
    if not ok:
        print("[!] ABORT: not in the training range at an item spawner.")
        rig.close()
        kit.close()
        session.close()
        return 1

    log = open(out, 'a', encoding='utf-8')
    log.write(json.dumps({
        'type': 'header', 'sight': args.sight, 'K': rig.K,
        'patch': rig.tracker.patch, 'patch_h': rig.tracker.patch_h,
        'patch_xs': list(rig.tracker.xs), 'band_y': rig.tracker.band_y,
        'mags': args.mags, 'configs': configs, 'slot': args.slot,
        'ts': datetime.now().isoformat(timespec='seconds'),
    }) + '\n')

    # Evidence only — see kit_facts.py. Nothing here edits the catalogue.
    facts = KitFacts()

    rows = []
    try:
        print(f"parts wanted: {', '.join(sorted(parts))}")
        if not stock_parts(sc, kit, parts):
            print("[!] could not stock the parts — continuing anyway; "
                  "kitting will fail loudly if one is missing")
        for i, weapon in enumerate(weapons):
            if not focus_keeper().ok(f'weapon {weapon}'):
                break
            # Between weapons, never mid-magazine: re-entry is a restart, not a
            # pause. The rack and the backpack come back empty, so whatever was
            # stocked has to be stocked again.
            ok, re_entered = session.ensure()
            if not ok:
                print("[!] could not get back into the range — stopping.")
                break
            if re_entered:
                print("re-entered the range — re-stocking parts")
                # The measurable band is a property of where the character is
                # standing and facing, and re-entry moves both. Measured again
                # on the first cell rather than carried over.
                rig.pitch_centre = 0
                if not stock_parts(sc, kit, parts):
                    print("[!] could not re-stock after re-entry")
            print(f"\n[{i+1}/{len(weapons)}] {weapon}")
            rows.extend(harvest_weapon(rig, kit, sc, weapon, configs,
                                       postures, args.mags, args.slot, log,
                                       done, want_parts=parts, facts=facts,
                                       apply_ema=args.apply,
                                       base_sight=args.sight,
                                       bare_mags=args.bare_mags))
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        try:
            rig.ensure_posture('standing')
        except Exception:
            pass
        rig.close()
        kit.close()
        session.close()
        log.close()
        facts.save()

    report(rows)
    facts.report()
    print(f"\n  raw -> {out}")
    print("  rebuild a curve from it with:")
    print(f"    python calibration/fit_curve.py --jsonl {out} "
          f"--weapon <name> --apply")
    return 0


if __name__ == '__main__':
    sys.exit(main())
