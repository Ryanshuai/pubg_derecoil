"""Automated training-range calibration: fire, measure, reload, repeat.

Shadow mode by design — it records residuals and diagnostics but does NOT
touch the curve. Closed-loop learning self-reinforces its own mistakes and is
hard to notice going wrong, so the residuals get inspected across a few dozen
magazines before anything is allowed to write back.

Why recoil compensation must stay ON while measuring:
  AUG's stock pattern totals ~1358 counts over 40 rounds = ~2100 px at
  K=1.55, i.e. one and a half screen heights. Uncompensated, the view ends up
  pointing at the sky, where there is no texture to correlate. With
  compensation the view barely moves and what is left IS the residual —
  exactly the quantity the learner needs, with no extra subtraction.

Empty and reload-complete are detected from the ammo counter rather than
timed per weapon, so magazine size, extended mags, quickdraw mags and
tactical reloads all work without a lookup table.

    python calibration/auto_calibrate.py --weapon aug --sight red_dot
    python calibration/auto_calibrate.py --weapon aug --mags 10
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
from detector.view_tracker import ViewTracker, MagazineRecorder
from detector.weapon import Weapon, WEAPON_RPM
from press.pico_mouse import get_mouse, HID_KEY_TAB

cv2.setNumThreads(1)
HERE = os.path.dirname(os.path.abspath(__file__))

AMMO_THRESH = 200        # binarisation level for the ammo digits. 180 was low
                         # enough that muzzle flash lighting the translucent
                         # HUD backing registered as a count change.
AMMO_CHANGED = 0.02      # fraction of pixels; above this the count moved
EMPTY_STATIC_S = 0.55    # ammo unchanged this long while firing => empty
MIN_FIRE_S = 0.8         # never call it empty before this
MAX_FIRE_S = 8.0
RELOAD_TIMEOUT_S = 8.0
# The ammo counter jumps back to full partway through the reload animation,
# while the weapon still cannot fire. Waiting only 0.5 s produced a magazine
# that fired zero rounds. The AUG animation runs ~3.4 s total.
SETTLE_AFTER_RELOAD_S = 1.8
ADS_SETTLE_S = 0.5       # scope-in animation after re-entering ADS
TAB_OPEN_S = 0.55        # inventory animation
TAB_CLOSE_S = 0.35

GAME_HINTS = ('battlegrounds', 'pubg', 'tslgame')


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


def tab_is_open(frame):
    """The 'Type' header only renders while the inventory is up."""
    crop = frame.get('type')
    if crop is None:
        return False
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    n = int((g > TAB_PIXEL_THRESH).sum())
    return TAB_COUNT_MIN <= n <= TAB_COUNT_MAX


def detect_attachments(grabber, mouse, slot):
    """Read the equipped attachments by driving Tab from the Pico.

    Skipping this is what produced a 30% over-compensation on the first run:
    weapon_scales.json is calibrated WITH compensator+grip, so a Weapon left
    at default attachments gets its scale divided back out to bare-gun level
    and then never multiplied down again.
    """
    det = AttachmentDetector()
    if tab_is_open(grabber.grab()):
        mouse.key(HID_KEY_TAB, 60)
        time.sleep(TAB_CLOSE_S)
    mouse.key(HID_KEY_TAB, 60)
    time.sleep(TAB_OPEN_S)
    frame = grabber.grab()
    ok = tab_is_open(frame)
    res = det.classify(frame) if ok else None
    mouse.key(HID_KEY_TAB, 60)
    time.sleep(TAB_CLOSE_S)
    if not ok:
        return None
    return res.get(slot)


def ammo_sig(frame):
    """Binarised ammo digits — bright glyphs survive, translucent HUD
    background does not, so recoil moving the scenery does not register."""
    bgr = frame['ammo']
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    _, b = cv2.threshold(g, AMMO_THRESH, 255, cv2.THRESH_BINARY)
    return b > 0


def sig_diff(a, b):
    return float(np.mean(a != b))


def fire_one_magazine(grabber, tracker, mouse):
    """Hold fire until the ammo counter stops moving, capturing throughout."""
    rec = MagazineRecorder(tracker)
    mouse.click(buttons=0x01, duration_ms=int(MAX_FIRE_S * 1000))

    t0 = time.perf_counter()
    prev = None
    last_change = t0
    n_ammo_steps = 0
    while True:
        now = time.perf_counter()
        el = now - t0
        if el > MAX_FIRE_S:
            break
        frame = grabber.grab()
        rec.push(now, frame)
        sig = ammo_sig(frame)
        if prev is not None and sig_diff(sig, prev) > AMMO_CHANGED:
            last_change = now
            n_ammo_steps += 1
        prev = sig
        if el > MIN_FIRE_S and (now - last_change) > EMPTY_STATIC_S:
            break

    mouse.click(buttons=0x00, duration_ms=0)     # release immediately
    return rec, time.perf_counter() - t0, n_ammo_steps


def wait_auto_reload(grabber, full_sig):
    """Wait out PUBG's automatic reload — no keypress needed.

    The game reloads by itself once the magazine empties, so R is never sent.
    It also drops out of ADS while doing so, which the caller has to undo:
    firing the next magazine from the hip would measure at the hip-fire K
    (0.50) while the analysis assumes the scoped one (1.55).
    """
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < RELOAD_TIMEOUT_S:
        frame = grabber.grab()
        if sig_diff(ammo_sig(frame), full_sig) < AMMO_CHANGED:
            time.sleep(SETTLE_AFTER_RELOAD_S)
            return time.perf_counter() - t0
    return None


def analyse(res, K, bullet_interval_s):
    """Frame-wise screen shift -> per-bullet residual in mouse counts."""
    dy = np.asarray(res.dy, dtype=float)
    ts = np.asarray(res.ts, dtype=float)
    if len(ts) == 0:
        return None
    ts = ts - ts[0]
    counts = dy / K                      # px -> counts
    n_bullets = int(ts[-1] / bullet_interval_s) + 1
    per_bullet = []
    for b in range(n_bullets):
        m = (ts >= b * bullet_interval_s) & (ts < (b + 1) * bullet_interval_s)
        per_bullet.append(float(np.nansum(counts[m])) if m.any() else 0.0)
    return {
        'n_frames': len(dy),
        'span_s': float(ts[-1]),
        'cum_px': float(np.nansum(dy)),
        'cum_counts': float(np.nansum(counts)),
        'per_bullet_counts': [round(v, 3) for v in per_bullet],
        'max_abs_frame_px': float(np.nanmax(np.abs(dy))) if len(dy) else 0.0,
        'n_rejected': int(np.sum(res.n_rejected)),
        'n_out_of_range': int(np.sum(res.out_of_range)),
        'n_low_gate': int(np.sum(res.gates)),
        'mean_mad': float(np.mean(res.mad)) if res.mad else float('nan'),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', required=True)
    ap.add_argument('--sight', default='red_dot',
                    choices=sorted(RECOIL_SIGHT_PROFILES.keys()))
    ap.add_argument('--mags', type=int, default=5)
    ap.add_argument('--countdown', type=int, default=6)
    ap.add_argument('--label', default='')
    ap.add_argument('--slot', type=int, default=1, choices=(1, 2),
                    help='which weapon slot is active (for attachment read)')
    ap.add_argument('--no-detect', action='store_true',
                    help='skip the Tab attachment read (pattern will assume '
                         'a bare gun, which over-compensates ~30%%)')
    args = ap.parse_args()

    prof = RECOIL_SIGHT_PROFILES.get(args.sight, {})
    K = prof.get('K', RECOIL_K_DEFAULT_SCOPED)
    tracker = ViewTracker(patch_xs=prof.get('patch_xs'))

    w = Weapon()
    w.set('name', args.weapon)
    w.set_seq()
    if not len(w.t_s):
        print(f"[!] no recoil pattern for '{args.weapon}'")
        return 1
    rpm = WEAPON_RPM.get(args.weapon, 600)

    print(f"weapon   : {args.weapon}  rpm={rpm}  "
          f"interval={w.bullet_interval_s*1000:.1f}ms  scale={w.scale}")
    print(f"sight    : {args.sight}  K={K:.4f}  "
          f"{len(tracker.xs)} patches at {tracker.xs}")
    print(f"mags     : {args.mags}   [SHADOW MODE — the curve is not updated]")

    mouse = get_mouse()
    regions = dict(tracker.regions())
    regions['ammo'] = HUD_REGIONS['ammo']
    if not args.no_detect:
        regions['type'] = HUD_REGIONS['type']
        for k, v in HUD_REGIONS.items():
            if k.startswith('att_'):
                regions[k] = v
    grabber, paced = make_grabber(regions)
    print(f"grabber  : {type(grabber).__name__} (paced={paced})")

    print("\n>>> Switch to the game NOW. Stand still, aim at texture, "
          "keep focus.")
    print("    It will fire and reload by itself. Do not touch the mouse.")
    for s in range(args.countdown, 0, -1):
        print(f"    starting in {s} ...", flush=True)
        time.sleep(1.0)

    if not game_focused():
        print("[!] ABORT: game is not focused.")
        grabber.close()
        return 1

    att = None
    if not args.no_detect:
        for _ in range(8):
            grabber.grab()
        att = detect_attachments(grabber, mouse, args.slot)
        if att is None:
            print("[!] could not read attachments (inventory did not open) — "
                  "aborting rather than\n    measuring against a bare-gun "
                  "pattern.")
            grabber.close()
            return 1
        print(f"\nattachments (slot {args.slot}):")
        for k, v in att.items():
            print(f"   {k:<10} {v if v else '(empty)'}")
        w.set('muzzle', att.get('muzzle', ''))
        w.set('grip', att.get('grip', ''))
        w.set_seq()

    print(f"\npattern  : {len(w.t_s)} pts over {w.t_s[-1]:.2f}s "
          f"({np.sum(w.dy_s):.0f} counts total)")

    # Compensation must be running, or the view climbs off the textured world.
    mouse.upload_pattern(w.dx_s, w.dy_s, w.t_s, w.bullet_interval_s)
    mouse.set_recoil_enabled(True)
    time.sleep(0.3)

    for _ in range(10):
        grabber.grab()
    full_sig = ammo_sig(grabber.grab())

    tag = args.label or f"{args.weapon}_{args.sight}_" \
                        f"{datetime.now().strftime('%m%d_%H%M')}"
    jl_path = os.path.join(HERE, f'autocal_{tag}.jsonl')
    jl = open(jl_path, 'w')
    jl.write(json.dumps({
        'type': 'header', 'weapon': args.weapon, 'sight': args.sight, 'K': K,
        'rpm': rpm, 'bullet_interval_s': w.bullet_interval_s,
        'scale': w.scale, 'patch_xs': list(tracker.xs),
        'patch': tracker.patch, 'band_y': tracker.band_y,
        'pattern_counts': float(np.sum(w.dy_s)),
    }) + '\n')

    mags = []
    try:
        for i in range(args.mags):
            if not game_focused():
                print(f"[!] lost focus before mag {i} — stopping.")
                break

            # The auto-reload from the previous magazine left us in hip fire.
            # Toggle ADS back on (the user's binding is toggle, not hold).
            if i > 0:
                mouse.click(buttons=0x02, duration_ms=60)
                time.sleep(ADS_SETTLE_S)

            rec, fire_s, steps = fire_one_magazine(grabber, tracker, mouse)
            # Zero ammo steps means the gun never fired — almost always the
            # reload animation still running. Recording it would feed a
            # magazine of pure noise into the residual statistics.
            if steps == 0:
                print(f"  mag {i}: DISCARDED — no rounds left the gun "
                      f"(reload not finished?), retrying after a pause")
                time.sleep(1.5)
                continue
            res = rec.finish()
            a = analyse(res, K, w.bullet_interval_s)
            if a is None:
                print(f"  mag {i}: no frames captured")
                continue
            a['mag'] = i
            a['fire_s'] = round(fire_s, 3)
            a['ammo_steps'] = steps
            a['n_dup'] = rec.n_duplicates
            a['fps'] = round(rec.effective_fps(), 1)
            mags.append(a)
            jl.write(json.dumps({'type': 'mag', **a,
                                 'dy_px': [None if not np.isfinite(v)
                                           else round(v, 3) for v in res.dy],
                                 'ts': [round(t - res.ts[0], 5)
                                        for t in res.ts]}) + '\n')
            jl.flush()

            print(f"  mag {i}: fire {fire_s:.2f}s  {steps} ammo steps  "
                  f"{a['n_frames']} frames @{a['fps']:.0f}fps  |  "
                  f"residual {a['cum_counts']:+8.1f} counts "
                  f"({a['cum_px']:+7.1f} px)  max {a['max_abs_frame_px']:.1f}px"
                  f"  rej={a['n_rejected']} oor={a['n_out_of_range']}")

            rl = wait_auto_reload(grabber, full_sig)
            if rl is None:
                print("  [!] auto-reload did not complete — stopping.")
                break
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        mouse.click(buttons=0x00, duration_ms=0)
        grabber.close()
        jl.close()

    if not mags:
        print("no magazines recorded")
        return 1

    print("\n" + "=" * 72)
    print(f"{len(mags)} magazines, residual in mouse counts "
          f"(pattern total {np.sum(w.dy_s):.0f})")
    cc = np.array([m['cum_counts'] for m in mags])
    print(f"  per-mag residual : {cc.mean():+8.1f} +- {cc.std():.1f} counts")
    print(f"  as % of pattern  : "
          f"{100*cc.mean()/np.sum(w.dy_s):+.2f}% +- "
          f"{100*cc.std()/np.sum(w.dy_s):.2f}%")
    print(f"  spread mag-to-mag: {cc.max()-cc.min():.1f} counts")
    print(f"\n  sign: positive = under-compensated (view drifts UP)")
    print(f"        negative = over-compensated (view drifts DOWN)")
    print(f"\n  frames/mag {np.mean([m['n_frames'] for m in mags]):.0f}   "
          f"rejected {np.sum([m['n_rejected'] for m in mags])}   "
          f"out-of-range {np.sum([m['n_out_of_range'] for m in mags])}   "
          f"low-gate {np.sum([m['n_low_gate'] for m in mags])}")
    print(f"\n  raw -> {jl_path}")

    try:
        plot(mags, w, K, tag)
    except Exception as e:
        print(f"  (plot skipped: {e})")
    return 0


def plot(mags, w, K, tag):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(16, 4.6))

    # Bullet counts differ between magazines (capture spans vary), and the
    # pattern has its own length — clip everything to the shortest.
    bi = w.bullet_interval_s
    t = np.array(w.t_s)
    dy_pat = np.array(w.dy_s)
    n_pat = int(t[-1] / bi) + 1
    cur = [dy_pat[(t >= b*bi) & (t < (b+1)*bi)].sum() for b in range(n_pat)]

    allb = [m['per_bullet_counts'] for m in mags]
    n = min(min(len(b) for b in allb), len(cur))
    med = np.median([b[:n] for b in allb], axis=0)
    cur = np.array(cur[:n])

    ax = axes[0]
    for m in mags:
        ax.plot(m['per_bullet_counts'][:n], lw=0.9, alpha=0.7)
    ax.plot(med, 'k-', lw=2, label='median')
    ax.axhline(0, color='gray', lw=0.8)
    ax.set_xlabel('bullet')
    ax.set_ylabel('residual (counts)')
    ax.set_title('per-bullet residual\n(+ = under-compensated)')
    ax.legend()
    ax.grid(alpha=0.3)

    ax = axes[1]
    ax.plot(cur, label='current curve', lw=1.6)
    ax.plot(cur + med, '--', lw=1.6,
            label='curve + residual (proposed)')
    ax.set_xlabel('bullet')
    ax.set_ylabel('counts')
    ax.set_title('what the residual implies')
    ax.legend()
    ax.grid(alpha=0.3)

    ax = axes[2]
    cc = [m['cum_counts'] for m in mags]
    ax.plot(cc, 'o-')
    ax.axhline(0, color='gray', lw=0.8)
    ax.axhline(np.mean(cc), color='r', ls='--', lw=1,
               label=f'mean {np.mean(cc):+.1f}')
    ax.set_xlabel('magazine')
    ax.set_ylabel('cumulative residual (counts)')
    ax.set_title('magazine-to-magazine repeatability')
    ax.legend()
    ax.grid(alpha=0.3)

    plt.tight_layout()
    p = os.path.join(HERE, f'autocal_{tag}.png')
    plt.savefig(p, dpi=100)
    print(f"  plot -> {p}")


if __name__ == '__main__':
    sys.exit(main())
