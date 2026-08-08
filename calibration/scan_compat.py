"""Scan every weapon's attachment slots and check the catalogue against them.

The verification `attachment_catalog` has referenced since it was written and
that never existed. Its SLOTS table is 22 wiki readings, 6 guesses and 2
screenshot reads — 0 measured in game. This measures them.

    pixi run python calibration/scan_compat.py                # all 30
    pixi run python calibration/scan_compat.py --only m416,vss
    pixi run python calibration/scan_compat.py --start-from tommy
    pixi run python calibration/scan_compat.py --report <run_dir>   # offline

Per weapon: spawn it into slot 2, open Tab, screenshot, read the five slots
with SlotDetector, close Tab. Runs land in calibration/artifacts/runs/slot_scan/<stamp>/ in the shared CaptureRun format
(calibration/capture_run.py), one full-screen capture per weapon, so every
claim here can be re-checked offline and other skills can consume the same
captures without driving the game again.

NO STRIPPING, DELIBERATELY. Spawning does not give a bare gun — PUBG auto-fits
whatever the backpack holds. That does not matter for slot PRESENCE, which
reads the tile's border ring and is nearly blind to contents (a stripped M416
reads 260/260/278/260 where a fitted one reads 260/260/318/260). And the
auto-fitted set is free evidence in the other direction: the game only fits
what is compatible, so every part it puts on is a confirmed fit that cost
nothing. Stripping would throw that away, put the parts back in the backpack,
and double the run time.

WHAT THIS DOES NOT ANSWER: which parts a present slot accepts. Tommy Gun's
muzzle takes a suppressor and refuses a compensator, and both leave the same
tile. That needs drags — see the calibrate-compat skill, Step 2. Nor does it
answer the scope slot, which draws no tile at all and always reads 'unknown'.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

from calibration.capture_run import CaptureRun, LABEL_DETECTED
from detector.attachment_catalog import ROSTER, SLOTS
from control.lobby import LobbyControl
from detector.slot_detector import SlotDetector
from detector.tab_layout import SLOT_NAMES
from control.focus import focus_keeper
from control.session import ensure_ready
from tools.drive_screen import SCREENS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIND = 'slot_scan'      # see calibration/capture_run.py for the layout

GUN_SLOT = 2            # the spawner always lands weapons in slot 2
SETTLE = 0.35


def scan_one(sc, ac, det, weapon, run):
    """Spawn one weapon and read its slots. -> record dict

    The two panels are driven by the control/ drivers that own them, not by a
    local press-and-sleep helper. There used to be one here — a `toggle(ptr,
    screen, want)` that pressed the key and slept on a per-screen constant —
    which was the fifth copy of that loop in the tree, on top of the four
    tools/drive_screen.py's own docstring already complains about. The copies
    do not merely duplicate: SpawnerControl.ensure_panel and
    InventoryControl.ensure_tab both re-press when the game SWALLOWS the key
    (docs/game_quirks.md), and a two-try sleep loop reads that as "the panel
    would not open" and drops the weapon from the scan.
    """
    rec = {'weapon': weapon, 'ok': False, 'error': None,
           'slots': None, 'scores': None, 'fitted': None}

    if not sc.ensure_panel(True):
        rec['error'] = 'spawner would not open'
        return rec
    got = sc.give_weapon(weapon)
    if not got.get('ok'):
        sc.ensure_panel(False)
        rec['error'] = f'spawn failed: {got.get("error")}'
        return rec
    # Tab and the spawner panel cannot share the screen: the panel swallows Tab.
    if not sc.ensure_panel(False):
        rec['error'] = 'spawner would not close'
        return rec

    if not ac.ensure_tab(True):
        rec['error'] = 'tab would not open'
        return rec
    frame = SCREENS['tab'].shoot()      # parks the cursor first — a hovered
                                        # row draws a highlight that moves the
                                        # measured bounds
    ac.ensure_tab(False)

    scores = det.scores(frame, GUN_SLOT)
    rec.update(ok=True, shot=f'{weapon}.png',
               slots={s: scores[s]['state'] for s in SLOT_NAMES},
               scores={s: {'ring': scores[s]['ring'],
                           'edges': scores[s]['edges']} for s in SLOT_NAMES},
               fitted=[s for s in SLOT_NAMES
                       if scores[s]['state'] == 'filled'])

    # Auto-fitted parts are recorded as LABEL_DETECTED, never as truth: nobody
    # asked for them, so only the detector under test can name them. Fitting
    # on purpose (add_fit) is what produces ground truth — see capture_run.
    run.add(frame, weapon, weapon=weapon, gun_slot=GUN_SLOT,
            slots=rec['slots'], scores=rec['scores'],
            labels=[{'slot': s, 'asset': None, 'source': LABEL_DETECTED}
                    for s in rec['fitted']])
    return rec


def compare(rec):
    """Screen vs catalogue for one weapon. -> list of disagreements."""
    if not rec['ok']:
        return []
    have = set(SLOTS.get(rec['weapon'], {}).get('slots', ()))
    out = []
    for s in SLOT_NAMES:
        state = rec['slots'][s]
        if state == 'unknown':          # scope: no tile, never a claim
            continue
        on_screen = state in ('empty', 'filled')
        if on_screen and s not in have:
            out.append((s, 'catalogue MISSING it', state))
        elif not on_screen and s in have:
            out.append((s, 'catalogue CLAIMS it', state))
    return out


def report(records):
    """Print the diff table. -> number of weapons that disagree."""
    print(f'\n{"weapon":10} {"conf":8} {"catalogue":34} {"screen":34}')
    bad = 0
    for r in records:
        w = r['weapon']
        if not r['ok']:
            print(f'{w:10} {"":8} {"":34} FAILED: {r["error"]}')
            bad += 1
            continue
        entry = SLOTS.get(w, {})
        cat = ','.join(sorted(entry.get('slots', ()))) or '(none)'
        seen = ','.join(s for s in SLOT_NAMES
                        if r['slots'][s] in ('empty', 'filled')) or '(none)'
        unk = [s for s in SLOT_NAMES if r['slots'][s] == 'unknown']
        if unk:
            seen += f'  +{"/".join(unk)}?'
        diffs = compare(r)
        mark = '  <<<' if diffs else ''
        print(f'{w:10} {entry.get("conf", "?"):8} {cat:34} {seen:34}{mark}')
        for s, why, state in diffs:
            print(f'{"":10} {"":8} !! {s}: {why} (screen says {state})')
            bad += 1
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', default=None, help='comma-separated keys')
    ap.add_argument('--start-from', default=None)
    ap.add_argument('--report', default=None, metavar='RUN_DIR',
                    help='offline: re-print the diff from a finished run')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    if args.report:
        run = CaptureRun.load(args.report, KIND)
        recs = [{'weapon': e['weapon'], 'ok': True, 'slots': e['slots']}
                for e in run.entries]
        bad = report(recs)
        print(f'\n{bad} disagreement(s)')
        return 0

    weapons = list(ROSTER)
    if args.only:
        want = [s.strip() for s in args.only.split(',') if s.strip()]
        weapons = [w for w in weapons if w in want]
    if args.start_from:
        if args.start_from not in weapons:
            print(f'{args.start_from} not in the list')
            return 1
        weapons = weapons[weapons.index(args.start_from):]
    if not weapons:
        print('nothing to scan')
        return 1

    # One call replaces the focus check AND the ensure_in_match below it, and
    # adds the two this scan was missing: Tab and the spawner panel both eat
    # the keypresses it is about to send. Its match leg also moves to the 200m
    # lane, off the compound where the traffic is.
    #
    ready = ensure_ready(label='scan_compat', countdown_s=args.countdown)
    if not ready['ok']:
        print(f'could not get the game ready — failed at {ready["failed"]!r}')
        return 1

    from control.inventory import InventoryControl
    from control.spawner import SpawnerControl
    sc = SpawnerControl()
    ac = InventoryControl(verbose=False)
    if not (sc.can_press() and ac.can_press()):
        print('no Pico — the spawner panel and Tab are both opened by keypress')
        return 1
    det = SlotDetector()

    # The run OWNS the directory. This used to build its own `out_dir` from a
    # module-level RUNS constant and then hand `run` to scan_one anyway —
    # except RUNS and `run` were both left behind when the artifacts moved
    # under docs/ (plan 5h), so neither existed and the live path died with a
    # NameError before it touched the game. Only --report still worked, which
    # is why it went unnoticed: --report builds its own CaptureRun and returns.
    run = CaptureRun.create(KIND, note=f'{len(weapons)} weapons')
    print(f'scanning {len(weapons)} weapons -> {run.path}\n')

    records = []
    t0 = time.perf_counter()
    for i, w in enumerate(weapons, 1):
        if not focus_keeper().ok(f'scan {w}'):
            print('lost the foreground and could not take it back — stopping')
            break
        el = time.perf_counter() - t0
        print(f'[{i:2d}/{len(weapons)}] {w:10} ({el:5.0f}s)', end=' ',
              flush=True)
        r = scan_one(sc, ac, det, w, run)
        records.append(r)
        if r['ok']:
            seen = ','.join(s for s in SLOT_NAMES
                            if r['slots'][s] in ('empty', 'filled'))
            d = compare(r)
            print(f'{seen or "(none)"}'
                  f'{"   <<< DIFFERS" if d else ""}')
        else:
            print(f'FAILED: {r["error"]}')
            # A spawn failure is usually a panel left in the wrong state.
            # Re-establish rather than carrying it into the next weapon.
            with LobbyControl(verbose=False) as lc2:
                lc2.ensure_in_match()

    bad = report(records)
    print(f'\n{len(records)} scanned in {time.perf_counter() - t0:.0f}s, '
          f'{bad} disagreement(s)')
    print(f'run: {run.path}')
    print(f'Re-read offline with --report {run.stamp}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
