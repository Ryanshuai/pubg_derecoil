"""Does grabbing the weapon's slot-number tag actually pick the gun up?

    pixi run python tools/probe_gun_grab.py

Presses at the tag, drags to the middle of the screen, SCREENSHOTS while the
button is still down, then releases back where it started so nothing moves.

This exists because "the drop did not work" has two causes with one symptom
and guessing between them wasted a run each time:

    the grab missed        -- the press landed somewhere that is not a handle
                              for the weapon, so nothing was ever held
    the release missed     -- the gun was held but 附近 was not drawn (it only
                              exists while something is on the ground) or the
                              release point was outside it

A picture taken mid-drag separates them in one look: either the gun is stuck
to the cursor or it is not. Nothing else has to be inferred.

Releasing back at the source is what makes this safe to run repeatedly -- a
drop onto its own slot is a no-op, so the rack is in the same state afterwards.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2

from config import SCREEN_W, SCREEN_H
from detector.cropper import win32_cap
from detector.tab_layout import gun_tag_point
from control.focus import ensure_focus

from control.inventory import InventoryControl
from control.stock import open_tab

OUT = os.path.join(ROOT, 'docs', 'gun_grab')
MID = (SCREEN_W // 2, SCREEN_H // 2)


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    if not ensure_focus(countdown_s=6, label='the gun-grab probe'):
        print('[!] could not focus the game')
        return 1
    time.sleep(0.7)

    ac = InventoryControl(verbose=False)
    os.makedirs(OUT, exist_ok=True)
    try:
        if not open_tab(ac, label='the gun-grab probe'):
            return 1
        view = ac.look()
        print(f"plates      : {ac.read_weapons()}")
        print(f"nearby rows : {view.rows('nearby')}   "
              f"inventory rows: {view.rows('inventory')}")
        src = gun_tag_point(1)
        print(f"grab point  : {src}")

        p = ac.pointer
        cv2.imwrite(os.path.join(OUT, '0_before.png'), win32_cap((0, 0, SCREEN_H, SCREEN_W)))
        p.move_to(*src)
        time.sleep(0.15)
        p._press(0x01)
        try:
            time.sleep(0.12)
            for i in range(1, 9):
                f = i / 8
                p.move_to(round(src[0] + (MID[0] - src[0]) * f),
                          round(src[1] + (MID[1] - src[1]) * f))
                time.sleep(0.02)
            time.sleep(0.25)
            cv2.imwrite(os.path.join(OUT, '1_holding.png'), win32_cap((0, 0, SCREEN_H, SCREEN_W)))
            # Back to the source before letting go: a release over the slot it
            # came from changes nothing.
            for i in range(1, 9):
                f = i / 8
                p.move_to(round(MID[0] + (src[0] - MID[0]) * f),
                          round(MID[1] + (src[1] - MID[1]) * f))
                time.sleep(0.02)
            time.sleep(0.12)
        finally:
            p._release(0x01)
        time.sleep(0.4)
        cv2.imwrite(os.path.join(OUT, '2_after.png'), win32_cap((0, 0, SCREEN_H, SCREEN_W)))
        print(f"plates after: {ac.read_weapons()}")
        print(f"\n  -> {os.path.relpath(OUT, ROOT)}  "
              f"(0_before / 1_holding / 2_after)")
        print('  1_holding is the answer: gun on the cursor = the grab works '
              'and the\n  drop target is wrong; nothing on the cursor = the '
              'grab point is wrong.')
    finally:
        ac.ensure_tab(False)
        ac.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
