"""Does the game need a raw motion report to see a drag as a drag?

A drag's travel is SetCursorPos, which does not reach the Pico, and the
firmware only sends a HID report when something changed (main.c,
send_hid_output). So the game's raw input receives button-down and button-up
with nothing between them — a click. The UI cursor still follows SetCursorPos,
which is why a drag that picked nothing up still reads back a perfect release
point.

This drops one item from 库存 onto the floor, over and over, alternating
between nudge=0 (the old behaviour) and nudge=N (a raw report per step, sign
alternating so the net displacement is zero). Same source, same target, same
timing; only the reports differ.

    pixi run python tools/probe_drag_nudge.py --trials 12

WHY IT ALTERNATES rather than running one block then the other: the failure
gets worse deeper into a burst, so two blocks would confound the variable with
position. Alternating spreads that evenly over both arms.

THE ITEM COMES BACK each trial. Dropping to the floor is one-way, so each
trial spawns a fresh part rather than reusing one -- which also keeps 库存 at
one row, the state the real collector drags from.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from calibration.collect_templates import inv_rows
from control.inventory import InventoryControl, at_ground, at_inv
from control.session import ensure_ready
from control.spawner import SpawnerControl

PART = 'comp_ar'          # small, always available, fits nothing on an empty
                          # rack so it lands in 库存 rather than on a gun


def burst(ac, sc, n, nudge):
    """Stage n parts and drag them out BACK TO BACK. -> [landed, ...]

    WHY THIS EXISTS BESIDE one(). one() re-stages between every drag, which
    means closing Tab, opening the spawner, spawning, reopening Tab — so every
    drag it measures is the FIRST of a burst, and the failure being chased
    happens at the third and fourth. Measuring one drag at a time cannot see
    it: 16/16 landed under one(), while the collector, which drags a full 库存
    out in a loop, was missing half.

    Nothing is re-read between drags on purpose. Row 0 is always the top of
    the list and the list closes up from below, so the same grab point stays
    valid — that is why clear_inventory drags the top row every time.
    """
    ac.ensure_tab(True)
    ac.clear_inventory()
    ac.ensure_tab(False)
    sc.give_many([PART] * n, switch=False)
    if not ac.ensure_tab(True):
        return []
    have = inv_rows(ac.frame())
    if have < 2:
        print(f'    staging produced {have} row(s), need at least 2')
        return []
    p0 = ac.point_of(at_inv(0))
    p1 = ac.point_of(at_ground(), from_y=p0[1])
    out = []
    for i in range(have):
        before = inv_rows(ac.frame())
        ac.pointer.drag(p0, p1, nudge=nudge, **ac.timing)
        after = inv_rows(ac.frame())
        out.append(after < before)
        print(f'    #{i + 1} rows {before}->{after}  '
              f'{"landed" if after < before else "MISSED"}')
        if after == 0:
            break
    return out


def one(ac, sc, nudge):
    """Spawn one part, drag it to the floor. -> True if the row left, or None.

    The spawner panel and Tab are mutually exclusive, so the staging closes
    Tab before asking for the part and reopens it to drag.
    """
    if not ac.ensure_tab(True):
        return None
    # An empty 库存 makes "did the row leave" unambiguous: 1 -> 0. Deliberately
    # NOT verified here — a clear_inventory that fails is itself a data point,
    # and the row count below refuses the trial rather than mislabelling it.
    ac.clear_inventory()
    ac.ensure_tab(False)
    sc.give_many([PART], switch=False)
    if not ac.ensure_tab(True):
        return None
    if inv_rows(ac.frame()) != 1:
        print('    staging did not leave exactly one row, skipping')
        return None
    p0 = ac.point_of(at_inv(0))
    p1 = ac.point_of(at_ground(), from_y=p0[1])
    ac.pointer.drag(p0, p1, nudge=nudge, **ac.timing)
    return inv_rows(ac.frame()) == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--trials', type=int, default=12,
                    help='per arm; they alternate')
    ap.add_argument('--nudge', type=int, default=2,
                    help='raw counts per step for the treated arm')
    ap.add_argument('--burst', type=int, default=0,
                    help='stage N parts and drag them out back to back, '
                         'which is what the collector does and what one-at-a-'
                         'time staging cannot reproduce')
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    rec = ensure_ready(label='drag nudge A/B', countdown_s=5)
    if not rec['ok']:
        print(f'not ready to drive the game: {rec["failed"]}')
        return 1

    # Both open a Pointer, and both get the SAME PicoMouse: get_mouse() is a
    # singleton, which is what lets the collector hold these two side by side.
    score = {0: [0, 0], args.nudge: [0, 0]}
    ac = InventoryControl(verbose=False)
    with SpawnerControl(verbose=False) as sc:
        for i in range(args.trials * 2):
            nudge = 0 if i % 2 else args.nudge
            if args.burst:
                print(f'  burst {i + 1:>3}  nudge={nudge}')
                got = burst(ac, sc, args.burst, nudge)
                score[nudge][0] += sum(got)
                score[nudge][1] += len(got)
                continue
            got = one(ac, sc, nudge)
            if got is None:
                continue
            score[nudge][0] += bool(got)
            score[nudge][1] += 1
            print(f'  trial {i + 1:>3}  nudge={nudge}  '
                  f'{"landed" if got else "MISSED"}')
    ac.ensure_tab(False)

    print()
    for nudge, (ok, n) in sorted(score.items()):
        if n:
            print(f'  nudge={nudge:<3} {ok}/{n} landed = {ok / n:.2f}')
    a, b = score[0], score[args.nudge]
    if a[1] and b[1]:
        print(f'\n  Without the raw report {a[0]}/{a[1]}, with it {b[0]}/{b[1]}.')
        print('  If those are far apart the firmware line is the cause and '
              'the nudge is the fix.\n  If they are the same it is not, and '
              'the drop point is the next suspect.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
