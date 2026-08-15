"""One bullet, 800 ms of quiet after it. Does the camera come back down?

Every recoil number in this repo is an increment over one FIRE INTERVAL --
83 ms for the aug -- because every magazine ever fired was a full magazine.
That makes one thing structurally unobservable:

    shot 1 kicks the view UP by A, then part of it falls back by B
    shot 2 arrives at 83 ms and its own rise CANCELS that fall
    the interval increment is A - B, and B never appears anywhere

If that is what happens, then the bullet -- which leaves within a few ms of
the shot, at the PEAK -- is displaced by A, while the curve is fitted to
A - B. No amount of correct fitting on A - B can compensate A, which is
exactly the shape of the operator's report: the first hole sits 2-3x further
out than the rest while the camera says the first shot is the SMALLEST.

Nothing in the store can settle it. The burst data is monotone over the first
83 ms (1.22 -> 8.52 counts, aug), which is consistent with a fall that starts
after 60 ms and is masked by shot 2 -- and equally consistent with no fall at
all. The two differ only in a window that has never been recorded.

SO: single fire mode, one trigger pull, and then leave it alone.

  - `ensure_fire_mode(want='single')` presses B and WATCHES the HUD until it
    reads single; it does not press and hope. B is a cycle.
  - the ammo counter is read before and after and must differ by EXACTLY 1.
    Anything else and the tap is discarded -- "one round" is a claim about
    what happened, and it needs a witness that is not the command we sent.
  - compensation is disarmed and the firmware is read back to confirm it,
    so y_obs IS y_true and no curve enters the arithmetic.

⚠ AND SINGLE MODE IS NOT ASSUMED TO STAND IN FOR FULL AUTO. The mg3's two
automatic modes are 1.50x apart in cyclic rate and are stored as two
different y_true(t); this repo has already paid for treating one mode's
measurement as another's. The first shot has no predecessor in either mode,
which is an argument, not a measurement -- so full-auto magazines are fired
in the SAME run, interleaved in blocks, and the two arms must agree over the
first interval. The fitter cannot arrange that; nothing here knows which arm
a reading came from until after it is taken.

WHAT THE VERDICT IS, fixed before the run:

  peak > value at 300 ms, by more than 3 sem   ->  THERE IS A BOUNCE, and its
                                                   height A is what the first
                                                   round must be compensated by
  peak == end within noise                     ->  no bounce; the first shot
                                                   really is small, and what
                                                   moves the bullet is not on
                                                   this screen
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from capture.cropper import DXGISyncGrabber                      # noqa: E402
from control.session import ensure_ready                         # noqa: E402

OUT_DIR = os.path.join('calibration', 'artifacts', 'single_shot')
# Long enough that the trigger is unambiguously one pull in single mode, and
# that the capture runs well past any plausible settle. fire_magazine_timed
# derives hold = 1.5 * this and captures to hold + 0.35 s.
TAP_INTERVAL_S = 0.30


def grid_mean(runs, grid):
    """Mean and sem of cumulative counts across taps, on a common time grid.

    Each tap is interpolated onto the grid FIRST and averaged after, because
    the taps do not share sample instants -- averaging raw frames would mix
    different times into one bin and flatten exactly the peak being looked for.
    """
    rows = [np.interp(grid, t, y) for t, y in runs
            if len(t) > 2 and t[-1] >= grid[-1]]
    if not rows:
        return None, None, 0
    a = np.asarray(rows)
    return a.mean(0), a.std(0) / np.sqrt(len(a)), len(a)


def cumulative(tracker, out, K):
    """(t, cumulative counts) for one capture, from the stored patches."""
    from calibration import collect_timed as CT
    t, dy_px, human_dy, _oor = CT.measure(tracker, out['t'], out['patches'])
    # dy_px describes an INTERVAL, so its cumulative sum lands on the LATER
    # timestamp. There are len(t) - 1 of them; returning them against t[:-1]
    # would put every sample one frame early, which on a 10 ms grid is most of
    # the window this probe exists to resolve.
    t = np.asarray(t, float)[1:]
    d = np.asarray(dy_px, float) - np.asarray(human_dy, float) * K
    if d.size != t.size:
        raise ValueError(f'{d.size} displacements against {t.size} instants')
    return t, np.cumsum(np.nan_to_num(d)) / K


def report(label, runs, grid, K):
    mu, sem, n = grid_mean(runs, grid)
    if mu is None:
        print(f'  {label}: nothing usable')
        return None
    print()
    print(f'  {label}  (n={n})')
    print('  %8s %10s %8s' % ('t ms', 'counts', 'sem'))
    for i, x in enumerate(grid):
        if i % 2:
            continue
        print('  %8.0f %10.2f %8.2f' % (x * 1000, mu[i], sem[i]))
    j = int(np.argmax(mu))
    end = mu[-1]
    drop = mu[j] - end
    print()
    print('    peak %.2f counts at t=%.0f ms   |   at %.0f ms: %.2f   |   '
          'fall %.2f (sem %.2f)'
          % (mu[j], grid[j] * 1000, grid[-1] * 1000, end, drop, sem[j]))
    if drop > 3 * max(sem[j], 1e-9):
        print('    -> THERE IS A BOUNCE. The bullet leaves at the peak, so the '
              'first round needs')
        print('       %.1f counts of compensation, not the %.1f the interval '
              'increment shows.' % (mu[j], end))
    else:
        print('    -> no bounce beyond noise. The rise is what it is, and the '
              'first shot is small.')
    return mu, sem, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', default='aug')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--taps', type=int, default=25)
    ap.add_argument('--block', type=int, default=5,
                    help='taps per block; one full-auto control magazine '
                         'between blocks')
    ap.add_argument('--no-control', action='store_true',
                    help='skip the full-auto arm (then nothing checks that '
                         'single mode stands in for it)')
    ap.add_argument('--aim-below', type=float, default=0.40)
    ap.add_argument('--upto-ms', type=float, default=500)
    a = ap.parse_args()

    if not ensure_ready(label='the single-shot probe')['ok']:
        return 1

    from calibration.sweep import Rig
    from calibration import collect_timed as CT, rpm_store
    from control.inventory import InventoryControl
    from control.spawner import SpawnerControl
    from control.stock import ensure_weapon_in_hand

    rig = Rig(a.sight, prefer_dxgi=False)
    grabber = None
    taps, bursts, log = [], [], []
    try:
        ac, sc = InventoryControl(), SpawnerControl()
        if not ensure_weapon_in_hand(ac, sc, weapon=a.weapon):
            print(f'could not get a {a.weapon} in hand')
            return 1
        if not CT.ensure_sight(ac, sc, 1, a.weapon, a.sight):
            return 1
        if not rig.fire.disarm():
            print('the firmware would not confirm compensation is off')
            return 1

        entry = (rpm_store.load() or {}).get(a.weapon) or {}
        interval_s = float(entry.get('interval_ms', 85.0)) / 1000.0
        grabber = DXGISyncGrabber(rig.tracker.regions())
        os.makedirs(OUT_DIR, exist_ok=True)

        done = 0
        while done < a.taps:
            mode = rig.gun.ensure_fire_mode(a.weapon, want='single')
            if mode != 'single':
                print(f'  the HUD reads {mode!r}, not single — refusing to fire')
                return 1
            rig.fire.top_up(weapon=a.weapon)
            if not CT.aim_and_scope(rig, 'standing', below_frac=a.aim_below):
                print('  could not aim/scope — stopping')
                break
            for _ in range(min(a.block, a.taps - done)):
                before = rig.fire.read_ammo()
                out = rig.fire.fire_magazine_timed(grabber, 1, TAP_INTERVAL_S)
                after = rig.fire.read_ammo()
                done += 1
                # The witness, not the command: "one round" has to be something
                # the game says, or a swallowed click reads as a shot with no
                # recoil and drags the mean down without a word.
                if before is None or after is None or before - after != 1:
                    print(f'    tap {done}: ammo {before} -> {after} — '
                          f'not exactly one round, DISCARDED')
                    continue
                t, y = cumulative(rig.tracker, out, rig.K)
                taps.append((t, y))
                log.append(('single', time.time(), len(t)))
                print(f'    tap {done}: {len(t)} frames, '
                      f'{len(t) / (t[-1] - t[0]):.0f} fps, '
                      f'y(300ms)={np.interp(0.300, t, y):+.2f} counts')
            if a.no_control or done >= a.taps:
                continue
            mode = rig.gun.ensure_fire_mode(a.weapon, want='full')
            if mode != 'full':
                print(f'  control arm: HUD reads {mode!r} — skipping this one')
                continue
            mag_size, _ = rig.fire.top_up(weapon=a.weapon)
            if not CT.aim_and_scope(rig, 'standing', below_frac=a.aim_below):
                break
            out = rig.fire.fire_magazine_timed(grabber, mag_size, interval_s)
            t, y = cumulative(rig.tracker, out, rig.K)
            bursts.append((t, y))
            log.append(('full', time.time(), len(t)))
            print(f'    control magazine: {len(t)} frames, '
                  f'y(83ms)={np.interp(0.08313, t, y):+.2f} counts')
            rig.fire.wait_reload(expect=mag_size, weapon=a.weapon)

        grid = np.arange(-0.060, a.upto_ms / 1000.0 + 1e-9, 0.010)
        print('\n' + '=' * 68)
        print('arm order:', ''.join('S' if k == 'single' else 'F'
                                    for k, _, _ in log))
        s = report(f'{a.weapon}: ONE ROUND, single fire mode', taps, grid, rig.K)
        f = report(f'{a.weapon}: full-auto control arm', bursts, grid, rig.K)

        # The control arm exists for one comparison and it is stated here
        # rather than left to the eye: over the first interval the two arms
        # measure the same event, so they must agree.
        if s and f:
            i = int(np.argmin(np.abs(grid - interval_s)))
            ds, df = s[0][i], f[0][i]
            se = np.hypot(s[1][i], f[1][i])
            print()
            print('  over the first interval (%.0f ms): single %.2f  full %.2f'
                  '  diff %.2f (combined sem %.2f)'
                  % (interval_s * 1000, ds, df, ds - df, se))
            if abs(ds - df) > 3 * max(se, 1e-9):
                print('  ⚠ THE ARMS DISAGREE. Single mode does not stand in for '
                      'full auto here,')
                print('    so the bounce above describes single mode only.')
            else:
                print('  the arms agree, so the single-shot trace describes the '
                      'first round of a burst.')

        stamp = time.strftime('%m%d_%H%M%S')
        path = os.path.join(OUT_DIR, f'{stamp}_{a.weapon}.npz')
        np.savez_compressed(
            path, grid=grid, K=rig.K, interval_s=interval_s,
            taps_t=np.array([t for t, _ in taps], dtype=object),
            taps_y=np.array([y for _, y in taps], dtype=object),
            bursts_t=np.array([t for t, _ in bursts], dtype=object),
            bursts_y=np.array([y for _, y in bursts], dtype=object),
            allow_pickle=True)
        print(f'\nsaved {path}')
    finally:
        if grabber is not None:
            grabber.close()
        rig.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
