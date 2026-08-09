"""The mp5k 2x2x2, read at a common time, against the independent 08-05 table.

    pixi run cube

Written because two cells whose configs differ by a compensator came out
within 0.5% of each other, and "the compensator does nothing on top of a
foregrip" and "one of these cells is not the config it says" produce exactly
that picture. Only one of them is a finding. It was the second one, twice.

⚠ THE ENDPOINT IS NOT THE COMPARISON. Magazines differ in length, and
post-burst drift goes different directions per config, so a cell's last sample
can sit past another's. Everything here is read at a COMMON t, taken as the
shortest burst in the comparison, and that t is printed.

WHY THE 08-05 COLUMN IS HERE AND NOT A FOOTNOTE
    data/kit_factors.json holds the same eight cells measured by the BULLET-BIN
    pipeline three days earlier, from different code, on a different day, in a
    coordinate system this one exists to replace. That makes it the second
    independent source the repository's cross-layer law asks for, and it has
    already earned its keep: it is what caught `grip-vert_grip` reading 0.482
    when it should read 0.747, which turned out to be five magazines fired out
    of the wrong gun. A cell agreeing across both pipelines is a cell two
    unrelated things had to be wrong in the same direction to fake.

    It is NOT ground truth. Where they disagree, the honest report is a
    disagreement -- see the muzzle+stock row.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from calibration import samples as S

DIR = Path(S.SAMPLE_DIR)
WEAPON = 'mp5k'
KIT_FACTORS = Path(__file__).resolve().parents[1] / 'data/kit_factors.json'

# The cube's corners as (label, config-key fragment sorted the way samples.py
# writes it). Spelled out rather than generated so a cell that is MISSING shows
# up as a missing row instead of silently not being asked about.
CUBE = [
    ('bare',          'bare'),
    ('M..',           'muzzle-comp_smg'),
    ('.G.',           'grip-vert_grip'),
    ('..K',           'stock-heavy_stock'),
    ('MG.',           'grip-vert_grip_muzzle-comp_smg'),
    ('M.K',           'muzzle-comp_smg_stock-heavy_stock'),
    ('.GK',           'grip-vert_grip_stock-heavy_stock'),
    ('MGK',           'grip-vert_grip_muzzle-comp_smg_stock-heavy_stock'),
]

# The same corners as the 08-05 table spells them.
OLD_KEY = {
    'bare': None,
    'M..': 'muzzle=comp_smg',
    '.G.': 'grip=vert_grip',
    '..K': 'stock=heavy_stock',
    'MG.': 'grip=vert_grip+muzzle=comp_smg',
    'M.K': 'muzzle=comp_smg+stock=heavy_stock',
    '.GK': 'grip=vert_grip+stock=heavy_stock',
    'MGK': 'grip=vert_grip+muzzle=comp_smg+stock=heavy_stock',
}

BOOT = 20000
RNG = np.random.default_rng(20260808)


def load(path):
    """Magazines off one cell file, each as (t from the click, y_true).

    ⚠ THROUGH calibration.samples, NOT BY RE-DERIVING IT. `dy_px` holds
    len(t)-1 frame-to-frame shifts and shift[i] belongs to t[i+1], with frame
    0 an exact zero anchor -- get that alignment wrong and every sample lands
    one frame early, which is the same class of error the bin coordinate was
    removed to end. Magazine.y_true_counts() is where that lives, and it also
    adds y_comp back, so this works whether or not a cell was compensating.
    """
    out = []
    for m in S.load(None, None, path=str(path)):
        t, y = m.y_true_counts()
        # ⚠ NOT `t - t[0]`. `t` IS ALREADY measured from the click -- the
        # frames before it carry NEGATIVE times (15-18 of them, ~130 ms), and
        # re-zeroing on the first frame slides every magazine later by its own
        # prefire length. It barely moves a comparison taken near the flat end,
        # and it moved a timing statistic by 130 ms the one time it was used
        # for one: "the screen first moves 195 ms after the click" was really
        # 69 ms, which is the difference between "after two shots" and "during
        # the first".
        # ⚠ THE ARM IS "WAS ANY COMPENSATION DELIVERED", NOT `comp_enabled`.
        # A --scale-sweep=0 magazine is armed with a curve of 174 knots that
        # are all ZERO, so comp_enabled is True while y_true = y_obs exactly --
        # a compensation-OFF magazine by every property that matters here.
        # Splitting on the flag put the 8 interleaved scale-0 magazines on the
        # compensated side and left the interleaving check reporting NO
        # OVERLAP on a run fired specifically to overlap.
        out.append({'ts': m.ts, 't': t, 'y': y, 'K': m.K,
                    'mag': m.magazine_size, 'sight': m.sight,
                    'comp': bool(sum(k.get('dy', 0.0) for k in m.curve)),
                    'config': m.config})
    return out


def at(m, t_q):
    """y at time t_q, or None if the magazine ended before it.

    No extrapolation and no edge clamp: np.interp holds the last value past
    the end, which turns "this burst was over" into a number that looks like a
    measurement. CLAUDE.md names that clamp as one of the things the time
    coordinate was supposed to remove.
    """
    if t_q > m['t'][-1] or t_q < m['t'][0]:
        return None
    return float(np.interp(t_q, m['t'], m['y']))


def _resample(a, n=BOOT):
    """n bootstrap means of `a`, resampling MAGAZINES.

    The magazine is the unit because it is what the failures are: a whole
    magazine is fired out of one gun under one config, so a bad one is bad end
    to end. Resampling frames would give a beautifully tight interval around
    whatever the contaminated cell happened to say.
    """
    a = np.asarray(a)
    return a[RNG.integers(0, len(a), (n, len(a)))].mean(1)


def boot_excess(obs, singles, bare, n=BOOT):
    """95% CI for obs/prod(singles) - 1, EVERY cell resampled.

    ⚠ THE PREDICTION HAS ERROR BARS TOO, and the first version of this treated
    it as exact -- it resampled only the observed cell and printed the result
    as if it were the excess interval. For the triple that hides the scatter of
    THREE measured singles inside a number presented as a constant, and it
    always narrows the interval, which is the direction that manufactures
    findings.

    `bare` cancels to first order (it is the denominator of both sides) but is
    resampled once and used on both, rather than cancelled algebraically:
    it is the same 11 magazines on both sides of the ratio, so pretending it is
    two independent draws would widen the interval as wrongly as dropping it
    narrows it.
    """
    b = _resample(bare, n)
    o = _resample(obs, n) / b
    p = np.ones(n)
    for s in singles:
        p = p * (_resample(s, n) / b)
    r = o / p - 1.0
    return float(np.percentile(r, 2.5)), float(np.percentile(r, 97.5))


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    cells, quarantined = {}, []
    for label, frag in CUBE:
        p = DIR / f'{WEAPON}__{frag}.jsonl'
        cells[label] = load(p) if p.exists() else []
    for p in sorted(DIR.glob(f'{WEAPON}__*.jsonl')):
        if '.MISLABELLED' in p.name or '.SUSPECT' in p.name:
            quarantined.append((p.name, load(p)))

    # ⚠ ONE ARM FOR THE CUBE. y_true is supposed to be arm-independent, and the
    # 16-magazine pool proves it holds to 4.6% -- but seven cells have only the
    # comp-OFF arm and one now has both, so pooling raises THAT cell alone and
    # slides every f against it. Measured: bare went 903.4 -> 915.0 and all
    # seven factors moved 1-2% with no measurement having changed.
    #
    # A comparison is between like things or it is not a comparison. The
    # compensated arm is reported below, on its own, where it answers the
    # question it can actually answer.
    comp_on = {k: [m for m in ms if m['comp']] for k, ms in cells.items()}
    cells = {k: [m for m in ms if not m['comp']] for k, ms in cells.items()}
    live = [m for ms in cells.values() for m in ms]
    if not live:
        print('nothing stored')
        return 1
    t_q = min(m['t'][-1] for m in live)
    print(f'{WEAPON} 2x2x2, comp OFF, red dot, standing')
    print(f'read at t = {t_q:.3f} s (the shortest burst across the eight cells)')
    print()

    old = {}
    if KIT_FACTORS.exists():
        d = json.loads(KIT_FACTORS.read_text(encoding='utf-8'))
        old = (d.get('kits', {}).get(WEAPON, {}).get('standing', {}))

    vals = {}
    print(f'{"cell":5} {"n":>3} {"counts":>9} {"sd":>6} {"cv":>6} '
          f'{"f(time)":>8} {"f(08-05)":>9} {"delta":>7}')
    base = None
    for label, _ in CUBE:
        ms = cells[label]
        if not ms:
            print(f'{label:5} {"--":>3}   (not measured)')
            continue
        a = np.array([v for v in (at(m, t_q) for m in ms) if v is not None])
        vals[label] = a
        if label == 'bare':
            base = a.mean()
        f = a.mean() / base if base else float('nan')
        ok = OLD_KEY[label]
        f_old = 1.0 if label == 'bare' else (old.get(ok, {}) or {}).get('f')
        sd = a.std(ddof=1) if len(a) > 1 else float('nan')
        delta = (f / f_old - 1) * 100 if f_old else float('nan')
        print(f'{label:5} {len(a):3d} {a.mean():9.1f} {sd:6.1f} '
              f'{sd/a.mean()*100:5.1f}% {f:8.3f} '
              f'{(f_old if f_old else float("nan")):9.4f} {delta:+6.1f}%')

    # ── orthogonality ──────────────────────────────────────────────────
    # MODEL.md's question, and the only one the cube exists to answer: does
    # fitting two parts cost the PRODUCT of fitting each? The excess is
    # measured, not asserted -- and it is reported with a CI because the whole
    # 2026-08-08 m416 result shrank from +10.6% to +2.5% under three different
    # ways of taking the same average, which is the shape of an artefact.
    print()
    print('orthogonality: observed / (product of the singles), 95% CI over '
          'magazines')
    pairs = [('MG.', ['M..', '.G.'], 'muzzle x grip'),
             ('M.K', ['M..', '..K'], 'muzzle x stock'),
             ('.GK', ['.G.', '..K'], 'grip   x stock'),
             ('MGK', ['M..', '.G.', '..K'], 'all three')]
    for cell, parts, name in pairs:
        if cell not in vals or any(p not in vals for p in parts):
            print(f'  {name:14} -- not measured')
            continue
        obs = vals[cell].mean() / vals['bare'].mean()
        pred = np.prod([vals[p].mean() / vals['bare'].mean() for p in parts])
        lo, hi = boot_excess(vals[cell], [vals[p] for p in parts],
                             vals['bare'])
        excess = (obs / pred - 1) * 100
        verdict = 'multiplicative' if lo <= 0 <= hi else 'NOT multiplicative'
        print(f'  {name:14} obs {obs:.4f}  pred {pred:.4f}  '
              f'excess {excess:+6.1f}%  [{lo:+.1%}, {hi:+.1%}]  {verdict}')

    both = [(k, cells[k], comp_on[k]) for k in cells if comp_on.get(k)]
    if both:
        print()
        print('the SAME cell down both arms — MODEL.md says y_true does not '
              'depend on which curve was playing')
        for k, off, on in both:
            ao = np.array([v for v in (at(m, t_q) for m in off) if v is not None])
            an = np.array([v for v in (at(m, t_q) for m in on) if v is not None])
            obs = np.array([abs(np.interp(t_q, m['t'], m['y'])) for m in on])
            print(f'  {k:5} comp OFF n={len(ao):2d} y_true {ao.mean():7.1f} '
                  f'sd {ao.std(ddof=1):5.1f}   |   comp ON n={len(an):2d} '
                  f'y_true {an.mean():7.1f} sd {an.std(ddof=1):5.1f}   '
                  f'-> {an.mean()/ao.mean()-1:+.1%}')
            # ⚠ AND WHETHER THE TWO ARMS EVER SHARED A SESSION, because
            # without that this line is not an arm comparison at all. On
            # 2026-08-08 mp5k bare read +4.1% (3.4 sigma) between arms whose
            # timestamps do not overlap by a single magazine -- OFF at 14:37
            # and 15:2x, ON from 17:xx onward. config.RECOIL_FIRE_DELAY_MS
            # records THIRTY COUNTS of between-session drift on the same gun
            # and the same lane twenty minutes apart, which on 950 is 3.2%:
            # the size of the whole disagreement.
            #
            # The same question asked with the arms INTERLEAVED -- the scale
            # sweep, 0.90/1.00/1.10 rotated per magazine -- answers 940.6 /
            # 941.5 / 943.1, flat to 0.3%. Same question, same evening, two
            # orders of magnitude apart in the answer, and the only difference
            # is interleaving.
            #
            # MODEL.md calls this check the one thing a fit cannot arrange.
            # That is true of the check; it is not true of a version that
            # compares two afternoons.
            t_off = sorted(m['ts'] for m in off if m.get('ts'))
            t_on = sorted(m['ts'] for m in on if m.get('ts'))
            if t_off and t_on:
                overlap = (t_off[0] <= t_on[-1] and t_on[0] <= t_off[-1])
                if not overlap:
                    print(f'        ⚠ THE ARMS DO NOT OVERLAP IN TIME '
                          f'(OFF {t_off[0]}..{t_off[-1]}, '
                          f'ON {t_on[0]}..{t_on[-1]}). This number cannot '
                          f'separate an arm difference from session drift, '
                          f'which runs ~3% on this gun. NOT A VERDICT.')
                else:
                    print(f'        arms interleave in time '
                          f'({t_off[0]}..{t_off[-1]} vs '
                          f'{t_on[0]}..{t_on[-1]}) — comparable')

    if quarantined:
        print()
        print('quarantined (loaded by nothing, kept because samples are never '
              'deleted)')
        for name, ms in quarantined:
            a = np.array([v for v in (at(m, t_q) for m in ms) if v is not None])
            print(f'  {name}')
            print(f'    n={len(a)}  mean={a.mean():.1f}  '
                  f'= {a.mean()/base:.3f} of bare')
    return 0


if __name__ == '__main__':
    sys.exit(main())
