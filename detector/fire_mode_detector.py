"""Fire mode detector — a RandomForest over eight structural features.

⚠ THERE WAS A 4 MB MobileNet HERE UNTIL 2026-08-08, and it went because it was
measured, not because it felt old. It sat behind the forest as a fallback ("RF
first, and only fall through to the net when it abstains"), and on the whole
calibration/artifacts/mismatch/fire_mode corpus — 859 crops, every one of them a case somebody
collected BECAUSE something disagreed — this is what the fallback did:

    RF abstained (said 'bg')                    3 / 859   0.35%
    of those, the net gave a real answer        2 / 859   0.23%
                                                (the third agreed: also 'bg')

Two answers out of 859, on a corpus deliberately stocked with hard cases. For
that it cost a torch forward pass on the frame path, a 4 MB checkpoint, 376 MB
of background plates and 12 MB of labelled crops to train on, and it kept torch
in the detector import graph — robot.py's device line existed for this class
alone.

⚠ THE HONEST LIMIT ON THAT NUMBER: calibration/artifacts/mismatch/fire_mode is a mismatch and
hard-case sink, not a representative frame sample (87.8% of it reads
single_bot_sniper). The abstention rate on ordinary frames is probably lower
still, but nobody has an unbiased sample, so 0.35% is a ceiling measured on the
hard cases rather than an average over play.

WHAT THIS COSTS. Those two frames now return None instead of a mode. None is
already the "not readable" answer every caller handles — the HUD does not draw
this icon at all in plenty of states — so the failure is the one the interface
was built for, not a new one.

THE FEATURES, and why they are not a bag of whatever was handy: the icon is
either a STACK OF BARS (full auto, burst) or a BULLET SILHOUETTE (single,
sniper, shotgun). `big_comp` counts components over 20 px — bars give >= 6,
a bullet <= 5 — and `bright_bars` measures the same thing radiometrically by
splitting the crop into five horizontal strips. The rest (contour area, extent,
aspect, mean/std, bar_range) separate within those two families.
"""
import os
import pickle

import cv2

# The vocabulary. It lived in dl_models/icon_layout.py until 2026-08-08, where
# a comment warned its ORDER could not be edited — that was true while a
# softmax head's indices were derived from it. The forest predicts these as
# STRINGS, so the order is now just presentation, and the list lives with its
# only consumer.
FIRE_MODE_CLASSES = ['single', 'burst2', 'burst3', 'full', 'single_sniper',
                     'single_shotgun', 'high', 'single_smoke']

_RF_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models',
                        'fire_mode_structural_rf.pkl')
_rf_model = None


def _rf():
    """Load the structural forest on first use.

    Unpickling at import time meant `import detector.fire_mode_detector` — or
    anything that transitively imports it — died outright if the .pkl was
    missing, which is a strange way for an unrelated module to fail.
    """
    global _rf_model
    if _rf_model is None:
        with open(_RF_PATH, 'rb') as f:
            _rf_model = pickle.load(f)
    return _rf_model


def _extract_features(gray):
    h, w = gray.shape
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, -5)
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    big_comp = sum(1 for i in range(1, n_labels) if stats[i, cv2.CC_STAT_AREA] > 20)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        c = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(c)
        x, y, bw, bh = cv2.boundingRect(c)
        extent = area / max(bw * bh, 1)
        aspect = bw / max(bh, 1)
    else:
        area, extent, aspect = 0, 0, 0

    mean_b, std_b = gray.mean(), gray.std()
    bar_h = h // 5
    bars = [gray[i * bar_h:(i + 1) * bar_h, w // 4:3 * w // 4].mean() for i in range(5)]
    bar_range = max(bars) - min(bars)
    bright_bars = sum(1 for b in bars if b > (min(bars) + bar_range * 0.5)) if bar_range > 10 else 0

    return [big_comp, area, extent, aspect, mean_b, std_b, bar_range, bright_bars]


def _structural_classify(crop):
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return _rf().predict([_extract_features(gray)])[0]


class FireModeDetector:

    def __init__(self, device=None):
        """`device` is accepted and ignored.

        It was a torch device until 2026-08-08. Kept in the signature because
        robot.py and regression_check pass it positionally, and removing the
        parameter is a separate edit from removing the model — doing both at
        once is how a caller ends up passing a crop as a device.
        """
        self.device = device

    def classify(self, crops):
        """Classify fire mode from crop dict. Returns mode string or None."""
        crop = crops.get('fire_mode') if isinstance(crops, dict) else crops
        if crop is None:
            return None
        name = _structural_classify(crop)
        # 'bg' is the forest saying the icon is not there (or not readable).
        # That IS None to every caller — see the module docstring for what used
        # to happen next and what it was worth.
        return str(name) if name and name != 'bg' else None
