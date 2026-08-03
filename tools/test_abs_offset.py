"""How far from its reference can the view be and still be PLACED against it?

    pixi run abs-offset

Offline, on 893 stored full-screen game frames. No game, no hardware.

absolute_offset() is the only check that can catch the incremental integral
having started from a wrong belief — drive pending_pitch to zero and the view
stays wherever that belief was wrong by, and nothing else notices. It works by
phase-correlating the current patches against a snapshot taken at the start of
the cell.

Phase correlation wraps at half a patch. So the check has a range, and past it
it does not degrade gracefully: the peak comes back a whole patch out, every
patch wraps together so the cross-patch agreement still looks healthy, and the
answer is confident and wrong. aim.absolute_offset refuses anything beyond
ABS_TRUST_FRAC * patch_h / 2 rather than believe it — 77 px, 50 counts.

Fifty counts is not much. A magazine walks the view by its whole residual, so
after three or four the view is out of range of its own reference and every
recentre falls back to the running total. That is the "cannot place the view
against the cell's reference" line, and it appeared on eight of nine magazines
in docs/impulse/ab_aug_0803_0050.json.

The fix is pre-shifting, which measure_pair's own docstring already names as
the caller's job: roll the reference by the offset the integral predicts, and
correlate what is left. The residual is then small whatever the absolute
distance, so the range stops being "±50 counts from the reference" and becomes
"±50 counts from the PREDICTION" — which is the quantity the check exists to
verify anyway.

GROUND TRUTH IS EXACT HERE, which is why this is worth doing offline. A
vertical view shift of D pixels moves band content up by D, and these are full
screenshots — so the shifted patch is sliced from the same image at band_y + D
rather than synthesised. No interpolation, no resampling, no model of what a
shift looks like.
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2
import numpy as np

from config import (RECOIL_PATCH, RECOIL_PATCH_H, RECOIL_PATCH_XS,
                    RECOIL_BAND_Y, SCREEN_H, SCREEN_W)
from detector.view_tracker import ViewTracker

K = 1.5474          # red dot, measured
SHIFTS_PX = [0, 20, 40, 77, 100, 128, 200, 300, 400]
PRED_ERRORS = [0, 10, 20, 40, 60]      # px of error in the predicted offset
TOL_PX = 4.0        # a placement is right if it is within this of the truth
ABS_TRUST_FRAC_PX = 0.6 * RECOIL_PATCH_H / 2      # aim.ABS_TRUST_FRAC, in px
ROLL_MAX_PX_EXPECTED = 0.5 * RECOIL_PATCH_H       # aim.ROLL_MAX_FRAC, in px

FAILS = []


def patches_at(img, y):
    """The seven tracked patches, sliced with their top edge at `y`."""
    out = []
    for x in RECOIL_PATCH_XS:
        crop = img[y:y + RECOIL_PATCH_H, x:x + RECOIL_PATCH]
        if crop.shape[:2] != (RECOIL_PATCH_H, RECOIL_PATCH):
            return None
        out.append(np.ascontiguousarray(crop[:, :, 1]))
    return out


def roll_ref(ref, shift_px):
    """Pre-shift the reference by the predicted screen displacement.

    np.roll rather than a crop because the reference IS a patch — the frame it
    came from is long gone by the time this is asked. The wrapped band it
    leaves at one edge is what the Hanning window is for, and how much of it
    the correlation tolerates is the thing this file measures rather than
    assumes.
    """
    s = int(round(shift_px))
    return [np.ascontiguousarray(np.roll(p, s, axis=0)) for p in ref]


def usable_frames(limit=40):
    """Frames big enough to slice a shifted band out of."""
    out = []
    need = RECOIL_BAND_Y + RECOIL_PATCH_H + max(SHIFTS_PX)
    for p in sorted(glob.glob(os.path.join(ROOT, 'docs', 'ads', 'runs',
                                           '**', '*.jpg'), recursive=True)):
        img = cv2.imread(p)
        if img is None or img.shape[0] < need or img.shape[1] < SCREEN_W:
            continue
        out.append((os.path.relpath(p, ROOT), img))
        if len(out) >= limit:
            break
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    frames = usable_frames()
    if not frames:
        print('[!] no stored frames tall enough — need '
              f'{RECOIL_BAND_Y + RECOIL_PATCH_H + max(SHIFTS_PX)} rows')
        return 1
    print(f'{len(frames)} frames, patch {RECOIL_PATCH}x{RECOIL_PATCH_H}, '
          f'band y={RECOIL_BAND_Y}\n')

    tr = ViewTracker()

    # ── 1. how far the CURRENT check reaches ──
    print('current: correlate against the raw reference')
    print('  shift px   counts   placed   err px   verdict')
    plain = {}
    for s in SHIFTS_PX:
        errs, placed = [], 0
        for _, img in frames:
            ref = patches_at(img, RECOIL_BAND_Y)
            cur = patches_at(img, RECOIL_BAND_Y + s)
            if ref is None or cur is None:
                continue
            m = tr.measure_pair(ref, cur)
            # Content moves UP the screen when the band is sampled lower, so
            # the correlator reports -s. Compare on magnitude.
            ok = (not m.out_of_range and np.isfinite(m.dy)
                  and abs(abs(m.dy) - s) <= TOL_PX)
            placed += ok
            if np.isfinite(m.dy):
                errs.append(abs(m.dy) - s)
        frac = placed / len(frames)
        plain[s] = frac
        print(f'  {s:7d}   {s/K:6.0f}   {frac:5.0%}    '
              f'{np.mean(errs) if errs else float("nan"):+6.1f}   '
              f'{"ok" if frac > 0.9 else "REFUSED/WRONG"}')

    # ── 2. the same shifts, with the reference pre-shifted ──
    print('\npre-shifted: roll the reference by the PREDICTED offset first')
    print('  shift px   pred err   placed   err px')
    shifted = {}
    for s in SHIFTS_PX:
        for e in PRED_ERRORS:
            pred = s - e            # the integral is wrong by e px
            errs, placed = [], 0
            for _, img in frames:
                ref = patches_at(img, RECOIL_BAND_Y)
                cur = patches_at(img, RECOIL_BAND_Y + s)
                if ref is None or cur is None:
                    continue
                # Sampling the band lower moves content up: -pred.
                m = tr.measure_pair(roll_ref(ref, -pred), cur)
                if not np.isfinite(m.dy):
                    continue
                total = -pred + m.dy          # roll + residual
                errs.append(abs(total) - s)
                placed += (not m.out_of_range) and abs(abs(total) - s) <= TOL_PX
            frac = placed / len(frames)
            shifted[(s, e)] = frac
            print(f'  {s:7d}   {e:8d}   {frac:5.0%}    '
                  f'{np.mean(errs) if errs else float("nan"):+6.1f}')

    # ── verdict ──
    print('\n── what changed ──')
    # The claim being tested: pre-shifting turns "within 50 counts of the
    # reference" into "within 50 counts of the prediction". So a big absolute
    # shift with a SMALL prediction error must now place, where it did not.
    # NOTE what this turned out to be, because the first version of this file
    # asserted something else and the measurement said no.
    #
    # The hypothesis was "pre-shifting removes the range limit". It does not,
    # and the reason is physical rather than a tuning failure: the reference
    # is a 256-pixel patch, np.roll WRAPS, and past one patch height there is
    # simply none of the sought content left in it. 300 px places 20%, 400 px
    # 8% — no threshold rescues that.
    #
    # What it actually buys is the band from 128 px down, where the raw match
    # was already refusing (77 px) or wrong. So the assertion is on that band,
    # and ROLL_MAX_FRAC exists to make everything past it a REFUSAL rather
    # than a confident wrong answer. Which is the real win here: at 190 px the
    # raw method returns +43 counts when the truth is -123.
    for s in (128, 200, 300, 400):
        before = plain.get(s, 0.0)
        after = shifted.get((s, 20), 0.0)
        print(f'  {s:3d} px ({s/K:3.0f} counts) from the reference, '
              f'integral off by 13 counts: {before:.0%} -> {after:.0%}'
              + ('' if s <= 128 else '   (beyond ROLL_MAX_FRAC: refused live)'))
        if before > 0.5:
            FAILS.append(f'{s}px placed {before:.0%} WITHOUT pre-shift — the '
                         f'premise that the range is ~77px is wrong')
        if s <= ROLL_MAX_PX_EXPECTED and after < 0.9:
            FAILS.append(f'{s}px is inside the roll budget but still only '
                         f'places {after:.0%} with pre-shift')

    # And the guard: a prediction that is badly wrong must NOT be rescued.
    # Pre-shifting must extend the range, not launder a wrong belief into a
    # confident answer -- that would be strictly worse than refusing.
    bad = shifted.get((200, 60), 0.0)
    print(f'\n  guard: integral off by 39 counts at 200 px -> {bad:.0%} placed')
    print('         (this SHOULD still work — 60 px is inside half a patch.')
    print('          The wrong-belief case is tested by aim.py\'s '
          'ABS_AGREE_COUNTS,')
    print('          which compares the two independent estimates.)')

    # ── 3. the real method, with its real signs ──
    #
    # Everything above is the algorithm. This is aim.ViewDriver.absolute_offset
    # itself, driven against a fake frame source, because the sign conventions
    # are where a change like this goes wrong silently: a pre-shift with the
    # wrong sign does not error, it doubles the offset and then refuses, and
    # the symptom is "the reference stopped working" rather than "the roll is
    # backwards".
    print('\nthe real absolute_offset(), signs included')
    from control.aim import ViewDriver, ROLL_MAX_FRAC

    class FakeFrames:
        """Serves the band at whatever y the test has moved the view to."""

        def __init__(self, img):
            self.img = img
            self.y = RECOIL_BAND_Y

        def grab(self):
            return {f'recoil_{i}': self.img[self.y:self.y + RECOIL_PATCH_H,
                                            x:x + RECOIL_PATCH]
                    for i, x in enumerate(RECOIL_PATCH_XS)}

        def flush(self, n=0):
            pass

    _, img0 = frames[0]
    ff = FakeFrames(img0)
    vd = ViewDriver(tr, mouse=None, frames=ff, K=K, sight='red_dot')
    vd.set_reference()

    print('  band px   true counts   predicted   returned   verdict')
    for s in (0, 40, 100, 150, 190):
        ff.y = RECOIL_BAND_Y + s
        truth = -s / K          # content up = the view rotated down
        for pred in (0.0, truth):
            got = vd.absolute_offset(predicted=pred)
            tag = 'no pred' if pred == 0.0 else 'with pred'
            if got is None:
                verdict = 'refused'
                ok = abs(truth) * K > ABS_TRUST_FRAC_PX or pred == 0.0
            else:
                verdict = 'ok' if abs(got - truth) < 3 else 'WRONG'
                ok = verdict == 'ok'
            print(f'  {s:7d}   {truth:11.0f}   {tag:>9}   '
                  f'{"None" if got is None else f"{got:8.1f}"}   {verdict}')
            # The claim: with a good prediction, a distance that used to be
            # refused is now placed. Only assert that direction.
            if pred != 0.0 and s * 1.0 <= ROLL_MAX_FRAC * RECOIL_PATCH_H \
                    and (got is None or abs(got - truth) >= 3):
                FAILS.append(f'{s}px with a correct prediction: '
                             f'returned {got}, want {truth:.0f}')
        ff.y = RECOIL_BAND_Y

    # And the refusal still happens when the prediction is far enough out that
    # the roll would exceed what the patch can hold.
    ff.y = RECOIL_BAND_Y + 100
    far = vd.absolute_offset(predicted=-400 / K)
    print(f'\n  prediction 400 px out (past ROLL_MAX_FRAC*{RECOIL_PATCH_H}): '
          f'{"refused" if far is None else f"returned {far:.0f}"}')
    if far is not None:
        FAILS.append('a roll past ROLL_MAX_FRAC was not refused')

    if FAILS:
        print(f'\n{len(FAILS)} problem(s):')
        for f in FAILS:
            print(f'  {f}')
        return 1
    print('\nall ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
