"""Tab type detector — is inventory open?

Reads the "Type" / 类型 header region: the glyphs are near-white ink on the
panel's dimmed backdrop, so an open panel shows both a bright pixel count in a
narrow band AND a surviving dark floor. 0.01 ms, no model.

THE DARK FLOOR IS NOT DECORATION. The count alone called 15 of 868 stored ADS
frames "open" — the region sits over the training range's sky and ADS blows a
patch of pale blue into it, which lands in the same count band. See the
measured distributions at TAB_DARK_FLOOR_MAX in config.py.

This is the ONLY copy of this predicate. control/gun.py and calibration/state.py
each grew their own — luma instead of the channel maximum, a closed band
instead of an open one — and both carried the sky bug. They call in here now.
The two callers still differ in how the crop ARRIVES (a banded grabber's dict
vs. an on-demand win32_cap), which is a capture concern, not a judgement one.

There used to be a MobileNet loaded here as an "optional cross-check". It was
never consulted — classify() has only ever looked at pixels — so it cost a
checkpoint read and a chunk of VRAM per process to compute nothing. If a
cross-check is ever actually wanted, add it to classify() first.
"""
import numpy as np

from config import (TAB_PIXEL_THRESH, TAB_COUNT_MIN, TAB_COUNT_MAX,
                    TAB_DARK_FLOOR_MAX)


class TabTypeDetector:
    """Pixel-based tab open/close detection."""

    def __init__(self, device=None):
        # device is accepted and ignored, so robot.py's call site still works.
        self.device = device

    def classify(self, crops):
        """Check if Type text is currently visible.

        Returns True if tab is open (Type visible), False if closed.
        Used for calibration — directly reflects screen state.

        Takes the crop either bare or under 'type' in a dict, so a caller that
        already holds the region (a banded grabber) hands it straight over
        rather than paying for a second capture.
        """
        crop = crops.get('type') if isinstance(crops, dict) else crops
        if crop is None:
            return False

        # Channel maximum, not luma: the ink is white, so any channel carrying
        # it is evidence, and averaging the three only dilutes it.
        gray = np.max(crop, axis=2)
        bright_count = int((gray > TAB_PIXEL_THRESH).sum())
        if not TAB_COUNT_MIN < bright_count < TAB_COUNT_MAX:
            return False
        # Ink implies a backdrop. Bright everywhere means sky, not glyphs.
        return int(np.percentile(gray, 10)) < TAB_DARK_FLOOR_MAX
