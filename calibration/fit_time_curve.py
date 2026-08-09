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

# ── A magazine that is not a burst ──────────────────────────────────────────
# WHILE THE TRIGGER IS DOWN, y_true CAN ONLY RISE. Recoil pushes the view up;
# nothing in the model pushes it back. So a trajectory that falls materially
# below its own running maximum BEFORE the release is not a magazine measured
# badly, it is not a magazine.
#
# ⚠ MEASURED BOTH SIDES, 204 stored magazines, drawdown as a fraction of the
# final value and taken ONLY over t <= 0.95 * hold_s:
#
#     0808_093842  m416 bare       10.23%     <- the one bad magazine
#     second worst                  4.16%
#     p99                           4.15%
#     p90                           2.62%
#     median                        0.65%
#
# 6% sits in a 2.5x gap: it rejects the one and keeps all 203 others with 1.44x
# of margin below and 1.7x above.
#
# ⚠ THE RESTRICTION TO THE HOLD IS WHAT MAKES IT WORK, and without it there is
# no gate at all. Over the WHOLE magazine the same statistic reads 10.2% on the
# bad one and 10.2% on a perfectly good one -- because after the trigger
# releases the game settles the view back down, and that drawdown is real in
# every magazine. The first version of this idea was measured unrestricted,
# found no separation, and would have been asserted as a gate anyway if the
# measurement had not been taken.
#
# What it catches, concretely: 0808_093842's y_true climbs to 702 counts at
# t=2.4 s, falls to 641, and then FLATLINES for the last 1.5 s of a 3.46 s
# hold. Clustering could not see it -- separation 5.55x against a gate of 8.0,
# "one population, all 13 kept" -- and it is the single reason the m416 bare
# cell reads cv 22.3% where every other m416 cell reads 0.6..3.2%.
HOLD_DRAWDOWN_MAX = 0.06

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


def _hold_drawdown(m):
    """How far y_true falls below its own running max WHILE THE TRIGGER IS DOWN,
    as a fraction of the magazine's final value. 0.0 when unanswerable.

    Unanswerable rather than zero-by-default matters: `hold_s` is 0 on every
    magazine stored before 2026-08-08, and treating those as "no drawdown"
    is a claim about them. It reads as "this gate does not apply", which is
    what it is -- the release time is not recorded, so there is no hold to
    restrict to. See HOLD_DRAWDOWN_MAX for why the restriction is the gate.
    """
    if not getattr(m, 'hold_s', 0):
        return 0.0
    t, y = m.y_true_counts()
    tt = np.asarray(t, dtype=float)
    if len(tt) < 10:
        return 0.0
    tt = tt - tt[0]
    sel = tt <= m.hold_s * 0.95
    if int(sel.sum()) < 10:
        return 0.0
    yy = np.asarray(y, dtype=float)[sel]
    end = max(abs(float(y[-1])), 1.0)
    return float(np.nanmax(np.maximum.accumulate(yy) - yy) / end)


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
    # ⚠ "TAKE THE LARGEST CLUSTER" IS A COIN FLIP WHEN THE SPLIT IS EVEN, and
    # it landed on the wrong side. Measured 2026-08-08: mp5k `grip-vert_grip`
    # held 5 clean magazines and 5 fired out of a different gun. The cut was
    # textbook -- separation 20.3x against a gate of 8.0 -- and then the fit
    # ran on the CONTAMINATED five, reporting 435.2 counts where the clean five
    # say 669. Nothing in the output said which five; `n_kept 5/10` looks
    # identical either way.
    #
    # An even split is not a cluster to pick, it is a cell holding two
    # populations of equal weight, and the size rule has no information left.
    # So it refuses, and the caller has to say which gun it meant -- which is
    # exactly what happened by hand, and cost a re-measurement to discover.
    if len(groups) > 1 and len(groups[0]) == len(groups[1]):
        return [], list(range(n)), eps, (
            f'REFUSING: separation {sep:.1f}x split {n} magazines into groups '
            f'of {[len(g) for g in groups]} — an even split means two '
            f'populations of equal weight, and "the largest cluster" is then a '
            f'coin flip. One of these is not the gun you asked for; quarantine '
            f'it (rename the file, never delete) and fit again.')
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

    # ⚠ VALIDITY BEFORE CLUSTERING, and they are different questions. Clustering
    # asks "is this magazine like the others"; this asks "is this a burst at
    # all". A trajectory that reverses mid-hold fails the second regardless of
    # how many others happen to reverse with it -- and clustering demonstrably
    # cannot cover for it (see HOLD_DRAWDOWN_MAX: separation 5.55x against a
    # gate of 8.0, so the flatlined magazine was kept as "one population").
    not_bursts = [(m, _hold_drawdown(m)) for m in mags
                  if _hold_drawdown(m) > HOLD_DRAWDOWN_MAX]
    if not_bursts:
        bad = {id(m) for m, _ in not_bursts}
        mags = [m for m in mags if id(m) not in bad]
        if not mags:
            return {'ok': False,
                    'why': f'every magazine falls more than '
                           f'{HOLD_DRAWDOWN_MAX:.0%} below its own running '
                           f'maximum while the trigger is still down — none of '
                           f'them is a burst'}
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
        # ⚠ REPORTED, NOT JUST FILTERED. A run that silently drops a magazine
        # reads exactly like one that had nothing to drop, and tools/CLAUDE.md's
        # rule is that a bound on coverage has to say what it bounded.
        'not_bursts': [dict(_label(m), hold_drawdown=d) for m, d in not_bursts],
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
        # ⚠ hold_s MATTERS HERE, and leaving it 0 silently disabled a gate.
        # _hold_drawdown answers 0.0 when the release time is unknown -- that
        # is honest for the pre-2026-08-08 magazines that never recorded one,
        # and it made every synthetic magazine exempt from the burst check, so
        # the case written to prove that check worked passed by not running it.
        hold_s=span,
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

    # 4c. NOT A BURST. A trajectory that reverses mid-hold is excluded before
    #     clustering ever runs, because clustering demonstrably cannot see it:
    #     on the real m416 bare cell the flatlined magazine sat inside "one
    #     population, all 13 kept" at separation 5.55x against a gate of 8.0.
    #     Both directions here -- the reversal is cut, and the clean six are not.
    reversed_mag = _synth(seed=77)
    _t, _y = reversed_mag.y_true_counts()
    half = len(reversed_mag.dy_px) // 2
    # Undo 12% of the climb over a few frames, well inside the hold. Written on
    # dy_px (the stored quantity) so it goes through y_true_counts like any
    # other magazine rather than being injected past it.
    back = 0.12 * abs(float(_y[-1])) * reversed_mag.K / 5.0
    for j in range(half, half + 5):
        reversed_mag.dy_px[j] -= back
    r4c = fit([_synth(seed=i) for i in range(6)] + [reversed_mag])
    n_nb = len(r4c.get('not_bursts', ()))
    print(f'  4c one reversal mid-hold not-a-burst {n_nb}, kept '
          f'{r4c["n_kept"]}/{r4c["n_total"]}')
    if n_nb != 1:
        fails.append(f'the mid-hold reversal was not excluded (not_bursts={n_nb})')
    if r4c['n_kept'] != 6:
        fails.append('the six clean magazines did not survive the burst check')
    r4d = fit([_synth(seed=i) for i in range(6)])
    if r4d.get('not_bursts'):
        fails.append('a clean magazine was called not-a-burst')

    # 4e. AN EVEN SPLIT REFUSES. Five clean and five at 0.65x is not a cluster
    #     to pick -- "the largest" has no information left, and on the real
    #     mp5k grip cell it picked the CONTAMINATED five (fitting 435 counts
    #     where the clean five say 669) with a textbook separation of 20.3x.
    even = [_synth(seed=i) for i in range(5)] + \
           [_synth(seed=200 + i, scale=0.65) for i in range(5)]
    r4e = fit(even)
    refused = r4e['n_kept'] == 0 and 'REFUSING' in r4e['cluster_why']
    print(f'  4e 5 clean vs 5 at 0.65x  '
          f'{"REFUSED, as it must" if refused else r4e["cluster_why"][:60]}')
    if not refused:
        fails.append('an even two-population split was resolved by coin flip')

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


# ── Does the fit depend on WHICH session it was fitted from? ────────────────
#
# ⚠ "REPLICATE IT" HAS NO MEANING ON POOLED DATA, and that is why this exists.
# Every other measurement in MODEL.md gets replicated by firing a second run;
# a fit eats the WHOLE store at once, and the store spans dozens of sessions.
# So the replication question here is not "run it again", it is
#
#     cut the store by session, fit each slice, do the slices agree?
#
# ⚠ AND IT MUST SPLIT BY THE CURVE ARM TOO, WHICH THE FIRST VERSION DID NOT.
# Grouping by session alone reported 1.54% for mp5k and 3.72% for m416 and
# called it a session effect. It is not: the store's early sessions fired
# uncompensated and the middle ones fired compensated, so "session" and "which
# curve was playing" were partly the same axis. MODEL.md sec.4's own nine-run
# table has the same flaw -- its first three runs are a pure comp-OFF arm
# (mean 821.1) and its middle four a pure compensated arm (865.4), 5.1% apart,
# and the whole spread was read as time.
#
# Split BOTH ways and it comes apart (MODEL.md sec.4.2):
#
#     compensated arm, between sessions   1.28% observed vs 1.09% sampling
#     comp-OFF arm,    between sessions   2.53% observed vs 1.23% sampling
#
# The compensated arm IS sampling noise -- so y_true has no session term, the
# gun is the gun. The excess sits only on the arm whose answer is 100%
# measurement (|y_obs| 830 counts against 36), which is where a MULTIPLICATIVE
# error lands and an additive one could not. That multiplier is K, drifting
# 1-2% per session.
#
# ⚠ "IT DEPENDS ON THE SESSION" IS NOT A MODEL, IT IS A LABEL FOR IGNORANCE. If
# y_true really were per-session there would be nothing to fit and nothing to
# play back tomorrow. Raised from the chair in exactly those terms, and the
# data agreed.
#
# ⚠ NO NUMBER HERE MEANS ANYTHING WITHOUT A BASELINE. A group holds 3..45
# magazines, so "two groups differ by 2%" is not a finding until sampling alone
# is shown to predict less. The baseline is computed from the WITHIN-group
# per-magazine CV, and for the shape comparison each group is also split
# alternately in two and fitted twice. This is sec.3's interleave-vs-replicate
# rule, pointed at the fitter instead of at the gun.
SESSION_GAP_MIN = 5.0            # same cut as MODEL.md sec.4's nine runs
SESSION_MIN_MAGS = 4             # below this a "session fit" is one magazine
SESSION_T_REF = 2.40             # where y_true is read, as in MODEL.md sec.4
ARM_ROUND = -2                   # curve totals binned to 100 counts


def _arm(m):
    """Which compensation arm this magazine was fired on, in counts."""
    return int(round(sum(k['dy'] for k in (m.curve or [])), ARM_ROUND))


def _y_at(m, t_ref=SESSION_T_REF):
    t, y = m.y_true_counts()
    if len(t) < 3 or t[-1] < t_ref:
        return None
    return float(np.interp(t_ref, t, y))


def _ts_minutes(ts):
    """'0808_143749' -> minutes. Returns None if it is not that shape."""
    try:
        d, hms = ts.split('_')
        h, m, s = int(hms[:2]), int(hms[2:4]), int(hms[4:6])
        return int(d[:2]) * 1440 + int(d[2:]) * 1440 + h * 60 + m + s / 60.0
    except (ValueError, IndexError, AttributeError):
        return None


def _sessions(mags, gap_min=SESSION_GAP_MIN):
    """Split the store's append order wherever the clock jumps by `gap_min`."""
    out, cur, prev = [], [], None
    for m in mags:
        t = _ts_minutes(getattr(m, 'ts', ''))
        if t is None:
            continue
        if prev is not None and t - prev > gap_min:
            out.append(cur)
            cur = []
        cur.append(m)
        prev = t
    if cur:
        out.append(cur)
    return out


def _curve(r):
    """A fit result -> (t seconds, cumulative counts)."""
    t = np.array([k['t_ms'] for k in r['knots']], float) / 1000.0
    return t, np.cumsum([k['dy'] for k in r['knots']])


def _compare(curves, grid):
    """-> (level per curve, shape matrix on `grid`). Shape is normalised by
    each curve's OWN level, so a pure scale difference vanishes from it."""
    lev, shp = [], []
    for t, y in curves:
        yi = np.interp(grid, t, y)
        lev.append(yi[-1])
        shp.append(yi / yi[-1] if yi[-1] else yi * np.nan)
    return np.array(lev), np.array(shp)


def by_session(mags, gap_min=SESSION_GAP_MIN, min_mags=SESSION_MIN_MAGS,
               t_ref=SESSION_T_REF):
    """Two-way: (session x curve arm). See the block comment above for why the
    arm axis is not optional."""
    cells = {}
    for s in _sessions(mags, gap_min):
        ts = getattr(s[0], 'ts', '?')
        for m in s:
            y = _y_at(m, t_ref)
            if y is not None:
                cells.setdefault((ts, _arm(m)), []).append((y, m))
    if not cells:
        print(f'[!] no magazine reaches t = {t_ref:.2f} s')
        return 2

    sessions = sorted({t for t, _ in cells})
    arms = sorted({a for _, a in cells})
    print(f'  y_true at t = {t_ref:.2f} s, sessions x curve arm '
          f'(cell = mean over n magazines)')
    print(f'{"session":>13} ' + ' '.join(f'{a:>10}' for a in arms))
    for t in sessions:
        row = f'{t:>13} '
        for a in arms:
            v = cells.get((t, a))
            row += (f'{np.mean([y for y, _ in v]):7.1f}/{len(v):<2d} '
                    if v else f'{"":>10} ')
        print(row)
    print('  (value / how many magazines).  arm = total counts in the curve '
          'that was playing; 0 means the compensation was off')

    # ── the decisive comparison: between sessions, WITHIN one arm ──
    print()
    print(f'{"arm":>22}  {"groups":>6}  {"per-mag CV":>10}  '
          f'{"sampling":>8}  {"observed":>8}  {"ratio":>6}')
    verdicts = {}
    for a in arms:
        g = [v for (t, aa), v in sorted(cells.items())
             if aa == a and len(v) >= 3]
        if len(g) < 3:
            continue
        means = np.array([np.mean([y for y, _ in v]) for v in g])
        ns = np.array([len(v) for v in g], float)
        wcv = float(np.sqrt(sum((len(v) - 1) * np.var([y for y, _ in v], ddof=1)
                                for v in g) / (ns - 1).sum()) / means.mean())
        pred = wcv / np.sqrt(ns.mean())
        obs = float(means.std(ddof=1) / means.mean())
        verdicts[a] = (obs, pred, len(g))
        print(f'{a:22d}  {len(g):6d}  {100*wcv:9.2f}%  {100*pred:7.2f}%  '
              f'{100*obs:7.2f}%  {obs/pred:5.2f}x')
    if not verdicts:
        print('  [!] no arm has 3 groups of >= 3 magazines. NOT A VERDICT — '
              'this store cannot separate session from arm, and reporting a '
              'session number from it would be the confound this check exists '
              'to catch.')
        return 4
    print('  sampling = the WITHIN-group per-magazine CV over sqrt(mean n). '
          'ratio ~1 means the between-session spread IS sampling noise, i.e. '
          'no session term at all.')

    print()
    comp = [(a, v) for a, v in verdicts.items() if a]
    off = verdicts.get(0)
    if comp:
        worst = max(r for _, (o, p, _) in comp for r in [o / p])
        if worst < 1.5:
            print(f'  ✅ NO SESSION TERM on the compensated arm(s): the '
                  f'between-session spread is sampling noise (worst ratio '
                  f'{worst:.2f}x). y_true is a property of the gun, not of '
                  f'the evening.')
        else:
            print(f'  ⚠ the compensated arm still scatters {worst:.2f}x more '
                  f'than sampling predicts — something session-dependent '
                  f'survives even where |y_obs| is small.')
    if off and comp:
        o, p, n = off
        excess = (o ** 2 - p ** 2) ** 0.5 if o > p else 0.0
        base = max(r for _, (oo, pp, _) in comp for r in [oo / pp])
        print(f'  comp-OFF arm: {100*o:.2f}% observed against {100*p:.2f}% '
              f'sampling -> {100*excess:.2f}% excess, on {n} groups.')
        if excess > 0.01 and o / p > 1.5 > base:
            print('  That excess appears ONLY where the answer is measurement '
                  '(|y_obs| is the whole of y_true there and near zero on the '
                  'compensated arm), which is where a MULTIPLICATIVE error '
                  'lands and an additive one could not. It is K, drifting per '
                  'session. MODEL.md sec.4.2.')
            print('  -> collect on the COMPENSATED arm. The closer the curve, '
                  'the smaller |y_obs|, the less of that drift reaches the '
                  'answer. It is a nulling measurement.')

    # ── shape, per arm, so pooling's cost is separated from the level's ──
    _shape_by_session(cells, arms, min_mags)
    return 0


def _shape_by_session(cells, arms, min_mags):
    """Does the CURVE SHAPE depend on the session, within one arm?"""
    for a in arms:
        groups = [(t, [m for _, m in v]) for (t, aa), v in sorted(cells.items())
                  if aa == a and len(v) >= min_mags]
        if len(groups) < 2:
            continue
        fits = []
        for t, ms in groups:
            r = fit(ms, window=0)
            if r['ok']:
                fits.append((t, ms, r))
        if len(fits) < 2:
            continue
        span = min(r['span_s'] for _, _, r in fits)
        grid = np.arange(0.0, span + 1e-9, 0.02)
        lev, shp = _compare([_curve(r) for _, _, r in fits], grid)
        obs = float(np.nanmean(np.nanstd(shp, axis=0, ddof=1))
                    / np.nanmean(np.abs(np.nanmean(shp, axis=0))))
        # baseline: halve each group and fit twice
        w = []
        for t, ms, _ in fits:
            if len(ms) < 2 * min_mags:
                continue
            hs = [fit(ms[0::2], window=0), fit(ms[1::2], window=0)]
            if not all(h['ok'] for h in hs):
                continue
            hg = np.arange(0.0, min(min(h['span_s'] for h in hs), span) + 1e-9,
                           0.02)
            _, s2 = _compare([_curve(h) for h in hs], hg)
            w.append(float(np.sqrt(np.nanmean((s2[1] - s2[0]) ** 2))))
        print()
        if not w:
            print(f'  SHAPE, arm {a}: {len(fits)} sessions, spread '
                  f'{100*obs:.2f}% — NO BASELINE (no group held '
                  f'{2*min_mags} magazines), so this number cannot be read.')
            continue
        samp = float(np.sqrt(np.mean(np.square(w)))) / 2.0
        eff = (obs ** 2 - samp ** 2) ** 0.5 if obs > samp else 0.0
        print(f'  SHAPE, arm {a}: {len(fits)} sessions   observed '
              f'{100*obs:.2f}%   sampling {100*samp:.2f}%   '
              f'session effect {100*eff:.2f}%  '
              f'= {eff*lev.mean():.1f} counts on a {lev.mean():.0f}-count curve')


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon')
    ap.add_argument('--config', default=None,
                    help='config key as stored, e.g. "bare"')
    ap.add_argument('--by-session', action='store_true',
                    help='cut the store by session and ask whether the fitted '
                         'curve depends on which session it came from')
    ap.add_argument('--gap-min', type=float, default=SESSION_GAP_MIN)
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
    if a.by_session:
        print(f'{a.weapon} {a.config or "bare"}: {len(mags)} magazines, '
              f'cut wherever the clock jumps {a.gap_min:.0f} min')
        return by_session(mags, gap_min=a.gap_min)
    r = fit(mags)
    if not r['ok']:
        print(r['why'])
        return 3
    print(f'{a.weapon} {a.config or "bare"}: {r["n_kept"]}/{r["n_total"]} '
          f'magazines, {len(r["knots"])} knots @ {r["grid_ms"]:.1f} ms')
    print(f'  total {r["total_counts"]:.1f} counts over {r["span_s"]:.2f} s')
    # Before the clustering line, because it happened before the clustering and
    # because `n_total` already excludes these -- printed after, "12/12" reads
    # like nothing was dropped.
    for b in r.get('not_bursts', ()):
        print(f'  [!] NOT A BURST, excluded before clustering: {b["ts"]} fell '
              f'{b["hold_drawdown"]:.1%} below its own running maximum while '
              f'the trigger was still down (gate {HOLD_DRAWDOWN_MAX:.0%}). '
              f'y_true only rises under the trigger.')
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
