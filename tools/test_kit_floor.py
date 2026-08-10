"""Gate on the SMG grip+muzzle floor that sits on explain_factor's 'parts' tier.

Four things, and the last two are why this is a file rather than a docstring:

  1. WHICH SLOTS. Every subset of RECOIL_SLOTS refitted, one parameter
     each, so the shipped `_FLOOR_SLOTS` has to win rather than be asserted.
     The three single-slot rows must come out EXACTLY equal to pure
     multiplication -- that is the arithmetic self-check, not a coincidence.
  2. THE CONSTANT. `_SMG_FLOOR_COUNTS` refitted from the file and compared to
     what ships. The slot list and the constant are fitted jointly, so a drift
     in either is a fail.
  3. WHETHER THE BRANCH CAN BE REACHED, and by what. It is switched on four
     conditions, and TODAY THE ANSWER IS ZERO -- both measured SMGs have three
     single-slot parts and all 2^3 combinations are already fired rows in
     `kits`, so tier 1 answers first every time. Printed every run, because a
     fallback that is never reached is indistinguishable from one that works.
  4. THE BRANCH ITSELF, against a SEEDED table rather than the shipped one --
     the same reason build_kit_factors' derived-row case seeds: a gate that can
     only test the file currently on disk is testing today's data, not the
     rule. Each case names the tier it must get, so both sides are structural:
     cases that must be REFUSED (AR, single floor slot, wiki-mixed, disagreeing
     bares) sit beside ones that must be ACCEPTED, and a floor that fired on
     everything would fail as loudly as one that never fired.
"""

from __future__ import annotations

import itertools
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from detector import weapon_attachments as wa           # noqa: E402
from detector.attachment_catalog import (ATTACHMENTS, RECOIL_SLOTS,  # noqa: E402
                                         weapon_class)

try:            # this gate prints box-drawing rules; a cp1252 console dies
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

KIT_PATH = ROOT / 'data' / 'kit_factors.json'


def _asset(key):
    return ATTACHMENTS[key]['asset']


def _mkrow(f, counts, src='measured'):
    return {'f': f, 'counts': counts, 'rel': 0.01, 'n': 6, 'src': src}


def seed(parts=None, kits=None):
    """Point the module at a synthetic table and return a restore callable."""
    old_p, old_k = wa._part_factors, wa._kit_factors
    wa._part_factors = parts or {}
    wa._kit_factors = kits or {}
    return lambda: (setattr(wa, '_part_factors', old_p),
                    setattr(wa, '_kit_factors', old_k))


# ── the fit ───────────────────────────────────────────────────────────────
def load_cells():
    d = json.loads(KIT_PATH.read_text(encoding='utf-8'))
    grouped = {}
    for w, post in d['kits'].items():
        for p, kits in post.items():
            for k, v in kits.items():
                key = tuple(sorted(x.split('=')[0] for x in k.split('+')))
                grouped.setdefault((w, p), {})[key] = v
    cells = []
    for (w, p), kits in grouped.items():
        singles = {k: v for k, v in kits.items() if len(k) == 1}
        if not singles:
            continue
        bare = float(np.mean([v['counts'] / v['f'] for v in kits.values()]))
        for slots, v in kits.items():
            if len(slots) < 2 or not all((x,) in singles for x in slots):
                continue
            cells.append(dict(w=w, cls=weapon_class(w), slots=slots,
                              R=v['counts'], rel=v['rel'], bare=bare,
                              f={s: singles[(s,)]['f'] for s in slots}))
    return cells


def predict(cell, r0, floor_slots):
    """Floor on `floor_slots`, plain product on the rest."""
    inside = [s for s in cell['slots'] if s in floor_slots]
    outside = [s for s in cell['slots'] if s not in floor_slots]
    rest = float(np.prod([cell['f'][s] for s in outside])) if outside else 1.0
    if not inside:
        return cell['bare'] * rest
    span = cell['bare'] - r0
    if span <= 0:
        return float('inf')
    q = [(cell['bare'] * cell['f'][s] - r0) / span for s in inside]
    if min(q) <= 0:
        return float('inf')
    return (r0 + span * float(np.prod(q))) * rest


def fit(cells, floor_slots):
    from scipy.optimize import minimize_scalar
    if not floor_slots:
        return None
    def cost(r0):
        return sum(((math.log(max(predict(c, r0, floor_slots), 1e-9))
                     - math.log(c['R'])) / c['rel']) ** 2 for c in cells)
    hi = min(c['bare'] for c in cells) * 0.98
    return float(minimize_scalar(cost, bounds=(-3000, hi),
                                 method='bounded').x)


def chi2(cells, r0, floor_slots):
    return sum(((math.log(max(predict(c, r0 or 0, floor_slots), 1e-9))
                 - math.log(c['R'])) / c['rel']) ** 2 for c in cells)


def report_subsets(cells):
    """The shipped slot list must WIN the scan, not merely be plausible."""
    smg = [c for c in cells if c['cls'] == 'SMG']
    rows = []
    for r in range(4):
        for sub in itertools.combinations(RECOIL_SLOTS, r):
            sub = frozenset(sub)
            r0 = fit(smg, sub)
            e = [abs(predict(c, r0 or 0, sub) / c['R'] - 1) * 100 for c in smg]
            rows.append((sub, chi2(smg, r0, sub), r0, np.mean(e), max(e)))
    rows.sort(key=lambda x: x[1])
    print(f"{'floor acts on':24s}{'chi2':>9s}{'R_min':>10s}"
          f"{'mean|err|':>11s}{'worst':>8s}")
    for sub, ch, r0, mean, worst in rows:
        name = '+'.join(sorted(sub)) or '(none) multiplied'
        print(f"{name:24s}{ch:9.1f}"
              f"{(f'{r0:10.1f}' if r0 else '         -')}"
              f"{mean:10.2f}%{worst:7.2f}%")

    shipped = frozenset(wa._FLOOR_SLOTS)
    won = rows[0][0] == shipped
    print(f"  [{'OK ' if won else 'FAIL'}] shipped _FLOOR_SLOTS "
          f"{tuple(sorted(shipped))} wins the scan"
          f" (best is {tuple(sorted(rows[0][0]))})")

    # single-slot rows must be numerically identical to no floor at all
    none_chi = next(ch for sub, ch, _, _, _ in rows if not sub)
    singles = [ch for sub, ch, _, _, _ in rows if len(sub) == 1]
    collapse = all(abs(ch - none_chi) < 1e-6 for ch in singles)
    print(f"  [{'OK ' if collapse else 'FAIL'}] a lone floor slot collapses to "
          f"the plain product ({len(singles)} subsets, chi2 {none_chi:.1f})")

    r0 = fit(smg, shipped)
    match = abs(r0 - wa._SMG_FLOOR_COUNTS) < 1.0
    print(f"  [{'OK ' if match else 'FAIL'}] refit R_min {r0:.1f} matches "
          f"shipped {wa._SMG_FLOOR_COUNTS}")

    for c in (x for x in cells if x['cls'] != 'SMG'):
        m = abs(predict(c, 0, frozenset()) / c['R'] - 1) * 100
        f = abs(predict(c, wa._SMG_FLOOR_COUNTS, shipped) / c['R'] - 1) * 100
        print(f"  NOT applied: {c['w']} {'+'.join(c['slots'])} -- "
              f"multiplied {m:.2f}% vs floored {f:.2f}%  <- the floor loses")
    return won and collapse and match


def report_disagreement(cells):
    """The two SMGs do not pick the same slot list. Say so every run."""
    smg = [c for c in cells if c['cls'] == 'SMG']
    print("each gun's own best subset (fitted on that gun alone):")
    for w in sorted({c['w'] for c in smg}):
        sub_cells = [c for c in smg if c['w'] == w]
        best = []
        for r in range(1, 4):
            for s in itertools.combinations(RECOIL_SLOTS, r):
                s = frozenset(s)
                r0 = fit(sub_cells, s)
                best.append(('+'.join(sorted(s)), chi2(sub_cells, r0, s), r0))
        best.sort(key=lambda x: x[1])
        n, ch, r0 = best[0]
        print(f"  {w:8s} {n:20s} chi2={ch:6.1f}  R_min={r0:6.1f}")
    print("  ^ they disagree about the stock. The shipped list is the better")
    print("    of the two POOLED, on 8 cells over 2 guns, chi2/dof ~19 against")
    print("    a noise floor of ~1. One fit, no reproduction behind it.")
    return True


def report_reach():
    d = json.loads(KIT_PATH.read_text(encoding='utf-8'))
    reach = 0
    for w, post in d.get('parts', {}).items():
        if weapon_class(w) != 'SMG':
            continue
        for p, rows in post.items():
            keys = sorted(rows)
            fired = set(d['kits'].get(w, {}).get(p, {}))
            for n in (2, 3):
                for combo in itertools.combinations(keys, n):
                    slots = {k.split('=')[0] for k in combo}
                    if not slots.issuperset(wa._FLOOR_SLOTS):
                        continue
                    if '+'.join(sorted(combo)) not in fired:
                        reach += 1
    print(f"kits the floor branch would answer on today's table: {reach}")
    if reach == 0:
        print("  ZERO -- both measured SMGs have 3 single-slot parts and every")
        print("  combination of them is already a fired row, so tier 1 answers")
        print("  first. The branch is correct and unreachable; it starts paying")
        print("  the first time an SMG gets a 4th measured part -- which is")
        print("  also the measurement that would settle the slot list above.")
    print("  and even when reached, explain_factor is NOT the compensation")
    print("  path -- plan A looks the curve up by exact configuration with no")
    print("  factor applied. The live caller is tools/import_kava4.py (seeds).")
    return True


# ── the branch ────────────────────────────────────────────────────────────
def cases():
    """(name, parts, kits, gun, (muzzle, grip, stock), want_tier)."""
    # bare 1000: vert_grip .75 -> 750, comp_smg .60 -> 600, heavy .90 -> 900
    smg = {'mp5k': {'standing': {
        'grip=vert_grip': _mkrow(0.75, 750.0),
        'muzzle=comp_smg': _mkrow(0.60, 600.0),
        'stock=heavy_stock': _mkrow(0.90, 900.0),
    }}}
    ar = {'m416': {'standing': {
        'grip=vert_grip': _mkrow(0.75, 1500.0),        # bare 2000
        'muzzle=comp_ar': _mkrow(0.60, 1200.0),
    }}}
    return [
        ("SMG, both floor slots filled",
         smg, {}, 'mp5k', ('comp_smg', 'vert_grip', None), 'parts_floor'),
        ("SMG, both floor slots + a stock (stock multiplies)",
         smg, {}, 'mp5k', ('comp_smg', 'vert_grip', 'heavy_stock'),
         'parts_floor'),
        ("SMG, grip+stock -- muzzle missing, floor must not claim it",
         smg, {}, 'mp5k', (None, 'vert_grip', 'heavy_stock'), 'parts'),
        ("SMG, muzzle+stock -- grip missing, likewise",
         smg, {}, 'mp5k', ('comp_smg', None, 'heavy_stock'), 'parts'),
        ("SMG, one slot only",
         smg, {}, 'mp5k', (None, 'vert_grip', None), 'parts'),
        ("AR, both floor slots -- floor is SMG-only",
         ar, {}, 'm416', ('comp_ar', 'vert_grip', None), 'parts'),
        ("SMG, muzzle unmeasured -- no counts to floor",
         {'mp5k': {'standing': {'grip=vert_grip': _mkrow(0.75, 750.0)}}}, {},
         'mp5k', ('comp_smg', 'vert_grip', None), 'parts'),
        ("SMG, single-slot rows disagree on bare -- refuse",
         {'mp5k': {'standing': {
             'grip=vert_grip': _mkrow(0.75, 750.0),        # bare 1000
             'muzzle=comp_smg': _mkrow(0.60, 720.0)}}},    # bare 1200
         {}, 'mp5k', ('comp_smg', 'vert_grip', None), 'parts'),
        ("fired kit outranks the floor",
         smg, {'mp5k': {'standing': {
             'grip=vert_grip+muzzle=comp_smg': _mkrow(0.4321, 432.1)}}},
         'mp5k', ('comp_smg', 'vert_grip', None), 'kit'),
    ]


def run_cases():
    ok = True
    for name, parts, kits, gun, worn, want in cases():
        restore = seed(parts, kits)
        try:
            args = [_asset(k) if k else '' for k in worn]
            f, src, _ = wa.explain_factor(gun, *args)
        finally:
            restore()
        good = src == want
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] {name}\n"
              f"         got {src!r} f={f:.4f}, want {want!r}")
    return ok


def check_arithmetic():
    """R_min=0 must collapse to the product; the stock must stay outside."""
    parts = {'mp5k': {'standing': {
        'grip=vert_grip': _mkrow(0.75, 750.0),
        'muzzle=comp_smg': _mkrow(0.60, 600.0),
        'stock=heavy_stock': _mkrow(0.90, 900.0)}}}
    restore = seed(parts, {})
    old = wa._SMG_FLOOR_COUNTS
    try:
        wa._SMG_FLOOR_COUNTS = 0.0
        f0, src0, _ = wa.explain_factor('mp5k', _asset('comp_smg'),
                                        _asset('vert_grip'))
        wa._SMG_FLOOR_COUNTS = 200.0
        f2, _, _ = wa.explain_factor('mp5k', _asset('comp_smg'),
                                     _asset('vert_grip'))
        f3, _, _ = wa.explain_factor('mp5k', _asset('comp_smg'),
                                     _asset('vert_grip'),
                                     _asset('heavy_stock'))
    finally:
        wa._SMG_FLOOR_COUNTS = old
        restore()
    want0 = 0.45                                        # 0.75 * 0.60
    want2 = (200 + 800 * (550 / 800) * (400 / 800)) / 1000
    want3 = want2 * 0.90                                # stock multiplies
    checks = [
        ("R_min=0 collapses to the product", src0 == 'parts_floor'
         and abs(f0 - want0) < 1e-9, f0, want0),
        ("R_min=200 matches the formula", abs(f2 - want2) < 1e-9, f2, want2),
        ("the stock stays OUTSIDE the floor", abs(f3 - want3) < 1e-9,
         f3, want3),
    ]
    ok = True
    for label, good, got, want in checks:
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] {label}  "
              f"({got:.6f} vs {want:.6f})")
    return ok


def main():
    cells = load_cells()
    print("── which slots ──")
    ok = report_subsets(cells)
    print("\n── the two guns disagree ──")
    ok &= report_disagreement(cells)
    print("\n── reach ──")
    ok &= report_reach()
    print("\n── branch (seeded tables) ──")
    ok &= run_cases()
    print("\n── arithmetic ──")
    ok &= check_arithmetic()
    print(f"\n{'GREEN' if ok else 'RED'}")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
