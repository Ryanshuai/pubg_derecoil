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

SHADOW MODE. Results go to JSONL. Nothing is written back to any curve or
scale file — see fit_curve.py for that, deliberately a separate step.

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

import cv2
import numpy as np

from config import (SCREEN_W, SCREEN_H, SPAWNER_ICON_ANCHORS, SPAWNER_ICON_W,
                    SPAWNER_ICON_H, SPAWNER_ICON_SEARCH)
from detector.cropper import RegionGrabber
from detector.spawner_detector import SpawnerDetector
from detector.weapon import Weapon, WEAPON_RPM, can_full_guns
from press.pico_mouse import HID_KEY_COMMA, HID_KEY_R

from sweep import (Rig, analyse, game_focused, ensure_focus, focus_keeper,
                   POSTURES)
import spawner_control as spawner_mod
from spawner_control import SpawnerControl, ROSTER
from attach_control import AttachControl
from range_session import get_session, DEFAULT_BUDGET_S

HERE = os.path.dirname(os.path.abspath(__file__))

PANEL_WATCH_S = 3.0       # comma -> panel drawn; generous, it is a full screen
PANEL_SETTLE_S = 0.5
KIT_SETTLE_S = 0.6

# Which part fills each slot under test, per weapon class. A class that has no
# part for a slot skips every config naming it, rather than silently measuring
# bare twice under two different labels.
#
# Overridable per slot with --parts muzzle=brake_ar,grip=angled_grip, which is
# how a second part in the same slot gets measured against the first.
PART_FOR_CLASS = {
    'AR':  {'muzzle': 'comp_ar',  'grip': 'vert_grip', 'stock': 'tactical_stock'},
    'DMR': {'muzzle': 'comp_ar',  'grip': 'vert_grip', 'stock': 'tactical_stock'},
    'SMG': {'muzzle': 'comp_smg', 'grip': 'vert_grip', 'stock': 'tactical_stock'},
    # The M249 takes the AR magazine and a stock, but no compensator — the
    # AR comp lists 突击步枪/精确射手步枪/O12/S12K and not the M249.
    'LMG': {'muzzle': None,       'grip': 'vert_grip', 'stock': 'tactical_stock'},
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

# How far a magazine's round count may sit from the cell's median before it is
# treated as a different measurement rather than a repeat. See measure_cell.
ROUNDS_TOL = 2

# The sight is pinned, not tested. Magnification is a different axis from
# recoil reduction: a scope does not damp the gun, it magnifies the view, so
# the compensation has to scale with it and the measurement's own K changes
# with it too (RECOIL_SIGHT_PROFILES). Mixing that into an attachment factorial
# would confound the two. Red dot is 1x, where counts and pixels agree.
SCOPE_PART = 'red_dot'

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


class Panel:
    """The item-spawner screen: comma toggles it, three button glyphs prove it.

    Uses its own grabber. The glyph windows are deliberately absent from
    HUD_REGIONS — the per-frame capture loop has no use for a panel that only
    exists while a tool is driving it, and pulling them in would drag the DXGI
    bounding box wider for every frame of every run.
    """

    def __init__(self, mouse):
        self.mouse = mouse
        self.det = SpawnerDetector()
        xs = [a[0] for a in SPAWNER_ICON_ANCHORS]
        ys = [a[1] for a in SPAWNER_ICON_ANCHORS]
        s = SPAWNER_ICON_SEARCH
        self._box = (max(0, min(ys) - s), max(0, min(xs) - s),
                     max(ys) + SPAWNER_ICON_H + 2 * s - min(ys),
                     max(xs) + SPAWNER_ICON_W + 2 * s - min(xs))
        self._grab = RegionGrabber({'panel': self._box})
        # classify() indexes screen coordinates, so the crop is blitted back
        # to where it came from rather than handed over on its own.
        self._buf = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)

    def close_grabber(self):
        self._grab.close()

    def is_open(self):
        y, x, h, w = self._box
        self._buf[y:y + h, x:x + w] = self._grab.grab()['panel']
        return self.det.classify(self._buf)

    def _toggle_until(self, want, tries=3):
        for _ in range(tries):
            if self.is_open() == want:
                return True
            self.mouse.key(HID_KEY_COMMA, 60)
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < PANEL_WATCH_S:
                if self.is_open() == want:
                    time.sleep(PANEL_SETTLE_S)
                    return True
                time.sleep(0.08)
        return self.is_open() == want

    def ensure_open(self):
        return self._toggle_until(True)

    def ensure_closed(self):
        return self._toggle_until(False)


class Kitter:
    """Puts a named set of attachments on the gun, and proves it landed.

    Parts are spawned once and then shuttled between the gun and the backpack.
    Spawning fresh ones per weapon would work too, but every spare in 库存 is
    one more thing find() can pick instead of the one meant.
    """

    def __init__(self, rig, slot=2, verbose=False):
        self.rig = rig
        self.slot = slot
        self.ac = AttachControl(verbose=verbose)

    def close(self):
        try:
            self.ac.close()
        except Exception:
            pass

    def _open(self):
        if not self.rig.ensure_inventory_open():
            print("      [!] inventory would not open")
            return False
        return bool(self.ac.sync())

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

    def apply(self, want):
        """want = {'scope': key or None, 'muzzle': ..., 'grip': ...}.

        Returns the slot readback, or None if any slot disagrees with what was
        asked for. A drag that silently did nothing would otherwise be recorded
        as a measurement of a configuration that never existed.
        """
        if not self._open():
            return None
        try:
            for slot_name, key in want.items():
                cur = self.ac.read_slots(self.slot).get(slot_name, '')
                if key is None:
                    if cur:
                        self.ac.unequip(self.slot, slot_name)
                    continue
                if cur and self._matches(cur, key):
                    continue
                if cur:
                    self.ac.unequip(self.slot, slot_name)
                view = self.ac.look()
                item = view.find(key)
                if item is None:
                    print(f"      [!] {key} not on screen — cannot fit")
                    return None
                self.ac.equip(self.slot, slot_name, item)
            time.sleep(KIT_SETTLE_S)
            got = self.ac.read_slots(self.slot)
        except Exception as e:
            print(f"      [!] kitting failed: {e}")
            return None
        finally:
            self.rig.ensure_inventory_closed()

        for slot_name, key in want.items():
            cur = got.get(slot_name, '')
            if key is None and cur:
                print(f"      [!] {slot_name} should be empty, reads {cur!r}")
                return None
            if key is not None and not self._matches(cur, key):
                print(f"      [!] {slot_name} should be {key}, reads {cur!r}")
                return None
        return got

    @staticmethod
    def _matches(readback, key):
        """read_slots names and spawner keys are different vocabularies; the
        asset name in the spawner table is the bridge."""
        if not readback:
            return False
        asset = spawner_mod.ATTACHMENTS.get(key, {}).get('asset', '')
        r = readback.lower()
        return key.lower() in r or (asset and asset.lower() in r) or \
            (asset and r in asset.lower())


def measure_cell(rig, weapon, posture, mags, slot, log, cfg_name, want):
    """Fire `mags` magazines and record what the curve did not cancel."""
    gun_seen, att = rig.read_loadout(slot=slot)
    if gun_seen is None:
        print("      [!] inventory would not open — cannot read attachments")
        return None
    if gun_seen and gun_seen != weapon:
        print(f"      [!] expected {weapon}, inventory says {gun_seen!r}")
        return None

    w = Weapon()
    w.set('name', weapon)
    w.set('posture', posture)
    w.set('muzzle', (att or {}).get('muzzle', ''))
    w.set('grip', (att or {}).get('grip', ''))
    w.set_seq()
    if not len(w.t_s):
        print(f"      [!] no curve for {weapon}")
        return None
    pattern_counts = float(np.sum(w.dy_s))

    rig.mouse.upload_pattern(w.dx_s, w.dy_s, w.t_s, w.bullet_interval_s)
    rig.mouse.set_recoil_enabled(True)
    time.sleep(0.3)

    if not rig.ensure_posture(posture):
        print(f"      [!] could not reach posture {posture}")
        return None
    # Home against the pitch stop and rise to the measurable middle, so every
    # magazine in every cell starts from the same absolute aim. The reference
    # is taken after that, in ADS, because it describes wherever homing landed.
    # Top up before the first round. Fitting a magazine does not fill it — the
    # gun keeps whatever the last configuration left in it, so the opening
    # burst of a cell runs short. In the bare m416 cell that one short
    # magazine pulled the mean 85 counts off and took the cell's spread from
    # ~2% to 10%, which then propagated into every ratio measured against it
    # and made three multiplicative combinations look 6% wrong.
    rig.mouse.key(HID_KEY_R, 60)
    time.sleep(0.4)
    rig.wait_reload()

    rig.flush(6)
    rig.goto_pitch_centre()
    rig.set_reference()

    rows = []
    for i in range(mags):
        if not focus_keeper().ok(f'mag {i}'):
            break
        if i > 0:
            if not rig.ensure_ads():
                print("      [!] could not re-enter ADS after reload")
                break
            back = rig.goto_pitch_centre()
            rig.set_reference()
            if back:
                print(f"        re-homed, {back:+d} counts above the stop")
            # A magazine fired from an unknown position is not noisy data, it
            # is wrong data that looks fine — at the pitch clamp the view
            # barely moves and the weapon measures unusually mild. Stop the
            # cell instead of recording it.
            if rig.tracking_lost:
                print("        [!] view position is no longer known — "
                      "abandoning the rest of this cell")
                break
        rec, fire_s, steps, fire_end = rig.fire_magazine()
        if steps == 0:
            print("        no rounds fired (still reloading?) — skipped")
            time.sleep(1.5)
            continue
        a = analyse(rec.finish(), rig.K, w.bullet_interval_s, fire_end)
        if a is None:
            continue
        a.update(mag=i, fire_s=round(fire_s, 2), ammo_steps=steps,
                 fps=round(rec.effective_fps(), 1))
        rows.append(a)
        rig.pending_pitch += a['view_drift_counts']
        print(f"        mag {i}: {fire_s:.2f}s  residual "
              f"{a['cum_counts']:+8.1f} ({100*a['cum_counts']/pattern_counts:+6.1f}%)"
              f"  oor={a['n_out_of_range']} hand={a['human_counts']:+.0f}"
              f"/{a['human_abs_counts']:.0f}")
        if rig.wait_reload() is None:
            print("        [!] auto-reload never finished — stopping cell")
            break

    if not rows:
        return None

    # Magazines that fired a different number of rounds are not repeats of the
    # same measurement and averaging them is not noise reduction, it is a
    # wrong answer with a big error bar. A short magazine carries less recoil
    # AND less compensation, so its residual is not comparable — one of them
    # in the bare m416 cell moved the mean 85 counts and took the cell from
    # 2% spread to 10%, which propagated into every ratio taken against it.
    lens = [len(r['per_bullet_counts']) for r in rows]
    keep = int(np.median(lens))
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
    bi = w.bullet_interval_s
    nb = int(w.t_s[-1] / bi) + 1
    comp = np.zeros(nb)
    for dy, t in zip(w.dy_s, w.t_s):
        comp[min(nb - 1, int(t / bi))] += dy
    fired = int(np.median([len(r['per_bullet_counts']) for r in rows]))
    comp_fired = float(comp[:fired].sum())
    # Rounds past the end of the curve get no compensation at all, which shows
    # up as a spike in the last bins rather than as noise. Worth naming.
    uncovered = max(0, fired - nb)
    rec = {
        'type': 'cell', 'weapon': weapon, 'config': cfg_name, 'want': want,
        'posture': posture, 'sight': rig.sight, 'K': rig.K,
        'attachments': att, 'scale': w.scale,
        'posture_factor': w.get_posture_factor(),
        'pattern_counts': pattern_counts, 'n_mags': len(rows),
        'residual_counts_mean': float(cc.mean()),
        'residual_counts_std': float(cc.std()),
        'residual_pct': float(100 * cc.mean() / pattern_counts),
        # The quantity every downstream comparison is actually about.
        'true_counts': float(comp_fired + cc.mean()),
        'comp_over_fired': comp_fired,
        'bullets_fired': fired,
        'bullets_in_curve': nb,
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


def harvest_weapon(rig, panel, kit, sc, weapon, configs, postures, mags,
                   slot, log, done):
    cls = ROSTER.get(weapon, (None,))[0]
    parts = PART_FOR_CLASS.get(cls, {})

    todo = [c for c in configs if (weapon, c) not in done]
    # A config asking for a slot this class has no part for measures the same
    # thing as one that does not ask, under a different name. Drop it.
    skipped = [c for c in todo
               if any(not parts.get(s) for s in parse_config(c))]
    for c in skipped:
        need = [s for s in parse_config(c) if not parts.get(s)]
        print(f"    skipping {c}: no {'/'.join(need)} part for class {cls}")
    todo = [c for c in todo if c not in skipped]
    if not todo:
        print(f"  nothing to do for {weapon}")
        return []

    # Strip first: the incoming gun evicts whatever is in the rack, and an
    # evicted gun leaves wearing everything it had on.
    kit.strip()

    if not panel.ensure_open():
        print("  [!] spawner panel would not open")
        return []
    if not sc.sync(need_cols=(1,)):    # the weapon column
        print("  [!] spawner layout would not read")
        panel.ensure_closed()
        return []
    if not sc.give_weapon(weapon):
        print(f"  [!] spawner would not produce {weapon}")
        panel.ensure_closed()
        return []
    if not panel.ensure_closed():
        print("  [!] spawner panel would not close")
        return []

    out = []
    for cfg in todo:
        fill = parse_config(cfg)
        want = {'scope': SCOPE_PART, 'magazine': MAG_FOR_CLASS.get(cls)}
        # Every controlled slot is named, filled or emptied — see TEST_SLOTS.
        want.update({s: (parts.get(s) if s in fill else None)
                     for s in TEST_SLOTS})
        print(f"    config {cfg}: {want}")
        if kit.apply(want) is None:
            print(f"    [!] could not reach config {cfg} — skipping")
            continue
        for posture in postures:
            print(f"      posture {posture}")
            r = measure_cell(rig, weapon, posture, mags, slot, log, cfg, want)
            if r:
                out.append(r)
                done.add((weapon, cfg))
    return out


def stock_parts(panel, sc, keys):
    """A backpack, then one of each part, in that order.

    The order is the point. An attachment spawns INTO the backpack, so with no
    backpack there is nowhere for one to go — and it does not fail cleanly. The
    parts land somewhere else, the inventory rows shift under the drag targets,
    and kitting reads back a part nobody asked for: a run was told to fit a
    compensator, fitted a suppressor, and skipped seven configs in a row.

    Re-run after every range re-entry, which empties the backpack along with
    everything in it.
    """
    if not panel.ensure_open():
        return False
    # Only column 2 needs finding: the backpack is driven from fixed
    # coordinates, because the translucent panel makes finding column 3
    # depend on what the player is standing in front of.
    ok = sc.sync(need_cols=(2,))
    if ok:
        if not sc.give_gear(BACKPACK):
            print(f"[!] spawner would not produce {BACKPACK} — parts have "
                  f"nowhere to go; stopping rather than kitting blind")
            panel.ensure_closed()
            return False
        for k in keys:
            if not sc.give_attachment(k):
                print(f"[!] spawner would not produce {k}")
                ok = False
    panel.ensure_closed()
    return ok


def load_done(path):
    done = set()
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get('type') == 'cell':
                done.add((r['weapon'], r['config']))
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
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapons', default='ar')
    ap.add_argument('--configs', default='bare,both')
    ap.add_argument('--postures', default='standing')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--parts', default='',
                    help='swap which part fills a slot, e.g. '
                         'muzzle=brake_ar,grip=angled_grip. This is how a '
                         'second part in the same slot gets measured against '
                         'the first.')
    ap.add_argument('--semi', action='store_true',
                    help='include semi-auto and burst weapons, which have no '
                         'full-auto curve to measure')
    ap.add_argument('--mags', type=int, default=3)
    ap.add_argument('--slot', type=int, default=2,
                    help='the spawner always fills slot 2')
    ap.add_argument('--out', default='')
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--countdown', type=int, default=6)
    ap.add_argument('--session', default='auto', choices=('manual', 'auto'),
                    help="how to get back in when the range evicts us; 'auto' "
                         "drives the lobby via detector/lobby_control.py")
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
        HERE, f"harvest_{args.sight}_{datetime.now().strftime('%m%d_%H%M')}.jsonl")
    done = load_done(out) if args.resume else set()

    print(f"weapons  : {len(weapons)} — {', '.join(weapons)}")
    print(f"configs  : {', '.join(configs)}")
    print(f"postures : {', '.join(postures)}")
    print(f"out      : {out}")
    print(f"est.     : ~{len(weapons)*len(configs)*len(postures)*args.mags*9/60:.0f}"
          f" min of firing, plus spawner and inventory work")
    print("\n[SHADOW MODE] nothing is written back to any curve or scale.\n")

    rig = Rig(args.sight)
    panel = Panel(rig.mouse)
    sc = SpawnerControl()
    kit = Kitter(rig, slot=args.slot)
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
        panel.close_grabber()
        return 1
    time.sleep(0.6)     # the game ignores input for a few frames after a
                        # foreground change; the first comma would be eaten

    # "Are we in the training range?" has exactly one honest answer here: the
    # item spawner opens. Nothing else tells the range apart from any other
    # match, and the spawner is what the run needs anyway — so the in-range
    # test and the at-a-spawner test are the same press.
    def at_spawner():
        ok = panel.ensure_open()
        panel.ensure_closed()
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
        panel.close_grabber()
        return 1

    log = open(out, 'a', encoding='utf-8')
    log.write(json.dumps({
        'type': 'header', 'sight': args.sight, 'K': rig.K,
        'patch': rig.tracker.patch, 'patch_h': rig.tracker.patch_h,
        'patch_xs': list(rig.tracker.xs), 'band_y': rig.tracker.band_y,
        'mags': args.mags, 'configs': configs, 'slot': args.slot,
        'ts': datetime.now().isoformat(timespec='seconds'),
    }) + '\n')

    # Only the slots some config actually fills get stocked: every spare in
    # 库存 is one more thing find() can pick instead of the one meant.
    wanted_slots = frozenset().union(*(parse_config(c) for c in configs)) \
        if configs else frozenset()
    parts = {SCOPE_PART}
    for w in weapons:
        cls = ROSTER.get(w, (None,))[0]
        table = PART_FOR_CLASS.get(cls, {})
        parts.update(x for x in
                     [table.get(s) for s in wanted_slots] +
                     [MAG_FOR_CLASS.get(cls)] if x)

    rows = []
    try:
        print(f"stocking parts: {', '.join(sorted(parts))}")
        if not stock_parts(panel, sc, sorted(parts)):
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
                if not stock_parts(panel, sc, sorted(parts)):
                    print("[!] could not re-stock after re-entry")
            print(f"\n[{i+1}/{len(weapons)}] {weapon}")
            rows.extend(harvest_weapon(rig, panel, kit, sc, weapon, configs,
                                       postures, args.mags, args.slot, log,
                                       done))
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
        panel.close_grabber()
        log.close()

    report(rows)
    print(f"\n  raw -> {out}")
    print("  rebuild a curve from it with:")
    print(f"    python calibration/fit_curve.py --jsonl {out} "
          f"--weapon <name> --apply")
    return 0


if __name__ == '__main__':
    sys.exit(main())
