"""Rebuild a weapon's recoil curve from measured residuals, bullet by bullet.

    python calibration/fit_curve.py --jsonl calibration/sweep_....jsonl
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
    nb = int(w.t_s[-1] / bi) + 1
    comp_dy = np.zeros(nb)
    comp_dx = np.zeros(nb)
    for dx, dy, t in zip(w.dx_s, w.dy_s, w.t_s):
        b = min(nb - 1, int(t / bi))
        comp_dy[b] += dy
        comp_dx[b] += dx

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
    true_dy = smooth(true_dy[:last + 1])
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
    }
    if verbose:
        print(f"  {posture:<10} {len(rec['mags'])} mags, kept {len(shots)}/"
              f"{n} bullets @ {report['bullet_interval_ms']} ms")
        print(f"    curve total {report['curve_total_before']:.1f} -> "
              f"{report['curve_total_after']:.1f} counts "
              f"({100*(report['curve_total_after']/report['curve_total_before']-1):+.1f}%)")
        print(f"    impact-point wander during the magazine was "
              f"{report['max_wander_before']:.1f} counts")
        if grew:
            print(f"    curve grew by {grew} bullet(s) — the magazine outlasts "
                  f"it, and those rounds were firing uncompensated")
        dropped = n - len(shots)
        if dropped:
            print(f"    dropped {dropped} post-fire bin(s) — camera recovery, "
                  f"not recoil")
    return shots, report


def _shots_of(w, weapon):
    """The raw shot list the loaded curve came from."""
    key = f'{weapon}_att'
    data = w.bullet_calculator.recoil_data.get(key) or \
        w.bullet_calculator.recoil_data.get(weapon) or {}
    return data.get('standing') or next(iter(data.values()), [])


def write_curve(weapon, shots, report, src_note):
    """Overwrite the _att standing curve, keeping a timestamped backup.

    weapon_curve_kava4/ is not in git, so the backup is the only way back.
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
