"""MODEL.md's 「常数现值」 table against config.py, which authors those values.

    pixi run model-consts

WHY THIS EXISTS, and it is one commit old: c196aa0 is titled "MODEL.md carried
D = -19 after it became -90, and nothing anywhere recorded the optic ratios".
The spec's constants table is a SECOND AUTHOR for numbers config.py already
holds, and the way that fails is silent -- MODEL.md is the 主法则, so a reader
who takes a number from it is doing the right thing with a stale value.

THE VALUES ARE READ FROM config AT RUN TIME AND NEVER WRITTEN DOWN HERE. A
scanner that carries its own copy is just a third author, and the worst kind:
one that says everything agrees. What IS written here is the pairing -- which
row of the table describes which config name -- because that is the only part
a machine cannot infer, and it is also the part that changes when the model
changes.

WHAT IT CANNOT SEE, stated so nobody reads a pass as more than it is:

  - The 怎么测的 and 状态 columns. "两个 run 的实弹梭等效值 1.5416 / 1.5250 夹住它"
    is prose about a measurement; no constant in this repository holds it.
  - Rows whose value is not a number: `human` 的符号, `η`(交付率) = 不存在.
  - Any constant MODEL.md forgets to mention. This checks that what the table
    SAYS is true, not that the table is complete -- see MISSING_OK below for
    the one place that distinction is made mechanical.

INJECT TO SEE IT RED: change any digit in MODEL.md's table, or in the config
constant behind it. Verified both directions 2026-08-10.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:            # the table is Chinese; a cp1252 console dies printing it
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import config                                                    # noqa: E402

MODEL = ROOT / 'MODEL.md'

# (row label as it appears in the table, [(what the number is, live value)])
# `label` is matched against the row's FIRST cell, so it has to be the part of
# that cell that will not move -- the constant's name, not its prose.
CHECKS = [
    ('`K`(red_dot)',
     [('K red_dot', config.RECOIL_SIGHT_PROFILES['red_dot']['K'])]),
    ('镜位比 `R(s)`',
     [(f'ratio {s}', config.RECOIL_SIGHT_RATIO[s]) for s in ('2x', '3x', '4x')]),
    ('`M`',
     [('RECOIL_COMP_LAG_MS', config.RECOIL_COMP_LAG_MS)]),
    ('`D`',
     [('RECOIL_FIRE_DELAY_MS', config.RECOIL_FIRE_DELAY_MS)]),
]

# Rows that carry no number on purpose. Listed rather than skipped by "no digit
# found", because that test would also swallow a row whose number went missing.
MISSING_OK = ('`human` 的符号', '`human` 的幅度', '`η`(交付率)')


def rows():
    """{first cell: whole row} for the FIRST table under 三、常数现值.

    ⚠ THE FIRST TABLE, NOT EVERY TABLE IN THE SECTION, and the difference bit
    on this gate's first run: a second table further down starts with a row
    whose first cell is also `D` (it tabulates what three candidate D values
    did on screen). Scanning the whole section matched that one, read its
    single `0`, and reported the constants table as wrong while it was right --
    a gate failing on the wrong evidence, which is worse than one that passes.
    """
    text = MODEL.read_text(encoding='utf-8')
    start = text.index('常数现值')
    end = text.find('\n## ', start)
    out, seen = {}, False
    for line in text[start:end if end > 0 else len(text)].split('\n'):
        if not line.startswith('|'):
            if seen:
                break              # the first table has ended
            continue
        seen = True
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) >= 2 and not set(cells[0]) <= set('-: '):
            out[cells[0]] = line
    return out


def numbers(s):
    """Every number in a table row, as written."""
    return set(re.findall(r'−?-?\d+(?:\.\d+)?', s))


def same(written, live):
    """Does `written` mean `live`?  MODEL.md uses U+2212 for minus."""
    try:
        return float(written.replace('−', '-')) == float(live)
    except ValueError:
        return False


def main():
    table = rows()
    fails = []
    print(f'MODEL.md 三、常数现值 -- {len(table)} row(s)\n')

    for label, wants in CHECKS:
        row = table.get(label)
        if row is None:
            fails.append(f'no row matching {label!r}')
            print(f'  FAIL  {label}: no such row in the table')
            continue
        written = numbers(row)
        for what, live in wants:
            ok = any(same(w, live) for w in written)
            if not ok:
                fails.append(f'{what}: config says {live}, the row does not')
            print(f"  [{'OK ' if ok else 'FAIL'}] {what:22s} config {live!r}"
                  f"{'' if ok else '  <- not among ' + repr(sorted(written))}")

    for label in MISSING_OK:
        row = table.get(label)
        if row is None:
            fails.append(f'{label}: declared as number-free but the row is gone')
            print(f'  FAIL  {label}: declared number-free, but no such row')
        else:
            print(f'  [OK ] {label:22s} carries no checkable number, declared')

    known = {lab for lab, _ in CHECKS} | set(MISSING_OK) | {'常数'}
    unchecked = [k for k in table if k not in known]
    if unchecked:
        print('\n  not checked (no pairing declared in CHECKS):')
        for k in unchecked:
            print(f'    {k}')
        print('  A row nothing pairs is a row this gate cannot defend. Pair it'
              ' or say here why it has no author in config.')

    print()
    if fails:
        print(f'{len(fails)} FAILED:')
        for f in fails:
            print(f'  {f}')
        return 1
    print('MODEL.md agrees with config.py on every paired constant')
    return 0


if __name__ == '__main__':
    sys.exit(main())
