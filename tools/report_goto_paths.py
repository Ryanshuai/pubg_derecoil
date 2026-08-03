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
    """-> the bucket this transition belongs in, for the verdict."""
    if not r.get('ok'):
        return 'failed'
    src, dst = r.get('from'), r.get('to')
    if r.get('path') == 'already':
        return 'already there'
    if src is None:
        return 'from a collapsed panel'          # uninformative by construction
    if list(src) == list(dst or []):
        return 'already there'
    if r.get('path') == 'closed-blocker':
        # Geometry, not menu discipline: a submenu above the target in the
        # same column pushes its row down out from under the coordinate.
        return 'blocker above it, same column'
    if r.get('path') == 'direct':
        return 'ACCORDION: one click closed the other'
    if r.get('path') == 'via-root':
        return 'MULTI-OPEN: needed a full collapse'
    return f"other ({r.get('path')})"


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

    acc = buckets['ACCORDION: one click closed the other']
    multi = buckets['MULTI-OPEN: needed a full collapse']
    decisive = acc + multi
    print(f'\ndecisive transitions (another category was open, and no blocker '
          f'above the target): {decisive}')

    if decisive == 0:
        print('\nNothing decisive yet. Every logged transition either started '
              'from a collapsed panel or had to close a blocker above the '
              'target in the same column, and neither answers the question. '
              'What is missing is a switch BETWEEN categories with one already '
              'open — give_many() makes those whenever a list spans two '
              'categories in the same column, so a run that spawns e.g. a '
              'muzzle and a grip will produce them.')
        return 0
    if decisive < MIN_DECISIVE:
        print(f'\nOnly {decisive} of them — not enough. {acc} say accordion, '
              f'{multi} say multi-open. Do not write this into '
              f'detector/spawner_layout.py or the cost table yet; "3 out of 3" '
              f'is exactly how a wrong constant gets in.')
        return 0

    if multi == 0:
        print(f'\nACCORDION, {acc}/{acc}. Opening one category closes the '
              f'last, so a same-column switch really is 1 click and '
              f'refactor_plan section 2\'s cost table is achievable. Write it '
              f'into docs/game_quirks.md with this count.')
    elif acc == 0:
        print(f'\nMULTI-OPEN, {multi}/{multi}. Several categories stand open, '
              f'so a switch genuinely costs the collapse. The cost table\'s '
              f'"1 click" is not reachable — say so there and stop planning '
              f'around it.')
    else:
        print(f'\nMIXED: {acc} accordion, {multi} multi-open. That is the '
              f'interesting outcome and it means the menu\'s behaviour depends '
              f'on something not being recorded here — most likely which '
              f'column, or whether the two categories share one. Look at the '
              f'raw rows before concluding anything.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
