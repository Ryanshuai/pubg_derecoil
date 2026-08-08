"""A retry must re-aim. Flags gesture points computed outside their retry loop.

THE BUG CLASS THIS EXISTS FOR, which bit three times in one night (2026-08-07):

  ensure_kit        planned every step off one detection pass, then executed
                    them in sequence. Fitting a part removes its backpack row
                    and everything below it moves, so step 2 onward aimed at
                    rows that had already shifted.
  Kit._swap_back    read the backpack once, then clicked each row in turn.
                    Same shape.
  right_click_equip computed `x, y = self.point_of(src)` ONCE, outside
                    `for attempt in range(retries + 1)`. The first click
                    evicted the incumbent into the backpack -- inserting a row
                    -- so the retry put the evicted part straight back on and
                    threw this run's part off. The readback then reported the
                    factory part and the caller wrote "this weapon will not
                    take ext_smg" into kit_facts.json. Watched on screen and
                    described exactly: "好像是装对了,然后又装一次,装错了,然后
                    说找不到,把枪扔了".

All three read the same from outside -- a gesture that "didn't work" -- and
all three had clean geometry in the journal, because the gesture was fine and
the TARGET was wrong. That is why this is worth a machine: the symptom points
away from the cause, so the next one will be found by lint or not at all.

The rule, and it is deliberately narrow so it can be trusted:

  Inside a `for` loop that issues a `self.pointer.<something>` call, any
  coordinate pair the gesture uses must be assigned INSIDE that loop, or the
  loop must carry an explicit justification comment naming this file.

A positional address is only stale if something moved it, so a loop that
cannot move anything is exempt by writing down WHY -- in the loop, where the
next reader is, not in a table here.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Only where gestures are issued. Widen when another module grows a Pointer.
FILES = ('control/inventory.py', 'control/spawner.py', 'control/stock.py')

# A loop body containing this substring is asserting the addresses it aims at
# cannot move between iterations. It has to say so in words -- the marker on
# its own is not the point, the sentence after it is.
WAIVER = 'RETRY-SAFE:'

GESTURES = ('drag', 'click_at', 'right_click_at', 'left_click_at', 'press',
            'release', 'move_to', 'place')


def _pointer_calls(node):
    """Every `self.pointer.<gesture>(...)` inside `node`."""
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if not isinstance(f, ast.Attribute) or f.attr not in GESTURES:
            continue
        v = f.value
        if (isinstance(v, ast.Attribute) and v.attr == 'pointer'
                and isinstance(v.value, ast.Name) and v.value.id == 'self'):
            out.append(n)
    return out


def _names_used(call):
    """Bare names this call passes as arguments (positional and keyword)."""
    out = set()
    for a in list(call.args) + [k.value for k in call.keywords]:
        for n in ast.walk(a):
            if isinstance(n, ast.Name):
                out.add(n.id)
    return out


def _names_assigned(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        out.add(x.id)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            for x in ast.walk(n.target):
                if isinstance(x, ast.Name):
                    out.add(x.id)
        elif isinstance(n, ast.For):
            for x in ast.walk(n.target):
                if isinstance(x, ast.Name):
                    out.add(x.id)
    return out


def _point_sources(tree):
    """Names bound from a coordinate-producing call, anywhere in the file.

    `point_of` and the two geometry helpers are the only things that turn an
    ADDRESS into pixels, so a name bound from one of them is a positional
    address frozen at the moment it was computed.
    """
    makers = ('point_of', 'gun_tag_point', 'att_slot_point', 'row_point')
    out = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Assign):
            continue
        calls = [c for c in ast.walk(n.value) if isinstance(c, ast.Call)]
        if not any(isinstance(c.func, ast.Attribute) and c.func.attr in makers
                   or isinstance(c.func, ast.Name) and c.func.id in makers
                   for c in calls):
            continue
        for t in n.targets:
            for x in ast.walk(t):
                if isinstance(x, ast.Name):
                    out.add(x.id)
    return out


def check(path):
    src = open(path, encoding='utf-8').read()
    lines = src.splitlines()
    tree = ast.parse(src)
    frozen = _point_sources(tree)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.While)):
            continue
        calls = _pointer_calls(node)
        if not calls:
            continue
        body = '\n'.join(lines[node.lineno - 1:
                               (node.end_lineno or node.lineno)])
        if WAIVER in body:
            continue
        inside = _names_assigned(node)
        for c in calls:
            stale = sorted((_names_used(c) & frozen) - inside)
            if stale:
                bad.append((c.lineno, ', '.join(stale), node.lineno))
    return bad


def main():
    hits = 0
    for rel in FILES:
        p = os.path.join(ROOT, rel.replace('/', os.sep))
        if not os.path.exists(p):
            continue
        for lineno, names, loop in check(p):
            hits += 1
            print(f'{rel}:{lineno}: gesture aims at {names}, computed outside '
                  f'the retry loop at line {loop}')
    if hits:
        print(f'\n{hits} gesture(s) reuse a point across a retry. A retry '
              f'happens because\nsomething did not land -- and the commonest '
              f'reason a gesture does not land is\nthat the row it aimed at '
              f'MOVED. Re-find the target inside the loop, or\nwrite '
              f'`{WAIVER} <why nothing can shift here>` in the loop body.')
        return 1
    print(f'checked {len(FILES)} file(s) — every retry re-aims')
    return 0


if __name__ == '__main__':
    sys.exit(main())
