"""Which per-knot centre ships a more REPEATABLE curve? Offline, real store.

    pixi run python tools/probe_estimator.py
    pixi run python tools/probe_estimator.py --reps 80 --min-mags 10

The question is not which estimator sounds more robust. It is: if I had fired
a different half of these magazines, how different a curve would I have
shipped? So each cell's magazines are split in half at random, BOTH halves are
fitted by the real fit(), and the two answers are compared. Repeat, take the
median. The estimator with the smaller half-to-half disagreement is the more
precise one ON THIS DATA, which is the only place the choice can be settled --
the efficiency numbers in the textbooks are for Gaussian noise, and nothing
here has ever shown that this noise is Gaussian.

⚠ IT CALLS fit(), IT DOES NOT REIMPLEMENT IT. calibration/CLAUDE.md's standing
finding is that every parallel implementation in this repository eventually
drifted from the one it copied, and the symptom was never an exception -- it
was a batch of numbers that looked completely normal. A probe that fitted its
own curves would be measuring a copy.

⚠ WHAT THIS CANNOT SEE: bias. Both halves come from the same magazines, so an
estimator that is consistently wrong in the same direction scores perfectly
here. Bias is what harness/verdict.py's arms check is for -- magazines fired
under different compensation curves must estimate the same y_true, and the
fitter cannot tell which arm a magazine came from. Precision here, bias there.
"""
import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from calibration import samples as S                              # noqa: E402
from calibration.fit_time_curve import fit                        # noqa: E402

# Where on the curve to compare the two halves. 1.5 s is where MODEL.md's own
# cross-arm check reads 0.9%; 2.4 s is the top of the verified band. `total` is
# the number every report in this project prints, so it is here whether or not
# it is the sharpest one.
PROBE_S = (0.5, 1.0, 1.5, 2.0, 2.4)


def _curve_at(res, ts):
    """y_true in counts at each of `ts`, from a fit result. NaN past its end."""
    t = np.array([k['t_ms'] for k in res['knots']], dtype=float) / 1000.0
    y = np.cumsum([k['dy'] for k in res['knots']])
    out = np.interp(ts, t, y, left=np.nan, right=np.nan)
    return out


def _cells(min_mags):
    """[(key, magazines)] for every stored cell big enough to split.

    ⚠ BY FILE, NOT BY A WEAPON LIST. The store's filename IS the cell key
    (`samples.path_for`), so globbing it enumerates exactly what exists; a
    roster typed here would quietly skip any cell whose weapon nobody thought
    to list, and the probe would report on a subset while looking complete.
    """
    import glob
    out = []
    for path in sorted(glob.glob(os.path.join(S.SAMPLE_DIR, '*.jsonl'))):
        mags = S.load(None, None, path=path)
        if len(mags) >= min_mags:
            out.append((os.path.basename(path)[:-len('.jsonl')], mags))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--reps', type=int, default=40,
                    help='random half-splits per cell')
    ap.add_argument('--min-mags', type=int, default=8,
                    help='skip cells with fewer magazines than this — a split '
                         'of 6 is two fits of 3, which measures the split')
    ap.add_argument('--centres', default='median,iqm,mean')
    a = ap.parse_args()

    centres = [c for c in a.centres.split(',') if c]
    cells = _cells(a.min_mags)
    if not cells:
        print(f'no stored cell has {a.min_mags} magazines')
        return 1
    print(f'{len(cells)} cell(s), {a.reps} half-splits each, '
          f'centres {centres}\n')

    ts = np.array(PROBE_S)
    # disagreement[centre] -> list of relative |A-B| over every (cell, rep,
    # probe time) that both halves reached.
    dis = {c: [] for c in centres}
    per_cell = {c: {} for c in centres}
    shifts = []                       # full-pool curve movement vs the median

    for key, mags in cells:
        n = len(mags)
        rng = np.random.default_rng(0)       # deterministic: same splits for
        splits = [rng.permutation(n) for _ in range(a.reps)]   # every centre
        full = {}
        for c in centres:
            got = []
            for perm in splits:
                A = [mags[i] for i in perm[:n // 2]]
                B = [mags[i] for i in perm[n // 2:]]
                rA, rB = fit(A, centre=c), fit(B, centre=c)
                if not (rA.get('ok') and rB.get('ok')):
                    continue
                yA, yB = _curve_at(rA, ts), _curve_at(rB, ts)
                ok = np.isfinite(yA) & np.isfinite(yB)
                mid = (yA + yB) / 2.0
                ok &= np.abs(mid) > 1.0      # 0 counts has no relative error
                got.extend(np.abs(yA[ok] - yB[ok]) / np.abs(mid[ok]))
            if got:
                dis[c].extend(got)
                per_cell[c][key] = float(np.median(got))
            r = fit(mags, centre=c)
            if r.get('ok'):
                full[c] = r
        if 'median' in full and 'iqm' in full:
            m, q = full['median']['total_counts'], full['iqm']['total_counts']
            if abs(m) > 1:
                shifts.append((key, full['median']['n_kept'], m, q,
                               (q - m) / abs(m)))

    print(f'{"cell":<44} {"n":>3}  ' +
          '  '.join(f'{c:>8}' for c in centres))
    keys = sorted({k for c in centres for k in per_cell[c]})
    sizes = {k: len(m) for k, m in cells}
    for k in keys:
        row = f'{k:<44} {sizes.get(k, 0):>3}  '
        row += '  '.join(f'{per_cell[c].get(k, float("nan")) * 100:7.2f}%'
                         for c in centres)
        print(row)

    print(f'\n{"":<44} {"":>3}  ' + '  '.join(f'{c:>8}' for c in centres))
    print(f'{"MEDIAN half-to-half disagreement":<44} {"":>3}  ' +
          '  '.join(f'{np.median(dis[c]) * 100:7.2f}%' if dis[c] else '      —'
                    for c in centres))
    print(f'{"MEAN  half-to-half disagreement":<44} {"":>3}  ' +
          '  '.join(f'{np.mean(dis[c]) * 100:7.2f}%' if dis[c] else '      —'
                    for c in centres))
    print(f'{"comparisons":<44} {"":>3}  ' +
          '  '.join(f'{len(dis[c]):8d}' for c in centres))

    base = np.median(dis['median']) if dis.get('median') else None
    if base:
        print('\nrelative to the median (lower is more repeatable):')
        for c in centres:
            if dis[c]:
                print(f'  {c:<8} {np.median(dis[c]) / base:5.3f}x')

    if shifts:
        print('\nHOW FAR THE SHIPPED CURVE MOVES, full pool, iqm vs median:')
        for key, nk, m, q, rel in sorted(shifts, key=lambda s: -abs(s[4])):
            print(f'  {key:<44} {nk:>3} kept  {m:8.1f} -> {q:8.1f} '
                  f'({rel:+.2%})')
        big = [s for s in shifts if abs(s[4]) > 0.02]
        print(f'  {len(big)}/{len(shifts)} cell(s) move more than 2% — those '
              f'are the curves that need re-shipping if this changes.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
