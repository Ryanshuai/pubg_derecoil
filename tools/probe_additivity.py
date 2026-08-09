"""Do compensation counts and recoil ADD, or does the game blend them?

    pixi run python tools/probe_additivity.py --trials 8

The last place eta can be. Both mechanisms that were obvious are dead:

    SIZE  240 one-count moves give full K (1.5357 vs 1.5128)
    PATH  the firmware's player loses 0.90% [0.08, 1.72], not 4.11%

What every probe so far lacked, and every real magazine has, is that the game
is applying RECOIL at the same moment the compensation arrives. Counts and
recoil need not add linearly.

⚠ THE TRICK IS ONE LONG HOLD, and it exists because "trigger held, gun not
firing" is otherwise unreachable: PUBG auto-reloads from reserve the moment a
magazine empties, measured as 24 -> 40 after a 4 s hold, so an empty gun is not
a state the range can hold. But WITHIN a single hold it is:

    0 .. 2.6 s     40 rounds go out      recoil + compensation
    2.6 .. 5.0 s   the gun is dry        compensation ALONE
    the whole time the firmware plays the same curve

Both regimes, one trial, one K, one session, one curve. The refill happens
after the button comes up, which is exactly why it does not intrude.

So with a flat curve, the compensation's contribution is the difference
between the comp-ON and comp-OFF arms, and it can be read in BOTH windows:

    additive        the difference climbs at the same rate in both windows
    not additive    it climbs slower while rounds are going out, and the
                    shortfall is eta

⚠ ARMS ALTERNATE PER TRIAL. Every cross-arm comparison this project made
across sessions turned out to be a comparison of sessions -- 30 counts of
drift twenty minutes apart, which on 950 is 3.2% and was most of a 6.4%
"finding".

⚠ THE CURVE IS FLAT ON PURPOSE. A front-loaded curve puts most of its counts
inside the firing window, so the two windows would differ in RATE as well as in
whether rounds are flying, and nothing could say which mattered.
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
from calibration.collect_timed import aim_and_scope

HOLD_S = 5.0          # trigger held this long; the mp5k empties at ~2.6 s
TOTAL = 500           # counts of curve over the whole hold, flat
KNOT_MS = 17
FIRE_WIN = (0.30, 2.30)   # rounds are going out
DRY_WIN = (3.20, 4.90)    # the gun is empty, the curve is still playing
TEXTURE_MIN = 40.0


def upload_flat(mouse, total, span):
    """A constant-rate curve over `span`, uploaded and read back.

    Read back rather than assumed: int16 quantisation with a carry, and the
    negative-offset fold, both sit between the request and the wire.
    """
    n = int(span * 1000 / KNOT_MS)
    # ⚠ THE CURVE PUSHES DOWN, AGAINST THE RECOIL, like the real thing. The
    # first version had it pushing UP with the recoil: comp-ON then walked the
    # view 785 counts = ~1190 px upward, the tracked band ended on open sky,
    # and the dry window -- which is the LAST part of the trial -- read a
    # quarter of the commanded rate. That is the correlator going blind, not a
    # blend, and it lands exactly on the window the experiment is about.
    mouse.upload_pattern([0.0] * n, [total / n] * n,
                         [i * KNOT_MS / 1000.0 for i in range(n)])
    got = mouse.read_pattern() or []
    return got, abs(sum(k.get('dy', 0.0) for k in got))


def one_trial(rig, grabber, comp):
    """-> (t array from the click, cumulative y_obs in counts)."""
    for _ in range(3):
        grabber.grab_timed()
    prev = None
    while prev is None:
        _t, f = grabber.grab_timed()
        prev = rig.tracker.slice_frame(f) if f is not None else None

    rig.mouse.set_recoil_enabled(bool(comp))
    t_click = rig.mouse.click(buttons=0x01, duration_ms=int(HOLD_S * 1000))
    ts, dys = [], []
    while time.perf_counter() < t_click + HOLD_S + 0.3:
        t, f = grabber.grab_timed()
        if f is None:
            continue
        cur = rig.tracker.slice_frame(f)
        if cur is None:
            continue
        m = rig.tracker.measure_pair(prev, cur, 0.0)
        prev = cur
        ts.append(t - t_click)
        dys.append(m.dy if np.isfinite(m.dy) else 0.0)
    rig.mouse.set_recoil_enabled(False)
    t = np.asarray(ts)
    y = np.concatenate([[0.0], np.nancumsum(np.asarray(dys))]) / rig.K
    return t, y[:len(t)]


def texture(rig, grabber):
    import cv2
    for _ in range(3):
        grabber.grab_timed()
    _t, f = grabber.grab_timed()
    p = rig.tracker.slice_frame(f) if f is not None else None
    if p is None:
        return 0.0
    arrs = p if isinstance(p, (list, tuple)) else [p]
    vs = []
    for a in arrs:
        a = np.asarray(a)
        if a.ndim == 3:
            a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        vs.append(float(cv2.Laplacian(a.astype(np.uint8), cv2.CV_64F).var()))
    return float(np.median(vs))


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--trials', type=int, default=8)
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--weapon', default='mp5k')
    ap.add_argument('--countdown', type=int, default=8)
    a = ap.parse_args()

    if not ensure_ready(label='the additivity probe',
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
    runs = {True: [], False: []}
    try:
        curve, held = upload_flat(rig.mouse, TOTAL, HOLD_S)
        print(f'  curve: {len(curve)} knots, firmware holds {held:.1f} counts '
              f'over {HOLD_S:g} s')
        grabber = DXGISyncGrabber(rig.tracker.regions())
        tx = texture(rig, grabber)
        print(f'  patch texture {tx:.0f} (need {TEXTURE_MIN:.0f})')
        if tx < TEXTURE_MIN:
            print('[!] REFUSING: nothing to track. Open sky reads a confident '
                  'zero, not an error.')
            return 6
        for r in range(a.trials):
            for comp in (True, False):
                if not aim_and_scope(rig, 'standing'):
                    print(f'  r{r}: could not re-aim — stopping')
                    break
                n = rig.fire.read_ammo()
                if not n:
                    print(f'  r{r} comp={comp}: ammo reads {n} — reloading')
                    rig.fire.top_up()
                    n = rig.fire.read_ammo()
                t, y = one_trial(rig, grabber, comp)
                runs[comp].append((t, y))
                print(f'  r{r} comp={str(comp):5s} ammo {n}  {len(t):4d} frames  '
                      f'y_obs(end) {y[-1]:+8.1f} counts')
                rig.fire.top_up()
    finally:
        try:
            rig.mouse.set_recoil_enabled(False)
        except Exception:
            pass
        rig.close()

    if not runs[True] or not runs[False]:
        print('[!] one arm is empty')
        return 1

    G = np.arange(0.05, HOLD_S, 0.01)

    def stack(rs):
        out = []
        for t, y in rs:
            ok = np.isfinite(y)
            v = np.interp(G, t[ok], y[ok], left=np.nan, right=np.nan)
            v[(G < t[ok][0]) | (G > t[ok][-1])] = np.nan
            out.append(v)
        return np.nanmedian(out, axis=0)

    on, off = stack(runs[True]), stack(runs[False])
    # The compensation's contribution is what the ON arm has and the OFF arm
    # does not. Both arms fire the same rounds, so the recoil cancels. It is
    # NEGATIVE: the curve pushes the view down.
    d = on - off
    print()
    print(f'{"window":22} {"span":>10} {"d(comp)/dt":>12} {"counts/s":>10}')
    rates = {}
    for name, (lo, hi) in (('firing (rounds out)', FIRE_WIN),
                           ('dry (trigger held)', DRY_WIN)):
        sel = (G >= lo) & (G <= hi) & np.isfinite(d)
        if sel.sum() < 20:
            print(f'{name:22} -- too few samples')
            continue
        sl = np.polyfit(G[sel], d[sel], 1)[0]
        rates[name] = sl
        print(f'{name:22} {lo:4.1f}..{hi:4.1f} {sl:12.1f} {sl:10.1f}')
    print()
    print(f'the curve commands {held / HOLD_S:.1f} counts/s')
    if len(rates) == 2:
        a_, b_ = rates['firing (rounds out)'], rates['dry (trigger held)']
        print(f'  firing / dry = {a_/b_:.4f}   -> the compensation is '
              f'{100*(a_/b_ - 1):+.2f}% effective while rounds are going out')
        print()
        print('  additive      the two rates agree')
        print('  NOT additive  firing is lower, and that shortfall is eta '
              '(-4.1% is what the arms say)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
