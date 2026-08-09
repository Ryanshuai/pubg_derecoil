"""A PRIOR for a cell nobody has fired, built only from cells somebody has.

    pixi run estimate --weapon mp5k --config muzzle-comp_smg_grip-vert_grip_stock-tactical_stock --sight 2x
    pixi run estimate ... --write        # ship it so the runtime plays it

⚠ WHAT THIS PRODUCES IS NOT A MEASUREMENT AND IS MARKED `seed: True`, which is
the same flag import_kava4 sets and which detector.weapon announces on every
load ("is a SEED, not a measurement"). The runtime cannot tell the two apart
from the shots alone -- that is the root CLAUDE.md's second law -- so the flag
is the only thing that keeps them apart, and it is not optional.

It refuses to overwrite a fit, for the reason import_kava4 states: a guess
replacing a measurement is the one direction that loses information, and the
file keeps the same name either way.


WHY A BORROWED SHAPE IS AN ACCEPTABLE PRIOR, MEASURED RATHER THAN ASSUMED
────────────────────────────────────────────────────────────────────────
A curve is a shape and a scale. This borrows the shape from the nearest
measured cell and computes only the scale -- which is worth doing only if the
shape is the stable part. It is. Normalised cumulative curves, max and mean
deviation as a fraction of the burst total (2026-08-09, over the shipped
curves):

    OPTIC   mp5k bare red_dot vs 2x               max 2.8%   mean 1.4%
            mp5k bare red_dot vs 3x               max 2.7%   mean 1.4%
    KIT     mp5k bare vs comp+vert+heavy          max 3.2%   mean 1.4%
            vector bare vs comp_smg               max 4.7%   mean 1.5%
            vector heavy stock vs tactical stock  max 1.1%   mean 0.6%

So changing the optic, or changing an entire three-part kit, moves the SHAPE
by a few percent while moving the TOTAL by tens of percent. The scale is where
the answer lives and the shape is nearly free.

⚠ THAT IS A STATEMENT ABOUT THESE TWO GUNS, and both are SMGs with sub-3 s
bursts. Nothing here has checked it on a 3.9 s SCAR.


THE SCALE, AND WHERE EACH FACTOR IN IT COMES FROM
─────────────────────────────────────────────────
    total = donor_total  x  (kit factors)  x  (coupling)  x  (optic ratio)

Every factor is a RATIO OF TWO CELLS OF THE SAME GUN wherever one exists,
because that is what cancels the per-run multiplicative error the store is
known to carry (calibration/CLAUDE.md). Where one does not exist, the fallback
is named in the output and in the written file, never silently substituted.

    kit       f(part) = <weapon>__<slot>-<part> / <weapon>__bare, both at the
              same optic. Measured, same gun, same coordinate.

    coupling  the slots DO NOT multiply -- mp5k muzzle x grip is +6.6%
              [+4.9, +8.2] and all three +15.4% [+12.5, +18.2] during the
              burst. Taken from the same gun's 2^3 cube when it has one, else
              from data/kit_factors.json's old-coordinate cube, else 1.0 with
              a warning. ⚠ A missing coupling term biases the prior LOW on the
              guns that have one, i.e. it under-compensates, which is the safe
              direction: the view drifts up rather than being driven down.

    optic     r(sight) = <weapon>__bare__<sight> / <weapon>__bare when both
              exist. Otherwise config.RECOIL_SIGHT_RATIO -- see below.


⚠ THE OPTIC TERM IS THE WEAKEST LINK, AND ONE VERSION OF IT ALREADY SHIPPED
WRONG
──────────────────────────────────────────────────────────────────────────
The first version of this file fitted r = 0.442 x magnification to the mp5k's
two scoped cells (2x 0.8803, 3x 1.3304 -- 0.4402 and 0.4435 per unit, 0.75%
apart, which looked like a law). It shipped a 4x prior at 1.768 and the
operator reported the high magnification as simply unusable, "like the 1x
coefficient is what is being played".

⚠ THE LAW WAS FITTED TO TWO POINTS THAT DISAGREE WITH PHYSICS AND WITH EACH
OTHER. Written out as counts, the mp5k bare cell nulls at

    red dot  ~900        2x  834        3x  624

-- which is NOT MONOTONE IN MAGNIFICATION. More zoom cannot need less
compensation for a fixed angular kick. Four magazines with a 92-count lever
between the arms is not enough to measure r, and a straight line through two
such points is not a law however tight it looks.

So the optic term now comes from config.RECOIL_SIGHT_RATIO: ONE NUMBER PER
OPTIC, relative to the red dot, which is what the operator asked for after
this went wrong. It replaces a two-field derivation (`mag` x `K`) precisely
because nobody could tell which field to correct.

⚠ A MEASURED CELL FOR THAT GUN AND OPTIC STILL BEATS THE TABLE. The table is
the fallback for an optic nobody has fired on that gun.

⚠ AND r IS NOT CONSTANT ACROSS ONE BURST even where it is measured: it spans
5.6-5.7% between t=1.2 s and t=2.4 s on both magnifications. A single scalar
cannot represent that and is not trying to.
"""
import argparse
import json
import os
import statistics
import sys

try:                                    # the ⚠ and ✗ below are not decoration
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg                                       # noqa: E402
from detector.attachment_catalog import ATTACHMENTS        # noqa: E402

REF_SIGHT = 'red_dot'


def _load(stem):
    p = os.path.join(cfg.CURVES_DIR, stem + '.json')
    if not os.path.exists(p):
        return None
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def _stem(weapon, config_key_str, sight):
    tag = '' if sight in (REF_SIGHT, None) else f'__{sight}'
    return f'{weapon}__{config_key_str or "bare"}{tag}'


def optic_ratio(weapon, sight, why):
    """counts at `sight` / counts at the red dot, and how we know."""
    if sight == REF_SIGHT:
        return 1.0
    here = _load(_stem(weapon, 'bare', sight))
    ref = _load(_stem(weapon, 'bare', REF_SIGHT))
    if here and ref:
        r = here['total_counts'] / ref['total_counts']
        why.append(f'optic  r({sight}) = {r:.4f}  MEASURED on this gun '
                   f'({here["total_counts"]:.0f}/{ref["total_counts"]:.0f} '
                   f'counts, bare)')
        return r
    r = cfg.RECOIL_SIGHT_RATIO.get(sight)
    if r is None:
        why.append(f'optic  r({sight}) = 1.0  ⚠ NOT IN config.'
                   f'RECOIL_SIGHT_RATIO — the prior is the red dot\'s, which '
                   f'is certainly wrong. Add the sight to that table.')
        return 1.0
    why.append(f'optic  r({sight}) = {r:.4f}  ⚠ NOT MEASURED on any gun — '
               f'config.RECOIL_SIGHT_RATIO, one number per optic, move it '
               f'there if play says it is off')
    return r


def _part_across_guns(slot, part, skip):
    """[(gun, factor)] for every OTHER gun that measured this part alone."""
    out = []
    for name in sorted(os.listdir(cfg.CURVES_DIR)):
        if not name.endswith(f'__{slot}-{part}.json'):
            continue
        gun = name.split('__', 1)[0]
        if gun == skip:
            continue
        cell, bare = _load(name[:-5]), _load(f'{gun}__bare')
        if cell and bare and not cell.get('seed') and not bare.get('seed'):
            out.append((gun, cell['total_counts'] / bare['total_counts']))
    return out


def kit_scale(weapon, parts, sight, why):
    """Product of single-part factors, this gun first, other guns as a last tier.

    ⚠ THE CROSS-GUN TIER IS THE WEAKEST THING IN THIS FILE AND IT IS DELIBERATE.
    `calibrate-recoil` says factors are never borrowed across guns, and it is
    right about MEASUREMENTS -- vert_grip reads 0.7470 / 0.7723 / 0.7875 /
    0.7959 on four guns with sems of 1%, so a borrowed number is wrong by more
    than its own error bar. This tier exists because the alternative for a cell
    whose part this gun has never worn is NO CURVE AT ALL, and no curve means
    the view reaches open sky where the correlator returns 0 confidently.
    It prints the spread across the guns it averaged, which IS the error bar
    on the borrow, and the written file carries the same line.
    """
    scale = 1.0
    ref = _load(_stem(weapon, 'bare', sight)) or _load(
        _stem(weapon, 'bare', REF_SIGHT))
    if not ref:
        return None
    at = REF_SIGHT if _load(_stem(weapon, 'bare', sight)) is None else sight
    for slot, part in sorted(parts.items()):
        cell = _load(_stem(weapon, f'{slot}-{part}', at))
        if cell:
            f = cell['total_counts'] / ref['total_counts']
            scale *= f
            why.append(f'kit    f({part}) = {f:.4f}  measured, this gun, {at}')
            continue
        others = _part_across_guns(slot, part, weapon)
        if not others:
            why.append(f'kit    f({part}): ✗ not measured on {weapon}, and no '
                       f'other gun has it either')
            return None
        vals = [v for _g, v in others]
        f = statistics.median(vals)
        spread = (max(vals) - min(vals)) / f if len(vals) > 1 else 0.0
        scale *= f
        why.append(f'kit    f({part}) = {f:.4f}  ⚠ BORROWED — {weapon} has '
                   f'never worn it. Median of ' +
                   ', '.join(f'{g} {v:.4f}' for g, v in others) +
                   f' (spread {100 * spread:.1f}% of the value, and that '
                   f'spread IS the error bar)')
    return scale


def coupling(weapon, parts, why):
    """How much the slots FAIL to multiply, as a factor on their product."""
    if len(parts) < 2:
        return 1.0
    d = {}
    if os.path.exists(cfg.KIT_FACTORS_PATH):
        with open(cfg.KIT_FACTORS_PATH, encoding='utf-8') as f:
            d = json.load(f)
    kits = (d.get('kits', {}).get(weapon, {}).get('standing', {}))
    # The old cube is keyed slot=part, joined by '+', sorted.
    singles, whole = [], None
    for k, v in kits.items():
        got = dict(p.split('=', 1) for p in k.split('+') if '=' in p)
        if len(got) == 1:
            slot, part = next(iter(got.items()))
            if slot in parts:
                singles.append((slot, v['f']))
        elif set(got) == set(parts):
            whole = v['f']
    if whole is None or len(singles) != len(parts):
        why.append(f'couple ⚠ 1.0 — no {len(parts)}-slot cube for this gun in '
                   f'kit_factors.json, so the prior assumes the slots '
                   f'multiply. They do not (mp5k: +15.4% for three), and the '
                   f'error is toward UNDER-compensating.')
        return 1.0
    prod = 1.0
    for _s, f in singles:
        prod *= f
    c = whole / prod
    why.append(f'couple {c:.4f} ({100 * (c - 1):+.1f}%) — this gun\'s cube in '
               f'data/kit_factors.json, which is the RETIRED bullet-bucket '
               f'coordinate. ⚠ Its stock may differ from the one asked for.')
    return c


def check():
    """Estimate every MEASURED multi-part cell and report how far off it is.

    ⚠ THIS IS THE ONLY NUMBER THAT SAYS WHETHER ANY OF THIS IS WORTH SHIPPING.
    Every factor above is defensible on its own; the question the user of a
    prior actually has is "how wrong is it", and the store answers that
    directly for the cells somebody already fired. A prior with no hold-out is
    a chain of plausible reasoning, which is what this repository keeps paying
    for.

    ⚠ IT CANNOT CHECK THE OPTIC TERM. Every measured multi-part cell is at the
    red dot, so the extrapolated r() -- the weakest link by far -- goes
    entirely untested here. What comes back is the error of the KIT half only.
    """
    rows = []
    for name in sorted(os.listdir(cfg.CURVES_DIR)):
        if not name.endswith('.json'):
            continue
        d = _load(name[:-5])
        if not d or d.get('seed') or len(d.get('config') or {}) < 2:
            continue
        if d.get('sight') != REF_SIGHT or d.get('posture') != 'standing':
            continue
        weapon, parts = d['weapon'], d['config']
        why = []
        bare = _load(_stem(weapon, 'bare', REF_SIGHT))
        if not bare:
            continue
        scale = kit_scale(weapon, parts, REF_SIGHT, why)
        if scale is None:
            continue
        est = bare['total_counts'] * scale * coupling(weapon, parts, why)
        got = d['total_counts']
        rows.append((name[:-5], got, est, 100 * (est - got) / got,
                     any('BORROWED' in w for w in why),
                     any('couple ⚠' in w for w in why)))
    if not rows:
        print('no measured multi-part cell to check against')
        return 1
    print(f'{"cell":<58} {"measured":>9} {"prior":>9} {"err":>8}  notes')
    for stem, got, est, err, borrowed, nocouple in rows:
        note = ' '.join(t for t, on in (('borrowed-part', borrowed),
                                        ('no-coupling', nocouple)) if on)
        print(f'{stem:<58} {got:>9.1f} {est:>9.1f} {err:>+7.1f}%  {note}')
    errs = [abs(r[3]) for r in rows]
    print(f'\n  n={len(errs)}  median |err| {statistics.median(errs):.1f}%  '
          f'worst {max(errs):.1f}%')
    print('  ⚠ KIT HALF ONLY — every cell here is at the red dot, so the '
          'extrapolated optic ratio is untested.')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true',
                    help='hold-out: estimate the cells that ARE measured')
    ap.add_argument('--weapon')
    ap.add_argument('--config', default='bare',
                    help='cell name, config.parse_config_key grammar')
    ap.add_argument('--sight', default=REF_SIGHT)
    ap.add_argument('--posture', default='standing')
    ap.add_argument('--write', action='store_true',
                    help='ship it to config.CURVES_DIR (refuses over a fit)')
    a = ap.parse_args()
    if a.check:
        return check()
    if not a.weapon:
        ap.error('--weapon or --check')

    parts = cfg.parse_config_key(a.config)
    if parts is None:
        print(f'  [!] --config {a.config!r} is not a cell name.')
        return 2
    for slot, part in parts.items():
        if part not in ATTACHMENTS:
            print(f'  [!] {part!r} is not a catalogue key.')
            return 2

    want = _stem(a.weapon, cfg.config_key(parts), a.sight)
    existing = _load(want)
    if existing and not existing.get('seed'):
        print(f'  ✗ {want}.json is a FITTED curve '
              f'({existing["total_counts"]:.0f} counts from '
              f'{existing.get("n_magazines", "?")} magazines). Nothing to '
              f'estimate — fire it if you want it better.')
        return 1

    why = []
    donor_stem = _stem(a.weapon, cfg.config_key(parts), REF_SIGHT)
    donor = _load(donor_stem)
    if donor and not donor.get('seed'):
        why.append(f'shape  {donor_stem} — SAME KIT, only the optic differs')
    else:
        donor_stem = _stem(a.weapon, 'bare', REF_SIGHT)
        donor = _load(donor_stem)
        if not donor:
            print(f'  [!] {a.weapon} has no bare curve at the red dot; there '
                  f'is nothing on this gun to build a prior from.')
            return 1
        why.append(f'shape  {donor_stem} — bare, because no cell with this '
                   f'kit has been fired')

    base = donor['total_counts']
    if 'SAME KIT' in why[0]:
        scale, coup = 1.0, 1.0
        why.append('kit    1.0 — the donor already wears it')
    else:
        scale = kit_scale(a.weapon, parts, REF_SIGHT, why)
        if scale is None:
            print('\n'.join('  ' + w for w in why))
            print(f'  [!] cannot price this kit on {a.weapon} — a single-part '
                  f'cell is missing. Fire the singles, or ask for a config '
                  f'this gun has.')
            return 1
        coup = coupling(a.weapon, parts, why)
    r = optic_ratio(a.weapon, a.sight, why)

    total = base * scale * coup * r
    print(f'\n{a.weapon} {cfg.config_key(parts)} {a.posture} {a.sight}')
    print(f'  donor {base:.1f} counts')
    for w in why:
        print(f'  {w}')
    print(f'  => {base:.1f} x {scale:.4f} x {coup:.4f} x {r:.4f} = '
          f'{total:.1f} counts')

    k = total / base
    doc = dict(donor)
    doc['weapon'] = a.weapon
    doc['config'] = parts
    doc['sight'] = a.sight
    doc['posture'] = a.posture
    doc['shots'] = [{'delay_ms': s['delay_ms'],
                     'dx': float(s['dx']) * k,
                     'dy': float(s['dy']) * k} for s in donor['shots']]
    doc['total_counts'] = total
    doc['seed'] = True
    doc['estimated_from'] = donor_stem
    doc['derivation'] = why
    doc['source'] = (f'ESTIMATE, not a measurement. Shape borrowed from '
                     f'{donor_stem} and scaled by {k:.4f}. Every factor is '
                     f'in `derivation`. tools/estimate_cell.py says why a '
                     f'borrowed shape is defensible and why the optic term '
                     f'is the weak one. Fire this cell and the fit replaces '
                     f'it.')
    for gone in ('n_magazines', 'n_total', 'spread_counts', 'samples_per_knot',
                 'centre', 'n_windowed', 'padded_knots', 'kit_factor',
                 'borrowed_from'):
        doc.pop(gone, None)

    if not a.write:
        print(f'  (not written — pass --write)')
        return 0
    path = os.path.join(cfg.CURVES_DIR, want + '.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
    print(f'  wrote {path}')
    print(f'  ⚠ it loads as a SEED and the runtime will say so every start.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
