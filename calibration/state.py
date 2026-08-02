"""Where am I, and what is the gun wearing? One read, no keys pressed.

Most calibration failures are state questions — wrong weapon, attachment that
will not read back, not in ADS, panel already open, evicted from the range —
and every one of them used to cost a full five-minute harvest run to discover.
Every question below is answerable off a single frame.

    pixi run python calibration/state.py
    pixi run python calibration/state.py --tab     # also read the inventory
    pixi run python calibration/state.py --watch   # keep printing

Nothing here presses a key or moves the mouse, so it is safe to run at any
moment, including while wondering whether some other agent is mid-run. The one
exception is --tab, which needs the inventory ALREADY open (open it yourself);
it will say so rather than pressing Tab and changing what it was asked to
observe.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from config import (HUD_REGIONS, SCREEN_W, SCREEN_H, RECOIL_SIGHT_PROFILES,
                    TAB_PIXEL_THRESH, TAB_COUNT_MIN, TAB_COUNT_MAX,
                    SPAWNER_ICON_ANCHORS, SPAWNER_ICON_W, SPAWNER_ICON_H,
                    SPAWNER_ICON_SEARCH)
from detector.cropper import RegionGrabber
from detector.posture_detector import PostureDetector
from detector.spawner_detector import SpawnerDetector
from detector.view_tracker import ViewTracker

OK, NO, HUH = '  ok ', ' -- ', ' ?? '


def _try(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


class Probe:
    def __init__(self, sight='red_dot'):
        self.sight = sight
        self.tracker = ViewTracker(
            patch_xs=RECOIL_SIGHT_PROFILES.get(sight, {}).get('patch_xs'))
        regions = {k: HUD_REGIONS[k] for k in
                   ('ammo', 'type', 'posture', 'weapon_1', 'weapon_2')}
        regions.update(self.tracker.regions())
        xs = [a[0] for a in SPAWNER_ICON_ANCHORS]
        ys = [a[1] for a in SPAWNER_ICON_ANCHORS]
        s = SPAWNER_ICON_SEARCH
        self._sp_box = (max(0, min(ys) - s), max(0, min(xs) - s),
                        max(ys) + SPAWNER_ICON_H + 2 * s - min(ys),
                        max(xs) + SPAWNER_ICON_W + 2 * s - min(xs))
        regions['spawner'] = self._sp_box
        self.regions = regions
        self.grab = RegionGrabber(regions)
        self.posture_det = PostureDetector()
        self.spawner_det = SpawnerDetector()
        self.ads_det = _try(lambda: __import__(
            'detector.ads_detector', fromlist=['x']).AdsDetector())
        self.ammo_det = _try(lambda: __import__(
            'detector.ammo_detector', fromlist=['x']).AmmoDetector())
        self._buf = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)

    def close(self):
        self.grab.close()

    def read(self):
        for _ in range(3):
            f = self.grab.grab()
        y, x, h, w = self._sp_box
        self._buf[y:y + h, x:x + w] = f['spawner']

        g = cv2.cvtColor(f['type'], cv2.COLOR_BGR2GRAY)
        n_tab = int((g > TAB_PIXEL_THRESH).sum())
        posture = self.posture_det.classify({'posture': f['posture']})

        out = {
            'tab_open': TAB_COUNT_MIN <= n_tab <= TAB_COUNT_MAX,
            'tab_px': n_tab,
            'spawner_open': bool(self.spawner_det.classify(self._buf)),
            'posture': posture,
            # The posture icon only renders in ADS, so its presence is an ADS
            # reading in its own right — and the only one available before
            # ads_detector existed. Kept alongside it because they disagree in
            # a useful way: the icon lags the transition, the crosshair does
            # not.
            'ads_by_icon': posture is not None,
            'ads_by_crosshair': _try(
                lambda: bool(self.ads_det.scoped(self._full(f)))),
            'ammo': _try(lambda: self.ammo_det.classify(f)),
            'gates': [round(self.tracker.gate_score(p), 2)
                      for p in (self.tracker.slice_frame(f) or [])],
        }
        return out

    def _full(self, f):
        """A screen-coordinate buffer, because AdsDetector crops relative to
        the frame's own centre — hand it a crop and it reads the middle of the
        crop, which is somewhere else entirely."""
        for name, (y, x, h, w) in self.regions.items():
            crop = f.get(name)
            if crop is not None:
                self._buf[y:y + h, x:x + w] = crop
        return self._buf


def game_focused():
    """Foreground process, and whether it is the game. Matched on the EXE:
    this repository's own name contains "pubg", so a title match calls an
    editor window the game."""
    try:
        import win32gui
        import win32process
        import psutil
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = psutil.Process(pid).name()
    except Exception:
        return False, '?', '?'
    return exe.lower().startswith('tslgame'), exe, title


def pico_state():
    from press.pico_mouse import get_mouse
    m = get_mouse()
    time.sleep(0.6)                       # let a heartbeat arrive
    return m, m.human_available()


def show(p, args):
    focused, exe, title = game_focused()
    s = p.read()

    print(f"focus       {OK if focused else NO}  {exe}  {title[:40]!r}")
    print(f"spawner     {OK if s['spawner_open'] else NO}  "
          f"{'panel is up' if s['spawner_open'] else 'panel closed'}"
          f"  — also the in-range test: comma only works inside it")
    print(f"inventory   {OK if s['tab_open'] else NO}  "
          f"{s['tab_px']} bright px in 'type' "
          f"(open window {TAB_COUNT_MIN}..{TAB_COUNT_MAX})")
    icon, cross = s['ads_by_icon'], s['ads_by_crosshair']
    agree = '' if cross is None else (
        '' if icon == cross else '   <-- DISAGREE, mid-transition?')
    print(f"ADS         {OK if icon else NO}  by posture icon"
          f"{'' if cross is None else f'   /  {OK if cross else NO} by crosshair'}"
          f"{agree}")
    print(f"posture     {OK if s['posture'] else HUH}  {s['posture'] or 'unreadable — not in ADS?'}")
    print(f"ammo        {OK if s['ammo'] else HUH}  {s['ammo']}")
    g = s['gates']
    if g:
        worst = min(g)
        print(f"texture     {OK if worst >= 0.1 else NO}  gate per patch "
              f"{g}  (floor 0.1; low = aiming at something flat)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--tab', action='store_true',
                    help='also read the inventory — the Tab screen must ALREADY be open')
    ap.add_argument('--watch', action='store_true')
    ap.add_argument('--pico', action='store_true',
                    help='also open the Pico; skip it to leave the port free')
    args = ap.parse_args()

    p = Probe(args.sight)
    prof = RECOIL_SIGHT_PROFILES.get(args.sight, {})
    print(f"sight       {args.sight}  K={prof.get('K')}  "
          f"patches {p.tracker.patch}x{p.tracker.patch_h} "
          f"wrap {p.tracker.patch_h // 2} px  xs={list(p.tracker.xs)}")

    if args.pico:
        try:
            m, human = pico_state()
            print(f"pico        {OK}  hand reporting "
                  f"{'on' if human else 'OFF (old firmware)'}")
        except Exception as e:
            print(f"pico        {NO}  {str(e).splitlines()[0]}")

    try:
        while True:
            print()
            show(p, args)
            if args.tab:
                read_tab(p)
            if not args.watch:
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        p.close()
    return 0


def read_tab(p):
    """Inventory contents. Needs the Tab screen already open — this never
    presses Tab, because the thing being diagnosed is often what Tab does."""
    s = p.read()
    if not s['tab_open']:
        print("inventory   -- not open; open it yourself, then re-run --tab")
        return
    from attach_control import AttachControl
    ac = AttachControl(verbose=False)
    try:
        if not ac.sync():
            print("inventory   ?? Tab reads open but the layout would not sync")
            return
        view = ac.look()
        for panel in ('inventory', 'nearby'):
            items = getattr(view, panel)
            named = [(i, it) for i, it in enumerate(items) if it is not None]
            print(f"  {panel}: "
                  f"{'empty' if not named else ''}")
            for i, it in named:
                print(f"    row {i} {it.key or '?':<14} {it.zh}")
        unknown = [u for u in view.unknown]
        if unknown:
            print(f"    UNRECOGNISED (occupied, no template): {unknown}")
            print("    -> a drifted template. find() cannot see these at all;"
                  " they surface as 'not on screen'.")
        for gun in (1, 2):
            slots = ac.read_slots(gun)
            print(f"  gun{gun} " + '  '.join(
                f"{k}={v or '-'}" for k, v in sorted(slots.items())))
    finally:
        ac.close()


if __name__ == '__main__':
    sys.exit(main())
