"""samples.comp_counts_at vs a tick-exact copy of the firmware. -> exit 1.

    pixi run comp-counts

WHY THIS EXISTS. `comp_counts_at` is `C` in MODEL.md's identity

    y_true(t) = y_obs(t) + C(t - M)

and the collection arm that carries the answer is the COMPENSATED one, where
|y_obs| is about 36 counts against a C of about 900. So `C` is not a small
correction on that arm -- it IS the measurement, and an error in it lands
almost undiluted on every fitted curve. MODEL.md 6.1 item 1 has been the only
open model problem since the coordinate change, and it is the one item there
that is not a parameter but a FUNCTION THAT COMPUTES THE WRONG THING.

⚠ THE EVIDENCE THAT SOMETHING IS WRONG IS ALREADY IN, AND IT IS NOT FROM HERE.
tools/probe_delivery_path.py --hold-sweep fired it twice: curve/move RISES with
how long the button was held, +0.0148/s (2.8 sigma) and +0.0242/s (5.7 sigma),
peaking exactly when the curve finishes playing and flat after. Pixels per
count cannot know how long a button was held, so the trend has to be in the
integration. This file is the offline half of that: it does not need the game,
the Pico, or a magazine.

WHAT IS BEING COMPARED, and why a second implementation is legitimate here
when this repo's own rule is that parallel implementations drift:

    comp_counts_at      CONTINUOUS. Each knot's dy is spread linearly over its
                        duration; the cumulative curve is piecewise linear.
    _firmware_ticks     DISCRETE. What main.c actually does: called once per
                        millisecond, add `dy/dur` to a float accumulator, emit
                        int(accum) and carry the remainder.

The parallel-implementation rule says two copies of the same thing drift. This
is not two copies of the same thing -- it is the model and the machine, and the
whole point is to measure the gap between them. The rule that DOES apply is
that the copy must come from reading main.c, not from reading comp_counts_at,
which is why the source lines are quoted at each step below.

⚠ WHAT WAS ALREADY RULED OUT BEFORE WRITING THIS, so nobody re-runs it:

  the jitter    `jitter_bullet` multiplies by (1 + 0.02*rng) and adds
                0.2*rng per knot. That WAS a mean +2% and +0.2 counts always
                downward, because rng_float() returned [0, +2). ⚠ IT IS FIXED
                -- main.c now returns [-1, +1) and the two terms are zero-mean,
                so they add variance and no trend. Not the cause.
  int + carry   The docstring on comp_counts_at guesses "the LATE, SMALL knots
                -- where int(accum)-with-carry lives". Truncate-and-carry is
                EXACT in total: whatever is truncated stays in the accumulator
                and comes out on a later tick. Its only permanent loss is the
                sub-count remainder still held when the curve ends, which is
                bounded by 1 count and does not grow. ⚠ SO THAT GUESS IS WRONG,
                and this file records it rather than leaving it to be believed.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

import numpy as np                                             # noqa: E402

from calibration.samples import comp_counts_at                 # noqa: E402

FAILS = []

# A knot grid like the ones actually played: MODEL.md fits on a 17 ms grid.
GRID_MS = 17
# Long enough to cover a real burst and then some, so a trend has room to show.
HOLDS_S = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.4)
# The gap this has to resolve is 3-4% at burst length; anything that calls
# itself agreement has to be well under that.
AGREE_FRAC = 0.005


def _bullet_duration(tk, i):
    """main.c:371 bullet_duration -- the LAST knot reuses the gap before it."""
    prev_dur = (tk[i] - tk[i - 1]) if i > 0 else 100
    next_t = tk[i + 1] if i + 1 < len(tk) else tk[i] + prev_dur
    dur = next_t - tk[i]
    return 1 if dur < 1 else dur


def _firmware_ticks(curve, upto_ms, jitter=None, skip=None):
    """Cumulative counts EMITTED by the firmware after `upto_ms` 1 ms ticks.

    A transcription of main.c:402 get_recoil_delta, called once per ms from
    send_hid_output (main.c:910). `jitter` is a callable(i) -> multiplier so a
    caller can force the zero-mean terms on; None means the mean behaviour.

    ⚠ `skip` IS NOT A HYPOTHETICAL. main.c:530 returns from send_hid_output
    when `tud_hid_ready()` is false, and `last_send` has ALREADY been advanced
    on the line above -- so that millisecond is dropped and never made up.
    `skip` is a callable(elapsed) -> bool marking those dropped ticks, and it
    exists because this transcription used to model only the perfect 1 ms
    cadence, which is the one cadence under which the bug below cannot happen.
    """
    tk = [int(k['t_ms']) for k in curve]
    dy = [float(k.get('dy', 0.0)) for k in curve]
    durs = [float(curve[i]['dur_ms']) if 'dur_ms' in curve[i]
            else float(_bullet_duration(tk, i)) for i in range(len(tk))]
    durs = [1.0 if d < 1 else d for d in durs]

    fire_index = 0
    spread_per_ms = 0.0
    spread_until = 0
    accum = 0.0
    emitted = 0
    out = np.zeros(upto_ms + 1)
    for elapsed in range(upto_ms + 1):
        # main.c:530 -- tud_hid_ready() false: this millisecond never runs
        # get_recoil_delta at all, and last_send has already moved past it.
        if skip is not None and skip(elapsed):
            out[elapsed] = emitted
            continue
        # main.c:410 -- advance to newly reached knots, PAYING OUT whatever
        # their window already owes. It used to overwrite the spread rate per
        # knot, which silently dropped every knot the loop stepped past.
        while fire_index < len(tk) and tk[fire_index] <= elapsed:
            d = dy[fire_index] * (jitter(fire_index) if jitter else 1.0)
            dur = durs[fire_index]
            end = tk[fire_index] + dur
            if end <= elapsed:
                # Window entirely in the past: the whole delta is owed now.
                accum += d
                spread_per_ms = 0.0
                spread_until = 0
            else:
                # Partly elapsed: pay the ms already gone, spread the rest.
                per_ms = d / dur
                accum += per_ms * (elapsed - tk[fire_index])
                spread_per_ms = per_ms
                spread_until = end
            fire_index += 1
        # main.c:425 -- accumulate only while inside the current knot's window
        if elapsed < spread_until:
            accum += spread_per_ms
        # main.c:430 -- emit int(accum), carry the remainder
        iy = int(accum)            # C cast to int16_t: truncation toward zero
        accum -= iy
        emitted += iy
        out[elapsed] = emitted
    return out


def synth_curve(total_counts, n_knots, grid_ms=GRID_MS, shape=1.3):
    """A curve of the shape the fitter produces: rising, on a fixed grid."""
    t = np.arange(n_knots, dtype=float)
    cum = total_counts * ((t + 1) / n_knots) ** shape
    dy = np.diff(np.concatenate([[0.0], cum]))
    return [{'t_ms': int(i * grid_ms), 'dy': float(v)} for i, v in enumerate(dy)]


def check(label, cond, detail=''):
    print(f'  {"ok  " if cond else "FAIL"}  {label}' +
          (f'\n           {detail}' if detail and not cond else ''))
    if not cond:
        FAILS.append(label)


def main():
    print('comp_counts_at vs a tick-exact firmware transcription\n')

    curve = synth_curve(900.0, 200)            # 200 knots x 17 ms = 3.4 s
    played_ms = curve[-1]['t_ms'] + GRID_MS

    print(f'  curve: {len(curve)} knots on a {GRID_MS} ms grid, '
          f'{sum(k["dy"] for k in curve):.1f} counts over {played_ms} ms\n')

    print('  hold      model C     firmware     diff      diff %')
    rows = []
    for hold in HOLDS_S:
        ms = int(hold * 1000)
        model = float(comp_counts_at(curve, np.array([hold]))[0])
        fw = float(_firmware_ticks(curve, ms)[ms])
        d = fw - model
        pct = 100 * d / model if model else float('nan')
        rows.append((hold, model, fw, d, pct))
        print(f'  {hold:4.1f}s  {model:9.2f}  {fw:9.2f}  {d:+8.2f}  {pct:+7.3f}%')
    print()

    # ── 1. the difference is bounded in COUNTS, which is its natural unit ──
    #
    # ⚠ THIS CHECK WAS FIRST WRITTEN IN PERCENT AND THAT WAS THE WRONG
    # DIMENSION. Truncate-and-carry can hold back at most one count, so the gap
    # is a bounded ABSOLUTE offset; expressed as a fraction of a C that grows
    # from 74 to 900 counts it necessarily looks huge at the short holds
    # (-0.64%) and small at the long ones (-0.11%), and reading that as a trend
    # is the root CLAUDE.md's 判据必须能看见它要管的那个维度 in one line. The
    # quantity that is bounded is counts, so counts is what gets a threshold.
    inside = [r for r in rows if r[0] * 1000 <= played_ms]
    worst_counts = max(abs(r[3]) for r in inside)
    check('the two integrations differ by at most one carried count',
          worst_counts <= 1.0 + 1e-9,
          f'worst |diff| is {worst_counts:.2f} counts — more than one means '
          f'the two integrations really do disagree, not just round differently')

    # ── 2. and it does NOT grow with hold, which is the whole question ──
    #
    # The game measured curve/move RISING with hold duration, +0.0148/s and
    # +0.0242/s. On a C of ~265 counts/s those slopes are about 4 and 6 counts
    # per second of extra hold, so if this arithmetic were the cause the diff
    # column would climb by tens of counts across the sweep. A flat sub-count
    # offset cannot produce a trend of that size.
    hold_s = np.array([r[0] for r in inside])
    diffs = np.array([r[3] for r in inside])
    slope = float(np.polyfit(hold_s, diffs, 1)[0]) if len(inside) > 2 else 0.0
    check('...and shows no trend with hold duration',
          abs(slope) < 1.0,
          f'diff drifts {slope:+.2f} counts per second of hold; the screen '
          f'measured a gap needing about +4 to +6 counts/s')

    # ── 3. the total, once everything has played ──
    #
    # ⚠ THE ONE PERMANENT LOSS truncate-and-carry has is the remainder still in
    # the accumulator when the curve ends. It is bounded by ONE count and this
    # pins that, so nobody re-derives it as a candidate for a 3-4% effect.
    end = _firmware_ticks(curve, played_ms + 200)[-1]
    commanded = sum(k['dy'] for k in curve)
    check('the firmware emits the whole curve, less at most one carried count',
          0 <= commanded - end <= 1.0 + 1e-9,
          f'commanded {commanded:.2f}, emitted {end:.0f}, '
          f'lost {commanded - end:.3f} (the bound is ONE count: whatever is '
          f'truncated stays in the accumulator, so the only permanent loss is '
          f'the remainder still held when the curve stops)')

    # ── 3b. A DROPPED TICK MUST NOT LOSE A KNOT ──
    #
    # ⚠ THE HEAD OF THE CURVE IS ONE KNOT AND IT IS THE ONE THAT GETS LOST.
    # upload_pattern folds everything owed before the click into knot[0], so at
    # RECOIL_FIRE_DELAY_MS = -90 that single knot carries the whole head lead --
    # and `bullet_duration(0)` is the gap to knot[1], only 12 ms, where every
    # later knot has 17. A burst starts at the instant USB is busiest (button
    # state change + movement in the same report), so `tud_hid_ready()` false is
    # most likely exactly there.
    #
    # get_recoil_delta's while-loop OVERWRITES spread_*_per_ms per knot instead
    # of accumulating, so any knot the loop steps PAST in one tick contributes
    # nothing at all -- it is never added to recoil_accum. Drop enough ticks at
    # the start and knot[0]'s counts vanish silently.
    #
    # This is the shape of "只有第一发不压": the later knots have 17 ms windows
    # and survive, the folded head has 12 and does not.
    #
    # ⚠ AND IT IS WHY EVERY OTHER GATE HERE STAYED GREEN. The transcription
    # above modelled the perfect 1 ms cadence, which is the one cadence where a
    # tick can never step past two knots. CMD_RECOIL_SIM cannot see it either:
    # run_recoil_sim (main.c:690) walks the pattern array calling jitter_bullet
    # and never runs the time advance at all, so it measures the jitter and
    # nothing about playback, whatever its name suggests.
    folded = [{'t_ms': 0, 'dy': 8.5}] + [
        dict(k, t_ms=k['t_ms'] + 12) for k in synth_curve(900.0, 200)]
    ideal = _firmware_ticks(folded, played_ms + 400)[-1]
    dropped = _firmware_ticks(folded, played_ms + 400,
                              skip=lambda e: e < 14)[-1]
    check('a dropped tick does not silently discard a knot',
          abs(ideal - dropped) <= 1.0 + 1e-9,
          f'perfect cadence emits {ideal:.0f}, dropping the first 14 ticks '
          f'emits {dropped:.0f} — a gap of {ideal - dropped:.0f} counts, which '
          f'is knot[0] (the folded head) never reaching recoil_accum because '
          f'the while-loop overwrote its spread rate instead of adding it')

    # ── 4. the zero-mean jitter really is zero-mean ──
    #
    # ⚠ THIS IS THE HALF THAT WAS A REAL BUG, so it gets a gate rather than a
    # sentence. rng_float() returned [0, +2) and made both terms one-sided:
    # +2% magnitude and +0.2 counts, every knot, always downward -- about +29
    # counts on an AUG magazine, quietly absorbed into the stored curve. main.c
    # now returns [-1, +1). If it ever regresses, the mean moves off zero here.
    rng = np.random.default_rng(0)
    means = []
    for _ in range(60):
        j = rng.uniform(-1.0, 1.0, size=len(curve) + 4)
        got = _firmware_ticks(curve, played_ms + 200,
                              jitter=lambda i: 1.0 + 0.02 * j[i])[-1]
        means.append(got)
    bias = float(np.mean(means)) - end
    sem = float(np.std(means, ddof=1) / np.sqrt(len(means)))
    check('zero-mean jitter adds variance and no bias',
          abs(bias) < 3 * sem + 1.0,
          f'mean shift {bias:+.2f} counts (sem {sem:.2f}) on {commanded:.0f}')

    # ── 5. the ONE-SIDED jitter must be caught, or case 4 proves nothing ──
    #
    # A gate that only ever sees the fixed firmware is a gate that has been
    # watched passing. This replays the OLD rng_float() -- [0, +2) instead of
    # [-1, +1) -- and demands the same check notice.
    old = [1.0 + 0.02 * rng.uniform(0.0, 2.0) for _ in range(len(curve) + 4)]
    got = _firmware_ticks(curve, played_ms + 200, jitter=lambda i: old[i])[-1]
    check('...and the pre-fix one-sided rng WOULD be caught',
          got - end > 3 * sem + 1.0,
          f'one-sided jitter shifted the total by {got - end:+.2f} counts, '
          f'which this gate must call a bias')

    # ── what this does NOT rule out, made into a number worth measuring ──
    #
    # ⚠ THE SIMULATION ASSUMES ONE TICK PER MILLISECOND, and that is exactly
    # the assumption main.c cannot guarantee: send_hid_output is called from
    # the main loop right after tud_task() (main.c:908), so the cadence is
    # whatever the loop achieves, not a timer. It matters because the while
    # loop OVERWRITES the spread when a new knot is reached -- a knot that gets
    # fewer ticks than its duration never catches up, it is simply short, and
    # that shortfall repeats once per knot. So a slow loop under-delivers by an
    # amount that GROWS with how many knots have played, which is the shape the
    # screen measured.
    #
    # This prints what tick period would be needed to produce it, so the next
    # step is a measurement of one number rather than another hypothesis.
    print('  if the loop is SLOWER than 1 ms, knots are cut short and never '
          'catch up:\n')
    print('   tick    delivered     vs commanded    per second of hold')
    for tick_ms in (1.0, 1.05, 1.1, 1.25, 1.5):
        # A tick period of p ms means a knot of duration d gets floor(d/p)
        # accumulations instead of d, so it delivers dy*floor(d/p)/d.
        got = 0.0
        for i, k in enumerate(curve):
            d = float(k.get('dur_ms', GRID_MS))
            got += k['dy'] * min(1.0, int(d / tick_ms) / d)
        short = commanded - got
        print(f'  {tick_ms:5.2f}ms  {got:9.1f}     {-100*short/commanded:+7.2f}%'
              f'      {-short/(played_ms/1000.0):+7.2f} counts/s')
    print('\n  ⚠ NEGATIVE means the firmware delivers LESS than commanded. The '
          'screen\n     measured curve/move RISING with hold, i.e. reality '
          'delivering MORE\n     than the model says, so a slow loop has the '
          'WRONG SIGN and is not it\n     either. Recorded so nobody spends a '
          'session on it.\n')

    if FAILS:
        print(f'{len(FAILS)} FAILED: {FAILS}\n')
        print('  A failure here is INFORMATION, not necessarily a regression: '
              'cases 1 and 2 failing means the offline pair reproduces the gap '
              'the game measured, and the cause is in this file to find.')
        return 1
    print('all ok — the two integrations agree, so the measured 3-4% gap that '
          'GROWS with hold duration is NOT in the arithmetic modelled here.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
