"""Offline gate on ONE number: what a night PAYS to walk its own plan.

    pixi run plan-order

`harness.night.plan_cells` decides the order the cells are measured in, and
because every cell pins every controlled slot -- want_for forces the ones it
does not fill EMPTY -- the order cannot change any result. It only changes the
bill: the attachment changes between consecutive cells.

    typed order   bare muzzle grip stock muzzle+grip muzzle+stock
                  grip+stock muzzle+grip+stock            13 slot changes
    planned       bare muzzle muzzle+grip grip grip+stock
                  muzzle+grip+stock muzzle+stock stock     7 slot changes

⚠ THE ASSERTION IS THE COST, NOT THE SEQUENCE. Pinning the exact permutation
would fail on any equally-good reordering and would still pass if the cost
silently doubled -- the wrong way round on both counts. What the night pays is
the number of slot changes, so that is the number checked, against a
brute-forced floor computed here rather than a constant copied from the
implementation.

⚠ WHY THIS IS NOT IN tools/test_harness.py, where a reader would look first:
that file does not import. It asks harness.verdict for CLUSTER_MIN and
AGREE_SPREAD_MAX, and verdict has neither -- the thresholds became MAGS_MIN and
RATE_RESID_MS_MAX, `n_kept` became `mags_kept`, the two-arm agreement check
became a fire-rate check, and a fifth check appeared. That is a
PRE-EXISTING break, not one this file's subject caused. ✅ SETTLED 2026-08-09:
MODEL.md is the law and its out-of-loop check is the arm agreement, so
verdict.py's 4th check is that again and `pixi run harness` is green. This
gate still does not inherit that import -- keeping them independent is why
this one kept working while the other was broken.

WHAT THE SAVING IS WORTH, measured rather than asserted (the shared gesture
journal, calibration/artifacts/drag/journal.jsonl, 2026-08-08):

    1115 gestures aimed at a gun slot -> 789 landed fits   1.41 gestures/fit
    21% of fits needed more than one gesture
    58 fit actions took over 5 s

So 46% fewer fits is 46% fewer draws against a gesture that misses one time in
five, and fitting is this project's largest single source of wasted runs.
"""
import os
import sys
from itertools import permutations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from control.kitting import parse_config                       # noqa: E402
from harness.night import order_configs, plan_cells            # noqa: E402

FULL = ['bare', 'muzzle', 'grip', 'stock', 'muzzle+grip', 'muzzle+stock',
        'grip+stock', 'muzzle+grip+stock']

FAILS = []


def cost(seq):
    """Attachment changes a night pays to walk `seq` in order.

    ⚠ A SECOND, INDEPENDENT COPY OF THE COST, deliberately not night's
    `_slot_changes`. The floor below is brute-forced with THIS function, so
    importing the implementation's would let a wrong cost agree with itself and
    still report "reaches the floor". The old copy was `len(a ^ b)` on
    frozensets, and it had to change here for the same reason it changed there:
    a config can now name its part, and on sets 'grip=vert_grip' and
    'grip=half_grip' are the same config.

    ⚠ Membership and value, not value alone: a slot absent and a slot taking
    the class default BOTH read None, so comparing `.get` alone scores the
    whole factorial at zero. This function got that wrong first and the two
    checks below said so.
    """
    s = [parse_config(c) for c in seq]
    return sum(sum(1 for slot in set(a) | set(b)
                   if (slot in a, a.get(slot)) != (slot in b, b.get(slot)))
               for a, b in zip(s, s[1:]))


def check(label, cond, detail=''):
    print(f'  {"ok  " if cond else "FAIL"}  {label}'
          + (f'   {detail}' if detail and not cond else ''))
    if not cond:
        FAILS.append(label)


def main():
    print('plan order — same cells, fewer attachment changes')
    got = order_configs(FULL)
    floor = cost(min(permutations(FULL), key=cost))

    check('the full 2^3 factorial keeps every cell',
          sorted(got) == sorted(FULL), f'{got}')
    check(f'...and reaches the brute-forced floor ({floor})',
          cost(got) == floor, f'cost {cost(got)} vs floor {floor}')
    check('...which beats the order a human types',
          cost(got) < cost(FULL), f'{cost(got)} vs typed {cost(FULL)}')

    # ⚠ THE NEGATIVE, and without it `order_configs = lambda c: c` passes
    # everything above the moment someone hands in an already-cheap list. It
    # also pins the two numbers the docstring quotes, so the prose cannot
    # drift from the code the way test_harness.py did.
    check('the typed order really is the expensive one (13 vs 7)',
          cost(FULL) == 13 and floor == 7,
          f'typed {cost(FULL)}, floor {floor}')

    print()
    print('it must not quietly change the PLAN, only its order')
    for tiny in ([], ['bare'], ['bare', 'muzzle']):
        check(f'{len(tiny)} config(s) pass through untouched',
              order_configs(tiny) == tiny, f'{order_configs(tiny)}')

    # An unparseable name disables the reordering instead of dropping the
    # entry. plan_cells filters those itself; silently losing one here would
    # shorten the night without saying so.
    weird = ['bare', 'muzzle', 'nonsense_slot']
    check('an unknown config name leaves the list alone',
          order_configs(weird) == weird, f'{order_configs(weird)}')

    # Duplicates in, duplicates out -- supported_configs has already
    # de-duplicated by the time plan_cells calls this, and a reorder that also
    # de-duplicated would hide a planner bug rather than report it.
    dup = ['bare', 'muzzle', 'bare']
    check('it neither drops nor merges duplicates',
          sorted(order_configs(dup)) == sorted(dup), f'{order_configs(dup)}')

    print()
    print('a config can NAME its part, and a swap is one change — not zero')
    # ⚠ THE CASE THAT MAKES A CAMPAIGN SCHEDULABLE, and the one a set-based
    # cost cannot even express. Under `len(a ^ b)` on slot names these two are
    # the SAME config, so the planner would rate a real grip swap as free and
    # cheerfully interleave them with cells that cost something.
    check('swapping the part in one slot costs 1',
          cost(['grip=vert_grip', 'grip=half_grip']) == 1,
          f'{cost(["grip=vert_grip", "grip=half_grip"])}')
    check('...and is still cheaper than swapping two slots',
          cost(['muzzle=comp_smg+grip=vert_grip',
                'muzzle=flash_smg+grip=half_grip']) == 2)
    # A named part that IS the class default must stay anonymous, or every
    # already-logged cell id changes and --resume stops matching.
    check('a defaulted slot keeps its short name',
          plan_cells(['m416'], ['standing'], 'red_dot',
                     ['muzzle'])[0][3] == 'muzzle')
    # The mixed axis: one slot named, one defaulted, in the same config.
    mixed = plan_cells(['m416'], ['standing'], 'red_dot',
                       ['muzzle+grip=half_grip'])
    check('a half-named config survives planning',
          [c[3] for c in mixed] == ['muzzle+grip=half_grip'], f'{mixed}')
    # The vector cannot wear tilted_grip (attachment_catalog.EXCLUDE, confirmed
    # by hand). An explicitly named part has to degrade the way a missing slot
    # does -- otherwise the cell is planned, spawned, dragged and refused.
    vec = plan_cells(['vector'], ['standing'], 'red_dot',
                     ['grip=tilted_grip', 'grip=half_grip'])
    check('a part the weapon refuses degrades out of the plan',
          [c[3] for c in vec] == ['bare', 'grip=half_grip'], f'{vec}')

    print()
    print('end to end, through plan_cells')
    cells = plan_cells(['m416'], ['standing'], 'red_dot', FULL)
    check('one cell per config, all 8 present',
          len(cells) == 8 and len({c[3] for c in cells}) == 8,
          f'{[c[3] for c in cells]}')
    check('emitted in the cheap order',
          cost([c[3] for c in cells]) == floor,
          f'cost {cost([c[3] for c in cells])}')

    # postures are innermost: they change no attachment, so a posture sweep
    # inside one config must not split that config into two visits.
    many = plan_cells(['m416'], ['standing', 'crouch'], 'red_dot', FULL)
    runs = [c[3] for c in many]
    contiguous = all(runs[i] != runs[i + 1] or True for i in range(len(runs) - 1)) \
        and len([i for i in range(1, len(runs)) if runs[i] != runs[i - 1]]) == 7
    check('a posture sweep stays inside one config visit',
          contiguous, f'{runs}')

    print()
    if FAILS:
        print(f'{len(FAILS)} FAILED: {FAILS}')
        return 1
    print(f'plan order holds — {cost(FULL)} -> {floor} attachment changes '
          f'per weapon, {100 * (cost(FULL) - floor) // cost(FULL)}% fewer')
    return 0


if __name__ == '__main__':
    sys.exit(main())
