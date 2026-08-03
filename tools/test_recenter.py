"""Does recenter() actually bring the view back? Offline, against a fake world.

    pixi run recenter

No game, no hardware. A stored screenshot plus a number saying where the view
is pointing is enough to close the whole loop: grab() slices the tracker band
at an offset, mouse.move() changes that offset, and everything in between is
the real ViewDriver.

This exists because recenter() has now been broken twice by changes that were
individually reasonable and could not be checked without firing a magazine:

  * the ABSOLUTE loop discarded _move_tracked()'s return value, so
    pending_pitch stayed at the offset it had just corrected. Harmless while
    absolute_offset() ignored pending_pitch; the moment it started PRE-SHIFTING
    by it, every pass after the first predicted a view that had already moved,
    the residual came back the size of the correction, and the loop spun until
    it ran out of tries. Live symptom: "view will not come back, 19 to 63
    counts off", four magazines running.

  * before that, a probe that never called set_reference() reported nine
    magazines of "cannot place the view against the cell's reference" — which
    reads like a range problem and is not.

Both are convergence properties of a loop with no randomness in it. Neither
needed the game to find, and neither was found without it.

WHAT THE FAKE WORLD IS NOT: a physics model. There is no clamp, no latency and
no texture failure in it, so a pass here does not mean recentring works in the
game — it means the arithmetic closes. The failures it cannot see are exactly
the ones the live probes are for.
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2
import numpy as np

from config import (RECOIL_PATCH, RECOIL_PATCH_H, RECOIL_PATCH_XS,
                    RECOIL_BAND_Y)
from detector.view_tracker import ViewTracker
from control.aim import ViewDriver, RECENTER_TOL

K = 1.5474
FAILS = []


class FakeWorld:
    """A view that can be pointed somewhere, and a screen that follows it.

    `p` is where the view is, in mouse counts above the reference. Positive is
    up, the direction recoil pushes.

    The sign chain, which is the entire point of the fixture and the thing
    that has to match control/aim.py: a view `p` counts UP means the world has
    slid DOWN the screen by p*K pixels, so the band is sampled p*K pixels
    HIGHER in the image. measure_pair() then reports +p*K, and
    absolute_offset() divides by K and returns +p. A positive mouse dy pulls
    the view down, so it DECREASES p.
    """

    def __init__(self, img):
        self.img = img
        self.p = 0.0
        self.moves = []

    # ── the frame source (grab/flush) ──

    def grab(self):
        y = int(round(RECOIL_BAND_Y - self.p * K))
        y = max(0, min(self.img.shape[0] - RECOIL_PATCH_H, y))
        return {f'recoil_{i}': self.img[y:y + RECOIL_PATCH_H,
                                        x:x + RECOIL_PATCH]
                for i, x in enumerate(RECOIL_PATCH_XS)}

    def flush(self, n=0):
        pass

    # ── the device (move) ──

    def move(self, dx, dy):
        self.moves.append(dy)
        self.p -= dy

    def click(self, *a, **kw):
        pass

    def key(self, *a, **kw):
        pass


def a_frame():
    for p in sorted(glob.glob(os.path.join(ROOT, 'docs', 'ads', 'runs',
                                           '**', '*.jpg'), recursive=True)):
        img = cv2.imread(p)
        if img is not None and img.shape[0] >= 1200 and img.shape[1] >= 2700:
            return os.path.relpath(p, ROOT), img
    return None, None


def check(name, ok, detail):
    print(f'  {"ok " if ok else "FAIL"}  {name:<44} {detail}')
    if not ok:
        FAILS.append(f'{name}: {detail}')


def run_case(img, drift, label):
    """Put the view `drift` counts off, recentre, report where it ended."""
    tr = ViewTracker()
    w = FakeWorld(img)
    vd = ViewDriver(tr, mouse=w, frames=w, K=K, sight='red_dot')
    vd.set_reference()
    # A magazine happened: the view is off and the integral knows roughly how
    # far. `+3` is deliberate — the integral is never exactly right, and a
    # loop that only converges from a perfect starting belief is not closing
    # anything.
    w.p = float(drift)
    vd.pending_pitch = float(drift) + 3.0
    vd.recenter()
    left = w.p
    check(label, abs(left) <= RECENTER_TOL,
          f'started {drift:+.0f}, ended {left:+.1f} counts '
          f'(tol {RECENTER_TOL}), {len(w.moves)} moves')
    return left


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    path, img = a_frame()
    if img is None:
        print('[!] no stored frame big enough')
        return 1
    print(f'world: {path}  {img.shape[1]}x{img.shape[0]}\n')

    print('recenter() converges from a drift the integral roughly knows')
    for drift in (20, 60, -60, 120, -120):
        run_case(img, drift, f'drift {drift:+4d} counts')

    print('\nthe reference is required, and its absence is NAMED')
    tr = ViewTracker()
    w = FakeWorld(img)
    vd = ViewDriver(tr, mouse=w, frames=w, K=K, sight='red_dot')
    # No set_reference() on purpose.
    off = vd.absolute_offset()
    check('absolute_offset without a reference', off is None, repr(off))
    check('and it says which of the four reasons',
          bool(vd.place_fail) and 'set_reference' in vd.place_fail,
          repr(vd.place_fail))

    print('\nevery refusal names its own reason')
    vd.set_reference()
    w.p = 400.0        # far past what a 256 px patch can hold
    off = vd.absolute_offset(predicted=400.0)
    check('a roll past the patch is refused', off is None, repr(off))
    check('  and says so', bool(vd.place_fail), repr(vd.place_fail))

    # THE LIMIT, stated rather than asserted away.
    #
    # 400 counts out with a prediction of zero, the correlation WRAPS and
    # comes back around -37 counts — inside ABS_TRUST_FRAC, so absolute_offset
    # believes it. That is not a hole to plug here: a 256 px patch cannot see
    # a view 619 px away, so there is no reading it could take that would be
    # right, and refusing on magnitude cannot distinguish a wrapped -37 from
    # an honest one.
    #
    # This case is what ABS_AGREE_COUNTS and tracking_confirmed() are for: the
    # integral and the reference are independent, and the integral does not
    # wrap. absolute_offset's contract is only "right when the prediction is
    # roughly right", and that is what the block above tests.
    off = vd.absolute_offset(predicted=0.0)
    print(f'  note  a 400-count error with a zero prediction reads '
          f'{"None" if off is None else f"{off:+.0f}"} counts — WRAPPED, and '
          f'not\n        detectable from the reading. Caught downstream by '
          f'ABS_AGREE_COUNTS.')

    # What IS promised: a prediction inside the roll budget gives the truth.
    for p_true in (40.0, 80.0):
        w.p = p_true
        got = vd.absolute_offset(predicted=p_true - 10.0)
        check(f'{p_true:.0f} counts out, prediction off by 10',
              got is not None and abs(got - p_true) < 4,
              f'{"None" if got is None else f"{got:+.1f}"} '
              f'(want {p_true:+.0f})')

    if FAILS:
        print(f'\n{len(FAILS)} failed:')
        for f in FAILS:
            print(f'  {f}')
        return 1
    print('\nall ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
