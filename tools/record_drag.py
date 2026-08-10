"""Record a HUMAN drag — cursor path and button state, sampled at 1 kHz.

    pixi run python tools/record_drag.py [n_drags] [timeout_s]

Run it, then drag things in the Tab screen by hand. It stops after n_drags
press-release cycles that actually moved, and writes one JSON line per drag
with the whole path in it.

WHY THIS EXISTS AGAIN. The repo has three numbers from a recorder like this —
18-25 px median per update, 51 max, 7.7 ms apart — under a note saying "that
recording script is no longer on disk, those three numbers are all it left".
Those three fixed DRAG_STEP_PX and nothing else, because a median cannot
answer the question that is actually open.

WHY THE PATH AND NOT JUST THE SPACING. The gesture this repo sends travels by
SetCursorPos, which does NOT go through the Pico, so the game's raw input sees
a press and a release with nothing in between (press/firmware/src/main.c:
send_hid_output returns early when nothing moved). A hand goes through the
mouse, so raw input sees every millimetre. If the game decides "was that a
drag" from raw input rather than from the cursor, the two gestures are not the
same gesture and no release coordinate will make them one — DRAG_NUDGE_COUNTS
exists because someone suspected this and left it at 0 for want of evidence.

IT DRIVES NOTHING. Pure sampling: GetCursorPos and GetAsyncKeyState, no
focus grab, no Pico. Safe to run while a human is using the machine.
"""
import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from detector.tab_layout import PANELS

OUT = ROOT / 'calibration' / 'artifacts' / 'drag' / 'human'

SAMPLE_HZ = 1000
VK_LBUTTON = 0x01

# A press-release cycle that moved less than this is a CLICK, not a drag, and
# recording it as one would put a zero-length path in the corpus that every
# later statistic then averages over.
MIN_TRAVEL_PX = 40

user32 = ctypes.windll.user32


class POINT(ctypes.Structure):
    _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]


def cursor():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def held():
    # GetAsyncKeyState's high bit is "down right now". The low bit is "was
    # pressed since last call" and is a trap here: it would report a press
    # that already ended inside one sample interval.
    return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def panel_of(x):
    for name, (x0, x1, _) in PANELS.items():
        if x0 <= x <= x1:
            return name
    return 'outside'


def summarise(path):
    """Per-update steps and gaps. Samples are 1 kHz; the MOUSE reports far
    slower, so consecutive identical samples are not updates — the deltas that
    matter are the ones where the cursor actually changed."""
    steps, gaps = [], []
    last_t, last_xy = path[0][0], (path[0][1], path[0][2])
    for t, x, y in path[1:]:
        if (x, y) == last_xy:
            continue
        steps.append(((x - last_xy[0]) ** 2 + (y - last_xy[1]) ** 2) ** 0.5)
        gaps.append((t - last_t) * 1000)
        last_t, last_xy = t, (x, y)
    steps.sort()
    gaps.sort()

    def med(v):
        return round(v[len(v) // 2], 2) if v else None

    return {'updates': len(steps),
            'step_med': med(steps), 'step_max': round(max(steps), 2) if steps else None,
            'gap_med_ms': med(gaps)}


def main():
    n_drags = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0

    OUT.mkdir(parents=True, exist_ok=True)
    path_out = OUT / f'{time.strftime("%Y%m%d_%H%M%S")}.jsonl'

    print(f'recording up to {n_drags} drags, {timeout:.0f}s limit')
    print('drag things by hand now — press and release the LEFT button')
    print(f'-> {path_out}')

    period = 1.0 / SAMPLE_HZ
    t0 = time.perf_counter()
    recorded, path, was_down = 0, None, held()
    if was_down:
        print('left button is already down — release it first')

    with open(path_out, 'a', encoding='utf-8') as fh:
        while recorded < n_drags and time.perf_counter() - t0 < timeout:
            t = time.perf_counter()
            down = held()
            x, y = cursor()

            if down and not was_down:
                path = [(t - t0, x, y)]
            elif down and path is not None:
                path.append((t - t0, x, y))
            elif was_down and not down:
                path.append((t - t0, x, y))
                sx, sy = path[0][1], path[0][2]
                travel = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
                if travel < MIN_TRAVEL_PX:
                    print(f'  (click at ({sx},{sy}), {travel:.0f} px — not a drag)')
                else:
                    recorded += 1
                    rec = {'grab': [sx, sy], 'release': [x, y],
                           'travel_px': round(travel, 1),
                           'held_ms': round((path[-1][0] - path[0][0]) * 1000, 1),
                           'from': panel_of(sx), 'to': panel_of(x),
                           'samples': len(path),
                           **summarise(path),
                           'path': [[round(pt[0] * 1000, 2), pt[1], pt[2]]
                                    for pt in path]}
                    fh.write(json.dumps(rec) + '\n')
                    fh.flush()
                    print(f'  #{recorded} ({sx},{sy}) {rec["from"]} -> '
                          f'({x},{y}) {rec["to"]}  {travel:.0f} px  '
                          f'{rec["held_ms"]:.0f} ms  {rec["updates"]} updates  '
                          f'step med {rec["step_med"]} max {rec["step_max"]}  '
                          f'gap {rec["gap_med_ms"]} ms')
                path = None

            was_down = down
            slack = period - (time.perf_counter() - t)
            if slack > 0:
                time.sleep(slack)

    print(f'\nrecorded {recorded} drag(s) -> {path_out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
