"""Does a part drag straight from one gun's slot to the other's? Occupies the game.

    pixi run python tools/probe_transfer.py --slot muzzle
    pixi run python tools/probe_transfer.py --slot grip --repeat 4

MOVES[('weapon', 'weapon')] is the one entry marked `evidence: 'untested'`.
The module docstring has advertised slot-to-slot drags since it was written
and nothing has ever measured one, so InventoryControl.transfer() does NOT
use that route — it goes out through the backpack, where every gesture is
measured. This is the probe that would let it stop.

WHY IT IS IN DOUBT RATHER THAN ASSUMED FINE: a drag INTO a weapon slot *from
库存* is measured at 0/4 — it does not land at all — while right-click is 4/4
(docs/game_quirks.md). If that failure is about the DROP TARGET, the direct
slot-to-slot drag fails the same way. If it is about the source being a list
row, the direct route may be fine. Nobody has separated the two.

WHAT IT REPORTS, and why the second column matters as much as the first:

    landed      the target slot reads the part afterwards
    vacated     the source slot is empty afterwards

They can disagree, and the disagreement is the finding. `landed` alone would
call a swap a success; a part that left the source and did NOT arrive is on
the floor, and that is the failure mode the 0/4 number is about.

Takes the game window and the Pico. It does NOT restore: each successful pass
alternates direction, so an even number of them leaves the part on the gun it
started on and an odd number leaves it on the other. A pass that half-worked
leaves it in 库存 or on the floor — which is the outcome being measured, so
cleaning up automatically would hide it. Look at the rack afterwards.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from control.focus import ensure_focus, focus_keeper
from control.inventory import InventoryControl, at_slot, move_info
from detector.attachment_detector import SLOT_NAMES


def one_pass(ac, src_gun, dst_gun, slot, retries=0):
    """One direct drag, measured both ways. -> record"""
    before = ac.read_slots()
    part = before[src_gun][slot]
    if not part:
        return {'ok': False, 'error': f'gun{src_gun} has nothing in {slot}'}
    if before[dst_gun][slot]:
        # A swap has a different outcome and a different failure mode. Keep
        # this probe to the one question it can answer cleanly.
        return {'ok': False,
                'error': f'gun{dst_gun}.{slot} is occupied ({before[dst_gun][slot]}) '
                         f'— this measures an empty target only'}

    t0 = time.perf_counter()
    rec = ac.drag(at_slot(src_gun, slot), at_slot(dst_gun, slot),
                  retries=retries)
    dt = time.perf_counter() - t0

    after = ac.read_slots()
    landed = after[dst_gun][slot] == part
    vacated = not after[src_gun][slot]
    return {'ok': landed and vacated, 'part': part, 'seconds': round(dt, 3),
            'landed': landed, 'vacated': vacated,
            'src_after': after[src_gun][slot], 'dst_after': after[dst_gun][slot],
            'drag_said': rec['ok'], 'error': rec['error']}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--slot', default='muzzle', choices=SLOT_NAMES)
    ap.add_argument('--from-gun', type=int, default=1, choices=(1, 2))
    ap.add_argument('--to-gun', type=int, default=2, choices=(1, 2))
    ap.add_argument('--repeat', type=int, default=4,
                    help='0/4 vs 4/4 is the shape of the answer, so more than '
                         'one pass is the point')
    ap.add_argument('--backend', default='auto')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    if args.from_gun == args.to_gun:
        ap.error('--from-gun and --to-gun must differ')

    print(f'MOVES currently says: {move_info(at_slot(1, "x"), at_slot(2, "x"))}')
    print(f'>>> This takes the game window and the Pico. Stand in the range '
          f'with both rack slots holding a gun, and {args.slot} fitted on '
          f'gun{args.from_gun} only.\n')

    if not ensure_focus(countdown_s=args.countdown, label='probe_transfer'):
        print('[!] could not focus the game')
        return 1
    time.sleep(0.6)

    ac = InventoryControl(args.backend)
    try:
        if not ac.can_press():
            print('[!] no Pico: Tab is a keypress')
            return 1
        with ac.tab_up() as up:
            if not up:
                print('[!] the Tab screen would not open')
                return 1
            a, b = args.from_gun, args.to_gun
            rows = []
            for i in range(args.repeat):
                if not focus_keeper().ok(f'pass {i + 1}'):
                    print('[!] lost the foreground — stopping')
                    break
                rec = one_pass(ac, a, b, args.slot)
                rows.append(rec)
                print(f'  pass {i + 1}: {rec}')
                if rec.get('error') and 'ok' in rec and not rec.get('part'):
                    break
                # Put it back for the next pass, using the route that IS
                # measured — otherwise a working direct drag would be the only
                # thing that could set up its own next trial.
                if rec.get('landed'):
                    a, b = b, a

            landed = sum(1 for r in rows if r.get('landed'))
            vacated = sum(1 for r in rows if r.get('vacated'))
            n = len(rows)
            print(f'\nlanded {landed}/{n}   vacated {vacated}/{n}')
            if n and landed == n and vacated == n:
                print(f"\nDirect slot-to-slot drag WORKS ({n}/{n}). Change\n"
                      f"  MOVES[('weapon','weapon')]['evidence'] -> 'measured'\n"
                      f"with these numbers in the note, then let transfer() "
                      f"take it: it halves the gestures.")
            elif n:
                print(f"\nDirect slot-to-slot drag is NOT reliable "
                      f"({landed}/{n} landed). Leave MOVES as 'untested' — or "
                      f"mark it as measured-and-broken — and leave transfer() "
                      f"going through the backpack. Record the numbers in "
                      f"docs/game_quirks.md either way: a measured NO is worth "
                      f"as much here as a measured yes.")
    finally:
        ac.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
