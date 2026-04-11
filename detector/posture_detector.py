"""Posture detector — standing/crouching/prone from HUD icon.

Pure CV: HSV bright-pixel mask ∩ (Canny edges ∪ Sobel edges), then IoU match
against the three canonical icon templates from game assets.

Validated 99.42% accuracy on 1561 training samples, ~0.5ms per frame on CPU.
State-machine constraints apply to 'c'/'z' keys (valid transition only).
"""
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import POSTURE
from detector.cropper import win32_cap
from detector.utils import img_hash as _img_hash

# Screen rect: (y, x, h, w) for win32_cap
SLOT_RECT = (POSTURE['y1'], POSTURE['x1'],
             POSTURE['y2'] - POSTURE['y1'],
             POSTURE['x2'] - POSTURE['x1'])

# Canonical icon templates from game assets (66×66, aligned with HUD crop)
ASSETS_DIR = os.path.join(os.path.dirname(__file__), '..', 'training_data',
                          'pubg_assets', 'posture')
POSTURE_CLASSES = ['standing', 'crouching', 'prone']

# Two-tier thresholds:
#   MIN_IOU (0.32) — used for the actual posture decision. Tuned for best
#                    overall accuracy (99.42% on 1561 labeled samples).
#   COLLECT_IOU_LO / COLLECT_IOU_HI (0.10, 0.50) — hard-case collection band.
#                    Any crop whose best template IoU lies in [LO, HI) is
#                    saved to COLLECT_DIR for offline review, regardless of
#                    whether the decision path accepted or rejected it.
#                    IoU ≥ 0.50 is a confident match (no need to review).
#                    IoU < 0.10 or empty mask is pure background noise.
MIN_IOU = 0.32
COLLECT_IOU_LO = 0.10
COLLECT_IOU_HI = 0.50
MIN_AREA = 200
HSV_V_THRESH = 180
HSV_S_THRESH = 80
CANNY_LOW = 50
CANNY_HIGH = 150
CANNY_DILATE_KSIZE = 5
SOBEL_THRESH = 80
SOBEL_DILATE_KSIZE = 3

COLLECT_DIR = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'posture_collect')
COLLECT_MAX = 2000

# State machine: key → {current_posture: set of valid next postures}
_VALID_TRANSITIONS = {
    'c': {
        'standing':  {'crouching'},
        'crouching': {'standing'},
        'prone':     {'crouching'},
    },
    'z': {
        'standing':  {'prone'},
        'crouching': {'prone'},
        'prone':     {'standing'},
    },
}


def _load_templates():
    """Load the 3 canonical posture icons (alpha channel → binary mask)."""
    templates = {}
    for cls in POSTURE_CLASSES:
        path = os.path.join(ASSETS_DIR, f'posture_{cls}_icon_bgra.png')
        bgra = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if bgra is None or bgra.shape[-1] != 4:
            raise FileNotFoundError(f'posture template missing or not BGRA: {path}')
        templates[cls] = (bgra[:, :, 3] > 128).astype(np.uint8)
    return templates


def _extract_silhouette(bgr):
    """Canny ∩ Sobel bright-pixel silhouette, largest connected component."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    bright = (hsv[:, :, 2] > HSV_V_THRESH) & (hsv[:, :, 1] < HSV_S_THRESH)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    # Canny branch
    canny_edges = cv2.Canny(gray, CANNY_LOW, CANNY_HIGH)
    canny_dil = cv2.dilate(canny_edges, np.ones((CANNY_DILATE_KSIZE,) * 2, np.uint8))
    canny_mask = bright & (canny_dil > 0)

    # Sobel branch
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    sobel_edges = (mag > SOBEL_THRESH).astype(np.uint8)
    sobel_dil = cv2.dilate(sobel_edges, np.ones((SOBEL_DILATE_KSIZE,) * 2, np.uint8))
    sobel_mask = bright & (sobel_dil > 0)

    # Intersection = pixels that both edge detectors agree on
    combined = (canny_mask & sobel_mask).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(combined)
    if n <= 1:
        return np.zeros_like(combined)
    sizes = stats[1:, cv2.CC_STAT_AREA]
    best = int(np.argmax(sizes)) + 1
    return (labels == best).astype(np.uint8)


class PostureDetector:
    """CV-based posture classifier. Same interface as the DL version it replaces."""

    def __init__(self, device=None, state=None):
        # device kept for interface compatibility with other detectors
        _ = device
        self.state = state
        self._templates = _load_templates()
        self._collect_count = 0
        self._last_collect_time = 0

    def _classify_raw(self, crop):
        """Internal: return (best_cls, best_iou) regardless of thresholds.

        best_cls is None only when the silhouette is empty (area < MIN_AREA).
        Otherwise best_cls is always one of the three posture classes and
        best_iou is its IoU against that template.
        """
        if crop is None or crop.size == 0:
            return None, 0.0
        mask = _extract_silhouette(crop)
        if int(mask.sum()) < MIN_AREA:
            return None, 0.0

        ious = {}
        for cls, tmpl in self._templates.items():
            if tmpl.shape != mask.shape:
                t = cv2.resize(tmpl, (mask.shape[1], mask.shape[0]),
                               interpolation=cv2.INTER_NEAREST)
            else:
                t = tmpl
            inter = int((mask & t).sum())
            union = int((mask | t).sum())
            ious[cls] = inter / union if union else 0.0

        best_cls = max(ious, key=ious.get)
        return best_cls, ious[best_cls]

    def classify(self, crop):
        """Return (label, confidence). Empty label means rejected.

        Uses MIN_IOU as the decision threshold.
        """
        best_cls, best_iou = self._classify_raw(crop)
        if best_cls is None or best_iou < MIN_IOU:
            return '', best_iou
        return best_cls, best_iou

    def _save_collect(self, crop, label):
        """Save labeled screenshot for training data (up to COLLECT_MAX)."""
        if self._collect_count >= COLLECT_MAX:
            return
        os.makedirs(COLLECT_DIR, exist_ok=True)
        h = _img_hash(crop)
        fname = f'{label}_{h}.png'
        path = os.path.join(COLLECT_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)
            self._collect_count += 1
            if self._collect_count >= COLLECT_MAX:
                print(f'[posture] collected {COLLECT_MAX} samples, stopping', flush=True)

    def _maybe_save_hard_case(self, crop, best_cls, best_iou):
        """Save crop if its best IoU falls in the hard-case band.

        Covers both 'accepted but borderline' and 'rejected but non-trivial'
        ranges: [COLLECT_IOU_LO, COLLECT_IOU_HI). The classifier's own best
        guess is used as the filename label so you can triage by comparing
        filename vs. content on disk.
        """
        if best_cls is None:
            return
        if COLLECT_IOU_LO <= best_iou < COLLECT_IOU_HI:
            self._save_collect(crop, best_cls)

    def query(self, key_name=None):
        """Capture posture region, classify, update state.

        Uses MIN_IOU (0.32) for the actual decision — this is the best-overall
        threshold from the sweep. Any crop whose best IoU lies in the hard-case
        band [COLLECT_IOU_LO, COLLECT_IOU_HI) is additionally saved to
        COLLECT_DIR for offline review, regardless of whether it was accepted.

        key_name: the trigger key ('c', 'z', 'right_down', etc.). The state
        machine still vetoes impossible transitions.
        """
        crop = win32_cap(SLOT_RECT)
        best_cls, best_iou = self._classify_raw(crop)

        # Always harvest borderline crops first (even if decision rejects)
        self._maybe_save_hard_case(crop, best_cls, best_iou)

        # Decision: below MIN_IOU means no confident match → do nothing
        if best_cls is None or best_iou < MIN_IOU:
            return

        pos = best_cls
        cur = self.state.posture
        valid = _VALID_TRANSITIONS.get(key_name, {}).get(cur)

        if valid is not None and pos not in valid:
            # CV detection conflicts with state machine → also worth keeping
            self._save_collect(crop, f'{cur}_to_{pos}_key_{key_name}')
            if len(valid) == 1:
                pos = next(iter(valid))
            else:
                return

        self.state.set_posture(pos)

    def collect_tick(self):
        """Periodic harvester: scans HUD every ~0.5s and saves borderline crops.

        Unlike query(), this path does NOT update state or consult the state
        machine — it's a pure passive sampler for the hard-case collection
        pipeline.
        """
        now = time.monotonic()
        if now - self._last_collect_time < 0.5:
            return
        self._last_collect_time = now
        crop = win32_cap(SLOT_RECT)
        best_cls, best_iou = self._classify_raw(crop)
        self._maybe_save_hard_case(crop, best_cls, best_iou)
