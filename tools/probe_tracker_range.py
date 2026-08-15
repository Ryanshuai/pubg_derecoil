"""Measure ViewTracker's transfer function: what it REPORTS vs what actually moved.

Every recoil number in this repo comes out of `ViewTracker.measure_pair`, and
nothing has ever measured that function itself. The curves are fitted to what it
says, so if it under-reads, the curve under-compensates, and no constant
anywhere -- lead, offset, K -- can put back counts that were never asked for.

The reason to suspect it is arithmetic, not a hunch. The operator reports the
first bullet hole sits 2-3x further out than the rest; the fitted curve gives
the first shot 0.22x an average shot's compensation. Those two claims are about
the same quantity and they disagree by more than ten. One of them is measured
through this function.

METHOD -- a rigid translation with a known answer, built from a real frame:

    A = full[y0        : y0 + H]      what the window sees before
    B = full[y0 - N    : y0 - N + H]  what it sees after content slid DOWN N px

Recoil rotates the view up, which slides content down the screen, so B is the
window looking at content that used to sit N px higher. measure_pair(A, B) must
report dy = +N. This is the real thing, not an approximation: a pitch rotation
on the screen's vertical centre line IS a pure translation (that is why the
patches live there), and the pixels are real game pixels with real texture.

Not np.roll: rolling wraps the bottom N rows around to the top, which fabricates
a second, opposite correlation peak. The failure being tested here is precisely
about wraparound, so a synthetic wrap would answer the question by assuming it.

WHAT IT CANNOT SAY: this measures the correlator on a static scene. It does not
measure motion blur, rolling shutter, or a frame that arrived mid-kick. If the
transfer function is clean here, the head under-read is still unexplained -- it
just is not THIS.
"""
import argparse
import glob
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import SCREEN_H, SCREEN_W                           # noqa: E402
from detector.view_tracker import ViewTracker                   # noqa: E402


def frames(pattern, limit):
    """Full-screen frames, largest-first so a bad thumbnail cannot sneak in."""
    paths = sorted(glob.glob(pattern))
    out = []
    for p in paths:
        img = cv2.imread(p, cv2.IMREAD_COLOR)
        if img is None:
            continue
        if img.shape[0] != SCREEN_H or img.shape[1] != SCREEN_W:
            # A resized frame would change the px scale silently, which is the
            # one thing this probe must not let happen.
            continue
        out.append((p, img))
        if len(out) >= limit:
            break
    return out


def slice_at(tracker, full, dy_shift):
    """The patch list a grabber would produce if content had slid DOWN dy_shift.

    Returns None when the shifted band would leave the frame, so the caller
    never silently measures a clamped -- i.e. wrong -- displacement.
    """
    y0 = tracker.band_y - dy_shift
    if y0 < 0 or y0 + tracker.patch_h > full.shape[0]:
        return None
    out = []
    for x in tracker.xs:
        crop = full[y0:y0 + tracker.patch_h, x:x + tracker.patch, :]
        out.append(np.ascontiguousarray(crop[:, :, tracker.channel]))
    return out


def run(pattern, limit, shifts, patch_h):
    tracker = ViewTracker(patch_h=patch_h) if patch_h else ViewTracker()
    fs = frames(pattern, limit)
    if not fs:
        print(f'no {SCREEN_W}x{SCREEN_H} frames matched {pattern}')
        return 1

    print(f'{len(fs)} frames, patch {tracker.patch}x{tracker.patch_h} '
          f'at band_y={tracker.band_y}, {len(tracker.xs)} patches')
    print(f'unambiguous |dy| < {tracker.patch_h / 2:.0f} px, '
          f'stated usable < {tracker.patch_h * 3 / 8:.0f} px\n')

    print('%6s  %9s  %7s  %7s  %6s  %s' %
          ('true', 'reported', 'gain', 'sd', 'n_oor', 'per-frame reported'))
    rows = []
    for n in shifts:
        reported, oor = [], 0
        for _, full in fs:
            a = slice_at(tracker, full, 0)
            b = slice_at(tracker, full, n)
            if a is None or b is None:
                continue
            # predicted_dy = n: the out-of-range flag asks "is this reading
            # further from the prediction than the correlator can travel", and
            # here the prediction is known exactly. Passing 0 would flag every
            # large shift as out-of-range for the trivial reason that it is
            # large, which says nothing about whether it was read correctly.
            m = tracker.measure_pair(a, b, predicted_dy=float(n))
            if np.isfinite(m.dy):
                reported.append(m.dy)
            oor += int(m.out_of_range)
        if not reported:
            continue
        r = np.asarray(reported)
        gain = float(np.median(r)) / n if n else float('nan')
        rows.append((n, float(np.median(r)), gain))
        head = '  '.join('%.1f' % v for v in r[:6])
        print('%6d  %9.2f  %7.3f  %7.2f  %6d  %s' %
              (n, np.median(r), gain, r.std(), oor, head))

    # ── the guard, under the prediction it actually gets ──
    #
    # measure_pair flags a reading as out-of-range when it sits further than
    # half a patch from `predicted_dy`. The loop above fed it the TRUE shift,
    # which is not what it gets in production: MagazineRecorder.finish() passes
    # the PREVIOUS frame's reading, and at the start of a burst that is ~0.
    #
    # That difference is the whole ballgame, because a single wrap of d px
    # reports d - H, and for d in (H/2, H) that lands in (-H/2, 0) -- inside the
    # guard's own tolerance BY CONSTRUCTION. The test can only fire on a reading
    # that wrapped so far it came back out the other side. Its comment says it
    # flags "any reading further from the prediction than half a patch"; it does,
    # and a wrap is never that far.
    print('\nthe guard, with the prediction it gets in production (prev frame ~0):')
    print('%6s  %9s  %10s  %s' % ('true', 'reported', 'flagged?', ''))
    escaped = []
    for n in shifts:
        flags, rep = [], []
        for _, full in fs:
            a = slice_at(tracker, full, 0)
            b = slice_at(tracker, full, n)
            if a is None or b is None:
                continue
            m = tracker.measure_pair(a, b, predicted_dy=0.0)
            flags.append(m.out_of_range)
            rep.append(m.dy)
        if not rep:
            continue
        med = float(np.median(rep))
        wrapped = abs(med - n) > 1.0
        hit = sum(flags)
        note = ''
        if wrapped and hit == 0:
            note = '  <-- WRAPPED AND UNFLAGGED'
            escaped.append(n)
        print('%6d  %9.2f  %5d/%-4d%s' % (n, med, hit, len(flags), note))
    if escaped:
        print(f'\n  {len(escaped)} of the wrapping shifts pass the guard silently '
              f'({escaped}).')
        print('  The guard cannot catch a single wrap: a wrap of d reports d - H,')
        print('  which is within H/2 of zero for every d it can happen to.')

    # ── verdict ──
    # The claim under test is ordered ("it holds up to some size, then breaks"),
    # so the criterion has to be ordered too: report where the gain first
    # leaves a band, not whether some aggregate looks fine. This repo has been
    # bitten twice by an aggregate that could not see the dimension it governed.
    print()
    bad = [(n, g) for n, _med, g in rows if abs(g - 1.0) > 0.10]
    if not bad:
        print('VERDICT: gain within 10% of 1.0 at every shift tested.')
        print('  The correlator reads large displacements correctly on a static')
        print('  scene, so under-reading is NOT the mechanism. Look at what the')
        print('  frames themselves carry during the first shot (blur, timing),')
        print('  not at how they are correlated.')
    else:
        first = bad[0][0]
        print(f'VERDICT: gain leaves +/-10% at {first} px '
              f'(reads {bad[0][1]:.2f}x).')
        print('  Everything fitted from displacements at or above that size is')
        print('  wrong low, and the fix is the patch geometry -- not any')
        print('  constant. Re-run with --patch-h to price a taller patch.')
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--frames', default='calibration/artifacts/ads/runs/*/*.jpg')
    ap.add_argument('--limit', type=int, default=12)
    ap.add_argument('--shifts', default='2,5,10,20,40,60,80,100,120,140,160')
    ap.add_argument('--patch-h', type=int, default=None,
                    help='override RECOIL_PATCH_H to price a taller patch')
    a = ap.parse_args()
    shifts = [int(v) for v in a.shifts.split(',') if v.strip()]
    raise SystemExit(run(a.frames, a.limit, shifts, a.patch_h))


if __name__ == '__main__':
    main()
