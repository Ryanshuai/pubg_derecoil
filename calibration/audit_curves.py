"""Did the alpha=1 runs damage any curve? Compare each to its oldest backup.

    pixi run python calibration/audit_curves.py

The per-magazine EMA landed with curve_updates() returning 0 for curves that
had in fact been fitted many times -- the ema_updates field was new that
afternoon and no existing curve carried it -- so alpha came out 1 and a single
noisy magazine rewrote the whole curve. On the AUG that also amputated the tail
from 41 bullets to 30, because the fit truncates where the curve falls below
FIRE_FLOOR_FRAC of its plateau and a scrambled shape drops below it early.

Every write kept a timestamped .bak, so the damage is measurable: shot count
and total counts, now versus the oldest surviving backup. A curve that lost
bullets or moved its total by a large fraction was overwritten by noise, not
improved by measurement.
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURVES = os.path.join(ROOT, 'docs', 'recoil', 'curves')

TOTAL_WARN = 0.30         # fractional move that wants a human look

# A curve is per-shot dy in curve units, scaled by counts_per_unit at load. A
# shot count near 200 is a SEED -- the wiki-derived spray every weapon started
# with, longer than any magazine. Shrinking to ~40 is the fit replacing a guess
# with a measurement, not damage, so seeds are excluded from the shrink test.
SEED_BULLETS = 150


def info(path):
    with open(path, encoding='utf-8') as fh:
        j = json.load(fh)
    shots = j.get('shots') or []
    dy = [float(s.get('dy', 0.0)) for s in shots]
    m = j.get('measured') or {}
    return len(dy), sum(dy), m.get('ema_updates'), m.get('n_mags'), dy


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    names = sorted(f for f in os.listdir(CURVES)
                   if f.endswith('.json') and '.bak.' not in f)
    print(f"{'curve':<24}{'n':>4}{'total':>10}{'ema':>5}{'mags':>5}"
          f"   {'oldest bak':>13}{'n':>4}{'total':>10}   note")
    hurt = []
    for f in names:
        stem = f[:-5]
        try:
            n, t, ema, mags, dy = info(os.path.join(CURVES, f))
        except Exception as exc:                       # noqa: BLE001
            print(f"{stem:<24}  unreadable: {exc}")
            continue
        if not n:
            continue                                   # never fitted
        baks = sorted(glob.glob(os.path.join(CURVES, stem + '.*.bak.json')))
        note = ''
        # Sign first, because it is not a matter of degree. Recoil pulls the
        # view up, so compensation pulls it down and dy is positive. A curve
        # that came out negative is not a bad fit of the right thing -- it was
        # measured through the wrong K, and firing it ADDS to the recoil.
        neg = sum(1 for v in dy if v < 0)
        if t < 0:
            note = f'NEGATIVE total -- fires upward ({neg}/{n} shots < 0)'
        bn = bt = None
        stamp = '-'
        if baks:
            bn, bt, _, _, _ = info(baks[0])
            stamp = os.path.basename(baks[0])[len(stem) + 1:-9]
            if not note and bn < SEED_BULLETS and n < bn:
                note = f'SHRUNK {bn}->{n} bullets'
            elif not note and bt and abs(t - bt) / abs(bt) > TOTAL_WARN:
                note = f'total moved {100.0 * (t - bt) / bt:+.0f}%'
        if note:
            hurt.append(stem)
        seed = '  (seed)' if bn and bn >= SEED_BULLETS else ''
        print(f"{stem:<24}{n:>4}{t:>10.1f}{str(ema):>5}{str(mags):>5}"
              f"   {stamp:>13}"
              f"{('' if bn is None else str(bn)):>4}"
              f"{('' if bt is None else f'{bt:.1f}'):>10}{seed}   {note}")
    print()
    if hurt:
        print(f"[!] wants a look: {', '.join(hurt)}")
        print("    A backup is a curve that was believed at the time. Restoring "
              "one\n    is a judgement, not a rollback -- check which backup "
              "was measured\n    under a sound ADS gate and a correct time "
              "origin before picking.")
    else:
        print("no curve is negative, lost measured bullets, or moved its total "
              f"by more than {TOTAL_WARN:.0%}.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
