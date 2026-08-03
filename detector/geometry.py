"""Screen-agnostic geometry primitives shared by the layout modules.

Nothing here knows which screen it is looking at — no ROIs, no thresholds, no
game. That is the point: `segments` was living in spawner_layout.py, so
lobby_nav.py had to import the spawner's module to find rows in the lobby's
tab bar. A primitive used by two screens belongs to neither.
"""


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
