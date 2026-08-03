"""Rebuild a weapon's recoil curve from measured residuals, bullet by bullet.

    python calibration/fit_curve.py --jsonl docs/recoil/runs/sweep_....jsonl
    python calibration/fit_curve.py --jsonl ... --apply

The scalar knobs (weapon scale, posture factor) can only stretch a curve
uniformly. They are fitted to ONE number -- the residual summed over the whole
magazine -- so any error, wherever it comes from, gets pushed into whichever
knob you happen to be turning. Measured on the AUG that went badly wrong: the
preset curve's tail collapses to near zero over the last five rounds while the
gun is still kicking at full strength, and correcting the total made the scalar
over-compensate the first 34 rounds to pay for it. The endpoint improved and
the actual spread of impact points got worse.

What the observer gives us is per-bullet, so the fix should be too:

    true_recoil[b] = compensation_applied[b] + residual_measured[b]

Both terms are known per bullet -- the first from the curve that was loaded
during the run, the second straight out of the sweep. Their sum is the gun's
own recoil, and writing THAT back fixes shape and total together.

Two things this deliberately does not do:

  * It stops at the last bullet that actually fired. PUBG pulls the camera
    back toward the pre-fire aim once you stop shooting, and that recovery
    lands in the frames after the magazine ends -- it flatters the endpoint
    while doing nothing for the rounds already downrange. Bullets past the end
    of fire are dropped rather than modelled.
  * It does not guess at rounds it never saw -- but it does keep the ones it
    did see. A magazine longer than the curve fires rounds with no
    compensation at all, and their residual is their entire recoil, measured.
    Those extend the curve. Rounds past the end of FIRE are still dropped:
    inventing compensation that plays after the magazine empties would pull
    the view down while the player is reloading.
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from detector.weapon import Weapon, CURVE_DIR

HERE = os.path.dirname(os.path.abspath(__file__))

# A bullet counts as "fired" while the measured recoil is at least this
# fraction of the magazine's plateau. The tail-off at the end of a magazine is
# abrupt (30 counts -> 4), so the exact threshold does not matter much.
FIRE_FLOOR_FRAC = 0.40
PLATEAU_FROM = 6          # first bullets ramp up; median the plateau only
SMOOTH_W = 5              # bullets; see smooth() for why this is not cosmetic

# The update is an exponential moving average towards the measured truth:
#
#     curve <- curve + alpha * (measured_truth - curve)
#            = curve + alpha * residual
#
# because the residual IS truth minus curve — that is what measuring it means.
# So alpha is simply the fraction of the residual applied, and running the
# thing repeatedly is an EMA over every measurement ever taken, weighting
# recent ones by (1-alpha)^k. No batch, no history to keep: each pass sees a
# FRESH residual, measured against the curve the last pass wrote.
#
# Two alphas, because the two quantities are measured to wildly different
# precision.
#
#   MAGNITUDE  how much recoil there is in total. Measured to about 1% at 5
#              magazines (sem ~10 counts on 1080), so it takes gain 1 and
#              converges in one pass: +31.1 measured, +31.1 applied, +2.7 left.
#
#   SHAPE      how that total is distributed over the magazine. This is where
#              the game's own per-burst randomness lands: magazine-to-magazine
#              deviation is CORRELATED within a burst, not per-shot white, so
#              it does not average down the way noise should. Pooled over 20
#              homed magazines the cumulative spread grows to +-27 counts by
#              bullet 29 -- 2.7x what a random walk would give -- and then
#              plateaus. At 5 magazines that leaves sem ~12 on a shape error
#              of ~19: signal-to-noise about 1.
#
# Correcting a quantity you cannot measure, at gain 1, injects noise. Measured
# directly: a pass that applied the full measured shape flipped the
# mid-magazine profile from +19 to -22 and took wander from 19.2 to 31.7. That
# is the loop oscillating, and it will not settle -- each pass writes in the
# previous pass's sampling error with the sign reversed.
#
# So alpha is a precision-versus-speed knob, and the right setting is not a
# constant at all.
#
# The gun's real recoil is STATIC — it does not move until a game patch moves
# it — so the best estimator is not a fixed-alpha EMA at all, it is the running
# mean: alpha_k = 1/(k+1) over the magazines seen so far. Simulated against the
# measured per-magazine scatter of 27 counts, starting 100 counts out, 4000
# runs each; rms error left after k magazines:
#
#     alpha      k=1    k=3    k=5   k=20
#     1.0       26.8   26.8   27.4   26.0   <- never gets below the noise
#     0.5       51.7   19.8   15.8   15.1
#     0.3       70.4   36.0   20.0   11.1
#     0.1       90.0   73.1   59.2   13.6   <- still converging
#     1/(k+1)   26.8   15.5   11.9    5.9   <- wins at every horizon
#
# A fixed alpha has a floor: sigma*sqrt(a/(2-a)) of pure noise written into the
# curve forever — 27 counts at alpha=1, 11 at 0.3. The running mean has no
# floor, it decays as 1/sqrt(k).
#
# alpha=1.0 was right while this ran once per CELL, because five magazines had
# already been averaged before it saw them. Per MAGAZINE it is the worst choice
# on the table.
#
# The floor is the only reason not to let alpha reach zero: a patch does move
# the target, and an estimator that has averaged 200 magazines would need
# another 200 to notice. Flooring turns the tail into a fixed-alpha EMA with a
# ~1/floor magazine memory — that is the tracking-versus-precision knob.
#
# Shape gets the lower floor because its signal-to-noise is worse: the game
# re-rolls how a magazine's recoil is distributed on every burst, so per-bullet
# structure averages down far more slowly than the total does.
ALPHA_MAG_FLOOR = 0.10      # ~10 magazine memory once converged
ALPHA_SHAPE_FLOOR = 0.05

# A running mean starts at alpha=1 because the first observation is all it
# knows. That is only true from nothing, and this never starts from nothing:
# the curve on disk already embodies every magazine ever fitted into it. Told
# otherwise, the first update throws all of that away and replaces it with one
# magazine of noise — measured directly, six per-magazine updates that way made
# the residual swing -0.6, -13.8, +59.4, -34.7, -73.3, +74.3, a spread of 51
# counts where the same gun measured 12.5 with the loop switched off.
#
# So a fitted curve with no counter is credited with PRIOR_MAGS magazines of
# history, and alpha is capped below 1 regardless. One magazine may refine the
# curve; it may not rewrite it.
PRIOR_MAGS = 5
ALPHA_MAX = 0.5


def smooth(y, w=SMOOTH_W):
    """Symmetric moving average, window shrinking to 1 at both ends.

    Not cosmetic. Per-bullet measurement noise written into a curve stops
    being noise -- it repeats identically every magazine, while the real
    thing re-rolls, so it adds a fixed error that random-walks along the
    magazine. Measured on the AUG at 5 magazines: SEM 2.0 counts per bullet,
    which walks to 12.3 counts over 38 rounds against the 41 counts of real
    wander being corrected. Averaging 5 bullets cuts that to 5.5.

    A symmetric moving average reproduces a linear trend exactly, so the
    ramp over the opening rounds survives; shrinking the window at the edges
    leaves the first and last bullets untouched, which matters because the
    opening bullet is genuinely different from its neighbours.
    """
    y = np.asarray(y, dtype=float)
    n = len(y)
    out = np.empty(n)
    half = w // 2
    for i in range(n):
        h = min(half, i, n - 1 - i)
        out[i] = y[i - h:i + h + 1].mean() if h else y[i]
    # Smoothing must not move the total: it is measured far better than any
    # single bullet, and the total is what the scalar knobs were fighting over.
    s = out.sum()
    return out * (y.sum() / s) if s else out


def load_combo(path, weapon, posture, config=None):
    """One measured cell. sweep.py writes 'combo', harvest.py writes 'cell'
    with an attachment config attached; both carry the same fields."""
    hits = []
    for line in open(path, encoding='utf-8'):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get('type') not in ('combo', 'cell'):
            continue
        if r['weapon'] != weapon or r['posture'] != posture:
            continue
        if config is not None and r.get('config') != config:
            continue
        hits.append(r)
    if not hits:
        return None
    if len(hits) > 1:
        cfgs = sorted({h.get('config', '-') for h in hits})
        if len(cfgs) > 1:
            print(f"  [!] {len(hits)} cells match ({', '.join(cfgs)}); using "
                  f"the last. Pass --config to choose.")
    return hits[-1]


def rebuild(rec, verbose=True):
    """Return (shots, report) for one measured combo, or (None, why)."""
    weapon, posture = rec['weapon'], rec['posture']
    att = rec['attachments'] or {}

    w = Weapon()
    w.set('name', weapon)
    w.set('posture', posture)
    w.set('muzzle', att.get('muzzle', ''))
    w.set('grip', att.get('grip', ''))
    w.set_seq()
    if not len(w.t_s):
        return None, f"no curve for {weapon}"

    # The reconstruction is only valid against the curve that was actually
    # loaded during the run. Anything else silently mixes two curves.
    total = float(np.sum(w.dy_s))
    if abs(total - rec['pattern_counts']) > 0.5:
        return None, (f"curve changed since the run: now {total:.1f} counts, "
                      f"the run used {rec['pattern_counts']:.1f}. Re-measure.")

    bi = w.bullet_interval_s
    # Bullet indices, not floored times — see Weapon.comp_bins. Flooring put
    # every entry one bullet early, so the correction below landed on the
    # wrong shot and the curve crept forward on every pass.
    nb = w.curve_bullets()
    comp_dy, comp_dx = w.comp_bins(nb)

    # Zero-pad rather than truncate to the curve's length. A magazine longer
    # than the curve fires rounds that got no compensation at all, and their
    # residual IS their whole recoil -- measured, not guessed, which is what
    # the original refusal to extrapolate was guarding against. Truncating
    # here is how a curve stays permanently two rounds short of the magazine.
    # Magazines do not all fire the same number of rounds -- an extended
    # magazine comes back 40, 41, 41 -- so this cannot be a rectangular mean.
    # Averaging each bullet over the magazines that actually reached it uses
    # every round measured without inventing any; the alternative, truncating
    # to the shortest, throws away exactly the tail rounds that matter most.
    # measure_cell has already dropped magazines more than ROUNDS_TOL off the
    # median, so what is left differs by a round or two at the end.
    per_mag = [m['per_bullet_counts'] for m in rec['mags']]
    # The magazine's own count decides the length, not the longest recording.
    # Sizing off the recordings comes back a round or two short every time --
    # the last shot's kick is still playing when the counter hits zero -- and a
    # curve fitted to that can never reach the end of the magazine: it grows a
    # little each pass and still reports rounds firing uncompensated. Measured
    # on the AUG: the counter says 42, three passes produced curves of 38, 40
    # and 41.
    n = rec.get('magazine_size') or max(len(p) for p in per_mag)
    resid = np.array([np.mean([p[b] for p in per_mag if b < len(p)])
                      if any(b < len(p) for p in per_mag) else 0.0
                      for b in range(n)])
    depth = np.array([sum(1 for p in per_mag if b < len(p)) for b in range(n)])
    thin = int((depth < len(per_mag)).sum())
    if thin:
        print(f"  last {thin} bullet(s) averaged over fewer magazines than the "
              f"rest (magazine lengths {[len(p) for p in per_mag]}, "
              f"magazine holds {rec.get('magazine_size')})")
    blind = int((depth == 0).sum())
    if blind:
        print(f"  [!] {blind} bullet(s) of the magazine were never recorded at "
              f"all — the curve would be FLAT where the recoil is steepest. "
              f"Fix the recording before trusting this fit.")
    comp_dy = np.pad(comp_dy, (0, max(0, n - nb)))[:n]
    comp_dx = np.pad(comp_dx, (0, max(0, n - nb)))[:n]
    true_dy = comp_dy + resid[:n]

    plateau = float(np.median(true_dy[PLATEAU_FROM:])) if n > PLATEAU_FROM \
        else float(np.max(true_dy))
    fired = np.nonzero(true_dy >= plateau * FIRE_FLOOR_FRAC)[0]
    if not len(fired):
        return None, "no bullet cleared the fire threshold"
    last = int(fired[-1])
    # One-way: a curve may grow, never shrink. FIRE_FLOOR_FRAC decides where
    # the magazine stopped by looking for the last bullet still kicking at 40%
    # of the plateau, which is sound on a clean measurement and catastrophic on
    # a noisy one -- a tail that dips under the threshold gets amputated, the
    # rounds past the cut then fire with no compensation at all, and the next
    # magazine measures a huge residual that chops it again. Observed live: 41
    # bullets to 30 in three magazines, residual +588, and the gun effectively
    # uncompensated for its last quarter.
    #
    # Extending on evidence is fine. Retreating on noise is not: the rounds
    # already in the curve were put there by measurements too.
    last = max(last, nb - 1)
    # Split the correction into magnitude and shape and apply them at their
    # own gains -- see ALPHA_SHAPE. Smoothing acts on the RESIDUAL only: the
    # old code smoothed comp + resid, which adds smooth(comp) - comp on every
    # pass, a low-pass acting on the whole accumulated curve regardless of
    # what was measured. That term walks +-8.9 counts across an AUG magazine
    # and quietly damped the shape loop; the damping was doing real work, but
    # by blurring the curve rather than by declining to trust a noisy
    # measurement, which is not a knob anyone can reason about.
    #
    # The magnitude goes on as a rescale, not as a constant per bullet:
    # recoil scales multiplicatively with attachments, posture and sight, so
    # 3% more recoil is 3% more on every bullet, not 0.8 counts on each.
    resid_s = smooth(resid[:n])
    comp_sum = float(comp_dy.sum())
    if comp_sum <= 0:
        return None, "source curve sums to zero"
    seen = curve_updates(weapon)
    a_mag = min(ALPHA_MAX, max(ALPHA_MAG_FLOOR, 1.0 / (seen + 1)))
    a_shape = min(ALPHA_MAX, max(ALPHA_SHAPE_FLOOR, 1.0 / (seen + 1)))
    scale = 1.0 + a_mag * float(resid_s.sum()) / comp_sum
    shape = resid_s - comp_dy * (float(resid_s.sum()) / comp_sum)
    true_dy = (comp_dy * scale + a_shape * shape)[:last + 1]
    comp_dx = comp_dx[:last + 1]
    grew = max(0, (last + 1) - nb)

    # counts = shot_dy * COUNTS_PER_RECOIL_UNIT * factor. Rather than rebuild
    # that factor chain, recover the product from the curve we just loaded --
    # self-consistent by construction, and immune to how the chain is spelled.
    shots_dy = np.array([s['dy'] for s in _shots_of(w, weapon)])
    nz = shots_dy != 0
    if not nz.any():
        return None, "source curve is all zeros"
    k_unit = float(np.median(np.array(w.dy_s)[nz] / shots_dy[nz]))
    if not np.isfinite(k_unit) or k_unit == 0:
        return None, "could not recover the unit scale"

    shots = [{'delay_ms': int(round(bi * 1000)),
              'dx': round(float(comp_dx[i] / k_unit), 3),
              'dy': round(float(true_dy[i] / k_unit), 3)}
             for i in range(len(true_dy))]

    cum_before = np.cumsum(resid[:last + 1])
    report = {
        'weapon': weapon, 'posture': posture, 'n_mags': len(rec['mags']),
        'bullets_kept': len(shots), 'bullets_measured': int(n),
        'bullet_interval_ms': int(round(bi * 1000)),
        'curve_total_before': float(comp_dy[:last + 1].sum()),
        'curve_total_after': float(true_dy.sum()),
        'max_wander_before': float(np.abs(cum_before).max()),
        'plateau_counts': plateau,
        'bullets_added': grew,
        'k_unit': k_unit,
        'ema_updates': seen + 1,
        'alpha_mag': round(a_mag, 4),
        'alpha_shape': round(a_shape, 4),
    }
    if verbose:
        print(f"  {posture:<10} {len(rec['mags'])} mags, kept {len(shots)}/"
              f"{n} bullets @ {report['bullet_interval_ms']} ms")
        print(f"    curve total {report['curve_total_before']:.1f} -> "
              f"{report['curve_total_after']:.1f} counts "
              f"({100*(report['curve_total_after']/report['curve_total_before']-1):+.1f}%)")
        print(f"    impact-point wander during the magazine was "
              f"{report['max_wander_before']:.1f} counts")
        print(f"    EMA update #{report['ema_updates']}: alpha "
              f"{a_mag:.3f} magnitude / {a_shape:.3f} shape")
        if grew:
            print(f"    curve grew by {grew} bullet(s) — the magazine outlasts "
                  f"it, and those rounds were firing uncompensated")
        dropped = n - len(shots)
        if dropped:
            print(f"    dropped {dropped} post-fire bin(s) — camera recovery, "
                  f"not recoil")
    return shots, report


def ema_update(rec, src_note, verbose=True):
    """One EMA step: measure -> correct -> write. Returns the report or None.

    The whole loop in one call, so a harvest can close it per cell instead of
    dumping JSONL for a human to feed back by hand. The next magazine of the
    next pass then measures a FRESH residual against what this wrote, which is
    what makes repeated passes an EMA rather than a re-fit of stale data.

    Callers holding a live Weapon must reload it afterwards
    (Weapon.bullet_calculator.reload()) or they keep firing the old pattern.
    """
    shots, report = rebuild(rec, verbose=verbose)
    if shots is None:
        if verbose:
            print(f"    [!] no update: {report}")
        return None
    write_curve(rec['weapon'], shots, report, src_note)
    return report


def curve_updates(weapon):
    """How many EMA updates this curve has already absorbed.

    Persisted in the curve file so the schedule survives a restart. A run that
    forgot would go back to alpha 1 and throw away every magazine averaged so
    far. Reset it by deleting the field, which is what a game patch calls for:
    a patch is the one event that makes the old magazines wrong rather than
    merely old.
    """
    path = os.path.join(CURVE_DIR, f'{weapon}_att.json')
    try:
        m = json.load(open(path, encoding='utf-8')).get('measured') or {}
    except Exception:
        return 0
    n = int(m.get('ema_updates') or 0)
    if n:
        return n
    # No counter, but a `measured` block means this curve was fitted from real
    # magazines — it is a prior, not a blank slate. Credit it rather than
    # letting the next magazine overwrite it wholesale.
    return PRIOR_MAGS if m else 0


def _shots_of(w, weapon):
    """The raw shot list the loaded curve came from."""
    key = f'{weapon}_att'
    data = w.bullet_calculator.recoil_data.get(key) or \
        w.bullet_calculator.recoil_data.get(weapon) or {}
    return data.get('standing') or next(iter(data.values()), [])


def write_curve(weapon, shots, report, src_note):
    """Overwrite the _att standing curve, keeping a timestamped backup.

    docs/recoil/curves/ is not in git, so the backup is the only way back.
    """
    path = os.path.join(CURVE_DIR, f'{weapon}_att.json')
    if os.path.exists(path):
        stamp = datetime.now().strftime('%m%d_%H%M%S')
        backup = os.path.join(CURVE_DIR, f'{weapon}_att.{stamp}.bak.json')
        shutil.copy2(path, backup)
        print(f"  backed up -> {os.path.basename(backup)}")
    out = {
        'weapon': f'{weapon}_att',
        'stance': 'standing',
        'source': src_note,
        'measured': report,
        'shots': shots,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"  wrote {os.path.basename(path)}  ({len(shots)} shots)")


def main():
    ap = argparse.ArgumentParser(
        description='Rebuild a recoil curve from measured per-bullet residual.')
    ap.add_argument('--jsonl', required=True, help='sweep output')
    ap.add_argument('--weapon', default='aug')
    ap.add_argument('--posture', default='standing',
                    help="which cell to fit; the curve is the standing one, so "
                         "fitting a crouching cell folds that posture's factor "
                         "into it -- almost never what you want")
    ap.add_argument('--config', default=None,
                    help="harvest.py runs only: which attachment cell to fit "
                         "('bare', 'both', ...). The curve is normalised by "
                         "the attachment factor either way, so every config "
                         "should rebuild the SAME base curve -- fitting two "
                         "and comparing is a free check on the model.")
    ap.add_argument('--apply', action='store_true',
                    help='write the curve (default: dry run)')
    args = ap.parse_args()

    rec = load_combo(args.jsonl, args.weapon, args.posture,
                     args.config)
    if rec is None:
        print(f"[!] no {args.weapon}/{args.posture} cell in {args.jsonl}")
        return 1

    print(f"fitting {args.weapon} from {os.path.basename(args.jsonl)}")
    shots, report = rebuild(rec)
    if shots is None:
        print(f"[!] {report}")
        return 1

    if args.posture != 'standing':
        print(f"  [!] {args.posture} cell: the written curve is the standing "
              f"one, so this bakes in the posture factor. Use --posture "
              f"standing unless you know why you want this.")

    if not args.apply:
        print("\n  dry run — nothing written. Re-run with --apply.")
        print("  then re-measure: the residual should drop to ~0 AND the "
              "wander should shrink; the wander is the one that matters.")
        return 0

    write_curve(args.weapon, shots, report,
                f"measured {report['n_mags']} mags, "
                f"{os.path.basename(args.jsonl)} "
                f"({datetime.now().strftime('%Y-%m-%d')})")
    print("\n  re-measure to confirm. Nothing else was touched — the weapon "
          "scale and posture factors are unchanged, and they should stay that "
          "way now that the curve carries the shape.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
