"""Why the ammo counter reads 40/40 standing still and 5 times in a magazine.

    pixi run python tools/probe_ammo_during_fire.py --weapon aug

Fires one magazine and saves EVERY ammo crop with what the detector made of
it, so the failures can be looked at instead of guessed about.

This blocks more than it looks. The counter is the only source for a weapon's
real fire rate, and detector/weapon.WEAPON_RPM -- a hand-typed wiki table -- is
wrong on a third of the roster by up to 17%. A wrong bullet interval is not a
small error, it compounds: the firmware lays each round's compensation on the
nominal grid, so an interval off by x% puts round n's pulse 0.01*x*n rounds
late. sweep.fit_interval exists to measure it and cannot, because with five
readings a magazine there is nothing to fit.

Everything is written whether it read or not, named by poll index and result,
so a contact sheet of the directory answers it at a glance.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))

import cv2
import numpy as np

from detector.weapon import Weapon
from control.focus import ensure_focus

from sweep import Rig, MAX_FIRE_S

OUT_ROOT = os.path.join(ROOT, 'docs', 'ammo_fire')


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapon', default='aug')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--countdown', type=int, default=8)
    ap.add_argument('--keep', type=int, default=200, help='crops to save')
    args = ap.parse_args()

    out = os.path.join(OUT_ROOT, datetime.now().strftime('%m%d_%H%M'))
    print('>>> One magazine, compensation on. Every ammo crop is saved.')
    if not ensure_focus(countdown_s=args.countdown, label='the ammo probe'):
        print('[!] could not focus the game')
        return 1
    time.sleep(0.6)

    rig = Rig(args.sight)
    try:
        w = Weapon()
        w.set('name', args.weapon)
        w.set('posture', 'standing')
        w.set_seq()
        if len(w.t_s):
            rig.mouse.upload_pattern(w.dx_s, w.dy_s, w.t_s, w.bullet_interval_s)
            rig.mouse.set_recoil_enabled(True)
            time.sleep(0.3)
        if not rig.ensure_ads():
            print('[!] could not enter ADS')
            return 1

        # Baseline: the same read, standing still, for the same number of polls.
        idle = [rig.read_ammo(rig.grab()) for _ in range(40)]
        idle_ok = sum(1 for v in idle if v is not None)
        print(f'  idle: {idle_ok}/40 read, values {sorted({v for v in idle if v is not None})}')

        os.makedirs(out, exist_ok=True)
        rows = []
        rig.flush(3)
        t0 = time.perf_counter()
        rig.mouse.click(buttons=0x01, duration_ms=int(MAX_FIRE_S * 1000))
        n_last, still = None, 0
        while time.perf_counter() - t0 < MAX_FIRE_S:
            now = time.perf_counter()
            frame = rig.grab()
            crop = frame['ammo']
            n = rig.read_ammo(frame)
            rows.append({'t_ms': round(1000 * (now - t0), 1), 'n': n})
            if len(rows) <= args.keep:
                cv2.imwrite(os.path.join(
                    out, f'{len(rows)-1:04d}_{"none" if n is None else n}.png'),
                    crop)
            if n == 0:
                still += 1
                if still > 20:
                    break
            n_last = n
        rig.mouse.click(buttons=0x00, duration_ms=0)
        rig.wait_reload()
    finally:
        rig.close()

    ok = [r for r in rows if r['n'] is not None]
    print(f'\n  firing: {len(ok)}/{len(rows)} polls read '
          f'({100.0*len(ok)/max(1,len(rows)):.0f}%)')
    if ok:
        vals = [r['n'] for r in ok]
        print(f'  values seen: {sorted(set(vals), reverse=True)}')
        print(f'  first read at {ok[0]["t_ms"]:.0f} ms, last at {ok[-1]["t_ms"]:.0f} ms')
    print(f'  idle was {idle_ok}/40')
    if len(ok) < 0.5 * len(rows) and idle_ok > 30:
        print('\n  [!] It reads standing still and not while firing. The crops '
              'are in\n      the directory below, named by poll index and '
              'result — compare a\n      "none" against a neighbouring number '
              'and the difference is the bug.')
    with open(os.path.join(out, 'trace.json'), 'w', encoding='utf-8') as fh:
        json.dump({'weapon': args.weapon, 'idle_ok': idle_ok,
                   'polls': rows}, fh, indent=1)
    print(f'\n  -> {os.path.relpath(out, ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
