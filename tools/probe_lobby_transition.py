"""End-to-end probe of the lobby -> match transition.

Everything about entering a match is unmeasured: whether the PLAY button's "F"
hint works as a keypress, whether the daily-mission popup eats that key, what
the loading screen reads as, and how long the whole thing takes. One run
answers all of it, so control/lobby.py can be written against measurements.

    pixi run python tools/probe_lobby_transition.py          # the full run
    pixi run python tools/probe_lobby_transition.py --watch  # observe only

The full run is four phases and needs no babysitting between them:

    wait    poll until the screen reads LOBBY. Start anywhere — if a round is
            still up, back out to the lobby by hand and the probe picks it up.
    press   send F through the Pico.
    follow  sample until IN_GAME. If F has done nothing after --f-timeout,
            fall back to clicking the PLAY button, so one run distinguishes
            "F is not the right key" from "the popup swallowed it".
    settle  keep sampling after IN_GAME, to catch a state that flickers back.

Full-screen shots and states.csv land in docs/lobby/runs/<n>/, so a transition
only has to be captured once to be replayable offline.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from config import LOBBY_BAR_ROI, LOBBY_PING_ROI, LOBBY_PLAY_XY
from detector.cropper import capture_screen
from detector.geometry import cut
from detector.lobby_detector import LobbyState, bar_max, classify, ping_fraction
from control.focus import game_focused

RUNS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'docs', 'lobby', 'runs')


def sample(frame):
    return (classify(cut(frame, LOBBY_BAR_ROI), cut(frame, LOBBY_PING_ROI)),
            bar_max(cut(frame, LOBBY_BAR_ROI)),
            ping_fraction(cut(frame, LOBBY_PING_ROI)))


def next_run_dir():
    os.makedirs(RUNS, exist_ok=True)
    n = 1 + max([int(d) for d in os.listdir(RUNS) if d.isdigit()] or [0])
    path = os.path.join(RUNS, str(n))
    os.makedirs(path)
    return path


class Recorder:
    """Samples the screen, saves shots on every state change, writes the csv."""

    def __init__(self, run, shot_every):
        self.run = run
        self.shot_every = shot_every
        self.rows = []
        self.t0 = time.perf_counter()
        self.last_shot = -1e9
        self.last_state = None
        self.marks = []

    @property
    def t(self):
        return time.perf_counter() - self.t0

    def mark(self, label):
        """Record an action (a keypress, a click) on the same timeline."""
        self.marks.append((self.t, label))
        print(f'  {self.t:6.2f}s  >>> {label}', flush=True)

    def step(self, tag=''):
        t = self.t
        frame = capture_screen()
        state, bmax, pfrac = sample(frame)
        changed = state is not self.last_state

        if changed or t - self.last_shot >= self.shot_every:
            cv2.imwrite(os.path.join(self.run, f'{t:07.2f}_{state.value}.jpg'),
                        frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
            self.last_shot = t

        self.rows.append((t, state.value, bmax, pfrac, int(changed), tag))
        if changed:
            prev = self.last_state.value if self.last_state else '-'
            print(f'  {t:6.2f}s  {prev:<10} -> {state.value:<10} '
                  f'bar_max={bmax:3d} ping={pfrac:.3f}', flush=True)
            self.last_state = state
        return state

    def write(self):
        csv = os.path.join(self.run, 'states.csv')
        with open(csv, 'w', encoding='utf-8') as f:
            f.write('t,state,bar_max,ping_frac,changed,phase\n')
            for r in self.rows:
                f.write(f'{r[0]:.3f},{r[1]},{r[2]},{r[3]:.4f},{r[4]},{r[5]}\n')
        if self.marks:
            with open(os.path.join(self.run, 'actions.csv'), 'w',
                      encoding='utf-8') as f:
                f.write('t,action\n')
                for t, label in self.marks:
                    f.write(f'{t:.3f},{label}\n')
        return csv

    def sequence(self):
        seen = []
        for _, s, *_ in self.rows:
            if not seen or seen[-1] != s:
                seen.append(s)
        return seen


def press_f():
    """Press F through the Pico. Returns False if there is no Pico.

    Deliberately no SendInput fallback: the game reads keys through raw input,
    and a silent no-op would look identical to "F does not start a match",
    which is the question this script exists to answer.
    """
    from press.pico_mouse import HID_KEY_F, PicoMouse, get_mouse
    mouse = get_mouse()
    if not isinstance(mouse, PicoMouse):
        print(f'[probe] {type(mouse).__name__} cannot send keys; '
              f'set config.MOUSE_BACKEND = "pico"')
        return False
    mouse.key(HID_KEY_F, 60)
    return True


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--watch', action='store_true',
                    help='observe only, never press or click')
    ap.add_argument('--interval', type=float, default=0.5)
    ap.add_argument('--shot-every', type=float, default=2.0)
    ap.add_argument('--lobby-timeout', type=float, default=180.0,
                    help='how long to wait for the lobby to appear')
    ap.add_argument('--f-timeout', type=float, default=15.0,
                    help='no state change this long after F -> try clicking')
    ap.add_argument('--enter-timeout', type=float, default=120.0,
                    help='give up waiting for IN_GAME after this')
    ap.add_argument('--settle', type=float, default=10.0,
                    help='keep sampling this long after reaching IN_GAME')
    ap.add_argument('--no-click-fallback', action='store_true',
                    help='if F does nothing, do not try the PLAY button')
    ap.add_argument('--countdown', type=int, default=5)
    args = ap.parse_args()

    print('>>> Bring the game to the foreground.')
    if not args.watch:
        print('>>> Any state is fine — if a round is up, back out to the '
              'lobby and the probe will pick it up.')
    for s in range(args.countdown, 0, -1):
        print(f'    starting in {s} ...', flush=True)
        time.sleep(1.0)

    run = next_run_dir()
    print(f'[probe] recording to {os.path.relpath(run)}\n')
    rec = Recorder(run, args.shot_every)

    def sleep_step():
        time.sleep(args.interval)

    # ── watch ────────────────────────────────────────────────────────────
    if args.watch:
        end = rec.t + args.lobby_timeout
        while rec.t < end:
            rec.step('watch')
            sleep_step()
        return report(rec, run, entered=None)

    # ── phase 1: wait for the lobby ──────────────────────────────────────
    print('[probe] phase 1: waiting for LOBBY')
    nagged = False
    while rec.t < args.lobby_timeout:
        state = rec.step('wait')
        if state is LobbyState.LOBBY:
            break
        if not nagged and rec.t > 8:
            print(f'       currently {state.value} — back out to the lobby '
                  f'when ready', flush=True)
            nagged = True
        sleep_step()
    else:
        print('[probe] never reached the lobby')
        return report(rec, run, entered=False)

    if not game_focused():
        print('[probe] the game is not the foreground window — aborting '
              'before pressing anything')
        return report(rec, run, entered=False)

    # ── phase 2: press F ─────────────────────────────────────────────────
    print('\n[probe] phase 2: pressing F')
    rec.mark('press F')
    if not press_f():
        return report(rec, run, entered=False)
    f_at = rec.t
    clicked = False

    # ── phase 3: follow ──────────────────────────────────────────────────
    print('[probe] phase 3: following the transition')
    entered = False
    deadline = rec.t + args.enter_timeout
    while rec.t < deadline:
        state = rec.step('follow')
        if state is LobbyState.IN_GAME:
            entered = True
            print(f'  {rec.t:6.2f}s  IN_GAME reached, '
                  f'{rec.t - f_at:.2f}s after F', flush=True)
            break
        # F did nothing at all: still sitting in the lobby well after the press
        if (not clicked and not args.no_click_fallback
                and state is LobbyState.LOBBY
                and rec.t - f_at >= args.f_timeout):
            if not game_focused():
                print('[probe] lost focus — not clicking')
                break
            print(f'       F produced no state change in '
                  f'{args.f_timeout:.0f}s; trying the PLAY button')
            rec.mark(f'click PLAY {LOBBY_PLAY_XY}')
            from press.pointer import Pointer
            Pointer('auto').click_at(*LOBBY_PLAY_XY)
            clicked = True
        if not game_focused():
            print(f'  {rec.t:6.2f}s  game lost focus — stopping; this run is '
                  f'not usable data')
            break
        sleep_step()

    # ── phase 4: settle ──────────────────────────────────────────────────
    if entered and args.settle > 0:
        print(f'[probe] phase 4: settling for {args.settle:.0f}s')
        end = rec.t + args.settle
        while rec.t < end:
            rec.step('settle')
            sleep_step()

    return report(rec, run, entered, f_at if not args.watch else None, clicked)


def report(rec, run, entered, f_at=None, clicked=False):
    csv = rec.write()
    print(f'\n[probe] {len(rec.rows)} samples over {rec.rows[-1][0]:.1f}s')
    print(f'[probe] states: {" -> ".join(rec.sequence())}')
    for t, label in rec.marks:
        print(f'[probe] action @ {t:6.2f}s  {label}')
    print(f'[probe] {os.path.relpath(csv)}')

    if entered is None:
        return 0
    if entered:
        print(f'[probe] VERDICT: entered a match via '
              f'{"the PLAY button (F did nothing)" if clicked else "F"}')
        if f_at is not None:
            print(f'[probe]          {rec.rows[-1][0] - f_at:.1f}s from press '
                  f'to settled')
        return 0
    print('[probe] VERDICT: never reached IN_GAME. Check the shots in '
          f'{os.path.relpath(run)} for what it was sitting on.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
