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
cross-check is ever actually wanted, add it to classify() first. Its
checkpoint and its training task went on 2026-08-05, and the `device`
parameter this class kept accepting-and-ignoring went with them.
"""
import numpy as np

from config import (TAB_PIXEL_THRESH, TAB_COUNT_MIN, TAB_COUNT_MAX,
                    TAB_DARK_FLOOR_MAX)


class TabTypeDetector:
    """Pixel-based tab open/close detection."""

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
        bright_count = self.ink(crop)
        if not TAB_COUNT_MIN < bright_count < TAB_COUNT_MAX:
            return False
        # Ink implies a backdrop. Bright everywhere means sky, not glyphs.
        return int(np.percentile(np.max(crop, axis=2), 10)) < TAB_DARK_FLOOR_MAX

    @staticmethod
    def ink(crop):
        """Bright pixels in the Type/类型 band. -> int

        The number `classify` thresholds, exposed because callers were
        recomputing it and getting a DIFFERENT number. calibration/
        collect_templates.py measured it with `cvtColor(BGR2GRAY)` -- a luma
        average -- and then compared the result against TAB_COUNT_MIN/MAX,
        which were measured on the channel maximum. Averaging three channels
        to find white ink dilutes it, so every count came out low against
        bounds that assumed otherwise, and the run's own `ok` flag on those
        captures was wrong in one direction.

        One definition, one caller-visible number. Recomputing a detector's
        internals beside it is how the two drift, and this one had already
        drifted before anybody looked.
        """
        if crop is None:
            return 0
        # Channel maximum, not luma: the ink is white, so any channel carrying
        # it is evidence, and averaging the three only dilutes it.
        return int((np.max(crop, axis=2) > TAB_PIXEL_THRESH).sum())
