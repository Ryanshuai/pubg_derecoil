"""Screen-agnostic primitives shared by the layout and reading modules.

Nothing here knows which screen it is looking at — no ROIs, no thresholds, no
game. That is the point: `segments` was living in spawner_layout.py, so
lobby_nav.py had to import the spawner's module to find rows in the lobby's
tab bar. A primitive used by two screens belongs to neither.

⚠ **The module is named for `segments`, and two of the three things in it are
not geometry.** `cut` slices and `detail` reads pixels. The name is kept
anyway because the contract that actually decides what may live here is the
paragraph above — knows-no-screen — and splitting on the word "geometry" would
put shared primitives in two places, which is the failure this module exists
to end. Judge additions by the contract, not the filename.

`detail` is why this file imports cv2 at all; everything else here is pure
Python. That cost is deliberate: the alternative home, `detector/utils.py`,
pulls in torch and `dl_models.train`, and `control/stock.py` needs a Laplacian
reading without needing a neural network.
"""
import cv2


def cut(frame, roi):
    """The sub-image an ROI names. -> ndarray view

    **The tuple is (y, x, h, w), not (x, y, w, h)**, and that is the whole
    reason this is a function: getting the order wrong does not raise, it
    silently returns a different rectangle, and every ROI table in this repo
    (HUD_REGIONS, LOBBY_*_ROI, the layout modules) uses the row-major order.

    Three copies of exactly this were live: lobby_detector.classify_frame's
    nested `cut(roi)`, probe_lobby_transition's and verify_lobby_detector's
    module-level `cut(frame, roi)`. Two of the three were in scripts written
    to CHECK the first one — a checker that re-implements the thing it checks
    can only ever agree with itself.

    ⚠ **This is not the repo's dominant idiom and it is not trying to be.**
    Measured 2026-08-06: 72 occurrences of `frame[y:y + h, x:x + w]`, of which
    only 11 are the two-line "unpack then slice once" shape. The other 61
    unpack because they NEED y/x/h/w for something else as well — an offset, a
    second window, a loop bound. Converting those would not remove a spelling,
    it would add one. So this is for the callers that only want the slice.
    """
    y, x, h, w = roi
    return frame[y:y + h, x:x + w]


def detail(crop):
    """High-frequency energy in a crop. -> float

    The one question underneath every "is UI drawn here, or is it the blurred
    world showing through": an icon has hard edges, blurred scenery does not.
    **Pixel variance cannot ask it** — blurred scenery is colourful, so its
    variance is high while its Laplacian is not.

    Six copies of `float(cv2.Laplacian(gray, CV_32F).var())` were live on
    2026-08-06, in five files, under five names — `detail`, two different
    `drawn`, `backpack_worn`, `panel_rows`/`_read_row`. They did not disagree
    about the maths. They disagreed about the GUARDS, which is the drift that
    does not announce itself:

        collect_templates.detail    None guard, grayscale guard
        weapon_hud_detector.drawn   None guard, grayscale guard
        attachment_detector.drawn                grayscale guard
        stock.backpack_worn         neither
        tab_items ×2                size guard only

    So `backpack_worn` raised on a single-channel crop and
    `attachment_detector.drawn` raised on None, and each would have done so
    only on the day some caller first handed it one.

    **The threshold does NOT come along.** Every caller keeps its own, and they
    are genuinely different readings of different boxes — SLOT_DETAIL_MIN 100,
    ROW_DETAIL_MIN 100, BACKPACK_DETAIL_MIN 300, PLATE_INK_MIN 12.0, each
    measured against its own corpus. Merging those would be the actual bug.

    Returns 0.0 rather than raising on None / empty, which is safe precisely
    BECAUSE every threshold is positive: `0.0 >= MIN` is False everywhere, and
    "nothing drawn" is the right answer for a crop that does not exist.
    """
    if crop is None or crop.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return float(cv2.Laplacian(gray, cv2.CV_32F).var())


def segments(profile, thresh, min_len, max_len=None, gap=0):
    """Runs of profile > thresh, merging runs closer than `gap`."""
    runs = []
    start = None
    for i, v in enumerate(profile):
        if v > thresh and start is None:
            start = i
        elif v <= thresh and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(profile)])

    if gap:
        merged = []
        for r in runs:
            if merged and r[0] - merged[-1][1] <= gap:
                merged[-1][1] = r[1]
            else:
                merged.append(r)
        runs = merged

    out = []
    for a, b in runs:
        if b - a < min_len:
            continue
        if max_len is not None and b - a > max_len:
            continue
        out.append((a, b))
    return out
