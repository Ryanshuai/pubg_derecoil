"""Is the player in ADS? Answered by the ZEROING READOUT being drawn.

    from detector.zeroing_detector import ZeroingDetector
    z = ZeroingDetector()
    z.in_ads(frame)      # True / False
    z.ink(frame)         # raw surviving pixels, for a log line

    python detector/zeroing_detector.py --selftest      # offline, 867 frames

⚠ PRESENCE, NOT ABSENCE, AND THAT IS THE WHOLE POINT. `ads_detector` answers
the same question from the hip crosshair's ABSENCE, and an absence criterion
says "yes" for every reason the thing might not be drawn. Measured cost of
that, twice in this repository:

    an EMPTY-HANDED character draws no crosshair, so AdsDetector reads
    "scoped" forever and `ensure_hip` can never confirm what it asks for
    (2026-08-10, calibration/collect_inventory_vlm.py)

    firing, vss: static 387/387 = 1.00, while FIRING 0.79
    (2026-08-06, detector/CLAUDE.md)

The zeroing readout is drawn only when a sight picture exists. Nothing else
puts white glyphs in this box, and the box does not move.

⚠ IT IS NOT A REPLACEMENT. It is a SECOND, INDEPENDENT source, and the value
is in the pair: different pixels, different mechanism, opposite polarity. Two
readings that cannot fail the same way are what the root CLAUDE.md law asks
for -- 「同一个东西两个独立说法，对不上就拒绝」. Measured over every
full-screen frame in the ADS corpus, WITH NO LABELS INVOLVED:

    867 frames, zeroing-presence vs crosshair-absence:  867 agree = 100.000%

⚠ AND THE ONE PLACE THEY EVER DISAGREED IS WORTH KEEPING, because it says what
the readout does mid-toggle. At a gate of 30 the pair scored 865/867, and both
misses were `t0040` -- 40 ms after the right click, the documented toggle
window (40 ms still reads un-scoped, 150 ms all read scoped). Their ink sat
between 30 and 100, i.e. **the readout is FADING IN**, not yet drawn. So the
low gate was calling a half-drawn overlay "in ADS" 110 ms early.

⚠ THE GATE WAS NOT TUNED TO FIX THAT. 100 was chosen for margin on both sides
of an empty 29..269 band, before those two frames were looked at; that it also
resolves them is a check on the choice, not the reason for it. A threshold
moved until the last two samples passed is the failure mode this repository
keeps paying for.

⚠ ALL 867 ARE STATIC FRAMES, and the case that matters is FIRING. `capture_ads`
only ever captured a still character, while every consumer of this question is
mid-burst -- which is exactly why `ads_end` has 42 of 74 sample files carrying
False and nobody can say whether that is a real dropout or the detector losing
the crosshair under recoil. THIS FILE DOES NOT SETTLE THAT. It supplies the
second opinion that can, once a firing corpus exists.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from detector.row_name_detector import text_mask

# ⚠ MEASURED, NOT EYEBALLED, and the first version of this WAS eyeballed and
# was wrong. Reading the box off one screenshot gave y 1165..1215 x 1600..1840
# -- wide enough to include part of the ammo HUD -- and scored 70% against
# labels that were themselves wrong. Two errors that happened to point the
# same way.
#
# THE DERIVATION, which `--selftest` re-runs rather than trusting this comment:
# over a generous band, accumulate the white-text mask separately for ADS and
# for hip frames, and keep the pixels drawn in >90% of one class.
#
#     ADS   222 frames    265 px drawn in >90% of them
#     hip   323 frames      0 px drawn in >90% of them
#     bbox  y 1179..1207   x 1672..1765
#
# The hip column being EMPTY is what makes this a box and not a threshold: it
# is not that the text is brighter in ADS, it is that nothing is drawn here at
# all otherwise.
ZEROING_BOX = (1179, 1208, 1672, 1766)          # y0, y1, x0, x1

# Surviving white-text pixels above which the readout counts as drawn.
#
# ⚠ SET IN THE GAP, NOT ON THE EDGE. Measured over the same corpus:
#
#     ADS   median 269   p95 296   max 404
#     hip   median   0   p95   5   max  29
#
# 29..269 is empty, so anything in it separates on this corpus. 100 is 3.4x
# above the worst hip frame and 2.7x below the median ADS one -- margin on
# BOTH sides, rather than a value lowered until one sample passed. A gate at
# 30 also scores identically here and is the wrong choice for the same reason
# MSE_EMPTY_TH was raised off 450: it sits on the edge of the negative class.
INK_MIN = 100


class ZeroingDetector:
    """The zeroing readout, present or not. Stateless; holds no templates."""

    def __init__(self, box=ZEROING_BOX, ink_min=INK_MIN):
        self.box = box
        self.ink_min = ink_min

    def ink(self, frame):
        """White-text pixels inside the box. -> int (0 if out of frame).

        Returns a COUNT rather than a bool so a caller can log how far from
        the gate a reading sat. `ads_frac` became untrustworthy partly because
        nothing ever recorded the margin behind it.
        """
        y0, y1, x0, x1 = self.box
        if frame is None or frame.shape[0] < y1 or frame.shape[1] < x1:
            return 0
        return int(np.count_nonzero(text_mask(frame[y0:y1, x0:x1])))

    def in_ads(self, frame):
        """-> True / False. A frame too small to hold the box reads False."""
        return self.ink(frame) >= self.ink_min


def _selftest():
    """Re-derive the box, then check the pair. OFFLINE, no game.

    ⚠ THE AGREEMENT CHECK USES NO LABELS, deliberately. The adjudicated truth
    in calibration/fit_ads_detector.py covers six run/state combinations, and
    the filename `state` is documented as unreliable (one whole run labelled
    `ads` is shoulder aim that never scoped). Two independent detectors
    agreeing is a stronger statement than either agreeing with a label, and it
    is the only one available at this corpus size.
    """
    import glob
    import collections
    from detector.ads_detector import AdsDetector

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runs = os.path.join(root, 'calibration', 'artifacts', 'ads', 'runs')
    fails = []

    def check(label, got, want):
        ok = got == want
        print(f'  {"ok  " if ok else "FAIL"}  {label:<54} {got}')
        if not ok:
            fails.append(label)

    frames = []
    for p in glob.glob(os.path.join(runs, '*', '*', '*')):
        im = cv2.imread(p)
        if im is not None and im.shape[:2] == (1440, 3440):
            frames.append((p, im))
    print(f'{len(frames)} full-screen frames in the ADS corpus')
    if not frames:
        print('[!] no corpus on disk — this selftest cannot run')
        return 1

    # ── 1. the box, re-derived rather than trusted ──
    BAND = (1100, 1300, 1300, 2150)
    by0, by1, bx0, bx1 = BAND
    acc = collections.defaultdict(
        lambda: np.zeros((by1 - by0, bx1 - bx0), np.float32))
    n = collections.Counter()
    for p, im in frames:
        b = os.path.basename(p).lower()
        if b.startswith('ads') and any(t in b for t in
                                       ('t0400', 't0700', 't0800', 't1000')):
            k = 'ads'
        elif b.startswith(('hip', 'center')):
            k = 'hip'
        else:
            continue
        acc[k] += (text_mask(im[by0:by1, bx0:bx1]) > 0).astype(np.float32)
        n[k] += 1
    hot = (acc['ads'] / max(n['ads'], 1)) > 0.90
    ys, xs = np.nonzero(hot)
    derived = (by0 + int(ys.min()), by0 + int(ys.max()) + 1,
               bx0 + int(xs.min()), bx0 + int(xs.max()) + 1)
    check(f'the box re-derives from {n["ads"]} ADS frames', derived,
          ZEROING_BOX)
    # ⚠ AND THE NEGATIVE HALF: it is a box because nothing is drawn here
    # otherwise, not because the text is brighter.
    hip_hot = int(((acc['hip'] / max(n['hip'], 1)) > 0.90).sum())
    check(f'...and NOTHING is drawn there in {n["hip"]} hip frames',
          hip_hot, 0)

    # ── 2. the gate has room on both sides ──
    det = ZeroingDetector()
    inks = collections.defaultdict(list)
    for p, im in frames:
        b = os.path.basename(p).lower()
        if b.startswith('ads') and 't0040' not in b:
            inks['ads'].append(det.ink(im))
        elif b.startswith(('hip', 'center')):
            inks['hip'].append(det.ink(im))
    hip_max = max(inks['hip'])
    check(f'gate {INK_MIN} clears the worst hip frame ({hip_max})',
          INK_MIN > hip_max, True)
    ads_med = int(np.median(inks['ads']))
    check(f'...and sits under the median ADS frame ({ads_med})',
          INK_MIN < ads_med, True)

    # ── 3. THE PAIR. No labels.  ──
    ads_det = AdsDetector()
    agree, dis = 0, []
    for p, im in frames:
        z, c = det.in_ads(im), bool(ads_det.scoped(im))
        if z == c:
            agree += 1
        else:
            dis.append((os.path.basename(p), z, c))
    rate = agree / len(frames)
    print(f'\n  zeroing vs crosshair: {agree}/{len(frames)} = {rate:.3%}, '
          f'no labels used')
    check('the two independent readings agree above 99%', rate > 0.99, True)
    # ⚠ EVERY disagreement must be a TRANSITION frame. If one ever shows up at
    # a settled t, the two are no longer describing the same thing and this
    # file's whole claim is void -- so it fails rather than reporting a rate.
    off = [d for d in dis if 't0040' not in d[0]]
    check('every disagreement is the 40 ms toggle window', off, [])
    for name, z, c in dis[:4]:
        print(f'      {name:24} zeroing={z!s:5} crosshair={c!s:5}')

    print()
    if fails:
        print(f'{len(fails)} FAILED: {", ".join(fails)}')
        return 1
    print('all ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(_selftest() if '--selftest' in sys.argv else 0)
