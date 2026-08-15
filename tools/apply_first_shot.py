"""Put the wall's first-shot measurement into a curve. One round, nothing else.

    pixi run python tools/apply_first_shot.py --weapon aug --ratio 3.54
    pixi run python tools/apply_first_shot.py --weapon aug --ratio 3.54 --write

WHAT IS BEING CORRECTED
-----------------------
The curve is fitted from camera motion, and the camera is at its weakest on the
opening round: the whole first kick lands inside one or two frames at 144 fps,
and the stored `y_obs` starts from a `cumsum`, so t=0 reads zero BY
CONSTRUCTION rather than by measurement. Bullet holes are outside that chain,
and they say the opening round is the LARGEST of the burst where the camera
says it is the smallest. Measured on the aug: curve 6.41 counts in round one
against 11.68 in round two, wall 3.54x the other way.

THE TWO GRIDS DO NOT LINE UP, AND THAT IS THE WHOLE ARITHMETIC
--------------------------------------------------------------
Knots sit on the CAPTURE cadence (17 ms). Rounds sit on the GUN's cadence
(83.12 ms for the aug, 53.08 for the vector). So "the first round" is a time
window, not a count of knots, and on the aug it ends 83.12 ms after it starts --
inside a knot, so that knot is SPLIT at the boundary first. Total counts and
every piecewise slope are preserved by the split; only the boundary moves.

⚠ AND THE WINDOW DOES NOT START AT ZERO. It starts at
`config.RECOIL_SHOT_VISIBLE_MS` = 51 ms, and getting that wrong is the single
most expensive thing this file has done. The window for the gap between hole k
and hole k+1 is

    [S + k*T,  S + (k+1)*T]        S = W + P + C = 51 ms

because a hole records the game's INTERNAL view at the instant the bullet left
(real time W + k*T), and that internal state does not reach a captured frame --
which is the only thing the curve was ever fitted from -- until L = 38 ms later.

⚠ IT USED TO SCALE `t < interval`, i.e. [0, T], AND THAT IS OFF BY A WHOLE
ROUND ON A FAST GUN. On the mg3 (T = 59.97 ms) the two windows overlap for
9 ms out of 60: 85% of [0, T] is time in which nothing has appeared on screen
yet, so the fitted curve is ~0 there BECAUSE THAT IS CORRECT, and dividing the
wall's ratio by that near-zero asked for 15.3x where the right window asks for
2.5x. Written, fired, and reported by the operator as 「压多了」 within the hour.

⚠ THE CHECK THAT SHOULD HAVE CAUGHT IT IS FREE AND IS NOW PRINTED: the wall
also gives gap1 in PIXELS, so gap1/K is an independent absolute estimate of the
same quantity. On the correct window the two agree (mg3: 27.15 asked for
against 28.3 measured, 4%); on [0, T] they did not (39.03 against 28.3, 38%).
⚠ It is a WEAK check, not a gate -- px/count is scene-dependent and the aug's
two routes still disagree 2.35x -- so it prints and never refuses.

⚠ AND THIS WRITES OVER A FIT. `y_true(t)` is refitted from the whole sample
store every time, so the next full refit ERASES this. That is not a bug and it
must not be silently defended against: the store is the authority for the parts
of the curve the camera can measure. The curve therefore carries
`first_shot_override` saying where the number came from, so nobody reads a
hand-written round as a fitted one.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import config as cfg                                          # noqa: E402
from calibration import rpm_store                             # noqa: E402
from calibration.samples import comp_counts_at                # noqa: E402


def knots_of(doc):
    """-> [(t_ms, dy, dx), ...] with ABSOLUTE times.

    ⚠ `delay_ms` IS AN INCREMENT, not a timestamp. The file reads 0, 17, 17,
    17 ... and a reader that takes those as absolute gets a curve three knots
    long that delivers everything at 17 ms.
    """
    t, out = 0.0, []
    for s in doc['shots']:
        t += float(s.get('delay_ms', 0))
        out.append((t, float(s.get('dy', 0.0)), float(s.get('dx', 0.0))))
    return out


def to_doc(doc, knots):
    """Write absolute knots back as increments, preserving everything else."""
    new = copy.deepcopy(doc)
    shots, prev = [], 0.0
    for t, dy, dx in knots:
        shots.append({'delay_ms': round(t - prev, 4), 'dx': dx, 'dy': dy})
        prev = t
    new['shots'] = shots
    return new


def counts(knots, ts):
    curve = [{'t_ms': t, 'dy': dy} for t, dy, _dx in knots]
    return [float(v) for v in comp_counts_at(curve, [x / 1000.0 for x in ts])]


def split_at(knots, boundary):
    """Split the knot straddling `boundary` so a window ends exactly there.

    Each knot's delta is spread evenly over its own window, so the piece before
    the boundary is `dy * (boundary - t) / dur` and the piece after is the rest.
    Total and slopes are unchanged; only the knot list gains one entry.
    """
    out = []
    for i, (t, dy, dx) in enumerate(knots):
        nxt = knots[i + 1][0] if i + 1 < len(knots) else t + (
            t - knots[i - 1][0] if i else 1.0)
        if t < boundary < nxt:
            frac = (boundary - t) / (nxt - t)
            out.append((t, dy * frac, dx * frac))
            out.append((boundary, dy * (1 - frac), dx * (1 - frac)))
        else:
            out.append((t, dy, dx))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', required=True)
    ap.add_argument('--config', default='bare')
    ap.add_argument('--ratio', type=float, required=True,
                    help='wall-measured gap1/gap2 for this gun')
    ap.add_argument('--source', default='',
                    help='where the ratio came from, stored in the curve')
    ap.add_argument('--offset-ms', type=float, default=cfg.RECOIL_SHOT_VISIBLE_MS,
                    dest='offset_ms',
                    help='when a round is VISIBLE on screen, ms after the '
                         'click. Default config.RECOIL_SHOT_VISIBLE_MS; see '
                         'the note there for what setting this to 0 cost.')
    ap.add_argument('--gap1-px', type=float, default=0.0, dest='gap1_px',
                    help="the wall's own gap1 in px, for the printed "
                         'cross-check. Optional; never gates.')
    ap.add_argument('--k', type=float, default=cfg.RECOIL_K_RED_DOT
                    if hasattr(cfg, 'RECOIL_K_RED_DOT') else 1.5413,
                    help='px per count for the sight, for --gap1-px only')
    ap.add_argument('--write', action='store_true')
    a = ap.parse_args()

    path = os.path.join(cfg.CURVES_DIR, f'{a.weapon}__{a.config}.json')
    if not os.path.exists(path):
        print(f'[!] no curve at {path}')
        return 1
    doc = json.load(open(path, encoding='utf-8'))
    rec = rpm_store.load().get(a.weapon)
    dt = rec.get('interval_ms') if isinstance(rec, dict) else None
    if not dt:
        print(f'[!] no measured interval for {a.weapon} — rpm_store has '
              f'{rec!r}. Without the gun cadence "the first round" has no '
              f'duration and this cannot be computed.')
        return 2

    s = a.offset_ms
    grid = [s, s + dt, s + 2 * dt]
    knots = knots_of(doc)
    before = counts(knots, grid)
    g1, g2 = before[1] - before[0], before[2] - before[1]
    target = a.ratio * g2
    print(f'{a.weapon} {a.config}: {len(knots)} knots, interval {dt:.2f} ms')
    print(f'  window [{s:.0f}, {s + dt:.0f}] vs [{s + dt:.0f}, {s + 2 * dt:.0f}] ms '
          f'(shot visible at {s:.0f} ms after the click)')
    print(f'  round 1 {g1:7.2f} counts   round 2 {g2:7.2f}   '
          f'curve ratio {g1 / g2:.3f}')
    print(f'  wall ratio {a.ratio:.3f}  ->  round 1 should be {target:.2f} '
          f'counts  ({target / g1:.2f}x)')
    if a.gap1_px:
        print(f'  cross-check: the wall\'s own {a.gap1_px:.1f} px / K {a.k:.4f} '
              f'= {a.gap1_px / a.k:.1f} counts, {target / (a.gap1_px / a.k):.2f}x '
              f'the target (scene-dependent, not a gate)')

    # Two boundaries, so the scaled span is exactly the one the holes measured.
    split = split_at(split_at(knots, s), s + dt)
    k = target / g1 if g1 > 0 else 0.0
    scaled = [(t, dy * k, dx * k) if s - 1e-9 <= t < s + dt - 1e-9
              else (t, dy, dx) for t, dy, dx in split]
    after = counts(scaled, grid)
    n1, n2 = after[1] - after[0], after[2] - after[1]
    print(f'  after: round 1 {n1:7.2f}   round 2 {n2:7.2f}   '
          f'ratio {n1 / n2:.3f}')
    # ⚠ THE HEAD IS A SECOND THING THAT MUST NOT MOVE, and it is a new check:
    # with the window starting at 51 ms there is now curve BEFORE it, and
    # nothing this measured says anything about that stretch.
    head_b, head_a = counts(knots, [0, s]), counts(scaled, [0, s])
    head_drift = abs((head_a[1] - head_a[0]) - (head_b[1] - head_b[0]))
    print(f'  pre-shot [0, {s:.0f}] drift {head_drift:.4f} counts '
          f'{"OK" if head_drift < 0.01 else "<-- LEAKED"}')
    # ⚠ ROUND TWO IS THE CHECK, NOT AN OUTPUT. It must not move: if it does,
    # the split failed and the correction leaked into a round the wall never
    # measured.
    drift = abs(n2 - g2)
    print(f'  round 2 drift {drift:.4f} counts '
          f'{"OK" if drift < 0.01 else "<-- LEAKED, do not write"}')
    tot_before, tot_after = sum(x[1] for x in knots), sum(x[1] for x in scaled)
    print(f'  magazine total {tot_before:.1f} -> {tot_after:.1f} counts')
    if drift >= 0.01 or head_drift >= 0.01:
        return 3

    if not a.write:
        print('\n(not written — pass --write)')
        return 0
    new = to_doc(doc, scaled)
    new['first_shot_override'] = {
        'ratio': a.ratio, 'interval_ms': dt,
        'round1_counts': round(n1, 3), 'was': round(g1, 3),
        'source': a.source or 'calibration/hole_groups.py, bullet holes',
        'note': 'HAND-WRITTEN over the fitted curve for t < interval_ms. A full '
                'refit from the sample store will erase it; that is correct, '
                'the store owns what the camera can measure.'}
    json.dump(new, open(path, 'w', encoding='utf-8'), indent=1)
    print(f'\nwrote {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
