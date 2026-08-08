"""Fit the recoil curve from accumulated samples. MODEL.md's fitter.

    pixi run python calibration/fit_time_curve.py --weapon m416
    pixi run python calibration/fit_time_curve.py --selftest

    from calibration.fit_time_curve import fit
    res = fit(mags)
    res['knots']        # [{'t_ms':, 'dx':, 'dy':}, ...] ready for upload
    res['kept'] / res['dropped']

WHAT THIS IS NOT
----------------
It is not an update step. There is no previous curve, no alpha, no EMA, no
convergence gate. Every magazine ever fired for this weapon+config is an
estimate of the same function y_true(t); this pools them and fits once.

"Has this cell converged?" is not a question here -- the question is "are there
enough samples", which is a number you can read off. Root CLAUDE.md's three
sections on convergence criteria (whole-cell mean, first/last magazine, tail
window) are about the iterative loop this replaces.

THE OUTLIERS ARE WHOLE MAGAZINES
--------------------------------
What ruins a magazine -- a hand nudge, the wrong posture, an attachment that
did not seat, dropping out of ADS mid-burst, the correlator losing the view --
ruins the WHOLE trajectory, not scattered points on it. And the ruined
trajectory looks entirely reasonable on its own: smooth, monotone, plausible
magnitude. Per-point outlier rejection cannot see it. It is only visible next
to the other magazines.

So the clustering unit is one magazine, and the fit runs on the largest
cluster.

⚠ THE THRESHOLD COMES FROM THE DATA. Every hand-picked threshold in this
repository ended up as a gate that had only ever been checked on one side --
MARGIN_MIN rejected 0% of the errors it was set to catch, ADS_FRAC_MIN threw
away 55 magazines of which not one was a genuine hip-fire. Here the cut is the
largest relative jump in the single-linkage merge distances, i.e. the point
where the data itself stops being continuous.

⚠ AND WHAT IT DROPPED IS RETURNED, not just counted. A gate that cannot say
what it rejected can neither be tuned nor trusted; the evidence for changing it
is exactly the thing it discards.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from calibration import samples as S                              # noqa: E402

# The firmware plays knots at 1 ms resolution and holds 300 of them
# (MAX_PATTERN_POINTS). 17 ms is the grid the imported curves already use and
# resolves the within-interval kick profile into ~5 segments; see MODEL.md.
GRID_MS = 17.0
MAX_KNOTS = 295                 # under the firmware's 300, leaving headroom

# A cluster holding less than this share of the magazines is not "the main
# cluster", it is a fit running on a minority. Not a rejection -- the fit still
# happens -- but it is reported loudly, because a quiet fit on 3 of 20
# magazines looks exactly like a fit on all 20.
MIN_CLUSTER_FRAC = 0.5

# Below this many magazines there is nothing to cluster: any two trajectories
# are trivially "the largest cluster". Fit them all and say so.
MIN_FOR_CLUSTERING = 4

# How many of the most recent magazines the fit runs on. The store keeps
# everything forever; this bounds what any ONE fit averages.
#
# ⚠ IT IS NOT ABOUT STALENESS. y_true does not drift -- the gun is the gun.
# The reason old magazines have to age out is the curve-dependence measured on
# 2026-08-08: a magazine fired under a badly wrong curve gives a BIASED
# estimate (|y_obs| = 774 read 774 counts where the plateau reads ~900), and
# early iterations are exactly the ones fired under badly wrong curves. So the
# old magazines are not merely stale, they are wrong in a known direction, and
# averaging them in holds the fit away from the answer.
#
# The window is a proxy for the thing that actually matters, which is |y_obs|.
# A magazine fired under a near-correct curve is trustworthy however old it is,
# and one fired under a 1.5x curve is not, however recent. Weighting by |y_obs|
# directly would be sharper -- and is the obvious next refinement -- but a
# window needs no threshold and self-corrects as iteration proceeds.
RECENT_MAGS = 50

# How decisive the gap has to be before the magazines are called two groups
# rather than one. See cluster() for what it means and for the two-sided
# measurement that places it: 117.9x for a genuinely wrong magazine against
# 3.2x for a merely noisy one.
SEPARATION_MIN = 8.0


def _resample(mags, grid_s):
    """(n_mags, n_grid) of y_true, NaN where a magazine has no data."""
    M = np.full((len(mags), len(grid_s)), np.nan)
    for i, m in enumerate(mags):
        t, y = m.y_true_counts()
        ok = np.isfinite(t) & np.isfinite(y)
        if ok.sum() < 2:
            continue
        t, y = t[ok], y[ok]
        inside = (grid_s >= t[0]) & (grid_s <= t[-1])
        M[i, inside] = np.interp(grid_s[inside], t, y)
    return M


def _pairwise(M):
    """RMS distance in counts between every pair, over their shared span.

    ⚠ Pairs overlapping on fewer than a few points get inf, not 0. Two
    magazines that share no span are not identical, and a 0 there would merge
    them into one cluster -- which is how a truncated magazine would drag the
    whole fit toward itself.
    """
    n = len(M)
    D = np.full((n, n), np.inf)
    np.fill_diagonal(D, 0.0)
    for i in range(n):
        for j in range(i + 1, n):
            ok = np.isfinite(M[i]) & np.isfinite(M[j])
            if ok.sum() < 5:
                continue
            d = float(np.sqrt(np.mean((M[i][ok] - M[j][ok]) ** 2)))
            D[i, j] = D[j, i] = d
    return D


def _single_linkage(D):
    """(merge_order, merge_dists) -- Kruskal's MST, which IS single linkage."""
    n = len(D)
    edges = sorted((D[i, j], i, j) for i in range(n) for j in range(i + 1, n)
                   if np.isfinite(D[i, j]))
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    order, dists = [], []
    for d, i, j in edges:
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        parent[ri] = rj
        order.append((i, j))
        dists.append(d)
    return order, np.array(dists)


def _components(D, eps, n):
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        for j in range(i + 1, n):
            if D[i, j] <= eps:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=len, reverse=True)


def cluster(M):
    """-> (keep_idx, drop_idx, eps, why) with eps chosen by the data.

    WHERE to cut comes from the data: the largest RELATIVE jump between
    consecutive single-linkage merge distances. Relative rather than absolute
    because the scale is the weapon's -- 30 counts of disagreement means
    something different on a Vector than on an MG3, and an absolute threshold
    would be a per-weapon constant nobody would ever re-measure.

    WHETHER to cut at all needs a threshold, and pretending otherwise is how
    this went wrong the first time. A largest jump ALWAYS exists; with one
    homogeneous population it is just noise in the merge distances, and cutting
    on it split six identical magazines into 2 + 4. So the jump has to be
    decisive, and SEPARATION is the number that says whether it is:

        separation = (distance that would merge the two groups)
                   / (largest distance WITHIN the surviving group)

    ⚠ SEPARATION_MIN IS A THRESHOLD AND IT IS CHECKED ON BOTH SIDES. The
    self-test measures it in both directions and prints the margin:

        one magazine at the wrong posture   117.9x   must cut
        one magazine merely noisier           3.2x   must NOT cut

    A 36x gap between the two, so 8.0 sits far from either. Any change to this
    number has to move both of those and be re-run -- a gate measured only on
    the side it is supposed to catch is the single most repeated mistake in
    this repository (MARGIN_MIN rejected 0%, ADS_FRAC_MIN rejected 55
    magazines and not one of them was the thing it was for).
    """
    n = len(M)
    if n < MIN_FOR_CLUSTERING:
        return list(range(n)), [], float('inf'), (
            f'only {n} magazine(s) — nothing to cluster against, all kept')
    D = _pairwise(M)
    _order, dists = _single_linkage(D)
    if len(dists) < 2:
        return list(range(n)), [], float('inf'), (
            'trajectories share no common span — all kept, and that is itself '
            'a fault worth looking at')
    # +1e-9 keeps a run of identical distances from producing inf and winning
    # by accident.
    ratios = (dists[1:] + 1e-9) / (dists[:-1] + 1e-9)
    k = int(np.argmax(ratios))
    sep = float(ratios[k])
    eps = float(dists[k])
    span = f'merges span {dists[0]:.1f}..{dists[-1]:.1f}'
    if sep < SEPARATION_MIN:
        return list(range(n)), [], float('inf'), (
            f'no cut: best separation only {sep:.2f}x (need {SEPARATION_MIN}), '
            f'{span} — one population, all {n} kept')
    groups = _components(D, eps, n)
    keep = sorted(groups[0])
    drop = sorted(i for g in groups[1:] for i in g)
    why = (f'cut at {eps:.1f} counts RMS, separation {sep:.1f}x '
           f'(need {SEPARATION_MIN}), {span}')
    return keep, drop, eps, why


def fit(mags, grid_ms=GRID_MS, window=RECENT_MAGS):
    """Pool, cluster, fit. -> dict with knots, kept, dropped, diagnostics.

    `window` keeps only the most recent N magazines -- see RECENT_MAGS for why
    that is about bias and not about staleness. Pass 0 for all of them.

    ⚠ The order is the store's, which is append order, which is chronological
    because samples.append() only ever appends. Nothing sorts by `ts`: two
    magazines a second apart can share a timestamp string, and a sort would
    then reorder them arbitrarily on every call.
    """
    mags = [m for m in mags if m.n_frames() >= 3]
    if not mags:
        return {'ok': False, 'why': 'no magazines with samples'}
    n_all = len(mags)
    if window and len(mags) > window:
        mags = mags[-window:]

    span = max(float(np.max(m.t)) for m in mags if len(m.t))
    if span <= 0:
        return {'ok': False, 'why': 'every magazine has a zero-length span'}
    # Never emit more knots than the firmware holds. An M249's 150 rounds at
    # 85 ms is 12.75 s, which at 17 ms would be 750 knots; the grid coarsens
    # rather than the tail being silently cut off.
    step_ms = max(grid_ms, span * 1000.0 / MAX_KNOTS)
    grid_s = np.arange(0.0, span + step_ms / 1000.0, step_ms / 1000.0)
    # Trim the tail that no magazine can reach. Left in, it is an all-NaN
    # column: nanmedian warns and returns NaN, and the "hold the last value"
    # rule below then extends the curve past every measurement that exists.
    grid_s = grid_s[grid_s <= span]

    M = _resample(mags, grid_s)
    keep, drop, eps, why = cluster(M)

    Mk = M[keep]
    with np.errstate(all='ignore'):
        n_at = np.sum(np.isfinite(Mk), axis=0)
        Y = np.nanmedian(Mk, axis=0)
        spread = np.nanstd(Mk, axis=0)
    # Grid points no kept magazine reached carry no information. Held at the
    # last known value rather than extrapolated: a curve that keeps climbing
    # past the data is compensation for rounds nobody fired.
    if np.any(n_at > 0):
        last = int(np.max(np.nonzero(n_at > 0)[0]))
        Y[:last + 1] = _fill_forward(Y[:last + 1])
        Y[last + 1:] = Y[last]
    Y = np.nan_to_num(Y, nan=0.0)
    # y_true is measured from the first frame, so it starts at 0 by
    # construction; subtracting it again costs nothing and makes a magazine
    # whose baseline drifted show up as a shifted curve rather than a scaled one.
    Y = Y - Y[0]

    dy = np.diff(Y, prepend=0.0)
    knots = [{'t_ms': int(round(grid_s[i] * 1000)), 'dx': 0.0,
              'dy': float(dy[i])} for i in range(len(grid_s))]

    frac = len(keep) / len(mags)
    return {
        'ok': True,
        'knots': knots,
        'grid_ms': step_ms,
        'span_s': span,
        'total_counts': float(Y[-1]),
        'kept': [_label(mags[i]) for i in keep],
        'dropped': [dict(_label(mags[i]),
                         rms_to_centre=_dist_to(M, i, keep))
                    for i in drop],
        'n_kept': len(keep), 'n_total': len(mags),
        'n_stored': n_all, 'window': window,
        'eps': eps, 'cluster_why': why,
        'minority': frac < MIN_CLUSTER_FRAC,
        'samples_per_knot': float(np.mean(n_at[n_at > 0])) if np.any(n_at > 0)
        else 0.0,
        'spread_counts': float(np.nanmedian(spread[n_at > 1]))
        if np.sum(n_at > 1) else float('nan'),
    }


def _fill_forward(a):
    out = a.copy()
    idx = np.arange(len(out))
    ok = np.isfinite(out)
    if not ok.any():
        return np.zeros_like(out)
    return np.interp(idx, idx[ok], out[ok])


def _separation(mags, grid_ms=GRID_MS):
    """The raw separation ratio, for reporting the gate's margin both ways."""
    span = max(float(np.max(m.t)) for m in mags if len(m.t))
    step = max(grid_ms, span * 1000.0 / MAX_KNOTS)
    g = np.arange(0.0, span + step / 1000.0, step / 1000.0)
    D = _pairwise(_resample(mags, g[g <= span]))
    _o, d = _single_linkage(D)
    if len(d) < 2:
        return float('nan')
    return float(np.max((d[1:] + 1e-9) / (d[:-1] + 1e-9)))


def _label(m):
    return {'ts': m.ts, 'weapon': m.weapon, 'posture': m.posture,
            'n_frames': m.n_frames(), 'ads_frac': m.ads_frac,
            'comp_enabled': m.comp_enabled, 'note': m.note}


def _dist_to(M, i, keep):
    """How far this magazine sits from the kept cluster's centre, in counts."""
    if not len(keep):
        return float('nan')
    with np.errstate(all='ignore'):
        centre = np.nanmedian(M[keep], axis=0)
    ok = np.isfinite(M[i]) & np.isfinite(centre)
    if ok.sum() < 5:
        return float('inf')
    return float(np.sqrt(np.mean((M[i][ok] - centre[ok]) ** 2)))


# ── offline gate ──

def _synth(n_frames=300, span=3.4, total=3300.0, K=1.5474, scale=1.0,
           bump=0.0, noise=0.5, seed=0, curve=None):
    """A magazine whose true answer is known: y_true(t) = total * (t/span)**1.3."""
    rng = np.random.default_rng(seed)
    t = np.linspace(0.0, span, n_frames)
    y_true = total * scale * (t / span) ** 1.3
    if bump:
        y_true = y_true + bump * np.exp(-((t - span * 0.5) / (span * 0.1)) ** 2)
    comp = S.comp_counts_at(curve or [], t)
    y_obs = y_true - comp
    d_counts = np.diff(y_obs) + rng.normal(0, noise, n_frames - 1)
    return S.Magazine(
        weapon='synth', sight='red_dot', K=K, curve=curve or [],
        comp_enabled=bool(curve), t=list(t),
        dy_px=list(d_counts * K), human_dy=[0.0] * (n_frames - 1),
        oor=[False] * (n_frames - 1), ts=f'seed{seed}')


def selftest():
    """Two-sided: the fit must find the truth, AND the clustering must reject
    a bad magazine while NOT rejecting a merely noisy one."""
    fails = []

    # 1. No compensation, clean: the fit must recover the truth.
    mags = [_synth(seed=i) for i in range(6)]
    r = fit(mags)
    err = abs(r['total_counts'] - 3300.0)
    print(f'  1 clean, comp off        total {r["total_counts"]:8.1f} '
          f'(truth 3300)  err {err:5.1f}   kept {r["n_kept"]}/{r["n_total"]}')
    if err > 40:
        fails.append('clean fit missed the truth by more than 40 counts')
    if r['n_kept'] != 6:
        fails.append('clean magazines were not all kept')

    # 2. THE POINT OF THE STORE: magazines fired under DIFFERENT curves must
    #    still agree, because each adds its own curve back.
    c1 = [{'t_ms': int(t), 'dx': 0.0, 'dy': 20.0} for t in range(13, 3400, 17)]
    c2 = [{'t_ms': int(t), 'dx': 0.0, 'dy': 8.0} for t in range(13, 3400, 17)]
    mixed = ([_synth(seed=10 + i, curve=c1) for i in range(3)]
             + [_synth(seed=20 + i, curve=c2) for i in range(3)])
    r2 = fit(mixed)
    err2 = abs(r2['total_counts'] - 3300.0)
    print(f'  2 two different curves   total {r2["total_counts"]:8.1f} '
          f'(truth 3300)  err {err2:5.1f}   kept {r2["n_kept"]}/{r2["n_total"]}')
    if err2 > 60:
        fails.append('pooling across curves did not recover the truth')
    if r2['n_kept'] != 6:
        fails.append('magazines under different curves were split into clusters')

    # 3. One magazine fired at the wrong posture (0.55x). It must be dropped.
    bad = [_synth(seed=i) for i in range(6)] + [_synth(seed=99, scale=0.55)]
    r3 = fit(bad)
    dropped_ok = r3['n_kept'] == 6 and len(r3['dropped']) == 1
    print(f'  3 one wrong-posture mag  kept {r3["n_kept"]}/{r3["n_total"]}  '
          f'{r3["cluster_why"]}')
    if not dropped_ok:
        fails.append('the wrong-posture magazine was not the one dropped')

    # 4. ⚠ THE OTHER SIDE, and the reason SEPARATION_MIN exists. Merely
    #    noisier magazines must NOT be dropped. A clusterer that only ever
    #    rejects is indistinguishable from one that rejects correctly, and
    #    this repository has shipped three of those. The FIRST version of this
    #    file failed exactly here: it cut on the largest jump whether or not
    #    the jump meant anything, and split six identical magazines 2 + 4.
    noisy = [_synth(seed=i) for i in range(5)] + [_synth(seed=50, noise=3.0)]
    r4 = fit(noisy)
    print(f'  4 one noisy (not wrong)  kept {r4["n_kept"]}/{r4["n_total"]}  '
          f'{r4["cluster_why"]}')
    if r4['n_kept'] != 6:
        fails.append('a merely noisy magazine was rejected — the cut is too tight')

    # 4b. The two-sided margin, printed rather than asserted, so a change to
    #     SEPARATION_MIN shows what it costs on each side.
    s_bad = _separation([_synth(seed=i) for i in range(6)]
                        + [_synth(seed=99, scale=0.55)])
    s_ok = _separation([_synth(seed=i) for i in range(5)]
                       + [_synth(seed=50, noise=3.0)])
    s_clean = _separation([_synth(seed=i) for i in range(6)])
    print(f'    separation:  wrong-posture {s_bad:7.1f}x   '
          f'noisy {s_ok:5.1f}x   all-clean {s_clean:5.1f}x   '
          f'gate {SEPARATION_MIN}')
    if not (s_ok < SEPARATION_MIN < s_bad and s_clean < SEPARATION_MIN):
        fails.append('SEPARATION_MIN no longer sits between the two sides')

    # 5. Knot count must stay inside the firmware's table for a long magazine.
    long = [_synth(seed=i, span=12.75, total=9000.0, n_frames=1100)
            for i in range(4)]
    r5 = fit(long)
    print(f'  5 12.75 s magazine       {len(r5["knots"])} knots @ '
          f'{r5["grid_ms"]:.1f} ms  (firmware holds 300)')
    if len(r5['knots']) > 300:
        fails.append('emitted more knots than the firmware can hold')

    print()
    if fails:
        for f in fails:
            print(f'  [FAIL] {f}')
        return 1
    print('  all 5 pass')
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon')
    ap.add_argument('--config', default=None,
                    help='config key as stored, e.g. "bare"')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.weapon:
        ap.error('--weapon or --selftest')
    p = os.path.join(S.SAMPLE_DIR, f'{a.weapon}__{a.config or "bare"}.jsonl')
    mags = S.load(a.weapon, path=p)
    if not mags:
        print(f'no samples at {p}')
        return 2
    r = fit(mags)
    if not r['ok']:
        print(r['why'])
        return 3
    print(f'{a.weapon} {a.config or "bare"}: {r["n_kept"]}/{r["n_total"]} '
          f'magazines, {len(r["knots"])} knots @ {r["grid_ms"]:.1f} ms')
    print(f'  total {r["total_counts"]:.1f} counts over {r["span_s"]:.2f} s')
    print(f'  {r["cluster_why"]}')
    print(f'  samples per knot {r["samples_per_knot"]:.1f}, '
          f'spread {r["spread_counts"]:.1f} counts')
    if r['minority']:
        print(f'  [!] the kept cluster is a MINORITY ({r["n_kept"]}/'
              f'{r["n_total"]}) — this is a fit on the smaller group')
    for d in r['dropped']:
        print(f'  dropped {d["ts"]}: {d["rms_to_centre"]:.1f} counts from '
              f'the centre, ads_frac {d["ads_frac"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
