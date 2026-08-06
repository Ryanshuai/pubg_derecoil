"""Why does the SECOND drag land somewhere the first one did not?

`Pointer.drag` places the cursor with SetCursorPos and then reads it back, and
in run 20260805_010546 the read-back disagreed with what was just set:

    [pointer] drag released at (946, 186), not (870, 199)
    [pointer] drag aborted before press: cursor at (1011, 190), wanted (974, 199)

Something moves the cursor after SetCursorPos returns. There are three
candidates and they need different fixes, so this separates them rather than
guessing:

  A  the cursor drifts on its own          -> the Pico's passthrough is
     with nothing sent at all                 forwarding a real mouse, or the
                                              game re-places the cursor itself

  B  it drifts only once the Pico speaks   -> the click report carries dx/dy,
     (the drag's DRAG_REARM_S top-ups)        and re-arming a held button
                                              flushes accumulated motion

  C  it drifts only across a whole drag    -> the game does it at drop time,
     and only the second one onwards          e.g. snapping to its own UI cursor

Read-only apart from moving the cursor and, in stage B, holding a button down
over a harmless spot. Nothing is dragged and no item is touched.

    pixi run python tools/probe_drag_cursor.py            # stages A and B
    pixi run python tools/probe_drag_cursor.py --live     # plus C, needs Tab
"""
import argparse
import ctypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from press.pointer import (DRAG_HOLD_MS, DRAG_REARM_S, MOVE_WAIT, Pointer,
                           cursor_pos, move_cursor)


pos = cursor_pos      # 本文件的旧名字，实现在 press.pointer


# Where the probe parks. Inside the Tab panel's 库存 column, which is where the
# real drags start, so any game-side cursor handling that is specific to that
# region is in play.
PARK = (974, 199)
WAITS = (0.0, 0.02, MOVE_WAIT, 0.25, 0.5, 1.0)


def settle_curve(label, before=None, pico=None):
    """SetCursorPos, then watch the cursor for a second. -> [(t, dx, dy), ...]

    `before` runs after the placement and before the watching, which is where
    stage B injects the Pico report.

    `pico` adds the firmware's own count of REAL mouse movement over the same
    window. That is what separates "a hand or a passthrough is moving it" from
    "something in software is": the cursor drifting while the human totals sit
    still means no mouse moved and the motion was synthesised.
    """
    move_cursor(PARK)
    h0 = pico.human_totals() if pico is not None else None
    if before is not None:
        before()
    out, t0 = [], time.perf_counter()
    for w in WAITS:
        while time.perf_counter() - t0 < w:
            time.sleep(0.002)
        x, y = pos()
        out.append((time.perf_counter() - t0, x - PARK[0], y - PARK[1]))
    drift = out[-1][1], out[-1][2]
    human = ''
    if pico is not None:
        h1 = pico.human_totals()
        human = f'   human {(h1[0] - h0[0], h1[1] - h0[1])}'
    print(f'  {label:<26}' + ' '.join(f'{t*1000:4.0f}ms:{dx:+5d},{dy:+4d}'
                                      for t, dx, dy in out)
          + ('   STILL' if drift == (0, 0) else f'   DRIFTED {drift}') + human)
    return out


def stage_a(reps, pico=None):
    print('\nA — SetCursorPos alone, nothing sent to the Pico')
    for i in range(reps):
        settle_curve(f'place #{i + 1}', pico=pico)


def stage_b(pointer, reps):
    if pointer.pico is None:
        print('\nB — skipped, no Pico behind this Pointer')
        return
    print(f'\nB — same, but one click report ({DRAG_HOLD_MS} ms hold) right '
          f'after placing')
    for i in range(reps):
        settle_curve(f'place + click #{i + 1}',
                     before=lambda: pointer.pico.click(0x01, DRAG_HOLD_MS))
        time.sleep(0.4)                      # let the hold expire before the next
    print(f'\nB2 — a held button re-armed every {DRAG_REARM_S}s, as drag() does')
    for i in range(reps):
        def rearm():
            for _ in range(3):
                pointer.pico.click(0x01, DRAG_HOLD_MS)
                time.sleep(DRAG_REARM_S)
            pointer.pico.click(0x01, 0)       # release, as _release does
        settle_curve(f'place + 3 re-arms #{i + 1}', before=rearm)
        time.sleep(0.4)


def stage_c(pointer, reps):
    """Full drags back to back, reading the cursor between them.

    The gesture is 库存 row 0 -> the same row, so nothing moves: the question
    is only what the cursor does across a press/travel/release cycle, and a
    real transfer would change the list underneath and confound the next rep.
    """
    print('\nC — back-to-back drags that move nothing (src == dst)')
    for i in range(reps):
        before = pos()
        ok = pointer.drag(PARK, PARK)
        after = pos()
        print(f'  drag #{i + 1}  ok={ok!s:<5} cursor {before} -> {after}'
              + ('' if after == PARK else f'   OFF BY '
                 f'{(after[0] - PARK[0], after[1] - PARK[1])}'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reps', type=int, default=4)
    ap.add_argument('--live', action='store_true',
                    help='also run stage C, which presses the mouse button')
    args = ap.parse_args()

    # PARK is a 库存 row, so the panel has to be up for any of this to describe
    # the situation the real drags are in. With Tab shut the cursor belongs to
    # the view and SetCursorPos is overridden every frame by something that has
    # nothing to do with the bug.
    #
    # ensure_ready first, not ensure_focus: the lobby, the loading screen and
    # the ESC menu all match the window title and all swallow input, and a
    # probe that reads a cursor there measures nothing. It leaves Tab DOWN,
    # which is what tab_up() below then opens.
    from control.session import ensure_ready
    from control.inventory import InventoryControl
    rec = ensure_ready(label='drag cursor probe', countdown_s=4)
    if not rec['ok']:
        raise SystemExit(f'not ready to drive the game: {rec["failed"]}')
    ac = InventoryControl(verbose=False)
    # ONE Pointer for the process: a second would find the CDC port held by the
    # first and either fail or silently fall back to SendInput.
    with ac.tab_up() as up:
        if not up:
            print('[!] the inventory would not open — readings below are not '
                  'about the drag path')
        p = ac.pointer
        print(f'cursor starts at {pos()}, parking at {PARK}, '
              f'human reporting {"on" if p.pico and p.pico.human_available() else "off"}')
        stage_a(args.reps, pico=p.pico)
        stage_b(p, args.reps)
        if args.live:
            stage_c(p, args.reps)
    print('\nA drifting means nothing this process sends is responsible.\n'
          'B drifting and A still means the click report carries motion.\n'
          'C drifting with both still means the game moves it at drop time.')


if __name__ == '__main__':
    main()
