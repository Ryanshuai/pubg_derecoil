"""Same displacement, ONE correlation or FORTY. Which K is the biased one?

    pixi run python tools/probe_correlator_bias.py --trials 10

K has two values and they disagree by 9.4 sigma:

    drop dups (calibrate_k, how K has always been defined)  1.5171 +- 0.0030
    keep dups (collect_timed, how every burst is measured)  1.5520 +- 0.0021

They differ in nothing but HOW MANY FRAME PAIRS share the same total motion.
Dropping a frame does not merely drop a sample -- it correlates k-1 against
k+1, so the displacement per pair doubles and the pair count halves. One of
the two is biased and the repository did not know both existed.

⚠ THE TEST IS THE SAME TOTAL THROUGH DIFFERENT NUMBERS OF PAIRS, which is the
only thing that separates them:

    one-pair    80 counts injected BETWEEN two frames, correlated once
    many-pairs  80 counts spread over ~1.5 s, every presented frame summed

    unbiased        the two totals agree
    per-pair bias   many-pairs is off by (pairs x bias), one-pair by (1 x bias)

⚠ 80 COUNTS IS THE LARGEST SINGLE STEP THAT IS STILL MEASURABLE. At the ADS
K it is ~121 px against a correlator unambiguous only to RECOIL_PATCH_H/2 =
128 px. Past that it wraps by exactly 256 px and reports a SMALL displacement,
which is how four K calibration runs were quietly ruined (tools/audit_k.py).

⚠ NO CLICK, SO NO GUN BEHAVIOUR ENTERS. Only mouse.move(). The trigger is
what forced every earlier probe into contortions -- an empty magazine refills,
bare hands punch -- and none of it applies here.

⚠ ARMS ALTERNATE AND EACH FIRES BOTH WAYS, so the view has no net drift and
cannot walk into the pitch clamp or onto open sky. Both were paid for tonight.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np

from control.session import ensure_ready
from calibration.sweep import Rig

# ⚠ 70, NOT 80, AND THE GATE BELOW IS 127 NOT 118. At 80 the single pair
# reads 123.1 px and an ALIAS_PX of 118 threw away EVERY one-pair trial --
# a gate set to catch wraps, killing readings that were not wrapped. A wrap
# reports a SMALL number (it subtracts 256), so "too big" is the wrong shape
# for that gate: the only thing it can honestly refuse is a reading at the
# ceiling itself.
COUNTS = 70
SPREAD_S = 1.5        # the many-pairs arm spreads it over this long
SETTLE_S = 0.35
TEXTURE_MIN = 40.0
ALIAS_PX = 127.0


def texture(rig, grabber):
    import cv2
    for _ in range(3):
        grabber.grab_timed()
    _t, f = grabber.grab_timed()
    p = rig.tracker.slice_frame(f) if f is not None else None
    if p is None:
        return 0.0
    arrs = p if isinstance(p, (list, tuple)) else [p]
    out = []
    for a in arrs:
        a = np.asarray(a)
        if a.ndim == 3:
            a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        out.append(float(cv2.Laplacian(a.astype(np.uint8), cv2.CV_64F).var()))
    return float(np.median(out))


def _first_patch(rig, grabber):
    while True:
        _t, f = grabber.grab_timed()
        p = rig.tracker.slice_frame(f) if f is not None else None
        if p is not None:
            return p


def one_pair(rig, grabber, sign):
    """The whole displacement across a SINGLE correlation. -> (px, pairs)."""
    for _ in range(3):
        grabber.grab_timed()
    a = _first_patch(rig, grabber)
    rig.mouse.move(0, -sign * COUNTS)
    time.sleep(SETTLE_S)
    for _ in range(2):
        grabber.grab_timed()          # flush anything mid-motion
    b = _first_patch(rig, grabber)
    m = rig.tracker.measure_pair(a, b, 0.0)
    return (abs(m.dy) if np.isfinite(m.dy) else float('nan')), 1


def many_pairs(rig, grabber, sign):
    """The same displacement summed over every presented frame."""
    for _ in range(3):
        grabber.grab_timed()
    prev = _first_patch(rig, grabber)
    n = int(SPREAD_S * 200)
    t0 = time.perf_counter()
    import threading

    def inject():
        acc = 0.0
        for i in range(n):
            dt = t0 + SPREAD_S * i / n - time.perf_counter()
            if dt > 0:
                time.sleep(dt)
            acc += COUNTS / n
            s = int(acc)
            acc -= s
            if s:
                rig.mouse.move(0, -sign * s)

    threading.Thread(target=inject, daemon=True).start()
    total, pairs, worst = 0.0, 0, 0.0
    while time.perf_counter() < t0 + SPREAD_S + SETTLE_S:
        _t, f = grabber.grab_timed()
        if f is None:
            continue
        cur = rig.tracker.slice_frame(f)
        if cur is None:
            continue
        m = rig.tracker.measure_pair(prev, cur, 0.0)
        prev = cur
        if np.isfinite(m.dy):
            total += m.dy
            worst = max(worst, abs(m.dy))
            pairs += 1
    return abs(total), pairs


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--trials', type=int, default=10)
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--weapon', default='mp5k')
    ap.add_argument('--countdown', type=int, default=8)
    a = ap.parse_args()

    if not ensure_ready(label='the correlator-bias probe',
                        countdown_s=a.countdown)['ok']:
        print('[!] could not get the game ready')
        return 1

    from control.inventory import InventoryControl
    from control.spawner import SpawnerControl
    from control.stock import ensure_weapon_in_hand
    with InventoryControl() as ac, SpawnerControl(verbose=False) as sc:
        slot = ensure_weapon_in_hand(ac, sc, a.weapon)
        if not slot:
            print(f'[!] no {a.weapon} would come to hand')
            return 1
        with ac.tab_up():
            ac.ensure_kit(slot, {'scope': a.sight}, weapon=a.weapon)
        ac.hold(slot)

    rig = Rig(a.sight, prefer_dxgi=False)
    from capture.cropper import DXGISyncGrabber
    out = {'one-pair': [], 'many-pairs': []}
    npairs = []
    try:
        rig.mouse.set_recoil_enabled(False)
        if not rig.gun.ensure_ads():
            print('[!] could not get into ADS — K would be the hip-fire one')
            return 1
        grabber = DXGISyncGrabber(rig.tracker.regions())
        tx = texture(rig, grabber)
        print(f'  patch texture {tx:.0f} (need {TEXTURE_MIN:.0f})')
        if tx < TEXTURE_MIN:
            print('[!] REFUSING: nothing to track.')
            return 6
        for r in range(a.trials):
            if not rig.gun.in_ads() and not rig.gun.ensure_ads():
                print(f'  r{r}: dropped out of ADS — stopping (K is 3x)')
                break
            for sign in (+1, -1):
                for name, fn in (('one-pair', one_pair),
                                 ('many-pairs', many_pairs)):
                    px, pairs = fn(rig, grabber, sign)
                    tag = 'up' if sign > 0 else 'dn'
                    if not np.isfinite(px) or px < 5:
                        print(f'  r{r} {name:11s} {tag} {px} — DROPPED')
                        continue
                    if name == 'one-pair' and px > ALIAS_PX:
                        print(f'  r{r} {name:11s} {tag} {px:.1f} px > '
                              f'{ALIAS_PX:.0f} — DROPPED, at the wrap ceiling')
                        continue
                    out[name].append(px / COUNTS)
                    if name == 'many-pairs':
                        npairs.append(pairs)
                    print(f'  r{r} {name:11s} {tag} {px:7.2f} px / {COUNTS} '
                          f'= {px/COUNTS:.4f}   pairs {pairs}')
                    time.sleep(0.2)
                # undo this direction before the next, so nothing accumulates
                rig.mouse.move(0, sign * COUNTS * 2)
                time.sleep(SETTLE_S)
    finally:
        rig.close()

    print()
    for k in ('one-pair', 'many-pairs'):
        v = np.array(out[k])
        if not len(v):
            print(f'{k:11} --')
            continue
        sd = v.std(ddof=1) if len(v) > 1 else float('nan')
        print(f'{k:11} n={len(v):3d}  K = {v.mean():.4f}  sd {sd:.4f}  '
              f'sem {sd/len(v)**0.5:.4f}')
    o, m = np.array(out['one-pair']), np.array(out['many-pairs'])
    if len(o) > 1 and len(m) > 1:
        d = m.mean() - o.mean()
        se = (o.std(ddof=1)**2/len(o) + m.std(ddof=1)**2/len(m))**0.5
        print()
        print(f'many-pairs - one-pair = {100*d/o.mean():+.2f}%   '
              f'{abs(d)/se:.1f} sigma   (median {np.median(npairs):.0f} pairs)')
        print()
        print('  agree            the correlator is unbiased; the drop/keep')
        print('                   difference is something else')
        print('  many > one       each pair over-reads; keep-duplicates is the')
        print('                   biased one and drop-duplicates is closer')
        print('  many < one       each pair under-reads; the other way round')
    return 0


if __name__ == '__main__':
    sys.exit(main())
