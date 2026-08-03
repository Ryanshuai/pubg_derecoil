"""Is the spawner menu an accordion? Reads the log goto() keeps. Offline.

    pixi run goto-paths
    pixi run goto-paths --log <file>

docs/refactor_plan.md section 2 costs a same-column category switch at 2
clicks (collapse + expand) and claims it should be 1. Whether it can be is not
a design question, it is a fact about the menu: does opening one category
close the last one (an accordion) or do several stand open at once?

Nobody has ever asked the game. Every capture in docs/spawner/runs/ expands
from a COLLAPSED panel, so all of them are consistent with either answer.

THE SPLIT THIS REPORT MAKES IS THE WHOLE POINT. goto() records `path`, and
counting paths alone would look like evidence while proving nothing:

    path='direct' with NOTHING expanded    one click was always going to be
                                           enough. Says nothing. This is most
                                           of the traffic.
    path='direct' with ANOTHER category    opening the target closed the other
      expanded                             one. THAT is the accordion.
    path='closed-blocker'                  a category above the target in the
                                           same column had to be closed first
                                           -- but that is a GEOMETRY fix (an
                                           expanded submenu pushes lower rows
                                           down ~360 px), not evidence about
                                           accordion behaviour. Counted apart.

So the answer comes from ONE cell of this table: transitions that started with
some other category expanded and did not need it closed first.

`ok=False` rows are excluded from the verdict but printed: a failed transition
says nothing about the menu's discipline, and letting it into the ratio would
quietly move the number.
"""
import argparse
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from control.spawner import GOTO_LOG

# Below this the split is noise. Not a statistical test -- just a reminder
# that "3 out of 3" is how a wrong answer gets written into a constants file.
MIN_DECISIVE = 12


def load(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def classify(r):
    """-> the bucket this transition belongs in, for the verdict.

    THE BUCKETS DESCRIBE WHAT WAS OBSERVED, NOT WHY. They used to be named
    'ACCORDION' and 'MULTI-OPEN', on the assumption that the menu had to be
    one or the other. It is neither, and the first real sample said so:
    the panel keeps BOTH columns open at once, and what a transition costs
    depends on DIRECTION, not on any accordion discipline. A bucket named
    after a mechanism would have gone on asserting the wrong one while the
    counts underneath it were perfectly good.

    So: same-column-up, same-column-down and cross-column are separated,
    because that is the split the measurements actually landed on.
    """
    if not r.get('ok'):
        return 'failed'
    src, dst = r.get('from'), r.get('to')
    if r.get('path') == 'already':
        return 'already open — 0 clicks'
    if src is None:
        return 'from a collapsed panel'          # uninformative by construction
    if list(src) == list(dst or []):
        return 'already open — 0 clicks'
    same_col = dst and src[0] == dst[0]
    if not same_col:
        where = 'cross-column'
    elif src[1] < dst[1]:
        # The open one is ABOVE the target, so its submenu pushes the target's
        # row ~360 px down out from under the measured coordinate. Geometry.
        where = 'same column, DOWN (open one is above)'
    else:
        where = 'same column, UP'
    return f'{where}: {r.get("path")} ({r.get("clicks")} clicks)'


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--log', default=GOTO_LOG)
    args = ap.parse_args()

    rows = load(args.log)
    if not rows:
        print(f'no transitions logged yet.\n\n{args.log}\n\n'
              f'It fills up on its own — every goto() appends one line, and '
              f'every calibration run that touches the spawner makes dozens. '
              f'Run a harvest or a scan and come back.')
        return 0

    buckets = Counter(classify(r) for r in rows)
    try:                       # a --log on another drive has no relpath
        where = os.path.relpath(args.log, ROOT)
    except ValueError:
        where = args.log
    print(f'{len(rows)} transitions in {where}\n')
    for name, n in buckets.most_common():
        print(f'  {n:5d}  {name}')

    # Cost per direction, which is what the cost table in
    # docs/refactor_plan.md section 2 is actually asking about.
    # `already` rows are counted apart, NOT folded into a direction. The
    # target IS the open node, so there is no direction to attribute them to,
    # and averaging them into both buckets (which an earlier version did)
    # halves every number by double-counting the same rows twice.
    already = sum(1 for r in rows if r.get('ok') and r.get('from')
                  and classify(r) == 'already open — 0 clicks')
    print(f'\n{already} transitions were free: the target was ALREADY open. '
          f'That is the multi-open payoff — a column keeps its expansion while '
          f'you work in another one.')
    print('\nmean clicks when a move was actually needed:')
    for where in ('cross-column', 'same column, UP',
                  'same column, DOWN (open one is above)'):
        got = [r for r in rows
               if r.get('ok') and r.get('from') and r.get('path') != 'already'
               and classify(r).startswith(where)]
        if not got:
            print(f'  {where:42} no samples')
            continue
        n = len(got)
        total = sum(r.get('clicks') or 0 for r in got)
        flag = '' if n >= MIN_DECISIVE else f'   <-- only {n}, thin'
        print(f'  {where:42} {total / n:4.2f}  (n={n}){flag}')

    print(f'\nThe question this was built to answer — accordion or '
          f'multi-open — turned out to be the wrong question. Measured '
          f'2026-08-03: the panel holds ONE expansion PER COLUMN and keeps '
          f'them across columns, so cost depends on direction, not on any '
          f'accordion rule. See docs/game_quirks.md. What this report is for '
          f'now is noticing when that stops being true.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
