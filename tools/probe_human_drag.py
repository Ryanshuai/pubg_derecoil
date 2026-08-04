"""Record a HUMAN drag off the real mouse, to compare against press/pointer.py.

    pixi run python temp_debug/record_human_drag.py [--seconds 25]

Reads the cursor position and the left button at ~1 kHz and prints what the
gesture actually looked like. Touches nothing: no Pico, no clicks, no focus
grab — the game keeps the foreground the whole time, which it must, because
the gesture being measured is one a person performs in it.

WHY. Pointer.drag() crosses the screen in DRAG_STEPS=10 interpolated jumps.
Over the 1600 px from 库存 to 附近 that is ~160 px per step, one step every
16 ms. Nothing has ever compared that to a hand, and the failure it would
produce — the game losing the held item partway and the drop landing nowhere —
is exactly what `the drops are not landing` looks like from the outside.

Run it, then drag one item from 库存 to 附近 in the game the way you normally
would. Everything printed is measured from the OS cursor, so it is what the
game saw, not what any script intended.
"""
import argparse
import ctypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

user32 = ctypes.windll.user32
VK_LBUTTON = 0x01


class POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


def cursor():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def down():
    return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def summarise(samples, tag):
    """samples: [(t, x, y, down)] for one press..release."""
    t0 = samples[0][0]
    press_xy = samples[0][1:3]
    rel_xy = samples[-1][1:3]
    moves = []          # (dt_ms, dist) between consecutive DISTINCT positions
    last = samples[0]
    for s in samples[1:]:
        if (s[1], s[2]) != (last[1], last[2]):
            d = ((s[1] - last[1]) ** 2 + (s[2] - last[2]) ** 2) ** 0.5
            moves.append(((s[0] - last[0]) * 1000, d))
            last = s
    total = (samples[-1][0] - t0) * 1000
    span = ((rel_xy[0] - press_xy[0]) ** 2
            + (rel_xy[1] - press_xy[1]) ** 2) ** 0.5
    first_move = moves[0][0] if moves else float('nan')
    # time from the last position change to the release
    still = (samples[-1][0] - last[0]) * 1000
    steps = [d for _, d in moves]
    print(f'\n── {tag} ──')
    print(f'  press at            {press_xy}')
    print(f'  release at          {rel_xy}   ({span:.0f} px apart)')
    print(f'  button held         {total:.0f} ms')
    print(f'  press -> 1st move   {first_move:.0f} ms')
    print(f'  position updates    {len(moves)}')
    if steps:
        steps_sorted = sorted(steps)
        print(f'  px per update       min {min(steps):.0f}  median '
              f'{steps_sorted[len(steps)//2]:.0f}  max {max(steps):.0f}')
        gaps = sorted(dt for dt, _ in moves)
        print(f'  ms between updates  min {gaps[0]:.1f}  median '
              f'{gaps[len(gaps)//2]:.1f}  max {gaps[-1]:.1f}')
    print(f'  last move -> release {still:.0f} ms')
    return {'held_ms': total, 'updates': len(moves), 'span': span,
            'max_step': max(steps) if steps else 0,
            'first_move_ms': first_move, 'still_ms': still}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=25)
    args = ap.parse_args()

    from press.pointer import (DRAG_STEPS, DRAG_GRAB_WAIT, DRAG_STEP_WAIT,
                               DRAG_HOVER_WAIT, MOVE_WAIT)

    print(f'Recording for {args.seconds:.0f}s — go to the game and drag ONE '
          f'item from 库存 to 附近,\nthe way you normally would. Nothing here '
          f'touches the mouse.\n')
    end = time.perf_counter() + args.seconds
    samples, drags, was = [], [], False
    while time.perf_counter() < end:
        t = time.perf_counter()
        d = down()
        if d:
            x, y = cursor()
            samples.append((t, x, y, d))
        elif was and samples:
            drags.append(samples)
            samples = []
            print(f'  ...captured a drag ({len(drags)})')
        was = d
        time.sleep(0.001)
    if samples:
        drags.append(samples)

    drags = [d for d in drags if len(d) > 5 and
             (abs(d[-1][1] - d[0][1]) + abs(d[-1][2] - d[0][2])) > 40]
    if not drags:
        print('\nNo drag recorded. A click with no travel is filtered out; '
              'run again\nand hold the button while moving.')
        return 1
    for i, d in enumerate(drags, 1):
        summarise(d, f'human drag {i}')

    print(f'\n── what press/pointer.py does, for the same gesture ──')
    print(f'  press -> 1st move   {DRAG_GRAB_WAIT * 1000:.0f} ms')
    print(f'  position updates    {DRAG_STEPS}')
    print(f'  ms between updates  {DRAG_STEP_WAIT * 1000:.0f}')
    print(f'  last move -> release {DRAG_HOVER_WAIT * 1000:.0f} ms')
    print(f'  button held         '
          f'{(DRAG_GRAB_WAIT + DRAG_STEPS * DRAG_STEP_WAIT + DRAG_HOVER_WAIT) * 1000:.0f} ms')
    print(f'  settle before press {MOVE_WAIT * 1000:.0f} ms')
    print('\n  Over the 1600 px from 库存 to 附近 those 10 updates are ~160 px '
          'each.\n  Compare that with "px per update" above.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
