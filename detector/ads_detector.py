"""Is the player scoped in right now, from a single frame.

The cue is the hip-fire crosshair, and specifically its *absence*. PUBG draws a
crosshair at screen centre whenever the player is not looking through a sight,
and draws nothing there once they are — no reticle a scope paints is in the
same place with the same shape. So this asks "is the crosshair still there?"
and answers the ADS question by negation.

Two crosshairs, not one. Holding the right button is shoulder aim, which is
still hip fire, and the crosshair tightens when it happens: the four ticks sit
at ±56 px standing still and pull in while aiming. Matching only the wide one
reads shoulder aim as scoped, which is exactly the distinction that matters —
run 20260801_222936 is 64 frames of shoulder aim that a wide-only match called
ADS. Both templates are tried and the better score wins.

Scoring is relative, never absolute. Each template is five blobs (four ticks
and a centre dot); a frame scores each blob's mean dewhite minus the mean of a
ring around it, and takes the *minimum* over the five. Requiring every arm to
be lit is what rejects a scope's own reticle, which lights the centre and
nothing else: on the 3x that single change moved the worst case from 53.9 to
0.03. And because the score is a local difference rather than a brightness, it
does not care that a magnified scene is sharper than a hip one — an absolute
"count the bright pixels" version of this fails outright (scoped frames reach
3667 bright pixels, un-scoped ones can reach 0).

Measured over 492 labelled frames from four runs — six scopes in the run it
was fitted on, eight in one it was not, plus the shoulder-aim negatives:

    not scoped (n=344)   min  20.8   median  83.3
    scoped     (n=148)   max   1.5   median  -7.4

THRESHOLD sits between them with 14x of room. The weakest un-scoped frames are
mid-transition into shoulder aim, where the crosshair is halfway between the
two shapes and matches neither well.

Latency is under 150 ms: frames sampled 40 ms after the button still read
un-scoped (the toggle has not landed), and every frame at 150 ms reads scoped.
That is the reason to prefer this over the posture icon, which is the other
ADS indicator this project has (docs/game_quirks.md) and which still missed
5% of settled scoped frames in the same data.

    from detector.ads_detector import AdsDetector
    ads = AdsDetector()
    ads.scoped(frame)        # True / False
    ads.score(frame)         # the raw margin, for logging a near miss
"""
import os

import cv2
import numpy as np

from dl_models.icon_merging import dewhite

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(HERE, '..', 'training_data', 'pubg_assets',
                             'ads_crosshair.npz')

# The crop is centred on the screen centre and has to hold the outermost tick
# (±56 px) plus the ring drawn around it.
CROP_R = 70

BLOB_MIN = 60.0          # dewhite level that counts as crosshair in a template
RING = 9                 # dilation that makes the local background ring
THRESHOLD = 10.0         # between 1.5 (worst scoped) and 20.8 (worst un-scoped)


class AdsDetector:

    def __init__(self, path=TEMPLATE_PATH):
        self.templates = []
        data = np.load(path)
        for name in sorted(data.files):
            self.templates.append(_blobs(data[name]))
        if not self.templates:
            raise RuntimeError(f'no crosshair templates in {path}')

    def crop(self, frame):
        h, w = frame.shape[:2]
        cy, cx = h // 2, w // 2
        return frame[cy - CROP_R:cy + CROP_R, cx - CROP_R:cx + CROP_R]

    def score(self, frame):
        """How strongly a crosshair is present. High = not scoped."""
        return self.score_crop(self.crop(frame))

    def score_crop(self, crop):
        """Same, for a caller that already holds the centre crop.

        Exists so a per-frame loop can have the crosshair region grabbed
        alongside everything else instead of paying for a full-screen capture
        to throw all but 140x140 of it away.
        """
        d = dewhite(crop).astype(np.float32)
        best = -1e9
        for parts, ring in self.templates:
            bg = float(d[ring].mean())
            best = max(best, min(float(d[p].mean()) - bg for p in parts))
        return best

    def scoped(self, frame):
        """True when the player is looking through a sight."""
        return self.score(frame) < THRESHOLD

    def scoped_crop(self, crop):
        """True when the player is looking through a sight, from the crop."""
        return self.score_crop(crop) < THRESHOLD


def _blobs(median_dewhite):
    """A template's five parts and the ring of background around them."""
    mask = (median_dewhite > BLOB_MIN).astype(np.uint8)
    n, lab = cv2.connectedComponents(mask)
    parts = [lab == i for i in range(1, n)]
    ring = (cv2.dilate(mask, np.ones((RING, RING), np.uint8)).astype(bool)
            & ~mask.astype(bool))
    return parts, ring
