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
  occupancy  A PART IS RECOGNISED, or the slot is empty. Nothing else counts.

Verified 28/28 slots over 7 captures with known ground truth (UZI no grip,
Mk12 no stock, G36C no stock, VSS mag+stock only, stripped and fitted M416,
SKS all five).

⚠ OCCUPANCY USED TO BE AN EDGE COUNT and that is what cost 74 guns.

The weapon's own picture is drawn behind the tiles, and on some weapons it
reaches into one. An AKM's magazine fills most of its magazine tile: stripped
bare, that tile still measured 395 Canny edges against a threshold of 120, so
it read `filled` forever. `strip` then pulled a slot that was already empty,
and a gesture at an empty slot reaches the weapon row underneath and throws
the whole gun on the floor (see control/inventory.py unequip). Watched on
screen 2026-08-04 after 74 silent losses across 11 collector runs.

NO EDGE THRESHOLD FIXES THIS. The edges are real and they are a magazine --
they just belong to the gun rather than to an attachment. Nor does a
brightness or background model: the panel is semi-transparent, so the tile
carries scenery too, and the only thing that separates "a part is fitted"
from everything else is RECOGNISING THE PART.

So occupancy is now a template match, and the numbers say it separates
cleanly. Fitted tiles score MSE p50 15.2, p90 40.3, p99 89.2 over 1685
captures; the bare AKM magazine scores 346.6 and the 24 measurable empty
tiles in the corpus run 891..; TAB_SLOT_MATCH_MAX sits in that gap.
(tools/scan_slot_bleed.py --mse, calibration/scan_bare_tiles.py.)

⚠ WHAT THIS GIVES UP, deliberately: a part with NO TEMPLATE now reads `empty`.
This class used to be the reader that a missing template could not touch, and
that is no longer true. The trade is taken because the failure directions are
not comparable -- an unrecognised part reads `empty`, so a gesture is REFUSED
and the part stays on the gun, while the old reader's failure put a gesture on
an empty slot and lost the weapon. A caller that has just fitted something
itself and knows better says so (`known_filled`), which is the channel
calibration/collect_templates.py already used.

⚠ The scope slot draws NO TILE, so its PRESENCE is unanswerable and always
was. It used to report 'unknown' for that reason and nothing could improve on
it. Occupancy no longer goes through the tile, so the position is now read
like any other: a sight matches a template or it does not. 'unknown' survives
only as the answer no caller should turn into 'absent'.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from config import (TAB_SLOT_MATCH_MAX, TAB_SLOT_NO_TILE,
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
                 match_max=TAB_SLOT_MATCH_MAX):
        self.present_min = present_min
        self.match_max = match_max
        self._att = None

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
        """Canny edges inside the tile. NOT the occupancy judgement any more.

        Kept because it is the cheapest description of how busy a tile is and
        every probe and log in the tree prints it — but see the module
        docstring for why it cannot decide whether a part is fitted.
        """
        y, x, h, w = HUD_REGIONS[f'att_{gun}_{slot}']
        return int((cv2.Canny(_gray(frame)[y:y + h, x:x + w], 40, 120) > 0)
                   .sum())

    def fill_match(self, frame, gun, slot, weapon=None):
        """Best template in this tile. -> (name, mse) with mse=inf for none.

        Built on first use, not in __init__: the presence half of this class
        needs no templates at all, and a caller reading only `absent` vs
        `present` should not pay for a bank it never asks.
        """
        if self._att is None:
            from detector.attachment_detector import AttachmentDetector
            self._att = AttachmentDetector()
        y, x, h, w = HUD_REGIONS[f'att_{gun}_{slot}']
        # read_tile carries the `drawn` floor -- the reason 257 of the corpus's
        # 281 empty tiles never reach a template at all -- and `prefer='solved'`,
        # which this used to spell out for itself.
        name, mse, _ = self._att.read_tile(frame[y:y + h, x:x + w], slot, weapon)
        return (name, mse)

    # ── Verdicts ──

    def state(self, frame, gun, slot, weapon=None):
        """'absent' | 'empty' | 'filled' | 'unknown'. Full-screen BGR.

        `weapon` narrows the template bank to what this gun can hold, exactly
        as AttachmentDetector.read_slots narrows it. Without it the whole
        slot's bank is tried, which is looser in one direction only: a part
        the weapon cannot take could be named, never the reverse.
        """
        # THE SCOPE POSITION IS NO LONGER `unknown`. It draws no tile, so
        # PRESENCE is unanswerable there and always was -- but presence is not
        # what a caller aiming a gesture needs, and occupancy no longer goes
        # through the tile at all. A sight either matches a template or it does
        # not, exactly like every other slot.
        #
        # This closes the second half of the gun-loss path. `unequip` lets
        # `unknown` through, because refusing it would have made every sight
        # unremovable -- so an EMPTY scope position took a gesture, and a
        # gesture at an empty slot reaches the weapon row and drops the gun.
        # An unfitted position now reads `empty` and is refused.
        #
        # A weapon whose bank has no candidates for the slot also lands on
        # `empty`. `absent` would be the truer word and nothing here can tell
        # the two apart; `empty` is the one that refuses gestures.
        if slot not in TAB_SLOT_NO_TILE and \
                self.ring_grad(frame, gun, slot) < self.present_min:
            return ABSENT
        _, mse = self.fill_match(frame, gun, slot, weapon)
        return FILLED if mse <= self.match_max else EMPTY

    def classify(self, frame, gun, weapon=None):
        """-> {slot: state} for all five."""
        return {s: self.state(frame, gun, s, weapon) for s in SLOT_NAMES}

    def scores(self, frame, gun, weapon=None):
        """-> {slot: {'ring', 'edges', 'hit', 'mse', 'state'}}. Logs and probes.

        `edges` rides along even though it decides nothing now: every probe in
        the tree prints it, and a tile whose edges are high while no template
        matches is exactly the weapon-render bleed this class stopped trusting.
        """
        out = {}
        for s in SLOT_NAMES:
            hit, mse = self.fill_match(frame, gun, s, weapon)
            out[s] = {'ring': round(self.ring_grad(frame, gun, s), 1),
                      'edges': self.fill_edges(frame, gun, s),
                      'hit': hit, 'mse': mse,
                      'state': self.state(frame, gun, s, weapon)}
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
