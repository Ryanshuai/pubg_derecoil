"""How long does a UI screen actually take to open and close? Needs the game.

    pixi run python tools/probe_toggle_latency.py --n 6
    pixi run python tools/probe_toggle_latency.py --screen tab

Four places in this repo press a toggle key and then sleep a constant, none of
them ever measured: control/inventory.py's TAB_TOGGLE_S = 0.45 (twice per hold(), so
0.9 s a weapon switch), drive_screen's open_wait=0.45 / close_wait=0.40, and
spawner's PANEL_SETTLE_S = 0.5. This is what replaced the first of them with
a poll; the numbers are in TAB_TOGGLE_TIMEOUT's comment there.

Polling the open-check instead costs 6 ms a pass (win32_cap is ~6 ms of fixed
GDI overhead almost regardless of size: 41x18 measures 6.0 ms, 475x700
measures 6.9 ms). So any constant above ~50 ms is worth replacing with a poll,
and this measures what to replace it with.

TWO instants are timed, because they are not the same and the gap is the whole
reason sync() re-reads:

  t_open    the cheap check flips -- the anchor glyph is on screen
  t_ready   a full read gives the SAME answer twice running -- the panel has
            stopped drawing

The spawner draws its columns progressively and the three button glyphs finish
before the last column does, which surfaced once as "category col3_row06 does
not exist" for a backpack plainly on screen. If t_ready >> t_open here, that
is that bug, quantified.

t=0 is the moment key() is called, so the numbers include the CDC write and
the firmware's report -- which is what a caller's sleep has to cover.
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

import cv2
import numpy as np

from config import SCREEN_H, SCREEN_W
from control.focus import ensure_focus, focus_keeper
from control.lobby import LobbyControl
from detector.cropper import capture_screen, win32_cap
from detector.spawner_detector import ICON_BOX, SpawnerDetector
from detector.spawner_layout import find_menu
from detector.tab_items import TabGrabber
from press.pico_mouse import HID_KEY_COMMA, HID_KEY_TAB, get_mouse

POLL_TIMEOUT = 3.0      # give up on a transition
QUIET = 0.35            # between the two halves of a cycle, so neither leaks
EXPECTED_ROWS = {1: 10, 2: 5, 3: 6}

# ICON_BOX (imported) is the one rect covering all three spawner button glyphs
# plus their search pad, so the open-check costs one small grab instead of a
# 70 ms full screen. This file used to compute it inline from SPAWNER_ICON_*,
# which came to the same numbers but without anchor_box's max(0, ...) clamp.


class TabScreen:
    """Tab inventory, read as the two WEAPON panels only.

    Readiness here is "the gun names and the ten attachment slots can be
    read", not "the whole inventory has drawn". That is what every caller of
    hold()/equip() is actually waiting for, and it is one screen block
    (tab_blocks()['right']) instead of two -- the 库存/附近 lists are a
    separate question with a separate cost.
    """
    name, key = 'tab', HID_KEY_TAB

    def __init__(self):
        from control.inventory import InventoryControl
        from detector.tab_items import tab_blocks
        self.ac = InventoryControl(verbose=False)
        self.ac.grabber.close()
        self.ac.grabber = TabGrabber(only=('right',))
        self.block = tab_blocks()['right']
        self._scratch = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)

    def is_open(self):
        return bool(self.ac.tab_open())

    def snap(self):
        """Just the pixels — no detection. This is the shot being raced."""
        y, x, h, w = self.block
        return self.ac.grabber.grab()[y:y + h, x:x + w].copy()

    def read_snap(self, crop):
        """Evaluate a stored snap exactly as full_read would have."""
        y, x, h, w = self.block
        self._scratch[y:y + h, x:x + w] = crop
        guns = self.ac._read_guns(self._scratch)
        self.ac.guns = guns
        slots = self.ac._slot_states(self._scratch)
        return (guns[1], guns[2],
                tuple((g, s, slots[g][s]) for g in (1, 2) for s in sorted(slots[g])))

    def full_read(self):
        """(gun names, every slot's template) -- hashable, so == means stable."""
        frame = self.ac._frame()
        guns = self.ac._read_guns(frame)
        self.ac.guns = guns
        slots = self.ac._slot_states(frame)
        return (guns[1], guns[2],
                tuple((g, s, slots[g][s]) for g in (1, 2) for s in sorted(slots[g])))

    @staticmethod
    def degenerate(ref):
        """No names, no attachments: this reading matches a blank screen too.

        Raced against, it scores a confident 24/24 win that never happened --
        which is exactly what one lost-focus run produced before this existed.
        """
        return ref is None or (not ref[0] and not ref[1]
                               and not any(s[2] for s in ref[2]))

    def close(self):
        self.ac.close()


class SpawnerScreen:
    """Training-range item spawner. Comma works anywhere in the range."""
    name, key = 'spawner', HID_KEY_COMMA

    def __init__(self):
        self.det = SpawnerDetector()
        self._buf = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)

    def is_open(self):
        y, x, h, w = ICON_BOX
        self._buf[y:y + h, x:x + w] = win32_cap(ICON_BOX)
        return bool(self.det.classify(self._buf))

    def full_read(self):
        """Which columns have drawn, and how many rows each shows."""
        menu = find_menu(capture_screen(), verbose=False)
        if not menu:
            return None
        return tuple(sorted((c, len(rows)) for c, rows in menu.items()))

    @staticmethod
    def degenerate(ref):
        return not ref

    def close(self):
        pass


def poll_until(pred, timeout=POLL_TIMEOUT):
    """-> (seconds, passes) from now until pred() is true. None on timeout."""
    t0 = time.perf_counter()
    passes = 0
    while time.perf_counter() - t0 < timeout:
        passes += 1
        if pred():
            return time.perf_counter() - t0, passes
    return None, passes


def poll_stable(read, timeout=POLL_TIMEOUT):
    """-> (seconds until the screen had settled, the answer it settled on).

    Settled means two consecutive reads agree. The time returned is when the
    FIRST of the two was taken: by then the screen already held that value,
    and charging the caller for the confirming read would inflate every number
    by one read's cost -- 70 ms for the spawner, which is most of the answer.
    """
    t0 = time.perf_counter()
    last, last_t = None, None
    while time.perf_counter() - t0 < timeout:
        t = time.perf_counter()
        now = read()
        if now is not None and now == last:
            return last_t - t0, now
        last, last_t = now, t
    return None, last


def race(screen, mouse, ref, timeout=2.0):
    """Press the key that closes the screen, then read as fast as possible.

    Answers "can a shot beat the switch": how many reads still return `ref`
    -- the exact reading taken while the screen was up and settled -- after
    the key is already on its way, and when the last of them landed.

    Comparing against `ref` rather than a "looks valid" heuristic is what
    makes this exact: a half-drawn or already-gone panel cannot accidentally
    equal a reading that was confirmed stable a moment earlier.
    """
    t0 = time.perf_counter()
    mouse.key(screen.key, 60)
    hits, last, reads = 0, None, 0
    while time.perf_counter() - t0 < timeout:
        t = time.perf_counter() - t0
        got = screen.full_read()
        reads += 1
        if got == ref:
            hits, last = hits + 1, t
        elif hits:
            break          # it was there, now it is not: the switch happened
    return {'reads': reads, 'hits': hits, 'last_hit': last,
            'missed_at': time.perf_counter() - t0}


def race_grabs(screen, mouse, ref, n=24, save_to=None):
    """Same race, but capture only — detection happens afterwards.

    race() re-reads through the detectors, so each pass costs ~32 ms and only
    one lands. The question is about the SHOT though, and a shot is 7 ms. So
    this fires raw grabs back to back, stores them, and scores them once the
    transition is over: how many screenshots actually beat the switch.
    """
    frames = []
    t0 = time.perf_counter()
    mouse.key(screen.key, 60)
    for _ in range(n):
        t = time.perf_counter() - t0
        frames.append((t, screen.snap()))
    span = time.perf_counter() - t0
    scored = [(t, screen.read_snap(f) == ref) for t, f in frames]
    good = [t for t, ok in scored if ok]
    if save_to:
        os.makedirs(save_to, exist_ok=True)
        for i, ((t, img), (_, ok)) in enumerate(zip(frames, scored)):
            cv2.imwrite(os.path.join(
                save_to, f'{i:02d}_{t * 1000:04.0f}ms_'
                         f'{"same" if ok else "changed"}.png'), img)
    # Only a leading run counts: a match after the panel is gone would mean
    # the reading is not specific enough, and should be shown, not smoothed.
    lead = 0
    for _, ok in scored:
        if not ok:
            break
        lead += 1
    return {'n': len(scored), 'span': span, 'good': len(good), 'lead': lead,
            'last_good': max(good) if good else None,
            'per_grab': span / max(1, len(frames))}


def cycle(screen, mouse):
    """One open + close. -> dict of measured instants, None values on timeout."""
    rec = {}
    t0 = time.perf_counter()
    mouse.key(screen.key, 60)
    rec['open'], rec['open_passes'] = poll_until(screen.is_open)
    if rec['open'] is None:
        return rec
    # poll_stable starts its own clock, so add the time already spent waiting
    # for the anchor to put both instants on the same t=0: the key press.
    rec['ready'], rec['shape'] = poll_stable(screen.full_read)
    if rec['ready'] is not None:
        rec['ready'] += rec['open']
    rec['total'] = time.perf_counter() - t0

    time.sleep(QUIET)
    if screen.degenerate(rec.get('shape')):   # nothing worth racing against
        t0 = time.perf_counter()
        mouse.key(screen.key, 60)
        rec['close'], rec['close_passes'] = poll_until(
            lambda: not screen.is_open())
        time.sleep(QUIET)
        return rec

    r = race(screen, mouse, rec['shape'])
    rec['race'] = r
    rec['last_hit'] = r['last_hit']
    # race() sent the close key and stopped the moment the reading changed;
    # the anchor glyph can outlive that, so keep polling and put the total
    # back on the key-press clock.
    tail, rec['close_passes'] = poll_until(lambda: not screen.is_open())
    rec['close'] = None if tail is None else r['missed_at'] + tail
    time.sleep(QUIET)
    return rec


def report(name, key, recs, unit='s'):
    vals = [r[key] for r in recs if r.get(key) is not None]
    lost = len(recs) - len(vals)
    if not vals:
        print(f'  {name:<26} no measurement ({lost} timed out)')
        return None
    med = statistics.median(vals)
    print(f'  {name:<26} median {med * 1000:6.0f} ms   '
          f'min {min(vals) * 1000:5.0f}   max {max(vals) * 1000:5.0f}'
          + (f'   [{lost} timed out]' if lost else ''))
    return med


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--n', type=int, default=6, help='open/close cycles each')
    ap.add_argument('--screen', choices=('tab', 'spawner'), action='append',
                    help='default: both')
    ap.add_argument('--countdown', type=int, default=4)
    ap.add_argument('--grab-race', type=int, default=0, metavar='N',
                    help='also race raw captures against the close, N times')
    ap.add_argument('--save', metavar='DIR',
                    help='write every raced frame here, named by its age and '
                         'whether it still read the same')
    args = ap.parse_args()

    if not ensure_focus(countdown_s=args.countdown, label='the toggle probe'):
        return 1
    time.sleep(0.6)

    with LobbyControl(verbose=False) as lc:
        st = lc.state()
        if not st.playable:
            print(f'not in a match ({st.value}) — walking back in ...')
            rec = lc.ensure_in_match()
            if not rec['ok']:
                print(f'[!] could not get into the range: {rec["error"]}')
                return 1
            print(f'  in, after {rec["elapsed"]:.0f}s')
            time.sleep(1.0)

    mouse = get_mouse()
    wanted = args.screen or ['tab', 'spawner']
    screens = {'tab': TabScreen, 'spawner': SpawnerScreen}
    keeper = focus_keeper()
    out = {}

    for name in wanted:
        screen = screens[name]()
        try:
            # Start from closed, whatever it is now: a toggle applied to an
            # unknown state lands on the opposite of what was wanted.
            for _ in range(3):
                if not screen.is_open():
                    break
                mouse.key(screen.key, 60)
                time.sleep(0.8)
            if screen.is_open():
                print(f'[!] {name} would not close; skipping')
                continue

            t0 = time.perf_counter()
            screen.is_open()
            probe_ms = (time.perf_counter() - t0) * 1000

            # Pre-flight: what does this screen read as when it is settled?
            # The race compares against exactly this, so a reading with no
            # content in it would match a blank screen too and report a win
            # that never happened.
            mouse.key(screen.key, 60)
            time.sleep(0.9)
            ref = screen.full_read()
            t0 = time.perf_counter()
            screen.full_read()
            read_ms = (time.perf_counter() - t0) * 1000
            mouse.key(screen.key, 60)
            time.sleep(0.9)

            print(f'\n=== {name} ===  open-check {probe_ms:.1f} ms/pass, '
                  f'full read {read_ms:.1f} ms/pass')
            print(f'  settled reading: {ref}')
            if screen.degenerate(ref):
                print('  [!] this screen reads as nothing — the race below '
                      'cannot tell "still up" from "already gone". Check '
                      'focus, and that a gun is kitted.')
                continue
            recs = []
            for i in range(args.n):
                if not keeper.ok(f'{name} cycle {i + 1}'):
                    return 1
                r = cycle(screen, mouse)
                recs.append(r)
                rc = r.get('race') or {}
                print(f'  {i + 1}: open {_ms(r.get("open"))}  '
                      f'ready {_ms(r.get("ready"))}  close {_ms(r.get("close"))}'
                      f'  |  after the close key: {rc.get("hits", 0)}'
                      f'/{rc.get("reads", 0)} reads still landed, last at '
                      f'{_ms(r.get("last_hit"))}')
            print()
            out[name] = {
                'open': report('key -> anchor on screen', 'open', recs),
                'ready': report('key -> done drawing', 'ready', recs),
                'close': report('key -> anchor gone', 'close', recs),
                'last_hit': report('key -> LAST good read', 'last_hit', recs),
            }
            hits = [(r.get('race') or {}).get('hits', 0) for r in recs]
            print(f'  {"reads that beat the switch":<26} '
                  f'median {statistics.median(hits):.0f}, min {min(hits)}, '
                  f'max {max(hits)}')

            if args.grab_race and hasattr(screen, 'snap'):
                print(f'\n  --- capture only, detection afterwards ---')
                gs = []
                for i in range(args.grab_race):
                    if not keeper.ok(f'{name} grab race {i + 1}'):
                        return 1
                    for _ in range(4):        # back to open, for the next race
                        if screen.is_open():
                            break
                        mouse.key(screen.key, 60)
                        time.sleep(0.5)
                    time.sleep(0.4)
                    if not screen.is_open():
                        print(f'  [!] {name} would not reopen — stopping')
                        break
                    ref2 = screen.full_read()
                    if screen.degenerate(ref2):
                        print(f'  [!] reference reads as nothing ({ref2}); '
                              f'refusing to race against it')
                        break
                    g = race_grabs(screen, mouse, ref2,
                                   save_to=(os.path.join(args.save, f'race{i + 1}')
                                            if args.save else None))
                    gs.append(g)
                    print(f'  {i + 1}: {g["lead"]}/{g["n"]} shots still showed '
                          f'the panel, last at {_ms(g["last_good"])}, '
                          f'{g["per_grab"] * 1000:.1f} ms/grab')
                    poll_until(lambda: not screen.is_open())
                    time.sleep(QUIET)
                lead = [g['lead'] for g in gs]
                last = [g['last_good'] for g in gs if g['last_good'] is not None]
                print(f'\n  shots that beat the switch: median '
                      f'{statistics.median(lead):.0f}, min {min(lead)}, '
                      f'max {max(lead)}')
                if last:
                    print(f'  last good shot at:          median '
                          f'{statistics.median(last) * 1000:.0f} ms, '
                          f'max {max(last) * 1000:.0f} ms')
        finally:
            screen.close()

    print('\n=== what the constants should be ===')
    for name, m in out.items():
        if m['ready'] and m['close']:
            print(f'  {name:<9} open  {m["ready"] * 1000:4.0f} ms measured  '
                  f'-> poll, or sleep {_round_up(m["ready"] * 1.5)}')
            print(f'  {" " * 9} close {m["close"] * 1000:4.0f} ms measured  '
                  f'-> poll, or sleep {_round_up(m["close"] * 1.5)}')
    return 0


def _ms(v):
    return '  --  ' if v is None else f'{v * 1000:4.0f}ms'


def _round_up(v):
    return round(v + 0.049, 1)


if __name__ == '__main__':
    sys.exit(main())
