"""GunTagDetector — is the boxed slot number 「1」「2」 drawn on this frame?

    pixi run gun-tag          # offline, 38 real frames, labelled by eye

⚠ IT ANSWERS A SHARPER QUESTION THAN "IS THE INVENTORY OPEN", and that is the
whole reason it exists. The tag is painted only when the panel is up AND a gun
occupies that slot, so it is the actual precondition for reading a loadout: an
open panel with an empty rack has nothing worth reading, and every millisecond
spent establishing that it is open was spent on a proxy.

⚠ AND IT SITS INSIDE THE RECTANGLE control/tab_watch.py ALREADY GRABS
(tab_layout.gun_tag_box: x 2216..2259, block x 2219..2848). The anchor it would
replace, HUD_REGIONS['type'], is 1282 px away and costs a second grab of
+4.24 ms — but the cost is not the point. The second grab happens ~4 ms AFTER
the panel one, so today the openness judgement and the loadout come off two
rectangles at two moments. Reading both off one frame makes "the record
describes the object that was measured" true by construction.

THE CRITERION IS WHITE-ON-DARK, JOINTLY, AND NEITHER HALF ALONE SURVIVES.
Every simpler thing has already been refuted by measurement:

    how much white is here      the play log recorded ink 11248 on a frame of
                                bare sky, where a real name plate reads in the
                                hundreds -- the false positive is BIGGER than
                                the true one
    how bright is it            `type`'s own margin is TWO counts: its text
                                clips at 238 and a bright background reaches
                                236
    is it desaturated           overexposed sky is saturation zero as well
    is there detail here        `any_drawn` says yes to grass and timber; its
                                46..173 separation was measured with a panel on
                                screen and means nothing without one

What none of those can fake is a WHITE GLYPH ON A DARK BOX. Sky has the white
and no dark; timber has the dark and no pure white. The test is that both are
present in the same 43x35 rectangle.

MEASURED ON 38 REAL FRAMES, both sides present, labelled by eye off a contact
sheet (calibration/artifacts/gun_tag):

    panel   n=16    white 110..112    median V   29..51
    world   n=22    white   0..  3    median V  112..205

    WHITE_MIN  20   sits between 3 and 110      37x
    DARK_V_MAX 80   sits between 51 and 112     61 counts

Both thresholds land in the middle of an empty gap, and neither class comes
within a factor of three of the line. The 12 paired empty-rack backgrounds in
calibration/artifacts/tab_type agree from the negative side: 0 of 48.

⚠ THE LABELS ARE NOT THE FILENAMES, and that is the point of the corpus.
control/tab_watch.py names each saved frame after what it BELIEVED --
`_closing` means it thought the panel was up -- and on 8 of 38 that belief was
wrong: bare grass and road, saved as a close. Those eight are a STALE `open`
FLAG, which is exactly the failure this detector replaces. Labelling from the
filename would have trained the criterion on the bug it exists to fix.

⚠ ONE GUN, ONE SESSION, ONE MAP. Every panel frame here is the same SKS on the
same range at the same time of day, and slot 2 was empty throughout -- so the
corpus says nothing about a second gun, a different UI scale, or a night map.
The separation is wide enough that this is a question of when it narrows, not
whether it holds today.

"""
import os
import sys

import cv2
import numpy as np

# Run as a module OR as a script -- `pixi run gun-tag` invokes the file
# directly, which puts detector/ on the path and not the repo root, so the
# absolute import below fails before __main__ can fix it. Same shape as
# detector/map_detector.py, which is the other detector with its own selftest.
if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

from detector.tab_items import tab_blocks                      # noqa: E402
from detector.tab_layout import gun_tag_box                    # noqa: E402

# Where control/tab_watch.py's saved crops came from, so the selftest can put
# them back into screen coordinates. Asked of tab_items rather than spelled.
BLOCK_Y, BLOCK_X = tab_blocks()['right'][:2]

# A pixel belongs to the glyph if it is bright AND fully desaturated. Both,
# because either alone is a measured failure -- see the module docstring.
WHITE_S_MAX = 0.06
WHITE_V_MIN = 200

# How many such pixels a drawn digit puts in the box. Real frames put it at
# 110-112 for a drawn 「1」 and 0-3 for bare world, so 20 is well clear of both.
WHITE_MIN = 20

# The box interior is dark: 29-51 with a panel, 112-205 without. 80 sits in the
# gap. It is the half that survives a bright background -- overexposed sky puts
# 700 white pixels in this box (bg11_shut) at median V 104, and only the
# darkness test refuses it.
DARK_V_MAX = 80


class GunTagDetector:
    """Reads the boxed slot numbers off a Tab frame. Stateless."""

    def score(self, frame, slot):
        """-> {'white': int, 'median_v': float, 'drawn': bool}.

        Returns the numbers as well as the verdict because a bare bool cannot
        be argued with. When this says `drawn: False` on a frame the operator
        can see a panel in, the two numbers say which half refused.
        """
        y, x, h, w = gun_tag_box(slot)
        box = frame[y:y + h, x:x + w]
        if box.size == 0:
            return {'white': 0, 'median_v': 0.0, 'drawn': False}
        hsv = cv2.cvtColor(box, cv2.COLOR_BGR2HSV)
        sat = hsv[:, :, 1].astype(np.float32) / 255.0
        val = hsv[:, :, 2]
        white = int(((sat < WHITE_S_MAX) & (val > WHITE_V_MIN)).sum())
        median_v = float(np.median(val))
        return {'white': white, 'median_v': median_v,
                'drawn': white >= WHITE_MIN and median_v <= DARK_V_MAX}

    def drawn(self, frame, slot):
        """Is slot 1 or 2's tag painted? -> bool."""
        return self.score(frame, slot)['drawn']

    def any_drawn(self, frame):
        """Is EITHER tag painted -- i.e. is there a panel with a gun in it?"""
        return any(self.drawn(frame, s) for s in (1, 2))


def _selftest():
    """38 real frames, labelled BY EYE, both sides present.

    ⚠ THE LABELS ARE NOT THE FILENAMES, and that distinction is the corpus.
    control/tab_watch.py names each saved frame after what it BELIEVED --
    `_closing` means it thought the panel was up -- and on 8 of 38 that belief
    was wrong: bare grass and road, saved as a close. A stale `open` flag,
    which is the very thing this detector exists to replace. Labelling from the
    filename would have trained the criterion on the bug.

    So they were labelled from a contact sheet of all 38 thumbnails, one look,
    `panel` iff the SKS and its slot box are visible. That is a human reading
    of the pixels, which is the only kind of ground truth this screen has.

    ⚠ AND THE FRAMES ARE BLOCK CROPS, THREE COLUMNS SHORT. The saved crop
    starts at x=2219 and the tag box at x=2216, so the corpus has 40 of the
    box's 43 columns. The live path grabs the full frame and sees all 43; the
    test is therefore slightly harder than reality, which is the right
    direction for it to be wrong in.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    corpus = os.path.join(root, 'calibration', 'artifacts', 'gun_tag')
    if not os.path.isdir(corpus):
        print(f'  corpus missing: {corpus}')
        return 1
    det = GunTagDetector()
    fails, seen = [], {'panel': [], 'world': []}
    for name in sorted(os.listdir(corpus)):
        if not name.endswith('.png'):
            continue
        label = name.split('__')[0]
        crop = cv2.imread(os.path.join(corpus, name))
        if crop is None or label not in seen:
            fails.append(f'unreadable or unlabelled: {name}')
            continue
        # Paste the block back where it came from, so the detector runs in the
        # screen coordinates every other detector here uses.
        frame = np.zeros((BLOCK_Y + crop.shape[0], BLOCK_X + crop.shape[1], 3),
                         np.uint8)
        frame[BLOCK_Y:, BLOCK_X:] = crop
        seen[label].append((name, det.score(frame, 1)))

    for label, want in (('panel', True), ('world', False)):
        rows = seen[label]
        wrong = [(n, s) for n, s in rows if s['drawn'] != want]
        w = sorted(s['white'] for _, s in rows)
        v = sorted(s['median_v'] for _, s in rows)
        print(f'  {label:5s}  n={len(rows):2d}   white {w[0]:4d}..{w[-1]:4d}   '
              f'median V {v[0]:5.1f}..{v[-1]:5.1f}   wrong {len(wrong)}')
        for n, sc in wrong:
            print(f'           {n}  {sc}')
        if wrong:
            fails.append(f'{len(wrong)} {label} frames misread')
        if not rows:
            fails.append(f'no {label} frames in the corpus')

    # ⚠ BOTH THRESHOLDS ARE CHECKED FOR MARGIN, not just for a verdict. A
    # criterion that passes with the worst case sitting one count from the
    # line passes today and fails on the next patch, and nothing says so.
    pw = [s['white'] for _, s in seen['panel']]
    ww = [s['white'] for _, s in seen['world']]
    pv = [s['median_v'] for _, s in seen['panel']]
    wv = [s['median_v'] for _, s in seen['world']]
    if pw and ww:
        print(f'  white   panel min {min(pw)}  world max {max(ww)}  '
              f'threshold {WHITE_MIN}')
        if not (max(ww) < WHITE_MIN < min(pw)):
            fails.append('WHITE_MIN is not between the two classes')
    if pv and wv:
        print(f'  darkness panel max {max(pv):.0f}  world min {min(wv):.0f}  '
              f'threshold {DARK_V_MAX}')
        if not (max(pv) < DARK_V_MAX < min(wv)):
            fails.append('DARK_V_MAX is not between the two classes')

    print()
    if fails:
        for f in fails:
            print(f'  FAIL  {f}')
        return 1
    print('all ok')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(_selftest())
