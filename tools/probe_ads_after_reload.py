"""Why does the sight not come back after an auto-reload?

    pixi run python tools/probe_ads_after_reload.py --mags 4

WHAT IS BROKEN. PUBG drops ADS when the magazine runs dry and it reloads
itself, so every measurement loop re-enters ADS between magazines. On
2026-08-05 that failed repeatedly — `could not re-enter ADS after reload` — and
it was the single largest source of lost magazines that day: it took both vss
cells outright and discarded mp5k magazines at 61..79% ADS. A discarded
magazine is the good outcome; the bad one is a magazine fired from the hip and
analysed with the scoped K, which reads about 3x high.

`GunDriver.ensure_ads` clicks up to three times and watches each for
ADS_WATCH_S = 2.5 s. Three clicks, 7.5 s of watching, and still no sight. Two
explanations survive that, they need OPPOSITE fixes, and nothing on record
separates them:

    A  TOO EARLY.  The game ignores the right button while the reload
       animation plays, so all three clicks land inside it and are eaten.
       Waiting longer per click cannot help; the fix is to click LATER.
    B  TOO SLOW.   The scope-in animation is slower right after a magazine
       (docs/game_quirks.md: "连打 5 梭后必失败, 只打 2 梭时能侥幸通过"), so
       2.5 s is simply short. The fix is to watch longer, not to click later.

And a third that is worse than either, because it looks like both: right click
is a TOGGLE, so an odd number of effective clicks leaves the sight UP and an
even number leaves it DOWN. Three clicks that all register put it back where
it started while every read says "not scoped". `ensure_ads`'s own docstring
already names this loop — "clicking again while the animation is still playing
just toggles back out".

THE DISCRIMINATOR IS WHETHER THE SCREEN MOVES, not whether it arrives.
`AdsDetector.score_crop` is continuous — how strongly a crosshair is present —
so this samples it right through the reload and the clicks:

    score moves after a click, then settles wrong   -> the click REGISTERED
                                                       (B, or the toggle race)
    score does not move at all after a click        -> the click was EATEN (A)

That is the same shape as the posture trace: ask a signal the thing under test
cannot reach, and let it say which of two stories happened. Here the signal is
the crosshair itself, which is present-or-absent rather than a
readiness-to-act, so it cannot be fooled by the button state.

Output: docs/ads/reload/<stamp>/{trace.jsonl, summary}
"""
import argparse
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from calibration.sweep import Rig                                  # noqa: E402
from control.inventory import InventoryControl                     # noqa: E402
from control.session import ensure_ready                           # noqa: E402
from control.spawner import SpawnerControl                         # noqa: E402
from control.stock import ensure_weapon_in_hand                    # noqa: E402
from detector.ads_detector import THRESHOLD                        # noqa: E402
from press.pico_mouse import get_mouse, other_agents               # noqa: E402

OUT_ROOT = os.path.join(ROOT, 'docs', 'ads', 'reload')
SAMPLE_HZ = 60
# How long to keep sampling after the magazine empties. Long enough to contain
# the reload, several clicks and the settle after the last one.
WATCH_S = 8.0
# When to try the button, as seconds after the counter stopped falling. Spread
# on purpose: if A is right the early ones are eaten and the late ones are not,
# and the crossover is the number ensure_ads needs.
CLICK_AT = (0.30, 1.20, 2.40, 4.00)
# A score change this large between consecutive samples is the screen doing
# something. Measured floor: the crosshair score's frame-to-frame jitter while
# nothing happens. Reported by --selftest against a still trace.
MOVE_MIN = 4.0


def _fmt(v):
    return '  --' if v is None else f'{v:6.1f}'


class Trace:
    """Samples the crosshair score continuously, and marks the clicks."""

    def __init__(self, rig):
        self.rig = rig
        self.rows = []

    def watch(self, seconds, clicks=(), mouse=None, label=''):
        """Sample for `seconds`, issuing a right-click at each offset in
        `clicks`. -> rows"""
        rows = []
        pending = list(clicks)
        t0 = time.perf_counter()
        prev = None
        while True:
            el = time.perf_counter() - t0
            if el > seconds:
                break
            if pending and el >= pending[0] and mouse is not None:
                mouse.click(buttons=0x02, duration_ms=60)
                clicked = pending.pop(0)
            else:
                clicked = None
            frame = self.rig.grab()
            s = float(self.rig.ads_det.score_crop(frame['crosshair']))
            rows.append({
                'label': label, 'ms': round(el * 1000, 1), 'score': round(s, 2),
                'scoped': s < THRESHOLD,
                'd': None if prev is None else round(s - prev, 2),
                'click': clicked,
            })
            prev = s
            time.sleep(max(0.0, 1.0 / SAMPLE_HZ
                           - (time.perf_counter() - t0 - el)))
        self.rows += rows
        return rows


def verdict(rows):
    """Did each click move the screen? -> [(click_ms, moved, settle_ms)]

    `moved` is the question this probe exists to answer. `settle_ms` is when
    the score stopped changing afterwards, which is the number a watch timeout
    would have to clear IF the click registered at all.
    """
    out = []
    clicks = [r for r in rows if r['click'] is not None]
    for i, c in enumerate(clicks):
        end = clicks[i + 1]['ms'] if i + 1 < len(clicks) else rows[-1]['ms']
        after = [r for r in rows if c['ms'] <= r['ms'] < end]
        moved = [r for r in after
                 if r['d'] is not None and abs(r['d']) >= MOVE_MIN]
        settle = moved[-1]['ms'] - c['ms'] if moved else None
        out.append((c['ms'], bool(moved), settle,
                    after[-1]['scoped'] if after else None))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', default='m416')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--mags', type=int, default=4)
    a = ap.parse_args()

    busy = other_agents()
    if busy:
        print('another agent holds the game / Pico — not taking focus:')
        for b in busy:
            print(f'  {b}')
        return 1
    if not ensure_ready(label='probe_ads_after_reload')['ok']:
        print('not ready')
        return 1

    with SpawnerControl() as sc:
        ac = InventoryControl(verbose=False)
        try:
            slot = ensure_weapon_in_hand(ac, sc, weapon=a.weapon)
        finally:
            ac.close()
    if slot is None:
        print('no weapon in hand')
        return 1

    stamp = time.strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(OUT_ROOT, stamp)
    os.makedirs(out_dir, exist_ok=True)

    rig = Rig(a.sight)
    tr = Trace(rig)
    m = get_mouse()
    try:
        for i in range(a.mags):
            # Start each round from a KNOWN sight state, through the driver
            # that watches it, so what follows is about the reload and not
            # about where the toggle happened to be.
            if not rig.gun.ensure_ads():
                print(f'  [{i}] could not get the sight up to begin with — '
                      f'that is a different bug from the one under test')
                break
            print(f'  [{i}] firing a magazine dry')
            rig.fire.fire_magazine()
            rows = tr.watch(WATCH_S, clicks=CLICK_AT, mouse=m,
                            label=f'mag{i}')
            print(f'      {"click@ms":>9} {"moved":>6} {"settle":>7} {"scoped":>7}')
            for ms, moved, settle, scoped in verdict(rows):
                print(f'      {ms:9.0f} {str(moved):>6} {_fmt(settle):>7} '
                      f'{str(scoped):>7}')
    finally:
        rig.close()

    with open(os.path.join(out_dir, 'trace.jsonl'), 'w',
              encoding='utf-8') as f:
        for r in tr.rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    all_v = [v for lab in dict.fromkeys(r['label'] for r in tr.rows)
             for v in verdict([r for r in tr.rows if r['label'] == lab])]
    if all_v:
        eaten = [v for v in all_v if not v[1]]
        print(f'\n=== {len(all_v)} clicks: {len(eaten)} moved NOTHING, '
              f'{len(all_v) - len(eaten)} registered ===')
        by_t = sorted({round(v[0] / 100) * 100 for v in all_v})
        for t in by_t:
            grp = [v for v in all_v if round(v[0] / 100) * 100 == t]
            reg = sum(1 for v in grp if v[1])
            print(f'  clicked ~{t:5.0f} ms after the magazine: '
                  f'{reg}/{len(grp)} registered')
        print('\nA click that moved nothing was EATEN — press later, not '
              'longer.\nA click that moved the score and still ended un-scoped '
              'is the TOGGLE\nrace, and pressing again is what causes it.')
    print(f'\n{len(tr.rows)} samples -> {out_dir}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
