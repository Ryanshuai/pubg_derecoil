"""Re-read every stored K calibration. K has never been cleanly measured.

    pixi run k-audit

Offline, and it changed its own mind twice on the way. Opened because two
things said K was about 5% high:

  the scale sweep   the F-coefficient slope is -1/(1+delta) and reads -0.948
  the two arms      y_true from compensation-OFF magazines is 6.4% BELOW the
                    same cell measured compensation-ON, 5.3 sigma

A K error is nearly invisible everywhere it is usually looked at -- the cube is
ratios and ratios cancel it, and a compensated magazine's y_obs is a handful of
counts against a curve of 950. It bites in exactly one place, and that place
matters: the FIRST curve of a gun that has none yet can only be fitted from
compensation-OFF magazines, where y_obs IS the whole measurement.

WHAT THE STORED DATA TURNED OUT TO BE. 98 rows over four ADS runs. The rows
carry `per_patch_cum` -- what each tracked patch reported for the same injected
move -- and `cum_px` folds them together, so a row where one patch has lost the
view arrives with a plausible K and nothing saying otherwise. Splitting them:

    patch spread    p10 0.42%   p50 63.6%   p90 155%
    22 rows under 2%, 23 under 5%          <- a gap, not a threshold I picked
    the outlier is patch 0 in 43 of the 76 bad rows

⚠ AND THE 22 SURVIVORS STILL RANGED 0.39 .. 1.59, WITH THE PATCHES AGREEING TO
0.3%. Every patch reporting the same quarter-size answer is not a tracking
failure. The mechanism is in two rows of one run:

    -240 rep 1   cum_px 382.77   K 1.5941   spread 0.1%
    -240 rep 2   cum_px 126.76   K 0.5284   spread 0.3%
                 difference      256.01

RECOIL_PATCH_H is 256. THE CORRELATOR ALIASES BY EXACTLY ONE PATCH HEIGHT when
an inter-frame gap exceeds its unambiguous range, and every patch aliases the
same way -- so agreement is preserved and the total comes up 256 px short.

⚠ `max_abs_frame` CANNOT SEE THIS, which is why the runs looked fine. An
aliased pair REPORTS A SMALL DISPLACEMENT; the statistic that would catch a
too-fast frame is the one the aliasing hides in. Agreeing rows and disagreeing
rows have the same median max_abs_frame (91 vs 92 px).

De-aliased -- solving for the single K that puts every clean row on an integer
multiple of 256 away from its observation, WITHOUT assuming K:

    -240 (up)    n= 3   1.5950 +- 0.0005   [1.5941 .. 1.5959]
    +120 (down)  n= 9   1.4803 +- 0.0310
    +240 (down)  n=10   1.5182 +- 0.0387
    up/down asymmetry +6.32%   (control/aim.py measured 5.37% independently)

⚠ AND THE CONSTANT IN USE, 1.5474, IS A BLEND OF TWO DIRECTIONS THAT DIFFER BY
6.3%, WHILE THE RECOIL ONLY EVER GOES ONE WAY. Which one is right is not a
matter of taste: the curve is a schedule of DOWNWARD counts, so converting
screen pixels into "counts of compensation owed" is K_down. That is 1.5002, and
1.5474 is 3.15% above it -- about 1.9 sigma, and the right sign to account for
roughly half of the two arms' 6.4%.

⚠ SO THE HYPOTHESIS THIS FILE OPENED WITH WAS BACKWARDS FOR THE UP DIRECTION.
K_up is 1.5950 and the constant is 3.0% BELOW it; had the recoil's own
direction been the one that mattered, correcting K would have made the two-arm
gap WORSE, not better. Recorded because the sign was decided by the physics
(what the curve commands), not by which way made the discrepancy shrink.

WHAT A CLEAN MEASUREMENT NEEDS, all three, none of which the stored runs did:
  - inject slowly enough that no inter-frame gap approaches 128 px. At
    INJECT_S = 0.15 and 480 counts that is ~50 px/frame NOMINAL and the
    observed peaks are 124. One second per injection puts it under 2.
  - median across patches, and reject the row when they disagree
  - report the two directions SEPARATELY and never pool them
"""
import argparse
import collections
import glob
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SPREAD_TOL = 0.02      # patches must agree to 2% of their median; see the gap
K_DIR = os.path.join(ROOT, 'calibration', 'artifacts', 'k')


def clean_rows(sight):
    """[(counts, |median px|, spread)] over every ADS run for this sight."""
    out = []
    for p in sorted(glob.glob(os.path.join(K_DIR, f'calib_k_{sight}*.json'))):
        d = json.load(open(p, encoding='utf-8'))
        if not d.get('args', {}).get('ads'):
            continue
        for r in d.get('rows', []):
            pp = r.get('per_patch_cum') or []
            if len(pp) < 3 or not r.get('counts'):
                continue
            a = np.asarray(pp, dtype=float)
            med = float(np.median(a))
            if med == 0:
                continue
            out.append((r['counts'], abs(med),
                        float(np.max(np.abs(a - med))) / abs(med),
                        os.path.basename(p)))
    return out


def solve_alias(rows, alias):
    """The K that explains every row as observation + alias*k, k a
    non-negative integer. Grid search, because the objective is piecewise and
    a gradient would walk into whichever branch it started in."""
    best = None
    for K in np.arange(1.10, 2.20, 0.0005):
        err = 0.0
        for cnt, m, _, _ in rows:
            want = abs(cnt) * K
            k = max(0, round((want - m) / alias))
            err += (m + alias * k - want) ** 2
        if best is None or err < best[1]:
            best = (float(K), err)
    return best[0]


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sight', default='red_dot')
    a = ap.parse_args()

    from config import RECOIL_SIGHT_PROFILES, RECOIL_PATCH_H
    in_use = RECOIL_SIGHT_PROFILES.get(a.sight, {}).get('K')
    alias = float(RECOIL_PATCH_H)

    rows = clean_rows(a.sight)
    if not rows:
        print(f'[!] no stored ADS calibration for {a.sight}')
        return 1
    good = [r for r in rows if r[2] < SPREAD_TOL]
    bad = [r for r in rows if r[2] >= SPREAD_TOL]
    print(f'{a.sight}: {len(rows)} row(s), {len(good)} with patches agreeing to '
          f'{100*SPREAD_TOL:g}%, {len(bad)} rejected')
    if bad:
        sp = np.array([r[2] for r in bad])
        print(f'   rejected rows spread {100*np.median(sp):.0f}% (median) — '
              f'a gap, not a borderline')
    if not good:
        print('[!] nothing survives; cannot measure')
        return 1

    K0 = solve_alias(good, alias)
    print(f'   alias {alias:g} px (RECOIL_PATCH_H); the K that puts every clean '
          f'row on a multiple of it: {K0:.4f}')

    by = collections.defaultdict(list)
    n_aliased = 0
    for cnt, m, _, _ in good:
        k = max(0, round((abs(cnt) * K0 - m) / alias))
        n_aliased += (k > 0)
        by[cnt].append((m + alias * k) / abs(cnt))
    print(f'   {n_aliased} of {len(good)} clean rows were ALIASED and are '
          f'un-aliased below')
    print()

    up, down = [], []
    for cnt in sorted(by):
        v = np.array(by[cnt])
        (up if cnt < 0 else down).extend(v)
        sem = v.std(ddof=1) / len(v) ** 0.5 if len(v) > 1 else float('nan')
        print(f'  {cnt:+5d} ({"up  " if cnt < 0 else "down"})  n={len(v):2d}   '
              f'K = {v.mean():.4f} +- {sem:.4f}   '
              f'[{v.min():.4f} .. {v.max():.4f}]')

    up, down = np.array(up), np.array(down)
    print()
    if len(up) and len(down):
        print(f'up   {up.mean():.4f}   down {down.mean():.4f}   '
              f'asymmetry {100*(up.mean()/down.mean() - 1):+.2f}%')
    # ⚠ DOWN IS THE ONE, and the reason is the model, not the numbers. y_obs is
    # converted into "counts of compensation owed", the curve is a schedule of
    # DOWNWARD counts, and the firmware plays it downward. Picking `up` because
    # the recoil goes up would convert a displacement into the units of the
    # thing that is not being commanded.
    if len(down) and in_use:
        sem = down.std(ddof=1) / len(down) ** 0.5
        bias = 100 * (in_use / down.mean() - 1)
        print()
        print(f'K in use vs K_down (the direction the curve commands):')
        print(f'   {in_use:.4f} vs {down.mean():.4f} +- {sem:.4f}  ->  in use is '
              f'{bias:+.2f}%  ({abs(in_use - down.mean())/sem:.1f} sigma)')
        print(f'   a high K makes y_obs read low, and the compensation-OFF arm '
              f'reads 6.4% low. Right sign, about half the size.')
    print()
    print('⚠ NOT WRITTEN ANYWHERE. 1.9 sigma off runs that had to be un-aliased')
    print('  first is a lead, not a constant. The clean measurement injects')
    print('  slowly enough that no frame gap approaches 128 px.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
