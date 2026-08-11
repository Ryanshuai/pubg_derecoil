"""Is the player in ADS? Answered by the ZEROING READOUT being drawn.

    from detector.zeroing_detector import ZeroingDetector
    z = ZeroingDetector()
    z.in_ads(frame)      # True / False
    z.iou(frame)         # 0.0 .. 1.0, for a log line

    python detector/zeroing_detector.py --selftest      # offline

⚠ PRESENCE, NOT ABSENCE, AND THAT IS THE POINT. `ads_detector` answers the same
question from the hip crosshair's ABSENCE, and an absence criterion says "yes"
for every reason the thing might not be drawn. Measured cost, twice:

    an EMPTY-HANDED character draws no crosshair, so AdsDetector reads
    "scoped" forever and `ensure_hip` can never confirm what it asks for

    firing, vss: static 387/387 = 1.00, while FIRING 0.79

⚠ AND `ensure_hip` REALLY DOES RETURN TRUE WHILE SCOPED. Frame #10 of the
2026-08-10 sight capture is a 2x scope picture -- tube, magnified scene, no
character -- captured immediately after `ensure_hip()` reported success. That
matters beyond this file: `goto_midline` positions pitch in hip fire
specifically so the scope's own sensitivity stays out of the measurement.

⚠ IT MATCHES THE GLYPHS. IT USED TO COUNT WHITE PIXELS IN A BOX, AND THAT WAS
WRONG. `text_mask` keeps anything bright and achromatic, which is a fair
description of sky, cloud and concrete as well as of text, so a box full of
world scored like a box full of writing:

    frame           eye     ink   IoU
    2x/hip v3       NOT     207   0.000      bright scenery in the box
    vss/hip v1      NOT     105   0.000
    vss/hip v4      NOT     159   0.000

⚠ THE INK VERSION SCORED 100% ON 867 FRAMES AND 95% ON 60, and the 60 are the
honest number: they were captured at six different view angles, the 867 were
one pose in one place. **A corpus that cannot break a criterion will always
agree with it** -- detector/CLAUDE.md's third law, paid for again.

MEASURED, against labels read off the frames BY EYE (no detector produced any
of them; `docs/ads_eyeball_labels.json`):

    ADS      n=31   IoU  min 0.648   p05 0.799   median 0.978
    not ADS  n=29   IoU  min 0.000   max 0.000        <- not "low". ZERO.
    -> CLEAN, no overlap, 60/60

The negatives are exactly 0.000 because `_template_match` discards a candidate
whose correlation peak is under 0.5 before any IoU is computed: there is no
partial credit for a box that merely has ink in it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from detector.row_name_detector import text_mask
from detector.weapon_template_detector import _template_match

TMPL = os.path.join(os.path.dirname(__file__), '..', 'data', 'templates',
                    'ocr_white', 'hud', 'zeroing.png')

# Where to LOOK, deliberately larger than where the text SITS. The readout was
# measured at y 1179..1207, x 1672..1765 (265 px drawn in >90% of 222 ADS
# frames, ZERO px in >90% of 323 hip frames); this band gives the match room to
# slide rather than pinning it to a box derived from one capture session.
BAND = (1160, 1225, 1630, 1810)          # y0, y1, x0, x1

# Same floor as `row_name_detector`, which uses the same scorer and the same
# mask. Sitting under the worst true positive (0.648) and far above every
# negative (0.000) -- margin on both sides rather than a value lowered until a
# sample passed.
IOU_MIN = 0.55


class ZeroingDetector:
    """The zeroing readout, matched as glyphs rather than counted as ink."""

    def __init__(self, tmpl_path=TMPL, band=BAND, iou_min=IOU_MIN):
        self.band = band
        self.iou_min = iou_min
        img = cv2.imread(tmpl_path, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise FileNotFoundError(
                f'no zeroing template at {tmpl_path}. Rebuild it with '
                f'`pixi run zeroing --rebuild` from a captured ADS frame; a '
                f'detector that silently loads nothing would read "never in '
                f'ADS" on every frame.')
        # ⚠ IMREAD_GRAYSCALE DOES NOT GUARANTEE ONE CHANNEL -- anything that
        # imported ultralytics has replaced cv2.imread with a colour-defaulting
        # wrapper, and every mask op below would then compare the wrong array.
        if img.ndim == 3:
            img = img[:, :, 0]
        self._bank = {'zeroing': [(img > 127).astype('uint8') * 255]}

    def iou(self, frame):
        """Windowed IoU of the readout against this frame. -> 0.0 .. 1.0

        A number rather than a bool so a caller can log how far from the gate a
        reading sat. `ads_frac` became untrustworthy partly because nothing
        ever recorded the margin behind it.
        """
        y0, y1, x0, x1 = self.band
        if frame is None or frame.shape[0] < y1 or frame.shape[1] < x1:
            return 0.0
        hits = _template_match(frame[y0:y1, x0:x1], self._bank,
                               mask_fn=text_mask)
        return float(hits[0][0]) if hits else 0.0

    def in_ads(self, frame):
        """-> True / False. A frame too small to hold the band reads False."""
        return self.iou(frame) >= self.iou_min


def _selftest():
    """Against labels read off the frames BY EYE. OFFLINE, no game.

    ⚠ THE TRUTH HERE COMES FROM A HUMAN READING, and that is a requirement
    rather than a convenience. Every automatic label available for this
    question is produced by one of the detectors under test: the capture
    program's own `state` is what `ensure_ads`/`ensure_hip` believed, and those
    ask `AdsDetector`. Scoring either detector against that is scoring it
    against itself -- which is how the ink version came to report 98.3% on a
    corpus where it was in fact wrong three times in twenty-nine.
    """
    import json

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # ⚠ THE LABELS ARE TRACKED, THE PIXELS ARE NOT. The frames live under
    # calibration/artifacts (gitignored) and can be re-captured with
    # `pixi run sight-intersect`; this file is a human reading and NOTHING can
    # re-produce it. So it sits in docs/ and the pixels are resolved against it.
    out = os.path.join(root, 'calibration', 'artifacts', 'sight_intersect')
    lab = os.path.join(root, 'docs', 'ads_eyeball_labels.json')
    fails = []

    def check(label, got, want):
        ok = got == want
        print(f'  {"ok  " if ok else "FAIL"}  {label:<52} {got}')
        if not ok:
            fails.append(label)

    if not os.path.exists(lab):
        print(f'[!] no eyeball labels at {lab} — this selftest cannot run')
        return 1
    frames = json.load(open(lab, encoding='utf-8'))['frames']
    det = ZeroingDetector()
    pos, neg, wrong = [], [], []
    for r in frames:
        im = cv2.imread(os.path.join(out, r['file']))
        if im is None:
            continue
        v = det.iou(im)
        want = r['label_eyeball'] == 'ads'
        (pos if want else neg).append(v)
        if det.in_ads(im) != want:
            wrong.append((r['n'], r['sight'], r['state'], round(v, 3)))
    if not pos or not neg:
        print(f'[!] {len(pos)} positive / {len(neg)} negative frames on disk — '
              f'the labels are tracked but the pixels are not. Re-capture with '
              f'`pixi run sight-intersect`; NOT reporting a score on a corpus '
              f'missing one of its classes.')
        return 0
    print(f'{len(pos) + len(neg)} frames with a HUMAN label')
    print(f'  ADS      n={len(pos):3}  IoU min {min(pos):.3f}  '
          f'median {sorted(pos)[len(pos) // 2]:.3f}')
    print(f'  not ADS  n={len(neg):3}  IoU max {max(neg):.3f}')
    check('every ADS frame clears the gate', [w for w in wrong if w], [])
    check('the classes do not overlap', min(pos) > max(neg), True)
    check(f'the gate {IOU_MIN} sits under the worst true positive',
          IOU_MIN < min(pos), True)
    check('...and above every negative', IOU_MIN > max(neg), True)

    # ⚠ AND THE OLD CRITERION, KEPT AS A LIVE COMPARISON rather than a claim in
    # prose. If counting ink ever beats matching glyphs on this corpus, the
    # rewrite was not worth its complexity and this says so.
    import numpy as np
    ink_ok = 0
    for r in frames:
        im = cv2.imread(os.path.join(out, r['file']))
        if im is None:
            continue
        ink = int(np.count_nonzero(text_mask(im[1179:1208, 1672:1766])))
        ink_ok += (ink >= 100) == (r['label_eyeball'] == 'ads')
    n = len(pos) + len(neg)
    print(f'\n  glyph match {n - len(wrong)}/{n},  ink count {ink_ok}/{n}')
    check('matching glyphs beats counting ink', n - len(wrong) > ink_ok, True)

    print()
    if fails:
        print(f'{len(fails)} FAILED: {", ".join(fails)}')
        return 1
    print('all ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(_selftest() if '--selftest' in sys.argv else 0)
