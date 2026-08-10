"""Run ONE 库存 -> 附近 drag and photograph both ends of it.

    pixi run python tools/show_drag.py

The gesture journal records numbers; this records the SCREEN. It exists
because "the release only just crosses the line" is an observation about
pixels, and the journal's `got.release` is a pair of integers that cannot be
held up against the divider it has to cross.

What it draws, on both the before and the after shot:

    the two panel x-ranges and the dead gap between them (880..907)
    the grab point, the release point, and the travel between them
    NEARBY_DROP_X against the 附近 edge it is 10 px inside of

Nothing here is a measurement. It is the same drag clear_inventory performs,
photographed. Read the landing off the journal, not off the arrow.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2

from capture.cropper import capture_screen
from control.focus import focus_keeper
from control.inventory import InventoryControl, NEARBY_DROP_X
from control.locations import at_ground, at_inv
from control.session import ensure_ready
from detector.tab_layout import INV_ROWS, PANELS, row_point

OUT = ROOT / 'calibration' / 'artifacts' / 'drag' / 'shots'

# The crop the shots are saved at. Full frame is 3440x1440 and the whole
# subject is two columns near the left, so a full-width shot is mostly the
# character model. x0..x1 spans 附近's left edge to past 库存's right one.
VIEW = (500, 120, 1320, 700)

# The shipped NEARBY_DROP_X, drawn alongside whatever this run is using so
# the two can be compared on one screen.
BASELINE_X = 870


def annotate(frame, grab, release, caption):
    """Panel bounds, the divider, and this drag's endpoints, on a crop."""
    x0, y0, x1, y1 = VIEW
    img = frame[y0:y1, x0:x1].copy()

    def px(p):
        return int(p[0]) - x0, int(p[1]) - y0

    red, grey, green, yellow = ((60, 60, 255), (170, 170, 170),
                                (80, 255, 80), (0, 220, 255))
    h = img.shape[0]
    for x, colour, label in ((PANELS['nearby'][0], grey, 'nearby 565'),
                             (PANELS['nearby'][1], red, 'nearby ends 880'),
                             (PANELS['inventory'][0], red, 'inv starts 907'),
                             (PANELS['inventory'][1], grey, 'inv 1236')):
        cv2.line(img, (x - x0, 0), (x - x0, h), colour, 2)
        cv2.putText(img, label, (x - x0 - 55, h - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1, cv2.LINE_AA)

    # The dead gap: past 附近, not yet 库存. Releasing here is the floor.
    overlay = img.copy()
    cv2.rectangle(overlay, (PANELS['nearby'][1] - x0, 0),
                  (PANELS['inventory'][0] - x0, h), (40, 40, 120), -1)
    cv2.addWeighted(overlay, 0.35, img, 0.65, 0, img)

    cv2.arrowedLine(img, px(grab), px(release), yellow, 2, tipLength=0.06)
    cv2.circle(img, px(grab), 7, yellow, -1)
    cv2.circle(img, px(release), 8, green, -1)
    cv2.putText(img, f'release x={release[0]}  ({880 - release[0]} px inside)',
                (px(release)[0] - 250, px(release)[1] - 18),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, green, 2, cv2.LINE_AA)

    base = (BASELINE_X - x0, px(release)[1])
    cv2.drawMarker(img, base, (255, 180, 60), cv2.MARKER_TILTED_CROSS, 16, 2)
    cv2.putText(img, f'shipped {BASELINE_X}', (base[0] - 30, base[1] + 32),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 180, 60), 1, cv2.LINE_AA)

    cv2.putText(img, caption, (10, 26), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (255, 255, 255), 2, cv2.LINE_AA)
    return img


def burst(ac, n, label):
    """Drag n times back to back, reading NOTHING in between, then count once.

    This is the shape the real caller has: clear_inventory fires twelve of
    these with nothing separating them. Reading back between drags is not a
    neutral observation here -- a look() is ~123 ms with the cursor parked, and
    the repo has already been fooled once by a sweep that did exactly that and
    concluded DROP_WAIT could go to 0.10 ("measuring a gesture requires that
    the measurement loop itself provide no gap, or what comes out describes
    the method rather than the game").
    """
    view = ac.look()
    if view.rows('nearby'):
        view = refill(ac)
    start_near = view.rows('nearby')
    sent = 0
    for _ in range(n):
        if not view.rows('inventory'):
            break
        ac.drag(at_inv(0), at_ground(), retries=0, verify=False)
        sent += 1
    view = ac.look()
    landed = view.rows('nearby') - start_near
    print(f'{label:>22}: sent {sent}, landed {landed}  '
          f'({100 * landed / sent:.0f}%)' if sent else f'{label}: nothing sent')
    return landed, sent


def fastest_human():
    """The quickest recorded hand drag, by px per ms.

    Quickest rather than median because the operator asked for the fast one,
    and because a slow hand drag and this repo's gesture already agree on
    everything except speed -- if a fast hand lands and the interpolated
    gesture does not, speed is not the difference either.
    """
    recs = []
    for f in sorted((ROOT / 'calibration' / 'artifacts' / 'drag'
                     / 'human').glob('*.jsonl')):
        with open(f, encoding='utf-8') as fh:
            recs += [json.loads(line) for line in fh if line.strip()]
    if not recs:
        raise SystemExit('no recordings — run tools/record_drag.py first')
    return max(recs, key=lambda r: r['travel_px'] / max(r['held_ms'], 1e-9))


def refill(ac):
    """Pull everything on the floor back into the pack, so the drain can run
    again. Uses DROP_XY['inventory'], the edge the journal has at 224/225."""
    view = ac.look()
    while view.rows('nearby'):
        # Bounded by the world (the floor empties) AND by a human taking the
        # foreground back — a loop that holds the cursor needs both, or it is
        # the one that sat on the mouse for eight minutes on 2026-08-08.
        if not focus_keeper().ok('refill'):
            print('lost the foreground during refill')
            break
        before = view.rows('nearby')
        ac.drag(at_ground(0), at_inv())
        view = ac.look()
        if view.rows('nearby') >= before:
            print(f'refill stalled at {before} rows on the floor')
            break
    print(f'refilled: inventory {view.rows("inventory")} rows, '
          f'nearby {view.rows("nearby")} rows')
    return view


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    # Both of these are OVERRIDES ON A PROBE, not new defaults. The operator
    # asked for a release a fifth further left and a faster gesture; wiring
    # them here keeps the constants honest until the numbers say otherwise.
    # Default: override NOTHING, so a bare run exercises the shipped path.
    # "849" pins one release point; "870,849" interleaves the two as arms;
    # "human" replays the fastest recorded hand drag, release point included.
    arg_x = sys.argv[2] if len(sys.argv) > 2 else None
    arms, human, release_x = [], None, None
    if arg_x == 'human':
        human = fastest_human()
        release_x = human['release'][0]
    elif arg_x and ',' in arg_x:
        arms = [int(v) for v in arg_x.split(',')]
        release_x = arms[0]
    elif arg_x:
        release_x = int(arg_x)
    step_wait = float(sys.argv[3]) if len(sys.argv) > 3 else None

    # Focus is not playability: the window title matches in the lobby, on the
    # loading screen and in the ESC menu, all of which swallow input while
    # every driver reports success. Tab is left to tab_up() below.
    #
    # ⚠ range_name=None BECAUSE THIS SCRIPT'S SUBJECT IS ON THE FLOOR. The
    # teleport leg moved the character to the 200m lane mid-session on
    # 2026-08-09 and every item dropped in the previous run stayed behind, so
    # 附近 read 0 rows and the run looked like a total drag failure. Anything
    # measuring the 附近 panel has to stand still.
    if not ensure_ready('show_drag', tab=True, panel=False,
                        range_name=None)['ok']:
        print('not ready to drive')
        return 1

    import control.inventory as inventory_mod
    import press.pointer as pointer_mod
    if release_x is None:
        release_x = inventory_mod.NEARBY_DROP_X
    else:
        inventory_mod.NEARBY_DROP_X = release_x
    if step_wait is not None:
        pointer_mod.DRAG_STEP_WAIT = step_wait
    globals()['NEARBY_DROP_X'] = release_x
    print(f'release x = {release_x} ({880 - release_x} px inside the floor '
          f'panel), step wait = '
          f'{pointer_mod.DRAG_STEP_WAIT * 1000:.0f} ms, '
          f'path = {"replay" if pointer_mod.HUMAN_DRAG_PATH else "none"}')

    ac = InventoryControl()
    if human:
        ac.timing['path'] = human['path']
        print(f'replaying a HAND drag: {human["grab"]} -> {human["release"]}, '
              f'{human["travel_px"]:.0f} px in {human["held_ms"]:.0f} ms '
              f'({human["travel_px"] / human["held_ms"]:.2f} px/ms), '
              f'{human["updates"]} updates, step med {human["step_med"]}, '
              f'gap med {human["gap_med_ms"]} ms')
    stamp = time.strftime('%Y%m%d_%H%M%S')
    out = OUT / stamp
    out.mkdir(parents=True, exist_ok=True)

    with ac.tab_up():
        view = ac.look()
        # n=0 is "just put the floor back in the pack", which is how a run
        # that ended with a full floor gets back to a measurable state.
        if n == 0 or not view.rows('inventory'):
            view = refill(ac)
            if n == 0:
                return 0

        # REFUSE rather than measure. Both lists at the 12-row display cap
        # means neither count can move, so every drag reads as "did not land"
        # whatever the game did -- control/CLAUDE.md has this as a known blind
        # spot of the row criterion, and a run made in it produces a number
        # that describes the criterion instead of the gesture.
        if (view.rows('inventory') >= INV_ROWS
                and view.rows('nearby') >= INV_ROWS):
            print(f'both lists are at the {INV_ROWS}-row cap — the row count '
                  f'cannot see a landing. Clear the floor first; refusing to '
                  f'produce a number that would only describe the criterion.')
            return 1

        print(f'start: inventory {view.rows("inventory")} rows, '
              f'nearby {view.rows("nearby")} rows')
        print(f'{"#":>3} {"inv":>4} {"near":>5}  {"released":>12} '
              f'{"ok":>5} {"moved":>6}  landed')

        tally = {}
        landed = stuck = 0
        for i in range(n):
            inv, near = view.rows('inventory'), view.rows('nearby')
            if not inv:
                if arms:
                    view = refill(ac)
                    inv, near = view.rows('inventory'), view.rows('nearby')
                if not inv:
                    print('the pack is empty')
                    break

            # INTERLEAVED, not one arm then the other. Arms alternate shot by
            # shot so anything that drifts over a round -- pack contents, the
            # world behind the translucent panel, whatever the unexplained ~20%
            # is -- lands on both arms equally instead of on whichever ran last.
            if arms:
                inventory_mod.NEARBY_DROP_X = release_x = arms[i % len(arms)]

            grab = row_point(0, 'inventory')
            frame = capture_screen()
            rec = ac.drag(at_inv(0), at_ground())
            got = tuple((rec.get('got') or {}).get('release')
                        or (NEARBY_DROP_X, grab[1]))

            view = ac.look()
            inv2, near2 = view.rows('inventory'), view.rows('nearby')
            # 库存 caps its display at INV_ROWS, so a full pack does not
            # shrink; 附近 growing is the reading that survives that.
            hit = near2 > near or inv2 < inv
            landed += hit
            stuck += not hit
            arm = tally.setdefault(release_x, [0, 0])
            arm[0] += hit
            arm[1] += 1
            print(f'{i:>3} {inv:>2}->{inv2:<2} {near:>2}->{near2:<2} '
                  f'{str(got):>12} {str(rec.get("ok")):>5} '
                  f'{str(rec.get("moved")):>6}  {"yes" if hit else "NO"}')

            cv2.imwrite(str(out / f'{i:02d}_{"ok" if hit else "miss"}.png'),
                        annotate(frame, grab, got,
                                 f'#{i}  inv {inv} -> {inv2}   '
                                 f'nearby {near} -> {near2}'))

        print(f'\nlanded {landed}, did not {stuck}')
        for x, (hit, tot) in sorted(tally.items()):
            print(f'  release x={x}: {hit}/{tot}'
                  + (f'  ({100 * hit / tot:.0f}%)' if tot else ''))
        print(f'shots -> {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
