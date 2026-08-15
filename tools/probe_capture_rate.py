"""How fast can the tracker's grabber actually sample, and what is it missing?

Every recoil number in this repo is a sum of frame-to-frame displacements, and
a sum is blind to anything that happens and UNDOES ITSELF between two samples.
A magazine fired on 2026-08-11 captured 390 frames while the compositor
presented ~1750: we look at 22% of them, 9.5 ms apart.

That matters for exactly one claim. The first shot's measured camera recoil is
a quarter of a later shot's, while the operator reports the first bullet HOLE
sits 2-3x further out. A camera transient that rises and falls inside one
sampling period would produce both readings at once: the bullet leaves during
it, the two frames straddling it show almost nothing, and nothing anywhere
reports a gap.

So the question is not "is the correlator accurate" -- that is measured
(tools/probe_tracker_range.py: gain 0.999 to 120 px). It is "how often do we
look", and whether looking more often is even available.

This fires nothing and needs no Pico. It grabs for a few seconds and reports
the achieved rate, the miss rate, and where the time goes. Run it with the
game rendering something -- an idle menu presents far fewer frames than a live
scene, and the answer depends on that.
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from capture.cropper import DXGISyncGrabber                     # noqa: E402
from detector.view_tracker import ViewTracker                   # noqa: E402


def run(seconds, slice_too):
    tracker = ViewTracker()
    regions = tracker.regions()
    g = DXGISyncGrabber(regions)
    try:
        ts, slice_ms = [], []
        t_end = time.perf_counter() + seconds
        # Warm up: the first grab pays for the duplication handle.
        for _ in range(30):
            g.grab_timed()
        g.n_missed = 0
        g.n_frames = 0
        while time.perf_counter() < t_end:
            t, f = g.grab_timed()
            if f is None:
                continue
            ts.append(t)
            if slice_too:
                t0 = time.perf_counter()
                tracker.slice_frame(f)
                slice_ms.append((time.perf_counter() - t0) * 1000)
        n_missed, n_frames = g.n_missed, g.n_frames
    finally:
        g.close()

    if len(ts) < 10:
        print(f'only {len(ts)} frames in {seconds}s — is anything rendering?')
        return 1

    a = np.asarray(ts)
    d = np.diff(a) * 1000
    span = a[-1] - a[0]
    got = len(a) / span
    composed = (n_frames + n_missed) / span

    print(f'region {g._region}  ({(g._region[2]-g._region[0])*(g._region[3]-g._region[1])/1e6:.2f} Mpx)')
    print(f'  captured        {len(a):6d} frames   {got:7.1f} /s')
    print(f'  compositor made {n_frames + n_missed:6d} frames   {composed:7.1f} /s')
    print(f'  we sampled      {100 * got / composed:6.1f}% of them')
    print()
    print('  interval between our samples, ms:')
    print('    p05 %.2f   p50 %.2f   p95 %.2f   max %.2f' %
          (np.percentile(d, 5), np.median(d), np.percentile(d, 95), d.max()))
    if slice_ms:
        s = np.asarray(slice_ms)
        print(f'  slice_frame: p50 {np.median(s):.3f} ms  p95 '
              f'{np.percentile(s, 95):.3f} ms  (7 patches)')
        print(f'    -> slicing accounts for {100 * np.median(s) / np.median(d):.1f}% '
              f'of the sampling period')

    # The verdict is about what a period this long can HIDE, which is a
    # statement about duration, not about accuracy. A transient shorter than
    # one period contributes only its NET displacement to the sum.
    print()
    print('  a camera movement that rises and returns within %.1f ms leaves'
          % np.median(d))
    print('  no trace in a sum of frame-to-frame displacements. At 723 px/s')
    print('  (aug plateau) that period is worth %.1f px of travel.'
          % (723 * np.median(d) / 1000))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=4.0)
    ap.add_argument('--no-slice', action='store_true',
                    help='grab only, to separate capture cost from slicing')
    a = ap.parse_args()
    raise SystemExit(run(a.seconds, not a.no_slice))


if __name__ == '__main__':
    main()
