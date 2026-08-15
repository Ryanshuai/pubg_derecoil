"""Where on this frame can a burst leave readable bullet holes?

Bullet holes are the ONE measurement outside the calibration loop's own chain
(calibration/probe_hole_pattern.py argues that at length). Taking them needs a
flat surface that records holes, and the training range has exactly one shape
of it: poured concrete. Everything else in view is sand, sky, rock, corrugated
steel, or UI -- and a burst fired at any of those leaves nothing, which looks
EXACTLY like perfect compensation.

That is the whole reason this is a module and not a look:

    an empty diff and a perfect group are the same picture

So the question "is there a wall here" has to be answered before firing, by
something that can say NO, and it has to be answerable again next session
without anybody remembering what last session looked like.

WHAT IT IS NOT
--------------
⚠ IT DOES NOT CERTIFY THAT HOLES WILL APPEAR. It reads colour, brightness and
local flatness -- a painted flat surface that does not take decals reads the
same as one that does. The second, physical witness is a TEST ROUND: fire one,
diff the frame, and require a mark within a few px of the predicted point.
`confirm_shot()` is that check, and it belongs to whoever fires, not here.
Pixels propose; the round disposes.

⚠ IT DOES NOT KNOW ABOUT THE PLAYER'S OWN BODY. In third person the character
occludes the lower centre; in ADS it does not. The body is dark and textured,
so the flatness test usually drops it, but that is a consequence, not a
guarantee -- which is why `survey()` returns REGIONS and a caller picks one,
rather than returning a single answer to be trusted.

THE CRITERION, and what each part is keeping out
------------------------------------------------
    neutral    |R-B| and |R-G| small     sand and rust are warm, sky is blue
    mid-bright 105 < gray < 215          sky/sun blows past, shadow falls short
    flat       local sd below a floor    doors, posters, windows, grass, UI
    big        an inscribed rect that fits a burst's climb plus margin

⚠ AND IT REPORTS THE LOSING SIDE. `survey()` carries `rejected`: the largest
blob that failed, and WHICH test failed it. A gate that only reports what it
accepted cannot be told apart from one that accepts everything -- this
repository has three separate instances of that and they all read as green.
"""
from dataclasses import dataclass, field

import cv2
import numpy as np

from config import SCREEN_H, SCREEN_W

# Concrete against everything else in the training range. Measured on
# calibration/artifacts/holes/at_wall_*.png; the losing side is reported by
# survey() so these can be re-tuned against evidence rather than by feel.
NEUTRAL_RB = 26          # |R - B|, sand/rust run warm, sky runs cold
NEUTRAL_RG = 22          # |R - G|
GRAY_LO, GRAY_HI = 100, 220
FLAT_WIN = 21            # local sd window
FLAT_SD = 15.0           # doors, posters and grass all sit well above this
MIN_AREA = 40_000        # a blob smaller than this cannot hold a burst

# The HUD is drawn over the world and is neither wall nor not-wall -- it is
# simply not a surface. Blanked rather than tested, because a UI panel is flat
# and neutral and would otherwise pass every test in the list.
HUD_BLANK = (
    (0, 0, SCREEN_W, 60),                    # top compass + net stats
    (0, SCREEN_H - 170, SCREEN_W, 170),      # bottom bar: ammo, posture, weapon
    (SCREEN_W - 700, 300, 700, SCREEN_H),    # right-hand key list + minimap
)


@dataclass
class Survey:
    """What the frame offers, and what it refused."""
    regions: list = field(default_factory=list)   # (x, y, w, h), largest first
    best: tuple = None                            # the region a burst should use
    aim: tuple = None                             # where to put the crosshair
    ok: bool = False
    why: str = ''
    rejected: list = field(default_factory=list)  # (area, which test, stats)
    coverage: float = 0.0                         # fraction of the frame that passed


class WallDetector:
    """Finds flat concrete big enough to record a burst."""

    def __init__(self, climb_px=145, margin_px=60):
        """`climb_px` is how far the group walks UP from the aim point.

        Default is one aug 6-round burst uncompensated (94 counts x K=1.54).
        It is a PARAMETER because it is a property of the burst, not of the
        wall: a 3-round burst needs a third of it, and a compensated one needs
        almost none. A detector that hard-coded one burst length would quietly
        reject perfectly good walls for the other.
        """
        self.climb_px = climb_px
        self.margin_px = margin_px

    # ── the three pixel tests, kept separate so a refusal can name one ──

    def _masks(self, frame):
        b, g, r = (c.astype(np.int16) for c in cv2.split(frame))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        neutral = (np.abs(r - b) < NEUTRAL_RB) & (np.abs(r - g) < NEUTRAL_RG)
        mid = (gray > GRAY_LO) & (gray < GRAY_HI)
        f = gray.astype(np.float32)
        mean = cv2.blur(f, (FLAT_WIN, FLAT_WIN))
        sq = cv2.blur(f * f, (FLAT_WIN, FLAT_WIN))
        flat = np.sqrt(np.maximum(sq - mean * mean, 0)) < FLAT_SD
        return neutral, mid, flat

    @staticmethod
    def _blank_hud(mask):
        for x, y, w, h in HUD_BLANK:
            mask[max(0, y):y + h, max(0, x):x + w] = 0
        return mask

    @staticmethod
    def _inscribed(mask, x, y, w, h):
        """Largest axis-aligned all-true rectangle inside one blob's bbox.

        Histogram method, O(w*h). The bounding box of a blob is NOT the usable
        area -- a wall with a door punched through it has a bbox covering the
        door -- and firing at a bbox is how a burst ends up in a doorway.
        """
        sub = mask[y:y + h, x:x + w].astype(np.int32)
        best = (0, None)
        heights = np.zeros(w, dtype=np.int32)
        for row in range(h):
            heights = np.where(sub[row] > 0, heights + 1, 0)
            stack = []
            for i in range(w + 1):
                cur = heights[i] if i < w else 0
                start = i
                while stack and stack[-1][1] >= cur:
                    s, hh = stack.pop()
                    area = hh * (i - s)
                    if area > best[0]:
                        best = (area, (x + s, y + row - hh + 1, i - s, hh))
                    start = s
                stack.append((start, cur))
        return best[1]

    def survey(self, frame):
        """-> Survey. Never raises on a frame; refuses instead."""
        if frame is None or frame.ndim != 3:
            return Survey(why='no frame')
        neutral, mid, flat = self._masks(frame)
        need_h = self.climb_px + 2 * self.margin_px
        need_w = 2 * self.margin_px

        mask = self._blank_hud((neutral & mid & flat).astype(np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)

        s = Survey(coverage=float(mask.mean()))
        order = sorted(range(1, n), key=lambda i: -stats[i, 4])
        for i in order[:12]:
            x, y, w, h, area = stats[i]
            if area < MIN_AREA:
                s.rejected.append((int(area), 'too small', (int(x), int(y))))
                continue
            rect = self._inscribed((lab == i).astype(np.uint8), x, y, w, h)
            if rect is None or rect[2] < need_w or rect[3] < need_h:
                got = (rect[2], rect[3]) if rect else (0, 0)
                s.rejected.append((int(area), f'inscribed {got[0]}x{got[1]} '
                                               f'< {need_w}x{need_h}',
                                   (int(x), int(y))))
                continue
            s.regions.append(tuple(int(v) for v in rect))

        if not s.regions:
            biggest = s.rejected[0] if s.rejected else None
            s.why = ('no flat concrete big enough for a '
                     f'{self.climb_px} px climb'
                     + (f'; largest candidate {biggest[0]} px failed: '
                        f'{biggest[1]}' if biggest else '; nothing neutral and '
                                                       'flat at all'))
            return s

        s.regions.sort(key=lambda r: -(r[2] * r[3]))
        s.best = s.regions[0]
        bx, by, bw, bh = s.best
        # Aim LOW in the region: the group walks up, so the headroom has to be
        # above the crosshair, not around it.
        s.aim = (bx + bw // 2, by + bh - self.margin_px)
        s.ok = True
        s.why = (f'{len(s.regions)} region(s); best {bw}x{bh} at ({bx},{by}), '
                 f'aim {s.aim}')
        return s

    def at(self, frame, point, half=130):
        """Is the surface UNDER `point` good enough to record a group?

        ⚠ THIS IS THE QUESTION survey() SHOULD HAVE BEEN ASKING. survey() finds
        the best wall on screen and hands back an aim point, which makes the
        caller TURN to reach it -- and on 2026-08-11 that produced a 553-count
        swing (~1300 px) to reach a region at the screen's left edge, which
        swung the whole wall out of view. "Where is the best wall" and "is this
        wall good enough" are different questions, and only the second one can
        be answered without moving.

        -> (ok, fraction of the box that is wall, why)
        """
        if frame is None:
            return False, 0.0, 'no frame'
        # ⚠ THE BOX IS ABOVE THE POINT, NOT AROUND IT, and the first version
        # was around it: 0.71 of a 260x295 centred box was concrete and it
        # refused a perfectly good wall, because the bottom of that box held
        # the player's own head and its right edge caught a door. Neither is
        # where the group goes. A group WALKS UP from the crosshair, so the
        # surface that has to be clean is the strip above it -- one round's
        # worth of margin below, the whole climb above.
        x0 = max(0, point[0] - half)
        x1 = min(SCREEN_W, point[0] + half)
        y0 = max(0, point[1] - self.climb_px - half // 2)
        y1 = min(SCREEN_H, point[1] + half // 3)
        neutral, mid, flat = self._masks(frame)
        m = self._blank_hud((neutral & mid & flat).astype(np.uint8))
        box = m[y0:y1, x0:x1]
        if box.size == 0:
            return False, 0.0, 'box off screen'
        frac = float(box.mean())
        # 0.80 rather than 1.0: a real slab carries stains, seams and bolts,
        # and demanding a spotless box refuses the wall this was written for.
        ok = frac >= 0.80
        return ok, frac, (f'{frac:.2f} of the {x1 - x0}x{y1 - y0} box under '
                          f'{point} is flat concrete'
                          + ('' if ok else ' — below 0.80'))

    def annotate(self, frame, s):
        """A picture a human can disagree with. Always produced, pass or fail."""
        vis = frame.copy()
        for x, y, w, h in HUD_BLANK:
            cv2.rectangle(vis, (x, y), (x + w, y + h), (60, 60, 60), 2)
        for r in s.regions:
            cv2.rectangle(vis, (r[0], r[1]), (r[0] + r[2], r[1] + r[3]),
                          (0, 200, 0), 3)
        if s.best:
            x, y, w, h = s.best
            cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 255), 5)
        if s.aim:
            cv2.drawMarker(vis, s.aim, (0, 0, 255), cv2.MARKER_CROSS, 60, 4)
            cv2.arrowedLine(vis, s.aim,
                            (s.aim[0], s.aim[1] - self.climb_px),
                            (0, 0, 255), 3, tipLength=0.25)
        cv2.putText(vis, ('OK  ' if s.ok else 'REFUSED  ') + s.why,
                    (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (0, 255, 0) if s.ok else (0, 0, 255), 3)
        return vis


def find_holes(after, before=None, near=None, box=(320, 260), rel_dark=0.45,
               area=(6, 120), max_side=16, aspect=(0.45, 2.2), min_drop=25,
               spread_px=70):
    """Bullet holes in `after`, lowest first. Two stages, and the order matters.

    STAGE 1 -- CANDIDATES, from the after frame alone. A hole core is nearly
    black on grey concrete: MEASURED on the first real group, wall median 168
    and hole cores under 76. The threshold is RELATIVE to that median, not an
    absolute grey, because the same wall in shade would fail an absolute one.
    Round, and small: 6-120 px, no side over 16, aspect within about 2.

    STAGE 2 -- NOVELTY, against the before frame. Stage 1 alone returned 9
    blobs for a 3-round group: the poster's edge and two wall stains are dark,
    round and small, and no amount of shape tuning separates them because they
    are genuinely the same shape. What separates them is that THEY WERE ALREADY
    THERE. A whole-frame diff cannot do this -- it drowns in the player's own
    head moving between frames, which is exactly what produced 3850 "holes" on
    the first attempt. Diffing only INSIDE a candidate disc is precise: a few
    hundred px, all of them on wall.

    ⚠ WITHOUT `before` IT RETURNS CANDIDATES, NOT HOLES, and says so by way of
    the flag on each row. A caller that treats stage 1 as an answer is reading
    the wall's own features as a group.

    -> [(x, y, area, is_new)], sorted by y DESCENDING -- the group walks UP, so
    the first round is the LOWEST hole.
    """
    h, w = after.shape[:2]
    cx, cy = near if near else (w // 2, h // 2)
    bw, bh = box
    x0, y0 = max(0, cx - bw // 2), max(0, cy - bh // 2)
    x1, y1 = min(w, cx + bw // 2), min(h, cy + bh // 2)
    a = cv2.cvtColor(after[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    med = float(np.median(a))
    m = (a < med * rel_dark).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, _lab, st, ce = cv2.connectedComponentsWithStats(m, 8)
    b = (cv2.cvtColor(before[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.int16)
         if before is not None else None)
    rows = []
    for i in range(1, n):
        ar, ww, hh = st[i, 4], st[i, 2], st[i, 3]
        if not (area[0] <= ar <= area[1]) or max(ww, hh) > max_side:
            continue
        if hh == 0 or not (aspect[0] <= ww / hh <= aspect[1]):
            continue
        px, py = ce[i][0], ce[i][1]
        is_new = True
        if b is not None:
            r = 4
            yy0, yy1 = max(0, int(py) - r), min(b.shape[0], int(py) + r + 1)
            xx0, xx1 = max(0, int(px) - r), min(b.shape[1], int(px) + r + 1)
            drop = float(b[yy0:yy1, xx0:xx1].min()) - float(a[yy0:yy1,
                                                              xx0:xx1].min())
            is_new = drop >= min_drop
        rows.append((x0 + px, y0 + py, int(ar), bool(is_new)))
    # A group is TIGHT: the rounds leave from one muzzle at one yaw, so the
    # holes share an x within a few tens of px. Keeping the widest x-cluster
    # drops a stain that survived both stages by being far to the side.
    new = [r for r in rows if r[3]]
    if len(new) >= 2:
        xs = sorted(r[0] for r in new)
        best, bestn = xs[0], 0
        for anchor in xs:
            k = sum(1 for x in xs if abs(x - anchor) <= spread_px)
            if k > bestn:
                best, bestn = anchor, k
        new = [r for r in new if abs(r[0] - best) <= spread_px]
    new.sort(key=lambda r: -r[1])
    return new


def confirm_shot(before, after, predicted, tol_px=80, min_pixels=12):
    """THE SECOND SOURCE: did a round actually mark the surface where expected?

    survey() reads pixels and can be fooled by any flat grey thing. This reads
    the consequence of firing, which nothing can fake: a mark appeared, and it
    appeared near where the crosshair was. Both halves matter -- "something
    changed" is satisfied by a cloud drifting, and this repository has already
    reported 1385 moving-cloud marks as a bullet group.

    -> (ok, n_pixels, centroid or None)
    """
    if before is None or after is None or before.shape != after.shape:
        return False, 0, None
    d = cv2.absdiff(before, after).max(axis=2)
    m = (d > 28).astype(np.uint8)
    for x, y, w, h in HUD_BLANK:
        m[max(0, y):y + h, max(0, x):x + w] = 0
    x0, y0 = max(0, predicted[0] - tol_px), max(0, predicted[1] - tol_px)
    win = m[y0:predicted[1] + tol_px, x0:predicted[0] + tol_px]
    n = int(win.sum())
    if n < min_pixels:
        return False, n, None
    ys, xs = win.nonzero()
    return True, n, (int(xs.mean()) + x0, int(ys.mean()) + y0)
