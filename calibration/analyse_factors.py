"""Attachment factors from a harvest log, with the error bars that decide.

    pixi run python calibration/analyse_factors.py --jsonl calibration/ortho_0802.jsonl

harvest.py's own report prints point estimates, which is enough to notice a
pattern and not enough to act on one. The game randomises每 shot by about ±5%
of a magazine, so at three magazines a cell a 3% gap and a 0% gap can be the
same measurement. Everything here is about telling those apart:

  * per-slot factor with a standard error, from the per-magazine spread
  * whether combinations equal the product of their parts, in sigma
  * whether a factor is the same on every weapon, in sigma

The multiplicativity test is done in log space, where the ratios become
differences and the bare measurement's own noise cancels the way it actually
does rather than being double-counted:

    log(measured/predicted) = log m_C - sum_s log m_s + (n-1) log m_bare

Why it matters: the compensation model this feeds has a single has_att
boolean. If the slots multiply, N slots cost N measurements and the boolean
becomes a product of N numbers. If they do not, it is 2^N curves per weapon
and the whole approach has to change.
"""
import argparse
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_SLOTS = ('muzzle', 'grip', 'stock')


def parse_config(name):
    if name == 'bare':
        return frozenset()
    if name == 'both':
        return frozenset(('muzzle', 'grip'))
    return frozenset(p for p in name.split('+') if p)


def load(path):
    """(weapon, config) -> list of per-magazine true-recoil counts.

    A magazine is the unit of measurement, not a cell: the spread between
    magazines IS the game's randomness, and it is the only estimate of it
    available. Later cells win, so a --resume rerun replaces rather than
    doubles.
    """
    cells = {}
    for line in open(path, encoding='utf-8'):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get('type') != 'cell':
            continue
        comp = r['comp_over_fired']
        samples = [comp + m['cum_counts'] for m in r.get('mags', [])]
        if samples:
            cells[(r['weapon'], r['config'], r.get('posture', 'standing'))] = {
                'samples': samples, 'want': r.get('want', {}),
                'fired': r.get('bullets_fired'),
                'uncovered': r.get('bullets_uncompensated', 0),
                'oor': sum(m.get('n_out_of_range', 0) for m in r['mags']),
            }
    return cells


def stats(xs):
    n = len(xs)
    m = sum(xs) / n
    if n < 2:
        return m, 0.0, 0.0
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    return m, sd, sd / math.sqrt(n)


def log_sem(mean, sem):
    """Relative error, which is the standard error of the log."""
    return sem / mean if mean else float('inf')


def posture_check(cells, base_posture):
    """Is the posture factor the same whatever is bolted to the gun?

    The compensation model multiplies a posture factor by an attachment factor
    and never asked whether it may. If crouching costs a fixed fraction of the
    recoil, the crouch/stand ratio is the same on a bare gun as on a kitted
    one; if the two interact, it is not, and every kitted crouching shot is
    compensated with a number nobody measured.

    Same weapon, same config, two postures — so the weapon's curve and the
    attachments both cancel and what is left is the posture alone.
    """
    postures = sorted({k[2] for k in cells})
    others = [p for p in postures if p != base_posture]
    if not others:
        return
    print('\n' + '=' * 74)
    print(f'IS POSTURE ORTHOGONAL TO THE ATTACHMENTS?   ratio against '
          f'{base_posture}')
    print('=' * 74)
    for p in others:
        rows = []
        for (w, c, pp), cell in cells.items():
            if pp != p:
                continue
            base = cells.get((w, c, base_posture))
            if not base:
                continue
            m, _, sem = stats(cell['samples'])
            bm, _, bsem = stats(base['samples'])
            rel = math.hypot(log_sem(m, sem), log_sem(bm, bsem))
            rows.append((w, c, m / bm, rel))
        if len(rows) < 2:
            continue
        print(f'\n  {p}:')
        for w, c, r, rel in sorted(rows):
            print(f'    {w:<9}{c:<20}{r:>8.4f}  +-{r*rel:.4f}')
        # Pooled in log space, then the worst deviation from it.
        ws = [(math.log(r), 1 / rel ** 2) for _, _, r, rel in rows if rel]
        mu = sum(l * wt for l, wt in ws) / sum(wt for _, wt in ws)
        worst = max(abs((math.log(r) - mu) / rel) for _, _, r, rel in rows
                    if rel)
        print(f'    -> pooled {math.exp(mu):.4f}; worst config deviates '
              f'{worst:.1f} sigma  '
              f'({"one factor fits every config" if worst < 2 else "POSTURE AND ATTACHMENTS INTERACT"})')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--jsonl', required=True, nargs='+')
    ap.add_argument('--posture', default='standing')
    args = ap.parse_args()

    cells = {}
    for p in args.jsonl:
        cells.update(load(p))
    all_cells = dict(cells)
    cells = {k: v for k, v in cells.items() if k[2] == args.posture}
    if not cells:
        print('[!] no cells')
        return 1

    weapons = sorted({k[0] for k in cells})
    configs = sorted({k[1] for k in cells},
                     key=lambda c: (len(parse_config(c)), c))

    # ── per cell ──
    print('=' * 74)
    print(f'CELLS  ({args.posture})   true recoil in counts over one magazine')
    print('=' * 74)
    print(f'{"weapon":<9}{"config":<20}{"n":>3}{"mean":>9}{"sd":>8}'
          f'{"sem":>7}{"cv":>7}   flags')
    for w in weapons:
        for c in configs:
            cell = cells.get((w, c, args.posture))
            if not cell:
                continue
            m, sd, sem = stats(cell['samples'])
            flags = []
            if cell['oor']:
                flags.append(f"oor={cell['oor']}")
            if cell['uncovered']:
                flags.append(f"{cell['uncovered']} past curve")
            if len(cell['samples']) < 2:
                flags.append('single magazine — no spread')
            print(f'{w:<9}{c:<20}{len(cell["samples"]):>3}{m:>9.1f}{sd:>8.1f}'
                  f'{sem:>7.1f}{100*sd/m if m else 0:>6.1f}%   '
                  f'{", ".join(flags)}')

    # ── single-slot factors ──
    print('\n' + '=' * 74)
    print('SINGLE-SLOT FACTORS   ratio to that weapon\'s own bare')
    print('=' * 74)
    fac = {}
    print(f'{"weapon":<9}{"slot":<10}{"part":<16}{"factor":>9}{"+-":>8}')
    for w in weapons:
        bare = cells.get((w, 'bare', args.posture))
        if not bare:
            print(f'{w:<9} no bare cell — every ratio for this weapon is '
                  f'unavailable')
            continue
        bm, _, bsem = stats(bare['samples'])
        for s in TEST_SLOTS:
            cell = cells.get((w, s, args.posture))
            if not cell:
                continue
            m, _, sem = stats(cell['samples'])
            r = m / bm
            rel = math.hypot(log_sem(m, sem), log_sem(bm, bsem))
            part = (cell.get('want') or {}).get(s) or s
            fac[(w, s)] = (r, rel, part)
            print(f'{w:<9}{s:<10}{part:<16}{r:>9.4f}{r*rel:>8.4f}')

    # ── is a factor the same on every weapon? ──
    # Grouped by PART, not by slot. The classes fit different hardware in the
    # same slot — comp_ar on a rifle, comp_smg on an SMG — so a "muzzle"
    # column pooled across them compares two different objects and calls the
    # difference a weapon effect.
    per_slot = defaultdict(list)
    for (w, s), (r, rel, part) in fac.items():
        per_slot[part].append((w, r, rel))
    if any(len(v) > 1 for v in per_slot.values()):
        print('\n' + '=' * 74)
        print('IS THE FACTOR WEAPON-INDEPENDENT?   same part, different '
              'weapons, in sigma')
        print('=' * 74)
        for s, entries in sorted(per_slot.items()):
            if len(entries) < 2:
                print(f'\n  {s}: only measured on '
                      f'{entries[0][0]} ({entries[0][1]:.4f}) — one weapon '
                      f'cannot answer this')
                continue
            # Inverse-variance weighted mean in log space.
            ws = [(math.log(r), 1 / rel ** 2) for _, r, rel in entries if rel]
            mu = sum(l * wt for l, wt in ws) / sum(wt for _, wt in ws)
            pooled = math.exp(mu)
            print(f'\n  {s}: pooled {pooled:.4f}')
            for w, r, rel in sorted(entries):
                dev = (math.log(r) - mu) / rel if rel else float('inf')
                print(f'    {w:<9}{r:>9.4f}   {dev:+6.1f} sigma from pooled')
            worst = max(abs((math.log(r) - mu) / rel) for _, r, rel in entries
                        if rel)
            print(f'    -> {"consistent" if worst < 2 else "NOT the same on every weapon"}'
                  f' (worst {worst:.1f} sigma)')

    # ── multiplicativity ──
    print('\n' + '=' * 74)
    print('DO THE SLOTS MULTIPLY?   measured / (product of single-slot factors)')
    print('=' * 74)
    print(f'{"weapon":<9}{"config":<20}{"pred":>8}{"meas":>8}{"gap":>8}'
          f'{"sigma":>8}   verdict')
    any_combo = False
    for w in weapons:
        bare = cells.get((w, 'bare', args.posture))
        if not bare:
            continue
        bm, _, bsem = stats(bare['samples'])
        b_rel = log_sem(bm, bsem)
        for c in configs:
            slots = parse_config(c)
            if len(slots) < 2:
                continue
            cell = cells.get((w, c, args.posture))
            if not cell or any((w, s) not in fac for s in slots):
                continue
            any_combo = True
            m, _, sem = stats(cell['samples'])
            meas = m / bm
            pred = 1.0
            for s in slots:
                pred *= fac[(w, s)][0]
            # log(meas/pred) = log m_C - sum log m_s + (n-1) log m_bare
            var = log_sem(m, sem) ** 2
            for s in slots:
                cs = cells[(w, s, args.posture)]
                sm, _, ssem = stats(cs['samples'])
                var += log_sem(sm, ssem) ** 2
            var += ((len(slots) - 1) * b_rel) ** 2
            sig = math.sqrt(var)
            delta = math.log(meas) - math.log(pred)
            gap = 100 * math.expm1(delta)
            nsig = delta / sig if sig else float('inf')
            verdict = ('multiplicative' if abs(nsig) < 2 else
                       'NOT multiplicative')
            print(f'{w:<9}{c:<20}{pred:>8.4f}{meas:>8.4f}{gap:>7.1f}%'
                  f'{nsig:>+8.1f}   {verdict}')
    if not any_combo:
        print('  no combination cells with all their single-slot cells present')

    posture_check(all_cells, args.posture)

    print('\nReading this: a gap is only real if sigma says so. At three')
    print('magazines a cell the game\'s own randomness alone produces gaps of')
    print('a few percent, so "3% off" and "exactly right" can be the same')
    print('measurement. Add magazines before believing a small effect.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
