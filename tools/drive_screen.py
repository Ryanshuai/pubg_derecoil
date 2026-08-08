"""Open a game screen, prove it is up, and shoot it — the step before any
screen can be calibrated.

Every calibration module here starts from "the screen is already open", and
each one re-implements getting there: press TAB, sleep, grab, press TAB again
appears in sweep.py, capture_ads.py, auto_calibrate.py and
collect_name_templates.py, four times, with four different waits. This is
that step, once, with the verification the copies skip.

    pixi run python tools/drive_screen.py list
    pixi run python tools/drive_screen.py tab --shoot
    pixi run python tools/drive_screen.py spawner --shoot baseline
    pixi run python tools/drive_screen.py tab --keep-open

The chain, in order, because each link fails differently:

  ready     control.session.ensure_ready() in main(), NOT in drive() — a
            keypress sent to the terminal is simply lost, TAB in the lobby
            does nothing, and the ESC menu eats every key while looking
            exactly like a live round to the pixel probes. The leg for the
            screen being opened is switched off, since shutting it is the one
            thing that would defeat the shot.
  known     If the screen is already up, close it first. A toggle key applied
            to an unknown state lands on the opposite of what was wanted.
  open      Press, wait, verify. Not "press and assume".
  park      Move the cursor off the panel before the shot — a hovered row
            draws a highlight that shifts the element's measured bounds.
  close     Press again, verify it closed. A panel left open breaks whatever
            runs next.

Shots land in calibration/artifacts/<screen>/. Keep them: the calibrate-screen and
calibrate-template skills both work off saved captures, and a screen that has
to be re-opened to be re-measured cannot be measured offline at all.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The console here is cp1252 and every Chinese label below would raise
# UnicodeEncodeError on print, not in the game logic but in the logging about
# it. Same family of trap as the PowerShell Get-Content one in the skill.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

import cv2

from detector import spawner_layout, tab_layout
from detector.spawner_detector import SpawnerDetector
from capture.cropper import capture_screen
from press.pico_mouse import HID_KEY_COMMA, HID_KEY_TAB
from press.pointer import Pointer, move_cursor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Built once and reused: SpawnerDetector loads templates off disk.
_spawner = None


def _spawner_up(frame):
    global _spawner
    if _spawner is None:
        _spawner = SpawnerDetector()
    return _spawner.classify(frame)


class Screen:
    """How to open one screen, and how to know it worked.

    open_wait is a render wait, not a guess at the game's mood. Anything that
    varies with matchmaking or loading belongs in control/lobby.py, which polls.
    """

    def __init__(self, name, key, verify, park, open_wait=0.45,
                 close_wait=0.40, settle=0.25, note=''):
        self.name = name
        self.key = key
        self.verify = verify
        self.park = park
        self.open_wait = open_wait
        self.close_wait = close_wait
        self.settle = settle
        self.note = note

    def shoot(self):
        """Screenshot with the cursor parked off the panel."""
        move_cursor(self.park)
        time.sleep(self.settle)
        return capture_screen()

    def is_up(self):
        return self.verify(self.shoot())


SCREENS = {
    'tab': Screen(
        'tab', HID_KEY_TAB, tab_layout.is_open, tab_layout.PARK_XY,
        note='inventory. Anchor is the 类型/Type column header — see '
             'tools/probe_tab_anchor.py, it is language-dependent.'),
    'spawner': Screen(
        'spawner', HID_KEY_COMMA, _spawner_up, spawner_layout.PARK_XY,
        open_wait=0.8,
        note='training-range item spawner. Its columns draw progressively, '
             'so the three button glyphs can confirm "open" before the last '
             'column exists — SpawnerControl.sync re-reads for that reason.'),
}


def drive(screen, shoot_as=None, keep_open=False, verbose=True):
    """-> {'ok', 'path', 'error'}

    ⚠ THIS DOES NOT GATE THE GAME, and it used to. It opened with
    ensure_focus() plus an open-coded ensure_in_match(), which put it on rule
    9's debt list with the note "its ensure_focus is inside drive(), a LIBRARY
    function that calibration/scan_compat.py calls once per weapon -- 30 times
    a run. ensure_ready there would teleport 30 times."

    Every clause of that was false, and it is worth writing down because the
    exemption survived a year on it. scan_compat imports SCREENS, not drive();
    `grep 'drive('` finds exactly ONE caller, main() below. And scan_compat
    has opened with ensure_ready since 2026-08-06 anyway, so the teleport the
    note was protecting against could not have happened. AN EXEMPTION IS A
    CLAIM ABOUT THE CODE, and this one was never checked against it.

    The gate belongs to whoever is starting a run — main() takes it now, and a
    caller that imports this drives through their own ensure_ready.
    """
    def log(m):
        if verbose:
            print(f'[drive] {m}', flush=True)

    # No `pico is None` branch: Pointer() raises when there is no device, and
    # the raise carries who is holding it. The dict this used to return said
    # only "no Pico".
    ptr = Pointer()

    if screen.is_up():
        log(f'{screen.name} is already up — closing first for a known state')
        ptr.pico.key(screen.key, 60)
        time.sleep(screen.close_wait)
        if screen.is_up():
            return {'ok': False, 'path': None,
                    'error': f'{screen.name} would not close; refusing to '
                             f'toggle blind'}

    log(f'opening {screen.name}')
    ptr.pico.key(screen.key, 60)
    time.sleep(screen.open_wait)

    frame = screen.shoot()
    if not screen.verify(frame):
        ptr.pico.key(screen.key, 60)          # put the key back
        time.sleep(screen.close_wait)
        return {'ok': False, 'path': None,
                'error': f'{screen.name} did not come up (anchor check '
                         f'failed after {screen.open_wait}s)'}
    log(f'{screen.name} is up and verified')

    path = None
    if shoot_as:
        out_dir = os.path.join(ROOT, 'calibration', 'artifacts', screen.name)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f'{shoot_as}.png')
        cv2.imwrite(path, frame)
        log(f'wrote {path}  {frame.shape[1]}x{frame.shape[0]}')

    if keep_open:
        log(f'leaving {screen.name} open as asked')
        return {'ok': True, 'path': path, 'error': None}

    ptr.pico.key(screen.key, 60)
    time.sleep(screen.close_wait)
    if screen.is_up():
        return {'ok': False, 'path': path,
                'error': f'{screen.name} would not close — the next tool to '
                         f'run will find it in the way'}
    log(f'{screen.name} closed')
    return {'ok': True, 'path': path, 'error': None}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('screen', choices=tuple(SCREENS) + ('list',))
    ap.add_argument('--shoot', nargs='?', const='__stamp__', default=None,
                    metavar='NAME',
                    help='save the verified frame to calibration/artifacts/<screen>/<NAME>.png')
    ap.add_argument('--keep-open', action='store_true')
    ap.add_argument('--no-ensure', action='store_true',
                    help='skip the readiness gate; assumes a live match with '
                         'nothing else on screen')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    if args.screen == 'list':
        for name, s in SCREENS.items():
            print(f'{name:10} key=0x{s.key:02X}  park={s.park}  '
                  f'open_wait={s.open_wait}s')
            print(f'{"":10} {s.note}')
        return 0

    shoot_as = args.shoot
    if shoot_as == '__stamp__':
        shoot_as = time.strftime('%Y%m%d_%H%M%S')

    # ⚠ THE GATE IS HERE, NOT IN drive(). And it is the whole five-leg gate,
    # not the focus + match that used to live one level down: this script's
    # entire job is to open ONE screen and photograph it, so the other three
    # legs are precisely the ones that ruin it. A Tab already up means the
    # toggle closes it; a spawner panel already up means the shot is of the
    # panel; and the shot is a CALIBRATION REFERENCE that later runs match
    # against, so a wrong one is not a failed run, it is a wrong constant.
    #
    # `panel=False` for the spawner screen, because that leg would shut the
    # very thing being photographed. Same reasoning as tab=False below it: a
    # gate that closes the subject is not a gate.
    if not args.no_ensure:
        from control.session import ensure_ready
        rec = ensure_ready(label=f'drive {args.screen}',
                           countdown_s=args.countdown,
                           tab=args.screen != 'tab',
                           panel=args.screen != 'spawner')
        if not rec['ok']:
            print(f"not ready: failed at {rec.get('failed')!r}")
            return 1

    rec = drive(SCREENS[args.screen], shoot_as=shoot_as,
                keep_open=args.keep_open)
    print(f'\n{rec}')
    return 0 if rec['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
