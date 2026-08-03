"""The unattended loop. Plain code, no model in it, about a hundred lines.

    pixi run night --weapons ar --mags 5            start (or resume)
    pixi run night --weapons ar --dry               print the plan and contract
    pixi run night --report docs/runs/<ts>          the morning read

There is no agent in the middle, and that is the design rather than an
omission. Anthropic's own guidance puts it first: plain code is preferable for
well-defined deterministic problems, and only escalates when the simpler thing
demonstrably underperforms. Measuring a cell is a fixed sequence. A model
placed inside it would improvise around failures it does not have the context
to understand -- which is not a hypothetical here: an afternoon of it produced
a magazine-swap check that verified "the slot holds something" on a slot that
is never empty, and a two-hour hunt for a drag that a docstring in the same
repo already recorded as landing 0 times out of 4.

The model belongs at the exits, where the frequency is low and the judgement
is real: reading the manifest in the morning, and deciding what a halt means.
So the loop's job is to stop cleanly and leave enough behind to be diagnosed
without the game -- see adapter.dump.

Three ways this ends, and they are different states on purpose:

    done      every cell has a verdict
    halted    HALT_STREAK failures in a row: something systemic, and
              continuing only spends the night making more of it
    killed    process died. The manifest is still true, because it is written
              after every cell -- restart to resume.
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import adapter                                    # noqa: E402
from harness.manifest import (Manifest, USABLE, FAILED,         # noqa: E402
                              SKIPPED, cell_id)
from harness.verdict import judge, PROBE_FOR                    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, 'docs', 'runs')

# Failures in a row before giving up. Two is impatient -- a single bad spawn
# happens -- and ten is a night spent on a broken detector. Four is one full
# batch of two guns failing twice.
HALT_STREAK = 4

# Attempts per cell. The first failure gets a LIGHT reset, the second a HEAVY
# one; a cell that fails after a full range re-entry has a problem that is not
# state, and a third attempt would only cost another five minutes to say so.
ATTEMPTS = 2


def plan_cells(weapons, postures, sight):
    return [(w, p, sight) for w in weapons for p in postures]


def run(manifest, rig, ac, session, mags, out_dir):
    """The loop. -> 'done' | 'halted'"""
    for cell in list(manifest.pending()):
        cid = cell['id']
        print(f"\n── {cid}  ({len(manifest.pending())} left)")
        rec, ver = None, None
        for attempt in range(1, ATTEMPTS + 1):
            rec = adapter.measure(rig, ac, cell, mags)
            ver = judge(rec)
            if ver['usable']:
                break
            print(f"   attempt {attempt}: {ver['why']} — {ver['detail']}")
            if attempt < ATTEMPTS:
                level = adapter.LIGHT if attempt == 1 else adapter.HEAVY
                if not adapter.reset(session, level=level):
                    print('   [!] reset failed — treating as systemic')
                    break

        if ver and ver['usable']:
            manifest.mark(cid, USABLE, verdict=ver, attempts=attempt)
            print(f"   usable")
        else:
            where = os.path.join(out_dir, f'fail_{cid.replace("|", "_")}')
            evidence = adapter.dump(where, ver['why'] if ver else 'exception',
                                    state=rec)
            manifest.mark(cid, FAILED, verdict=ver, evidence=evidence,
                          attempts=attempt)
            print(f"   FAILED: {ver['why']}   evidence -> "
                  f"{os.path.relpath(evidence, ROOT)}")

        streak = manifest.consecutive_failures()
        if streak >= HALT_STREAK:
            print(f'\n[HALT] {streak} cells failed in a row. Stopping rather '
                  f'than spending the night\n       producing more of the '
                  f'same. The manifest and the evidence are on disk.')
            return 'halted'
    return 'done'


def report(run_dir):
    """The morning read. Offline — no game, no hardware."""
    path = os.path.join(run_dir, 'manifest.json')
    m = Manifest.load(path)
    print(m.summary())
    reasons = m.by_reason()
    if reasons:
        print('\n  where to look:')
        for why in sorted(reasons, key=lambda w: -len(reasons[w])):
            print(f'    {why:<10} {PROBE_FOR.get(why, "—")}')
    n = m.counts()
    return 0 if n[FAILED] == 0 and n['unmeasured'] == 0 else 1


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapons', default='ar')
    ap.add_argument('--postures', default='standing')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--mags', type=int, default=5)
    ap.add_argument('--run-dir', default='',
                    help='resume this run instead of starting one')
    ap.add_argument('--report', default='',
                    help='read a finished run and stop. Offline.')
    ap.add_argument('--dry', action='store_true',
                    help='print the plan and the contract, touch nothing')
    args = ap.parse_args()

    if args.report:
        return report(args.report)

    from calibration.harvest import expand              # noqa: E402
    weapons = expand(args.weapons)
    postures = [p for p in args.postures.split(',') if p]
    cells = plan_cells(weapons, postures, args.sight)

    out_dir = args.run_dir or os.path.join(
        RUNS, f'night_{datetime.now():%Y%m%d_%H%M}')

    # --dry before anything is created. A dry run that leaves a run directory
    # behind puts a phantom night in docs/runs/ with every cell unmeasured,
    # which is indistinguishable from a real run that died before its first
    # cell -- the one state the manifest exists to report accurately.
    if args.dry:
        print(f'run     : {os.path.relpath(out_dir, ROOT)}   (not created)')
        print(f'cells   : {len(cells)}')
        print(f'halt    : {HALT_STREAK} failures in a row, {ATTEMPTS} '
              f'attempts each')
        print('\n── what the harness needs from calibration ──\n')
        print(adapter.contract())
        return 0

    os.makedirs(out_dir, exist_ok=True)
    params = {'mags': args.mags, 'sight': args.sight,
              'postures': postures, 'weapons': weapons}
    manifest, resumed = Manifest.open_or_build(
        os.path.join(out_dir, 'manifest.json'), cells, params=params)

    print(f'run     : {os.path.relpath(out_dir, ROOT)}'
          + ('   (RESUMED)' if resumed else ''))
    print(f'cells   : {len(manifest.cells)}, {len(manifest.pending())} pending')
    print(f'halt    : {HALT_STREAK} failures in a row, {ATTEMPTS} attempts each')

    print('[!] the three interfaces above are not implemented yet — see '
          'harness/CONTRACT.md')
    return 2


if __name__ == '__main__':
    sys.exit(main())
