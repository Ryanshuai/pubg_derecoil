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

WHAT THIS IS, next to sweep.py: one cell. One weapon, one posture, no weapon
switcher and no factorial — a human puts the gun in their hands and this
measures it. That is the only difference worth having, so the game is driven
through the same sweep.Rig the sweep uses. This file used to carry its own
firing loop, its own reload wait, its own Tab toggle and its own ADS click,
all of them parallel to control/fire.py and control/gun.py, and all of them
drifting away from the versions that had the fixes. See the note above the
analyse import for what the convergence did to the numbers.
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

from config import RECOIL_SIGHT_PROFILES
from detector.cropper import FocusLost
from detector.weapon import Weapon, WEAPON_RPM
from control.focus import game_focused

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# This file used to carry its own analyse(), a strictly worse one: it booked
# the player's own mouse motion as recoil, kept frames the correlator had
# flagged as wrapped, started the bins at the first frame captured rather than
# the first round fired, and rounded each frame pair into whichever bullet its
# timestamp landed in. Numbers from before 2026-08-02 were produced by that
# version and are not comparable with anything printed after it.
#
# 2026-08-03 moves them AGAIN, and for the same kind of reason. The burst is
# now fired by control/fire.py, which reports when the first round left the gun
# and when the last one did; both go to analyse(). So the bins now start at the
# first SHOT rather than at the first frame captured — between the click going
# out over USB and that round's recoil reaching a grabbed frame there are 20 to
# 50 ms against an 88 ms bullet interval — and the recording is cut at the last
# round instead of running on through the camera drifting back toward the aim.
# The fire loop also takes three baseline frames BEFORE the trigger, so bullet
# 0 has somewhere to be measured from. Every one of those is a fix, and every
# one of them shifts the residual: do not put a run from before 2026-08-03 in
# the same table as one from after it.
from analysis import analyse, ADS_FRAC_MIN
# The rig: capture, Pico, detectors, and the three closed loops assembled over
# them. Building that object graph by hand here is exactly how the parallel
# drivers this file used to carry came to be written, so it is imported rather
# than re-made. harvest.py reaches for it the same way.
from sweep import Rig

cv2.setNumThreads(1)
HERE = os.path.dirname(os.path.abspath(__file__))
# Runs are measurements, not source: they land under docs/ with the rest of
# what this repo has measured, never next to the script that wrote them.
RUNS = os.path.join(os.path.dirname(HERE), 'docs', 'recoil', 'runs')

# game_focused comes from control/focus.py. The copy that used to live here
# matched the window TITLE against ('battlegrounds', 'pubg', 'tslgame') -- and
# this repository is called pubg_derecoil, so an editor or terminal showing the
# path matched 'pubg' and the run believed the game had focus while every
# keypress went into the editor. control.focus matches the EXE and nothing else.


def read_attachments(rig, weapon, slot):
    """What is on the gun, and whether it is the gun that was asked for.

    -> ({slot: template name}, error string). Exactly one of the two is None.

    Skipping this is what produced a 30% over-compensation on the first run:
    weapon_scales.json is calibrated WITH compensator+grip, so a Weapon left
    at default attachments gets its scale divided back out to bare-gun level
    and then never multiplied down again.

    The Tab cycle itself is GunDriver.read_loadout — the same one the sweep and
    the harvest read their loadouts through. This file used to own that loop:
    press Tab, poll the 'Type' header, read, press Tab again. The judgement had
    already converged on TabTypeDetector; the LOOP had not, and a toggle driven
    from two places fails silently, because a swallowed keypress reads exactly
    like a bare gun.

    KNOWN COST OF THE SWAP, so nobody rediscovers it as a bug: read_loadout
    forces a close/open cycle and sleeps TAB_CLOSE_S + TAB_OPEN_S through it,
    where the loop here polled (measured 2026-08-02: the screen is up 28-38 ms
    after the key and gone 77-128 ms after it, tools/probe_toggle_latency.py).
    So this is roughly a second slower and presses Tab once more than it needs
    to. It happens ONCE per run, before anything is fired, and buying it back
    means fixing GunDriver — where every caller gets the fix — rather than
    keeping a faster copy here.

    The name plate comes back from the same frame as the slots, at no extra
    cost, so the weapon named on the command line is checked rather than
    trusted. --weapon is what picks the curve, and measuring an M416's residual
    against the AUG's curve is not a noisy measurement, it is a wrong one.
    """
    gun_seen, att = rig.read_loadout(slot=slot)
    if gun_seen is None:
        return None, ('the inventory would not open, so the attachments are '
                      'unknown')
    if gun_seen and gun_seen != weapon:
        return None, (f'slot {slot} holds {gun_seen!r}, not {weapon!r} — the '
                      f'curve under test belongs to a different gun')
    if att is None:
        return None, f'the inventory opened but slot {slot} read as nothing'
    return att, None


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

    w = Weapon()
    w.set('name', args.weapon)
    w.set_seq()
    if not len(w.t_s):
        print(f"[!] no recoil pattern for '{args.weapon}'")
        return 1
    rpm = WEAPON_RPM.get(args.weapon, 600)

    # Opens the capture and the Pico, so it comes after the cheap checks.
    rig = Rig(args.sight)

    print(f"weapon   : {args.weapon}  rpm={rpm}  "
          f"interval={w.bullet_interval_s*1000:.1f}ms  scale={w.scale}")
    print(f"sight    : {args.sight}  K={rig.K:.4f}  "
          f"{len(rig.tracker.xs)} patches at {rig.tracker.xs}")
    print(f"mags     : {args.mags}   [SHADOW MODE — the curve is not updated]")
    print(f"grabber  : {type(rig.grabber).__name__} (paced={rig.paced})")

    print("\n>>> Switch to the game NOW. Stand still, aim at texture, "
          "keep focus.")
    print("    It will fire and reload by itself. Do not touch the mouse.")
    for s in range(args.countdown, 0, -1):
        print(f"    starting in {s} ...", flush=True)
        time.sleep(1.0)

    if not game_focused():
        print("[!] ABORT: game is not focused.")
        rig.close()
        return 1

    att = None
    if not args.no_detect:
        try:
            rig.flush(8)
            att, err = read_attachments(rig, args.weapon, args.slot)
        except FocusLost:
            # grab() raises rather than handing back the frozen picture PUBG
            # leaves behind, so this is reachable between the check above and
            # the Tab cycle below.
            att, err = None, 'the game left the foreground during the Tab read'
        if err:
            print(f"[!] ABORT: {err}.\n    Refusing to measure — the pattern "
                  f"would be the wrong one.")
            rig.close()
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
    # Everything from here to the finally is inside the try, because the Pico
    # is now injecting: an exception escaping between these two lines and
    # rig.close() would leave the firmware pulling the mouse down after this
    # process is gone, on a machine two other agents share.
    tag = args.label or f"{args.weapon}_{args.sight}_" \
                        f"{datetime.now().strftime('%m%d_%H%M')}"
    jl_path = os.path.join(RUNS, f'autocal_{tag}.jsonl')
    jl = None
    mags = []
    n_hip = 0
    try:
        rig.mouse.upload_pattern(w.dx_s, w.dy_s, w.t_s, w.bullet_interval_s)
        rig.mouse.set_recoil_enabled(True)
        time.sleep(0.3)

        # In ADS before the FIRST magazine, not only after a reload. This used
        # to be asked of the human ("aim at texture") and never checked, and
        # reading the Tab screen just above drops out of ADS anyway — so the
        # opening magazine of an attachment-detecting run was fired from the
        # hip by construction, then analysed with the scoped K of 1.55 against
        # the hip's 0.50 and reported about three times high.
        if not rig.ensure_ads():
            print("[!] ABORT: could not confirm ADS (the crosshair is still "
                  "drawn). Nothing fired.")
            return 1
        rig.flush(10)

        os.makedirs(RUNS, exist_ok=True)
        jl = open(jl_path, 'w')
        jl.write(json.dumps({
            'type': 'header', 'weapon': args.weapon, 'sight': args.sight,
            'K': rig.K, 'rpm': rpm, 'bullet_interval_s': w.bullet_interval_s,
            'scale': w.scale, 'patch_xs': list(rig.tracker.xs),
            'patch': rig.tracker.patch, 'band_y': rig.tracker.band_y,
            'pattern_counts': float(np.sum(w.dy_s)), 'attachments': att,
        }) + '\n')

        for i in range(args.mags):
            if not game_focused():
                print(f"[!] lost focus before mag {i} — stopping.")
                break

            # The auto-reload from the previous magazine left us in hip fire.
            # ensure_ads WATCHES the toggle to completion instead of clicking
            # and sleeping: right click is a toggle, so a blind click lands in
            # the wrong state half the time, and clicking again while the
            # scope-in animation is still playing just toggles back out.
            if i > 0 and not rig.ensure_ads():
                print("  [!] could not re-enter ADS after the reload — "
                      "stopping rather than measuring hip fire.")
                break

            try:
                (rec, fire_s, steps, fire_end, first_shot,
                 ads_frac) = rig.fire_magazine()
            except FocusLost:
                # ScreenBuffer raises the moment the game leaves the
                # foreground, because the frames after that are the frozen
                # picture PUBG leaves on screen — which measures as a
                # suspiciously clean residual rather than as a failure. This
                # tool is human-supervised and its whole contract is "keep
                # focus", so it stops rather than trying to take it back.
                print("  [!] lost the foreground mid-magazine — stopping.")
                break

            # Zero ammo steps means the gun never fired — almost always the
            # reload animation still running. Recording it would feed a
            # magazine of pure noise into the residual statistics.
            if steps == 0:
                print(f"  mag {i}: DISCARDED — no rounds left the gun "
                      f"(reload not finished?), retrying after a pause")
                time.sleep(1.5)
                continue
            res = rec.finish()
            a = analyse(res, rig.K, w.bullet_interval_s, fire_end,
                        first_shot_ts=first_shot)
            if a is None:
                print(f"  mag {i}: no frames captured")
                continue
            a['mag'] = i
            a['fire_s'] = round(fire_s, 3)
            a['ammo_steps'] = steps
            a['n_dup'] = rec.n_duplicates
            a['fps'] = round(rec.effective_fps(), 1)
            # Whether the shots were AIMED, polled all the way through the
            # burst rather than assumed from the click that preceded it. PUBG
            # drops ADS on a reload, on a posture change and on being shot at,
            # and a magazine analysed at the scoped K when it was fired at the
            # hip's reads about three times high — a confident wrong number,
            # not a noisy one. The two signals are logged apart because they
            # fail for opposite reasons; see GunDriver.in_ads.
            a['ads_frac'] = ads_frac
            a['ads_icon_frac'] = round(getattr(rec, 'ads_icon_frac',
                                               float('nan')), 3)
            a['ads_cross_frac'] = round(getattr(rec, 'ads_cross_frac',
                                                float('nan')), 3)
            # NaN-safe: an unpolled magazine (no ADS reads at all) is not
            # evidence of hip fire, so it is not treated as such.
            aimed = not (ads_frac == ads_frac and ads_frac < ADS_FRAC_MIN)
            # Written either way — the point of a diagnostic run is that the
            # bad magazines are inspectable — but under a type that readers
            # filtering on 'mag' will not average in. temp_debug/plot_autocal.py
            # is one of those readers.
            jl.write(json.dumps({'type': 'mag' if aimed else 'mag_discarded',
                                 **a,
                                 'dy_px': [None if not np.isfinite(v)
                                           else round(v, 3) for v in res.dy],
                                 'ts': [round(t - res.ts[0], 5)
                                        for t in res.ts]}) + '\n')
            jl.flush()

            print(f"  mag {i}: fire {fire_s:.2f}s  {steps} ammo steps  "
                  f"{a['n_frames']} frames @{a['fps']:.0f}fps  "
                  f"ads {100*ads_frac:.0f}%  |  "
                  f"residual {a['cum_counts']:+8.1f} counts "
                  f"({a['cum_px']:+7.1f} px)  max {a['max_abs_frame_px']:.1f}px"
                  f"  rej={a['n_rejected']} oor={a['n_out_of_range']}")
            if aimed:
                mags.append(a)
            else:
                n_hip += 1
                print(f"       [!] DISCARDED — only {100*ads_frac:.0f}% of the "
                      f"polled frames were aimed (gate {100*ADS_FRAC_MIN:.0f}%)"
                      f"; crosshair {a['ads_cross_frac']:.0%} decided, posture "
                      f"icon said {a['ads_icon_frac']:.0%}")

            # Watches the counter move and then hold still, rather than waiting
            # for it to match a snapshot taken at the start of the run. The
            # snapshot went stale: one standing cell lost four of its five
            # magazines twice running to a reference that never came back.
            if rig.wait_reload() is None:
                print("  [!] auto-reload did not complete — stopping.")
                break
    except KeyboardInterrupt:
        print("\ninterrupted")
    except FocusLost:
        # Everything that reads the screen raises this now, not just the fire
        # loop: the reload wait and ensure_ads grab too. Caught rather than
        # allowed to escape, so the finally below still turns the compensation
        # off — a traceback out of here used to be survivable and no longer is.
        print("\n[!] the game left the foreground — stopping.")
    finally:
        # Releases the trigger and stops the compensation as well as closing
        # the capture.
        rig.close()
        if jl is not None:
            jl.close()

    if n_hip:
        print(f"\n[!] {n_hip} magazine(s) were not aimed and are excluded from "
              f"the summary. They are in the JSONL as 'mag_discarded'.")
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
        plot(mags, w, rig.K, tag)
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
    p = os.path.join(RUNS, f'autocal_{tag}.png')
    plt.savefig(p, dpi=100)
    print(f"  plot -> {p}")


if __name__ == '__main__':
    sys.exit(main())
