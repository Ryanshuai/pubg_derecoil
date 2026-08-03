"""Grab a labelled lobby screenshot into docs/lobby/, cursor parked.

Parking is not optional here. The mode tabs light up on hover with the same
brightness that marks the *selected* tab, so a shot taken with the cursor
resting on NORMAL is indistinguishable from a shot where NORMAL is selected —
and that is exactly the signal probe_lobby_nav.py reads. One careless
screenshot poisons the measurement it is meant to establish.

No focus stealing and no Pico: this only moves the system cursor and grabs
the desktop, so it is safe to run while the game is merely visible. Bring the
lobby up yourself, then let the countdown run.

    pixi run python tools/shoot_lobby.py play_normal
    pixi run python tools/shoot_lobby.py play_training_clean --countdown 8
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from detector.cropper import capture_screen
from press.pointer import move_cursor
from control.focus import game_focused

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'docs', 'lobby')

# Inside the left letterbox bar, which the lobby paints flat black and puts
# nothing clickable on. Not the right bar: LOBBY_BAR_ROI reads that one, and
# a cursor sitting in it would show up in the letterbox probe.
PARK_XY = (200, 700)
SETTLE = 0.35          # hover highlights fade out; give them time


def shoot(name, countdown=5, settle=SETTLE):
    for s in range(countdown, 0, -1):
        print(f'    grabbing in {s} ... (bring the lobby up)', flush=True)
        time.sleep(1.0)

    if not game_focused():
        print('[shoot] warning: the game is not the foreground window. The '
              'grab still works, but an overlapping window will be in it.')

    move_cursor(PARK_XY)
    time.sleep(settle)
    frame = capture_screen()

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f'{name}.png')
    cv2.imwrite(path, frame)
    print(f'[shoot] {path}  {frame.shape[1]}x{frame.shape[0]}')
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('name', help='file stem, e.g. play_normal')
    ap.add_argument('--countdown', type=int, default=5)
    ap.add_argument('--probe', action='store_true',
                    help='run probe_lobby_nav.py on the result')
    args = ap.parse_args()

    path = shoot(args.name, args.countdown)

    if args.probe:
        import subprocess
        subprocess.run([sys.executable,
                        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'probe_lobby_nav.py'), path])
    return 0


if __name__ == '__main__':
    sys.exit(main())
