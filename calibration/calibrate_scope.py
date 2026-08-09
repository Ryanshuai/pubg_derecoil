"""Measure what a magnified optic does to the compensation a gun needs.

    pixi run scope --weapon mp5k --sight 4x --analyse      # store only, offline
    pixi run scope --weapon mp5k --sight 4x --mags 4       # fires the gun
    pixi run scope-test                                    # offline self-test

⚠ THE SCOPE FACTOR IS NOT A FREE PARAMETER IN THIS REPOSITORY. It is derivable
from constants that are already in config.py, and this file exists to CHECK
that derivation rather than to fit around it. config.RECOIL_SIGHT_PROFILES says
it in one line -- "`mag / K` is what says how many counts a given VIEW ROTATION
costs" -- and recoil is a fixed angular kick, so

    y_true_counts(sight)      mag_s / K_s        mag_s * K_ref
    ────────────────────  =  ─────────────  =  ────────────────  =  r_pred
    y_true_counts(ref)        mag_r / K_r        mag_r * K_s

`--analyse` prints that number for the pair you ask about; on today's profiles
4x against the red dot lands near 3.3, which is where this repository's
folklore "about 3x" comes from. THE NUMBERS ARE NOT SPELLED OUT HERE, because
config owns them and a prose copy is a second author nothing keeps in step
(`pixi run params` enforces exactly that, and caught this line). THE POINT OF FIRING
ANYTHING IS TO TRY TO FALSIFY THAT. If it holds, every scoped curve is the red
dot's curve times a constant and no gun needs re-measuring through six optics;
if it fails, the sight is not orthogonal to the weapon and plan A's per-sight
curve key is load-bearing rather than merely cautious.

⚠ AND THE PREDICTION LEANS ON A K THAT WAS NEVER MEASURED. config.py's red-dot
entry carries pages of provenance; the 2x/3x/4x entries carry none. So a naive
r computed with the stored K would be testing the stored K against itself. This
file therefore produces TWO estimates that fail in different ways:

    r_stored   uses RECOIL_SIGHT_PROFILES[sight]['K'].  Precise, and only as
               true as that constant.
    K_solved   solved from the ARMS, using no sight constant at all (below).
               Assumption-free, and imprecise -- see the sem it prints.

Agreement between them is the result. Either alone is a number, not a finding.


HOW K FALLS OUT OF THE ARMS, WITH NO INJECTED MOTION
────────────────────────────────────────────────────
`harness/verdict.py`'s out-of-loop check already requires that magazines fired
under DIFFERENT compensation curves agree on y_true once the curve is added
back. Written out, y_true is affine in 1/K:

    y_true = y_obs_px / K + human + y_comp
             └── depends on the arm ──┘   └ known exactly, read off the firmware

Two arms have different y_obs_px AND different y_comp. Demanding that they land
on the same y_true is one equation in one unknown:

    K = (P₁ - P₂) / (D₂ - D₁)      P = Σ y_obs_px,  D = human + y_comp

So the arms that already exist to CHECK a cell also MEASURE the constant that
cell was analysed with. Nothing is injected, nothing extra is fired.

⚠ ITS PRECISION IS SET BY THE ARM SPACING, AND THE ARM SPACING IS NOT MINE TO
WIDEN. `harness/adapter.ARM_PLAN` is (True, True, 0.8, True, True) and the 0.8
is a measurement: wider spreads re-discover MODEL.md 6.1's open item (delivery
rising with hold duration) and the cells came apart at 8.1-8.8% when six arms
spanned 0..913 counts. A 0.2 lever on ~1000 counts is ~200 counts, so with n
per arm the standard error on K is roughly cv * y_true / (0.2 * y_true * sqrt(n))
= cv / (0.2 * sqrt(n)) -- about 6% at cv 2% and n=5, improving only as sqrt(n).
`--arm` exists to widen it and PRINTS THE WARNING, because a K measured on a
spread that is itself biased is not assumption-free after all.


WHAT HAS TO BE TRUE BEFORE ANY OF THIS MEANS ANYTHING
─────────────────────────────────────────────────────
⚠ EVERY ONE OF THE 885 MAGAZINES IN THE STORE TODAY IS `red_dot`. There is no
scoped data, so `--analyse` on a fresh store correctly reports that it cannot
answer, and that is the honest state rather than a failure.

⚠ `Magazine.sight` IS THE FLAG unless something read the gun. collect_timed
refuses when `read_sight` disagrees with `--sight`, so magazines written by
that path are checked -- but the CHECK happened in another process and the
record does not carry its own witness. `sight_asset` (added with this file)
is that witness: the raw optic asset read off the gun, stored beside the
profile name so a magazine can be audited without trusting the run that wrote
it. Root CLAUDE.md's second law, in one field.

⚠ `ads_frac` IS `nan` ON EVERY MAGAZINE and always will be on this path (the
timed grabber captures the tracker's patches, AdsDetector reads screen centre).
`ads_end` is the live witness, and this file REFUSES a pool where it is False:
out of the scope is exactly the failure that makes a scope measurement
meaningless, and it is worth the same ~3x as reading the wrong optic.
"""
import argparse
import os
import sys
from dataclasses import replace

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# ⚠ NOT COSMETIC. Without it this file dies on cp1252 the first time it prints
# a ⚠ -- which is the line warning that one gun does not establish the claim,
# i.e. the run reaches its conclusion and then throws while saying what the
# conclusion does not mean. Caught by running the red_dot-vs-red_dot control
# on 2026-08-09, which is what that control is for.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import config as cfg                                            # noqa: E402
from calibration import samples as S                            # noqa: E402

# Rule 10: everything in this package is imported as `calibration.X`. A bare
# `from samples import ...` loads the module a second time under a second name
# and produces two Magazine classes that duck-type as one.

# How many bootstrap resamples back the interval on a ratio. 2000 is where the
# third decimal stops moving on pools of the size this file sees (n <= 30); it
# is not a tuned number and nothing downstream reads it.
BOOTSTRAP = 2000

# The seed is FIXED so two runs of --analyse on the same store print the same
# interval. A confidence interval that moves when nothing moved is a number
# people learn to ignore.
BOOTSTRAP_SEED = 20260809

# A pair of arms is comparable with the best pair only if its lever is within
# this fraction of it. See solve_k_from_arms: below that, K = dP/dD divides by
# a difference the arms never really had, and the pair reports noise as signal.
# 0.5 rather than a tuned number -- the question it answers is "is this pair in
# the same league", and the real pools cluster at either ~1 count or ~500.
WELL_CONDITIONED = 0.5

# A sight name no profile has, so samples.analysis_k falls back to the stored
# K and this file can actually vary it. See y_true_at.
PROBE_SIGHT = '__calibrate_scope_probe__'

# Fractions of the common horizon at which the ratio is re-taken. A scope
# factor that is a constant is flat across these; one that is not is a
# different finding, and a single time cannot tell them apart. Starts at 0.4
# because the first frames are the burst finding its feet and y_true there is
# small enough that a ratio of two small numbers is mostly noise.
RATIO_TRAJECTORY = (0.4, 0.6, 0.8, 1.0)

# How much the ratio may move across those times and still be called one
# number. 5% matches the gate harness/verdict.py holds the arms to; the point
# is not the exact figure but that SOMETHING checks it, which is what the
# endpoint-only version had nothing of.
RATIO_FLAT = 0.05


# ════════════════════════════════════════════════════════════════════
# Pure analysis. No game, no hardware, no store -- everything below takes
# Magazine objects and returns numbers, which is what makes --selftest a
# test of the thing that runs rather than of a parallel copy of it.
# ════════════════════════════════════════════════════════════════════

def affine_in_inv_k(m, t_eval):
    """(P, D) such that y_true(t_eval) = P / K + D.  -> (float, float)

    ⚠ SOLVED BY EVALUATING THE REAL METHOD TWICE, NOT BY RE-DERIVING IT. y_true
    is affine in 1/K, so two points determine it exactly -- and taking those two
    points off `Magazine.y_true_counts()` means the oor mask, the human term,
    the comp_lag shift and the hold_s freeze are all whatever that method says
    they are. A second implementation here would be a second thing to keep in
    step, and this package has already paid for one of those (calibration/
    CLAUDE.md: `auto_calibrate` carried its own `analyse`).

    Returns (nan, nan) when the magazine cannot be evaluated at t_eval.
    """
    y1 = y_true_at(m, t_eval, K=1.0)
    y2 = y_true_at(m, t_eval, K=2.0)
    if not (np.isfinite(y1) and np.isfinite(y2)):
        return float('nan'), float('nan')
    # y(u) = P*u + D at u = 1 and u = 0.5
    P = 2.0 * (y1 - y2)
    return P, y1 - P


def y_true_at(m, t_eval, K=None):
    """This magazine's y_true at `t_eval` seconds after the click. -> float

    `K=None` uses whatever `samples.analysis_k` decides, which since
    2026-08-09 is the LIVE profile for the magazine's sight rather than the K
    the run was collected at. That is the right default and this file relies on
    it: pooling magazines collected under three superseded red-dot estimates is
    exactly what analysis_k exists to stop.

    ⚠ AND IT IS WHY OVERRIDING K NEEDS A SIGHT, NOT JUST A NUMBER. `mag.K` is
    now a RECORD, not a control -- analysis_k reads the profile off `mag.sight`
    and ignores the field whenever that profile exists. `replace(m, K=x)` alone
    therefore returns the SAME y_true for every x, P comes out 0, and the whole
    arm-solver silently answers zero. It did: this function was written against
    the older samples.py and the change landed underneath it mid-edit. The
    self-test went 12 red, which is the only reason this paragraph exists
    rather than a plausible K.
    """
    if K is None:
        mm = m
    else:
        # analysis_k's own documented escape hatch: no live profile -> the
        # stored K is used. Asserted rather than assumed, because a future
        # profile named this would re-break the sweep in exactly the silent
        # way it broke the first time.
        assert PROBE_SIGHT not in cfg.RECOIL_SIGHT_PROFILES, (
            f'{PROBE_SIGHT!r} became a real sight profile; pick another '
            f'sentinel or this function silently stops varying K')
        mm = replace(m, K=float(K), sight=PROBE_SIGHT)
    t, y = mm.y_true_counts()
    t = np.asarray(t, dtype=float)
    if t.size < 2 or not np.isfinite(t[-1]) or t_eval > t[-1] or t_eval < t[0]:
        return float('nan')
    return float(np.interp(t_eval, t, np.asarray(y, dtype=float)))


def common_horizon(pool):
    """The largest t every magazine in `pool` actually reached. -> float|nan

    The evaluation time has to be one no magazine has to be extrapolated to,
    and it has to be the SAME for both sights -- a ratio of y_true taken at two
    different times is a statement about the burst length, not about the optic.
    """
    ends = [float(np.asarray(m.t, dtype=float)[-1])
            for m in pool if len(m.t) >= 2]
    return min(ends) if ends else float('nan')


def arm_of(m):
    """A hashable label for which curve this magazine was fired under.

    The total commanded compensation IS the arm -- `comp_enabled` only
    separates zero from non-zero, and ARM_PLAN's arms are 1.0 and 0.8, which
    that flag cannot tell apart.
    """
    if not m.curve:
        return 0.0
    return round(sum(float(k.get('dy', 0.0)) for k in m.curve), 1)


def _sem(v):
    return (float(np.std(v, ddof=1) / np.sqrt(len(v)))
            if len(v) > 1 else float('nan'))


def _k_from_pair(groups, a, b):
    """K from exactly these two arms. -> (K, sem, lever)"""
    Pa = np.array([p for p, _ in groups[a]])
    Da = np.array([d for _, d in groups[a]])
    Pb = np.array([p for p, _ in groups[b]])
    Db = np.array([d for _, d in groups[b]])
    dD = Db.mean() - Da.mean()
    if abs(dD) < 1e-9:
        return float('nan'), float('nan'), 0.0
    return (float((Pa.mean() - Pb.mean()) / dD),
            float(abs(np.hypot(_sem(Pa), _sem(Pb)) / dD)), float(abs(dD)))


def solve_k_from_arms(pool, t_eval, allow_zero=False):
    """K implied by demanding the arms agree on y_true. -> dict

    -> {'ok', 'K', 'sem', 'arms', 'lever', 'pairs', 'spread', 'why'}

    ⚠ EVERY RETURN CARRIES THE SAME KEYS, including the refusals. The first
    version left `strong` off the two early exits, so a caller that reached for
    it after a refusal got KeyError instead of an empty list -- and the refusal
    path is exactly the one nobody exercises. Same lesson as `_await` in
    control/inventory.py: a function whose shape depends on its outcome makes
    every consumer branch on success before it can read anything.

    ⚠ THE WIDEST PAIR IS NOT THE BEST PAIR, AND THIS IS THE ONE PLACE THE
    ESTIMATOR CAN QUIETLY STOP BEING ASSUMPTION-FREE. Its whole basis is that
    arms agree on y_true -- and `harness/adapter.ARM_PLAN` records that they
    DO NOT when the spread is wide: six arms spanning 0..913 counts put the
    vector's cells 8.1-8.8% apart, y_true rising monotonically with commanded
    compensation, slope growing with t (MODEL.md 6.1, still open). A wide lever
    therefore buys precision by absorbing that effect INTO K.

    So two things, both of which the first draft of this function got wrong by
    reaching for the widest lever available:

      * the ZERO arm is excluded by default. ARM_PLAN names it "the worst
        choice available" -- its |y_obs| is the entire recoil, so it is the arm
        furthest from the null where this measurement is precise, and it is one
        end of every widest pair.
      * every usable pair is solved and the SPREAD is reported. If K is a
        constant the pairs agree; if they fan out with the lever, what is being
        measured is the delivery effect and `spread` says so instead of a
        single confident number hiding it.

    `lever` is |D₂ - D₁| in counts -- the denominator. A small one is why an
    answer is noisy, and printing it says what to change.
    """
    groups = {}
    for m in pool:
        P, D = affine_in_inv_k(m, t_eval)
        if np.isfinite(P) and np.isfinite(D):
            groups.setdefault(arm_of(m), []).append((P, D))
    keys = sorted(k for k in groups if allow_zero or abs(k) > 1e-9)
    dropped = sorted(set(groups) - set(keys))
    if len(keys) < 2:
        return {'ok': False, 'K': float('nan'), 'sem': float('nan'),
                'arms': keys, 'lever': 0.0, 'pairs': [], 'strong': [],
                'spread': float('nan'), 'dropped': dropped,
                'why': f'{len(keys)} usable arm(s); K needs two. A pool fired '
                       f'on one curve cannot measure the constant it was '
                       f'analysed with.'
                       + (f' (zero arm(s) {dropped} excluded -- pass '
                          f'allow_zero=True if that is really wanted)'
                          if dropped else '')}
    pairs = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            K, sem, lever = _k_from_pair(groups, a, b)
            if np.isfinite(K) and lever > 0:
                pairs.append({'arms': (a, b), 'K': K, 'sem': sem,
                              'lever': lever})
    if not pairs:
        return {'ok': False, 'K': float('nan'), 'sem': float('nan'),
                'arms': keys, 'lever': 0.0, 'pairs': [], 'strong': [],
                'spread': float('nan'), 'dropped': dropped,
                'why': 'every arm commanded the same total compensation'}
    # The single answer is the best-conditioned pair among the ones that are
    # allowed -- not the average, because a 12-count lever and a 200-count one
    # are not two measurements of equal worth.
    best = max(pairs, key=lambda p: p['lever'])
    # ⚠ THE SPREAD IS ONLY READABLE ACROSS WELL-CONDITIONED PAIRS, and the
    # first version of this line did not say so. K = dP/dD, so a pair whose
    # arms commanded within a few counts of each other divides by ~0 and
    # returns anything. On the real mp5k pool -- 139 magazines, arms at 917,
    # 918, 941, 943, 945, 948 -- 45 pairs exist and the raw spread came out
    # 983% of K, printed under the words "that is the arms disagreeing, not
    # noise". IT WAS NEITHER. It was six one-count levers.
    #
    # Arithmetically correct, self-consistent, and blind to the only thing it
    # was built to see (root CLAUDE.md: a criterion has to be able to see the
    # dimension it manages). Restricting to pairs with at least half the best
    # lever is what makes a large spread mean what the message says it means.
    strong = [p for p in pairs if p['lever'] >= WELL_CONDITIONED * best['lever']]
    Ks = [p['K'] for p in strong]
    return {'ok': True, 'K': best['K'], 'sem': best['sem'],
            'arms': keys, 'lever': best['lever'], 'pairs': pairs,
            'strong': strong,
            'spread': float(max(Ks) - min(Ks)) if len(Ks) > 1 else 0.0,
            'dropped': dropped,
            'why': None, 'n': {a: len(groups[a]) for a in best['arms']}}


def pool_y_true(pool, t_eval, K=None):
    """Every magazine's y_true at t_eval. -> np.ndarray (nan dropped)"""
    v = np.array([y_true_at(m, t_eval, K=K) for m in pool], dtype=float)
    return v[np.isfinite(v)]


def scope_ratio(ref, scoped, t_eval, K_ref=None, K_scope=None, seed=None):
    """y_true(scope) / y_true(ref), with a bootstrap interval. -> dict

    The ratio is of MEDIANS, not means: this store's known contaminants are
    whole magazines (a cell that changed guns, a burst that left the scope) and
    they arrive as outliers, which is the one thing calibration/CLAUDE.md says
    to expect. `MODEL.md`'s own rule -- the unit of an outlier is a magazine.
    """
    a = pool_y_true(ref, t_eval, K=K_ref)
    b = pool_y_true(scoped, t_eval, K=K_scope)
    if len(a) < 2 or len(b) < 2:
        return {'ok': False, 'r': float('nan'), 'lo': float('nan'),
                'hi': float('nan'), 'n_ref': len(a), 'n_scope': len(b),
                'why': 'each sight needs at least two usable magazines'}
    rng = np.random.default_rng(BOOTSTRAP_SEED if seed is None else seed)
    boot = np.empty(BOOTSTRAP)
    for i in range(BOOTSTRAP):
        boot[i] = (np.median(rng.choice(b, len(b), replace=True))
                   / np.median(rng.choice(a, len(a), replace=True)))
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return {'ok': True, 'r': float(np.median(b) / np.median(a)),
            'lo': float(lo), 'hi': float(hi),
            'y_ref': float(np.median(a)), 'y_scope': float(np.median(b)),
            'n_ref': len(a), 'n_scope': len(b), 'why': None}


def predicted_ratio(sight, ref='red_dot'):
    """r_pred from config alone. -> (r, why|None)

    Returns (nan, why) when either profile has no K -- which is the honest
    answer for a sight nobody has measured, and NOT a default. `Rig` falls back
    to RECOIL_K_DEFAULT_SCOPED for an unknown profile; borrowing that here
    would answer with the magnified group's constant and call it a prediction.
    """
    ps = cfg.RECOIL_SIGHT_PROFILES.get(sight)
    pr = cfg.RECOIL_SIGHT_PROFILES.get(ref)
    if not ps or not pr:
        missing = sight if not ps else ref
        return float('nan'), f'no K profile for {missing!r}'
    try:
        return ((ps['mag'] * pr['K']) / (pr['mag'] * ps['K'])), None
    except (KeyError, ZeroDivisionError) as e:                 # noqa: BLE001
        return float('nan'), f'profile is unusable: {e}'


def audit(pool):
    """Everything about this pool that would make its numbers mean something
    other than what they say. -> list[str], empty when clean.

    ⚠ THIS IS THE HALF THAT MATTERS. calibration/CLAUDE.md's table of five
    failures is five different ways a record described one object while the
    measurement came off another, and every one of them printed normal numbers.
    A scope measurement is the worst case for all five at once, because the
    thing being varied is exactly the thing that was never recorded.
    """
    out = []
    dropped = sum(1 for m in pool if m.ads_end is False)
    if dropped:
        out.append(f'{dropped}/{len(pool)} magazine(s) ended OUT of the sight '
                   f'(ads_end False). A burst that left the scope was analysed '
                   f'with the scope\'s K -- worth about 3x, and it is the exact '
                   f'quantity this file varies.')
    unread = sum(1 for m in pool if not getattr(m, 'sight_asset', ''))
    if unread:
        out.append(f'{unread}/{len(pool)} magazine(s) carry no `sight_asset`, '
                   f'so `sight` is the FLAG with no witness beside it. They were '
                   f'checked when they were written; nothing can re-check them '
                   f'now.')
    sights = {m.sight for m in pool}
    if len(sights) > 1:
        out.append(f'pool mixes sights {sorted(sights)} -- split it first')
    postures = {m.posture for m in pool}
    if len(postures) > 1:
        out.append(f'pool mixes postures {sorted(postures)}')
    sizes = {m.magazine_size for m in pool if m.magazine_size}
    if len(sizes) > 1:
        out.append(f'pool mixes magazine sizes {sorted(sizes)} -- a shorter '
                   f'burst is a different y_true(t), not a noisier one')
    left = [m for m in pool if getattr(m, 'rounds_left', None)]
    if left:
        out.append(f'{len(left)}/{len(pool)} magazine(s) had rounds LEFT when '
                   f'the trigger came up; those bursts are short')
    return out


# ════════════════════════════════════════════════════════════════════
# Report
# ════════════════════════════════════════════════════════════════════

def analyse(weapon, sight, ref, config=None, at=None, verbose=True):
    """Read the store and answer. -> dict. Touches no game and no hardware."""
    pool_all = S.load(weapon, config)
    ref_pool = [m for m in pool_all if m.sight == ref]
    sc_pool = [m for m in pool_all if m.sight == sight]
    rec = {'weapon': weapon, 'sight': sight, 'ref': ref,
           'n_ref': len(ref_pool), 'n_scope': len(sc_pool)}
    r_pred, why_pred = predicted_ratio(sight, ref)
    rec['r_pred'], rec['r_pred_why'] = r_pred, why_pred

    if verbose:
        print(f'\n=== {weapon} {S.config_key(config or {})} '
              f'{sight} vs {ref} ===')
        print(f'  stored magazines: {ref} {len(ref_pool)}, '
              f'{sight} {len(sc_pool)}')
        if np.isfinite(r_pred):
            ps = cfg.RECOIL_SIGHT_PROFILES[sight]
            pr = cfg.RECOIL_SIGHT_PROFILES[ref]
            print(f'  prediction from config: mag {ps["mag"]}/{pr["mag"]}, '
                  f'K {ps["K"]}/{pr["K"]}  ->  r_pred = {r_pred:.4f}')
        else:
            print(f'  prediction: UNAVAILABLE -- {why_pred}')

    if not sc_pool:
        rec['ok'] = False
        rec['why'] = (f'no magazine in the store was fired through {sight!r}. '
                      f'Nothing here is an opinion about the optic yet; fire '
                      f'some (drop --analyse).')
        if verbose:
            print(f'  [!] {rec["why"]}')
        return rec

    for name, pool in ((ref, ref_pool), (sight, sc_pool)):
        for line in audit(pool):
            rec.setdefault('warnings', []).append(f'{name}: {line}')
            if verbose:
                print(f'  [!] {name}: {line}')

    t_eval = at if at is not None else common_horizon(ref_pool + sc_pool)
    rec['t_eval'] = float(t_eval)
    if not np.isfinite(t_eval):
        rec['ok'] = False
        rec['why'] = 'no magazine carries a usable time axis'
        return rec
    if verbose:
        print(f'  evaluating y_true at t = {t_eval:.3f} s '
              f'(the largest time EVERY magazine reached)')

    ks = {}
    for name, pool in ((ref, ref_pool), (sight, sc_pool)):
        k = solve_k_from_arms(pool, t_eval)
        ks[name] = k
        stored = cfg.RECOIL_SIGHT_PROFILES.get(name, {}).get('K')
        if verbose:
            if k['ok']:
                off = ((k['K'] / stored - 1) * 100) if stored else float('nan')
                print(f'  K[{name}] = {k["K"]:.4f} +- {k["sem"]:.4f}  '
                      f'(best of {len(k["pairs"])} arm pair(s), '
                      f'lever {k["lever"]:.0f} counts)   '
                      f'stored {stored}  ({off:+.1f}%)')
                if len(k['strong']) > 1:
                    # ⚠ THE SPREAD IS THE RESULT WHENEVER IT IS BIG. K is a
                    # constant or it is not; pairs that fan out mean the arms
                    # disagree about y_true, which is MODEL.md 6.1 and not a
                    # noisier K. Only the well-conditioned pairs are counted --
                    # see WELL_CONDITIONED for the 983% this line once printed.
                    print(f'      {len(k["strong"])} of {len(k["pairs"])} pairs '
                          f'are well conditioned; they span {k["spread"]:.4f} '
                          f'({100 * k["spread"] / abs(k["K"]):.1f}% of K)'
                          + ('  ⚠ that is the arms disagreeing, not noise'
                             if abs(k['spread']) > 0.05 * abs(k['K']) else ''))
                if k['dropped']:
                    print(f'      zero arm(s) {k["dropped"]} excluded '
                          f'(ARM_PLAN: the worst choice available)')
            else:
                print(f'  K[{name}] not solvable: {k["why"]}')
    rec['K_solved'] = {n: (v['K'] if v['ok'] else None) for n, v in ks.items()}

    rec['stored'] = scope_ratio(ref_pool, sc_pool, t_eval)
    if verbose and rec['stored']['ok']:
        s = rec['stored']
        print(f'\n  r (stored K)  = {s["r"]:.4f}  '
              f'[{s["lo"]:.4f}, {s["hi"]:.4f}]   '
              f'y_true {s["y_ref"]:.0f} -> {s["y_scope"]:.0f} counts')

    # ⚠ AND THE SAME RATIO ACROSS THE BURST, BECAUSE ONE TIME IS AN ENDPOINT
    # READ. Committed 2026-08-09 with a single t_eval, and the very next commit
    # in this repository retracted a cell that "agreed to 0.11%" -- 0.11% was
    # y_true at the LAST sample, while the arms were up to 7% apart mid-burst
    # and converged to nothing at the end. That was the THIRD instance of the
    # same error here ("y_true is an inverted U", "the gain is 0.92"), all
    # arithmetically correct, all blind to the question.
    #
    # A scope factor that is a constant is flat in t. One that is not is a
    # different finding, and a single number cannot tell them apart.
    traj = rec['trajectory'] = []
    for frac in RATIO_TRAJECTORY:
        t = t_eval * frac
        rr = scope_ratio(ref_pool, sc_pool, t)
        if rr['ok']:
            traj.append({'t': t, 'r': rr['r'], 'n': (rr['n_ref'], rr['n_scope'])})
    if verbose and len(traj) > 1:
        rs = [p['r'] for p in traj]
        span = (max(rs) - min(rs)) / abs(rec['stored']['r'] or 1)
        print('  r across the burst: '
              + '  '.join(f'{p["t"]:.2f}s {p["r"]:.3f}' for p in traj))
        print(f'      spans {100 * span:.1f}% of r'
              + ('   ⚠ NOT a constant -- the endpoint alone cannot see this'
                 if span > RATIO_FLAT else '   (flat, so one number is honest)'))
        rec['flat'] = bool(span <= RATIO_FLAT)

    if all(ks[n]['ok'] for n in (ref, sight)):
        rec['solved'] = scope_ratio(ref_pool, sc_pool, t_eval,
                                    K_ref=ks[ref]['K'], K_scope=ks[sight]['K'])
        if verbose and rec['solved']['ok']:
            s = rec['solved']
            print(f'  r (solved K)  = {s["r"]:.4f}  '
                  f'[{s["lo"]:.4f}, {s["hi"]:.4f}]   '
                  f'<- uses no sight constant')

    if verbose and np.isfinite(r_pred) and rec['stored']['ok']:
        s = rec['stored']
        inside = s['lo'] <= r_pred <= s['hi']
        print(f'\n  r_pred {r_pred:.4f} is {"INSIDE" if inside else "OUTSIDE"} '
              f'the stored-K interval.')
        print('  ' + ('The optic is a constant on this gun: a scoped curve is '
                      'the red dot\'s times r.' if inside else
                      'The optic is NOT a plain constant here -- plan A\'s '
                      'per-sight curve key is load-bearing. Do not derive '
                      'scoped curves; measure them.'))
        print('  ⚠ ONE GUN IS NOT THE CLAIM. r_pred has no weapon in it, so a '
              'single agreeing gun is consistent with it and does not '
              'establish it. Repeat on a second gun before believing it.')
        rec['agrees'] = bool(inside)
    rec['ok'] = True
    return rec


# ════════════════════════════════════════════════════════════════════
# Live
# ════════════════════════════════════════════════════════════════════

def run(a):
    """Fire alternating blocks of `--sight` and `--ref`. -> exit code"""
    from calibration.collect_timed import collect_into_store, ensure_sight
    from calibration.sweep import Rig
    from control.inventory import InventoryControl
    from control.session import ensure_ready
    from control.spawner import SpawnerControl
    from control.stock import ensure_weapon_in_hand

    # Rule 9. Focus alone is one of five, and the other four each look like
    # success from outside.
    if not ensure_ready(label=f'the {a.sight} scope calibration')['ok']:
        return 1

    config = {}
    # ⚠ TWO ARMS, EQUAL SHARE, and that is not the night plan on purpose.
    # calibration/CLAUDE.md: `agree_spread` weights every arm the same however
    # many magazines it holds, so ARM_PLAN's (True, True, 0.8, True, True)
    # gives arm n = 1/2/6/6 and spread 19.7%, where a balanced two-arm sweep on
    # the same gun and the same store gave 0.11%. K here is solved FROM the arm
    # difference, so an arm of one magazine is a lever measured once.
    arm_plan = (True, a.arm)
    if a.arm == 1.0:
        # Not a warning: two identical arms make dD zero and solve_k_from_arms
        # refuses. Better to say so before firing than after.
        print('[!] ABORT: --arm 1.0 is the same curve twice. K is solved from '
              'the DIFFERENCE between the arms, so identical arms have no '
              'lever and the run would produce nothing this file can read.')
        return 5
    if a.arm != 0.8:
        print(f'  ⚠ arm spacing {a.arm} is not ARM_PLAN\'s 0.8. A wider spread '
              f'conditions K better AND re-discovers MODEL.md 6.1\'s delivery '
              f'effect, which biases the very number it sharpens.')

    rig = Rig(a.ref)
    try:
        with InventoryControl() as ac, SpawnerControl() as sc:
            slot = ensure_weapon_in_hand(ac, sc, weapon=a.weapon)
            if not slot:
                print(f'[!] ABORT: could not get a {a.weapon} in hand.')
                return 2
            # ⚠ ALTERNATING BLOCKS, NOT ALL OF ONE THEN ALL OF THE OTHER.
            # calibration/CLAUDE.md: the run-level multiplicative error is
            # constant within a run and cancels in a RATIO -- but only if both
            # sights are exposed to the same stretch of it. AAAA BBBB puts the
            # sight and the session drift on the same axis and nothing can
            # separate them afterwards.
            order = [a.ref, a.sight] * a.blocks
            for i, sight in enumerate(order):
                print(f'\n--- block {i + 1}/{len(order)}: {sight} ---')
                worn, asset = ensure_sight(ac, sc, slot, a.weapon, sight)
                if worn is None:
                    print(f'[!] ABORT: {asset}')
                    return 3
                print(f'  sight: {worn} (read back off the gun)')
                rig.set_sight(worn)
                fired, err = collect_into_store(
                    rig, a.weapon, config, a.posture, a.mags, arm_plan,
                    note_prefix=f'scope {a.sight}v{a.ref} ', scope_asset=asset)
                print(f'  fired {fired} magazine(s)'
                      + (f' -- {err}' if err else ''))
                if err and not fired:
                    return 4
    finally:
        rig.close()

    analyse(a.weapon, a.sight, a.ref, config, at=a.at)
    return 0


# ════════════════════════════════════════════════════════════════════
# Self-test -- offline, and it has to BITE
# ════════════════════════════════════════════════════════════════════

def _fake(sight, K, y_true_counts, arm, n_frames=60, span=2.4, seed=0,
          noise=0.0, ads_end=True, asset='FAKE_C'):
    """A magazine whose y_true is EXACTLY `y_true_counts` at t=span.

    Built backwards from the answer: choose the curve (the arm), then choose
    the screen motion that makes y_obs + y_comp land on the target. That is
    what makes this a test -- the expected value is an input, not something
    re-derived by the same arithmetic being checked.
    """
    rng = np.random.default_rng(seed)
    t = np.linspace(0.0, span, n_frames)
    # A curve delivering `arm * y_true_counts` linearly across the burst.
    total_comp = arm * y_true_counts
    knots = 24
    dt = span * 1000.0 / knots
    curve = [{'i': i, 't_ms': int(i * dt), 'dy': total_comp / knots,
              'dx': 0.0, 'dur_ms': int(dt)} for i in range(knots)]
    # y_obs must supply the rest. y_obs is NEGATIVE of the compensation's help:
    # the screen only shows what the curve failed to cancel.
    want_obs = y_true_counts - total_comp
    step = want_obs / (n_frames - 1)
    dy_px = np.full(n_frames - 1, step * K)
    if noise:
        dy_px = dy_px + rng.normal(0.0, noise * abs(step * K), n_frames - 1)
    return S.Magazine(
        weapon='fake', sight=sight, K=K, config={}, posture='standing',
        curve=curve, comp_enabled=True,
        t=[float(x) for x in t], dy_px=[float(x) for x in dy_px],
        human_dy=[0.0] * (n_frames - 1), oor=[False] * (n_frames - 1),
        magazine_size=40, hold_s=float(span) + 1.0,
        comp_lag_s=0.0, fire_delay_ms=0.0, fps=n_frames / span,
        ads_end=ads_end, ts='0101_000000', note='selftest',
        sight_asset=asset)


# The self-test's planted K values. Named rather than repeated as literals so
# `pixi run params` can see they are one decision -- and taken from the live
# profiles because the point of a PLANTED value is that the estimator gets it
# back, whatever it is. If a profile moves, these move with it and the test
# still means the same thing.
K_REF = cfg.RECOIL_SIGHT_PROFILES['red_dot']['K']
K_4X = cfg.RECOIL_SIGHT_PROFILES['4x']['K']
# One gun's worth of recoil, and three times it: the numbers are arbitrary --
# only their RATIO is asserted, and it is asserted against PLANT_R.
PLANT_Y = 1000.0
PLANT_R = 3.0

FAILS = []


def _check(name, got, want, tol=None):
    ok = (abs(got - want) <= tol) if (tol is not None and
                                      isinstance(got, float)) else got == want
    print(f'  {"ok  " if ok else "FAIL"}  {name:<52} {got!r}'
          + ('' if ok else f'  != {want!r}'
             + (f' (tol {tol})' if tol else '')))
    if not ok:
        FAILS.append(name)


def selftest():
    print('\n=== y_true is affine in 1/K, and we recover both coefficients ===')
    m = _fake('red_dot', K=K_REF, y_true_counts=1000.0, arm=0.8)
    P, D = affine_in_inv_k(m, 2.4)
    # y_obs = 200 counts at K -> P = 200 * K px; D = the commanded 800.
    _check('P is the pixel travel', P, 200.0 * K_REF, tol=1.0)
    _check('D is the commanded compensation', D, 800.0, tol=1.0)
    _check('and putting K back gives y_true', P / K_REF + D, PLANT_Y, tol=0.5)

    print('\n=== K falls out of two arms, with no sight constant ===')
    pool = ([_fake('red_dot', K_REF, PLANT_Y, 1.0, seed=i) for i in range(4)]
            + [_fake('red_dot', K_REF, PLANT_Y, 0.8, seed=10 + i)
               for i in range(4)])
    k = solve_k_from_arms(pool, 2.4)
    _check('solved K', round(k['K'], 4), K_REF, tol=0.002)
    _check('lever is the commanded difference', round(k['lever']), 200.0, tol=1)
    # ⚠ THE NEGATIVE THAT MAKES IT A TEST: one arm cannot do it, and the
    # failure has to be a REFUSAL rather than a number. A pool fired on one
    # curve is exactly the shape the night path produces when ARM_PLAN is
    # misconfigured, and answering it would be answering with 0/0.
    one = solve_k_from_arms(pool[:4], 2.4)
    _check('one arm refuses', one['ok'], False)
    _check('...and says why', 'needs two' in (one['why'] or ''), True)

    # ⚠ THE ZERO ARM IS EXCLUDED, AND THAT HAS TO BE TESTED BOTH WAYS, because
    # the first draft reached for the widest lever and the widest lever is
    # always the one ending at zero -- the arm ARM_PLAN calls the worst choice
    # available. A pool of {zero, one real arm} therefore has to REFUSE rather
    # than answer confidently off a 1000-count lever.
    zeroish = ([_fake('red_dot', K_REF, PLANT_Y, 0.0, seed=i) for i in range(3)]
               + [_fake('red_dot', K_REF, PLANT_Y, 1.0, seed=9 + i)
                  for i in range(3)])
    z = solve_k_from_arms(zeroish, 2.4)
    _check('a pool that is only {zero, one arm} refuses', z['ok'], False)
    _check('...and names the zero arm it dropped', z['dropped'], [0.0])
    _check('...but allow_zero=True still answers',
           round(solve_k_from_arms(zeroish, 2.4, allow_zero=True)['K'], 3),
           round(K_REF, 3), tol=0.002)
    # And with three real arms the spread across pairs must be ~0 when K really
    # is a constant -- that is what makes a NON-zero spread readable as the
    # arms disagreeing rather than as this function being noisy.
    three = (pool + [_fake('red_dot', K_REF, PLANT_Y, 0.6, seed=30 + i)
                     for i in range(4)])
    t3 = solve_k_from_arms(three, 2.4)
    _check('three arms give three pairs', len(t3['pairs']), 3)
    _check('...and a constant K spreads by nothing',
           round(t3['spread'], 6), 0.0, tol=1e-6)
    # ⚠ THE REGRESSION FOR THE 983% BUG. Two arms 1 count apart make a pair
    # whose K is meaningless; if it counts toward the spread, the report says
    # "the arms are disagreeing" about a pool where they are not. Planted with
    # a real 200-count lever plus a 1-count one, and the noise on the tiny
    # lever is what blows its K up -- exactly as it did on the live mp5k pool.
    # ⚠ THE PATHOLOGY IS PLANTED, NOT LEFT TO THE RNG: this arm sits 1 count
    # away from 0.8's in COMMANDED compensation while its y_true is 5 counts
    # higher -- which is what a real pool looks like, because magazine-to-
    # magazine scatter (cv ~2% = ~20 counts) is far larger than a 1-count
    # lever. K = dP/dD then divides a real disagreement by a lever that is not
    # there. Leaving this to noise made the check pass for the wrong reason
    # once already: the seeds happened to land near the truth.
    # It is planted NEXT TO THE TOP ARM (999 against 1000) so the best lever
    # -- 800 to 1000 -- stays clean. That separation is the point: the check
    # below asserts the ill-conditioned pair is discarded AND that discarding
    # it leaves the answer intact, and those are two different claims that a
    # polluted best pair would merge into one.
    illc = (pool + [_fake('red_dot', K_REF, PLANT_Y + 5.0, 999.0 / (PLANT_Y + 5.0),
                          seed=70 + i) for i in range(4)])
    ic = solve_k_from_arms(illc, 2.4)
    tiny = [p for p in ic['pairs'] if p['lever'] < 10]
    allK = [p['K'] for p in ic['pairs']]
    _check('the 1-count lever produces a pair', len(tiny), 1)
    # The raw material of the bug: that pair's K is not slightly off, it is
    # somewhere else entirely. If this ever stops being true the regression
    # below is passing for the wrong reason.
    #
    # `tiny[0] if tiny else` and not a bare index -- with the arm label
    # collapsed to a flag there is no 1-count pair, and IndexError here takes
    # the eight checks after it down with it. Every assertion has to survive
    # the mutation it tests for or it only reports when it is not needed.
    _check('...whose K is wild',
           abs((tiny[0]['K'] if tiny else K_REF) - K_REF) > 1.0, True)
    _check('...and it is excluded from `strong`',
           any(p['lever'] < 10 for p in ic['strong']), False)
    _check('...so the reported spread is small',
           ic['spread'] < 0.05 * ic['K'], True)
    _check('...while the raw spread would have screamed',
           bool(allK) and (max(allK) - min(allK)) > ic['K'], True)
    _check('...and the answer still comes off the good lever',
           round(ic['K'], 3), round(K_REF, 3), tol=0.01)

    print('\n=== the ratio recovers a planted scope factor ===')
    # Plant r = 3.0: the same gun needs 3x the counts through the scope. Both
    # sides are built with their OWN K, which is what a real pair of pools is.
    ref = [_fake('red_dot', K_REF, PLANT_Y, 1.0 if i % 2 else 0.8, seed=i,
                 noise=0.02) for i in range(6)]
    sco = [_fake('4x', K_4X, PLANT_Y * PLANT_R, 1.0 if i % 2 else 0.8, seed=50 + i,
                 noise=0.02) for i in range(6)]
    r = scope_ratio(ref, sco, 2.4)
    _check('r', round(r['r'], 3), 3.0, tol=0.02)
    _check('the interval contains it', r['lo'] <= 3.0 <= r['hi'], True)
    # And solving K per side first must give the SAME answer, because the
    # planted K is the stored one here.
    kr = solve_k_from_arms(ref, 2.4)['K']
    ksc = solve_k_from_arms(sco, 2.4)['K']
    r2 = scope_ratio(ref, sco, 2.4, K_ref=kr, K_scope=ksc)
    _check('r via solved K agrees', round(r2['r'], 3), round(r['r'], 3),
           tol=0.02)

    # ⚠ THE MEDIAN IS THE POINT, AND NOTHING TESTED IT UNTIL A MUTATION SAID
    # SO. Swapping np.median for np.mean in the bootstrap left this suite
    # GREEN, because synthetic pools are symmetric and the two agree on them.
    # calibration/CLAUDE.md's contaminants are not symmetric: they are WHOLE
    # magazines that measured something else (a cell that changed guns came out
    # 2.07x), so the test has to contain one.
    dirty = sco + [_fake('4x', K_4X, PLANT_Y * 3 * PLANT_R, 0.8, seed=99)]
    rd = scope_ratio(ref, dirty, 2.4)
    _check('one 3x-wrong magazine does not move the ratio',
           round(rd['r'], 2), 3.0, tol=0.1)
    # ⚠ AND THE INTERVAL TOO, because `r` and the bootstrap are two separate
    # expressions: swapping the bootstrap's median for a mean left `r` correct
    # and only moved lo/hi, so asserting on `r` alone reported green on a
    # resampler that had stopped being robust.
    _check('...nor the interval it is quoted with', rd['hi'] < 3.5, True)
    mean_r = float(np.mean(pool_y_true(dirty, 2.4))
                   / np.mean(pool_y_true(ref, 2.4)))
    _check('...where a mean would have moved a long way', mean_r > 3.5, True)

    print('\n=== a ratio that MOVES is not the same finding as one that does not ===')
    # ⚠ THE REGRESSION FOR AN ERROR THIS REPOSITORY HAS NOW MADE FOUR TIMES.
    # The scoped pool below is built so the two sights agree at the END of the
    # burst and disagree in the middle -- exactly the shape that made a cell
    # read "0.11%" at the last sample while the arms were 7% apart mid-burst
    # (commit ca51fb0, the day this file landed). A ratio taken at one time is
    # the one number in the trajectory that cannot see it.
    # ⚠ ARMS 0.8 AND 0.6, NOT 1.0: at arm 1.0 `_fake` gives the compensation the
    # whole of y_true, so dy_px is identically zero and there is no screen
    # motion to bend. That is a property of the fake, not of the game -- but it
    # is exactly the sort of thing that makes a test construct silently
    # degenerate, so it is named rather than worked around.
    # The same TOTAL screen motion, delivered in the first third of the burst
    # instead of evenly. y_true therefore lands on the same endpoint by
    # construction and takes a different path to it -- which is the whole
    # failure mode in one object.
    #
    # ⚠ ARMS 0.4/0.2 AND NOT ARM_PLAN'S: at arm 1.0 `_fake` gives the
    # compensation the whole of y_true, dy_px is identically zero, and there is
    # no screen motion left to bend. A weak arm is what puts the answer on the
    # screen -- which is also why ARM_PLAN does not use one, and why this is a
    # statement about the test construct rather than about how to fire.
    bent = []
    for i in range(6):
        m = _fake('4x', K_4X, PLANT_Y * PLANT_R, 0.4 if i % 2 else 0.2,
                  seed=200 + i)
        d = np.asarray(m.dy_px, dtype=float)
        w = np.zeros(len(d))
        w[:max(1, int(len(d) * 0.35))] = 1.0
        bent.append(replace(m, dy_px=[float(x) for x in
                                      w * (float(np.sum(d)) / float(np.sum(w)))]))
    end_r = scope_ratio(ref, bent, 2.4)['r']
    mid_r = scope_ratio(ref, bent, 2.4 * 0.6)['r']
    _check('the endpoint says it is the plain ratio', round(end_r, 2), PLANT_R,
           tol=0.05)
    _check('...while the middle of the burst does not',
           abs(mid_r - end_r) > RATIO_FLAT * PLANT_R, True)
    # ⚠ AND THE CONSTANT ITSELF, because the two checks above call scope_ratio
    # directly and would stay green with RATIO_TRAJECTORY cut back to (1.0,) --
    # i.e. with the report returned to exactly the endpoint-only shape this
    # section exists to prevent. Verified by mutation: without this line that
    # cut is invisible.
    _check('the report looks at more than the endpoint',
           len(RATIO_TRAJECTORY) > 1, True)
    _check('...including well before it', min(RATIO_TRAJECTORY) <= 0.5, True)

    print('\n=== the prediction comes from config, and refuses to guess ===')
    rp, why = predicted_ratio('4x', 'red_dot')
    want = cfg.RECOIL_SIGHT_PROFILES['4x']['mag'] * K_REF / K_4X
    _check('r_pred(4x)', round(rp, 4), round(want, 4), tol=1e-6)
    _check('r_pred(red_dot vs itself) is 1', round(
        predicted_ratio('red_dot', 'red_dot')[0], 6), 1.0, tol=1e-9)
    rp2, why2 = predicted_ratio('8x', 'red_dot')
    _check('an unmeasured sight gets nan, not a default', np.isnan(rp2), True)
    _check('...and names what is missing', "'8x'" in (why2 or ''), True)

    print('\n=== no extrapolation past the end of a burst ===')
    short = _fake('red_dot', K_REF, PLANT_Y, 0.8, span=1.2)
    _check('past the horizon is nan', np.isnan(y_true_at(short, 2.4)), True)
    _check('inside it is not', np.isfinite(y_true_at(short, 1.0)), True)
    _check('common_horizon takes the SHORTEST burst',
           round(common_horizon([m, short]), 3), 1.2, tol=1e-6)

    print('\n=== the audit refuses the pools that would lie ===')
    _check('a clean pool has nothing to say', audit(ref), [])
    out = audit(ref + [_fake('red_dot', K_REF, PLANT_Y, 0.8, ads_end=False)])
    _check('a burst that left the scope is named',
           any('OUT of the sight' in s for s in out), True)
    noasset = _fake('red_dot', K_REF, PLANT_Y, 0.8, asset='')
    _check('a magazine with no witness is named',
           any('sight_asset' in s for s in audit([noasset])), True)
    mixed = audit([_fake('red_dot', K_REF, PLANT_Y, 0.8),
                   _fake('4x', K_4X, PLANT_Y * PLANT_R, 0.8)])
    _check('a pool mixing sights is refused',
           any('mixes sights' in s for s in mixed), True)

    print()
    if FAILS:
        print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
        return 1
    print('all ok')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--weapon')
    ap.add_argument('--sight', default='4x', help='the optic under test')
    ap.add_argument('--ref', default='red_dot', help='what to compare against')
    ap.add_argument('--posture', default='standing')
    ap.add_argument('--mags', type=int, default=4, help='magazines per block')
    ap.add_argument('--blocks', type=int, default=2,
                    help='ref/sight pairs; the sights ALTERNATE')
    ap.add_argument('--arm', type=float, default=0.8,
                    help='second arm scale (ARM_PLAN uses 0.8 -- see the '
                         'module docstring before widening it)')
    ap.add_argument('--at', type=float, default=None,
                    help='evaluate y_true at this many seconds after the '
                         'click (default: the largest time every magazine '
                         'reached)')
    ap.add_argument('--analyse', action='store_true',
                    help='read the store and stop. No game, no hardware.')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if not a.weapon:
        ap.error('--weapon is required unless --selftest')
    if a.analyse:
        rec = analyse(a.weapon, a.sight, a.ref, {}, at=a.at)
        return 0 if rec.get('ok') else 5
    return run(a)


if __name__ == '__main__':
    sys.exit(main())
