"""Spawner screen detector — is the training range's item-spawner panel up?

Template matching on the three save-loadout / load-loadout / equip-lv3 button
glyphs at the bottom right. They exist on no other screen, so all three
matching is a far more specific answer than "the panel's category columns are
where I expect them".

Templates are binary masks of the glyphs' opaque bright pixels; see the
Spawner screen section of config.py for why nothing else about the tile is
usable, and docs/spawner/README.md for the measurements.
"""
import os

import cv2
import numpy as np

from config import (SPAWNER_ICON_ANCHORS, SPAWNER_ICON_W, SPAWNER_ICON_H,
                    SPAWNER_ICON_THRESH, SPAWNER_ICON_SEARCH,
                    SPAWNER_MIN_SCORE)

ASSET_DIR = os.path.join(os.path.dirname(__file__), '..', 'training_data',
                         'pubg_assets')
TMPL_NAME = 'spawner_icon_{}_mask.png'
N_ICONS = len(SPAWNER_ICON_ANCHORS)


def icon_mask(img):
    """Binary map of the opaque near-white UI pixels."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return ((gray > SPAWNER_ICON_THRESH) * 255).astype(np.uint8)


def build_templates(frame, asset_dir=ASSET_DIR):
    """Cut the three glyphs out of a known-good spawner screenshot."""
    os.makedirs(asset_dir, exist_ok=True)
    m = icon_mask(frame)
    paths = []
    for i, (x, y) in enumerate(SPAWNER_ICON_ANCHORS, 1):
        p = os.path.join(asset_dir, TMPL_NAME.format(i))
        cv2.imwrite(p, m[y:y + SPAWNER_ICON_H, x:x + SPAWNER_ICON_W])
        paths.append(p)
    return paths


class SpawnerDetector:
    """Screen-level check: are we on the item-spawner panel?"""

    def __init__(self, asset_dir=ASSET_DIR):
        self._templates = []
        self._load_templates(asset_dir)

    def _load_templates(self, asset_dir):
        for i in range(1, N_ICONS + 1):
            path = os.path.join(asset_dir, TMPL_NAME.format(i))
            t = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if t is None:
                self._templates = []
                return
            self._templates.append(t)

    @property
    def ready(self):
        return len(self._templates) == N_ICONS

    def scores(self, frame):
        """Best match per glyph, searched around its anchor. Full-screen BGR.

        Returns N_ICONS zeros when the templates are missing, so a caller that
        only looks at the verdict fails closed.
        """
        if not self.ready:
            return [0.0] * N_ICONS
        out = []
        for tmpl, (x, y) in zip(self._templates, SPAWNER_ICON_ANCHORS):
            x0, y0 = max(0, x - SPAWNER_ICON_SEARCH), max(0, y - SPAWNER_ICON_SEARCH)
            x1 = min(frame.shape[1], x + SPAWNER_ICON_W + SPAWNER_ICON_SEARCH)
            y1 = min(frame.shape[0], y + SPAWNER_ICON_H + SPAWNER_ICON_SEARCH)
            # Threshold inside the search window, not over the whole frame:
            # icon_mask is per-pixel, so this is the same answer for 1/100th of
            # the work — the three windows are ~44k px against 4.95M.
            win = icon_mask(frame[y0:y1, x0:x1])
            if win.shape[0] < tmpl.shape[0] or win.shape[1] < tmpl.shape[1]:
                out.append(0.0)
                continue
            out.append(float(cv2.matchTemplate(
                win, tmpl, cv2.TM_CCOEFF_NORMED).max()))
        return out

    def classify(self, frame, min_score=SPAWNER_MIN_SCORE):
        """True when the item-spawner panel is on screen."""
        return all(s >= min_score for s in self.scores(frame))
