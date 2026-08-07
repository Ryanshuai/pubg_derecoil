"""Which round does the compensation land on — measured against a BASELINE.

    pixi run python tools/probe_impulse_ab.py --weapon aug --bullets 0,12

Fires the same magazine twice: once with an all-zero curve, once with a curve
that is zero except one spiked round. The per-round view displacement is
recorded both times and SUBTRACTED. What is left is the firmware's pulse, with
the game's own recoil removed.

WHY A BASELINE, when probe_impulse_align.py already answers this.

That probe finds the spike as the round whose view motion is the most
negative, against the game's own upward recoil. On round 12 that works: the
recoil has plateaued at about 20 counts and the spike is 120, six to one, and
every neighbouring round looks the same as every other.

Round 0 is not round 12. It is where all the once-per-burst machinery lives —
the click edge, the pattern upload, the trigger-down transition — and it is
also where the ADS settle, the muzzle flash and the game's own first-shot
behaviour all land. Those are exactly the things that make bin 0 noisier than
its neighbours, and none of them are constant across weapons. The evidence
that the timing is right today covers rounds 12 and 30, both steady state.
Round 0 has never been measured.

Differencing removes everything that is common to the two runs — the game's
recoil, the ADS settle, the first-shot transient — because they happen in the
baseline too. It leaves the one thing that differs: the spike.

THE BASELINE IS THE WEAPON'S OWN CURVE, NOT ZERO. Differencing only requires
the two runs to share a baseline; it does not require that baseline to be
nothing. The first version used an all-zero curve, on the reasoning that
"baseline" means "no compensation", and that cost a whole run:

    40 rounds x 22 counts of uncompensated AUG recoil = 883 counts of climb,
    which at K=1.55 is 1366 px -- 5.3 times the 256 px tracker patch.

Past the texture the view is in sky, where phase correlation returns a
confident zero. So track_still() accumulated nothing, pending_pitch never came
down, recentring reported "moved +60 counts and the view did not follow", and
the absolute placement was refused on all nine magazines of
docs/impulse/ab_aug_0803_0107.json. The spike numbers came out clean anyway --
only the first 16 rounds are binned and those are still in texture -- which is
exactly the shape of failure worth being afraid of: a correct measurement
taken by a run whose state was garbage.

With the weapon's stored curve underneath, the view stays where it was aimed,
the recentring has something to measure, and the difference still isolates the
spike because the base is identical in both.
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
from control.focus import focus_keeper
from control.session import ensure_ready
from control.lobby import LobbyControl
from control.spawner import SpawnerControl

from analysis import interval_from_span
from harvest import build_weapon
from sweep import Rig

OUT_DIR = os.path.join(ROOT, 'docs', 'impulse')


def per_round(rig, rec, first_shot, fire_end, mag_size, nominal, n_bins):
    """View displacement per round, in mouse counts. -> (array, interval_s)

    Bins are anchored on the ammo counter's first change, which is the whole
    point of this measurement: the firmware plays on a grid anchored to the
    CLICK, and whether those two agree is the question.
    """
    iv, _ = interval_from_span(first_shot, fire_end, mag_size)
    iv = iv or nominal
    res = rec.finish()
    ts = np.asarray(res.ts, dtype=float) - first_shot
    dyv = np.asarray(res.dy, dtype=float)
    oor = np.asarray(res.out_of_range, dtype=bool)
    if len(oor) != len(dyv):
        oor = np.zeros(len(dyv), dtype=bool)
    counts = np.where(oor, np.nan, dyv / rig.K)
    good = ~np.isnan(counts)
    if good.sum() < 2:
        return None, iv
    cum = np.cumsum(np.where(good, counts, 0.0))
    tt, cc = ts[good], cum[good]
    edges = np.arange(n_bins + 1) * iv
    per = np.diff(np.interp(edges, tt, cc, left=0.0, right=float(cc[-1])))
    return per, iv


def base_curve(weapon, posture, n_pts, nominal):
    """The weapon's own per-bullet compensation, as the A/B baseline.

    Falls back to zeros when the weapon has no stored curve — measurable, but
    the view will climb and the run will say so.
    """
    w = build_weapon(weapon, posture, {})
    # np.asarray, not `or []`: dy_s is an ndarray and truth-testing one raises.
    dy_s = np.asarray(w.dy_s, dtype=float).ravel()
    t_s = np.asarray(w.t_s, dtype=float).ravel()
    if dy_s.size == 0 or t_s.size != dy_s.size:
        return [0.0] * n_pts, False
    # Merge the stored samples onto this probe's one-point-per-bullet grid.
    #
    # round(), not int(). The stored samples sit at exactly k * interval and
    # the probe's `nominal` is that same interval, so the quotient is k — but
    # in floating point it is 7.9999999, and int() truncates it to 7, which
    # collides two bullets into one bin. Measured: bullets 0 and 1 (6 and 7
    # counts) merged into a single 14, and every later bullet sat one index
    # early.
    #
    # It did NOT explain the 5 counts sitting at bullet 9 between neighbours
    # of 38 and 40 — that survives the fix, so it is in the stored curve
    # itself, not in this merge. Left alone here because this probe measures
    # TIMING and only needs the base to be shared and roughly compensating;
    # noted because a single bullet at an eighth of its neighbours is not a
    # feature of any recoil pattern and whoever refits the AUG should look.
    per = [0.0] * n_pts
    for dy, t in zip(dy_s, t_s):
        k = int(round(t / nominal))
        if 0 <= k < n_pts:
            per[k] += float(dy)
    return per, True


def fire_one(rig, dy_pattern, nominal, mag_size, n_bins, label):
    """Upload a curve, fire a magazine, return its per-round profile."""
    n = len(dy_pattern)
    # Bare, not rig.arm(): what goes up is a SYNTHETIC pattern -- zero
    # everywhere but one spiked round -- and there is no Weapon behind it.
    # This probe measures which round the firmware lands a spike on, so
    # the upload is the thing under test, the same exemption
    # calibrate_k.py holds for its bare mouse.move.
    rig.mouse.upload_pattern([0.0] * n, list(dy_pattern),
                             [k * nominal for k in range(n)], nominal)
    rig.mouse.set_recoil_enabled(True)
    time.sleep(0.25)
    if not rig.ensure_ads():
        print(f'    {label}: could not enter ADS')
        return None, None
    rec, fire_s, steps, fire_end, first_shot, ads = rig.fire_magazine()
    if steps == 0 or first_shot is None:
        print(f'    {label}: nothing fired')
        rig.wait_reload()
        return None, None
    per, iv = per_round(rig, rec, first_shot, fire_end, mag_size, nominal,
                        n_bins)
    if per is None:
        print(f'    {label}: the view was never tracked')
        rig.wait_reload()
        return None, iv
    # Pay the drift back before the next magazine. Without this the view walks
    # up by the whole uncompensated pattern every baseline run and is in the
    # sky by the third one.
    rig.pending_pitch += float(np.nansum(per))
    rig.reaim()
    rig.wait_reload()
    print(f'    {label}: ads {ads:.0%}, interval {1000*iv:.1f} ms, '
          f'sum {np.nansum(per):+.0f}')
    return per, iv


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapon', default='aug')
    ap.add_argument('--bullets', default='0,12',
                    help='which rounds get a spike, one magazine each. 12 is '
                         'the control: it is already known to land on 0, so a '
                         'clean 12 and a dirty 0 is a statement about the '
                         'FIRST ROUND rather than about the run.')
    ap.add_argument('--spike', type=float, default=120.0, help='counts')
    ap.add_argument('--rounds', type=int, default=3,
                    help='repeats. Each costs one baseline magazine plus one '
                         'per --bullets entry.')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--countdown', type=int, default=8)
    ap.add_argument('--no-spawn', action='store_true')
    args = ap.parse_args()

    ks = [int(x) for x in args.bullets.split(',') if x.strip() != '']
    n_bins = max(ks) + 4

    print(f'>>> {args.weapon}: spike {args.spike:.0f} counts on rounds {ks}, '
          f"each against the weapon's own curve as baseline")
    print(f'>>> {args.rounds} x ({1 + len(ks)}) = '
          f'{args.rounds * (1 + len(ks))} magazines')

    if not ensure_ready(label='the A/B impulse probe', countdown_s=args.countdown)['ok']:
        print('[!] could not focus the game')
        return 1
    with LobbyControl() as lc:
        if not lc.ensure_in_match()['ok']:
            print('[!] not in a match and could not get into one')
            return 1
    time.sleep(1.0)

    if not args.no_spawn:
        sc = SpawnerControl()
        res = sc.give_many(['backpack3', args.weapon, 'ext_ar'])
        print(f"  spawned in {res['clicks']} clicks"
              + ('' if res['ok'] else f" — {res['error']}"))
        sc.ensure_panel(False)
        time.sleep(0.8)

    rig = Rig(args.sight)
    nominal = 60.0 / WEAPON_RPM.get(args.weapon, 600)
    base_runs, spike_runs = [], {k: [] for k in ks}
    try:
        if not rig.ensure_ads():
            print('[!] could not enter ADS')
            return 1
        mag_size = rig.magazine_size()
        print(f'  magazine holds {mag_size}')
        if not mag_size:
            print('[!] no magazine reading — is a gun in hand? Nothing '
                  'measured here would mean anything.')
            return 1

        # The pattern has to cover the WHOLE magazine, not just the rounds
        # that get binned. Every round past the end of it is uncompensated,
        # and 26 uncompensated AUG rounds are 570 counts of climb — which is
        # how the view ended up in the sky when this probe covered
        # max(bullets)+2 points.
        n_pts = int(mag_size)
        # The cell's aim point, for the absolute placement check. Forgetting
        # this does not read as a missing feature: absolute_offset() returns
        # None on its first line and recenter() reports "cannot place the view
        # against the cell's reference" on every magazine, which reads exactly
        # like a range problem. Two live runs were spent on that.
        rig.set_reference()

        base, have_curve = base_curve(args.weapon, 'standing', n_pts, nominal)
        print(f'  baseline: '
              + (f'the stored {args.weapon} curve, {sum(base):+.0f} counts '
                 f'over {n_pts} rounds' if have_curve else
                 'ZERO — no stored curve for this weapon, so the view will '
                 'climb and the placement checks will fail'))

        for r in range(args.rounds):
            if not focus_keeper().ok(f'round {r}'):
                break
            print(f'  round {r + 1}/{args.rounds}')
            per, _ = fire_one(rig, base, nominal, mag_size, n_bins,
                              'baseline')
            if per is not None:
                base_runs.append(per)
            for k in ks:
                # The spike rides ON TOP of the same base both runs share, so
                # the subtraction still leaves only the spike.
                dy = list(base)
                dy[k] += args.spike
                per, _ = fire_one(rig, dy, nominal, mag_size, n_bins,
                                  f'spike@{k}')
                if per is not None:
                    spike_runs[k].append(per)
    finally:
        # clear=True because what this uploaded is a synthetic spike, not
        # any weapon's curve -- worse to leave loaded than a real one.
        rig.disarm(clear=True)
        rig.close()

    if not base_runs:
        print('[!] no baseline magazines survived — nothing to difference '
              'against')
        return 1

    base = np.nanmean(np.vstack(base_runs), axis=0)
    print(f'\nbaseline (n={len(base_runs)}), per round, counts:')
    print('  ' + ' '.join(f'{i}:{v:+.0f}' for i, v in enumerate(base[:n_bins])))

    out, ok_all = [], True
    for k in ks:
        runs = spike_runs[k]
        if not runs:
            print(f'\nround {k}: no magazine survived')
            ok_all = False
            continue
        sp = np.nanmean(np.vstack(runs), axis=0)
        diff = sp - base
        landed = int(np.nanargmin(diff))
        depth = float(diff[landed])
        # The spike is downward and everything common cancelled, so the
        # deepest bin IS the round the pulse played on. Reporting how much
        # deeper it is than its neighbours says whether that argmin means
        # anything: a spike that only just wins is not a measurement.
        others = np.delete(diff[:n_bins], landed)
        margin = float(np.nanmin(others) - depth)
        off = landed - k
        flag = 'ON THE ROUND' if off == 0 else f'OFF BY {off:+d}'
        print(f'\nround {k}: spike landed on round {landed}   {flag}'
              f'   (n={len(runs)})')
        print(f'  depth {depth:+.0f} counts, commanded {-args.spike:+.0f}; '
              f'next-deepest bin is {margin:.0f} counts shallower')
        print('  diff ' + ' '.join(f'{i}:{v:+.0f}'
                                   for i, v in enumerate(diff[:n_bins])))
        if margin < args.spike * 0.25:
            print('  [!] the margin is thin — this argmin is not conclusive')
            ok_all = False
        if off != 0:
            ok_all = False
        out.append({'bullet': k, 'landed': landed, 'off': off,
                    'depth': depth, 'margin': margin, 'n': len(runs),
                    'diff': [float(v) for v in diff[:n_bins]]})

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f'ab_{args.weapon}_{datetime.now():%m%d_%H%M}.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'weapon': args.weapon, 'spike': args.spike,
                   'rounds': args.rounds, 'baseline': [float(v) for v in base],
                   'results': out,
                   'ts': datetime.now().isoformat()}, f, indent=1)
    print(f'\n  -> {os.path.relpath(path, ROOT)}')

    if ok_all and out:
        print('\nEvery spike landed on the round it was commanded, including '
              'the first.')
    return 0 if ok_all and out else 1


if __name__ == '__main__':
    sys.exit(main())
