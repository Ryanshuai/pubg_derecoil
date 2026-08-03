"""Lobby navigation geometry — which mode tab is selected, and where to click.

Geometry only: every function takes a grey full-screen frame and returns
coordinates. No clicking, no capture, no game state. The driver is
`LobbyControl.ensure_mode`; the measurements are in the Lobby section of
config.py and docs/lobby/README.md.

    from detector.lobby_nav import read_mode, read_page
    page, _, _ = read_page(gray)          # 'PLAY'
    mode, margin, tabs = read_mode(gray)  # 'TRAINING', 891.0, [...]

WHY THIS EXISTS: `LOBBY_PLAY_XY` starts whatever the sub bar has selected.
Nothing used to select it, so `ensure_in_match()` entered a real match
whenever the lobby happened to be sitting on NORMAL — and per
detector/CLAUDE.md a real match cannot currently be left, because only the
training range's ESC menu has been captured and `leave_entry_confirmed()`
refuses to click LEAVE anywhere else. An unattended run that lands in NORMAL
is stuck there.

Tabs are found by projection rather than hardcoded, per calibrate-screen: sum
the bright mask down y over the bar's strip, and the runs are the labels.
Names come from position, left to right — nothing here is OCR'd, so a game
update that reorders the bars renames every tab silently. `read_page` refusing
any page but PLAY, and the caller reading the mode back after clicking, are
what turn that into a loud failure instead of a wrong click.
"""
import numpy as np

from config import (LOBBY_SUB_BAR_ROI, LOBBY_TAB_FIND_THRESH, LOBBY_TAB_GAP,
                    LOBBY_TAB_MIN_MARGIN, LOBBY_TAB_MIN_W,
                    LOBBY_TAB_SEL_THRESH, LOBBY_TOP_BAR_ROI)
from detector.geometry import segments

# Left to right, as the bars read.
TOP_TABS = ('PLAY', 'PASS', 'CAREER', 'CUSTOMIZE', 'HIDEOUT', 'WORKSHOP',
            'STORE')
SUB_TABS = ('NORMAL', 'RANKED', 'ARCADE', 'TRAINING', 'CUSTOM')

# The only sub tab anything here is allowed to start. See the module docstring.
SAFE_MODE = 'TRAINING'


def bar_labels(gray, roi, thresh=LOBBY_TAB_FIND_THRESH, gap=LOBBY_TAB_GAP,
               min_w=LOBBY_TAB_MIN_W):
    """Segment one bar's strip into labels. -> [{x0,x1,y0,y1,cx,cy,ink}, ...]"""
    y, x, h, w = roi
    strip = gray[y:y + h, x:x + w]
    mask = (strip > thresh).astype(np.uint8)
    out = []
    for a, b in segments(mask.sum(axis=0), 0, min_w, gap=gap):
        band = mask[:, a:b]
        rows = np.flatnonzero(band.sum(axis=1) > 0)
        if not len(rows):
            continue
        out.append({
            'x0': x + a, 'x1': x + b,
            'y0': y + int(rows[0]), 'y1': y + int(rows[-1]) + 1,
            'cx': x + (a + b) // 2,
            'cy': y + int(rows[0] + rows[-1]) // 2,
            'ink': int(band.sum()),
        })
    return out


def _sel_ink(gray, roi, box, thresh=LOBBY_TAB_SEL_THRESH):
    y, x, h, w = roi
    return int((gray[y:y + h, box['x0']:box['x1']] > thresh).sum())


def read_bar(gray, roi, names):
    """-> (labels, selected_label_or_None, margin_over_runner_up).

    Selection is argmax over the high-threshold ink, with the margin returned
    so a caller can refuse to act on a bar that has gone ambiguous. A plain
    ">0" test is not enough — unselected top-bar tabs pick up stray ink from
    decorations near them.

    `selected` is None when the label count is wrong, which is what a dialog
    covering the bar looks like. Do not fall back to a best guess there.
    """
    found = bar_labels(gray, roi)
    for i, s in enumerate(found):
        s['name'] = names[i] if i < len(names) else f'?{i}'
        s['sel_ink'] = _sel_ink(gray, roi, s)
    if len(found) != len(names):
        return found, None, 0.0
    order = sorted(found, key=lambda s: -s['sel_ink'])
    margin = order[0]['sel_ink'] / max(order[1]['sel_ink'], 1)
    return found, order[0], margin


def read_page(gray):
    """Which top-level tab is up. -> (name|None, margin, labels)"""
    found, best, margin = read_bar(gray, LOBBY_TOP_BAR_ROI, TOP_TABS)
    return (best['name'] if best else None), margin, found


def read_mode(gray):
    """Which mode tab is selected. -> (name|None, margin, labels)

    None means unreadable, NOT "no mode": the sub bar only exists under the
    PLAY page, so check `read_page` first.
    """
    found, best, margin = read_bar(gray, LOBBY_SUB_BAR_ROI, SUB_TABS)
    return (best['name'] if best else None), margin, found


def confident(name, margin):
    return name is not None and margin >= LOBBY_TAB_MIN_MARGIN


def tab_xy(labels, name):
    """Click point of a named tab. -> (x, y) or None.

    Valid whether or not the tab is selected: the two live captures put
    TRAINING at x 1757..1844 selected and 1758..1843 unselected. Selection
    changes brightness, not position.
    """
    for s in labels:
        if s.get('name') == name:
            return s['cx'], s['cy']
    return None
