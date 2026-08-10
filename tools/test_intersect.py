"""The gate on the intersection template builder. Offline.

    pixi run intersect-test

`collect_intersect` has no model in it -- no alpha, no least squares, nothing
to be numerically wrong. What it has instead is one operation applied over and
over, so every property that matters is a property of THAT, and each one fails
silently if broken:

    monotone            the template can only shrink. If a pixel could come
                        back, "nothing changed this frame" would stop meaning
                        converged and the loop would end on a coincidence
    exact equality      a tolerance would keep pixels nobody observed; the old
                        flow's sweep showed every tolerance from 0 to 30 scored
                        identically, so the loose ones bought nothing
    history folds in    intersecting the two guns and forgetting the previous
                        frames would make the template the LAST view, not the
                        agreement across all of them
    shape mismatch      refuses. A resize here is the scale error the old
                        pipeline made twice, in both directions
    ragged batches      the muzzle list runs out before the grip list; the
                        remaining batches carry fewer slots and that is normal,
                        not a failure

SYNTHETIC INPUTS, deliberately. A test fed real crops passes because of what
happens to be on disk today.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from calibration.collect_intersect import (GROUPS, alive, by_slot, intersect,
                                           loads_of, plan_group)
from detector.attachment_catalog import ATTACHMENTS, fits

FAIL = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"}  {name}' + (f'  — {detail}' if detail else ''))
    if not ok:
        FAIL.append(name)


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

    rng = np.random.RandomState(0)
    a = rng.randint(0, 255, (8, 8, 3), dtype=np.uint8)
    b = a.copy(); b[0, 0] = [1, 2, 3]
    c = a.copy(); c[0, 0] = [9, 9, 9]; c[1, 1] = [4, 4, 4]

    print('the operation')
    check('identical inputs change nothing', np.array_equal(intersect(None, a, a), a))
    r = intersect(None, a, b)
    check('a differing pixel is zeroed, its neighbours are not',
          (r[0, 0] == 0).all() and np.array_equal(r[1:], a[1:]))
    # One channel only: the reason exact equality is per-channel and not on a
    # distance. A pixel agreeing in two channels is not an agreeing pixel.
    d = a.copy(); d[2, 2, 1] = (int(a[2, 2, 1]) + 1) % 256
    check('one channel out of three is enough to drop it',
          (intersect(None, a, d)[2, 2] == 0).all())

    print('\nfolding history in')
    acc = intersect(None, a, a)
    acc2 = intersect(acc, c, c)
    check('an earlier frame still counts',
          (acc2[0, 0] == 0).all() and (acc2[1, 1] == 0).all())
    check('monotone: never regrows', alive(acc2) <= alive(acc))
    check('a pixel once dropped stays dropped even if inputs agree again',
          (intersect(acc2, a, a)[0, 0] == 0).all(),
          'else "no change" could fire on a coincidence rather than agreement')
    check('idempotent once converged',
          np.array_equal(intersect(acc2, acc2, acc2), acc2))

    print('\nrefusals')
    check('shape mismatch returns None rather than resizing',
          intersect(None, a, np.zeros((4, 4, 3), np.uint8)) is None)
    check('all-black input collapses the template to nothing',
          alive(intersect(None, a, np.zeros_like(a))) == 0,
          'a blank tile must not read as agreement')

    print('\nbatching')
    bs = by_slot(['comp_ar', 'flash_ar', 'supp_ar', 'vert_grip', 'laser',
                  'half_grip'])
    check('equal-length slots pair up',
          [sorted(x) for x in bs] == [['grip', 'muzzle']] * 3, f'{bs}')
    bs2 = by_slot(['comp_ar', 'vert_grip', 'laser', 'half_grip'])
    check('an exhausted slot is simply absent from later batches',
          len(bs2) == 3 and 'muzzle' in bs2[0] and 'muzzle' not in bs2[1],
          f'{bs2}')
    check('every part appears exactly once across the batches',
          sorted(k for x in bs for k in x.values())
          == sorted(['comp_ar', 'flash_ar', 'supp_ar', 'vert_grip', 'laser',
                     'half_grip']))

    print('\nthe group table')
    everywhere = []
    for g in sorted(GROUPS):
        hosts, batches, skipped = plan_group(g)
        planned = [k for b in batches for k in b.values()]
        everywhere += planned
        check(f'{g}: no part planned that both hosts cannot wear',
              all(all(fits(w, k) for w in hosts) for k in planned))
        check(f'{g}: nothing both planned and skipped',
              not (set(planned) & set(skipped)))
        check(f'{g}: a skipped part is one only ONE host can wear',
              all(any(fits(w, k) for w in hosts)
                  and not all(fits(w, k) for w in hosts) for k in skipped),
              f'{skipped}')

    # ⚠ THE TWO GATES THAT ACTUALLY BIT. The old completeness check was
    # per-group ("everything these hosts can wear is planned or skipped"), and
    # it passed while `uzi_stock` was collected by nobody -- no group's hosts
    # could wear it, so it was absent from every group's `want` and therefore
    # unaccounted for nowhere. Coverage is a property of the PLAN, not of a
    # group, so it is checked once, here, against the whole catalogue.
    check('every catalogue part is collected by some group',
          set(everywhere) == set(ATTACHMENTS),
          f'uncovered: {sorted(set(ATTACHMENTS) - set(everywhere))}')
    # And exactly once: two groups collecting the same part means the second
    # silently OVERWRITES the first, so which gun the template came from is
    # decided by dict iteration order.
    dupes = sorted({k for k in everywhere if everywhere.count(k) > 1})
    check('no part is collected by two groups', not dupes, f'{dupes}')

    print('\nspawner trips')
    waves = [{'muzzle': 'a', 'grip': 'b'}] * 5          # 4 rows each
    trips = loads_of(waves, rows=12)
    check('a trip carries as many whole waves as fit',
          [len(t) for t in trips] == [3, 2], f'{[len(t) for t in trips]}')
    check('no trip exceeds the row budget',
          all(sum(len(w) for w in t) * 2 <= 12 for t in trips))
    check('every wave lands in exactly one trip',
          sum(len(t) for t in trips) == len(waves))
    # A wave wider than the budget is carried ALONE rather than dropped: the
    # backpack limit is a packing hint, and silently discarding a wave would
    # lose parts while every other count still looked right.
    big = loads_of([{s: s for s in 'abcdefgh'}], rows=12)
    check('a wave larger than one load is not dropped',
          len(big) == 1 and len(big[0]) == 1)

    print(f'\n{"FAILED: " + ", ".join(FAIL) if FAIL else "all gates pass"}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    raise SystemExit(main())
