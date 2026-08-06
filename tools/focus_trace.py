"""Who owns the foreground, sampled over time.

Written because a run reported taking focus successfully and then lost it
inside 27 seconds, while the human watching swore the game never came forward
at all. Both accounts cannot be right, and neither is checkable from a single
reading — the question is *when* it changed and *to whom*.

    pixi run python tools/focus_trace.py            # watch for 15s
    pixi run python tools/focus_trace.py --raise    # take focus first, then watch
    pixi run python tools/focus_trace.py --windows  # list the game's windows

Prints only transitions, so a stable foreground costs one line.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32gui
import win32process

from control.focus import (GAME_EXES, game_focused, game_hwnd,
                           raise_game, window_info)


def _describe(hwnd):
    i = window_info(hwnd)
    return (f'hwnd={hwnd} pid={i["pid"]} {i["exe"] or "?"} '
            f'{i["title"][:44]!r}')


def list_windows():
    """Every visible window of the game process, largest first.

    raise_game() picks the largest: PUBG owns several windows and only one of
    them takes input. If the biggest is not the one you see, that is the bug.
    """
    found = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        exe = window_info(hwnd)['exe']
        if not any(exe.startswith(k) for k in GAME_EXES):
            return
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
        except Exception:
            return
        found.append(((r - l) * (b - t), hwnd, (l, t, r, b)))

    win32gui.EnumWindows(_cb, None)
    found.sort(reverse=True)
    if not found:
        print('no visible window belongs to', GAME_EXES)
        return
    print(f'{len(found)} visible game window(s), largest first '
          f'(raise_game takes the first):')
    for area, hwnd, rect in found:
        mark = ' <-- game_hwnd()' if hwnd == game_hwnd() else ''
        print(f'  area={area:>10}  rect={rect}  {_describe(hwnd)}{mark}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=15.0)
    ap.add_argument('--interval', type=float, default=0.25)
    ap.add_argument('--raise', dest='do_raise', action='store_true')
    ap.add_argument('--windows', action='store_true')
    args = ap.parse_args()

    if args.windows:
        list_windows()
        return 0

    if args.do_raise:
        t0 = time.perf_counter()
        ok = raise_game()
        print(f'[{time.perf_counter()-t0:5.2f}s] raise_game -> {ok}   '
              f'target {_describe(game_hwnd() or 0)}')

    t0 = time.perf_counter()
    last = None
    while time.perf_counter() - t0 < args.seconds:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd != last:
            print(f'[{time.perf_counter()-t0:5.2f}s] '
                  f'{"GAME " if game_focused() else "other"}  '
                  f'{_describe(hwnd)}', flush=True)
            last = hwnd
        time.sleep(args.interval)
    print(f'[{args.seconds:5.2f}s] end — game_focused={game_focused()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
