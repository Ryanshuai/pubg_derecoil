"""A magazine's recording turned into numbers, and a verdict on whether to
believe them. No game, no hardware, no screen.

Everything here takes a MagazineResult (or a plain list of counter readings)
and returns numbers. That is the whole entry requirement, and it is worth
stating because these functions used to live in sweep.py next to the rig that
drives the game — so checking one against a stored trace meant importing a
Pico backend, a torch-backed fire-mode detector and win32gui first, on a
machine that has none of them.

    pixi run analysis      # offline regression, tools/test_analysis.py

The pipeline they form, in order:

    interval_from_span / fit_interval    how long a bullet takes
    analyse                              where the view went, per bullet
    magazine_fault                       whether that magazine is admissible

The interval comes first for a reason that is easy to underrate: analyse()
bins on it, so an interval that is 5% long does not add 5% of noise, it slides
every later bullet into the wrong bin. See fit_interval's docstring.
"""
import numpy as np

# ── Outlier gates, read by magazine_fault ───────────────────────────────────
# A magazine that went wrong does not look noisy, it looks like a different
# gun. Hip fire is the worst case: the posture icon stops rendering, the shot
# is analysed with the scoped K of 1.55 against the hip's 0.50, and a perfectly
# confident +498 counts of residual comes back on a weapon that measured -31 an
# hour earlier. Averaged over five magazines that was survivable; written
# straight into the curve at alpha=1 on the first update, it is not.
#
# Physical gates first, because they are decisive and cheap. Statistics only
# where there is something to be statistical about.
ADS_FRAC_MIN = 0.80       # of the polled frames must show the ADS icon
HAND_COUNTS_MAX = 40      # total hand movement during a burst
OOR_FRAC_MAX = 0.05       # frames the correlator refused
# How far a magazine's round count may sit from the cell's median before it is
# treated as a different measurement rather than a repeat.
ROUNDS_TOL = 2
# Once a few magazines are in, a residual this many times the running spread
# is a broken measurement rather than a large correction. Kept generous: the
# first magazines of an uncalibrated gun are legitimately 35% out.
Z_MAX = 4.0
# Implied recoil = curve + residual, which IS what the gun kicked, so it is a
# physical quantity and a floor on it is a physical claim rather than a
# tolerance. "Not negative" was the only floor and it is far too loose.
#
# It catches the failure it exists for: a magazine fired at the PITCH CLAMP,
# where the view cannot move and the recoil therefore reads near zero. That
# produced `true recoil 32.1 counts over 22 rounds` on a vss — 1.5 per bullet
# — which cleared the not-negative floor, was absorbed by the EMA at --apply,
# and sent the next pass to a residual of -85.7%. docs/recoil/curves/ still
# carries vss_att.0802_BROKEN_negative.bak.json from the same loop at -307.
#
# ⚠ THE FLOOR IS SET FROM THE CORPUS, NOT FROM PHYSICS, and the first attempt
# got that wrong. 5.0 looked safe against the four weapons then to hand — mp5k
# 24.1, vector 24.7, m416 37.0, vss 48.1 per bullet — and `pixi run analysis`
# refused it: LMGs fire far longer magazines and their per-bullet figure is a
# different regime entirely. m249 measures 4.7 and mg3 2.5..2.7 in magazines
# the gates have always accepted.
#
# So 2.0 sits under everything in the corpus and over the clamp reading, and
# the margin is THIN — 2.5 against 1.5. It is a backstop for a cause that is
# already guarded upstream (harvest and sweep confirm the view still tracks
# before magazine 0), not a discriminator to lean on. Widening the basis with
# more LMG data is what would let it tighten.
IMPLIED_PER_BULLET_MIN = 2.0


def interval_from_span(first_shot_ts, last_change_ts, mag_size):
    """Bullet interval from the two endpoints of a magazine. (iv, rounds).

    T = (last round - first round) / (rounds - 1). That is the whole method,
    and it is better than the per-round fit it replaces for one reason: it uses
    only the two events this pipeline detects RELIABLY.

    The magazine's size is read once, standing still, where the digit OCR is
    40/40. The two endpoints come from the ammo region's pixel signature
    changing, which needs no OCR at all. The per-round fit needed the counter
    read on the fly and got 37% of polls -- five usable values in a 42-round
    magazine -- which is not enough to fit anything.

    Validated against the game's published rates on the weapons where the
    hand-typed table disagrees, and the measurement wins every time:

        m762   table 620   measured 697   game 698
        qbz    table 680   measured 649   game 652
        uzi    table 1050  measured 1261  game 1250
        p90    table 900   measured 1013  game 1034
        mg3    table 990   measured 658   game 660  (the LOW rate -- it never
                                                     switched to the high one)

    The failure mode to watch is a missed LAST change, which shortens the span
    and reads as a faster gun. It shows up as a rate that disagrees between
    magazines of the same cell, so the caller should require agreement before
    storing one.
    """
    if not mag_size or mag_size < 2 or first_shot_ts is None:
        return None, 0
    span = float(last_change_ts) - float(first_shot_ts)
    if span <= 0:
        return None, 0
    return span / (mag_size - 1), int(mag_size)


def fit_interval(trace, min_rounds=8):
    """Seconds between rounds, fitted to the HUD counter. (iv, n, resid_ms).

    `trace` is [(timestamp_s, rounds_left), ...] as polled during the burst.

    The counter is the game stating its own fire rate, and it is the only
    honest source for it. detector/weapon.WEAPON_RPM is typed from a wiki and
    disagrees with the game on a third of the roster -- the M762 by 11%, the
    Uzi by 17%, and the AUG by 5%.

    A wrong interval is worse than it sounds because it COMPOUNDS. The firmware
    lays each bullet's compensation on the nominal grid, so an interval 5% long
    puts bullet n's pulse 0.05*n bullets late: harmless at bullet 2, two whole
    rounds out by bullet 40. It also biases the measurement in the same
    direction, since analyse() bins on that same grid -- the AUG's curve grew a
    164-count spike on its last bullet, which is not recoil but four rounds of
    accumulated phase error piling into the final bin.

    Fitted, not differenced: the poll runs every few frames, so each transition
    is located to within a poll interval (~25 ms against an ~85 ms bullet). One
    difference is 30% noise; the slope over 40 rounds is not. Only the FIRST
    sighting of each count is used -- the later sightings are the same
    transition seen again and carry no new information.

    Robust, because the counter is not read perfectly. A single misread puts a
    low count at an early time, and the first version of this split the trace
    into monotone runs and took the longest -- so one bad digit in the middle
    of a magazine cut 42 usable rounds down to a fragment of five, and the fit
    was then rejected for being too short. Two AUG magazines in a row measured
    nothing that way. Fitting everything and throwing out what does not fit the
    line survives a handful of bad digits, which is what actually happens.
    """
    first = {}
    for t, n in trace:
        if n not in first or t < first[n]:
            first[n] = t
    if len(first) < 3:
        return None, len(first), float('nan')
    ts = np.array([first[n] for n in first], dtype=float)
    ns = np.array(list(first), dtype=float)
    keep = np.ones(len(ns), dtype=bool)
    # Three rounds is enough: each pass removes the points a straight line
    # through the survivors cannot explain, and the survivors are the descent.
    for _ in range(3):
        if keep.sum() < 3:
            return None, int(keep.sum()), float('nan')
        slope, intercept = np.polyfit(ns[keep], ts[keep], 1)
        err = ts - (slope * ns + intercept)
        # Scale from the survivors, by MAD -- a few wild misreads must not be
        # allowed to widen the band that is supposed to exclude them.
        mad = float(np.median(np.abs(err[keep] - np.median(err[keep]))))
        tol = max(3.0 * 1.4826 * mad, 0.030)     # never tighter than a poll
        new = np.abs(err - np.median(err[keep])) <= tol
        if (new == keep).all():
            break
        keep = new
    n_kept = int(keep.sum())
    if n_kept < min_rounds:
        return None, n_kept, float('nan')
    slope, intercept = np.polyfit(ns[keep], ts[keep], 1)
    iv = -float(slope)                  # t = a - iv*n
    resid = float(np.std(ts[keep] - (slope * ns[keep] + intercept))) * 1000.0
    if iv <= 0:
        return None, n_kept, resid
    return iv, n_kept, resid


def analyse(res, K, bullet_interval_s, fire_end_ts=None, n_bullets=None,
            first_shot_ts=None):
    dy = np.asarray(res.dy, dtype=float)
    ts = np.asarray(res.ts, dtype=float)
    if len(ts) < 2:
        return None

    # Screen motion is recoil - compensation - hand, all in mouse counts. The
    # compensation is what we set out to grade, so only the hand has to come
    # back out; without the Pico reporting it this is a zero vector and the
    # measurement silently assumes a still hand.
    human = (np.asarray(res.human_dy, dtype=float) if res.human_dy
             else np.zeros_like(dy))

    oor = np.asarray(res.out_of_range, dtype=bool)
    if len(oor) != len(dy):                     # replayed or truncated result
        oor = np.zeros(len(dy), dtype=bool)

    # Where the view actually ended up, over the WHOLE recording rather than
    # the burst — post-fire recovery moves it too, and recentring has to undo
    # the real position, not the analytically interesting part of it.
    drift = float(np.nansum(np.where(oor, np.nan, dy)) / K)

    # Cut at the last round fired, plus the one bullet interval its own recoil
    # needs to play out. Every per-frame array is sliced together — slicing
    # some and not others misaligns the out-of-range mask, and a misaligned
    # mask fails silently.
    if fire_end_ts is not None:
        keep = ts <= fire_end_ts + bullet_interval_s
        if keep.sum() >= 2:
            dy, human, ts, oor = dy[keep], human[keep], ts[keep], oor[keep]

    # Bin 0 starts at the FIRST SHOT, not at the first frame that happened to
    # be captured. Between mouse.click() going out over USB and that round's
    # recoil appearing in a grabbed frame there is input sampling, the shot
    # itself, a render and a present -- 20 to 50 ms, against an 88 ms bullet
    # interval. Starting the bins at the first frame put every bullet's kick
    # 23-57% of a bin early, split across two bullets.
    #
    # The ammo counter changing is the game saying a round has left. That is
    # the one timestamp not downstream of our own latency chain; it still lags
    # by up to one frame, which is a quarter of what it replaces.
    ts = ts - (first_shot_ts if first_shot_ts is not None else ts[0])
    counts = dy / K + human

    # Past half a patch the correlation peak wraps, so a frame flagged
    # out-of-range is not merely imprecise, it is wrong by a whole patch —
    # 83 counts at K=1.55. Dropping the frame costs only the ~1 count of
    # residual it carried; keeping it cost 266 counts on a magazine where the
    # hand moved fast enough to hit the limit three times.
    counts = np.where(oor, np.nan, counts)

    # How many rounds went out is the magazine's business, not the burst
    # duration's. Deriving it from the recording's span comes back one or two
    # short every time — the last round's kick is still playing when the
    # counter hits zero — and a curve rebuilt from that can never catch up to
    # the magazine: it grows by a round or two per pass and still reports rounds
    # firing uncompensated, forever. n_bullets is what the HUD counter said the
    # magazine held before the first round left it.
    span_bins = int(ts[-1] / bullet_interval_s) + 1
    nb = int(n_bullets) if n_bullets else span_bins
    short = max(0, nb - span_bins)

    # Split at the bin edges, do not round to whole frames.
    #
    # The obvious version sums every frame pair whose timestamp falls inside a
    # bullet's window. A frame pair is 10.7 ms at the 93 fps this loop actually
    # runs at, against an 88.2 ms bullet interval -- 12% of a bin -- and during
    # the kick a single pair carries ~11 counts. A boundary landing mid-pair
    # therefore hands a whole pair's motion to the wrong bullet, sign depending
    # on nothing but phase. Measured per-bullet residual noise was rms 4.71
    # counts, which is exactly that size, and it is what forces ALPHA_SHAPE
    # down to 0.35: the shape loop spends its gain budget on this.
    #
    # Displacement is additive, so the honest object is the CUMULATIVE curve
    # sampled at frame times. Interpolating it at the exact bin edges splits a
    # straddling pair in proportion instead of rounding it, and the error drops
    # from "one whole frame" to the second-order term of a straight line
    # through two samples.
    #
    # It also removes the half-frame bias for free: finish() stamps a pair with
    # the LATER frame's time although the motion happened between the two, so
    # every sample used to sit ~5.4 ms late. A cumulative curve is sampled AT
    # frame times and needs no stamp for the motion at all.
    #
    # An out-of-range frame still contributes zero rather than an estimate --
    # its motion is genuinely unknown -- but at 0.13 such frames per magazine
    # that is not what limits anything.
    good = ~np.isnan(counts)
    cum = np.cumsum(np.where(good, counts, 0.0))
    # One breakpoint per good sample, plus a zero at the start of the first
    # interval (its own start time is not in ts; one frame back is the best
    # estimate available and only the first bin can notice).
    dt0 = float(ts[1] - ts[0]) if len(ts) > 1 else 0.0
    tt = np.concatenate(([ts[0] - dt0], ts[good]))
    cc = np.concatenate(([0.0], cum[good]))
    edges = np.arange(nb + 1) * bullet_interval_s
    at_edge = np.interp(edges, tt, cc, left=0.0, right=float(cc[-1]))
    per_bullet = list(np.diff(at_edge))
    return {
        'n_frames': len(dy), 'span_s': float(ts[-1]),
        'cum_px': float(np.nansum(dy)),
        'cum_counts': float(np.nansum(counts)),
        'per_bullet_counts': [round(v, 3) for v in per_bullet],
        'max_abs_frame_px': float(np.nanmax(np.abs(dy))),
        'n_rejected': int(np.sum(res.n_rejected)),
        'n_out_of_range': int(np.sum(res.out_of_range)),
        'n_dropped_oor': int(np.sum(oor)),
        # Bins the magazine says exist that the recording did not reach. Zero
        # when healthy. Non-zero means the tail of per_bullet_counts is padding
        # rather than measurement, and a curve fitted to it would be flat where
        # the recoil is steepest.
        'bullets_missing': short,
        'view_drift_counts': drift,
        'n_low_gate': int(np.sum(res.gates)),
        # Net is what was removed from the residual; abs is how much hand
        # motion happened at all. A small net with a large abs means the hand
        # wandered and came back — the correction still holds, but the run is
        # noisier than a still one.
        'human_counts': float(np.nansum(human)),
        'human_abs_counts': float(np.nansum(np.abs(human))),
        'mean_mad': float(np.mean(res.mad)) if res.mad else float('nan'),
    }


def magazine_fault(a, pattern_counts, mag_size, ads_frac, seen):
    """Why this magazine must not be believed, or None.

    `a` is an analyse() result, `seen` the residuals of the cell's earlier
    magazines. Ordered cheapest-and-most-decisive first. Every one of these has
    actually happened and every one of them produced a confident wrong number
    rather than an obvious failure.
    """
    if ads_frac == ads_frac and ads_frac < ADS_FRAC_MIN:   # NaN-safe
        return (f"only {100*ads_frac:.0f}% of frames were in ADS — hip fire is "
                f"analysed with the scoped K and reads ~3x high")
    n = len(a['per_bullet_counts'])
    if mag_size and abs(n - mag_size) > ROUNDS_TOL:
        return f"{n} rounds against a magazine of {mag_size}"
    if a['human_abs_counts'] > HAND_COUNTS_MAX:
        return (f"the hand moved {a['human_abs_counts']:.0f} counts during the "
                f"burst; that is aim, not recoil")
    nf = max(1, a.get('n_frames', 1))
    if a['n_out_of_range'] / nf > OOR_FRAC_MAX:
        return (f"{a['n_out_of_range']}/{nf} frames out of range — the "
                f"correlator lost the view")
    # The gun cannot have negative recoil, and it cannot have almost none
    # either. Compensation plus residual is what it actually kicked, so this is
    # a physical quantity — see IMPLIED_PER_BULLET_MIN for why the floor is a
    # rate and not just a sign.
    implied = pattern_counts + a['cum_counts']
    if implied <= 0:
        return f"implied recoil {implied:.0f} counts is not positive"
    if n and implied / n < IMPLIED_PER_BULLET_MIN:
        return (f"implied recoil {implied:.0f} counts over {n} rounds is "
                f"{implied / n:.1f} per bullet — no weapon measures under "
                f"{IMPLIED_PER_BULLET_MIN}; the view was probably at the "
                f"pitch clamp")
    # Robust scale, once there is one. MAD rather than sd so that the outlier
    # being tested cannot inflate the threshold that is supposed to catch it.
    if len(seen) >= 3:
        med = float(np.median(seen))
        mad = float(np.median(np.abs(np.asarray(seen) - med))) * 1.4826
        scale = max(mad, 0.02 * pattern_counts)     # floor: 2% of the curve
        z = abs(a['cum_counts'] - med) / scale
        if z > Z_MAX:
            return (f"residual {a['cum_counts']:+.0f} is {z:.1f} sigma from "
                    f"the other magazines ({med:+.0f} +- {scale:.0f})")
    return None
