"""Tab type detector — is inventory open?

Reads the "Type" / 类型 header region. The count band and the dark floor are
still there, but only as a cheap pre-filter; WHAT DECIDES IS COLOUR — the
glyphs are pure white and the world is not.

⚠ A TREE BEATS THE PANEL AT ITS OWN TEST. Measured live 2026-08-05, one
viewpoint, Tab genuinely shut, the region over a tree against sky:

    Tab SHUT (tree)    count 299 / 301    floor 59
    Tab OPEN (类型)     count 204          floor 60

The trunk supplies the dark floor and the sky between the branches supplies
the bright count, so the false case scores HIGHER on the very feature meant to
separate them, and the two states are simply not distinguishable this way. The
dark floor was added for the opposite failure — open sky, floor 190..199 —
and a tree defeats both halves at once.

That is not a corner case, it is a cascade. A dozen `cond: '!tab_open'`
entries gate on this, INCLUDING whether recoil compensation runs, and
`ensure_tab(False)` re-presses while the reading disagrees — so facing a tree
made every Tab press toggle the panel open and shut again and report the key
as swallowed. It cost a posture run, a vector arm and a famas cell on
2026-08-05 before anyone looked at the pixels.

`tools/test_tab_open.py` scored 0 false-open over 970 stored shots throughout,
because not one of them has a tree in that window. A corpus can only refute
what it contains.

The Chinese client makes it worse without being the cause: 类型 is two glyphs
where TYPE is four, so the real label carries FEWER bright pixels (204) and
sits nearer the bottom of the band, leaving more room for scenery to outscore
it. The root cause is that the predicate never looked at shape.

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
                    TAB_DARK_FLOOR_MAX, TAB_TYPE_SAT_MAX)


class TabTypeDetector:
    """Is the inventory panel up. Colour decides; the counts pre-filter."""

    def classify(self, crops):
        """Check if the Type / 类型 header is currently visible.

        Returns True if tab is open, False if closed. Used for calibration —
        directly reflects screen state.

        Takes the crop either bare or under 'type' in a dict, so a caller that
        already holds the region (a banded grabber) hands it straight over
        rather than paying for a second capture.
        """
        crop = crops.get('type') if isinstance(crops, dict) else crops
        if crop is None:
            return False
        # Pre-filters. Both are cheap, both are correct on every stored frame,
        # and NEITHER is sufficient — a tree passes both. See the module
        # docstring.
        if not TAB_COUNT_MIN < self.ink(crop) < TAB_COUNT_MAX:
            return False
        if int(np.percentile(np.max(crop, axis=2), 10)) >= TAB_DARK_FLOOR_MAX:
            return False
        sat = self.saturation(crop)
        return sat is not None and sat <= TAB_TYPE_SAT_MAX

    @staticmethod
    def saturation(crop, thresh=TAB_PIXEL_THRESH):
        """Median HSV saturation of the BRIGHT pixels. -> float | None

        The glyphs are pure white; the world is not. Everything the region can
        show while the panel is shut carries colour — blue sky, green leaves,
        brown bark, tan sand — and it only takes one channel sitting below the
        others to say so.

        None when there are too few bright pixels to have a median worth
        taking; the count pre-filter has already excluded that case, and this
        is here so the function is safe to call on its own.
        """
        px = crop[np.max(crop, axis=2) > thresh].astype(np.float32)
        if px.shape[0] < 20:
            return None
        return float(np.median(
            1.0 - px.min(axis=1) / np.maximum(px.max(axis=1), 1.0)))

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
