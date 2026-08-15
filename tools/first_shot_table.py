"""The first shot, per gun: what the WALL says against what the CURVE plays.

    pixi run python tools/first_shot_table.py                 # read the logs
    pixi run python tools/first_shot_table.py --write         # ...and store it

WHY THIS FILE EXISTS AT ALL
---------------------------
⚠ `calibration/hole_groups.py` PRINTS ITS RESULT AND STORES NOTHING. Three aug
groups were fired on 2026-08-11 and the ratios they produced are gone -- the
run directories hold `g0_before.png` and `g0_after.png` and no number, so the
only way to learn what they measured is to re-detect the holes offline. Worse,
nothing recorded whether compensation was ON, which is the one thing that makes
a first-shot ratio mean something, and that cannot be recovered from the pixels
at all. **A measurement whose conditions are not written down is a number
without a subject.**

So the logs are teed per weapon and parsed here, and the table is written to
`docs/first_shot_holes.json` -- tracked, because the frames can be re-shot and
the reading cannot.

WHAT THE TABLE IS FOR, AND WHY THE RATIO ALONE CANNOT BE APPLIED
----------------------------------------------------------------
The wall gives `gap1 / gap2` -- how far the view moved between holes 1 and 2
against holes 2 and 3. The compensation curve has its own answer to the same
question: integrate `y_comp(t)` over the first round interval and over the
second. Those two ratios are the comparable pair.

    correction = wall(gap1/gap2) / curve(gap1/gap2)

⚠ APPLYING THE WALL RATIO DIRECTLY WOULD BE WRONG. It is not the curve's first
knot; it is the ratio the curve should REPRODUCE. A curve that already leans
that way needs less, and one that leans the other way needs more -- which is
exactly the case here: the camera reports the opening round as the SMALLEST of
the burst and the wall reports it as the largest.

⚠ AND COUNTS DO NOT TRAVEL BETWEEN RUNS. `hole_groups` measures px/count in the
scene it is standing in, and the same wall read -0.52 px/count from one spot and
-1.98 from another after a re-entry moved the character. The RATIO divides that
out; absolute counts from two runs are not the same quantity.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from calibration import rpm_store                              # noqa: E402
from calibration.samples import comp_counts_at                 # noqa: E402
from calibration.weapon_build import build_weapon              # noqa: E402

LOG_GLOB = os.path.join(ROOT, 'calibration', 'artifacts', 'holes',
                        'batch_*', '*.log')
OUT = os.path.join(ROOT, 'docs', 'first_shot_holes.json')

# `-> shot 1 recoil 15.84 counts, shot 2 recoil 7.84 counts, ratio 2.02`
RE_RATIO = re.compile(r'shot 1 recoil ([\d.]+) counts, shot 2 recoil '
                      r'([\d.]+) counts, ratio ([\d.]+)')
RE_GROUP = re.compile(r'── group (\d+): compensation (ON|OFF)')
RE_AGREE = re.compile(r'holes: (\d+)\s+rounds: (\d+)\s+AGREE: (True|False)')


def parse(path):
    """-> [{group, comp, holes, rounds, agree, gap1, gap2, ratio}, ...]

    ⚠ ONLY GROUPS THE TOOL ITSELF CALLED `AGREE` ARE KEPT. `holes != rounds`
    means the detector found marks the volley cannot account for -- an older
    group bleeding into the window, or spall -- and a ratio computed off that
    set describes a mixture. The tool already decides this; the parser must not
    quietly widen it.
    """
    out, cur = [], None
    for line in open(path, encoding='utf-8', errors='replace'):
        m = RE_GROUP.search(line)
        if m:
            cur = {'group': int(m.group(1)), 'comp': m.group(2).lower()}
            continue
        if cur is None:
            continue
        m = RE_AGREE.search(line)
        if m:
            cur.update(holes=int(m.group(1)), rounds=int(m.group(2)),
                       agree=m.group(3) == 'True')
            continue
        m = RE_RATIO.search(line)
        if m:
            cur.update(gap1=float(m.group(1)), gap2=float(m.group(2)),
                       ratio=float(m.group(3)))
            out.append(cur)
            cur = None
    return [g for g in out if g.get('agree')]


def curve_ratio(weapon, posture='standing', att=None):
    """What the CURVE ON DISK says the same two intervals should be.

    -> (gap1_counts, gap2_counts, ratio) or None when the cell has no curve.
    The interval is the MEASURED one from `rpm_store`, not the wiki RPM table,
    which `detector/weapon.py` records as wrong on a third of the roster.
    """
    # ⚠ `rpm_store.get` RETURNS **RPM**, NOT THE INTERVAL -- its own docstring
    # says "Measured RPM for one weapon" and I wrote a comment here asserting
    # the opposite without reading it. The number that came out was 721.80
    # "ms" for the aug, whose interval is 83.12 ms: a factor of 60 wrong, and
    # the correction it produced (2.23x for the m762) looked entirely
    # reasonable. A guess about an API, written down as a fact, is the same
    # failure as a guess about the game written down as a measurement.
    rec = rpm_store.load().get(weapon)
    dt_ms = rec.get('interval_ms') if isinstance(rec, dict) else None
    if not dt_ms:
        return None
    dt = float(dt_ms) / 1000.0
    # ⚠ A SCOPE IS PART OF THE CURVE KEY. `build_weapon(w, posture, {})` keys on
    # an EMPTY scope slot, which resolves to `iron` -- a sight this repository
    # has no curves for -- so every gun came back "(no curve)" while the curves
    # were on disk under the red dot. The measurement itself was fired with
    # whatever the spawner autofitted, so this is an assumption and it is
    # labelled as one in the output rather than folded in silently.
    w = build_weapon(weapon, posture,
                     dict(att or {}, scope='Upper_DotSight_01_C'))
    curve = [{'t_ms': t * 1000.0, 'dy': d}
             for t, d in zip(w.t_s, w.dy_s)] if w.t_s else []
    if not curve:
        return None
    at = comp_counts_at(curve, [0.0, dt, 2 * dt])
    g1, g2 = float(at[1] - at[0]), float(at[2] - at[1])
    if g2 <= 0:
        return None
    return g1, g2, g1 / g2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true')
    a = ap.parse_args()

    rows = {}
    for path in sorted(glob.glob(LOG_GLOB)):
        weapon = os.path.splitext(os.path.basename(path))[0]
        got = parse(path)
        if got:
            rows.setdefault(weapon, []).extend(got)

    if not rows:
        print(f'no usable groups under {LOG_GLOB}')
        return 1

    print(f'{"gun":<9}{"n":>3}{"comp":>6}{"wall gap1/gap2":>16}'
          f'{'curve gap1/gap2 (red_dot)':>26}{"correction":>12}')
    table = {}
    for w in sorted(rows):
        gs = rows[w]
        wall = sum(g['ratio'] for g in gs) / len(gs)
        comps = sorted({g['comp'] for g in gs})
        cr = curve_ratio(w)
        curve_s = f'{cr[2]:.2f}' if cr else '(no curve)'
        corr = f'{wall / cr[2]:.2f}x' if cr else '--'
        print(f'{w:<9}{len(gs):>3}{"/".join(comps):>6}{wall:>16.2f}'
              f'{curve_s:>17}{corr:>12}')
        table[w] = {
            'groups': gs,
            'wall_ratio_mean': round(wall, 3),
            'curve_ratio': None if cr else None if cr is None else round(cr[2], 3),
            'correction': None if cr is None else round(wall / cr[2], 3),
        }
        if cr:
            table[w]['curve_ratio'] = round(cr[2], 3)
            table[w]['curve_gap_counts'] = [round(cr[0], 2), round(cr[1], 2)]

    if a.write:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        json.dump({'measured': datetime.now().isoformat(timespec='seconds'),
                   'method': 'calibration/hole_groups.py, 3-round groups on the '
                             'Jump School concrete wall; only groups the tool '
                             'called AGREE are kept',
                   'weapons': table}, open(OUT, 'w', encoding='utf-8'),
                  indent=2, ensure_ascii=False)
        print(f'\nwrote {OUT}')
    else:
        print('\n(not stored — pass --write)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
