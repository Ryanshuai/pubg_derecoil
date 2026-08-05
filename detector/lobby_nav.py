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

Tabs are FOUND by projection, per calibrate-screen: sum the bright mask down y
over the bar's strip, and the runs are the labels. They are NAMED by matching
each run against one stored mask per label — `training_data/pubg_assets/lobby/
tabs/<bar>_<NAME>.<lang>.png`, built by `tools/build_lobby_tab_templates.py`.

NAMING USED TO COME FROM POSITION, and that broke on the Chinese client
(2026-08-05). Run i was called `TOP_TABS[i]`, which needs the run count to be
exactly right, and Chinese labels are narrow enough that the green event icon
right of 商店 fits inside `LOBBY_TOP_BAR_ROI`: 8 runs, 7 names, `read_page`
returns None, and `ensure_mode` → `press_play` → `ensure_in_match` all refuse
to act. Three attempts, three "top bar unreadable — a dialog may be over it",
with nothing over the bar.

Positional naming also had a worse failure it could never report: a game update
that REORDERS a bar renames every tab after the moved one, silently and
plausibly. Matching glyphs cannot do that — a run either looks like the label
or it does not.

An unmatched run is DROPPED, not guessed at. That is the right answer for the
event icon, and it is also what keeps the icon out of the selection argmax,
which it could otherwise win on brightness alone.
"""
import glob
import os

import cv2
import numpy as np

from config import (LOBBY_SUB_BAR_ROI, LOBBY_TAB_FIND_THRESH, LOBBY_TAB_GAP,
                    LOBBY_TAB_MIN_MARGIN, LOBBY_TAB_MIN_W,
                    LOBBY_TAB_SEL_THRESH, LOBBY_TAB_TMPL_MIN,
                    LOBBY_TAB_TMPL_PAD, LOBBY_TOP_BAR_ROI)
from detector.geometry import segments

# Left to right, as the bars read.
TOP_TABS = ('PLAY', 'PASS', 'CAREER', 'CUSTOMIZE', 'HIDEOUT', 'WORKSHOP',
            'STORE')
SUB_TABS = ('NORMAL', 'RANKED', 'ARCADE', 'TRAINING', 'CUSTOM')

# The only sub tab anything here is allowed to start. See the module docstring.
SAFE_MODE = 'TRAINING'

TAB_TMPL_DIR = os.path.join(os.path.dirname(__file__), '..', 'training_data',
                            'pubg_assets', 'lobby', 'tabs')
TAB_TMPL_MIN = LOBBY_TAB_TMPL_MIN

# Which bar a template belongs to, from its filename: top_PLAY.zh.png.
_TMPL_GLOB = '*.png'


def load_tab_templates(directory=TAB_TMPL_DIR):
    """-> {'top_PLAY': [mask, ...], ...}, every language variant per label.

    Best-of at match time, like `lobby_detector._load_template`: the variants
    are the same label in different UI languages, only one can be on screen,
    so the highest score IS the answer.
    """
    out = {}
    for path in sorted(glob.glob(os.path.join(directory, _TMPL_GLOB))):
        stem = os.path.basename(path).split('.')[0]      # top_PLAY.zh -> top_PLAY
        t = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        # ultralytics swaps cv2.imread for an IMREAD_COLOR wrapper; any process
        # that imported it gets 3 channels back. See detector/CLAUDE.md.
        if t is None:
            continue
        out.setdefault(stem, []).append(t[:, :, 0] if t.ndim == 3 else t)
    return out


_TAB_TMPLS = load_tab_templates()


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


def _run_window(gray, roi, box, pad=LOBBY_TAB_TMPL_PAD):
    """The run's mask, widened, for matchTemplate to slide a label over."""
    y, _, h, _ = roi
    x0 = max(0, box['x0'] - pad)
    x1 = min(gray.shape[1], box['x1'] + pad)
    return ((gray[y:y + h, x0:x1] > LOBBY_TAB_FIND_THRESH) * 255).astype(
        np.uint8)


def _match(win, tmpls):
    best = 0.0
    for t in tmpls:
        if win.shape[0] < t.shape[0] or win.shape[1] < t.shape[1]:
            continue
        best = max(best, float(
            cv2.matchTemplate(win, t, cv2.TM_CCOEFF_NORMED).max()))
    return best


def name_labels(gray, roi, bar, tmpls=None):
    """Find the runs on a bar and name them by template. -> [label, ...]

    Each label gains `name` (None when nothing matched), `tmpl_score` and
    `tmpl_second`. Assignment is greedy over the whole score matrix rather than
    per run: two runs cannot claim the same name, and the stronger claim wins.
    Greedy is enough here because the labels are unrelated glyphs — the runner
    up is an order of magnitude behind, not a near tie (see `--verify`).
    """
    if tmpls is None:
        tmpls = _TAB_TMPLS
    found = bar_labels(gray, roi)
    keys = [k for k in tmpls if k.startswith(f'{bar}_')]
    for s in found:
        s['name'] = None
        s['tmpl_score'] = 0.0
        s['tmpl_best'] = 0.0
        s['tmpl_second'] = 0.0
        s['sel_ink'] = _sel_ink(gray, roi, s)
    if not keys:
        return found

    scores = np.zeros((len(found), len(keys)), dtype=np.float32)
    for i, s in enumerate(found):
        win = _run_window(gray, roi, s)
        for j, k in enumerate(keys):
            scores[i, j] = _match(win, tmpls[k])
        order = np.sort(scores[i])[::-1]
        # `tmpl_best` is what this run reached against ANY label, kept apart
        # from `tmpl_score` (what it was assigned) so a run that matched
        # nothing still reports how close it came to the gate.
        s['tmpl_best'] = float(order[0]) if len(order) else 0.0
        s['tmpl_second'] = float(order[1]) if len(order) > 1 else 0.0

    taken_i, taken_j = set(), set()
    while True:
        best, at = TAB_TMPL_MIN, None
        for i in range(len(found)):
            if i in taken_i:
                continue
            for j in range(len(keys)):
                if j not in taken_j and scores[i, j] >= best:
                    best, at = float(scores[i, j]), (i, j)
        if at is None:
            break
        i, j = at
        taken_i.add(i)
        taken_j.add(j)
        found[i]['name'] = keys[j].split('_', 1)[1]
        found[i]['tmpl_score'] = best
    return found


def read_bar(gray, roi, names, bar):
    """-> (labels, selected_label_or_None, margin_over_runner_up).

    Selection is argmax over the high-threshold ink among the NAMED runs, with
    the margin returned so a caller can refuse to act on a bar that has gone
    ambiguous. A plain ">0" test is not enough — unselected top-bar tabs pick
    up stray ink from decorations near them.

    `selected` is None when any expected name is missing, which is what a
    dialog covering the bar looks like. Do not fall back to a best guess there.
    Extra runs that matched nothing are returned but never selected: the event
    icon is bright enough to win an ink argmax it has no business entering.
    """
    found = name_labels(gray, roi, bar)
    named = [s for s in found if s['name']]
    if len({s['name'] for s in named}) != len(names):
        return found, None, 0.0
    order = sorted(named, key=lambda s: -s['sel_ink'])
    margin = order[0]['sel_ink'] / max(order[1]['sel_ink'], 1)
    return found, order[0], margin


def read_page(gray):
    """Which top-level tab is up. -> (name|None, margin, labels)"""
    found, best, margin = read_bar(gray, LOBBY_TOP_BAR_ROI, TOP_TABS, 'top')
    return (best['name'] if best else None), margin, found


def read_mode(gray):
    """Which mode tab is selected. -> (name|None, margin, labels)

    None means unreadable, NOT "no mode": the sub bar only exists under the
    PLAY page, so check `read_page` first.
    """
    found, best, margin = read_bar(gray, LOBBY_SUB_BAR_ROI, SUB_TABS, 'sub')
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
