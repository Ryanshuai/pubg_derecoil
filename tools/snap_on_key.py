"""Press a key, get a full screenshot — without alt-tabbing out of the game.

    pixi run python tools/snap_on_key.py            # C shoots, Esc quits
    pixi run python tools/snap_on_key.py --key V --out docs/tab/runs/drop_point

Polled with GetAsyncKeyState rather than read from stdin, so the game keeps
focus the whole time: leaving the window is exactly what a screenshot of the
game must not require, and the inventory screen closes when it loses focus.

EVERY SHOT ALSO RECORDS THE CURSOR POSITION, into the filename and a JSON
sidecar. That is usually the point of taking it — "where should this be
released" is answered by putting the mouse there and pressing the key, and
guessing the coordinate off a picture afterwards is how the current release
point came to be wrong in the first place.

Shots land in docs/, so tools/regression_check.py picks them up on its own.
"""
import argparse
import ctypes
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import cv2

from capture.cropper import capture_screen
from press.pointer import cursor_pos

VK = {'C': 0x43, 'V': 0x56, 'X': 0x58, 'B': 0x42, 'F8': 0x77, 'F9': 0x78}
VK_ESC = 0x1B


def down(vk):
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


cursor = cursor_pos   # 本文件的旧名字，实现在 press.pointer


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--key', default='C', choices=sorted(VK))
    ap.add_argument('--out', default=None,
                    help='directory (default docs/shots/<stamp>/)')
    ap.add_argument('--note', default='',
                    help='goes into every sidecar — say what you are capturing')
    args = ap.parse_args()

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out = args.out or os.path.join(ROOT, 'docs', 'shots', stamp)
    os.makedirs(out, exist_ok=True)

    vk = VK[args.key]
    print(f'ready — press {args.key} in the game to shoot, Esc to stop')
    print(f'        -> {out}')
    n = 0
    try:
        while True:
            if down(VK_ESC):
                break
            if not down(vk):
                time.sleep(0.01)
                continue
            x, y = cursor()                 # BEFORE the grab; the grab is slow
            img = capture_screen()
            n += 1
            name = f'{n:02d}_cursor_{x}x{y}'
            cv2.imwrite(os.path.join(out, name + '.png'), img)
            with open(os.path.join(out, name + '.json'), 'w',
                      encoding='utf-8') as f:
                json.dump({'ts': datetime.now().isoformat(timespec='seconds'),
                           'cursor': [x, y], 'key': args.key,
                           'note': args.note,
                           'size': [img.shape[1], img.shape[0]]}, f,
                          ensure_ascii=False, indent=1)
            print(f'  {n:02d}  cursor ({x}, {y})  -> {name}.png')
            while down(vk):                 # one shot per press, not per frame
                time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    print(f'\n{n} shot(s) in {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
