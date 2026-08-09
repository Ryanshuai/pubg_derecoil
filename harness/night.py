"""The unattended loop. Plain code, no model in it, about a hundred lines.

    pixi run night --weapons ar --mags 5            start (or resume)
    pixi run night --weapons ar --dry               print the plan and contract
    pixi run night --report calibration/artifacts/runs/<ts>          the morning read

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
from harness.manifest import Manifest, USABLE, FAILED, cell_id  # noqa: E402
from harness.verdict import judge, PROBE_FOR                    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# NOT calibration/artifacts/runs/. That directory belongs to calibration/capture_run.py, whose
# shape is calibration/artifacts/runs/<kind>/<stamp>/ and whose contents are SCANS OF THE TAB
# SCREEN -- tools/test_tab_open.py reads the whole tree as ground truth for
# "the Tab screen was up in this frame".
#
# A night run is neither. Putting it there filed seven evidence frames as Tab
# scans, and those frames are the one kind that is guaranteed NOT to be:
# control/evidence.py photographs BEFORE it presses anything, precisely so a
# screen that would not open is still on disk. `pixi run tab-open` went red
# with seven false-shut failures the first time a night left evidence behind.
RUNS = os.path.join(ROOT, 'calibration', 'artifacts', 'nights')

# Failures in a row before giving up. Two is impatient -- a single bad spawn
# happens -- and ten is a night spent on a broken detector. Four is one full
# batch of two guns failing twice.
HALT_STREAK = 4

# Attempts per cell. The first failure gets a LIGHT reset, the second a HEAVY
# one; a cell that fails after a full range re-entry has a problem that is not
# state, and a third attempt would only cost another five minutes to say so.
ATTEMPTS = 2


def _slot_changes(a, b):
    """How many attachment ACTIONS separate two fill mappings.

    ⚠ SWAPPING A PART IS ONE CHANGE, NOT TWO, and it is not zero either. This
    replaced `len(a ^ b)` on frozensets when a config gained the power to name
    its part: on sets, {'grip'} and {'grip'} are identical, so 'grip=vert_grip'
    and 'grip=half_grip' would have cost NOTHING to reorder between -- the
    planner would rank a real grip swap as free and schedule around a fiction.
    Counting slots whose VALUE differs answers the question the caller is
    actually asking: how many slots does the kitter have to touch.

    ⚠ MEMBERSHIP FIRST, VALUE SECOND, and `a.get(s) != b.get(s)` alone is
    WRONG here for the same reason it is wrong in want_for: 'bare' and 'muzzle'
    both answer None for the muzzle -- one because the slot is absent, one
    because it takes the class default -- so a plain .get comparison rates the
    whole 2^3 factorial as costing ZERO. `pixi run plan-order` caught that on
    the case that pins 13 -> 7.
    """
    return sum(1 for s in set(a) | set(b)
               if (s in a, a.get(s)) != (s in b, b.get(s)))


def order_configs(configs):
    """Reorder configs so consecutive cells differ by as FEW slots as possible.

    -> the same configs, permuted. Never adds, drops or renames one.

    ⚠ THIS IS PURE SCHEDULING AND IT IS FREE. Every cell pins every controlled
    slot -- want_for forces the ones it does not fill EMPTY -- so a cell's
    result cannot depend on which cell ran before it. The order is therefore
    unconstrained by correctness, and it was costing measurable work:

        typed order   bare muzzle grip stock muzzle+grip muzzle+stock
                      grip+stock muzzle+grip+stock          13 slot changes
        this order    bare muzzle muzzle+grip grip grip+stock
                      muzzle+grip+stock muzzle+stock stock   7 slot changes

    A full 2^3 factorial is 13 attachment actions in the order a human types
    it and 7 in Gray-code order -- 46% fewer. Brute force over all 8! orders
    says 7 is the floor, so this is optimal and not merely better.

    The saving is not only seconds. Measured over the shared gesture journal:
    1115 gestures aimed at a gun slot produced 789 landed fits (1.41 gestures
    per fit) and 21% of fits needed a retry. Fitting is this project's largest
    single source of wasted runs, so 46% fewer fits is 46% fewer chances for
    the failure mode that eats a cell.

    Greedy nearest-neighbour from the emptiest config, then brute-forced when
    the list is short enough for it to be cheap. It starts at the emptiest
    because a cell arriving on a freshly spawned gun starts nearest to bare,
    and because `pending()` may hand back any tail of the plan on --resume.

    ⚠ IT WORKS ON INDICES, NOT ON NAMES, and its own gate is why. Filtering
    the candidates by VALUE (`[c for c in configs if c != start]`) silently
    collapsed a repeated config -- ['bare','muzzle','bare'] came back with one
    bare -- which is a planner turning two cells into one without saying so.
    De-duplication is supported_configs' job and it has already run; anything
    dropped here would be dropped invisibly.

    ⚠ AN UNPARSEABLE NAME DISABLES THE REORDER, and the first draft could not
    detect one: `parse_config(c) or frozenset()` turns None into the empty set,
    so the guard that followed could never fire and a junk name got scheduled
    as though it were `bare`. plan_cells drops such names itself, and this
    hands the list back untouched rather than sorting around a fiction.
    """
    from itertools import permutations
    from control.kitting import parse_config

    names = list(configs)
    if len(names) < 3:
        return names
    parsed = [parse_config(c) for c in names]
    if any(p is None for p in parsed):
        return names

    def cost(order):
        return sum(_slot_changes(parsed[a], parsed[b])
                   for a, b in zip(order, order[1:]))

    idx = list(range(len(names)))
    start = min(idx, key=lambda i: (len(parsed[i]), names[i]))
    greedy, rest = [start], [i for i in idx if i != start]
    while rest:
        nxt = min(rest, key=lambda i: (_slot_changes(parsed[i],
                                                     parsed[greedy[-1]]),
                                       names[i]))
        greedy.append(nxt)
        rest.remove(nxt)
    # ⚠ 8! is 40320 and each score is 7 set-diffs, so the exact answer costs
    # milliseconds at the size this actually runs at (2^3 = 8 configs). Above
    # that the greedy chain stands on its own -- it is what seeds the search
    # anyway, and it already returns the optimum on the full factorial.
    order = greedy
    if len(names) <= 8:
        best = min(permutations(idx), key=cost)
        if cost(best) < cost(greedy):
            order = list(best)
    return [names[i] for i in order]


def gray_order(axes):
    """Every combination of `axes`, ordered so ONE slot changes per step.

    `axes` is {slot: [value, ...]} with None meaning empty, e.g.

        {'muzzle': [None, 'comp_ar'], 'grip': [None, 'vert_grip', 'half_grip']}

    -> [{'muzzle': ..., 'grip': ...}, ...], every combination exactly once.

    ⚠ THIS IS THE GENERALISATION order_configs CANNOT DO, and the difference is
    not size, it is kind. order_configs works on SETS OF SLOT NAMES -- a config
    is "which slots are filled" -- so it cannot express vert_grip against
    angled_grip at all. Today a second part in the same slot is a separate
    night (`--parts grip=tilted_grip`), which means the ordering never crosses
    that axis and every boundary between runs pays the worst case. Asked for in
    those terms: 「grip 那一步，其实是多个 grip 轮番换。这个多种的怎么做格雷码」.

    ⚠ AND IT IS A CONSTRUCTION, NOT A SEARCH, because the search does not
    survive the generalisation. order_configs brute-forces 8! = 40320 and calls
    the winner optimal. Five grips x four muzzles x three stocks is 60 cells,
    and 60! is not a number anything enumerates.

    The mixed-radix reflected Gray code needs no search: write the index in
    mixed radix, then reflect digit j whenever the digits ABOVE it sum to odd.
    That is the same trick the binary reflected code uses, with unequal bases.

    ⚠ WHY THE RESULT IS OPTIMAL WITHOUT BEING CHECKED AGAINST ANYTHING:
    visiting N cells takes at least N-1 transitions, and every transition here
    costs exactly one slot change, so the tour costs exactly N-1. Nothing can
    beat that. What `pixi run gray` verifies is not the optimality -- it is the
    PROPERTY the optimality rests on, because a construction that quietly
    changed two slots somewhere would still look like a plausible tour.
    """
    slots = list(axes)
    radix = [len(axes[s]) for s in slots]
    if not slots or any(r == 0 for r in radix):
        return []
    total = 1
    for r in radix:
        total *= r
    out = []
    for i in range(total):
        digits, rem = [0] * len(radix), i
        for j in range(len(radix) - 1, -1, -1):
            digits[j] = rem % radix[j]
            rem //= radix[j]
        cell, higher = {}, 0
        for j, s in enumerate(slots):
            d = radix[j] - 1 - digits[j] if higher % 2 else digits[j]
            cell[s] = axes[s][d]
            # ⚠ THE OUTPUT DIGIT, NOT THE INPUT ONE. Accumulating digits[j]
            # here reads like the same thing and is not: on radices (2,2,2) it
            # sends index 4 to (1,1,1) where the binary reflected code says
            # (1,1,0), so one step changes TWO slots and the 2^3 tour costs 8
            # instead of 7. `pixi run gray` caught it on the case that pins the
            # number order_configs already proves.
            higher += d
        out.append(cell)
    return out


def plan_cells(weapons, postures, sight, configs=('bare',)):
    """Every (weapon, posture, sight, config) the night will attempt.

    ⚠ CONFIGS ARE PER WEAPON, not a global list, because a config naming a
    slot the weapon does not have is not a cell -- it is a guaranteed failure
    that costs a spawn, a kit attempt and a place in the halt streak. groza
    has one muzzle and no lower rail; asking it for `muzzle+grip` would fail
    four times in a row and stop the night on the strength of a plan error.

    The filtering is calibration.harvest.supported_configs, not a catalogue
    lookup here: layering rule 5 forbids this package from importing detector,
    and the reason applies exactly to this -- a planner that decides for itself
    what a weapon can wear is a second opinion about the game sitting beside
    the one it is supposed to be judging.
    """
    from control.kitting import supported_configs

    # ⚠ ORDERED PER WEAPON, AFTER supported_configs, and both halves matter.
    # Degradation is many-to-one -- on a gun with no lower rail 'grip' and
    # 'muzzle+grip' collapse onto configs it already has -- so the list this
    # weapon will actually wear is not the list that was typed, and ordering
    # the typed one would be scheduling cells that do not exist.
    #
    # `postures` stays innermost: it changes no attachment, so a posture sweep
    # inside one config costs nothing to set up.
    return [(w, p, sight, c)
            for w in weapons
            for c in order_configs(supported_configs(w, configs))
            for p in postures]


def _ship(rec, cell):
    """Put a USABLE cell's fitted curve where the RUNTIME reads it.

    ⚠ NOTHING DID THIS, so an unattended night measured every cell and shipped
    none of them. The fit went into the record and the seed stayed on disk, and
    the next visit to that cell fired the community guess again. Firing a seed
    is not WRONG -- y_comp is read back off the firmware, so y_true is exact
    whatever played -- but it is imprecise in a way that compounds: the further
    the compensation is from the truth, the further the view travels, and the
    more of the answer has to come off the screen instead of off the device.
    The vector's flash_smg cell failed the arms check twice on a wiki-guessed
    seed of 914 counts against a real 750, while comp_smg, whose seed was
    MEASURED, passed at 3.4%.

    ⚠ ONLY WHEN judge() SAID USABLE, and that is why it lives in the loop
    rather than in measure(). A cell that failed its arms check has a fit too,
    and it is the one thing that must never reach the runtime: the fit always
    succeeds, so "there is a curve" is not evidence about the curve.

    Asked for 2026-08-09:「每次结尾都更新曲线？」
    """
    from calibration.fit_time_curve import _write_curve
    from calibration.samples import config_key
    try:
        # Every key _write_curve needs, by name and NOT with `.get`: a missing
        # one has to raise here rather than write a curve with a hole in it.
        res = {'knots': rec['curve'], 'n_kept': rec['n_kept'],
               'grid_ms': rec['grid_ms'], 'total_counts': rec['total_counts'],
               'span_s': rec['span_s'],
               'spread_counts': rec.get('spread_counts'),
               'samples_per_knot': rec.get('samples_per_knot')}
        _write_curve(res, cell['weapon'], config_key(rec.get('config_read')),
                     rec.get('sight_read') or cell.get('sight') or 'red_dot',
                     cell['posture'], rec.get('n_total'))
    except Exception as e:                          # noqa: BLE001 — reported
        print(f'   [!] the cell is usable but its curve did not ship ({e}). '
              f'The samples are on disk; `fit_time_curve --write` ships it.')


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
            _ship(rec, cell)
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


def rejudge(run_dir, write=False):
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
    path = os.path.join(run_dir, 'manifest.json')
    m = Manifest.load(path)
    params = m.data.get('params') or {}

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
        # ⚠ judge() READS THE STORED RECORD DIRECTLY. It used to be handed
        # through adapter._fill first, which meant a re-judge depended on the
        # MEASUREMENT layer's shape as well as the record's -- so porting the
        # measurement would have silently changed what old runs re-judge to.
        # The record already carries every field judge() reads; that is what
        # RECORD_FIELDS is for.
        ver = judge(rec)
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
    # ⚠ DEFAULTS TO BARE, which is what this loop measured before configs
    # existed. A config naming a slot the weapon does not have is dropped by
    # plan_cells rather than attempted, so one list can be handed to a mixed
    # roster: 'bare,muzzle,grip,stock,muzzle+grip,...' gives groza one cell
    # and m416 eight.
    ap.add_argument('--configs', default='bare',
                    help="comma-separated slot combinations to measure, e.g. "
                         "'bare,muzzle,grip,muzzle+grip'. Slots a weapon does "
                         "not have are skipped for that weapon.")
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--mags', type=int, default=5)
    # ⚠ --impulse-off AND --no-ema WENT ON 2026-08-08 WITH THE MODEL.
    # The round-alignment probe (rejected outright, see harness/verdict.py)
    # checked that the measurement grid and the firmware's
    # playback grid shared an origin; under MODEL.md they do by construction,
    # and the out-of-loop check that replaced it is arranged PER CELL by
    # adapter.measure firing more than one compensation arm. --no-ema had
    # nothing left to switch off: every fit is a full refit over the stored
    # samples, so nothing is written back to a curve during a run at all.
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
        return rejudge(args.rejudge, write=args.write)

    from control.kitting import expand              # noqa: E402
    weapons = expand(args.weapons)
    postures = [p for p in args.postures.split(',') if p]
    configs = [c for c in args.configs.split(',') if c]
    cells = plan_cells(weapons, postures, args.sight, configs)

    out_dir = args.run_dir or os.path.join(
        RUNS, f'night_{datetime.now():%Y%m%d_%H%M}')

    # --dry before anything is created. A dry run that leaves a run directory
    # behind puts a phantom night in calibration/artifacts/runs/ with every cell unmeasured,
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
    # ⚠ NOTHING TAKEN OUTSIDE THE CELL IS RECORDED HERE ANY MORE, and that
    # is the point of the port: --rejudge used to need `impulse_off` because
    # the out-of-loop check was a per-session probe no cell record carried.
    # The replacement lives inside each cell's own numbers (agree_arms /
    # agree_spread), so a re-judge is a pure function of cells.jsonl.
    params = {'mags': args.mags, 'sight': args.sight,
              'postures': postures, 'weapons': weapons}
    manifest, resumed = Manifest.open_or_build(
        os.path.join(out_dir, 'manifest.json'), cells, params=params)

    print(f'run     : {os.path.relpath(out_dir, ROOT)}'
          + ('   (RESUMED)' if resumed else ''))
    print(f'cells   : {len(manifest.cells)}, {len(manifest.pending())} pending')
    print(f'halt    : {HALT_STREAK} failures in a row, {ATTEMPTS} attempts each')
    # The out-of-loop check is arranged PER CELL now — adapter.ARM_PLAN fires
    # more than one compensation curve, and a cell whose pool holds only one
    # arm fails closed on `agree`. There is nothing session-wide to announce
    # here, which is the improvement: the old line printed a warning about a
    # probe somebody had to remember to run in another window.
    print(f'arms    : {adapter.ARM_PLAN} per cell — a cell with one arm fails '
          f'closed')

    rigging, why = adapter.open_rig(
        args.sight, out_dir, countdown=args.countdown,
        weapons=weapons, configs=configs)
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
