"""How much does ONE frame pair over-read, as a function of ITS OWN motion?

    pixi run python tools/probe_correlator_bias.py --grid --trials 10

⚠ THE ANSWER IS A CURVE b(delta), NOT A NUMBER, and the two-arm version of this
probe measured it at exactly two points -- both of them outside the range a real
burst lives in. Measured over the whole sample store (272 magazines):

    real burst, per-pair |dy|    p25 0.90   MEDIAN 2.00   p75 3.78 px
    old probe, one-pair arm                      108 px
    old probe, many-pairs arm                   0.48 px

478 pairs per magazine, and the +7.54% correction the store now carries was
extrapolated from the 0.48 px end. If the over-read is a fixed 0.04 px per pair
a magazine is 1.2% high; if it is a fixed FRACTION of each pair's motion it is
7.5% high. Nothing measured so far separates those, because separating them
needs the two decades in between.

THE GRID. Each cell injects `counts` over `spread_s`, so the pairs and the
per-pair displacement move together (delta = counts*K/n):

    spread 0     ONE correlation carries the whole thing
    spread > 0   the same total spread over ~130*spread pairs
    counts 0     STILL: nothing injected at all -- see below

    total_px(cell) = counts*K_true + n * b(delta)

TWO INDEPENDENT ROUTES TO K_true, AND THEY HAVE TO AGREE:

  1. the one-pair arm swept over step size. n = 1, so
         K1(counts) = K_true + b/counts
     and regressing K1 on 1/counts gives INTERCEPT = K_true, SLOPE = b. The
     lever is 3.5x (20..70 counts) and the residuals are what tests whether b
     is really flat across that range.
  2. every other cell, given K_true, yields b(delta) = (K_cell - K_true)*counts/n

⚠ TWO CELLS SHARE EACH delta AT DIFFERENT n, AND THAT IS THE FALSIFIABLE PART.
(140, 0.40) sees the same per-pair motion as (70, 0.20) with twice the pairs and
twice the total. They CANNOT jointly solve for K_true -- holding delta fixed
forces n/counts = K/delta, so both cells put the identical coefficient on b --
but they must land on the same K. If they do not, the over-read depends on
something other than the pair's own displacement, and the whole b(delta) framing
is wrong.

⚠ THE STILL CELL IS THE CONTROL THAT COSTS NOTHING AND WOULD REWRITE EVERYTHING.
Nothing is injected for the same 1.5 s. An over-read that needs motion sums to
~0 here, because a static scene's readings are noise about zero with no sign to
align to. A still cell that accumulates means the correlator drifts on its own,
and then every trajectory in the store is wrong by (pairs x drift) regardless of
what the gun did.

⚠ 80 COUNTS IS THE LARGEST SINGLE STEP THAT IS STILL MEASURABLE. At the ADS K
it is ~121 px against a correlator unambiguous only to RECOIL_PATCH_H/2 = 128
px. Past that it wraps by exactly 256 px and reports a SMALL displacement, which
is how four K calibration runs were quietly ruined (tools/audit_k.py). Hence the
per-pair alias gate below, and hence the one-pair arm stopping at 70.

⚠ NO CLICK, SO NO GUN BEHAVIOUR ENTERS. Only mouse.move(). The trigger is what
forced every earlier probe into contortions -- an empty magazine refills, bare
hands punch -- and none of it applies here.

⚠ EVERY CELL FIRES BOTH WAYS BACK TO BACK, so the mouse gets +N then -N and the
view cannot walk into the pitch clamp or onto open sky. Both were paid for.

--------------------------------------------------------------------------------
WHAT THE TWO-ARM VERSION SAID (2026-08-08, still true, just not the whole story)

    one-pair    1.5413 +- 0.0011      many-pairs  1.6574 +- 0.0046
    many - one  +7.54%, 24.4 sigma over a median of 223 pairs
              = 0.036 px of over-read per pair AT delta = 0.48 px

It settled that the correlator over-reads and that the excess accumulates with
pair count -- which killed BOTH stored values of K (1.5171 from drop-duplicates
and 1.5520 from keep-duplicates were two doses of the same artefact). It did not
settle how b behaves between 0.48 and 108 px, and that is this grid's job.
"""
import argparse
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np

from control.session import ensure_ready
from calibration.sweep import Rig

# ⚠ THE GATE IS 127, NOT 118. An earlier 118 threw away EVERY one-pair trial at
# 70 counts (they read 123.1 px) -- a gate meant to catch wraps, killing
# readings that were not wrapped. A wrap reports a SMALL number (it subtracts
# 256), so "too big" is the wrong shape for that gate: the only thing it can
# honestly refuse is a reading sitting at the ceiling itself.
ALIAS_PX = 127.0
SETTLE_S = 0.35
TEXTURE_MIN = 40.0

# The original two-arm test, kept so the old invocation still means what it did.
PAIR_CELLS = [(70, 0.0), (70, 1.5)]

# (counts, spread_s). spread 0 = one correlation. counts 0 = inject nothing.
# The two marked cells share a per-pair displacement with a cell above them at a
# different pair count; they are the check that b depends on delta and only on
# delta.
GRID_CELLS = [
    (20, 0.00), (35, 0.00), (50, 0.00), (70, 0.00),
    (70, 0.10), (70, 0.20), (70, 0.40), (70, 0.80), (70, 1.50),
    (140, 0.40),          # same delta as (70, 0.20), 2x the pairs
    (35, 0.20),           # same delta as (70, 0.40), 0.5x the pairs
    (0, 1.50),            # STILL
]


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


def one_pair(rig, grabber, sign, counts):
    """The whole displacement across a SINGLE correlation."""
    for _ in range(3):
        grabber.grab_timed()
    a = _first_patch(rig, grabber)
    rig.mouse.move(0, -sign * counts)
    time.sleep(SETTLE_S)
    for _ in range(2):
        grabber.grab_timed()          # flush anything caught mid-motion
    b = _first_patch(rig, grabber)
    m = rig.tracker.measure_pair(a, b, 0.0)
    d = abs(m.dy) if np.isfinite(m.dy) else float('nan')
    return {'px': d, 'pairs': 1, 'mean_abs': d, 'max_abs': d, 'dur': SETTLE_S}


def many_pairs(rig, grabber, sign, counts, spread_s):
    """The same displacement summed over every presented frame."""
    for _ in range(3):
        grabber.grab_timed()
    prev = _first_patch(rig, grabber)
    n_steps = max(5, int(spread_s * 200))
    t0 = time.perf_counter()

    def inject():
        acc = 0.0
        for i in range(n_steps):
            dt = t0 + spread_s * i / n_steps - time.perf_counter()
            if dt > 0:
                time.sleep(dt)
            acc += counts / n_steps
            s = int(acc)
            acc -= s
            if s:
                rig.mouse.move(0, -sign * s)

    if counts:
        threading.Thread(target=inject, daemon=True).start()
    # ⚠ EVERY PAIR IS TIMESTAMPED, because the window runs spread + SETTLE and
    # the settle tail is PURE STILL FRAMES. Run A charged each cell's excess to
    # every pair in the window, and at spread 0.10 that is 44 still pairs
    # against 13 moving ones -- it divided by 4.5x too many, and the per-pair
    # displacement it reported was the average of a MIXTURE. The cells came back
    # squeezed into 0.50..2.27 px when they were built to span 0.6..8.5.
    total, absd, ts = 0.0, [], []
    while time.perf_counter() < t0 + spread_s + SETTLE_S:
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
            absd.append(abs(m.dy))
            ts.append(time.perf_counter() - t0)
    dur = time.perf_counter() - t0
    if not absd:
        return {'px': float('nan'), 'pairs': 0, 'mean_abs': float('nan'),
                'max_abs': float('nan'), 'dur': dur}
    a, t = np.asarray(absd), np.asarray(ts)
    mv = t <= spread_s + 1.0 / 60.0     # one frame of grace for the last step
    return {'px': abs(total) if counts else total, 'signed': total,
            'pairs': len(absd), 'mean_abs': float(a.mean()),
            'max_abs': float(a.max()), 'sum_abs': float(a.sum()),
            'moving': int(mv.sum()),
            'delta_moving': float(a[mv].mean()) if mv.any() else float('nan'),
            'tail_abs': float(a[~mv].sum()),
            'dur': dur}


def run_cell(rig, grabber, counts, spread_s, sign):
    if spread_s <= 0:
        return one_pair(rig, grabber, sign, counts)
    return many_pairs(rig, grabber, sign, counts, spread_s)


def fit_intercept(x, y):
    """Least squares y = a + b*x, with the standard errors."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    n = len(x)
    b, a = np.polyfit(x, y, 1)
    resid = y - (a + b * x)
    dof = n - 2
    s2 = float(resid @ resid) / dof if dof > 0 else float('nan')
    sxx = float(((x - x.mean()) ** 2).sum())
    se_b = (s2 / sxx) ** 0.5 if sxx > 0 else float('nan')
    se_a = (s2 * (1.0 / n + x.mean() ** 2 / sxx)) ** 0.5 if sxx > 0 else float('nan')
    return a, se_a, b, se_b, resid


def burst_projection(b_of_delta):
    """What b(delta) does to a real magazine, using the store's own pairs."""
    import glob
    import json
    from calibration.samples import SAMPLE_DIR
    tot_true, tot_bias, n_mag = 0.0, 0.0, 0
    for f in sorted(glob.glob(os.path.join(SAMPLE_DIR, '*.jsonl'))):
        if 'MISLABEL' in f or 'INVALID' in f:
            continue
        for line in open(f, encoding='utf-8'):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            dy = d.get('dy_px')
            if not dy:
                continue
            a = np.asarray(dy, dtype=float)
            a = a[np.isfinite(a)]
            if len(a) < 20:
                continue
            tot_true += float(np.abs(a).sum())
            tot_bias += float(sum(b_of_delta(v) for v in np.abs(a)))
            n_mag += 1
    return n_mag, tot_true, tot_bias


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--trials', type=int, default=10)
    ap.add_argument('--grid', action='store_true',
                    help='sweep step size AND pair count instead of the '
                         'original two arms')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--weapon', default='mp5k')
    ap.add_argument('--countdown', type=int, default=8)
    ap.add_argument('--selftest', action='store_true',
                    help='prove the judge against three known b(delta), no '
                         'game and no hardware')
    ap.add_argument('--save', default='',
                    help='write the raw per-trial rows here. ⚠ REQUIRED for '
                         'replication: nothing moves K until two independent '
                         'runs agree, and only saved rows can be compared')
    ap.add_argument('--replicate', default='',
                    help='an earlier --save file. Reports each run separately '
                         'and then whether they agree')
    ap.add_argument('--reanalyse', default='',
                    help='re-report a --save file. No game, no hardware — the '
                         'analysis changed after run A and its rows have to be '
                         'readable under the new one')
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.reanalyse:
        import json
        with open(a.reanalyse, encoding='utf-8') as fh:
            rows = json.load(fh)
        print(f'reanalysing {len(rows)} rows from {a.reanalyse}')
        rc = report(rows)
        if a.replicate:
            rc = replicate(a.replicate, rows) or rc
        return rc
    cells = GRID_CELLS if a.grid else PAIR_CELLS

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
    rows = []
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
            for counts, spread in cells:
                signs = (+1,) if counts == 0 else (+1, -1)
                for sign in signs:
                    m = run_cell(rig, grabber, counts, spread, sign)
                    tag = f'{counts:3d}c/{spread:.2f}s'
                    if not np.isfinite(m['px']) or m['pairs'] == 0:
                        print(f'  r{r} {tag} — DROPPED, no finite pair')
                        continue
                    if counts and m['px'] < 5:
                        print(f'  r{r} {tag} {m["px"]:.2f} px — DROPPED, dead')
                        continue
                    if m['max_abs'] > ALIAS_PX:
                        print(f'  r{r} {tag} worst pair {m["max_abs"]:.1f} px > '
                              f'{ALIAS_PX:.0f} — DROPPED, at the wrap ceiling')
                        continue
                    m.update(counts=counts, spread=spread, sign=sign, trial=r)
                    rows.append(m)
                    k = m['px'] / counts if counts else float('nan')
                    print(f'  r{r} {tag} {m["px"]:8.2f} px  n={m["pairs"]:4d}  '
                          f'delta={m["mean_abs"]:7.3f}  K={k:.4f}')
                # +N then -N cancel exactly, so nothing accumulates
    finally:
        rig.close()

    if not rows:
        print('[!] nothing survived.')
        return 2
    if a.save:
        import json
        os.makedirs(os.path.dirname(os.path.abspath(a.save)), exist_ok=True)
        with open(a.save, 'w', encoding='utf-8') as fh:
            json.dump(rows, fh)
        print(f'  rows -> {a.save}')
    rc = report(rows)
    if a.replicate:
        rc = replicate(a.replicate, rows) or rc
    else:
        print()
        print('⚠ ONE RUN. Nothing here moves K until a second, independent run '
              'agrees — see MODEL.md sec.3. Re-run with '
              f'--replicate <the --save file from this run>.')
    return rc


def replicate(path, rows_b):
    """Two independent runs, side by side. The gate MODEL.md sec.13 says is
    missing: eta survived one run at 4.7 sigma and died on the second."""
    import json
    import contextlib
    import io
    with open(path, encoding='utf-8') as fh:
        rows_a = json.load(fh)
    print()
    print('=' * 72)
    print(f'REPLICATION   run A = {path}   run B = this run')
    out = {}
    for tag, rr in (('A', rows_a), ('B', rows_b)):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report(rr)
        out[tag] = buf.getvalue()

    # ⚠ ANCHORED REGEXES, NOT "split on a substring". The first version keyed
    # `store bias %` on 'of every trajectory', which sits at the END of its
    # line, so split(needle)[1] was empty and the whole column printed nan --
    # in the one table whose entire job is to say whether two runs agree. A
    # comparison that silently reports nan is worse than no comparison.
    import re
    pats = (
        ('K_true', r'K_true = ([-\d.]+)', ''),
        ('b (one-pair slope, px)', r'\bb = ([-+\d.]+) \+-', ''),
        ('K_eff (real burst)', r'K_eff for a real burst = ([-\d.]+)', ''),
        ('store bias %', r'= ([-+\d.]+)% of every trajectory', '%'),
    )

    def pick(text, pat):
        m = re.search(pat, text)
        return float(m.group(1)) if m else float('nan')

    print(f'{"quantity":>28}  {"run A":>10}  {"run B":>10}  {"delta":>10}')
    bad = []
    for name, pat, unit in pats:
        va, vb = pick(out['A'], pat), pick(out['B'], pat)
        if not (np.isfinite(va) and np.isfinite(vb)):
            bad.append(name)
        print(f'{name:>28}  {va:10.4f}  {vb:10.4f}  {vb-va:+10.4f}{unit}')
    if bad:
        print(f'  [!] could not read {", ".join(bad)} out of both reports — '
              f'that is a broken comparison, not an agreement')
    print()
    print('  ⚠ AGREEMENT IS THE VERDICT, not either run on its own. A quantity '
          'that moves between two runs of the same probe is a property of the '
          'run, not of the correlator.')
    print('  Run A in full:')
    print('\n'.join('    ' + l for l in out['A'].splitlines()))
    return 0


def report(rows):
    """⚠ EVERY PER-PAIR NUMBER HERE IS OVER THE MOVING PAIRS ONLY.

    The measurement window is spread + SETTLE_S and the settle tail is still
    frames. Run A's analysis divided each cell's excess by EVERY pair in the
    window and averaged |dy| the same way, which at spread 0.10 means 44 still
    pairs diluting 13 moving ones. It squeezed cells built to span 0.6..8.5 px
    into 0.50..2.27 and reported the store as 0.86% LOW, when the moving-pair
    accounting on the same rows says the opposite.

    Rows without per-pair timing (anything saved before that was recorded) fall
    back to the nominal moving fraction and are flagged INFERRED, because a
    number derived from the schedule is not the same evidence as a number read
    off the frames.
    """
    print()
    keys = sorted({(r['counts'], r['spread']) for r in rows})
    inferred = False
    print(f'{"cell":>13}  {"n":>3}  {"pairs":>5}  {"moving":>6}  '
          f'{"delta_px":>9}  {"total_px":>9}  {"K":>8}')
    cell = {}
    for counts, spread in keys:
        sel = [r for r in rows if r['counts'] == counts and r['spread'] == spread]
        px = np.array([r['px'] for r in sel])
        if 'moving' in sel[0]:
            mv = np.array([r['moving'] for r in sel], float)
            delta = np.array([r['delta_moving'] for r in sel])
            tail = float(np.mean([r.get('tail_abs', 0.0) for r in sel]))
        elif spread > 0:
            # nominal fraction of the window that carried motion
            inferred = True
            frac = spread / (spread + SETTLE_S)
            mv = np.array([r['pairs'] for r in sel], float) * frac
            delta = np.array([abs(r['px']) for r in sel]) / np.maximum(mv, 1)
            tail = float('nan')
        else:
            mv = np.ones(len(px))
            delta = np.array([r['mean_abs'] for r in sel])
            tail = 0.0
        pairs = np.array([r['pairs'] for r in sel], float)
        sd = px.std(ddof=1) if len(px) > 1 else float('nan')
        k = px / counts if counts else None
        cell[(counts, spread)] = {
            'n': len(px), 'pairs': pairs.mean(), 'moving': mv.mean(),
            'delta': float(np.mean(delta)), 'tail': tail,
            'px': px.mean(), 'px_sem': sd / max(len(px), 1) ** 0.5,
            'K': float(k.mean()) if k is not None else float('nan'),
            'K_sem': float(k.std(ddof=1) / len(k) ** 0.5)
            if k is not None and len(k) > 1 else float('nan'),
        }
        c = cell[(counts, spread)]
        kt = '      --' if not counts else f'{c["K"]:.4f}'
        print(f'{counts:6d}c/{spread:.2f}s  {c["n"]:3d}  {c["pairs"]:5.0f}  '
              f'{c["moving"]:6.0f}  {c["delta"]:9.3f}  {c["px"]:9.2f}  {kt}')
    if inferred:
        print('  ⚠ INFERRED moving-pair counts: these rows carry no per-pair '
              'timing, so the split came from the nominal schedule, not the '
              'frames. One inference hop, and it is stated rather than hidden.')

    still = [r for r in rows if r['counts'] == 0]
    still_b = still_se = float('nan')
    if still:
        s = np.array([r.get('signed', r['px']) for r in still])
        pr = np.mean([r['pairs'] for r in still])
        sem = s.std(ddof=1) / len(s) ** 0.5 if len(s) > 1 else float('nan')
        still_b, still_se = s.mean() / pr, sem / pr
        print()
        print(f'STILL ({len(s)} x {pr:.0f} pairs, nothing injected)')
        print(f'  signed total   {s.mean():+7.3f} +- {sem:.3f} px  '
              f'= {still_b:+.5f} +- {still_se:.5f} px per pair')
        print(f'  per-pair |dy| noise floor  {np.mean([r["mean_abs"] for r in still]):.3f} px')
        print('  ~0  the over-read needs motion; a static scene does not drift')
        print('  !=0 the correlator drifts on its own and EVERY trajectory in '
              'the store is wrong by (pairs x drift), whatever the gun did')
        print('  ⚠ this is also the delta -> 0 end of b(delta) below, so the two '
              'have to join up')

    ones = [(c, v) for c, v in cell.items() if c[1] == 0.0 and c[0]]
    if len(ones) < 3:
        print()
        print('[!] fewer than 3 one-pair step sizes — no K_true regression. '
              'Run with --grid.')
        return 0

    x = np.array([1.0 / c[0] for c, _ in ones])
    y = np.array([v['K'] for _, v in ones])
    k_true, se_k, b1, se_b, resid = fit_intercept(x, y)
    print()
    print('ONE-PAIR REGRESSION   K1 = K_true + b/counts      (n = 1 pair each)')
    for (c, v), rr in zip(ones, resid):
        print(f'  {c[0]:3d} counts   delta {v["delta"]:7.2f} px   '
              f'K {v["K"]:.4f} +- {v["K_sem"]:.4f}   resid {rr:+.4f}')
    print(f'  K_true = {k_true:.4f} +- {se_k:.4f}      '
          f'b = {b1:+.3f} +- {se_b:.3f} px per pair')
    print(f'  stored K = 1.5413 (the old one-pair arm at 70 counts) — '
          f'{100*(k_true/1.5413-1):+.2f}%')
    if abs(b1) > 3 * se_b and np.isfinite(se_b):
        print('  the slope is real: a single pair over-reads by a FIXED amount, '
              'so the old one-pair K was itself biased by b/70')
    else:
        print('  the slope is not separable from zero over 20..70 counts: '
              'either b is tiny at large delta, or b scales WITH delta there '
              '(both give a flat line, and only the small-delta cells separate '
              'them)')

    # b = (total - counts*K_true)/pairs, so its error carries the cell's own
    # scatter AND the intercept's. The intercept term is COMMON to every cell,
    # which matters for the matched-delta test below.
    print()
    print('b(delta) — px of over-read per frame pair, given that K_true')
    print(f'{"cell":>13}  {"delta_px":>9}  {"pairs":>6}  {"b_px":>17}  '
          f'{"b/delta":>8}')
    bs = []
    if np.isfinite(still_b):
        bs.append((0.0, still_b, still_se, 0, 0.0, float('nan')))
        print(f'{"STILL":>13}  {0.0:9.3f}  {"":>6}  '
              f'{still_b:+9.4f} +-{still_se:.4f}  {"--":>8}')
    for c in sorted(cell, key=lambda c: cell[c]['delta']):
        counts, spread = c
        if not counts:
            continue
        v = cell[c]
        b = (v['px'] - counts * k_true) / v['moving']
        se = ((v['px_sem'] ** 2 + (counts * se_k) ** 2) ** 0.5) / v['moving']
        bs.append((v['delta'], b, se, counts, spread, v['moving']))
        print(f'{counts:6d}c/{spread:.2f}s  {v["delta"]:9.3f}  '
              f'{v["moving"]:6.0f}  {b:+9.4f} +-{se:.4f}  '
              f'{100*b/v["delta"]:7.2f}%')

    print()
    print('MATCHED delta, DIFFERENT pair count — b must agree or b is not a '
          'function of delta alone')
    print('  ⚠ K_true CANCELS here: matched delta forces counts/pairs to match, '
          'so both cells put the same coefficient on it. This is the one '
          'comparison the intercept cannot corrupt.')
    for lo, hi in (((70, 0.20), (140, 0.40)), ((35, 0.20), (70, 0.40))):
        if lo not in cell or hi not in cell:
            continue
        a_, b_ = cell[lo], cell[hi]
        ba = (a_['px'] - lo[0] * k_true) / a_['moving']
        bb = (b_['px'] - hi[0] * k_true) / b_['moving']
        leak = abs(lo[0] / a_['moving'] - hi[0] / b_['moving']) * se_k
        se_d = ((a_['px_sem'] / a_['moving']) ** 2
                + (b_['px_sem'] / b_['moving']) ** 2 + leak ** 2) ** 0.5
        sig = abs(ba - bb) / se_d if se_d > 0 else float('inf')
        print(f'  delta {a_["delta"]:5.2f} vs {b_["delta"]:5.2f} px   '
              f'b {ba:+.4f} ({a_["moving"]:.0f} moving) vs '
              f'{bb:+.4f} ({b_["moving"]:.0f} moving)   '
              f'{sig:.1f} sigma  {"AGREE" if sig < 3 else "DISAGREE"}')

    # ⚠ THE POWER-LAW FIT IS NOT THE VERDICT AND WAS WRONG ON A DRY RUN. Fed
    # data built with b = 0.040 px flat, the log-log slope came back -0.23,
    # because the one-pair cells carry b to +-0.08 px (n=1, so the cell's whole
    # scatter lands on a single pair) and logs cannot take the negative ones.
    # The verdict is the weighted chi-square of the two candidate models; the
    # exponent is printed after it, as colour.
    d = np.array([v[0] for v in bs])
    bb = np.array([v[1] for v in bs])
    se = np.array([v[2] for v in bs])
    w = 1.0 / np.maximum(se, 1e-9) ** 2
    ok = np.isfinite(bb) & np.isfinite(se) & (se > 0)
    print()
    print('WHICH MODEL — weighted over every cell above, chi2 per degree of '
          'freedom')
    if ok.sum() >= 3:
        b_flat = float((w[ok] * bb[ok]).sum() / w[ok].sum())
        chi_a = float((w[ok] * (bb[ok] - b_flat) ** 2).sum()) / (ok.sum() - 1)
        beta = float((w[ok] * bb[ok] * d[ok]).sum()
                     / max((w[ok] * d[ok] ** 2).sum(), 1e-12))
        chi_b = float((w[ok] * (bb[ok] - beta * d[ok]) ** 2).sum()) / (ok.sum() - 1)
        print(f'  A  fixed px per pair    b = {b_flat:+.4f} px         '
              f'chi2/dof {chi_a:7.2f}')
        print(f'  B  fixed fraction       b = {100*beta:+.3f}% of delta  '
              f'chi2/dof {chi_b:7.2f}')
        worse = ('' if min(chi_a, chi_b) < 3 else
                 '  ⚠ BUT NEITHER FITS: chi2/dof > 3 means b(delta) is some '
                 'other shape, and the interpolation below is the only '
                 'honest summary')
        print(f'  -> {"A" if chi_a < chi_b else "B"} fits better{worse}')
        pos = ok & (d > 0) & (bb > 0)
        if pos.sum() >= 3:
            g = np.polyfit(np.log(d[pos]), np.log(bb[pos]), 1,
                           w=np.sqrt(w[pos]) * bb[pos])[0]
            print(f'  (weighted log-log slope {g:+.2f} over the {pos.sum()} '
                  f'positive cells — colour only, see the note in the source)')

    order = np.argsort(d)

    def b_of(delta):
        return float(np.interp(delta, d[order], bb[order]))

    n_mag, tot_true, tot_bias = burst_projection(b_of)
    print()
    print(f'PROJECTED ONTO THE STORE ({n_mag} magazines, every pair, '
          f'b interpolated over the measured deltas)')
    print(f'  summed |dy|   {tot_true:12.0f} px')
    print(f'  summed bias   {tot_bias:12.0f} px   '
          f'= {100*tot_bias/tot_true:+.2f}% of every trajectory')
    print(f'  K_eff for a real burst = {k_true*(1+tot_bias/tot_true):.4f}   '
          f'(K_true {k_true:.4f}, stored 1.5413)')
    print('  ⚠ THIS is the number config.py should hold, not K at any single '
          'step. A bias that is a fixed FRACTION of each pair is absorbed into '
          'K and cancels; only the part of b/delta that VARIES with delta '
          'survives, because K is calibrated at one delta and applied at '
          'another.')
    print('  ⚠ still a projection, not a measurement. Same replication rule as '
          'anything else before it moves K.')
    return 0


# --- the judge, proved against truths it is supposed to tell apart -----------

def _synth(model, param, trials=10, k=1.5400, seed=0):
    """Rows built from a KNOWN b(delta), to see whether report() reads it back."""
    # ⚠ THE SYNTHETIC WINDOW CARRIES THE SETTLE TAIL TOO, so the judge is tested
    # on the same moving/still split the real cells have. Without it the
    # selftest would pass on rows that never exercise the bug run A hit.
    rng = np.random.default_rng(seed)
    rows = []
    for r in range(trials):
        for counts, spread in GRID_CELLS:
            n = 1 if spread == 0 else max(1, int(130 * (spread + SETTLE_S)))
            mv = 1 if spread == 0 else max(1, int(130 * spread))
            delta = counts * k / mv if counts else 0.0
            b = model(delta, param)
            px = counts * k + mv * b
            px += rng.normal(0, (0.10 ** 2 * n + (0.3 if n == 1 else 0) ** 2) ** 0.5)
            rows.append(dict(px=abs(px), signed=px, pairs=n, moving=mv,
                             delta_moving=delta if counts else 0.02,
                             tail_abs=0.0,
                             mean_abs=delta * mv / n if counts else 0.02,
                             max_abs=delta if counts else 0.05,
                             counts=counts, spread=spread, sign=1, trial=r))
    return rows


def selftest():
    """Three truths the judge must tell apart. It failed two of them once."""
    import contextlib
    import io
    # (title, b(delta), param, expectation, machine-checkable assertion)
    # ⚠ EVERY ASSERTION HAS TO BE ABLE TO FAIL. The judge's first version read
    # delta^-0.23 off data built dead flat, and printed it next to two other
    # blocks that looked equally reasonable.
    cases = [
        ('FLAT      b = 0.040 px per pair, whatever the pair did',
         lambda d, p: p, 0.040,
         'A wins, and the store reads ~1.5% high',
         lambda v: v['model'] == 'A' and 1.0 < v['proj'] < 2.2),
        ('PROPORTIONAL  b = 7.5% of each pair — ABSORBED BY K, harmless',
         lambda d, p: p * d, 0.075,
         'projection ~ 0% and K_true = 1.5400*1.075: K swallowed it whole',
         lambda v: abs(v['proj']) < 0.5 and abs(v['K'] / 1.5400 - 1.075) < 0.01),
        ('PEAK-LOCKING  b = 0.08*exp(-delta/2), all of it at small delta',
         lambda d, p: p * float(np.exp(-d / 2.0)), 0.080,
         'NEITHER model fits; the interpolation is the only honest summary',
         lambda v: v['neither']),
    ]
    ok = True
    for title, fn, p, expect, check in cases:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report(_synth(fn, p))
        out = buf.getvalue()
        lines = out.splitlines()

        def grab(needle, cast=str, default=float('nan')):
            for l in lines:
                if needle in l:
                    return l.strip()
            return None

        kl = grab('K_true =')
        pl = grab('of every trajectory')
        wl = grab('fits better')
        v = {'K': float('nan'), 'proj': float('nan'),
             'model': '?', 'neither': 'NEITHER FITS' in out}
        if kl:
            v['K'] = float(kl.split('K_true =')[1].split()[0])
        if pl:
            v['proj'] = float(pl.split('=')[-1].strip().rstrip('% of every trajectory').split('%')[0])
        if wl:
            v['model'] = wl.strip().split()[1]
        good = bool(kl and pl and wl) and check(v)
        ok = ok and good
        print(f'\n=== {title}')
        print(f'    expect: {expect}')
        for l in (kl, wl, pl):
            if l:
                print(f'    {l}')
        print(f'    {"PASS" if good else "FAIL"}   '
              f'(K_true {v["K"]:.4f}, model {v["model"]}, '
              f'projection {v["proj"]:+.2f}%)')
    print()
    print(f'{"3/3 — the judge tells the three apart" if ok else "FAILED"}')
    print('These are the three shapes b(delta) could have. A judge that cannot '
          'separate them cannot answer the question it was written for.')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
