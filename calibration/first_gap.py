"""Is the first bullet's hole separated by the HEAD LEAD? -> exit 1 if unproven.

    pixi run first-gap --weapon aug
    pixi run first-gap --selftest            # offline, no store needed

THE OBSERVATION THIS EXISTS FOR, from the chair on 2026-08-10:

    "弹孔打出来，第一发跟其他的都不一样，都离很远" — 顶上其他的二倍

THE CLAIM IT TESTS is that the separation is MADE BY the compensation's head
lead, not left behind by it:

    The first round leaves at t=0, when the firmware has emitted nothing.
    Every later round leaves having been handed `lead` ms of curve that the
    recoil has not caught up to yet. So round 2 sits one lead-worth below
    round 1, and rounds 3..n sit only the CHANGE in that lead apart.

    Measured on the wire the same night (desktop cursor as an oscilloscope,
    pointer acceleration off, 0.495 px/count): at aug's second-round instant
    (t = 83 ms) the firmware had emitted 19.87 counts while y_true asks for
    6.65. The first shot IS compensated -- emission starts 2.8 ms after the
    click -- and that is precisely the problem: rounds 2+ get a 90 ms head
    start that round 1 does not.

    The prediction is therefore ORDERED and it has a zero:

        r = |gap(1->2)| / (one round's own recoil)

    must fall toward 0 as the lead goes to 0, monotonically. At zero lead the
    compensation is aligned with the recoil, every round is handed the same
    amount of curve, and nothing can single the first hole out.

⚠ r IS NOT NORMALISED BY THE OTHER GAPS, and the first draft of this file was.
That reading is the natural one ("is the first gap bigger than the rest") and
it DESTROYS THE MEASUREMENT AT EXACTLY THE VALUE BEING TESTED: at lead 0 a
working compensation makes every gap vanish, so the statistic divides zero by
zero right where the claim predicts its answer. Its own selftest caught that,
which is the whole argument for writing the negative cases first.

⚠ WHAT THIS DOES NOT CLAIM. Seven different statistics were tried against the
four guns the operator graded by eye (akm 好, groza 好, aug 坏, famas 坏) and
NONE of them separated the pairs. This one is no different: it is a test of the
MECHANISM -- does the lead make the gap -- not a predictor of which gun feels
bad. Do not read a per-gun number here as a verdict on that gun.

⚠ THE COMPARISON IS BETWEEN ARMS OF ONE GUN, AND THAT IS WHY IT IS
TRUSTWORTHY. Every number the loop produces comes off one chain (patch
correlation -> / K -> compared to the curve), and calibration/CLAUDE.md records
a multiplicative measurement error that is constant within a run. The
denominator is a per-gun constant, so it cancels exactly between arms -- not
averaged down, cancelled. Comparing r across GUNS would not have that
property, and this file never does.

COLLECTION IS NOT HERE, and that is deliberate. `collect_timed` already
rotates the offset per magazine off ONE fit, interleaved in time, with the
one-gun gate and the sight readback in front of it:

    pixi run collect-timed --weapon aug --sight red_dot \\
        --fire-delay-sweep -20,-45,-70,-90 --mags 16

    -20 is lead 0 (offset = -L, the compensation aligned with the recoil)
    -90 is lead 70, what config ships today

⚠ --mags IS THE TOTAL AND THE ARMS SPLIT IT, so 16 over 4 arms is n=4 each.
calibration/CLAUDE.md measured what unbalanced arms cost: the same gun, same
library, same day read spread 19.7% when one arm got n=1 and 6.5% when every
arm got n=6, and the difference was that arm's own sampling noise wearing the
result's clothes. Ask for a multiple of the arm count.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import config                                                    # noqa: E402
from calibration import samples as S                             # noqa: E402

# Rounds whose gaps enter the denominator. Four gaps means five launch
# instants, which is 340..500 ms of burst depending on the gun -- long enough
# that the later gaps have settled, short enough that the view has not wandered
# into the part of the curve where the arms genuinely differ.
N_GAPS = 4

# An arm with fewer magazines than this is REPORTED AND NOT USED. See the
# --mags note above: an n=1 arm speaks as loudly as an n=6 arm in any
# across-arm comparison while carrying three times the sampling noise.
ARM_MIN = 3

FAILS = []


def _interval_ms(weapon):
    """The measured shot interval, or None. NEVER the wiki table.

    detector/weapon.WEAPON_RPM is a wiki figure and is wrong on a third of the
    roster; a wrong interval samples y_obs between the rounds rather than at
    them, which is this whole file's coordinate.
    """
    try:
        with open(config.MEASURED_RPM_PATH, encoding='utf-8') as fh:
            table = json.load(fh)
    except OSError:
        return None
    row = table.get(weapon)
    return float(row['interval_ms']) if row and 'interval_ms' in row else None


def gaps_of(t_s, y_counts, interval_ms, n_gaps=N_GAPS):
    """|Δ position| between consecutive launch instants. -> list or None.

    A round leaves at t = k * interval, and where it lands is where the view
    was pointing THEN -- so the hole spacing is the difference of y_obs at
    those instants. y_obs is what the screen did AFTER compensation, which is
    the residual, which is the hole pattern. No K, no binning, no curve.
    """
    need = n_gaps * interval_ms / 1000.0
    if t_s is None or len(t_s) < 2 or t_s[-1] < need:
        return None
    at = np.interp([k * interval_ms / 1000.0 for k in range(n_gaps + 1)],
                   t_s, y_counts)
    return [abs(float(at[k + 1] - at[k])) for k in range(n_gaps)]


def ratio_of(gaps, shot_counts):
    """gap(1->2) in units of ONE ROUND'S OWN RECOIL. -> float or None.

    ⚠ THE DENOMINATOR IS NOT THE OTHER GAPS, AND THE FIRST VERSION OF THIS FILE
    HAD IT WRONG -- its own selftest caught it, which is the entire argument for
    writing the negative cases first. Normalising by the later gaps looks
    natural ("is the first one bigger than the rest") and it DESTROYS THE
    MEASUREMENT AT EXACTLY THE VALUE BEING TESTED: at lead 0 a working
    compensation makes every gap vanish, so the statistic divides zero by zero
    right where the claim predicts its answer. The same flaw printed -26.90 for
    the scar and +6.97 for the groza on an earlier pass, and both read like
    findings.

    `shot_counts` is y_true over one shot interval, read off the fitted curve.
    It is a PER-GUN CONSTANT, so it cancels exactly out of any comparison
    between arms of the same gun -- which is the only comparison this file
    makes. It is there to give the number a unit ("the first hole sits N
    rounds' worth away"), not to do work in the verdict.
    """
    if gaps is None or not shot_counts or shot_counts <= 0:
        return None
    return gaps[0] / shot_counts


def shot_counts_of(weapon, config_key, interval_ms):
    """y_true accumulated over ONE shot interval, off the fitted curve."""
    key = f'{weapon}__{config_key or "bare"}.json'
    path = os.path.join(config.CURVES_DIR, key)
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as fh:
        shots = json.load(fh)['shots']
    t, s = 0.0, 0.0
    for k in shots:
        t += k['delay_ms']
        if t >= interval_ms:
            break
        s += k['dy']
    return s


def interleaved(spans):
    """Do the arms OVERLAP IN TIME? {arm: (first_ts, last_ts)} -> bool.

    ⚠ THE SINGLE MOST IMPORTANT GUARD IN THIS FILE, and the first version did
    not have it -- it happily compared 30 magazines fired at -19 in one week
    against 18 fired at -90 in another and printed a verdict. config.py records
    what that costs in this exact measurement: a -46 arm fired in its own run
    read +31.4 counts where the interleaved -50 and -30 either side of it read
    -4.7 and +1.0. Thirty counts apart, same gun, same lane, twenty minutes
    apart. Every offset comparison made across runs before that was unreliable,
    including one that set this constant.

    So the requirement is not "enough magazines", it is that the arms were
    ROTATED AGAINST EACH OTHER. Disjoint time ranges mean whatever drifts
    between runs is free to line up with the arm, and nothing downstream can
    tell that apart from the effect being measured.
    """
    windows = [w for w in spans.values() if w[0] and w[1]]
    if len(windows) < 2:
        return False
    latest_start = max(w[0] for w in windows)
    earliest_end = min(w[1] for w in windows)
    return latest_start <= earliest_end


def by_arm(mags, interval_ms, shot_counts):
    """{arm: [ratio,...]}, {arm: (first_ts,last_ts)}, n skipped."""
    arms, spans, skipped = {}, {}, 0
    for m in mags:
        if m.fire_delay_ms is None:
            skipped += 1
            continue
        try:
            t, y = m.y_obs_counts()
        except Exception:
            skipped += 1
            continue
        r = ratio_of(gaps_of(t, y, interval_ms), shot_counts)
        if r is None:
            skipped += 1
            continue
        arm = int(round(float(m.fire_delay_ms)))
        arms.setdefault(arm, []).append(r)
        ts = getattr(m, 'ts', '') or ''
        lo, hi = spans.get(arm, (ts, ts))
        spans[arm] = (min(lo, ts) if ts else lo, max(hi, ts) if ts else hi)
    return arms, spans, skipped


def check(label, cond, detail=''):
    print(f'  {"ok  " if cond else "FAIL"}  {label}'
          + (f'\n           {detail}' if detail and not cond else ''))
    if not cond:
        FAILS.append(label)


def report(weapon, arms, skipped, interval_ms, spans=None):
    """Print the arms in LEAD order and judge the trend. -> True if proven."""
    print(f'\n  {weapon}: shot interval {interval_ms:.1f} ms, '
          f'{sum(len(v) for v in arms.values())} magazines usable, '
          f'{skipped} could not answer\n')
    print(f"  {'offset':>8}{'lead':>6}{'mags':>6}{'r median':>10}"
          f"{'(p25..p75)':>16}")
    print('  ' + '-' * 46)

    rows = []
    for off in sorted(arms, reverse=True):          # -20 first, -90 last
        rs = arms[off]
        lead = -off - config.RECOIL_COMP_LAG_MS
        q1, med, q3 = np.percentile(rs, [25, 50, 75])
        thin = '  <- THIN, not used' if len(rs) < ARM_MIN else ''
        print(f'  {off:8d}{lead:6.0f}{len(rs):6d}{med:10.2f}'
              f'{f"({q1:5.2f}..{q3:5.2f})":>16}{thin}')
        if len(rs) >= ARM_MIN:
            rows.append((lead, med))

    if len(rows) < 2:
        print('\n  [!] REFUSING to judge: fewer than two arms with '
              f'{ARM_MIN}+ magazines.\n      This is not a negative result, it '
              'is an unrun experiment. See --mags\n      in the file header.')
        return False

    if spans is not None and not interleaved({a: spans[a] for a in arms
                                              if a in spans}):
        print('\n  [!] CROSS-RUN. The arms do not overlap in time, so they were '
              'never rotated\n      against each other and anything that drifts '
              'between runs is free to line\n      up with the arm. This file '
              'will not judge on that — see interleaved().\n      Collect a '
              'sweep in ONE run; the command is in the header.')
        for a in sorted(arms, reverse=True):
            if a in spans:
                print(f'      {a:5d} ms   {spans[a][0]}  ..  {spans[a][1]}')
        return False

    rows.sort()
    lo_lead, lo_r = rows[0]
    hi_lead, hi_r = rows[-1]
    print(f'\n  lead {lo_lead:.0f} ms -> r = {lo_r:.2f}     '
          f'lead {hi_lead:.0f} ms -> r = {hi_r:.2f}')
    # The claim is directional AND it has a zero, so the test is directional:
    # less lead must mean a smaller first gap. No threshold is invented here --
    # "smaller" is the whole prediction, and a threshold would be a number
    # nobody measured standing between the data and the answer.
    # ⚠ MONOTONE ACROSS EVERY ARM, NOT THE TWO ENDPOINTS. This line used to be
    # `lo_r < hi_r`, and on the very first real sweep it printed "the
    # separation FOLLOWS the lead" over 0.33 / 0.32 / 0.22 / 0.41 -- a series
    # whose MINIMUM is in the middle. The endpoints happened to be ordered;
    # nothing else was.
    #
    # The root CLAUDE.md carries this same error three times already ("只看末点
    # 就说 y_true 是倒 U", "用端点比值当增益"), and its rule is that a criterion
    # for an ORDERED claim must itself be ordered. The claim is that the lead
    # MAKES the gap, which is a statement about every step.
    #
    # The selftest already demanded monotonicity of the synthetic. A verdict
    # weaker than the file's own selftest is a gate that cannot fail where it
    # matters. bool() because np.percentile yields numpy.bool_ from the
    # comparison, which is truthy but `is not True`, and main() turns this into
    # an exit code.
    ok = bool(all(a[1] <= b[1] for a, b in zip(rows, rows[1:])))
    if ok:
        print('  -> the separation FOLLOWS the lead. Cutting the lead is the '
              'fix,\n     and adding lead is what made it worse.')
    else:
        worst = max((abs(b[1] - a[1]), a[0], b[0])
                    for a, b in zip(rows, rows[1:]) if b[1] < a[1])
        print(f'  -> NOT SHOWN. r is not monotone in the lead: it FALLS from '
              f'lead {worst[1]:.0f}\n     to lead {worst[2]:.0f}, which the '
              f'claim forbids. Either the effect is smaller\n     than the '
              f'scatter at this n, or the lead is not what separates that '
              f'hole.\n     Do not cut the lead on the strength of this run.')
    return ok


def selftest():
    """Synthesise y_obs for a known lead and check the analysis recovers it.

    ⚠ THE POINT IS THE TWO NEGATIVE CASES, not the positive one. A statistic
    that answers "yes" on data built to say yes has shown nothing; this repo
    has paid for gates that could not fail. So lead 0 must come back at 1.0,
    and a magazine whose later gaps are pure noise must be REFUSED rather than
    given a spectacular ratio.
    """
    print('first_gap selftest\n')
    iv = 83.0
    t = np.arange(0, 0.5, 0.001)
    AMP, SHAPE = 80.0, 1.6
    # ⚠ DERIVED FROM THE SYNTHETIC, NOT TYPED. The first draft hardcoded aug's
    # real 6.65 counts here while the synthetic curve carried a completely
    # different amplitude, so the unit and the thing being measured described
    # two different guns -- the root CLAUDE.md's second law, inside a selftest
    # written to defend it.
    SHOT = AMP * (iv / 1000.0) ** SHAPE

    def synth(lead_ms):
        """y_obs = recoil(t) - compensation, the compensation played `lead` early.

        Curved, not linear. A straight line's lead term is a CONSTANT offset and
        constants cancel out of differences, so a linear synthetic reports no
        effect at any lead -- which would make the positive case below pass for
        the wrong reason and hide a broken statistic.
        """
        recoil = AMP * t ** SHAPE
        comp = AMP * np.maximum(t + lead_ms / 1000.0, 0) ** SHAPE
        return t, recoil - comp

    # 1. lead 0 -> nothing separates the first hole
    r0 = ratio_of(gaps_of(*synth(0.0), iv), SHOT)
    check('lead 0 leaves the first gap at ~0', r0 is not None and r0 < 0.05,
          f'got {r0!r}; with the compensation aligned to the recoil every round '
          f'is handed the same amount of curve, so no gap can stand out')

    # 2. a real lead singles the first gap out
    # ⚠ RELATIVE TO THE ZERO-LEAD CASE, not against a typed threshold. The first
    # draft asserted `r70 > 1.0` and failed at 0.899 -- a line nobody measured,
    # standing between the data and the answer. What the claim actually says is
    # that the lead MAKES this gap, so the test is that removing the lead
    # removes the gap.
    r70 = ratio_of(gaps_of(*synth(70.0), iv), SHOT)
    check('lead 70 makes a gap that lead 0 does not',
          r70 is not None and r0 is not None and r70 > 10 * max(r0, 1e-3),
          f'got {r70!r} against {r0!r} at zero lead')

    # 3. and it must be MONOTONE in the lead, which is the actual claim
    ladder = [ratio_of(gaps_of(*synth(L), iv), SHOT) for L in (0, 20, 40, 70)]
    check('the first gap grows with the lead, every step',
          all(a is not None and b is not None and a < b
              for a, b in zip(ladder, ladder[1:])),
          f'got {["%.2f" % x if x else x for x in ladder]}; a statistic that '
          f'is not monotone here cannot attribute anything to the lead')

    # 4. NEGATIVE: a magazine too short to reach the last instant answers None
    short_t = np.arange(0, 0.10, 0.001)
    check('a magazine shorter than the window answers None',
          gaps_of(short_t, 40.0 * short_t, iv) is None,
          'gaps were computed past the end of the recording')

    # 5. NEGATIVE: no curve for the cell -> no unit -> no number
    check('a missing shot_counts refuses rather than divides',
          ratio_of([9.0, 1.0, 1.0, 1.0], None) is None
          and ratio_of([9.0, 1.0, 1.0, 1.0], 0.0) is None,
          'a ratio came back without a denominator to divide by')

    # 6. NEGATIVE: an arm under ARM_MIN must not reach the verdict
    thin = {-20: [0.1], -90: [1.9, 1.9, 1.9]}
    check('a thin arm is excluded from the verdict',
          report('selftest', thin, 0, iv) is False,
          'a one-magazine arm was allowed to set the direction')

    # 7. NEGATIVE: arms that never overlapped in time must not be judged.
    # ⚠ THIS ONE IS WHY THE FILE EXISTS IN THIS SHAPE. Run against the real
    # store on 2026-08-11 the first version compared 30 magazines fired at -19
    # in one week against 18 fired at -90 in another, and printed "the
    # separation FOLLOWS the lead" off a comparison config.py already records
    # as worth 30 counts of arm-vs-run confound.
    fat = {-20: [0.1, 0.1, 0.1], -90: [1.9, 1.9, 1.9]}
    disjoint = {-20: ('2026-08-01T10:00', '2026-08-01T11:00'),
                -90: ('2026-08-09T10:00', '2026-08-09T11:00')}
    check('arms from different runs are REFUSED',
          report('selftest-crossrun', fat, 0, iv, disjoint) is False,
          'a cross-run pair set the direction')
    overlap = {-20: ('2026-08-09T10:00', '2026-08-09T11:00'),
               -90: ('2026-08-09T10:05', '2026-08-09T10:55')}
    check('...but interleaved arms ARE judged',
          report('selftest-interleaved', fat, 0, iv, overlap) is True,
          'the guard also rejected arms that DO overlap — a gate that always '
          'refuses is the same as no gate')

    print(f'\n{"all ok" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"}')
    return 1 if FAILS else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', default='aug')
    ap.add_argument('--config', default=None,
                    help="config_key, e.g. grip-vert_grip. Default bare.")
    ap.add_argument('--sight', default=None)
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    iv = _interval_ms(args.weapon)
    if iv is None:
        print(f'[!] REFUSING: no MEASURED shot interval for {args.weapon}. '
              f'Run `pixi run sweep` first —\n    the wiki table is wrong on a '
              f'third of the roster and a wrong interval\n    samples y_obs '
              f'between the rounds instead of at them.')
        return 2

    # ⚠ --config IS A STORE KEY, NOT A CONFIG DICT. `S.load` builds the
    # filename by running config_key() over a dict, so handing it the key
    # string that is ALREADY the answer dies inside config_key with an
    # AttributeError about .items -- an error message about the wrong layer.
    # The key is what a human reads off the store, so take that and address the
    # file directly.
    key = args.config or 'bare'
    path = os.path.join(os.path.dirname(S.path_for(args.weapon)),
                        f'{args.weapon}__{key}.jsonl')
    if not os.path.exists(path):
        print(f'[!] no store at {path}')
        return 2
    mags = S.load(args.weapon, path=path, sight=args.sight)
    if not mags:
        print(f'[!] no magazines for {args.weapon} {key!r}.')
        return 2

    shot = shot_counts_of(args.weapon, key, iv)
    unit = f'{args.weapon} {key}'
    if not shot:
        # ⚠ FALL BACK AND SAY SO, rather than refuse. The denominator is the
        # SAME for every arm, so the verdict -- which is a comparison between
        # arms -- cannot move whichever curve supplies it. What does move is
        # what the printed number MEANS, and a number whose unit is silently
        # another cell's is exactly what gets quoted six weeks later.
        shot = shot_counts_of(args.weapon, 'bare', iv)
        unit = f'{args.weapon} bare (⚠ NO CURVE FOR {key} — unit borrowed)'
    if not shot:
        print(f'[!] REFUSING: no fitted curve for {args.weapon} at all, so the '
              f'gap has no unit.')
        return 2
    print(f'  one round of {unit} = {shot:.2f} counts (y_true over {iv:.0f} ms)')

    arms, spans, skipped = by_arm(mags, iv, shot)
    if not arms:
        print(f'[!] REFUSING: none of the {len(mags)} magazines carries a '
              f'fire_delay_ms.\n    Magazines fired before 2026-08-08 predate '
              f'the field. Collect a sweep;\n    the command is in this file\'s '
              f'header.')
        return 2

    return 0 if report(args.weapon, arms, skipped, iv, spans) else 1


if __name__ == '__main__':
    raise SystemExit(main())
