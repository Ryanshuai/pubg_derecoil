"""Measured fire rates, and the rule for when a measurement replaces a guess.

detector/weapon.WEAPON_RPM is typed from a wiki. Checked against what the HUD
ammo counter actually did, it is wrong on a third of the roster:

    m762   620 -> 697      qbz   680 -> 649      uzi  1050 -> 1261
    p90    900 -> 1013     scar  600 -> 646      aug   680 -> 718

and where the two disagree the measurement is the one that matches the game's
published rate, so it is the table that is wrong, not the method.

This matters more than a few percent sounds. The firmware lays each round's
compensation on the nominal grid, so an interval that is x% long puts bullet
n's pulse 0.01*x*n bullets late -- nothing at bullet 2, two whole rounds out by
bullet 40 at 5%, four rounds at the M762's 11%. And the error is not confined
to playing the curve: analyse() bins the measurement on the same grid, so the
phase error piles into the last bin and is then FITTED, which is where the
AUG's 164-count final bullet came from against a 93-count plateau.

A rate is only accepted from a magazine that fired most of itself with a tight
fit, because the two ways to get a wrong slope -- a short burst and a missed
transition -- both bias it the same way and both look like a clean number.
"""
import json
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PATH = os.path.join(ROOT, 'docs', 'recoil', 'weapon_rpm.json')

# A fitted interval is believed only if the fit itself is tight. The poll runs
# every few frames, so a transition is located to within ~25 ms; over 40 rounds
# the residual of a straight line through them should be a fraction of that.
# Anything looser means the counter was misread somewhere in the magazine.
RESID_MS_MAX = 12.0
# Below this many rounds a burst is too short to separate the slope from the
# noise in where the first and last transitions were seen.
MIN_ROUNDS = 12
# How far from the stored value a new measurement must land before it is worth
# rewriting. Under this the two are the same number seen twice.
REWRITE_FRAC = 0.005


def load(path=PATH):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def get(weapon, path=PATH):
    """Measured RPM for one weapon, or None."""
    rec = load(path).get(weapon)
    if isinstance(rec, dict):
        return float(rec['rpm'])
    return float(rec) if rec else None


def acceptable(n_rounds, resid_ms):
    """Is this fit good enough to overwrite a rate with?"""
    if n_rounds < MIN_ROUNDS:
        return False, f'only {n_rounds} rounds in the fit'
    if not (resid_ms == resid_ms) or resid_ms > RESID_MS_MAX:
        return False, f'fit residual {resid_ms:.1f} ms is too loose'
    return True, ''


def record(weapon, interval_s, n_rounds, resid_ms, note='', path=PATH):
    """Store a measured rate. Returns (rpm, wrote, why_not)."""
    ok, why = acceptable(n_rounds, resid_ms)
    rpm = 60.0 / interval_s
    if not ok:
        return rpm, False, why
    data = load(path)
    prev = data.get(weapon)
    prev_rpm = (float(prev['rpm']) if isinstance(prev, dict)
                else float(prev) if prev else None)
    if prev_rpm and abs(rpm - prev_rpm) / prev_rpm < REWRITE_FRAC:
        return rpm, False, f'within {REWRITE_FRAC:.1%} of the stored {prev_rpm:.0f}'
    data.setdefault('_source', 'calibration/sweep.fit_interval — slope of the '
                              'HUD ammo counter over one magazine')
    data.setdefault('_why', 'detector/weapon.WEAPON_RPM is a wiki table and is '
                            'wrong on a third of the roster. A wrong interval '
                            'compounds: bullet n is compensated 0.01*err*n '
                            'bullets late.')
    data[weapon] = {'rpm': round(rpm, 1),
                    'interval_ms': round(1000.0 * interval_s, 2),
                    'rounds': int(n_rounds),
                    'fit_resid_ms': round(resid_ms, 2),
                    'ts': datetime.now().isoformat(timespec='seconds'),
                    'note': note}
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)
    return rpm, True, ''
