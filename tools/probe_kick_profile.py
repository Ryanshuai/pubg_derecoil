"""What the view does WITHIN one bullet, not just where it ends up.

    pixi run python tools/probe_kick_profile.py --weapon aug --mags 1

Fires a magazine with the compensation switched OFF and records the raw view
displacement every frame, then folds all the rounds of the magazine onto one
bullet interval. The result is the kick's shape: 10 samples across a bullet at
120 fps against an 88 ms interval.

Why this is not the same measurement harvest already makes. harvest bins the
displacement per bullet and fits the SUM of each bin. That is enough to cancel
where the view ends up, and it says nothing at all about the path it took to
get there. If the game punches the view up 20 counts in 20 ms and lets it
settle back to 3 over the rest of the interval, a curve carrying "3" for that
bullet is CORRECT by the residual and still leaves a visible 20-count jump on
screen -- and the residual will read near zero, because the punch cancelled
itself before the bin closed.

That is exactly the complaint that prompted this: the AUG's first bullet
carries 0.7 counts of compensation, its measured residual is +0.4, and the
first shot still visibly jumps.

Two things come out of it:

  1. Whether the opening rounds really are as gentle as the per-bullet sums
     say, or whether the view moves inside the bin and comes back.
  2. The window the firmware should spread each bullet's compensation over.
     press/pico_mouse.py currently spreads it uniformly across the whole
     bullet interval, which was a guess -- if the kick is over in 30 ms, the
     other 58 ms of push is fighting nothing.

Compensation is disabled for the duration and switched back on at the end.
Nothing is written to any curve.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))

import numpy as np

from detector.weapon import Weapon, WEAPON_RPM
from control.focus import ensure_focus

from analysis import fit_interval
from sweep import Rig

OUT_DIR = os.path.join(ROOT, 'docs', 'kick')


def per_round(ts, counts, shots, interval, n_bins=12):
    """[(round_index, profile)] for every round that stayed trackable.

    Kept separate from the average because the average cannot answer the
    question that matters: is the FIRST round gentle, or does the curve merely
    believe it is? With the compensation off the view climbs out of the
    trackable band within a few rounds, so the rounds that survive are the
    opening ones -- which is exactly the interesting end, but it means an
    average over "whatever survived" would be an average over an unknown
    subset. Round numbers make the subset explicit.
    """
    edges = np.linspace(0.0, interval, n_bins + 1)
    good = ~np.isnan(counts)
    cum = np.cumsum(np.where(good, counts, 0.0))
    tt, cc = ts[good], cum[good]
    out = []
    if len(tt) < 3:
        return out
    for idx, t0 in shots:
        if t0 < tt[0] or t0 + interval > tt[-1]:
            continue
        out.append((idx, np.diff(np.interp(t0 + edges, tt, cc))))
    return out


def fold(ts, counts, shot_ts, interval, n_bins=12):
    """Average the per-frame displacement across every bullet of a magazine.

    Each round contributes its own interval, resampled onto a common grid, so
    the answer is the shape of ONE bullet with the magazine's worth of
    averaging behind it. Rounds are located by the ammo counter, not by
    assuming they are evenly spaced -- an interval fitted from the same counter
    is used only to size the window.
    """
    edges = np.linspace(0.0, interval, n_bins + 1)
    acc = np.zeros(n_bins)
    hits = np.zeros(n_bins)
    good = ~np.isnan(counts)
    cum = np.cumsum(np.where(good, counts, 0.0))
    tt, cc = ts[good], cum[good]
    if len(tt) < 3:
        return None, 0
    used = 0
    for t0 in shot_ts:
        if t0 + interval > tt[-1] or t0 < tt[0]:
            continue
        at = np.interp(t0 + edges, tt, cc)
        acc += np.diff(at)
        hits += 1
        used += 1
    if not used:
        return None, 0
    return acc / np.maximum(hits, 1), used


def shot_times(trace, t_origin):
    """[(round_index, t)] — when each round left, and which round it was.

    The counter states both. Round k is the transition to (mag_size - k - 1)
    rounds remaining, so the highest count seen is the full magazine and the
    index follows from it. No assumption that the rounds are evenly spaced,
    and no counting of frames.
    """
    first = {}
    for t, n in trace:
        if n not in first or t < first[n]:
            first[n] = t
    if not first:
        return []
    full = max(first)
    return sorted(((full - n, first[n] - t_origin) for n in first if n < full),
                  key=lambda r: r[1])


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapon', required=True,
                    help='what is in hand — only used for its nominal interval')
    ap.add_argument('--mags', type=int, default=1)
    ap.add_argument('--bins', type=int, default=12,
                    help='samples across one bullet interval')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--countdown', type=int, default=8)
    ap.add_argument('--posture', default='standing')
    ap.add_argument('--no-comp', action='store_true',
                    help='fire with the compensation disabled. Off by default, '
                         'and it should stay off: an uncompensated AUG lifts '
                         'the view ~20 counts a round, so it leaves the '
                         'trackable band inside five rounds and every round '
                         'after that is measured against sky. The first '
                         'attempt at this reported rounds 10-36 moving -0.9 '
                         'counts, which is not gentle recoil, it is no texture.')
    ap.add_argument('--aim-down', type=int, default=0,
                    help='counts to push the aim below level before firing, so '
                         'the climb stays on terrain instead of sky')
    args = ap.parse_args()

    print(f">>> Hold the {args.weapon}, face texture, and let it aim itself.")
    print(">>> Compensation is "
          + ("OFF — the view will climb, expect only the opening rounds to "
             "stay trackable" if args.no_comp else
             "ON — the view holds, so every round stays measurable"))
    if not ensure_focus(countdown_s=args.countdown, label='the kick probe'):
        print('[!] could not focus the game')
        return 1

    rig = Rig(args.sight)
    rows = []
    try:
        if args.no_comp:
            rig.mouse.set_recoil_enabled(False)
        else:
            w = Weapon()
            w.set('name', args.weapon)
            w.set('posture', args.posture)
            w.set_seq()
            rig.mouse.upload_pattern(w.dx_s, w.dy_s, w.t_s, w.bullet_interval_s)
            rig.mouse.set_recoil_enabled(True)
            time.sleep(0.3)
        if not rig.ensure_posture(args.posture):
            print(f'[!] could not reach {args.posture}')
            return 1
        if not rig.ensure_ads():
            print('[!] could not enter ADS')
            return 1
        nominal = 60.0 / WEAPON_RPM.get(args.weapon, 600)
        for i in range(args.mags):
            if i:
                if not rig.ensure_ads():
                    print('[!] lost ADS between magazines')
                    break
            if args.aim_down:
                # Start low so the climb lands on terrain. Recoil only pushes
                # up, and there is no texture in the sky -- a burst that ends
                # above the horizon is not a gentle burst, it is an unmeasured
                # one, and it reads as gentle.
                rig.mouse.move(0, args.aim_down)
                time.sleep(0.25)
                rig.flush(4)
            rec, fire_s, steps, fire_end, first_shot, ads = rig.fire_magazine()
            if steps == 0:
                print(f'  mag {i}: nothing fired')
                continue
            res = rec.finish()
            trace = getattr(rec, 'ammo_trace', [])
            iv, n_iv, resid = fit_interval(trace)
            interval = iv or nominal
            ts = np.asarray(res.ts, dtype=float)
            dy = np.asarray(res.dy, dtype=float)
            oor = np.asarray(res.out_of_range, dtype=bool)
            if len(oor) != len(dy):
                oor = np.zeros(len(dy), dtype=bool)
            counts = np.where(oor, np.nan, dy / rig.K)
            origin = first_shot if first_shot else ts[0]
            ts = ts - origin
            shots = shot_times(trace, origin)
            each = per_round(ts, counts, shots, interval, args.bins)
            if not each:
                print(f'  mag {i}: nothing trackable — {len(trace)} ammo '
                      f'reads, {len(shots)} shot times')
                continue
            prof, used = fold(ts, counts, [t for _, t in shots], interval,
                              args.bins)
            idxs = [k for k, _ in each]
            print(f'  mag {i}: {len(each)} rounds trackable (rounds '
                  f'{min(idxs)}..{max(idxs)}), {len(trace)} ammo reads, '
                  f'ads {ads:.0%}, interval {1000*interval:.1f} ms'
                  + (f' (fitted, {n_iv} rounds +-{resid:.1f} ms)' if iv else
                     ' (NOMINAL — the counter would not fit)'))
            rows.append({'mag': i, 'interval_s': interval, 'rounds': used,
                         'ads_frac': ads, 'bins': args.bins,
                         'profile_counts': [float(v) for v in prof],
                         'per_round': [[int(k), [float(v) for v in p]]
                                       for k, p in each],
                         'ammo_reads': len(trace),
                         'fitted': bool(iv), 'fit_rounds': n_iv})
            rig.wait_reload()
    finally:
        rig.mouse.set_recoil_enabled(True)
        rig.close()

    if not rows:
        print('[!] nothing measured')
        return 1

    prof = np.mean([r['profile_counts'] for r in rows], axis=0)
    interval = float(np.mean([r['interval_s'] for r in rows]))
    step = 1000.0 * interval / args.bins
    total = float(prof.sum())
    cum = np.cumsum(prof)
    peak = int(np.argmax(cum))
    print(f'\nKICK PROFILE — {args.weapon}, {sum(r["rounds"] for r in rows)} '
          f'rounds over {len(rows)} magazine(s), compensation off')
    print(f'{"t (ms)":>9}{"counts":>9}{"cumulative":>12}')
    for i, (v, c) in enumerate(zip(prof, cum)):
        bar = '#' * int(round(24 * max(0.0, c) / max(1e-6, max(cum))))
        print(f'{i*step:>6.0f}-{(i+1)*step:>3.0f}{v:>9.2f}{c:>12.2f}  {bar}')
    print(f'\n  one bullet moves the view {total:.1f} counts')
    print(f'  it gets there by bin {peak} of {args.bins} '
          f'({(peak+1)*step:.0f} ms of the {1000*interval:.0f} ms interval)')
    over = float(max(cum)) - total
    if over > 0.15 * abs(total):
        print(f'  and OVERSHOOTS by {over:.1f} counts before settling — the '
              f'view is punched\n  further than it ends up, which a per-bullet '
              f'sum cannot see and\n  a uniform spread cannot cancel')
    else:
        print('  no overshoot worth the name — the kick is monotone inside the '
              'interval,\n  so a uniform spread is not leaving a visible jump')

    # The question this probe exists for: what does round 1 actually do? The
    # curve carries 0.7 counts for it. Per round, pooled across magazines.
    by_round = {}
    for r in rows:
        for k, p in r['per_round']:
            by_round.setdefault(k, []).append(np.array(p))
    if by_round:
        print(f'\nPER ROUND — total counts the view moved during that round\'s '
              f'own interval')
        try:
            w = Weapon()
            w.set('name', args.weapon)
            w.set('posture', args.posture)
            w.set_seq()
            comp, _ = w.comp_bins(w.curve_bullets())
        except Exception:                                  # noqa: BLE001
            comp = []
        print(f'{"round":>7}{"n":>4}{"measured":>10}{"sd":>7}{"curve says":>12}')
        for k in sorted(by_round):
            v = np.array([p.sum() for p in by_round[k]])
            c = f'{comp[k]:.1f}' if k < len(comp) else '-'
            print(f'{k:>7}{len(v):>4}{v.mean():>10.1f}'
                  f'{(v.std(ddof=1) if len(v) > 1 else 0.0):>7.1f}{c:>12}')
        print('\n  "curve says" is what the compensation would have pushed for '
              'that round.\n  A gap here is the answer to "the first shot is '
              'not compensated" — the\n  residual cannot show it, because the '
              'residual is measured on the same\n  bins the curve was fitted '
              'to.')

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR,
                        f'{args.weapon}_{datetime.now():%m%d_%H%M}.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump({'weapon': args.weapon, 'sight': args.sight,
                   'posture': args.posture, 'K': rig.K,
                   'interval_s': interval, 'mags': rows}, fh, indent=2)
    print(f'\n  raw -> {os.path.relpath(path, ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
