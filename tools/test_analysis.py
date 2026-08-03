"""The measurement maths, offline. No game, no screen, no hardware.

    pixi run analysis

calibration/analysis.py turns a magazine's recording into numbers and then
decides whether to believe them. Every claim in its docstrings is checkable
without the game running, and none of them was checked, because the functions
used to live in sweep.py next to the rig — so importing one pulled in a Pico
backend, a torch-backed detector and win32gui first.

Two kinds of check here:

  * PROPERTIES, on synthetic traces. Each one encodes a specific bug the
    current code was written to fix, so a regression re-breaks the test rather
    than quietly changing a number.
  * REPLAY, on every magazine ever logged under docs/recoil/runs/*.jsonl. A logged
    magazine is by construction one the gates ACCEPTED — measure_cell discards
    before it appends — so a logged magazine that magazine_fault now refuses
    means a gate moved since that run. The six that legitimately do are listed
    below with why; anything else is a failure.
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))
# The logs moved out of calibration/ on 2026-08-02; the scripts stayed.
RUNS = os.path.join(ROOT, 'docs', 'recoil', 'runs')
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass


from analysis import (analyse, fit_interval, interval_from_span,
                      magazine_fault)
from detector.view_tracker import MagazineResult

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {name:<52} {got!r}'
          + ('' if ok else f'  != {want!r}'))
    if not ok:
        FAILS.append(name)


def close(name, got, want, tol):
    ok = got is not None and abs(got - want) <= tol
    shown = 'None' if got is None else f'{got:.4f}'
    print(f'  {"ok  " if ok else "FAIL"}  {name:<52} {shown}'
          + ('' if ok else f'  != {want:.4f} +- {tol:g}'))
    if not ok:
        FAILS.append(name)


def make_result(ts, dy, human=None, oor=None):
    """A MagazineResult as finish() would have produced it."""
    n = len(ts)
    res = MagazineResult()
    res.ts = list(ts)
    res.dy = list(dy)
    res.dx = [0.0] * n
    res.mad = [1.0] * n
    res.n_rejected = [0] * n
    res.out_of_range = list(oor) if oor is not None else [False] * n
    res.gates = [0] * n
    res.human_dy = list(human) if human is not None else []
    res.human_dx = [0.0] * n
    return res


# ══════════════════════════════════════════════════════════════
print('\n=== interval_from_span: (last - first) / (rounds - 1) ===')
# 42 rounds spanning 3.4 s. The endpoints are the only two events the pipeline
# detects reliably, which is the whole argument for this over a per-round fit.
iv, n = interval_from_span(10.0, 13.4, 42)
close('42 rounds over 3.400 s', iv, 3.4 / 41, 1e-9)
check('...and reports the round count', n, 42)

print('  -- and refuses rather than guessing:')
for name, args in [('no magazine size', (10.0, 13.4, None)),
                   ('a magazine of one', (10.0, 13.4, 1)),
                   ('first shot never seen', (None, 13.4, 42)),
                   ('the span ran backwards', (13.4, 10.0, 42)),
                   ('zero span', (10.0, 10.0, 42))]:
    check(name, interval_from_span(*args), (None, 0))


# ══════════════════════════════════════════════════════════════
print('\n=== fit_interval: the counter states the gun\'s own fire rate ===')
TRUE_IV = 0.085          # AUG-ish, 706 rpm
MAG = 42
POLL = 0.032             # every AMMO_OCR_EVERY frames at ~93 fps


def counter_trace(iv=TRUE_IV, mag=MAG, poll=POLL, corrupt=()):
    """What the OCR would have seen: rounds_left sampled every poll."""
    out = []
    t = 0.0
    while True:
        left = mag - int(t / iv)
        if left < 0:
            break
        out.append((round(t, 6), left))
        t += poll
    for i, wrong in corrupt:
        out[i] = (out[i][0], wrong)
    return out


iv, n, resid = fit_interval(counter_trace())
close('clean 42-round magazine at 85.0 ms', iv, TRUE_IV, 0.002)
check('...uses every round it saw', n >= 30, True)

# The regression this was rewritten for: the first version split the trace into
# monotone runs and kept the longest, so ONE bad digit mid-magazine cut 42
# usable rounds to a fragment of five and the fit was then rejected for being
# too short. Two AUG magazines in a row measured nothing that way.
iv, n, resid = fit_interval(counter_trace(corrupt=[(9, 3), (17, 40), (25, 8)]))
close('three misread digits mid-burst', iv, TRUE_IV, 0.002)
check('...and does not lose the magazine to them', n >= 30, True)

# Only the FIRST sighting of a count carries information; the counter sitting
# on the same number for three polls is the same transition seen again.
doubled = [x for x, in zip(counter_trace()) for _ in (0, 1)]
iv, _, _ = fit_interval(doubled)
close('repeated sightings change nothing', iv, TRUE_IV, 0.002)

print('  -- and refuses rather than guessing:')
# A real trace out of docs/recoil/runs/verify14_0802.jsonl. Three polls is enough to
# draw a line through and the line would look excellent (resid 0), which is
# exactly why the round floor exists: a confident wrong fire rate does not add
# noise, it slides every later bullet into the wrong bin.
sparse = [(0.0369, 42), (2.3106, 15), (3.2351, 4)]
check('a 3-poll trace fits nothing', fit_interval(sparse)[0], None)
check('fewer than 3 distinct counts', fit_interval([(0.0, 42), (0.1, 41)])[0],
      None)
check('an empty trace', fit_interval([])[0], None)


# ══════════════════════════════════════════════════════════════
print('\n=== analyse: where the view went, per bullet ===')
IV = 0.100
K = 2.0

# Deliberately IRREGULAR frame times, none of them landing on a bin edge. The
# motion is a constant rate, so the cumulative curve is a straight line and
# every whole bullet bin must receive exactly the same amount -- no matter
# where the frames fall. The version that summed whole frame pairs into
# whichever bin their timestamp landed in cannot satisfy this: a pair
# straddling an edge went entirely to one side, and at 12% of a bin that was
# rms 4.71 counts of per-bullet noise.
RATE = 300.0                                   # counts/s, so 30 per bullet
gaps = [0.011, 0.017, 0.009, 0.013, 0.021, 0.008, 0.015, 0.012, 0.019, 0.010]
ts = [0.0]
for i in range(60):
    ts.append(round(ts[-1] + gaps[i % len(gaps)], 6))
dy = [RATE * (ts[1] - ts[0]) * K]              # the prepended origin's own gap
dy += [RATE * (ts[i] - ts[i - 1]) * K for i in range(1, len(ts))]
dy = dy[:len(ts)]
a = analyse(make_result(ts, dy), K, IV)
covered = int((ts[-1] - (ts[1] - ts[0])) / IV)  # bins fully inside the record
per = a['per_bullet_counts'][:covered]
worst = max(abs(v - RATE * IV) for v in per)
close(f'constant motion splits evenly over {covered} bins', worst, 0.0, 0.05)
close('cum_counts is dy/K summed', a['cum_counts'],
      sum(dy) / K, 1e-6)

# The hand's own motion is in dy too, and without the Pico reporting it every
# nudge during a burst is booked as recoil.
still = analyse(make_result(ts, dy), K, IV)
nudged = analyse(make_result(ts, dy, human=[2.0] * len(ts)), K, IV)
close('the hand term comes back out of the residual',
      nudged['cum_counts'] - still['cum_counts'], 2.0 * len(ts), 1e-6)
close('...and is reported both net and absolute',
      nudged['human_abs_counts'], 2.0 * len(ts), 1e-6)

# Past half a patch the correlation peak wraps, so an out-of-range frame is not
# imprecise, it is wrong by a whole patch -- 83 counts at K=1.55.
oor = [False] * len(ts)
oor[7] = True
dropped = analyse(make_result(ts, dy, oor=oor), K, IV)
close('an out-of-range frame contributes nothing',
      still['cum_counts'] - dropped['cum_counts'], dy[7] / K, 1e-6)
check('...and is counted', dropped['n_dropped_oor'], 1)

# Bin 0 starts at the first SHOT, not the first frame captured: between the
# click going out over USB and the recoil appearing on screen there is 20-50 ms
# against an 88 ms bullet.
shifted = analyse(make_result(ts, dy), K, IV, first_shot_ts=ts[0] + 0.05)
close('the bins are anchored to the first shot', shifted['span_s'],
      ts[-1] - ts[0] - 0.05, 1e-9)

# How many rounds went out is the magazine's business. Derived from the
# recording's span it comes back one or two short every time, and a curve
# rebuilt from that can never catch up to the magazine.
padded = analyse(make_result(ts, dy), K, IV, n_bullets=covered + 8)
check('bins the recording never reached are flagged',
      padded['bullets_missing'] > 0, True)
check('...and the curve is padded to the magazine',
      len(padded['per_bullet_counts']), covered + 8)

check('a recording of one frame is not a measurement',
      analyse(make_result([0.0], [1.0]), K, IV), None)


# ══════════════════════════════════════════════════════════════
print('\n=== magazine_fault: the gates, against every magazine ever logged ===')

# A logged magazine was ACCEPTED when it ran. These six are refused by today's
# gates, and each one is the gate doing its job on data that predates it:
#
#   vss/bare x4 -- the whole cell is ruined, not the magazines. Its
#     pattern_counts is -307: a NEGATIVE compensation curve, fired at a weapon
#     whose fixed 4x PSO-1 makes the red-dot K wrong anyway. The residuals
#     swing +477, -525, -589, -364, +125 between magazines of one cell.
#   g36c/muzzle+grip mag 4, aug/muzzle+grip mag 3 -- 5.3 and 7.0 sigma from
#     their own cell. Both logged before the z-gate existed.
KNOWN_BAD = {
    ('all_p1.jsonl', 'vss', 'bare', 1),
    ('all_p1.jsonl', 'vss', 'bare', 2),
    ('all_p1.jsonl', 'vss', 'bare', 3),
    ('all_p1.jsonl', 'vss', 'bare', 4),
    ('all_p1.jsonl', 'g36c', 'muzzle+grip', 4),
    ('aug_verify2_0802.jsonl', 'aug', 'muzzle+grip', 3),
}

seen_total = 0
refused = set()
reasons = {}
for path in sorted(glob.glob(os.path.join(RUNS, '*.jsonl'))):
    for line in open(path, encoding='utf-8'):
        try:
            rec = json.loads(line.strip() or '{}')
        except Exception:
            continue
        if rec.get('type') != 'cell' or not rec.get('pattern_counts'):
            continue
        earlier = []
        for mag in (rec.get('mags') or []):
            seen_total += 1
            # ads_frac was not always logged; NaN means the gate abstains,
            # which is what magazine_fault does with it.
            why = magazine_fault(mag, rec['pattern_counts'],
                                 rec.get('magazine_size'),
                                 mag.get('ads_frac', float('nan')), earlier)
            if why:
                key = (os.path.basename(path), rec['weapon'],
                       rec.get('config'), mag['mag'])
                refused.add(key)
                reasons[key] = why
            earlier.append(mag['cum_counts'])

print(f'  replayed {seen_total} accepted magazines from '
      f'{len(glob.glob(os.path.join(RUNS, "*.jsonl")))} logs')
for key in sorted(refused):
    mark = 'known' if key in KNOWN_BAD else 'NEW  '
    print(f'    {mark}  {key[1]}/{key[2]} mag {key[3]} ({key[0]}): '
          f'{reasons[key]}')

check('no magazine the gates once accepted is newly refused',
      sorted(refused - KNOWN_BAD), [])
# The other direction matters just as much: a gate that stopped catching these
# has been loosened, and nothing else in the pipeline would say so.
missing = sorted(k for k in KNOWN_BAD if k not in refused
                 and os.path.exists(os.path.join(RUNS, k[0])))
check('the known-bad magazines are still caught', missing, [])

print('  -- each gate, on a magazine built to trip exactly it:')
GOOD = {'per_bullet_counts': [10.0] * 40, 'human_abs_counts': 5.0,
        'n_out_of_range': 0, 'n_frames': 400, 'cum_counts': 50.0}
check('a healthy magazine passes',
      magazine_fault(GOOD, 1000.0, 40, 0.95, []), None)


def trips(name, mag=None, ads=0.95, mag_size=40, pattern=1000.0, seen=()):
    m = dict(GOOD, **(mag or {}))
    why = magazine_fault(m, pattern, mag_size, ads, list(seen))
    print(f'  {"ok  " if why else "FAIL"}  {name:<52} '
          f'{why or "PASSED, and should not have"}')
    if not why:
        FAILS.append(name)


trips('hip fire (the scoped K reads ~3x high)', ads=0.5)
trips('a magazine that fired the wrong number of rounds',
      {'per_bullet_counts': [10.0] * 30})
trips('the hand moved during the burst', {'human_abs_counts': 100.0})
trips('the correlator lost the view',
      {'n_out_of_range': 40, 'n_frames': 400})
trips('implied recoil is negative', {'cum_counts': -2000.0})
trips('residual miles from the cell\'s other magazines',
      {'cum_counts': 900.0}, seen=[40.0, 45.0, 38.0, 42.0])
check('...but not when there is nothing to compare against yet',
      magazine_fault(dict(GOOD, cum_counts=900.0), 1000.0, 40, 0.95,
                     [40.0, 45.0]), None)
check('an unlogged ads_frac abstains rather than refusing',
      magazine_fault(GOOD, 1000.0, 40, float('nan'), []), None)


print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
    sys.exit(1)
print('all ok')
