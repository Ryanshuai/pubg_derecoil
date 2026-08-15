"""How many counts does the tracker BOOK AS ZERO, and where in the burst?

    pixi run python tools/audit_dropped_pairs.py
    pixi run python tools/audit_dropped_pairs.py --weapon aug --head-ms 140

WHAT IS BEING AUDITED
---------------------
A frame pair can fail to produce a number in two independent ways, and BOTH of
them end as a zero rather than as a hole:

    detector/view_tracker.py:170   every patch rejected     -> dy = nan
    calibration/samples.py:424     the pair is `oor`        -> counts = nan
    calibration/samples.py:426     np.nancumsum(counts)     -> nan reads as 0

⚠ THE SECOND STEP IS THE WHOLE POINT. `nancumsum` does not propagate and does
not interpolate: a pair that could not be measured contributes ZERO
DISPLACEMENT to the cumulative sum, and every later sample carries that deficit
forever. So the failure mode is not noise, it is a ONE-SIDED LOSS -- the view
moved, and the record says it did not.

⚠ AND IT IS ONE-SIDED IN THE WORST PLACE. Both rejection routes fire hardest
where the picture is changing fastest: the muzzle flash kills the texture gate,
and the seven patches disagree during a transient. That is the opening rounds,
which is exactly where the bullet holes say the curve reads ~3x too small.

SO THIS COUNTS THREE THINGS, and the third is the one that answers anything:

    rate       what fraction of pairs came back nan          (how often)
    where      that rate as a function of t since the click  (is it the head)
    counts     what those pairs would have carried           (how much)

⚠ THE THIRD IS AN ESTIMATE AND IT IS DELIBERATELY GENEROUS. A pair that was
never measured has no true value, so its displacement is imputed from the
LOCAL RATE of its finite neighbours (counts per second across the gap it
spans). That over-states the loss wherever a pair was rejected precisely
because nothing was moving -- which is the right direction for a bound: if the
generous estimate cannot reach the 3x, the mechanism is not the explanation.

⚠ IT IS AN AUDIT, NOT A FIX. Nothing here writes. If the loss turns out to
matter, the repair is at the point of measurement -- and imputing it at read
time would be exactly the smoothing the root CLAUDE.md forbids, spreading one
interval's deviation over its neighbours.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from calibration import samples as S                          # noqa: E402


def pair_table(mag):
    """-> (t_pair, counts, dead) for every frame pair in one magazine.

    `t_pair` is the LATER frame's time, because a shift describes an interval
    and samples.y_obs_counts attributes it to t[i+1]. Getting this wrong puts
    every pair one frame early -- the same class of error the store's own
    docstring records.
    """
    t = np.asarray(mag.t, dtype=float)
    dy = np.asarray(mag.dy_px, dtype=float)
    human = (np.asarray(mag.human_dy, dtype=float) if mag.human_dy
             else np.zeros_like(dy))
    oor = (np.asarray(mag.oor, dtype=bool) if mag.oor
           else np.zeros(len(dy), dtype=bool))
    n = min(len(dy), len(t) - 1)
    if n <= 0:
        return None
    counts = dy[:n] / S.analysis_k(mag) + human[:n]
    dead = ~np.isfinite(counts) | oor[:n]
    return t[1:n + 1], counts, dead


def impute(t_pair, t_prev, counts, dead):
    """Counts the dead pairs would have carried, at the local finite rate.

    Generous by construction -- see the module docstring. Returns 0.0 when a
    magazine has no finite pair to borrow a rate from, rather than guessing.
    """
    dur = t_pair - t_prev
    live = ~dead & (dur > 0)
    if not live.any():
        return np.zeros_like(counts)
    rate = float(np.sum(counts[live]) / np.sum(dur[live]))
    out = np.zeros_like(counts)
    out[dead] = rate * dur[dead]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', default='')
    ap.add_argument('--config', default='')
    ap.add_argument('--head-ms', type=float, default=140.0, dest='head_ms',
                    help='the window the bullet holes disagree over; pairs at '
                         '0 <= t < this are counted separately')
    a = ap.parse_args()

    root = os.path.join(ROOT, 'calibration', 'artifacts', 'recoil', 'samples')
    files = sorted(f for f in os.listdir(root) if f.endswith('.jsonl'))
    if a.weapon:
        files = [f for f in files if f.split('__')[0] == a.weapon]
    if a.config:
        files = [f for f in files if a.config in f]

    tot = {'pairs': 0, 'dead': 0, 'lost': 0.0, 'moved': 0.0}
    head = {'pairs': 0, 'dead': 0, 'lost': 0.0, 'moved': 0.0}
    rows, n_mags, worst = [], 0, []
    # Rate against time, so "is it the head" is answered by an ORDERED
    # criterion and not by one pooled number (root CLAUDE.md).
    edges = np.array([-1e9, 0, 60, 140, 300, 600, 1200, 2400, 1e9])
    by_t = np.zeros((len(edges) - 1, 2))

    for fn in files:
        stem = fn[:-6]
        weapon = stem.split('__')[0]
        cfg = stem.split('__', 1)[1] if '__' in stem else 'bare'
        try:
            # ⚠ BY PATH, NOT BY (weapon, config). `config` is the dict of
            # slots, and handing it the filename's config STRING silently
            # raises inside path_for -- 74 files "failed to load" with a
            # message about .items() and nothing said the audit was empty.
            mags = S.load(weapon, path=os.path.join(root, fn))
        except Exception as e:                                # noqa: BLE001
            print(f'  [!] {stem}: {e}')
            continue
        f_pairs = f_dead = 0
        f_lost = f_moved = 0.0
        for m in mags:
            tab = pair_table(m)
            if tab is None:
                continue
            t_pair, counts, dead = tab
            t_prev = np.asarray(m.t, dtype=float)[:len(t_pair)]
            lost = impute(t_pair, t_prev, np.nan_to_num(counts), dead)
            n_mags += 1
            ms = t_pair * 1000.0
            in_head = (ms >= 0) & (ms < a.head_ms)
            moved = np.abs(np.nan_to_num(counts))
            for bag, sel in ((tot, np.ones_like(dead)), (head, in_head)):
                bag['pairs'] += int(np.sum(sel))
                bag['dead'] += int(np.sum(dead & sel))
                bag['lost'] += float(np.sum(np.abs(lost[sel.astype(bool)])))
                bag['moved'] += float(np.sum(moved[sel.astype(bool)]))
            idx = np.clip(np.searchsorted(edges, ms, 'right') - 1,
                          0, len(edges) - 2)
            for i, d in zip(idx, dead):
                by_t[i, 0] += 1
                by_t[i, 1] += int(d)
            f_pairs += len(dead)
            f_dead += int(dead.sum())
            f_lost += float(np.sum(np.abs(lost)))
            f_moved += float(np.sum(moved))
            if dead.sum():
                worst.append((int(dead.sum()), stem, len(dead)))
        if f_pairs:
            rows.append((stem, len(mags), f_pairs, f_dead, f_lost, f_moved))

    if not tot['pairs']:
        print('no magazines matched')
        return 1

    print(f'{n_mags} magazines, {tot["pairs"]} frame pairs\n')
    print('WHOLE BURST')
    print(f'  dead pairs      {tot["dead"]:8d} / {tot["pairs"]}  '
          f'{100.0 * tot["dead"] / tot["pairs"]:.3f}%')
    print(f'  counts booked as zero (generous)  {tot["lost"]:9.1f}')
    print(f'  counts actually measured          {tot["moved"]:9.1f}  '
          f'-> the loss is {100.0 * tot["lost"] / max(tot["moved"], 1e-9):.3f}%')

    print(f'\nHEAD  0 <= t < {a.head_ms:.0f} ms   '
          f'(the window the holes disagree over)')
    if head['pairs']:
        print(f'  dead pairs      {head["dead"]:8d} / {head["pairs"]}  '
              f'{100.0 * head["dead"] / head["pairs"]:.3f}%')
        print(f'  counts booked as zero (generous)  {head["lost"]:9.1f}')
        print(f'  counts actually measured          {head["moved"]:9.1f}')
        # ⚠ THE ONLY NUMBER THAT ANSWERS THE QUESTION. A 3x under-read needs
        # the loss to be about 2x what was measured; anything far below that
        # and this mechanism is not what the bullet holes are seeing.
        need = head['moved'] * 2.0
        print(f'\n  to explain a 3x under-read this had to delete ~{need:.1f} '
              f'counts. It deletes {head["lost"]:.1f} '
              f'({100.0 * head["lost"] / max(need, 1e-9):.2f}% of what is '
              f'needed).')
    else:
        print('  no pairs in the head window at all — that would itself be '
              'the answer, and it is not what the store holds')

    print('\nDEAD RATE AGAINST TIME  (ordered, not pooled)')
    names = ['t<0 prefire', '0-60', '60-140', '140-300', '300-600',
             '600-1200', '1200-2400', '2400+']
    for nm, (n, d) in zip(names, by_t):
        if n:
            print(f'  {nm:>12} ms   {int(d):6d} / {int(n):7d}   '
                  f'{100.0 * d / n:6.3f}%')

    if worst:
        worst.sort(reverse=True)
        print('\nWORST MAGAZINES')
        for d, stem, n in worst[:8]:
            print(f'  {stem:<44} {d:4d} / {n} pairs')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
