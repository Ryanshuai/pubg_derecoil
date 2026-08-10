"""Every recoil-relevant kit a weapon can wear, enumerated -- not derived.

    pixi run kit-grid                 # report coverage
    pixi run kit-grid --write         # -> data/kit_grid.json
    pixi run kit-grid --selftest      # offline, no game, no hardware

WHY ENUMERATE INSTEAD OF STORING PER-SLOT COEFFICIENTS.

Because the slots are NOT orthogonal, and that is measured, not assumed.

⚠ THE FOUR MEASUREMENTS THAT SETTLE IT ARE IN calibration/build_kit_factors.py's
HEADER AND ARE NOT REPEATED HERE. That file is the one that took them (eight
weapons, 28 cells, 2026-08-05) and the one that writes the table they justify;
this file only lays the grid out. A second copy of four numbers is two things
free to drift, and the sigma figures are exactly the kind that get rounded on
the way past.

A product of per-slot numbers cannot represent what those four say. A table
can. So the table has a row for every combination the weapon can physically
wear, and a measurement replaces an estimate wherever one exists.

WHAT THIS FILE DOES NOT DO. It does not fit, average, merge or decide
anything. It lays out the grid and marks each cell's provenance. Deciding how
to fuse estimates into measurements is a later question, and keeping it later
is the point: a fused number that cannot say where it came from is exactly
what this project keeps paying for.

⚠ THE ESTIMATE IS A PLACEHOLDER AND IS LABELLED AS ONE. With no measurement
for a cell there is nothing to do but multiply per-part means -- the model the
evidence above rejects. That is acceptable for a cell nobody has fired only
because `src` says so on every row, so an analysis can drop them with one
filter. It is NOT acceptable to let an estimate silently stand in for a
measurement, which is why measured cells are copied verbatim and never
re-derived.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import config  # noqa: E402
from calibration.build_kit_factors import RECOIL_SLOTS, kit_key  # noqa: E402
from detector.attachment_catalog import SLOTS, compatible  # noqa: E402

OUT = os.path.join(config.DATA_DIR, 'kit_grid.json')

# Postures the grid covers -- all of them, from config.POSTURES. They are here
# because posture x kit was measured to interact, so a posture is part of a
# cell's identity rather than a multiplier on it.
from config import POSTURES                                  # noqa: E402,F401


def load_measured(path=None):
    """{weapon: {posture: {kit_key: row}}} from the measured table."""
    path = path or config.KIT_FACTORS_PATH
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f).get('kits', {})
    except (OSError, ValueError):
        return {}


def part_means(measured):
    """{slot: {part: mean f}} from the SINGLE-SLOT measured cells only.

    Single-slot cells only, because a multi-slot cell's factor belongs to the
    combination -- folding it back into a per-part mean would be assuming the
    very separability this table exists to avoid.
    """
    acc = {}
    for weapon, by_posture in measured.items():
        for posture, kits in by_posture.items():
            for key, row in kits.items():
                if '+' in key or '=' not in key:
                    continue                       # multi-slot or bare
                slot, part = key.split('=', 1)
                if slot not in RECOIL_SLOTS:
                    continue
                f = row.get('f') if isinstance(row, dict) else row
                if f:
                    acc.setdefault(slot, {}).setdefault(part, []).append(float(f))
    return {s: {p: sum(v) / len(v) for p, v in parts.items()}
            for s, parts in acc.items()}


def enumerate_kits(weapon):
    """Every kit this weapon can physically wear, as [{slot: part}, ...].

    Includes the bare kit ({}), and one entry per slot the weapon does not
    have -- a missing slot contributes exactly one option (nothing), which is
    what makes the product the right size rather than a guess at it.
    """
    comp = compatible(weapon)
    options = []
    for slot in RECOIL_SLOTS:
        options.append([None] + sorted(comp.get(slot, [])))
    kits = [{}]
    for slot, opts in zip(RECOIL_SLOTS, options):
        kits = [dict(k, **({slot: o} if o else {})) for k in kits for o in opts]
    return kits


def build(measured=None):
    """-> (grid, stats). Enumerates; fills measured first, estimates the rest."""
    measured = load_measured() if measured is None else measured
    means = part_means(measured)
    grid, n_meas, n_est = {}, 0, 0

    for weapon in sorted(SLOTS):
        per_posture = {}
        for posture in POSTURES:
            have = measured.get(weapon, {}).get(posture, {})
            cells = {}
            for kit in enumerate_kits(weapon):
                key = kit_key(kit)
                if key in have:
                    # ⚠ VERBATIM. A measured cell is never recomputed, not even
                    # to "harmonise" it with its neighbours.
                    cells[key] = dict(have[key])
                    n_meas += 1
                    continue
                f, unknown = 1.0, []
                for slot, part in sorted(kit.items()):
                    m = means.get(slot, {}).get(part)
                    if m is None:
                        unknown.append(f'{slot}={part}')
                    else:
                        f *= m
                cells[key] = {
                    'f': round(f, 4),
                    'src': 'estimated',
                    # Which parts had no per-part mean to stand on. An estimate
                    # resting on nothing is not the same as one resting on a
                    # measured mean, and a reader cannot tell them apart from
                    # the number.
                    'unpriced': unknown,
                    'n': 0,
                }
                n_est += 1
            per_posture[posture] = cells
        grid[weapon] = per_posture
    return grid, {'measured': n_meas, 'estimated': n_est,
                  'weapons': len(grid),
                  'priced_parts': {s: len(p) for s, p in sorted(means.items())}}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--weapon', help='report one weapon only')
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    grid, st = build()
    total = st['measured'] + st['estimated']
    print(f"weapons        {st['weapons']}")
    print(f"cells          {total}  ({st['measured']} measured, "
          f"{st['estimated']} estimated)")
    print(f"coverage       {st['measured'] / total:.2%}")
    print(f"priced parts   {st['priced_parts']}")

    if a.weapon:
        w = grid.get(a.weapon)
        if not w:
            print(f'\n[!] {a.weapon} is not in the catalogue')
            return 1
        print(f'\n{a.weapon} / standing')
        for key, row in sorted(w['standing'].items(),
                               key=lambda kv: kv[1]['f']):
            tag = 'M' if row.get('src') == 'measured' else ' '
            up = f"  unpriced={','.join(row['unpriced'])}" if row.get('unpriced') else ''
            print(f"  {tag} {row['f']:.4f}  {key or '(bare)'}{up}")

    if a.write:
        with open(OUT, 'w', encoding='utf-8') as f:
            json.dump({'_note': __doc__.strip().splitlines()[0],
                       '_slots': list(RECOIL_SLOTS),
                       '_postures': list(POSTURES),
                       '_stats': st,
                       'grid': grid}, f, indent=1, ensure_ascii=False)
        print(f'\nwrote {os.path.relpath(OUT, ROOT)}')
    return 0


def selftest():
    """Offline. Asserts the grid is a PRODUCT and that measurement wins."""
    fails = []

    # 1. the enumeration is the catalogue's product, not a subset of it
    for w in ('m416', 'mp5k', 'aug'):
        comp = compatible(w)
        want = 1
        for s in RECOIL_SLOTS:
            want *= 1 + len(comp.get(s, []))
        got = len(enumerate_kits(w))
        print(f'  {"OK  " if got == want else "FAIL"}  {w:8} '
              f'{got} kits, catalogue product = {want}')
        if got != want:
            fails.append(f'{w}: {got} != {want}')

    # 2. every measured cell survives into the grid UNCHANGED
    measured = load_measured()
    grid, _ = build(measured)
    checked = 0
    for weapon, by_posture in measured.items():
        for posture, kits in by_posture.items():
            for key, row in kits.items():
                cell = grid.get(weapon, {}).get(posture, {}).get(key)
                if cell is None:
                    fails.append(f'{weapon}/{posture}/{key} vanished')
                elif cell.get('f') != row.get('f'):
                    fails.append(f'{weapon}/{posture}/{key} '
                                 f"{cell.get('f')} != {row.get('f')}")
                else:
                    checked += 1
    print(f'  {"OK  " if not fails else "FAIL"}  {checked} measured cells '
          f'copied verbatim')

    # 3. an estimate must NEVER overwrite a measurement -- inject one and see
    fake = {'m416': {'standing': {'muzzle=comp_ar':
                                  {'f': 0.1234, 'src': 'measured', 'n': 9}}}}
    g2, _ = build(fake)
    got = g2['m416']['standing']['muzzle=comp_ar']
    ok = got.get('f') == 0.1234 and got.get('src') == 'measured'
    print(f'  {"OK  " if ok else "FAIL"}  a planted measurement wins over the '
          f'estimate (got {got.get("f")})')
    if not ok:
        fails.append('estimate overwrote a measurement')

    # 4. the bare kit is in the grid for every weapon -- it is the reference
    missing = [w for w in grid if '' not in grid[w]['standing']]
    print(f'  {"OK  " if not missing else "FAIL"}  bare kit present for all '
          f'{len(grid)} weapons')
    if missing:
        fails.append(f'bare kit missing: {missing[:5]}')

    print()
    if fails:
        for f in fails[:10]:
            print(f'  {f}')
        print(f'{len(fails)} failure(s)')
        return 1
    print('all checks pass.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
