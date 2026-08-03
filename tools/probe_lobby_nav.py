"""Dump the lobby's tab bars from a saved screenshot.

The geometry lives in detector/lobby_nav.py — this is the offline view of it,
for a capture rather than a live game. Use it to re-measure after a game
update, or to check a shot before trusting it as a regression asset.

    pixi run python tools/probe_lobby_nav.py docs/lobby/lobby.png
    pixi run python tools/probe_lobby_nav.py docs/lobby/play_normal.png

Live equivalents:
    pixi run python control/lobby.py state    # includes the mode
    pixi run python control/lobby.py mode     # select + verify

WHAT TO LOOK FOR: `sel` is zero for every unselected tab in every capture so
far, so exactly one tab per bar should be non-zero. Two lit tabs means the
cursor was hovering when the shot was taken — hover lights a tab exactly like
selection does, and the shot is poisoned for measuring this. Re-grab with
tools/shoot_lobby.py, which parks first.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from config import LOBBY_SUB_BAR_ROI, LOBBY_TAB_SEL_THRESH, LOBBY_TOP_BAR_ROI
from detector.lobby_nav import SUB_TABS, TOP_TABS, read_bar


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('shot', help='a lobby screenshot (3440x1440)')
    args = ap.parse_args()

    img = cv2.imread(args.shot, cv2.IMREAD_GRAYSCALE)
    if img is None:
        print(f'cannot read {args.shot}')
        return 1
    if img.ndim == 3:                    # ultralytics patches imread
        img = img[:, :, 0]
    print(f'{args.shot}  {img.shape[1]}x{img.shape[0]}')

    rc = 0
    for label, roi, names in (('top bar', LOBBY_TOP_BAR_ROI, TOP_TABS),
                              ('sub bar', LOBBY_SUB_BAR_ROI, SUB_TABS)):
        print(f'\n-- {label}  roi(y,x,h,w)={roi}')
        found, best, margin = read_bar(img, roi, names)
        if len(found) != len(names):
            print(f'   !! found {len(found)} labels, expected {len(names)} '
                  f'-- a dialog is covering the bar, or the game changed it')
            rc = 1
        for s in found:
            mark = '  <== SELECTED' if s is best else ''
            print(f'   {s["name"]:<10} x {s["x0"]:4d}..{s["x1"]:4d}  '
                  f'y {s["y0"]:4d}..{s["y1"]:4d}  '
                  f'click ({s["cx"]:4d},{s["cy"]:4d})  '
                  f'ink {s["ink"]:5d}  sel {s["sel_ink"]:5d}{mark}')
        lit = [s for s in found if s['sel_ink'] > 0]
        if len(lit) > 1:
            print(f'   !! {len(lit)} tabs above sel_thresh='
                  f'{LOBBY_TAB_SEL_THRESH} -- cursor was hovering when this '
                  f'was taken? Re-grab with tools/shoot_lobby.py')
            rc = 1
        if best:
            print(f'   selected={best["name"]}  margin={margin:.0f}x')
    return rc


if __name__ == '__main__':
    sys.exit(main())
