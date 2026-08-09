"""The cell order when a slot has MORE THAN TWO values. -> exit 1 on failure.

    pixi run gray

WHY THIS EXISTS. harness.night.order_configs solves the BINARY case: each
controlled slot is filled or empty, so 2^n cells and a brute force over 8!
finds the floor. That is the whole story only while "grip" means one specific
part. It does not:

    grip     vert_grip  angled_grip  half_grip  thumb_grip  laser  (or empty)
    muzzle   comp_ar    supp_ar      brake_ar                      (or empty)

Today a second part in the same slot is a SEPARATE NIGHT (`--parts
grip=tilted_grip`), so the ordering never crosses that axis and every boundary
between runs pays the worst case. Asked from the chair on 2026-08-09:

    grip 那一步，其实是多个 grip 轮番换。这个多种的怎么做格雷码

⚠ AND BRUTE FORCE STOPS WORKING HERE, which is why this is a construction and
not a search. Five grips x four muzzles x three stocks is 60 cells; 60! is not
a number anything enumerates. The binary case got away with it at 8! = 40320.

THE ANSWER IS THE MIXED-RADIX REFLECTED GRAY CODE. Slot j has k_j values
(counting empty), and the tour visits all prod(k_j) cells changing exactly ONE
slot at each step. Construction: write the index in mixed radix, then reflect
digit j whenever the sum of the digits above it is odd -- the same trick that
makes the binary reflected code work, generalised to unequal bases.

⚠ IT IS OPTIMAL BY COUNTING, not by search, and that is the point: visiting N
cells takes at least N-1 transitions, and this makes every transition cost
exactly one slot change. No ordering can do better, so nothing has to be
verified against a brute force -- but the cases below check the property that
makes the claim true, since a construction that silently changes two slots
somewhere would still look like a plausible tour.
"""
import os
import sys
from math import prod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

from harness.night import gray_order                          # noqa: E402

FAILS = []


def check(label, cond, detail=''):
    print(f'  {"ok  " if cond else "FAIL"}  {label}'
          + (f'\n           {detail}' if detail and not cond else ''))
    if not cond:
        FAILS.append(label)


def transitions(tour):
    """How many SLOTS change at each step."""
    return [sum(1 for k in a if a[k] != b[k]) for a, b in zip(tour, tour[1:])]


def main():
    print('mixed-radix Gray order — one slot changes per step, whatever the '
          'radix\n')

    # The real shape: a slot that is filled-or-empty, next to one with five
    # parts. This is what a night testing every grip against every muzzle is.
    axes = {
        'muzzle': [None, 'comp_ar', 'supp_ar'],
        'grip': [None, 'vert_grip', 'angled_grip', 'half_grip', 'thumb_grip'],
    }
    tour = gray_order(axes)
    n = prod(len(v) for v in axes.values())

    check('every cell appears exactly once',
          len(tour) == n and len({tuple(sorted(c.items())) for c in tour}) == n,
          f'{len(tour)} cells, {len({tuple(sorted(c.items())) for c in tour})} '
          f'distinct, expected {n}')

    t = transitions(tour)
    check('EXACTLY one slot changes at every step',
          all(x == 1 for x in t),
          f'steps changing !=1 slot: '
          f'{[(i, x) for i, x in enumerate(t) if x != 1][:6]}')

    check(f'so the tour costs {n - 1} changes, which is the floor',
          sum(t) == n - 1, f'cost {sum(t)}, floor {n - 1}')

    # ⚠ THE NEGATIVE. Without this, `gray_order = lambda a: [any tour]` passes
    # the count checks whenever it happens to be handed a tidy input. The
    # typed order -- nested loops, the way a human writes it -- is what the
    # night pays today, and it must come out WORSE or there is nothing to buy.
    typed = [{'muzzle': m, 'grip': g}
             for m in axes['muzzle'] for g in axes['grip']]
    tt = sum(transitions(typed))
    check('...and the order a human types really is worse',
          tt > sum(t), f'typed {tt} vs gray {sum(t)}')
    print(f'\n  {n} cells: typed order {tt} slot changes, Gray {sum(t)} — '
          f'{100 * (tt - sum(t)) // tt}% fewer\n')

    # Degenerate radices, because a night may pin a slot to one part.
    for axes2, want in (({'a': [None, 'x']}, 2),
                        ({'a': ['only']}, 1),
                        ({'a': [None, 'x'], 'b': ['pinned']}, 2)):
        tour2 = gray_order(axes2)
        t2 = transitions(tour2)
        check(f'radix {[len(v) for v in axes2.values()]} -> {want} cells, '
              f'all single-slot steps',
              len(tour2) == want and all(x == 1 for x in t2),
              f'{len(tour2)} cells, transitions {t2}')

    # The binary case must still be the binary case: 2^3 in 7 changes is the
    # number order_configs already proves and plan-order already pins.
    cube = gray_order({s: [None, 'p'] for s in ('muzzle', 'grip', 'stock')})
    check('the 2^3 factorial still costs 7 — same floor as order_configs',
          len(cube) == 8 and sum(transitions(cube)) == 7,
          f'{len(cube)} cells, {sum(transitions(cube))} changes')

    print()
    if FAILS:
        print(f'{len(FAILS)} FAILED: {FAILS}')
        return 1
    print('all ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
