"""What happens in the FIRST shot? Offline, over every magazine ever stored.

    pixi run python tools/probe_first_shot.py
    pixi run python tools/probe_first_shot.py --weapon m416 --config bare

WHY IT IS A QUESTION AT ALL. `MODEL.md` fits `y_true(t)`, a function of time
since the CLICK, and the firmware plays that same curve -- but not from t=0.
`upload_pattern` FOLDS every knot before the offset into one step delivered at
the click:

    offset = RECOIL_COMP_LAG_MS + lead_ms

So the first shot is the one place where the delivered compensation is not the
curve: it is a single step whose size depends on where that offset falls
between two knots, on a 17 ms grid. Roughly 16 of every 17 one-millisecond lead
nudges change it by exactly zero, and then the seventeenth moves it by a whole
knot.

WHAT THIS PRINTS, and why each column rather than a single number:

    y_obs      what the screen did              measured
    y_comp     what the firmware delivered      known exactly, stored by value
    y_true     y_obs + y_comp                   the gun

per early time slice, plus the same three at the end of the burst for scale.
⚠ THE SLICES ARE ORDERED AND PRINTED SEPARATELY rather than summed. The root
CLAUDE.md's rule -- a criterion must be able to see the dimension it governs --
has bitten three times in this coordinate by reading one aggregate (an endpoint
ratio, a sorted table header, a last point) as if it described a curve.

⚠ IT SAYS NOTHING ABOUT WHETHER THE FIRST SHOT IS *COMPENSATED WELL*. That is
`y_obs` near zero, which is a different column from `y_true` being right, and
both are printed for exactly that reason.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import numpy as np                                            # noqa: E402

import config as cfg                                          # noqa: E402
from calibration import samples                               # noqa: E402

# Where to sample. Dense over the first two rounds, then one late point for
# scale -- the question is about the head of the burst, and a grid that spans
# the whole magazine would put 90% of its columns where nobody is looking.
EARLY_MS = (0, 17, 34, 51, 68, 100, 150, 200)


def _cum_px(m):
    """Cumulative observed view movement, in COUNTS, against its own t.

    ⚠ THE SUM LANDS ON t[k+1], NOT t[k]. `dy_px[k]` is the displacement over
    the interval ENDING at t[k+1] -- calibration/samples.py says so in as many
    words -- so returning it against `t[:-1]` makes every sample one frame
    early, which at 144 fps is 7 ms against a 17 ms grid.
    """
    dy = np.asarray(m.dy_px, dtype=float)
    t = np.asarray(m.t, dtype=float)
    n = min(len(dy), len(t) - 1)
    if n <= 0:
        return None, None
    return t[1:n + 1], np.cumsum(dy[:n]) / float(m.K)


def rows(weapon, want_config=None):
    out = []
    for m in samples.all_magazines(weapon):
        if want_config and samples.config_key(m.config) != want_config:
            continue
        t, obs = _cum_px(m)
        if t is None or not len(t):
            continue
        # y_obs is measured DOWNWARD-positive on screen the same way y_comp is
        # delivered, so y_true = y_obs + y_comp with no sign juggling here.
        comp = samples.comp_counts_at(m.curve, t)
        out.append((m, t, obs, comp))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon')
    ap.add_argument('--config')
    ap.add_argument('--min-mags', type=int, default=4)
    a = ap.parse_args()

    # ⚠ THE STORE IS THE ROSTER HERE, not config's. A gun with no magazines has
    # nothing to say about its first shot, and asking `samples` for one that was
    # never fired is a file that does not exist.
    import glob
    weapons = [a.weapon] if a.weapon else sorted(
        {os.path.basename(p).split('__')[0]
         for p in glob.glob(os.path.join(samples.SAMPLE_DIR, '*.jsonl'))})
    hdr = '  '.join(f'{ms:>7}' for ms in EARLY_MS)
    print(f'{"cell":<34} {"n":>3}  {"":<6}{hdr}   {"end":>8}')
    print(f'{"":<34} {"":>3}  {"":<6}' + '  '.join(f'{"ms":>7}' for _ in EARLY_MS))

    any_row = False
    for w in weapons:
        try:
            mags = samples.all_magazines(w)
        except Exception:
            continue
        if not mags:
            continue
        keys = sorted({samples.config_key(m.config) for m in mags})
        for key in keys:
            if a.config and key != a.config:
                continue
            data = rows(w, key)
            if len(data) < a.min_mags:
                continue
            any_row = True
            grids = {}
            for label in ('y_obs', 'y_comp', 'y_true'):
                vals = []
                for m, t, obs, comp in data:
                    src = {'y_obs': obs, 'y_comp': comp,
                           'y_true': obs + comp}[label]
                    # np.interp clamps outside the sampled span. The first
                    # frame lands 5-8 ms after the click, so t=0 is BEFORE the
                    # data and the clamp would report frame one's value as if
                    # it were the click. Report nan there instead -- "not
                    # observed" and "zero" are different claims.
                    v = np.interp(np.array(EARLY_MS) / 1000.0, t, src)
                    v[np.array(EARLY_MS) / 1000.0 < t[0]] = np.nan
                    vals.append(np.concatenate([v, [src[-1]]]))
                grids[label] = np.nanmean(np.array(vals), axis=0)
            cell = f'{w} {key}'
            for label in ('y_obs', 'y_comp', 'y_true'):
                g = grids[label]
                body = '  '.join(f'{x:7.0f}' if np.isfinite(x) else f'{"--":>7}'
                                 for x in g[:-1])
                print(f'{cell if label == "y_obs" else "":<34} '
                      f'{len(data) if label == "y_obs" else "":>3}  '
                      f'{label:<6}{body}   {g[-1]:8.0f}')
            print()

    if not any_row:
        print('no cell had enough magazines — try --min-mags 1')
        return 1
    print(f'offset at upload = RECOIL_COMP_LAG_MS {cfg.RECOIL_COMP_LAG_MS} ms '
          f'+ lead {cfg.RECOIL_HEAD_LEAD_MS} ms; every knot before it is '
          f'folded into one step at t=0')
    return 0


if __name__ == '__main__':
    sys.exit(main())
