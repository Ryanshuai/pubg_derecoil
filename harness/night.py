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
import json
import os
import sys
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import adapter                                    # noqa: E402
from harness.manifest import (Manifest, USABLE, FAILED,         # noqa: E402
                              SKIPPED, cell_id)
from harness.verdict import judge, PROBE_FOR                    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# NOT docs/runs/. That directory belongs to calibration/capture_run.py, whose
# shape is docs/runs/<kind>/<stamp>/ and whose contents are SCANS OF THE TAB
# SCREEN -- tools/test_tab_open.py reads the whole tree as ground truth for
# "the Tab screen was up in this frame".
#
# A night run is neither. Putting it there filed seven evidence frames as Tab
# scans, and those frames are the one kind that is guaranteed NOT to be:
# control/evidence.py photographs BEFORE it presses anything, precisely so a
# screen that would not open is still on disk. `pixi run tab-open` went red
# with seven false-shut failures the first time a night left evidence behind.
RUNS = os.path.join(ROOT, 'docs', 'nights')

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


def run(manifest, rigging, mags, out_dir):
    """The loop. -> 'done' | 'halted'"""
    for cell in list(manifest.pending()):
        cid = cell['id']
        print(f"\n── {cid}  ({len(manifest.pending())} left)")
        rec, ver = None, None
        for attempt in range(1, ATTEMPTS + 1):
            # An exception here used to leave the process. The contract said
            # so on purpose -- "exceptions are for the harness being wrong,
            # and the loop lets those out" -- and that was wrong about the
            # cost. A KeyError in one cell's inventory read took the whole
            # first night with it, including every cell after the one that
            # broke, and left a manifest saying `unmeasured` for all of them.
            #
            # The distinction the contract was reaching for is real, and it is
            # kept: a game-state problem is `reached=False` and a bad cell,
            # while a code fault is `crash`, which is a DIFFERENT verdict with
            # its own routing and its own place in the halt streak. What is
            # not kept is the idea that a code fault should cost the night.
            # Four in a row still halts.
            try:
                rec = adapter.measure(rigging, cell, mags)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                rec = {'reached': False,
                       'reached_why': f'{type(e).__name__}: {e}',
                       'crashed': True,
                       'traceback': traceback.format_exc()}
                print(f'   [!] {type(e).__name__}: {e}')
            ver = judge(rec)
            if ver['usable']:
                break
            print(f"   attempt {attempt}: {ver['why']} — {ver['detail']}")
            if attempt < ATTEMPTS:
                level = adapter.LIGHT if attempt == 1 else adapter.HEAVY
                if not adapter.reset(rigging, level=level):
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


def rejudge(run_dir, write=False, impulse_off=None):
    """Re-run judge() over a finished run's records. Offline.

    A threshold is a claim about what makes a measurement usable, and claims
    get corrected -- ADS_FRAC_MIN went from 0.90 to 0.80 the same night it was
    written, after rejecting a clean akm cell twice at 0.897. Re-FIRING 23
    cells to find out what the new number says would cost an hour of game time
    to recompute something that is a pure function of numbers already on disk.

    So the records are kept whole in cells.jsonl and the verdict is derived
    again from them. This is only sound because judge() is a pure function of
    the record: same record, same thresholds, same answer, tonight or next
    month. The moment it reads anything else, this stops being safe.

    What it CANNOT do: revive a cell whose record was never written. A cell
    that crashed or never reached its configuration has no numbers to re-judge
    and stays exactly as it was -- which is the distinction the manifest exists
    to keep, so it is preserved rather than papered over.
    """
    from harness.adapter import _blank, _fill

    path = os.path.join(run_dir, 'manifest.json')
    m = Manifest.load(path)
    params = m.data.get('params') or {}
    impulse = params.get('impulse_off')
    if impulse is None and impulse_off is not None:
        # The manifest did not record it -- runs from before it was a param,
        # which includes the two nights this was written for. Supplying it by
        # hand is a claim the caller is making about a measurement they took;
        # it is printed so the claim is in the output rather than only in
        # somebody's shell history.
        impulse = impulse_off
        print(f'impulse : {impulse:+.2f} rounds, supplied on the command line '
              f'(the manifest predates the field)')
    if impulse is None:
        print('impulse : NOT RECORDED and not supplied — every cell will '
              'fail closed on `impulse`.\n          Pass --impulse-off with '
              'the value measured for that run.')

    records = {}
    cells_path = os.path.join(run_dir, 'cells.jsonl')
    if os.path.exists(cells_path):
        with open(cells_path, encoding='utf-8') as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get('type') != 'cell':
                    continue
                # LAST wins, and it is deliberately NOT "the record the
                # verdict was formed on" -- it is the last one that actually
                # MEASURED something, which is not the same thing and is the
                # better one.
                #
                # g36c is the case. Attempt 1 kitted, fired four clean
                # magazines and was rejected by a threshold that was wrong.
                # Attempt 2 never got past the kit, so it wrote no record, and
                # the manifest kept ITS reason (`state`) -- which describes the
                # retry, not the cell. Only attempt 1's numbers exist, so LAST
                # picks them, and the cell re-judges as the usable measurement
                # it always was.
                #
                # The manifest keeping only the last attempt's verdict is a
                # real gap this papers over rather than fixes: a cell can read
                # as a failure of something that never happened to it.
                records[cell_id(r['weapon'], r['posture'],
                                r.get('sight') or params.get('sight'))] = r

    changed = []
    for cell in m.cells:
        rec = records.get(cell['id'])
        if rec is None:
            continue
        ver = judge(_fill(_blank(cell), rec, impulse))
        was = cell['state']
        now = USABLE if ver['usable'] else FAILED
        if now != was or (cell.get('verdict') or {}).get('why') != ver['why']:
            changed.append((cell['id'], was, now,
                            (cell.get('verdict') or {}).get('why'), ver['why']))
        if write:
            m.mark(cell['id'], now, verdict=ver,
                   evidence=cell.get('evidence'),
                   attempts=cell.get('attempts'))

    print(f'{len(records)} cell record(s) on disk, {len(changed)} verdict(s) '
          f'change under the current thresholds'
          + ('' if write else '   (dry — pass --write to apply)'))
    for cid, was, now, why_was, why_now in changed:
        print(f'  {cid:<28} {was} ({why_was}) -> {now} ({why_now})')
    if write and changed:
        print(f'\nmanifest rewritten: {path}')
    return 0


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
    ap.add_argument('--impulse-off', type=float, default=None,
                    help='rounds the commanded spike landed from where it '
                         'was asked for, from a `pixi run impulse-ab` taken '
                         'THIS session. Without it every cell fails closed on '
                         '`impulse` — deliberately: the timing chain is the '
                         'one thing a clean residual cannot vouch for, so an '
                         'unverified night is worth nothing and should say so '
                         'rather than produce curves.')
    ap.add_argument('--no-ema', action='store_true',
                    help='measure without writing back to any curve')
    ap.add_argument('--countdown', type=int, default=6)
    ap.add_argument('--run-dir', default='',
                    help='resume this run instead of starting one')
    ap.add_argument('--report', default='',
                    help='read a finished run and stop. Offline.')
    ap.add_argument('--rejudge', default='',
                    help='re-run the verdict over a finished run\'s records '
                         'with the CURRENT thresholds, and say what changes. '
                         'Offline, and a dry run unless --write. For when a '
                         'threshold was wrong, which is cheaper to correct '
                         'than a night is to re-fire.')
    ap.add_argument('--write', action='store_true',
                    help='--rejudge: actually rewrite the manifest')
    ap.add_argument('--dry', action='store_true',
                    help='print the plan and the contract, touch nothing')
    args = ap.parse_args()

    if args.report:
        return report(args.report)
    if args.rejudge:
        return rejudge(args.rejudge, write=args.write,
                       impulse_off=args.impulse_off)

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
              'postures': postures, 'weapons': weapons,
              # Recorded because --rejudge needs it: it is a measurement taken
              # OUTSIDE the cell, so no cell record carries it, and re-judging
              # without it would fail every cell closed on `impulse` and look
              # like the night had gone wrong.
              'impulse_off': args.impulse_off}
    manifest, resumed = Manifest.open_or_build(
        os.path.join(out_dir, 'manifest.json'), cells, params=params)

    print(f'run     : {os.path.relpath(out_dir, ROOT)}'
          + ('   (RESUMED)' if resumed else ''))
    print(f'cells   : {len(manifest.cells)}, {len(manifest.pending())} pending')
    print(f'halt    : {HALT_STREAK} failures in a row, {ATTEMPTS} attempts each')
    if args.impulse_off is None:
        print('impulse : NOT CHECKED — every cell will fail closed. Run '
              '`pixi run impulse-ab`\n          and pass --impulse-off.')
    else:
        print(f'impulse : {args.impulse_off:+.2f} rounds off, measured this '
              f'session')

    rigging, why = adapter.open_rig(
        args.sight, out_dir, apply_ema=not args.no_ema,
        countdown=args.countdown, impulse_off=args.impulse_off,
        weapons=weapons)
    if rigging is None:
        # Not an exception. A night that cannot start has a reason the morning
        # can read, and the manifest already says every cell is unmeasured --
        # which is the true statement, and a different one from "failed".
        print(f'\n[!] ABORT: {why}')
        return 1

    try:
        how = run(manifest, rigging, args.mags, out_dir)
    except KeyboardInterrupt:
        how = 'interrupted'
    finally:
        try:
            adapter.reset(rigging, level=adapter.LIGHT)
        except Exception:
            pass
        rigging.close()
        manifest.save()

    print(f'\n{how}.')
    print(manifest.summary())
    print(f'\n  morning read:  pixi run night --report '
          f'{os.path.relpath(out_dir, ROOT)}')
    return 0 if how == 'done' else 1


if __name__ == '__main__':
    sys.exit(main())
