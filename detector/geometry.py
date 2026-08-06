"""Screen-agnostic geometry primitives shared by the layout modules.

Nothing here knows which screen it is looking at — no ROIs, no thresholds, no
game. That is the point: `segments` was living in spawner_layout.py, so
lobby_nav.py had to import the spawner's module to find rows in the lobby's
tab bar. A primitive used by two screens belongs to neither.
"""


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
