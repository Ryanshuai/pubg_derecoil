"""How much of the burst's recoil the CURRENT curve fails to cancel, per instant.

⚠ IT EXISTS BECAUSE EVERY OTHER CRITERION IN THIS REPOSITORY IS AN AGGREGATE.
`residual` is a signed sum over the burst, `sum|per-bullet e|` is a mean over a
cell, the fired `--fire-delay-sweep` scored whole-burst RMS. All three are blind
to the first shot -- and the first shot is what a player reports. Root
CLAUDE.md: 判据必须能看见它要管的那个维度. This prints the shortfall AT
INSTANTS, which is the ordered form that law asks for.

WHAT IT COMPARES, and both halves are deliberate:

    y_true(t)   the weapon's own recoil, from the store. Measured.
    delivered   what the firmware WOULD emit for the curve now on disk, run
                through the real upload_pattern transform: offset, then the fold
                of everything before t=0 into a step, then comp_counts_at.

⚠ THE OFFSET AND THE FOLD ARE THE POINT, NOT A DETAIL. Reading the curve file's
own knot times answers a question nobody asks -- the firmware never plays those
times. Measured 2026-08-09: at RECOIL_FIRE_DELAY_MS = -90 the fold puts 8.55
counts of an aug's curve into a step at t=0, and at -30 it puts 0.69. A version
of this probe that skipped the transform reported the -30 case's shortfall for
every offset, which is how a 10 ms change came to look like it should have been
visible when it was worth one pixel.

⚠ IT DOES NOT USE EACH MAGAZINE'S OWN `curve`. That answers "was this magazine
compensated well", which is history: those magazines were fired BEFORE the curve
they produced. Using it reported an aug shortfall of 109 counts at t=2 s that
belongs to a curve no longer on disk, while the current one is short by 36.

⚠ AND y_true's OWN HEAD IS THE UNVERIFIED TERM. It comes from a phase-correlation
tracker, and the first shot is the moment that tracker has had the least time to
lock. If the head of y_true is under-read, every number this probe prints for
t < 0.3 s is under-read with it -- which is the live disagreement (2026-08-09):
this probe says -60 nulls the burst from 0.5 s on and -90 over-compensates, while
the operator fires both and reports -90 as the one that holds the first shot
down. The probe cannot settle that; it can only say which of the two the STORE
prefers, and it says -60.

    pixi run head-shortfall                        # every cell with a curve
    pixi run head-shortfall --weapon aug            # one gun
    pixi run head-shortfall --offset=-20,-60,-90    # sweep the offset offline

⚠ `--offset=` NEEDS THE EQUALS SIGN. Every value here is negative, and argparse
reads a leading `-` as another flag: `--offset -60` fails with "expected one
argument". The form above is the only one that works.
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config                                                  # noqa: E402
from calibration import samples as S                           # noqa: E402
from config import parse_config_key, config_key                # noqa: E402

GRID_S = (0.1, 0.3, 0.5, 1.0, 2.0, 3.0)


def as_firmware(shots, off_ms):
    """The knots the firmware receives. Mirrors press.pico_mouse.upload_pattern.

    ⚠ THE FOLD IS NOT A ROUNDING STEP. A knot at t < 0 is an instruction the
    firmware cannot obey, so everything owed before the click is delivered as a
    step AT the click -- which is the whole mechanism by which a large lead acts
    on the first shot rather than on the phase of the curve.

    Kept as a transcription rather than an import because upload_pattern also
    quantises to int16 with a carry and writes to a serial port; this needs the
    times and amounts, not the wire format. If the two ever disagree the
    difference is at most one carried count -- see comp_counts_at's own note and
    `pixi run comp-counts`.
    """
    t, ks = 0.0, []
    for i, k in enumerate(shots):
        if i:
            t += k['delay_ms'] / 1000.0
        ks.append((t + off_ms / 1000.0, float(k['dy'])))
    fold, out = 0.0, []
    for tt, dy in ks:
        if tt < 0:
            fold += dy
            continue
        if not out:
            out.append({'t_ms': 0.0, 'dy': dy + fold, 'dx': 0})
        else:
            out.append({'t_ms': tt * 1000.0, 'dy': dy, 'dx': 0})
    return out or [{'t_ms': 0.0, 'dy': fold, 'dx': 0}]


def cells(weapon=None):
    """(gun, config_key, shots) for every curve that also has magazines."""
    for f in sorted(glob.glob(os.path.join(ROOT, 'data', 'curves', '*.json'))):
        if f.endswith('.bak.json'):
            continue
        with open(f, encoding='utf-8') as fh:
            d = json.load(fh)
        if not d.get('shots'):
            continue
        gun = d['weapon']
        if weapon and gun != weapon:
            continue
        ck = config_key(d.get('config'))
        yield gun, ck, d['shots'], bool(d.get('seed'))


def shortfall(mags, knots, grid, lag_s):
    """(y_true, delivered, short) at each instant in `grid`. NaN where no data."""
    out = []
    for g in grid:
        yt = []
        for m in mags:
            t, y = m.y_true_counts()
            t = np.asarray(t, float)
            y = np.asarray(y, float)[:len(t)]
            if len(t) < 5 or t[-1] < g:
                continue
            v = np.interp(g, t, y)
            if np.isfinite(v):
                yt.append(v)
        if not yt:
            out.append((float('nan'),) * 3)
            continue
        dl = float(np.asarray(
            S.comp_counts_at(knots, np.array([max(0.0, g - lag_s)]))).ravel()[0])
        mu = float(np.mean(yt))
        out.append((mu, dl, mu - dl))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--weapon', default=None)
    ap.add_argument('--offset', default=None,
                    help='comma-separated offsets in ms to try INSTEAD of '
                         'config.RECOIL_FIRE_DELAY_MS. The point of a sweep here '
                         'is that y_true is measured and only the delivery moves, '
                         'so it costs no game time -- but it also cannot see the '
                         'magazine-to-magazine spread that the FIRED sweep found '
                         'over-leading to produce.')
    ap.add_argument('--percent', action='store_true',
                    help='print the shortfall as a fraction of y_true. ⚠ At '
                         't=0.1 s y_true is a handful of counts, so the '
                         'percentage there amplifies noise; counts are the '
                         'honest unit at the head.')
    a = ap.parse_args()

    offs = ([float(x) for x in a.offset.split(',')] if a.offset
            else [float(config.RECOIL_FIRE_DELAY_MS)])
    lag_s = config.RECOIL_COMP_LAG_MS / 1000.0
    unit = '%' if a.percent else ' counts'
    print(f'y_true - delivered, in{unit}. Positive = UNDER-compensated.')
    print(f'comp lag L = {config.RECOIL_COMP_LAG_MS} ms. '
          f'Offsets shown include the fold.\n')
    hdr = f'{"cell":34}{"offset":>8}' + ''.join(f'{g:>8}' for g in GRID_S)
    print(hdr)
    n = 0
    for gun, ck, shots, seed in cells(a.weapon):
        mags = list(S.load(gun, parse_config_key(ck) or {}))
        if not mags:
            continue
        n += 1
        for off in offs:
            rows = shortfall(mags, as_firmware(shots, off), GRID_S, lag_s)
            cs = []
            for mu, dl, sh in rows:
                if not np.isfinite(sh):
                    cs.append(f'{"-":>8}')
                elif a.percent:
                    cs.append(f'{(sh/mu*100 if mu else 0):>7.1f}%')
                else:
                    cs.append(f'{sh:>8.0f}')
            tag = f'{gun} {ck}'[:32] + (' *' if seed else '')
            print(f'{tag:34}{off:>8.0f}' + ''.join(cs))
    print(f'\n{n} cell(s). `*` = the curve is a SEED, so the shortfall is '
          f'against somebody else\'s guess.')
    if not a.offset:
        print('Pass --offset to sweep it offline; y_true is measured and only '
              'the delivery moves.')


if __name__ == '__main__':
    main()
