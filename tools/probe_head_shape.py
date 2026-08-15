"""Is the soft head REAL, or is it something the compensation put there?

The shipped curves ask for ~0.2-0.5x of a round's worth of compensation over
the first shot. The operator sees the first bullet land 2-3x further out than
the rest. Those are claims about the same quantity, an order of magnitude apart.

This asks the one question that can be settled offline, on magazines already in
the store, WITHOUT the fitter being able to arrange the answer:

    y_true(t) = y_obs(t) + y_comp(t)

must be the same curve whether the compensation was ON or OFF, and whether the
arm was strong or weak. The fitter never sees which arm a magazine came from,
so it cannot make disagreeing arms agree. m416 and mp5k are the only guns with
both, which is why they are the subjects -- not because they are interesting.

WHAT EACH OUTCOME MEANS

  head agrees across comp on/off
        The gun's CAMERA really does barely move on the first shot. Then the
        fitted curve is honest, no constant in this repo is at fault, and
        whatever throws the first bullet is not in the camera path at all.

  head disagrees (comp-ON reads softer)
        The compensation is corrupting its own measurement at the head -- the
        observation layer cannot see motion that is being cancelled underneath
        it within a frame. Then the curve is fitted on its own output, the head
        can never converge, and the fix is in the measurement, not the constant.

⚠ THE COMPARISON IS ONLY LEGITIMATE WITHIN ONE GUN AND CONFIG. Pooling arms
across weapons would compare recoil, not the observation of it.

⚠ AND IT IS ONLY LEGITIMATE INTERLEAVED. Two arms collected on different nights
differ by everything that changed between the nights, so the time range of each
arm is printed and overlap is required -- this repo has already once read a
30-magazine arm from one day against an 18-magazine arm from the next and
reported a clean separation that was a date.
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calibration.samples as S                                  # noqa: E402


def mags_for(gun):
    out = []
    for p in glob.glob(os.path.join(S.SAMPLE_DIR, f'{gun}__*.jsonl')):
        cfg = os.path.basename(p)[:-6].split('__', 1)[1]
        for line in open(p, encoding='utf-8'):
            if not line.strip():
                continue
            m = json.loads(line)
            m['_config'] = cfg
            out.append(m)
    return out


def y_true(m):
    """(t, y_true counts) for one magazine, t measured from the click.

    dy_px describes an INTERVAL, so its cumulative sum lands on the LATER
    timestamp -- t[1:], not t[:-1]. Returning it against t[:-1] would make every
    sample one frame early, which is the same class of error as mis-binning.
    """
    t = np.asarray(m.get('t') or [], float)
    d = np.asarray(m.get('dy_px') or [], float)
    h = np.asarray(m.get('human_dy') or [], float)
    if d.size < 20 or t.size != d.size + 1:
        return None
    if h.size == d.size:
        # Screen motion is hand + compensation + recoil. Leaving the hand in
        # books any nudge during the burst as recoil.
        d = d - h * S.analysis_k(_Mag(m))
    tt = t[1:]
    obs = np.cumsum(np.nan_to_num(d)) / S.analysis_k(_Mag(m))
    comp = S.comp_counts_at(m.get('curve'), tt) if m.get('comp_enabled') else 0.0
    return tt, obs + comp


class _Mag:
    """analysis_k only reads .sight and .K; the store hands back plain dicts."""

    def __init__(self, d):
        self.sight = d.get('sight')
        self.K = d.get('K')


def profile(mags, edges):
    """Mean y_true at each bin edge, across magazines. NaN where unsampled."""
    rows = []
    for m in mags:
        r = y_true(m)
        if r is None:
            continue
        tt, y = r
        if tt[-1] < edges[-1]:
            continue
        rows.append(np.interp(edges, tt, y))
    if not rows:
        return None, 0
    return np.asarray(rows), len(rows)


def span_of(mags):
    ts = [m.get('ts') for m in mags if m.get('ts')]
    return (min(ts), max(ts)) if ts else ('?', '?')


def run(gun, config, upto_ms, step_ms):
    mags = [m for m in mags_for(gun) if config is None or m['_config'] == config]
    on = [m for m in mags if m.get('comp_enabled')]
    off = [m for m in mags if not m.get('comp_enabled')]
    if not off:
        print(f'{gun}: no compensation-OFF magazines — nothing to compare against')
        return 1

    # Restrict to configs that have BOTH arms, or the comparison is between two
    # different guns wearing different attachments.
    both = {m['_config'] for m in on} & {m['_config'] for m in off}
    on = [m for m in on if m['_config'] in both]
    off = [m for m in off if m['_config'] in both]
    print(f'{gun}: configs with both arms: {sorted(both)}')
    print(f'  comp ON  n={len(on):4d}  {span_of(on)[0]} .. {span_of(on)[1]}')
    print(f'  comp OFF n={len(off):4d}  {span_of(off)[0]} .. {span_of(off)[1]}')
    if not on or not off:
        print('  one arm is empty after matching configs — not comparable')
        return 1
    a, b = span_of(on), span_of(off)
    if a[1] < b[0] or b[1] < a[0]:
        print('  ⚠ THE TWO ARMS DO NOT OVERLAP IN TIME. Any difference below is '
              'confounded with everything that changed between them.')

    edges = np.arange(0, upto_ms + 1, step_ms) / 1000.0
    p_on, n_on = profile(on, edges)
    p_off, n_off = profile(off, edges)
    if p_on is None or p_off is None:
        print('  a magazine set is too short to reach the window')
        return 1

    print(f'\n  y_true (counts) at t, mean across magazines '
          f'[ON n={n_on}, OFF n={n_off}]')
    print('  %6s %10s %10s %10s %9s' %
          ('t ms', 'comp ON', 'comp OFF', 'diff', 'OFF sd'))
    for i, e in enumerate(edges):
        mo, mf = p_on[:, i].mean(), p_off[:, i].mean()
        print('  %6.0f %10.2f %10.2f %10.2f %9.2f' %
              (e * 1000, mo, mf, mo - mf, p_off[:, i].std()))

    # The claim is ordered -- "the head is soft" -- so the criterion is ordered
    # too: how much of the whole-window climb has arrived by the first round,
    # measured on each arm separately. An aggregate over the window cannot see
    # a difference that lives only at its start.
    print()
    for lbl, p in (('comp ON ', p_on), ('comp OFF', p_off)):
        tot = p[:, -1].mean()
        for w in (50, 83, 166):
            i = int(round(w / step_ms))
            if i < p.shape[1]:
                v = p[:, i].mean()
                print('  %s  by %3d ms: %7.2f  = %5.1f%% of the %.0f ms total '
                      '(pro-rata %5.1f%%)' %
                      (lbl, w, v, 100 * v / tot if tot else float('nan'),
                       upto_ms, 100 * w / upto_ms))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gun', default='m416')
    ap.add_argument('--config', default=None)
    ap.add_argument('--upto-ms', type=int, default=500)
    ap.add_argument('--step-ms', type=int, default=25)
    a = ap.parse_args()
    raise SystemExit(run(a.gun, a.config, a.upto_ms, a.step_ms))


if __name__ == '__main__':
    main()
