"""Unattended recoil calibration sweep for the training range.

Works through {weapons} x {postures}, firing magazines and measuring the
residual left over by the current curve. Everything the game needs — ADS,
posture, reload recovery — is driven from the Pico; the only human step is
swapping weapons, and even that goes away once SpawnerSwitcher lands.

    python calibration/sweep.py --weapons ar --mags 3
    python calibration/sweep.py --weapons aug,m416 --postures standing
    python calibration/sweep.py --weapons smg --resume

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
RELOAD_STATIC_S = 0.35    # counter must hold this long before the mag is ready
RELOAD_MIN_S = 2.0        # ...and if it never visibly moved, wait at least this
SETTLE_AFTER_RELOAD_S = 1.8   # counter refills mid-animation; gun is not ready
ADS_SETTLE_S = 0.5
ADS_WATCH_S = 2.5         # how long to watch for the icon after a right-click;
                          # measured ~0.85 s idle and slower right after firing
POSTURE_WATCH_S = 1.5     # same, for the C/Z animation
TAB_OPEN_S = 0.55
TAB_CLOSE_S = 0.35
POSTURE_SETTLE_S = 0.6
RECENTER_SETTLE_S = 0.25   # let the view stop before the next burst
POSTURES = ('standing', 'crouching', 'prone')

# One implementation, in press/pointer.py, because it guards the mouse calls
# that live there too. Re-exported under the old names so importers of this
# module keep working.
from press.pointer import (game_focused, raise_game, ensure_focus,  # noqa: E402
                           FocusKeeper, focus_keeper, GAME_EXES)


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
        # Pitch owed back to the view, in ADS mouse counts. See recenter().
        self.pending_pitch = 0.0

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

    def read_posture(self, timeout_s=0.8, gap_s=0.08):
        """Watch the posture icon until it reads, or time out.

        Deadline rather than a sample count: the icon is absent during the ADS
        animation, the inventory fade and mid-posture-change, and how long that
        lasts is not a constant — measured ~0.85 s to appear after a click, and
        slower right after firing. A fixed handful of quick samples reads None
        while the animation is still running, and the caller then toggles ADS
        back off, which never converges.

        A None must never be treated as a posture: toggling on an unknown state
        is how an unattended run walks itself into a posture nobody asked for.
        """
        t0 = time.perf_counter()
        while True:
            self.flush(2)
            p = self.posture_det.classify({'posture': self.grab()['posture']})
            if p:
                return p
            if time.perf_counter() - t0 >= timeout_s:
                return None
            time.sleep(gap_s)

    def dump(self, tag):
        """Save the crops behind a failed decision, so a human can see what
        the detector saw instead of guessing from a one-line error."""
        try:
            frame = self.grab()
            for k in ('posture', 'type', 'ammo'):
                if k in frame:
                    cv2.imwrite(os.path.join(HERE, f'fail_{tag}_{k}.png'),
                                frame[k])
            print(f"      [dbg] wrote fail_{tag}_*.png")
        except Exception as e:
            print(f"      [dbg] dump failed: {e}")

    def ensure_inventory_closed(self, tries=3):
        """An inventory left open hides the posture icon AND swallows C/Z,
        which looks exactly like a broken detector."""
        for _ in range(tries):
            self.flush(2)
            if not self.tab_open(self.grab()):
                return True
            self.mouse.key(HID_KEY_TAB, 60)
            time.sleep(TAB_CLOSE_S)
        self.flush(2)
        return not self.tab_open(self.grab())

    def ensure_inventory_open(self, tries=3):
        """Tab is a toggle, so pressing it blind lands in the wrong state half
        the time. Watch instead."""
        for _ in range(tries):
            self.flush(2)
            if self.tab_open(self.grab()):
                return True
            self.mouse.key(HID_KEY_TAB, 60)
            time.sleep(TAB_OPEN_S)
        self.flush(2)
        return self.tab_open(self.grab())

    def read_loadout(self, slot=1):
        """One Tab cycle returns both the weapon name and its attachments."""
        self.ensure_inventory_closed()
        self.mouse.key(HID_KEY_TAB, 60)
        time.sleep(TAB_OPEN_S)
        frame = self.grab()
        ok = self.tab_open(frame)
        gun = att = None
        if ok:
            names = self.gun_det.classify(frame)
            gun = names[slot - 1] or ''
            att = self.att_det.classify(frame).get(slot)
        if not self.ensure_inventory_closed():
            print("      [!] inventory would not close")
        return (gun, att) if ok else (None, None)

    # ── state control ──

    def ensure_ads(self, tries=3):
        """Get into ADS, using the posture icon as the ADS indicator.

        The icon only renders while aiming, so "icon visible" == "in ADS".
        That matters because right-click is a TOGGLE here: clicking it without
        knowing the current state lands in the wrong one half the time, and
        there is no other reliable read of ADS from the HUD.

        Each click is then WATCHED to completion rather than sampled once.
        Clicking again while the previous ADS animation is still playing just
        toggles back out, so an impatient version of this oscillates forever —
        which is exactly what it did before."""
        if self.read_posture(timeout_s=ADS_SETTLE_S) is not None:
            return True
        for _ in range(tries):
            self.mouse.click(buttons=0x02, duration_ms=60)
            t0 = time.perf_counter()
            if self.read_posture(timeout_s=ADS_WATCH_S) is not None:
                dt = time.perf_counter() - t0
                if dt > ADS_SETTLE_S:
                    print(f"      [ads] icon appeared {dt:.2f}s after click")
                return True
        return False

    def ensure_posture(self, target, tries=4):
        """Toggle until the icon detector agrees. Keypresses alone are not
        trusted: one dropped toggle would mislabel an entire run.

        Requires ADS (the icon does not render from the hip) and a closed
        inventory (which hides the icon and swallows C/Z)."""
        if not self.ensure_inventory_closed():
            print("      [!] inventory stuck open — C/Z would be swallowed")
            self.dump('inventory')
            return False
        if not self.ensure_ads():
            print("      [!] no posture icon — not in ADS? cannot verify")
            self.dump('ads')
            return False
        for _ in range(tries):
            cur = self.read_posture(timeout_s=POSTURE_WATCH_S)
            if cur is None and self.ensure_ads(tries=2):
                # Going prone can drop ADS, and the icon goes with it — that
                # reads identically to "detector broken", so re-aim and re-read
                # before believing it.
                cur = self.read_posture(timeout_s=POSTURE_WATCH_S)
            if cur == target:
                return True
            if cur is None:
                # Never toggle on an unknown state — a blind C/Z here is how an
                # unattended run ends up measuring a posture nobody asked for.
                print(f"      [!] posture unreadable (want {target})")
                self.dump('posture')
                return False
            print(f"      posture {cur} -> {target}")
            if target == 'prone':
                self.mouse.key(HID_KEY_Z, 60)
            elif target == 'crouching':
                # from prone, Z stands up first; C then crouches
                self.mouse.key(HID_KEY_Z if cur == 'prone' else HID_KEY_C, 60)
            else:  # standing
                self.mouse.key(HID_KEY_Z if cur == 'prone' else HID_KEY_C, 60)
            time.sleep(POSTURE_SETTLE_S)
        cur = self.read_posture(timeout_s=POSTURE_WATCH_S)
        if cur != target:
            print(f"      [!] gave up at {cur!r}, wanted {target}")
            self.dump('posture')
        return cur == target

    def recenter(self):
        """Undo the pitch drift left behind by previous magazines.

        A magazine never ends where it started: the compensation is wrong by
        exactly the residual being measured, so every burst walks the view a
        few hundred counts and the walk accumulates. PUBG clamps pitch — at
        straight up or straight down the view simply stops moving, and a
        magazine fired there measures near-zero recoil while reporting nothing
        wrong. That is a silently corrupted cell, which is worse than a failed
        one.

        Must run in ADS. The drift was measured in ADS counts and a mouse
        count buys a third as much rotation from the hip (K 0.50 against
        1.55), so recentring from the hip under-corrects by 3x — and the
        auto-reload drops out of ADS, which is exactly when it is tempting to
        do this.
        """
        d = int(round(self.pending_pitch))
        self.pending_pitch = 0.0
        if abs(d) < 2:
            return 0
        self.mouse.move(0, d)
        time.sleep(RECENTER_SETTLE_S)
        return d

    def enter_ads(self):
        self.mouse.click(buttons=0x02, duration_ms=60)
        time.sleep(ADS_SETTLE_S)

    # ── one magazine ──

    def fire_magazine(self):
        rec = MagazineRecorder(
            self.tracker,
            human_fn=getattr(self.mouse, 'human_totals', None))
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
        # last_change is the last round leaving the magazine. Everything after
        # it is the camera drifting back toward the pre-fire aim, which is real
        # but happens after the bullets are already gone -- folding it into the
        # residual flatters the total while telling you nothing about where the
        # rounds went.
        return rec, time.perf_counter() - t0, steps, last_change

    def wait_reload(self):
        """PUBG reloads by itself; we only wait it out (and it exits ADS).

        Watches for the counter to leave its empty-magazine reading and then
        hold still, rather than waiting for it to match a snapshot taken back
        at the start of the cell. The snapshot goes stale — the standing cell
        lost four of its five magazines twice running to a reference that
        never came back — and "moved, then settled" needs no reference at all.
        """
        empty = self.ammo_sig(self.grab())
        t0 = time.perf_counter()
        prev, stable_since = None, None
        while True:
            now = time.perf_counter()
            if now - t0 >= RELOAD_TIMEOUT_S:
                break
            sig = self.ammo_sig(self.grab())
            if prev is not None and float(np.mean(sig != prev)) < AMMO_CHANGED:
                if stable_since is None:
                    stable_since = now
            else:
                stable_since = None
            prev = sig
            if stable_since is None or now - stable_since <= RELOAD_STATIC_S:
                continue
            # Normally the counter visibly climbs back and holds. But the
            # magazine can refill inside the 0.55 s the fire loop spends
            # confirming the gun is empty — a quickdraw magazine is that fast —
            # and then `empty` is already the full reading and no change ever
            # comes. Settled plus long enough is the same evidence.
            if float(np.mean(sig != empty)) > AMMO_CHANGED or \
                    now - t0 > RELOAD_MIN_S:
                time.sleep(SETTLE_AFTER_RELOAD_S)
                return now - t0
        self.dump('reload')
        return None


def analyse(res, K, bullet_interval_s, fire_end_ts=None):
    dy = np.asarray(res.dy, dtype=float)
    ts = np.asarray(res.ts, dtype=float)
    if len(ts) < 2:
        return None

    # Screen motion is recoil - compensation - hand, all in mouse counts. The
    # compensation is what we set out to grade, so only the hand has to come
    # back out; without the Pico reporting it this is a zero vector and the
    # measurement silently assumes a still hand.
    human = (np.asarray(res.human_dy, dtype=float) if res.human_dy
             else np.zeros_like(dy))

    oor = np.asarray(res.out_of_range, dtype=bool)
    if len(oor) != len(dy):                     # replayed or truncated result
        oor = np.zeros(len(dy), dtype=bool)

    # Where the view actually ended up, over the WHOLE recording rather than
    # the burst — post-fire recovery moves it too, and recentring has to undo
    # the real position, not the analytically interesting part of it.
    drift = float(np.nansum(np.where(oor, np.nan, dy)) / K)

    # Cut at the last round fired, plus the one bullet interval its own recoil
    # needs to play out. Every per-frame array is sliced together — slicing
    # some and not others misaligns the out-of-range mask, and a misaligned
    # mask fails silently.
    if fire_end_ts is not None:
        keep = ts <= fire_end_ts + bullet_interval_s
        if keep.sum() >= 2:
            dy, human, ts, oor = dy[keep], human[keep], ts[keep], oor[keep]

    ts = ts - ts[0]
    counts = dy / K + human

    # Past half a patch the correlation peak wraps, so a frame flagged
    # out-of-range is not merely imprecise, it is wrong by a whole patch —
    # 83 counts at K=1.55. Dropping the frame costs only the ~1 count of
    # residual it carried; keeping it cost 266 counts on a magazine where the
    # hand moved fast enough to hit the limit three times.
    counts = np.where(oor, np.nan, counts)
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
        'n_dropped_oor': int(np.sum(oor)),
        'view_drift_counts': drift,
        'n_low_gate': int(np.sum(res.gates)),
        # Net is what was removed from the residual; abs is how much hand
        # motion happened at all. A small net with a large abs means the hand
        # wandered and came back — the correction still holds, but the run is
        # noisier than a still one.
        'human_counts': float(np.nansum(human)),
        'human_abs_counts': float(np.nansum(np.abs(human))),
        'mean_mad': float(np.mean(res.mad)) if res.mad else float('nan'),
    }


def calibrate_combo(rig, weapon, posture, mags, log):
    """Measure one (weapon, posture) cell. Returns a summary dict or None."""
    # Loadout first: opening Tab drops ADS, and posture can only be verified
    # from ADS (the icon does not render from the hip).
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

    # Enters ADS as a side effect — the posture icon is the ADS indicator.
    if not rig.ensure_posture(posture):
        print(f"    [!] could not reach posture {posture}")
        return None
    rig.recenter()
    rig.flush(6)

    rows = []
    for i in range(mags):
        if not focus_keeper().ok(f'{weapon}/{posture} mag {i}'):
            break
        if i > 0:                    # auto-reload drops us to the hip
            if not rig.ensure_ads():
                print("      [!] could not re-enter ADS after reload")
                break
            rig.recenter()

        rec, fire_s, steps, fire_end = rig.fire_magazine()
        if steps == 0:
            print(f"      mag {i}: no rounds fired (reload still running?) "
                  f"— skipped")
            time.sleep(1.5)
            continue
        a = analyse(rec.finish(), rig.K, w.bullet_interval_s, fire_end)
        if a is None:
            continue
        a.update(mag=i, fire_s=round(fire_s, 2), ammo_steps=steps,
                 fps=round(rec.effective_fps(), 1))
        rows.append(a)
        rig.pending_pitch += a['view_drift_counts']
        print(f"      mag {i}: {fire_s:.2f}s {steps:3d} steps  "
              f"residual {a['cum_counts']:+8.1f} counts "
              f"({100*a['cum_counts']/pattern_counts:+6.1f}% of pattern)  "
              f"rej={a['n_rejected']} oor={a['n_out_of_range']} "
              f"hand={a['human_counts']:+.0f}/{a['human_abs_counts']:.0f}")
        if rig.wait_reload() is None:
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

    print("\n>>> Taking the foreground. Stand still and aim at something with "
          "texture — the recoil is measured off it.")
    if not ensure_focus(countdown_s=args.countdown, label='the sweep'):
        print("[!] ABORT: game not focused, and could not take the "
              "foreground. Is PUBG running?")
        rig.close()
        return 1
    time.sleep(0.6)              # the game ignores the first frames after a
    keeper = focus_keeper()      # foreground change

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
                if not keeper.ok(f'{weapon}/{posture}'):
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
