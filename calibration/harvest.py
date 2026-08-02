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
from detector.weapon import Weapon, WEAPON_RPM
from press.pico_mouse import HID_KEY_COMMA

from sweep import Rig, analyse, game_focused, POSTURES
import spawner_control as spawner_mod
from spawner_control import SpawnerControl, ROSTER
from attach_control import AttachControl

HERE = os.path.dirname(os.path.abspath(__file__))

PANEL_WATCH_S = 3.0       # comma -> panel drawn; generous, it is a full screen
PANEL_SETTLE_S = 0.5
KIT_SETTLE_S = 0.6

# A compensator exists per weapon class and nowhere else; LMGs have none, so
# their muzzle cells are skipped rather than silently measured bare twice.
MUZZLE_FOR_CLASS = {'AR': 'comp_ar', 'DMR': 'comp_ar', 'SMG': 'comp_smg'}

# half_grip, not thumb_grip: detector/CLAUDE.md records Lower_ThumbGrip_C as
# drifted, so it reads back as something else and every verification fails.
# angled_grip is the interesting control — the model predicts exactly 1.000
# for it, which is the most falsifiable claim in the whole table.
GRIP_FOR_CLASS = {'AR': 'half_grip', 'DMR': 'half_grip', 'SMG': 'half_grip'}

SCOPE_PART = 'red_dot'

# (name, wants_muzzle, wants_grip)
CONFIGS = {
    'bare':   (False, False),
    'muzzle': (True, False),
    'grip':   (False, True),
    'both':   (True, True),
}


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
    rig.flush(6)

    rows = []
    for i in range(mags):
        if not game_focused():
            print("      [!] lost focus — abandoning this cell")
            break
        if i > 0 and not rig.ensure_ads():
            print("      [!] could not re-enter ADS after reload")
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
        print(f"        mag {i}: {fire_s:.2f}s  residual "
              f"{a['cum_counts']:+8.1f} ({100*a['cum_counts']/pattern_counts:+6.1f}%)"
              f"  oor={a['n_out_of_range']} hand={a['human_counts']:+.0f}"
              f"/{a['human_abs_counts']:.0f}")
        if rig.wait_reload() is None:
            print("        [!] auto-reload never finished — stopping cell")
            break

    if not rows:
        return None
    cc = np.array([r['cum_counts'] for r in rows])
    rec = {
        'type': 'cell', 'weapon': weapon, 'config': cfg_name, 'want': want,
        'posture': posture, 'sight': rig.sight, 'K': rig.K,
        'attachments': att, 'scale': w.scale,
        'posture_factor': w.get_posture_factor(),
        'pattern_counts': pattern_counts, 'n_mags': len(rows),
        'residual_counts_mean': float(cc.mean()),
        'residual_counts_std': float(cc.std()),
        'residual_pct': float(100 * cc.mean() / pattern_counts),
        # pattern + residual is the gun's own recoil, which is the quantity
        # every downstream comparison is actually about.
        'true_counts': float(pattern_counts + cc.mean()),
        'mags': rows,
        'ts': datetime.now().isoformat(timespec='seconds'),
    }
    log.write(json.dumps(rec) + '\n')
    log.flush()
    print(f"      => true recoil {rec['true_counts']:.1f} counts "
          f"(residual {cc.mean():+.1f} +- {cc.std():.1f})")
    return rec


def harvest_weapon(rig, panel, kit, sc, weapon, configs, postures, mags,
                   slot, log, done):
    cls = ROSTER.get(weapon, (None,))[0]
    muzzle_key = MUZZLE_FOR_CLASS.get(cls)
    grip_key = GRIP_FOR_CLASS.get(cls)

    todo = [c for c in configs if (weapon, c) not in done]
    todo = [c for c in todo
            if not (CONFIGS[c][0] and not muzzle_key)
            and not (CONFIGS[c][1] and not grip_key)]
    if not todo:
        print(f"  nothing to do for {weapon}")
        return []

    # Strip first: the incoming gun evicts whatever is in the rack, and an
    # evicted gun leaves wearing everything it had on.
    kit.strip()

    if not panel.ensure_open():
        print("  [!] spawner panel would not open")
        return []
    if not sc.sync():
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
        want_m, want_g = CONFIGS[cfg]
        want = {'scope': SCOPE_PART,
                'muzzle': muzzle_key if want_m else None,
                'grip': grip_key if want_g else None}
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
    """Spawn one of each part, once, before anything else runs."""
    if not panel.ensure_open():
        return False
    ok = sc.sync()
    if ok:
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


def expand(spec):
    groups = {}
    for key, (cls, _) in ROSTER.items():
        groups.setdefault(cls.lower(), []).append(key)
    groups['all'] = sorted(ROSTER)
    out = []
    for tok in spec.split(','):
        tok = tok.strip()
        if tok:
            out.extend(sorted(groups.get(tok, [tok])))
    seen, uniq = set(), []
    for x in out:
        if x in WEAPON_RPM and x in ROSTER and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def report(rows):
    if not rows:
        print("\nnothing harvested")
        return
    by = {}
    for r in rows:
        by.setdefault(r['weapon'], {})[r['config']] = r['true_counts']
    print("\n" + "=" * 78)
    print("TRUE RECOIL PER CONFIG (counts over one magazine)")
    print("=" * 78)
    print(f"{'weapon':<9}{'bare':>9}{'muzzle':>9}{'grip':>9}{'both':>9}"
          f"{'m x g':>9}{'measured':>10}  orthogonal?")
    print("-" * 78)
    for w in sorted(by):
        c = by[w]
        b = c.get('bare')
        cells = [c.get(k) for k in ('bare', 'muzzle', 'grip', 'both')]
        line = f"{w:<9}" + ''.join(
            f"{v:>9.0f}" if v else f"{'-':>9}" for v in cells)
        if b and c.get('muzzle') and c.get('grip') and c.get('both'):
            pred = (c['muzzle'] / b) * (c['grip'] / b)
            meas = c['both'] / b
            gap = 100 * (meas / pred - 1)
            verdict = 'yes' if abs(gap) < 3 else f'NO ({gap:+.1f}%)'
            line += f"{pred:>9.3f}{meas:>10.3f}  {verdict}"
        print(line)
    print("\n  ratios are against that weapon's own bare measurement, so a")
    print("  weapon-independent model shows the same column everywhere.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapons', default='ar')
    ap.add_argument('--configs', default='bare,both')
    ap.add_argument('--postures', default='standing')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--mags', type=int, default=3)
    ap.add_argument('--slot', type=int, default=2,
                    help='the spawner always fills slot 2')
    ap.add_argument('--out', default='')
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    weapons = expand(args.weapons)
    configs = [c.strip() for c in args.configs.split(',') if c.strip()]
    postures = [p.strip() for p in args.postures.split(',') if p.strip()]
    bad = [c for c in configs if c not in CONFIGS] + \
          [p for p in postures if p not in POSTURES]
    if bad:
        print(f"[!] unknown: {bad}")
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

    print("\n>>> Stand at an item spawner, facing something with texture.")
    for s in range(args.countdown, 0, -1):
        print(f"    starting in {s} ...", flush=True)
        time.sleep(1.0)
    if not game_focused():
        print("[!] ABORT: game not focused.")
        rig.close()
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

    parts = {SCOPE_PART}
    for w in weapons:
        cls = ROSTER.get(w, (None,))[0]
        parts.update(x for x in (MUZZLE_FOR_CLASS.get(cls),
                                 GRIP_FOR_CLASS.get(cls)) if x)

    rows = []
    try:
        print(f"stocking parts: {', '.join(sorted(parts))}")
        if not stock_parts(panel, sc, sorted(parts)):
            print("[!] could not stock the parts — continuing anyway; "
                  "kitting will fail loudly if one is missing")
        for i, weapon in enumerate(weapons):
            if not game_focused():
                print("[!] lost focus — stopping.")
                break
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
