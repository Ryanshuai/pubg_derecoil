"""Attachment slots: absent, empty, or filled — read off the Tab screen.

    from detector.slot_detector import SlotDetector
    slots = SlotDetector()
    slots.classify(frame, 2)     # {'scope': 'unknown', 'grip': 'absent', ...}
    slots.state(frame, 2, 'grip')

Answers what a weapon CAN take, not what is on it. Which attachment sits in a
filled slot is AttachmentDetector's job; this one only says whether there is a
slot and whether anything is in it. The split matters because the two use
opposite crops — see `tab_layout` on the inner/outer windows.

Why it exists: dragging a part onto a slot the weapon does not have drops the
item, and "this gun has no such slot" looks exactly like "the part was
rejected" if you only watch the mouse. `attachment_catalog.SLOTS` is supposed
to prevent that, and its entries are wiki readings and guesses — 0 measured
before this. One spawn plus one screenshot per weapon replaces a drag matrix.

Geometry and thresholds are in tab_layout / config; this is the detector
wrapper over them, so callers get the same shape as SpawnerDetector and
AdsDetector rather than a bag of functions.

THREE STATES, TWO JUDGEMENTS, AND THEY LOOK AT DIFFERENT PIXELS:

  presence   Sobel p90 on the tile's BORDER RING. Nothing from the interior:
             the interior holds an icon, which says nothing about whether the
             tile exists and would make the answer depend on what is fitted.
             absent 5.0..26.0, present 46.0..172.7, threshold 36.
  occupancy  Canny edges INSIDE the tile. Empty 0..71, filled 202..885.

Verified 28/28 slots over 7 captures with known ground truth (UZI no grip,
Mk12 no stock, G36C no stock, VSS mag+stock only, stripped and fitted M416,
SKS all five).

⚠ The scope slot is always 'unknown' and no threshold changes that: it draws
no tile at all. Details in config.py. Never let 'unknown' become 'absent'.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from config import (TAB_SLOT_FILLED_EDGES, TAB_SLOT_NO_TILE,
                    TAB_SLOT_PRESENT_MIN, TAB_SLOT_RING_HALF,
                    TAB_SLOT_RING_PAD)
from config import HUD_REGIONS
from detector.tab_layout import slot_tile_box, SLOT_NAMES

ABSENT, EMPTY, FILLED, UNKNOWN = 'absent', 'empty', 'filled', 'unknown'


def _gray(frame):
    return (cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3
            else frame)


class SlotDetector:
    """Per-slot check: does this weapon have the slot, and is it filled?"""

    def __init__(self, present_min=TAB_SLOT_PRESENT_MIN,
                 filled_edges=TAB_SLOT_FILLED_EDGES):
        self.present_min = present_min
        self.filled_edges = filled_edges

    # ── The two measurements ──

    def ring_grad(self, frame, gun, slot):
        """Gradient along the tile's border ring. Presence, not contents.

        Sobel magnitude rather than Canny. Canny's fixed hysteresis returned
        exactly 0 on the VSS magazine — a real slot whose tile sits on bright
        sand at almost its own brightness. The border is there, just
        low-contrast, and hysteresis quantises it away; Sobel keeps it at 46.
        A judgement that reads a hard zero on a present element is not
        conservative, it is broken.
        """
        ty, tx, t, _ = slot_tile_box(gun, slot)
        p, half = TAB_SLOT_RING_PAD, TAB_SLOT_RING_HALF
        g = _gray(frame)
        if ty - p < 0 or tx - p < 0 or ty + t + p > g.shape[0] or \
                tx + t + p > g.shape[1]:
            return 0.0

        win = cv2.GaussianBlur(
            g[ty - p:ty + t + p, tx - p:tx + t + p].astype(np.float32),
            (3, 3), 0)
        mag = np.hypot(cv2.Sobel(win, cv2.CV_32F, 1, 0, ksize=3),
                       cv2.Sobel(win, cv2.CV_32F, 0, 1, ksize=3))

        H, W = win.shape
        a, b = p - half, p + half
        m = np.zeros(win.shape, bool)
        m[a:H - a, a:b + 1] = True             # left
        m[a:H - a, W - b - 1:W - a] = True     # right
        m[a:b + 1, a:W - a] = True             # top
        m[H - b - 1:H - a, a:W - a] = True     # bottom
        return float(np.percentile(mag[m], 90))

    def fill_edges(self, frame, gun, slot):
        """Canny edges inside the tile. Contents, not presence."""
        y, x, h, w = HUD_REGIONS[f'att_{gun}_{slot}']
        return int((cv2.Canny(_gray(frame)[y:y + h, x:x + w], 40, 120) > 0)
                   .sum())

    # ── Verdicts ──

    def state(self, frame, gun, slot):
        """'absent' | 'empty' | 'filled' | 'unknown'. Full-screen BGR."""
        if slot in TAB_SLOT_NO_TILE:
            return UNKNOWN
        if self.ring_grad(frame, gun, slot) < self.present_min:
            return ABSENT
        return (FILLED if self.fill_edges(frame, gun, slot) >= self.filled_edges
                else EMPTY)

    def classify(self, frame, gun):
        """-> {slot: state} for all five."""
        return {s: self.state(frame, gun, s) for s in SLOT_NAMES}

    def scores(self, frame, gun):
        """-> {slot: {'ring', 'edges', 'state'}}. For logs and probes."""
        out = {}
        for s in SLOT_NAMES:
            out[s] = {'ring': round(self.ring_grad(frame, gun, s), 1),
                      'edges': self.fill_edges(frame, gun, s),
                      'state': self.state(frame, gun, s)}
        return out

    def present(self, frame, gun):
        """Slots this weapon has. 'unknown' is NOT included — it is not a
        claim of absence, and a caller wanting to drag must resolve it by
        fitting something (see the calibrate-compat skill)."""
        return {s for s, v in self.classify(frame, gun).items()
                if v in (EMPTY, FILLED)}


def main():
    import argparse
    ap = argparse.ArgumentParser(description='Read slot states off a capture.')
    ap.add_argument('shot')
    ap.add_argument('--gun', type=int, default=2, choices=(1, 2))
    args = ap.parse_args()

    frame = cv2.imread(args.shot)
    if frame is None:
        print(f'cannot read {args.shot}')
        return 1
    det = SlotDetector()
    print(f'{args.shot}  weapon slot {args.gun}')
    for s, v in det.scores(frame, args.gun).items():
        print(f'  {s:10} {v["state"]:8} ring {v["ring"]:7.1f}  '
              f'edges {v["edges"]:4d}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
