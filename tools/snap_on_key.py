"""Press a key, get a full screenshot — without alt-tabbing out of the game.

    pixi run snap-att                              # V shoots, Esc quits
    pixi run snap                                  # same, generic scratch dir
    pixi run snap --key F8 --out calibration/artifacts/tab/runs/drop_point

Polled with GetAsyncKeyState rather than read from stdin, so the game keeps
focus the whole time: leaving the window is exactly what a screenshot of the
game must not require, and the inventory screen closes when it loses focus.

EVERY SHOT ALSO RECORDS THE CURSOR POSITION, into the filename and a JSON
sidecar. That is usually the point of taking it — "where should this be
released" is answered by putting the mouse there and pressing the key, and
guessing the coordinate off a picture afterwards is how the current release
point came to be wrong in the first place.

WHERE THEY LAND MATTERS. `--kind` picks the root, and the roots are not
interchangeable:

    scratch      calibration/artifacts/shots/<stamp>/            anything, no consumer
    attachment   calibration/artifacts/attachments/tiles/<stamp>/  slot-tile corpus

⚠ NOT `calibration/artifacts/attachments/runs/`. That path IS ground truth --
`pixi run attachments` reads it as "a part was fitted on purpose and
confirmed" -- and hand-shot frames carry no such confirmation. Dropping them
in there would mix two kinds of evidence under one name, which is this repo's
most expensive recurring mistake. `tiles/` is a corpus; `runs/` is a claim.
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

# ⚠ THE SHUTTER KEY MUST NOT BE A KEY THE GAME USES, and the default was one
# for as long as this file existed: `C` is CROUCH. Every screenshot also
# changed the character's posture -- which moves the camera, the weapon and the
# posture icon, so the frame captured is not the frame that was on screen when
# the key went down. Checked against config.KEY_ACTION_TABLE on 2026-08-09:
#
#     in the table   1 2 5 b c down f f9 g pause right shift tab up win x z
#     clashing       B  C  X  F9        <- F9 is this repo's own
#     free           F8  V
#
# V is the default (the operator's pick); F8 is the other free one. Re-run
# that check before adding a key: a shutter that also fires an action photographs
# the consequence of pressing it.
VK = {'V': 0x56, 'F8': 0x77, 'C': 0x43, 'X': 0x58, 'B': 0x42, 'F9': 0x78}
GAME_KEYS = {'b', 'c', 'x', 'f9'}       # of the above; see the table
VK_ESC = 0x1B

# Corpus roots. A stamped subdirectory is created under whichever is picked.
ROOTS = {
    'scratch': ('calibration', 'artifacts', 'shots'),
    'attachment': ('calibration', 'artifacts', 'attachments', 'tiles'),
}


def down(vk):
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


cursor = cursor_pos   # 本文件的旧名字，实现在 press.pointer


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--key', default='V', choices=sorted(VK))
    ap.add_argument('--kind', default='scratch', choices=sorted(ROOTS),
                    help='which corpus this batch belongs to')
    ap.add_argument('--out', default=None,
                    help='exact directory, overriding --kind')
    ap.add_argument('--note', default='',
                    help='goes into every sidecar — say what you are capturing')
    args = ap.parse_args()

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out = args.out or os.path.join(ROOT, *ROOTS[args.kind], stamp)
    os.makedirs(out, exist_ok=True)

    if args.key.lower() in GAME_KEYS:
        print(f'[!] {args.key} is a key the game acts on — every shot would '
              f'also fire that action, and the frame saved is the one AFTER '
              f'it. Pick V or F8.')
        return 2
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
