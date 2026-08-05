"""TabWatch against the real game. Needs the game.

    pixi run python tools/probe_tab_watch_live.py --n 20

tools/test_tab_watch.py proves the state machine against a scripted screen.
This proves the screen reading itself: drive Tab from the Pico N times and,
after each toggle, compare what TabWatch believes against an INDEPENDENT read
of the same anchor. Agreement is the whole claim -- state.tab_open used to be
a flipped cache, and the point of the change is that it now follows pixels.

Also checks the part that replaced `delay: -50`: when the panel closes, the
loadout TabWatch is holding must be the one taken while it was up, not an
empty read from after it went away.
"""
import argparse
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from config import HUD_REGIONS
from control.focus import ensure_focus, focus_keeper
from control.lobby import LobbyControl
from control.tab_watch import TabWatch
from detector.attachment_detector import AttachmentDetector
from detector.cropper import win32_cap
from detector.game_state import GameState
from detector.tab_detector import TabTypeDetector
from detector.weapon_template_detector import TabWeaponDetector
from press.pico_mouse import HID_KEY_TAB, get_mouse

TICK = 0.011            # what the dispatcher loop runs at
SETTLE_BUDGET = 1.0     # how long to let TabWatch notice a toggle

_truth_det = None


def truth():
    """Independent answer to 'is Tab up', not going through TabWatch."""
    global _truth_det
    if _truth_det is None:
        _truth_det = TabTypeDetector()
    return bool(_truth_det.classify({'type': win32_cap(HUD_REGIONS['type'])}))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--n', type=int, default=20, help='toggles')
    ap.add_argument('--countdown', type=int, default=4)
    args = ap.parse_args()

    if not ensure_focus(countdown_s=args.countdown, label='the TabWatch probe'):
        return 1

    with LobbyControl(verbose=False) as lc:
        if not lc.state().playable:
            print('walking back into the range ...')
            if not lc.ensure_in_match()['ok']:
                print('[!] could not get in')
                return 1
            time.sleep(1.0)

    mouse = get_mouse()
    state = GameState()
    watch = TabWatch(state, {'tab_type': TabTypeDetector(),
                             'tab_weapon': TabWeaponDetector(),
                             'tab_attachment': AttachmentDetector()},
                     verbose=False)

    # Start from the truth, however the screen happens to be right now.
    watch._set_open(truth(), time.perf_counter())
    print(f'starting with Tab {"open" if watch.open else "closed"}\n')

    agree = disagree = 0
    lags, loadout_ok, loadout_n = [], 0, 0
    keeper = focus_keeper()
    try:
        for i in range(args.n):
            if not keeper.ok(f'toggle {i + 1}'):
                return 1
            was_open = watch.open

            t0 = time.perf_counter()
            watch.on_key(t0)
            mouse.key(HID_KEY_TAB, 60)

            # Tick exactly as the dispatcher does, until it notices or gives up.
            while time.perf_counter() - t0 < SETTLE_BUDGET:
                watch.tick()
                if watch.open != was_open:
                    break
                time.sleep(TICK)
            lag = time.perf_counter() - t0

            # Let it settle, then ask the screen directly.
            time.sleep(0.25)
            real = truth()
            ok = watch.open == real
            agree, disagree = agree + ok, disagree + (not ok)
            if watch.open != was_open:
                lags.append(lag)

            note = ''
            if was_open and not watch.open:
                # It just closed: the reading it kept must be the one from
                # while the panel was up, not an empty post-close read.
                loadout_n += 1
                lo = watch.loadout
                good = lo is not None and lo.get('weapons') not in (None, ('', ''))
                loadout_ok += good
                note = (f'  loadout={lo["weapons"] if lo else None}'
                        f'{"" if good else "   *** EMPTY ***"}')

            print(f'  {i + 1:2d}: {"open " if was_open else "closed"} -> '
                  f'{"open " if watch.open else "closed"}  '
                  f'noticed in {lag * 1000:4.0f} ms   screen says '
                  f'{"open" if real else "closed"}  '
                  f'{"OK" if ok else "*** DISAGREES ***"}{note}')

            # Keep the panel-up half short; a long one just burns range time.
            time.sleep(0.35 if watch.open else 0.2)
    finally:
        watch.close()

    print(f'\n{agree}/{agree + disagree} agreed with the screen')
    if lags:
        print(f'noticed the change in: median {statistics.median(lags) * 1000:.0f} ms, '
              f'max {max(lags) * 1000:.0f} ms')
    if loadout_n:
        print(f'{loadout_ok}/{loadout_n} closes kept a non-empty loadout')
    return 0 if disagree == 0 and loadout_ok == loadout_n else 1


if __name__ == '__main__':
    sys.exit(main())
