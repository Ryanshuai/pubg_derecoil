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

import numpy as np

from config import (HUD_REGIONS, RECOIL_SIGHT_PROFILES,
                    TAB_PIXEL_THRESH, TAB_COUNT_MIN, TAB_COUNT_MAX,
                    TAB_DARK_FLOOR_MAX)
from detector.cropper import ScreenBuffer
from detector.posture_detector import PostureDetector
from detector.spawner_detector import ICON_BOX, SpawnerDetector
from detector.tab_detector import TabTypeDetector
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
        self._sp_box = ICON_BOX
        regions['spawner'] = self._sp_box
        self.regions = regions
        # NO focus_fn, deliberately. This tool's whole promise is that it is
        # safe to run at any moment -- including while wondering whether some
        # other agent has the game -- and it PRINTS focus as one of its
        # readings. A guard here would make it refuse to work at exactly the
        # moment it is most wanted.
        self.frames = ScreenBuffer(regions)
        self.posture_det = PostureDetector()
        self.spawner_det = SpawnerDetector()
        self.tab_det = TabTypeDetector()
        self.ads_det = _try(lambda: __import__(
            'detector.ads_detector', fromlist=['x']).AdsDetector())
        self.ammo_det = _try(lambda: __import__(
            'detector.ammo_detector', fromlist=['x']).AmmoDetector())

    def close(self):
        self.frames.close()

    def read(self):
        f = self.frames.flush(3)
        # One blit, shared by both detectors that index screen coordinates.
        # This used to blit `spawner` on its own and then blit EVERY region a
        # second time inside _full(), because the two calls did not know about
        # each other.
        buf = self.frames.full(f)

        # The verdict comes from the detector, not from a copy of its maths.
        # The two numbers beside it are re-derived here only so the printout
        # can show WHY it said what it said -- this was a third fork of the
        # predicate until 2026-08, and the one that had drifted furthest.
        m = np.max(f['type'], axis=2)
        n_tab = int((m > TAB_PIXEL_THRESH).sum())
        floor = int(np.percentile(m, 10))
        posture = self.posture_det.classify({'posture': f['posture']})

        return {
            'tab_open': bool(self.tab_det.classify(f['type'])),
            'tab_px': n_tab,
            'tab_floor': floor,
            'spawner_open': bool(self.spawner_det.classify(buf)),
            'posture': posture,
            # The posture icon only renders in ADS, so its presence is an ADS
            # reading in its own right — and the only one available before
            # ads_detector existed. Kept alongside it because they disagree in
            # a useful way: the icon lags the transition, the crosshair does
            # not.
            'ads_by_icon': posture is not None,
            'ads_by_crosshair': _try(lambda: bool(self.ads_det.scoped(buf))),
            'ammo': _try(lambda: self.ammo_det.classify(f)),
            'gates': [round(self.tracker.gate_score(p), 2)
                      for p in (self.tracker.slice_frame(f) or [])],
        }


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
    from control.focus import GAME_EXES
    return (any(exe.lower().startswith(k) for k in GAME_EXES), exe, title)


def pico_state():
    """Open the CDC link and see whether hand reporting is alive.

    THE ONLY THING IN THIS FILE THAT TAKES SHARED HARDWARE, which is why it is
    behind --pico and not in the default read. The module promises it is safe
    to run at any moment, including while wondering whether some other agent is
    mid-run — and the Pico is single-tenant, so opening its port is exactly the
    kind of interference that promise is about.

    So it asks first. `other_agents()` names the other python processes running
    out of this project; the port is not locked, and a collision shows up as
    symptoms rather than an error (a run whose next command silently goes
    nowhere), so refusing here is worth more than a heartbeat reading.
    """
    from press.pico_mouse import get_mouse, other_agents
    busy = other_agents()
    if busy:
        raise RuntimeError(
            f'another agent is running ({busy}) — not taking the Pico out '
            f'from under it. Re-run with the other process stopped.')
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
          f"(open window {TAB_COUNT_MIN}..{TAB_COUNT_MAX}), "
          f"dark floor {s['tab_floor']} (needs < {TAB_DARK_FLOOR_MAX}: "
          f"a bright floor is sky, not glyphs)")
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
    from control.inventory import InventoryControl
    ac = InventoryControl(verbose=False)
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
