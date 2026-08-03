"""Does the compensation land on the round it was meant for?

    pixi run python tools/probe_impulse_align.py --weapon aug --bullet 12

Uploads a curve that is ZERO everywhere except one bullet, which gets a large
downward spike, fires a magazine, and finds which round the spike actually
landed on.

This is the check the residual cannot make. fit_curve measures per-bullet sums
on bins anchored to the ammo counter; the firmware plays the pattern on a grid
anchored to the click. Those are two clocks, and the loop closes on whichever
one it is using -- so a curve fitted under a wrong offset is SELF-CONSISTENT
and reports a residual near zero while every pulse sits on the wrong round.
Three separate timing bugs hid behind a clean residual this way:

    RECOIL_LEAD_FRAC = 0.30     26 ms early, wrong sign and wrong units
    36 ms - interval/2          another 44 ms early on the AUG, from assuming
                                the game's recoil is an impulse
    WEAPON_RPM off by 5-17%     a phase error that GROWS with the round number

Nothing here is fitted and nothing is written back. One spike, one magazine,
one number: how many rounds off the compensation is. Zero is right.

The spike must be big enough to stand out against a round's own recoil (~20
counts on an AUG) and small enough not to throw the aim out of the trackable
band. 120 counts is about six rounds' worth in one interval.
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

import numpy as np

from detector.weapon import WEAPON_RPM
from control.focus import ensure_focus
from control.lobby import LobbyControl
from control.spawner import SpawnerControl

from analysis import interval_from_span
from sweep import Rig

OUT_DIR = os.path.join(ROOT, 'docs', 'impulse')


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapon', default='aug')
    ap.add_argument('--bullet', type=int, default=12,
                    help='which round gets the spike. Far enough in that the '
                         'recoil has plateaued, near enough that the view is '
                         'still on texture.')
    ap.add_argument('--spike', type=float, default=120.0, help='counts')
    ap.add_argument('--mags', type=int, default=3)
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--countdown', type=int, default=8)
    ap.add_argument('--no-spawn', action='store_true',
                    help='use whatever is already in hand instead of spawning')
    args = ap.parse_args()

    print(f'>>> {args.weapon}: a {args.spike:.0f}-count spike on round '
          f'{args.bullet}, nothing anywhere else.')
    print('>>> The view will climb — the rest of the curve is deliberately '
          'empty.')
    if not ensure_focus(countdown_s=args.countdown, label='the impulse probe'):
        print('[!] could not focus the game')
        return 1

    # In a match, with a gun, before anything is believed. The first run of
    # this fired a whole magazine at the LOBBY -- another agent had left the
    # game there -- and reported "the spike landed -3.3 rounds off" from a view
    # that never moved at all. Focus was fine, ADS read 100%, and every number
    # printed was garbage.
    with LobbyControl() as lc:
        if not lc.ensure_in_match()['ok']:
            print('[!] not in a match and could not get into one')
            return 1
    time.sleep(1.0)

    if not args.no_spawn:
        # Re-entering the range empties the character, so the gun has to be
        # put back every time. One batched visit: backpack, weapon, extended
        # magazine -- the longer magazine is free measurement here, the spike
        # has to sit inside it.
        sc = SpawnerControl()
        res = sc.give_many(['backpack3', args.weapon, 'ext_ar'])
        print(f"  spawned in {res['clicks']} clicks"
              + ('' if res['ok'] else f" — {res['error']}"))
        # Closed on the way out: the panel covers the HUD the magazine
        # counter is read from. give_many opens it itself.
        sc.ensure_panel(False)
        time.sleep(0.8)

    rig = Rig(args.sight)
    rows = []
    try:
        nominal = 60.0 / WEAPON_RPM.get(args.weapon, 600)
        n_pts = args.bullet + 2
        dy = [0.0] * n_pts
        dy[args.bullet] = args.spike
        t_s = [k * nominal for k in range(n_pts)]
        rig.mouse.upload_pattern([0.0] * n_pts, dy, t_s, nominal)
        rig.mouse.set_recoil_enabled(True)
        time.sleep(0.3)

        mag_size = None
        for i in range(args.mags):
            if not rig.ensure_ads():
                print(f'  mag {i}: could not enter ADS')
                break
            if mag_size is None:
                mag_size = rig.magazine_size()
                print(f'  magazine holds {mag_size}')
                if not mag_size:
                    print('[!] no magazine reading — is a gun in hand? '
                          'Nothing measured here would mean anything.')
                    return 1
            rec, fire_s, steps, fire_end, first_shot, ads = rig.fire_magazine()
            if steps == 0 or first_shot is None:
                print(f'  mag {i}: nothing fired')
                rig.wait_reload()
                continue
            iv, _ = interval_from_span(first_shot, fire_end, mag_size)
            iv = iv or nominal
            res = rec.finish()
            ts = np.asarray(res.ts, dtype=float) - first_shot
            dyv = np.asarray(res.dy, dtype=float)
            oor = np.asarray(res.out_of_range, dtype=bool)
            if len(oor) != len(dyv):
                oor = np.zeros(len(dyv), dtype=bool)
            counts = np.where(oor, np.nan, dyv / rig.K)

            # Per-round totals on the MEASURED interval. The spike is the one
            # round that moves the view down while every other round moves it
            # up, so it needs no model of the recoil to find -- just the
            # minimum.
            good = ~np.isnan(counts)
            cum = np.cumsum(np.where(good, counts, 0.0))
            tt, cc = ts[good], cum[good]
            nb = min(mag_size or 0, args.bullet + 4) or args.bullet + 4
            edges = np.arange(nb + 1) * iv
            per = np.diff(np.interp(edges, tt, cc, left=0.0,
                                    right=float(cc[-1])))
            landed = int(np.argmin(per))
            print(f'  mag {i}: ads {ads:.0%}, interval {1000*iv:.1f} ms, '
                  f'spike landed on round {landed} '
                  f'(wanted {args.bullet}, off by {landed - args.bullet:+d})')
            print('        ' + ' '.join(
                f'{k}:{v:+.0f}' for k, v in enumerate(per[:nb])))
            rows.append({'mag': i, 'interval_s': iv, 'landed': landed,
                         'wanted': args.bullet, 'ads_frac': ads,
                         'per_round': [float(v) for v in per]})
            rig.wait_reload()
    finally:
        rig.mouse.set_recoil_enabled(True)
        rig.close()

    if not rows:
        print('[!] nothing measured')
        return 1
    off = [r['landed'] - r['wanted'] for r in rows]
    print(f'\n{args.weapon}: the spike landed {np.mean(off):+.1f} rounds from '
          f'where it was commanded  (n={len(off)}, {off})')
    if all(o == 0 for o in off):
        print('  On the round. The offset and the interval are both right.')
    else:
        iv = float(np.mean([r['interval_s'] for r in rows]))
        print(f'  Off by {np.mean(off)*1000*iv:+.0f} ms. A CONSTANT offset '
              f'means\n  RECOIL_FIRE_DELAY_MS is wrong; an error that grows '
              f'with the round\n  number means the bullet interval is. Run '
              f'this at two --bullet values\n  to tell them apart.')

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR,
                        f'{args.weapon}_b{args.bullet}_{datetime.now():%m%d_%H%M}.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump({'weapon': args.weapon, 'spike': args.spike,
                   'K': rig.K, 'mags': rows}, fh, indent=2)
    print(f'  raw -> {os.path.relpath(path, ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
