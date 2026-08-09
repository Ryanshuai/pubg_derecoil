"""Is the 14 ms between RECOIL_FIRE_DELAY_MS and -L amplitude, or a missed lag?

    pixi run python tools/probe_offset_decomposition.py

Offline. Reads the interleaved offset sweep already in the sample store and
asks the one question the sweep was not asked at the time.

THE SETUP. With the compensation on, what is left on screen is

    y_obs_j(t) = y_true(t) - (1-eps) * C_j(t - M)

  C_j   the schedule the firmware actually emitted for arm j -- READ BACK off
        the Pico, so RECOIL_FIRE_DELAY_MS is already inside it
  eps   the fraction of the curve that never arrives (the "amplitude term")
  M     the TRUE command->photon lag, which is what config.RECOIL_COMP_LAG_MS
        is trying to be

C_j is F shifted by the arm's commanded offset D_j, so to first order

    y_obs_j(t) ~ eps * F(t)  -  (-D_j - M) * F'(t)

⚠ ONE ARM CANNOT SEPARATE THESE. F and F' are nearly collinear over a single
burst shape, which is exactly how an earlier least-squares decomposition
returned +4.74% gain and +3.2 ms lag and was then refuted by firing it. What
makes this well-posed is that D_j VARIES BY A KNOWN AMOUNT across five arms:
the arm-to-arm difference is pure F' with a coefficient nobody had to fit,

    y_obs_j - y_obs_k = (D_j - D_k) * F'(t)

so the sweep measures its own sensitivity, and only then is the level split
between eps and M identified.

WHAT THE TWO ANSWERS MEAN, and they are not equally harmless:

  amplitude   M ~ 20, the analysis's L is right, y_true = y_obs + C(t - L) is
              exact, and -36 is a good practical offset that trades a bit of
              lead against a shortfall. Per-gun, and it will move.
  missed lag  M ~ 36, the analysis is using 20, and the fit's own feedback
              carries (M - L) * F' with gain (M-L)*omega -- still divergent
              above ~1/16ms = 10 Hz on a 59 Hz grid. Only 4/5 fixed.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np

from calibration import samples as S
from calibration.samples import comp_counts_at

# The five commanded offsets, and the knot count each one produces. The fold in
# upload_pattern eats one knot per ~17 ms of lead, so the count IS the arm's
# signature -- and it is a signature READ OFF THE STORED CURVE rather than off
# the run's prose, which is the only version of it the magazine can defend.
ARMS = {171: -90, 172: -70, 174: -50, 175: -30, 176: -10}

GRID = np.arange(0.05, 2.60, 0.01)      # inside every burst's hold


def arm_of(m):
    """Which offset this magazine was fired at, from its own curve. None if it
    is not one of the sweep's arms."""
    if not m.curve or not m.comp_enabled:
        return None
    return ARMS.get(len(m.curve))


def resample(m, grid):
    """y_obs on the common grid, in counts. NaN outside the burst."""
    t, y = m.y_obs_counts()
    t = np.asarray(t, dtype=float)
    ok = np.isfinite(y)
    if ok.sum() < 20:
        return np.full_like(grid, np.nan)
    out = np.interp(grid, t[ok], y[ok], left=np.nan, right=np.nan)
    out[(grid < t[ok][0]) | (grid > t[ok][-1])] = np.nan
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapon', default='mp5k')
    ap.add_argument('--cell', default='bare')
    a = ap.parse_args()

    cfg = {} if a.cell == 'bare' else a.cell
    mags = [m for m in S.load(a.weapon, cfg) if arm_of(m) is not None]
    if not mags:
        print('[!] no sweep magazines found')
        return 1

    by_arm = {}
    for m in mags:
        by_arm.setdefault(arm_of(m), []).append(m)
    print(f'{len(mags)} magazine(s) over {len(by_arm)} arm(s): '
          + ', '.join(f'{d:+d} ms x{len(v)}' for d, v in sorted(by_arm.items())))
    if len(by_arm) < 2:
        print('[!] need at least two arms -- one arm cannot separate eps from M')
        return 1

    # F and F' from the arm whose curve is closest to unshifted, evaluated on
    # the SAME grid everything else lives on. Undo that arm's own offset so F is
    # the fitted curve in its own time.
    ref_d = max(by_arm, key=lambda d: d)          # -10 ms, the least shifted
    ref = by_arm[ref_d][0]
    F = comp_counts_at(ref.curve, GRID + ref_d / 1000.0)
    Fp = np.gradient(F, GRID)                     # counts per second
    print(f'F from the {ref_d:+d} arm: {F[-1]:.1f} counts by t={GRID[-1]:.2f} s, '
          f"F' median {np.median(Fp):.0f} counts/s")

    obs = {d: np.nanmean([resample(m, GRID) for m in v], axis=0)
           for d, v in by_arm.items()}

    ds = sorted(by_arm)

    # ⚠ PAIRWISE DIFFERENCING LOOKS LIKE THE CLEAN WAY TO GET THE SENSITIVITY
    # AND IT IS NOT. Each difference is two five-magazine means subtracted, so
    # the between-magazine scatter lands on it twice while the signal is only
    # 20 ms of lead. Printed anyway, because the numbers coming out ranging
    # from -0.88 to +2.31 around a "median 0.66" is the evidence that a median
    # of four wild numbers is not a measurement -- and it is the reason the
    # slope below is fitted over all five arms at once instead.
    print()
    print("pairwise d(residual)/d(offset), observed / F'-predicted:")
    for i in range(len(ds) - 1):
        d0, d1 = ds[i], ds[i + 1]
        diff, pred = obs[d1] - obs[d0], (d1 - d0) / 1000.0 * Fp
        ok = np.isfinite(diff) & np.isfinite(pred) & (np.abs(pred) > 1e-9)
        g = float(np.sum(diff[ok] * pred[ok]) / np.sum(pred[ok] ** 2))
        print(f'   {d0:+4d} -> {d1:+4d} ms:  {g:6.2f}')
    print('   ^ noise. Five magazines an arm cannot carry a difference of two'
          ' means.')

    def split(pick):
        """(slope, intercept, mean eps) from one draw. `pick` maps arm -> the
        magazines to use, so the caller can bootstrap it."""
        rows = []
        for d in ds:
            y = np.nanmean([resample(m, GRID) for m in pick[d]], axis=0)
            ok = np.isfinite(y)
            if ok.sum() < 50:
                return None
            A = np.column_stack([F[ok], Fp[ok]])
            coef, *_ = np.linalg.lstsq(A, y[ok], rcond=None)
            rows.append((d, coef[0], 1000.0 * coef[1]))
        D = np.array([r[0] for r in rows], dtype=float)
        c = np.array([r[2] for r in rows])
        sl, ic = np.polyfit(D, c, 1)
        return sl, ic, float(np.mean([r[1] for r in rows])), rows

    # The F' coefficient should be -(-D - M)*G = G*D + G*M, so the LINE over
    # five arms gives the sensitivity as its slope and M as intercept/slope.
    # The collinear pair is never asked to separate itself within one arm.
    sl, ic, eps, rows = split({d: by_arm[d] for d in ds})
    print()
    print("per-arm least squares on [F, F']:")
    for d, e, c in rows:
        print(f'   {d:+4d} ms:  eps {100*e:+6.2f}%   F\' coef {c:+7.1f} ms')
    M = ic / sl
    print()
    print(f"F' coef vs commanded offset:  slope {sl:+.2f}  intercept {ic:+.1f} ms"
          f'  ->  M = {M:+.1f} ms,  eps = {100*eps:+.2f}%')

    # ⚠ BOOTSTRAP OVER MAGAZINES, because "M = 24.3" against a 20 the analysis
    # uses and a 36 the sweep picked is a three-way distinction that a point
    # estimate cannot referee. Resampling magazines within each arm is the
    # right unit: a magazine is what goes wrong as a whole.
    rng = np.random.default_rng(7)
    bs = []
    for _ in range(400):
        pick = {d: [by_arm[d][i] for i in rng.integers(0, len(by_arm[d]),
                                                       len(by_arm[d]))]
                for d in ds}
        r = split(pick)
        if r:
            bs.append((r[0], r[1] / r[0] if abs(r[0]) > 1e-6 else np.nan, r[2]))
    bs = np.array([b for b in bs if np.isfinite(b[1])])
    lo, hi = np.percentile(bs, [2.5, 97.5], axis=0)
    print(f'   bootstrap (n={len(bs)}):  slope [{lo[0]:+.2f}, {hi[0]:+.2f}]   '
          f'M [{lo[1]:+.1f}, {hi[1]:+.1f}] ms   '
          f'eps [{100*lo[2]:+.2f}%, {100*hi[2]:+.2f}%]')

    # ── the decomposition has to reproduce the sweep that produced it ──
    # residual = eps*F - u*F', u = -D - M, so whole-path least squares is
    # minimised at u* = eps * <F,F'> / <F',F'>, i.e. at D* = -M - u*. If this
    # does not land on the offset the firing actually picked, the split is
    # arithmetic about noise and nothing here means anything.
    ok = np.isfinite(F) & np.isfinite(Fp)
    u_star = 1000.0 * eps * np.sum(F[ok] * Fp[ok]) / np.sum(Fp[ok] ** 2)
    print()
    print(f'BACK-PREDICTION: minimum-RMS offset D* = -M - {u_star:.1f} '
          f'= {-M - u_star:+.1f} ms')
    rms = {d: float(np.sqrt(np.nanmean(obs[d] ** 2))) for d in ds}
    q = np.polyfit(np.array(ds, dtype=float),
                   np.array([rms[d] for d in ds]), 2)
    print('   observed whole-path RMS  '
          + '  '.join(f'{d:+d}:{rms[d]:.1f}' for d in ds)
          + f'   -> parabola minimum {-q[1] / (2 * q[0]):+.1f} ms')

    print()
    print('read it like this:')
    print('   M near 20  -> the analysis L is right; -36 is an amplitude trade')
    print('   M near 36  -> the analysis is 16 ms short and the loop still has')
    print('                 positive feedback above ~10 Hz')
    print()
    print('⚠ AND THEN GO FIRE IT. The last offline decomposition of this same')
    print('  residual returned +4.74% gain / +3.2 ms lag, was arithmetically')
    print('  fine, and was refuted the moment it was fired. The prediction')
    print('  this one has to survive: scaling the curve by 1/(1-eps) should')
    print(f'  move the optimum from {-M - u_star:+.1f} to {-M:+.1f} ms and')
    print('  leave M where it is.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
