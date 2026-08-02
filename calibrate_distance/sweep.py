"""Unattended recoil calibration sweep for the training range.

Works through {weapons} x {postures}, firing magazines and measuring the
residual left over by the current curve. Everything the game needs — ADS,
posture, reload recovery — is driven from the Pico; the only human step is
swapping weapons, and even that goes away once SpawnerSwitcher lands.

    python calibrate_distance/sweep.py --weapons ar --mags 3
    python calibrate_distance/sweep.py --weapons aug,m416 --postures standing
    python calibrate_distance/sweep.py --weapons smg --resume

SHADOW MODE: results are written to JSONL and printed as suggested factors.
Nothing is written back to weapon_scales.json / posture_scales.json — closed
loop learning reinforces its own errors, so a human approves first.

Why each piece exists (all learned the hard way, see
docs/recoil_observer_design.md):

  * compensation stays ON while measuring — AUG's pattern is ~1358 counts over
    40 rounds = 2100 px at K=1.55, so uncompensated the view ends up in the
    sky where there is no texture to correlate. Compensated, what remains IS
    the residual.
  * attachments are read via Tab before every weapon — weapon_scales.json is
    calibrated WITH compensator+grip, so a bare-gun pattern over-compensates
    by 30%.
  * PUBG auto-reloads and drops out of ADS doing so; measuring the next
    magazine from the hip would apply the scoped K (1.55) to hip-fire motion
    (0.50).
  * posture is verified by the icon detector rather than assumed from
    keypresses, because a missed toggle silently mislabels a whole run.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np
import win32gui
import win32process

from config import (HUD_REGIONS, RECOIL_SIGHT_PROFILES,
                    RECOIL_K_DEFAULT_SCOPED, TAB_PIXEL_THRESH,
                    TAB_COUNT_MIN, TAB_COUNT_MAX)
from detector.attachment_detector import AttachmentDetector
from detector.cropper import make_grabber
from detector.posture_detector import PostureDetector
from detector.view_tracker import ViewTracker, MagazineRecorder
from detector.weapon import Weapon, WEAPON_RPM, ar, smg, mg
from detector.weapon_template_detector import TabWeaponDetector
from press.pico_mouse import (get_mouse, HID_KEY_TAB, HID_KEY_C, HID_KEY_Z)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weapon_switcher import get_switcher

cv2.setNumThreads(1)
HERE = os.path.dirname(os.path.abspath(__file__))

# ── timing (wall clock; frame counts are unreliable because DXGI re-serves
#    the previous frame while the screen is idle) ──
AMMO_THRESH = 200
AMMO_CHANGED = 0.02
EMPTY_STATIC_S = 0.55     # ammo frozen this long while firing => magazine out
MIN_FIRE_S = 0.8
MAX_FIRE_S = 9.0
RELOAD_TIMEOUT_S = 9.0
SETTLE_AFTER_RELOAD_S = 1.8   # counter refills mid-animation; gun is not ready
ADS_SETTLE_S = 0.5
TAB_OPEN_S = 0.55
TAB_CLOSE_S = 0.35
POSTURE_SETTLE_S = 0.6
GAME_HINTS = ('battlegrounds', 'pubg', 'tslgame')
POSTURES = ('standing', 'crouching', 'prone')


def game_focused():
    hwnd = win32gui.GetForegroundWindow()
    try:
        title = win32gui.GetWindowText(hwnd)
    except Exception:
        title = ''
    exe = ''
    try:
        import psutil
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = psutil.Process(pid).name()
    except Exception:
        pass
    return any(k in (title + exe).lower() for k in GAME_HINTS)


class Rig:
    """Owns the capture, the Pico and the detectors for one sweep."""

    def __init__(self, sight):
        prof = RECOIL_SIGHT_PROFILES.get(sight, {})
        self.sight = sight
        self.K = prof.get('K', RECOIL_K_DEFAULT_SCOPED)
        self.tracker = ViewTracker(patch_xs=prof.get('patch_xs'))
        self.mouse = get_mouse()
        self.att_det = AttachmentDetector()
        self.gun_det = TabWeaponDetector()
        self.posture_det = PostureDetector()

        regions = dict(self.tracker.regions())
        for k in ('ammo', 'type', 'posture', 'gun_name_1', 'gun_name_2'):
            regions[k] = HUD_REGIONS[k]
        for k, v in HUD_REGIONS.items():
            if k.startswith('att_'):
                regions[k] = v
        self.grabber, self.paced = make_grabber(regions)

    def close(self):
        try:
            self.mouse.click(buttons=0x00, duration_ms=0)
            self.mouse.set_recoil_enabled(False)
        except Exception:
            pass
        self.grabber.close()

    # ── screen reads ──

    def grab(self):
        return self.grabber.grab()

    def flush(self, n=8):
        for _ in range(n):
            self.grabber.grab()

    def ammo_sig(self, frame):
        g = cv2.cvtColor(frame['ammo'], cv2.COLOR_BGR2GRAY)
        return cv2.threshold(g, AMMO_THRESH, 255, cv2.THRESH_BINARY)[1] > 0

    def tab_open(self, frame):
        g = cv2.cvtColor(frame['type'], cv2.COLOR_BGR2GRAY)
        n = int((g > TAB_PIXEL_THRESH).sum())
        return TAB_COUNT_MIN <= n <= TAB_COUNT_MAX

    def read_posture(self):
        return self.posture_det.classify({'posture': self.grab()['posture']})

    def read_loadout(self, slot=1):
        """One Tab cycle returns both the weapon name and its attachments."""
        if self.tab_open(self.grab()):
            self.mouse.key(HID_KEY_TAB, 60)
            time.sleep(TAB_CLOSE_S)
        self.mouse.key(HID_KEY_TAB, 60)
        time.sleep(TAB_OPEN_S)
        frame = self.grab()
        ok = self.tab_open(frame)
        gun = att = None
        if ok:
            names = self.gun_det.classify(frame)
            gun = names[slot - 1] or ''
            att = self.att_det.classify(frame).get(slot)
        self.mouse.key(HID_KEY_TAB, 60)
        time.sleep(TAB_CLOSE_S)
        return (gun, att) if ok else (None, None)

    # ── state control ──

    def ensure_posture(self, target, tries=4):
        """Toggle until the icon detector agrees. Keypresses alone are not
        trusted: one dropped toggle would mislabel an entire run."""
        for _ in range(tries):
            cur = self.read_posture()
            if cur == target:
                return True
            if target == 'prone':
                self.mouse.key(HID_KEY_Z, 60)
            elif target == 'crouching':
                # from prone, Z stands up first; C then crouches
                self.mouse.key(HID_KEY_Z if cur == 'prone' else HID_KEY_C, 60)
            else:  # standing
                self.mouse.key(HID_KEY_Z if cur == 'prone' else HID_KEY_C, 60)
            time.sleep(POSTURE_SETTLE_S)
        return self.read_posture() == target

    def enter_ads(self):
        self.mouse.click(buttons=0x02, duration_ms=60)
        time.sleep(ADS_SETTLE_S)

    # ── one magazine ──

    def fire_magazine(self):
        rec = MagazineRecorder(self.tracker)
        self.mouse.click(buttons=0x01, duration_ms=int(MAX_FIRE_S * 1000))
        t0 = time.perf_counter()
        prev, last_change, steps = None, t0, 0
        while True:
            now = time.perf_counter()
            if now - t0 > MAX_FIRE_S:
                break
            frame = self.grab()
            rec.push(now, frame)
            sig = self.ammo_sig(frame)
            if prev is not None and float(np.mean(sig != prev)) > AMMO_CHANGED:
                last_change = now
                steps += 1
            prev = sig
            if (now - t0) > MIN_FIRE_S and (now - last_change) > EMPTY_STATIC_S:
                break
        self.mouse.click(buttons=0x00, duration_ms=0)
        return rec, time.perf_counter() - t0, steps

    def wait_reload(self, full_sig):
        """PUBG reloads by itself; we only wait it out (and it exits ADS)."""
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < RELOAD_TIMEOUT_S:
            if float(np.mean(self.ammo_sig(self.grab()) != full_sig)) \
                    < AMMO_CHANGED:
                time.sleep(SETTLE_AFTER_RELOAD_S)
                return time.perf_counter() - t0
        return None


def analyse(res, K, bullet_interval_s):
    dy = np.asarray(res.dy, dtype=float)
    ts = np.asarray(res.ts, dtype=float)
    if len(ts) < 2:
        return None
    ts = ts - ts[0]
    counts = dy / K
    nb = int(ts[-1] / bullet_interval_s) + 1
    per_bullet = []
    for b in range(nb):
        m = (ts >= b * bullet_interval_s) & (ts < (b + 1) * bullet_interval_s)
        per_bullet.append(float(np.nansum(counts[m])) if m.any() else 0.0)
    return {
        'n_frames': len(dy), 'span_s': float(ts[-1]),
        'cum_px': float(np.nansum(dy)),
        'cum_counts': float(np.nansum(counts)),
        'per_bullet_counts': [round(v, 3) for v in per_bullet],
        'max_abs_frame_px': float(np.nanmax(np.abs(dy))),
        'n_rejected': int(np.sum(res.n_rejected)),
        'n_out_of_range': int(np.sum(res.out_of_range)),
        'n_low_gate': int(np.sum(res.gates)),
        'mean_mad': float(np.mean(res.mad)) if res.mad else float('nan'),
    }


def calibrate_combo(rig, weapon, posture, mags, log):
    """Measure one (weapon, posture) cell. Returns a summary dict or None."""
    if not rig.ensure_posture(posture):
        print(f"    [!] could not reach posture {posture}")
        return None

    gun_seen, att = rig.read_loadout()
    if gun_seen is None:
        print("    [!] inventory would not open — cannot read attachments")
        return None
    if gun_seen and gun_seen != weapon:
        print(f"    [!] expected {weapon}, inventory says {gun_seen!r}")
        return None

    w = Weapon()
    w.set('name', weapon)
    w.set('posture', posture)
    w.set('muzzle', (att or {}).get('muzzle', ''))
    w.set('grip', (att or {}).get('grip', ''))
    w.set_seq()
    if not len(w.t_s):
        print(f"    [!] no pattern for {weapon}")
        return None
    pattern_counts = float(np.sum(w.dy_s))

    rig.mouse.upload_pattern(w.dx_s, w.dy_s, w.t_s, w.bullet_interval_s)
    rig.mouse.set_recoil_enabled(True)
    time.sleep(0.3)

    rig.enter_ads()
    rig.flush(6)
    full_sig = rig.ammo_sig(rig.grab())

    rows = []
    for i in range(mags):
        if not game_focused():
            print("    [!] lost focus — aborting this combo")
            break
        if i > 0:
            rig.enter_ads()          # auto-reload dropped us to hip fire

        rec, fire_s, steps = rig.fire_magazine()
        if steps == 0:
            print(f"      mag {i}: no rounds fired (reload still running?) "
                  f"— skipped")
            time.sleep(1.5)
            continue
        a = analyse(rec.finish(), rig.K, w.bullet_interval_s)
        if a is None:
            continue
        a.update(mag=i, fire_s=round(fire_s, 2), ammo_steps=steps,
                 fps=round(rec.effective_fps(), 1))
        rows.append(a)
        print(f"      mag {i}: {fire_s:.2f}s {steps:3d} steps  "
              f"residual {a['cum_counts']:+8.1f} counts "
              f"({100*a['cum_counts']/pattern_counts:+6.1f}% of pattern)  "
              f"rej={a['n_rejected']} oor={a['n_out_of_range']}")
        if rig.wait_reload(full_sig) is None:
            print("      [!] auto-reload did not finish — stopping combo")
            break

    if not rows:
        return None

    cc = np.array([r['cum_counts'] for r in rows])
    ratio = 1.0 + cc.mean() / pattern_counts
    summary = {
        'type': 'combo', 'weapon': weapon, 'posture': posture,
        'sight': rig.sight, 'K': rig.K, 'n_mags': len(rows),
        'attachments': att, 'scale': w.scale,
        'posture_factor': w.get_posture_factor(),
        'pattern_counts': pattern_counts,
        'residual_counts_mean': float(cc.mean()),
        'residual_counts_std': float(cc.std()),
        'residual_pct': float(100 * cc.mean() / pattern_counts),
        'implied_ratio': float(ratio),
        'mags': rows,
        'ts': datetime.now().isoformat(timespec='seconds'),
    }
    log.write(json.dumps(summary) + '\n')
    log.flush()
    print(f"    => residual {cc.mean():+.1f} +- {cc.std():.1f} counts "
          f"({summary['residual_pct']:+.1f}%)   implied factor {ratio:.3f}")
    return summary


def load_done(path):
    """(weapon, posture) pairs already measured, for --resume."""
    done = set()
    if os.path.exists(path):
        for line in open(path):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get('type') == 'combo':
                done.add((r['weapon'], r['posture']))
    return done


def expand_weapons(spec):
    groups = {'ar': sorted(ar), 'smg': sorted(smg), 'mg': sorted(mg),
              'all': sorted(ar | smg | mg)}
    out = []
    for tok in spec.split(','):
        tok = tok.strip()
        if not tok:
            continue
        out.extend(groups.get(tok, [tok]))
    seen, uniq = set(), []
    for x in out:
        if x in WEAPON_RPM and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def main():
    ap = argparse.ArgumentParser(
        description='Unattended recoil calibration sweep (shadow mode).')
    ap.add_argument('--weapons', default='ar',
                    help="'ar', 'smg', 'mg', 'all', or names: aug,m416")
    ap.add_argument('--postures', default=','.join(POSTURES))
    ap.add_argument('--sight', default='red_dot',
                    choices=sorted(RECOIL_SIGHT_PROFILES.keys()))
    ap.add_argument('--mags', type=int, default=3,
                    help='magazines per (weapon, posture) cell')
    ap.add_argument('--switcher', default='manual',
                    choices=('manual', 'spawner'))
    ap.add_argument('--out', default='')
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    weapons = expand_weapons(args.weapons)
    postures = [p.strip() for p in args.postures.split(',') if p.strip()]
    bad = [p for p in postures if p not in POSTURES]
    if bad:
        print(f"[!] unknown posture(s): {bad}")
        return 1
    if not weapons:
        print("[!] no weapons selected")
        return 1

    out = args.out or os.path.join(
        HERE, f"sweep_{args.sight}_{datetime.now().strftime('%m%d_%H%M')}.jsonl")
    done = load_done(out) if args.resume else set()

    total = len(weapons) * len(postures)
    print(f"sweep     : {len(weapons)} weapons x {len(postures)} postures "
          f"= {total} cells, {args.mags} mags each")
    print(f"weapons   : {', '.join(weapons)}")
    print(f"postures  : {', '.join(postures)}")
    print(f"sight     : {args.sight}")
    print(f"out       : {out}")
    if done:
        print(f"resume    : {len(done)} cells already done, skipping")
    print(f"est. time : ~{total * args.mags * 9 / 60:.0f} min of firing "
          f"plus weapon swaps")
    print("\n[SHADOW MODE] nothing is written back to the scale files.\n")

    rig = Rig(args.sight)
    print(f"grabber   : {type(rig.grabber).__name__} (paced={rig.paced})  "
          f"K={rig.K:.4f}  {len(rig.tracker.xs)} patches")

    switcher = get_switcher(
        args.switcher, verify_fn=lambda: (rig.read_loadout()[0] or ''))

    print("\n>>> Switch to the game. Stand still, aim at something with "
          "texture,\n    and keep the game focused for the whole run.")
    for s in range(args.countdown, 0, -1):
        print(f"    starting in {s} ...", flush=True)
        time.sleep(1.0)
    if not game_focused():
        print("[!] ABORT: game not focused.")
        rig.close()
        return 1

    log = open(out, 'a')
    log.write(json.dumps({
        'type': 'header', 'sight': args.sight, 'K': rig.K,
        'patch_xs': list(rig.tracker.xs), 'patch': rig.tracker.patch,
        'band_y': rig.tracker.band_y, 'mags': args.mags,
        'ts': datetime.now().isoformat(timespec='seconds'),
    }) + '\n')

    results = []
    try:
        for wi, weapon in enumerate(weapons):
            todo = [p for p in postures if (weapon, p) not in done]
            if not todo:
                print(f"[{wi+1}/{len(weapons)}] {weapon}: all done, skipping")
                continue

            print(f"\n[{wi+1}/{len(weapons)}] {weapon}")
            if not switcher.switch_to(weapon):
                print(f"    skipped — could not equip {weapon}")
                continue

            for posture in todo:
                if not game_focused():
                    print("[!] lost focus — stopping sweep.")
                    raise KeyboardInterrupt
                print(f"    posture: {posture}")
                s = calibrate_combo(rig, weapon, posture, args.mags, log)
                if s:
                    results.append(s)
            rig.ensure_posture('standing')
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        rig.close()
        switcher.close()
        log.close()

    report(results, out)
    return 0


def report(results, out):
    if not results:
        print("\nno cells completed")
        return
    print("\n" + "=" * 88)
    print("SUGGESTED FACTORS (shadow mode — not written to disk)")
    print("=" * 88)
    print(f"{'weapon':<10}{'posture':<11}{'mags':>5}{'residual %':>12}"
          f"{'implied':>9}{'now':>8}{'suggest':>9}  note")
    print("-" * 88)
    for r in sorted(results, key=lambda x: (x['weapon'], x['posture'])):
        ratio = r['implied_ratio']
        if r['posture'] == 'standing':
            now, sug, what = r['scale'], r['scale'] * ratio, 'scale'
        else:
            now = r['posture_factor']
            sug = now * ratio
            what = f"posture[{r['posture']}]"
        flag = ''
        if abs(r['residual_pct']) > 40:
            flag = '  <-- large, check the run'
        if r['residual_counts_std'] > abs(r['residual_counts_mean']) * 0.5:
            flag += '  <-- noisy'
        print(f"{r['weapon']:<10}{r['posture']:<11}{r['n_mags']:>5}"
              f"{r['residual_pct']:>+11.1f}%{ratio:>9.3f}{now:>8.3f}"
              f"{sug:>9.3f}  {what}{flag}")
    print(f"\n  raw -> {out}")
    print("  review, then apply with a separate step — nothing was written.")


if __name__ == '__main__':
    sys.exit(main())
