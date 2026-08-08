"""Layer 2: which attachments a present slot actually accepts.

scan_compat answers which slots a weapon has, off a screenshot. It cannot
answer this one — Tommy Gun's muzzle takes a suppressor and refuses a
compensator, and both leave an identical empty tile. The only way to know is
to fit the part and read the slot back.

    pixi run python calibration/scan_fits.py --only tommy --slots muzzle
    pixi run python calibration/scan_fits.py --only tommy,aug
    pixi run python calibration/scan_fits.py                    # everything
    pixi run python calibration/scan_fits.py --report <stamp>

Ground truth is what was asked for: `equip(gun, slot, att=<key>)` verifying
means that slot now holds <key>. Nothing is labelled by a detector, which is
also what makes these captures reusable as template samples — see
calibration/capture_run.py and the calibrate-template skill.

⚠ NOT WORKING YET — the equip call below is wrong. `equip(gun, slot=...,
att=...)` hits the shorthand branch at the top of InventoryControl.equip, which
does `src, slot = slot, None` and then treats 'muzzle' as a drag SOURCE:
every attempt fails with "source 'muzzle' is not a location". A first run of
tommy produced 27 attempts, 0 fits.

The fix is to locate the part in the backpack first and pass the Item:

    view = ac.look()                 # or whatever returns a TabView
    item = view.find(key)            # None if the spawn did not land
    if item: ac.equip(GUN_SLOT, item)

which also removes a silent failure mode this version has — if `give_attachment`
never put the part in the backpack, there is nothing to drag and "does not fit"
is indistinguishable from "was never there". Check `find()` before each drag and
record the difference.

MEASURED COST, from that run: 3.3 s per attempt (89 s for 27), on the failure
path with retries, so treat it as an upper bound. 715 drags ≈ 40 min for the
full sweep, 319 ≈ 18 min for predicted-fits only.

TWO THINGS MAKE THIS CHEAPER THAN IT LOOKS:

Replacement counts as proof. Dropping a second part onto an occupied slot
replaces it, so there is no unequip between candidates — N parts cost N drags,
not 2N.

Parts persist in the backpack. Spawn each attachment once for the whole run
and every later weapon reuses it, so spawning is ~41 operations total rather
than one per pair.
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

from calibration.capture_run import CaptureRun
from detector.attachment_catalog import ATTACHMENTS, ROSTER, SLOTS, fits
from detector.slot_detector import SlotDetector
from control.focus import focus_keeper
from control.session import ensure_ready
from tools.drive_screen import SCREENS

KIND = 'fit_scan'
GUN_SLOT = 2

# scope is excluded by default: SlotDetector cannot confirm the slot exists,
# so a failed fit there is ambiguous in a way it is not elsewhere.
DEFAULT_SLOTS = ('muzzle', 'grip', 'magazine', 'stock')


def candidates(weapon, slots):
    """Every attachment worth trying on this weapon. -> [(slot, key)]

    Everything of the right slot is tried, not just what fits() predicts —
    predicting is what is being tested. `expect` records the prediction so the
    report can separate "confirmed" from "the table was wrong".
    """
    have = set(SLOTS.get(weapon, {}).get('slots', ()))
    out = []
    for key, meta in ATTACHMENTS.items():
        slot = meta.get('slot')
        if slot not in slots or slot not in have:
            continue
        out.append((slot, key, bool(fits(weapon, key))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', default=None, help='comma-separated weapons')
    ap.add_argument('--slots', default=','.join(DEFAULT_SLOTS))
    ap.add_argument('--predicted-only', action='store_true',
                    help='skip pairs fits() already rejects (527 not 967) — '
                         'faster, but cannot find slots the table under-lists')
    ap.add_argument('--report', default=None)
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    slots = tuple(s.strip() for s in args.slots.split(',') if s.strip())

    if args.report:
        run = CaptureRun.load(args.report, KIND)
        print(run)
        wrong = [e for e in run.entries if e.get('expected') != e.get('fitted')]
        print(f'{len(run.entries)} attempts, {len(run.labelled())} confirmed '
              f'fits, {len(wrong)} disagreements with the catalogue')
        for e in wrong:
            lab = e['labels'][0] if e['labels'] else {}
            print(f"  {e['weapon']:10} {e.get('slot',''):9} "
                  f"{e.get('asset',''):16} table said "
                  f"{'FITS' if e.get('expected') else 'no'}, game said "
                  f"{'FITS' if e.get('fitted') else 'no'}")
        return 0

    weapons = list(ROSTER)
    if args.only:
        want = [s.strip() for s in args.only.split(',') if s.strip()]
        weapons = [w for w in weapons if w in want]

    plan = [(w, c) for w in weapons for c in candidates(w, slots)]
    if args.predicted_only:
        plan = [(w, c) for w, c in plan if c[2]]
    if not plan:
        print('nothing to try')
        return 1
    print(f'{len(weapons)} weapons, {len(plan)} drags planned')

    # Was focus + a match, open-coded — two of the five legs, and this script
    # opens and closes BOTH of the screens the other three are about. It is
    # the least likely of the four to be hurt by the ones it was missing (it
    # drags rather than fires, so the spawn compound cannot ruin a magazine
    # it never shoots), and it goes through the gate anyway: the argument for
    # one door is that nobody has to decide which legs their script needs.
    #
    # ⚠ THE NOTE HERE WAS ABOUT `args.backend` NOT BEING FORWARDED to
    # ensure_ready, and the parameter it worried about is gone (2026-08-08,
    # with the SendInput backend). What it said about the real requirement
    # still holds and is now the whole story: the Pico requirement is
    # checked directly below by can_press().
    rec = ensure_ready(label='scan_fits', countdown_s=args.countdown)
    if not rec['ok']:
        print(f"not ready: failed at {rec.get('failed')!r}")
        return 1

    from control.inventory import InventoryControl
    from control.spawner import SpawnerControl
    sc = SpawnerControl()
    ac = InventoryControl()
    if not (sc.can_press() and ac.can_press()):
        print('no Pico — the spawner panel and Tab are both opened by keypress')
        return 1
    det = SlotDetector()
    # Only the shot is still taken through drive_screen -- it parks the cursor
    # first, and a hovered row draws a highlight that moves the measured
    # bounds. Opening and closing both panels is the drivers' job now.
    tab = SCREENS['tab']

    run = CaptureRun.create(KIND, note=f'{len(plan)} drags over '
                                       f'{len(weapons)} weapons')
    print(f'-> {run.path}\n')

    t0 = time.perf_counter()
    stocked = set()
    n_ok = n_diff = 0

    for weapon in weapons:
        cands = [c for c in candidates(weapon, slots)
                 if not args.predicted_only or c[2]]
        if not cands:
            continue
        if not focus_keeper().ok(f'fits {weapon}'):
            print('lost the foreground — stopping')
            break

        # One spawner session per weapon: the gun, plus any part not yet in
        # the backpack. Parts persist, so this shrinks to nothing after the
        # first few weapons.
        need = [k for _, k, _ in cands if k not in stocked]
        if not sc.ensure_panel(True):
            print(f'{weapon}: spawner would not open')
            continue
        sc.give_weapon(weapon)
        for k in need:
            if sc.give_attachment(k).get('ok'):
                stocked.add(k)
        # Tab and the spawner panel cannot share the screen.
        sc.ensure_panel(False)

        if not ac.ensure_tab(True):
            print(f'{weapon}: tab would not open')
            continue
        ac.sync()

        for slot, key, expect in cands:
            r = ac.equip(GUN_SLOT, slot=slot, att=key, weapon=weapon)
            got = bool(r.get('ok') and r.get('verified'))
            frame = tab.shoot()
            name = f'{weapon}_{slot}_{key}'
            if got:
                run.add_fit(frame, name, weapon, slot, key,
                            slot_field=slot, asset=key, expected=expect,
                            fitted=True)
                n_ok += 1
            else:
                run.add(frame, name, weapon=weapon, slot=slot, asset=key,
                        expected=expect, fitted=False,
                        error=r.get('error'), labels=[])
            if got != expect:
                n_diff += 1
                print(f'  {weapon:10} {slot:9} {key:16} table '
                      f'{"FITS" if expect else "no":4} game '
                      f'{"FITS" if got else "no":4}   <<<')
        ac.ensure_tab(False)

        el = time.perf_counter() - t0
        done = len(run.entries)
        print(f'{weapon:10} {len(cands):3d} drags  ({done}/{len(plan)}, '
              f'{el:5.0f}s, {el / max(done, 1):.1f}s/drag)')

    el = time.perf_counter() - t0
    print(f'\n{len(run.entries)} drags in {el:.0f}s '
          f'({el / max(len(run.entries), 1):.1f}s each), '
          f'{n_ok} fitted, {n_diff} disagree with the catalogue')
    print(f'run: {run.path}   ground-truth samples: {len(run.labelled())}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
