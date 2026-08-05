"""What an EMPTY attachment tile scores, per weapon. The negative class.

    pixi run python calibration/scan_bare_tiles.py --only akm
    pixi run python calibration/scan_bare_tiles.py
    pixi run python calibration/scan_bare_tiles.py --report <run_dir>

Spawn each weapon into an empty rack with an empty backpack, so it arrives
wearing nothing, and read all five tiles with BOTH readers. Every number that
comes back describes a slot that is empty by construction -- nobody asked a
detector.

WHY. `SlotDetector` calls a slot `filled` on Canny edges inside the tile, and
the weapon's own picture is drawn behind it. An AKM's magazine reaches down
into the box: the tile reads `filled` forever, `strip` pulls the slot that is
already empty, and the gesture lands on the weapon row underneath and throws
the whole gun on the floor (see unequip). Watched on screen 2026-08-04, 74
times across 11 collector runs before that.

The fix being measured for is `filled` ⟺ A PART IS RECOGNISED -- not "there
are edges", which the weapon's own magazine produces and which no threshold
can separate, because the edges are real and they are a magazine. That turns
the question into "how well does an empty tile match the bank", and the
existing corpus cannot answer it: its 281 empty tiles cover sks, uzi and
vector, none of which bleeds (tools/scan_slot_bleed.py).

THE SCOPE POSITION DRAWS NO TILE and reads `unknown` forever. It is scanned
anyway -- config.py already records 71 edges of "weapon render showing
through" there, which is this same effect measured and left alone.

Lands in docs/runs/bare_tiles/<stamp>/ with one full screen per weapon, so
every number here is re-checkable offline, and writes the per-weapon summary
to docs/compat/bare_tiles.json -- a conclusion, not raw data, so it goes in
git under the rule in calibration/CLAUDE.md.
"""
import argparse
import json
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

from capture_run import CaptureRun
from config import HUD_REGIONS, TAB_SLOT_FILLED_EDGES
from control.session import ensure_ready
from control.focus import focus_keeper
from control.inventory import at_ground
from detector.attachment_catalog import ROSTER
from detector.attachment_detector import AttachmentDetector, MSE_EMPTY_TH
from detector.slot_detector import SlotDetector
from detector.tab_layout import SLOT_NAMES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIND = 'bare_tiles'
OUT = os.path.join(ROOT, 'docs', 'compat', 'bare_tiles.json')

PLATE_INK_MIN = 200     # collect_templates' band: 0 empty, 679-901 with a gun
RISE_MIN = 3.0          # AKM measured 29.8 fitted -> 346.6 bare, a 11.6x rise


def read_one(ac, det, slots, frame, gun, weapon):
    """Both readers on all five tiles. -> {slot: {...}}"""
    sc = slots.scores(frame, gun)
    out = {}
    for s in SLOT_NAMES:
        y, x, h, w = HUD_REGIONS[f'att_{gun}_{s}']
        crop = frame[y:y + h, x:x + w]
        cand = det.candidates(s, weapon)
        # Three ways the bank can decline, kept apart. `drawn` failing is the
        # SAFE answer -- the tile holds too little detail to be anything --
        # and lumping it in with "no template for this slot" would hide which
        # weapons the catalogue simply has nothing to say about.
        if not cand:
            hit, mse, margin, why = '', None, None, 'no candidates'
        elif not det.drawn(crop):
            hit, mse, margin, why = '', None, None, 'not drawn'
        else:
            hit, m, g = det.best_two(crop, cand)
            hit, mse, margin, why = hit, float(m), float(g), ''
        out[s] = {'tile': sc[s]['state'], 'ring': sc[s]['ring'],
                  'edges': sc[s]['edges'], 'hit': hit, 'mse': mse,
                  'margin': margin, 'why': why}
    return out


def scan_one(sc, ac, det, slots, weapon, run):
    rec = {'weapon': weapon, 'ok': False, 'error': None, 'slots': None}

    # THE RACK EMPTY FIRST, so "which slot holds a gun" has one answer, and
    # so the gun that arrives is this weapon rather than a previous round's.
    # Dropped guns take their attachments with them (drop_weapon), which is
    # also what keeps the backpack empty and the next spawn bare.
    if not ac.ensure_tab(True):
        rec['error'] = 'tab would not open to clear the rack'
        return rec
    ac.clear_rack()
    if not ac.ensure_tab(False):
        rec['error'] = 'tab would not close'
        return rec

    if not sc.ensure_panel(True):
        rec['error'] = 'spawner would not open'
        return rec
    got = sc.give_weapon(weapon)
    if not got.get('ok'):
        sc.ensure_panel(False)
        rec['error'] = f'spawn failed: {got.get("error")}'
        return rec
    if not sc.ensure_panel(False):
        rec['error'] = 'spawner would not close'
        return rec

    if not ac.ensure_tab(True):
        rec['error'] = 'tab would not open to read'
        return rec
    frame = ac.frame()
    gun = ac.gun_slot()
    # gun_slot() answers "which row draws its boxes", which a blurred empty
    # panel satisfies too. The name plate does not have that failure mode --
    # 0 ink on an empty row against 679-901 with a gun. Without this the scan
    # would happily report five tile readings off a rack with no gun in it,
    # which is the exact shape of bug it exists to measure.
    ink = ac.plate_ink(gun, frame) if gun else 0
    if gun is None or ink < PLATE_INK_MIN:
        ac.ensure_tab(False)
        rec['error'] = (f'no gun in the rack after spawning {weapon} '
                        f'(slot={gun}, plate ink={ink})')
        return rec
    rec['gun'] = gun

    # A SPAWNED GUN IS NOT BARE. It auto-fits whatever the backpack holds as
    # it appears, and the training range hands some of them out already kitted
    # -- collect_templates' bare_host records an sks arriving with a 6x, a
    # suppressor, an extended magazine and a cheek pad. Measured here too: a
    # fresh match, an emptied backpack, and the AKM still came with a red dot
    # and a compensator on it (20260804_210805).
    #
    # ONE strip pass, never two. The gun-losing gesture needs a slot that is
    # ALREADY empty, and after a spawn every slot the strip picks is genuinely
    # filled -- so the first pass is safe and the second is the bug. Nothing
    # below re-reads the tiles to decide whether to pull again.
    fitted = read_one(ac, det, slots, frame, gun, weapon)
    strip = ac.strip(gun, to=at_ground())
    rec['stripped'] = [s['src'][2] for s in strip.get('steps', ())
                       if s.get('src') and s.get('ok')]

    frame = ac.frame()
    ink = ac.plate_ink(gun, frame)
    rec['plate_after'] = ink
    if ink < PLATE_INK_MIN:
        ac.ensure_tab(False)
        rec['error'] = (f'the strip took the gun with it (plate ink {ink}) — '
                        f'pulled {rec["stripped"]}')
        return rec

    bare = read_one(ac, det, slots, frame, gun, weapon)
    # THE SAME TILE BEFORE AND AFTER IS THE WITNESS. Counting rows in 附近 was
    # the obvious choice and it is wrong here: panel_rows counts the visible
    # WINDOW, the floor is already 12 rows deep by the third weapon, and every
    # scan after that reported "nothing reached the floor" while the strips
    # were working (run 20260804_211135). control/CLAUDE.md names that limit.
    #
    # A tile's own MSE does not saturate and needs no absolute threshold: a
    # part that left takes its icon with it, so the score has to JUMP. The AKM
    # measured 29.8 fitted and 346.6 bare. Anything that does not move by
    # RISE_MIN is not a slot this scan knows anything about, and is dropped
    # rather than recorded as empty.
    for s in SLOT_NAMES:
        f_mse, b_mse = fitted[s]['mse'], bare[s]['mse']
        was = s in rec['stripped']
        bare[s]['fitted_mse'] = f_mse
        bare[s]['fitted_edges'] = fitted[s]['edges']
        bare[s]['pulled'] = was
        # `trusted` is the only field the report and the JSON read. Two ways
        # to earn it, and both are statements about what CHANGED:
        #   a part was pulled and the tile stopped matching it, or
        #   the tile was already inert before the strip and still is.
        rose = (was and f_mse is not None
                and (b_mse is None or b_mse >= f_mse * RISE_MIN))
        inert = (not was and f_mse is None and b_mse is None)
        bare[s]['trusted'] = bool(rose or inert)
    rec['slots'] = bare
    rec['ok'] = True
    ac.ensure_tab(False)

    # No labels: nothing here was confirmed by a human or by a requested fit.
    # What makes these tiles empty is HOW they were produced, and that is
    # `bare=True` in the facts, not a ground-truth label. See capture_run.
    run.add(frame, weapon, weapon=weapon, gun_slot=gun, bare=True,
            plate_ink=ink, slots=rec['slots'])
    return rec


def line(rec):
    if not rec['ok']:
        return f'{rec["weapon"]:<10} FAILED: {rec["error"]}'
    bits = []
    for s in SLOT_NAMES:
        v = rec['slots'][s]
        mse = 'inf' if v['mse'] is None else f'{v["mse"]:.0f}'
        flag = '!' if (v['tile'] == 'filled' or
                       (v['mse'] is not None and v['mse'] <= MSE_EMPTY_TH)) \
            else ' '
        bits.append(f'{s[:4]}{flag}e{v["edges"]:<4}m{mse:<6}')
    return f'{rec["weapon"]:<10} ' + ' '.join(bits)


def report(records):
    """Which weapons would read FILLED on an empty tile, and by which reader."""
    print(f'\n{"weapon":<10} {"slot":<9} {"edges":>6} {"mse":>8} '
          f'{"margin":>7}  reads')
    print('-' * 62)
    bad_edges, bad_mse, untrusted = [], [], []
    for r in records:
        if not r['ok']:
            print(f'{r["weapon"]:<10} -- {r["error"]}')
            continue
        for s in SLOT_NAMES:
            v = r['slots'][s]
            # A slot whose reading was not earned by a CHANGE is not evidence
            # of anything and is counted apart. Letting it through as "empty"
            # would put the scan's own guess into the table the gate is set
            # from -- the circularity this whole run exists to avoid.
            if not v.get('trusted'):
                untrusted.append((r['weapon'], s))
                continue
            by = []
            if v['tile'] == 'filled':
                by.append('EDGES')
                bad_edges.append((r['weapon'], s, v))
            if v['mse'] is not None and v['mse'] <= MSE_EMPTY_TH:
                by.append(f'TEMPLATE({v["hit"]})')
                bad_mse.append((r['weapon'], s, v))
            if not by:
                continue
            mse = 'inf' if v['mse'] is None else f'{v["mse"]:.1f}'
            marg = '-' if v['margin'] is None else f'{v["margin"]:.2f}'
            print(f'{r["weapon"]:<10} {s:<9} {v["edges"]:>6} {mse:>8} '
                  f'{marg:>7}  {" + ".join(by)}')

    print()
    print(f'edge reader (threshold {TAB_SLOT_FILLED_EDGES}): '
          f'{len(bad_edges)} confirmed-empty tile(s) read filled')
    print(f'template reader (MSE_EMPTY_TH {MSE_EMPTY_TH}): '
          f'{len(bad_mse)} confirmed-empty tile(s) named a part')
    if untrusted:
        print(f'{len(untrusted)} slot(s) proved nothing (no jump across the '
              f'strip) and are NOT counted either way')
    if bad_mse:
        worst = min(v['mse'] for _, _, v in bad_mse)
        print(f'  the closest empty tile scored {worst:.1f}. A positive-match '
              f'floor has to sit below that and above the fitted p99 of 89.2; '
              f'TAB_SLOT_MATCH_MAX is {TAB_SLOT_MATCH_MAX}'
              + ('  ** TOO HIGH **' if worst <= TAB_SLOT_MATCH_MAX else ''))
    return len(bad_edges), len(bad_mse)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', help='comma-separated weapon keys')
    ap.add_argument('--start-from')
    ap.add_argument('--report', metavar='RUN_DIR', help='offline re-print')
    ap.add_argument('--countdown', type=int, default=6)
    ap.add_argument('--backend', default='auto')
    ap.add_argument('--no-restart', dest='restart', action='store_false',
                    help='skip the leave-and-re-enter that clears the floor')
    args = ap.parse_args()

    if args.report:
        run = CaptureRun.load_dir(args.report)
        report([{'weapon': e['weapon'], 'ok': True, 'slots': e['slots']}
                for e in run.entries if e.get('slots')])
        return 0

    weapons = list(ROSTER)
    if args.only:
        want = [s.strip() for s in args.only.split(',') if s.strip()]
        dead = [w for w in want if w not in ROSTER]
        if dead:
            print(f'not in the roster: {", ".join(dead)}')
            return 1
        weapons = [w for w in weapons if w in want]
    if args.start_from:
        if args.start_from not in weapons:
            print(f'{args.start_from} not in the list')
            return 1
        weapons = weapons[weapons.index(args.start_from):]

    ready = ensure_ready(label='scan_bare_tiles', countdown_s=args.countdown)
    if not ready['ok']:
        print(f'not ready: {ready["failed"]}')
        return 1

    # A FRESH RANGE, because the floor does not clear itself. Every weapon
    # here is dropped on the ground with its attachments, and after a dozen
    # rounds the spawner stops delivering: the 4th run of this scan reported
    # "no gun in the rack" for every weapon in a row on a floor holding
    # fifteen of them. Leaving and re-entering is the only reset there is.
    #
    # Before sc/ac exist, not after: LobbyControl opens the same one serial
    # port they do, and ensure_ready closes each control it builds for that
    # reason.
    if args.restart:
        from control.lobby import LobbyControl
        with LobbyControl(args.backend) as lc:
            if not lc.exit_to_lobby()['ok']:
                print('could not get back to the lobby for a fresh range')
                return 1
            if not lc.ensure_in_match()['ok']:
                print('left the range and could not get back in')
                return 1

    from control.inventory import InventoryControl
    from control.spawner import SpawnerControl
    sc = SpawnerControl(args.backend)
    ac = InventoryControl(args.backend, verbose=False)
    if not (sc.can_press() and ac.can_press()):
        print('no Pico — the spawner panel and Tab are both opened by keypress')
        return 1
    det, slots = AttachmentDetector(), SlotDetector()

    run = CaptureRun.create(KIND, note=f'{len(weapons)} weapons, bare')
    print(f'scanning {len(weapons)} weapons -> {run.path}\n')

    records = []
    try:
        # SpawnerControl is a context manager and has no close(); entering it
        # here rather than wrapping the loop keeps the one exit path below.
        sc.__enter__()
        # A FULL BACKPACK IS WHY A "BARE" SPAWN ARRIVES WEARING THINGS: PUBG
        # auto-fits whatever fits out of the bag as the gun appears. Emptied
        # once here rather than per weapon -- clear_rack drops each gun with
        # its attachments, so nothing comes back.
        if ac.ensure_tab(True):
            ac.clear_inventory()
            ac.ensure_tab(False)
        t0 = time.perf_counter()
        for i, w in enumerate(weapons, 1):
            if not focus_keeper().ok(f'scan {w}'):
                print('lost the foreground and could not take it back')
                break
            el = time.perf_counter() - t0
            r = scan_one(sc, ac, det, slots, w, run)
            records.append(r)
            print(f'[{i:2d}/{len(weapons)} {el:5.0f}s] {line(r)}', flush=True)
    finally:
        run.save()
        sc.__exit__(None, None, None)
        ac.close()

    n_e, n_m = report(records)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump({'stamp': run.stamp,
                   'note': 'empty-tile scores per weapon; bare spawn',
                   'weapons': {r['weapon']: r['slots']
                               for r in records if r['ok']},
                   'failed': {r['weapon']: r['error']
                              for r in records if not r['ok']}},
                  f, indent=1, ensure_ascii=False)
    print(f'\n-> {os.path.relpath(OUT, ROOT)}   captures in {run.path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
