"""The wall and the correlator, on THE SAME TWO IMAGES.

    pixi run python tools/probe_holes_vs_correlator.py

WHY THIS PAIR OF FRAMES IS THE WHOLE EXPERIMENT
-----------------------------------------------
`calibration/hole_manual.py` writes a full-screen PNG immediately before the
burst and another after it, with NOTHING in between but the burst and a settle
(no homing, no ADS toggle, no view command). So one pair of images carries both
answers at once:

    the wall        hole k to hole k+1, in px, read off the AFTER frame
    the correlator  phaseCorrelate(before, after) over the tracker's patches

⚠ THAT REMOVES EVERY VARIABLE THE TWO ROUTES USED TO DISAGREE THROUGH. No
clock (no S, no L, no comp_lag, no frame pairing), no accumulation over 15
pairs, no fit, no K on one side and not the other -- the two numbers are in
the same units, from the same pixels, taken at the same two instants.

AND IT ISOLATES ACCUMULATION, WHICH IS THE OPEN QUESTION. The live path chains
~15 correlations and cumsums them; this does ONE. config.py's red_dot block
already measured that the two are not the same thing -- 1.5413 one-pair against
1.6574 many-pairs, 24.4 sigma -- and chose the one-pair number. This asks
whether that difference is what the bullet holes have been complaining about.

⚠ WHAT THIS CANNOT RULE OUT, SAID BEFORE THE NUMBERS. The `after` frame is
taken 3 s after the trigger releases, and PUBG pulls part of the view back down
when firing stops. So a correlation SMALLER than the hole span has two possible
causes -- the correlator under-reading, or the game recovering -- and this
pair of frames cannot separate them. A correlation that MATCHES the hole span
has only one, and that is the outcome worth having.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import config as cfg                                          # noqa: E402
from detector.view_tracker import ViewTracker                  # noqa: E402

MANUAL = os.path.join(ROOT, 'calibration', 'artifacts', 'holes', 'manual')
# The crosshair sits at the screen centre and never moves; hole_manual.py
# aims the VIEW so this point lands on clean concrete. Same constant that
# file calls CENTRE.
CROSSHAIR_Y = 720.0


def patches_of(tracker, frame):
    """Slice the tracker's own regions straight out of a full-screen frame.

    Uses the tracker's own geometry rather than re-deriving it, so this
    measures the patches the live path measures and not a second opinion
    about where they are.

    ⚠ names(), NOT regions(). regions() also carries the reticle box, which is
    a different shape and is never correlated; passing it to measure_pair puts
    that crop through a Hanning window built for the patches. This file did
    exactly that for one run after the reticle was added, and the symptom was
    an empty report rather than a wrong number only by luck.
    """
    out = []
    r = tracker.regions()
    for (y, x, h, w) in (r[n] for n in tracker.names()):
        crop = frame[y:y + h, x:x + w]
        if crop.shape[0] != h or crop.shape[1] != w:
            return None
        out.append(np.ascontiguousarray(crop[:, :, cfg.RECOIL_CHANNEL]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=MANUAL)
    a = ap.parse_args()

    rows = []
    jl = os.path.join(a.dir, 'groups.jsonl')
    if os.path.exists(jl):
        with open(jl, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    # groups.jsonl is keyed (stamp, group); the frames are named the same way,
    # except the first session's files predate the stamped naming.
    by_tag = {}
    for r in rows:
        by_tag[f"{r['stamp']}_g{r['group']}"] = r
        by_tag.setdefault(f"g{r['group']}", r)

    tracker = ViewTracker()
    befores = sorted(f for f in os.listdir(a.dir) if f.endswith('_before.png'))
    if not befores:
        print(f'no *_before.png in {a.dir}')
        return 1

    print(f'{len(befores)} frame pair(s) in {a.dir}\n')
    print(f'{"tag":<20}{"holes px":>10}{"corr px":>10}{"per-patch":>28}'
          f'{"ratio":>8}')
    print('-' * 78)
    keep, corr_of = [], {}
    for bn in befores:
        tag = bn[:-len('_before.png')]
        an = os.path.join(a.dir, f'{tag}_after.png')
        if not os.path.exists(an):
            continue
        before = cv2.imread(os.path.join(a.dir, bn), cv2.IMREAD_COLOR)
        after = cv2.imread(an, cv2.IMREAD_COLOR)
        if before is None or after is None:
            print(f'{tag:<20}  unreadable')
            continue
        pb, pa = patches_of(tracker, before), patches_of(tracker, after)
        if pb is None or pa is None:
            print(f'{tag:<20}  frame smaller than the patch band')
            continue
        # predicted_dy=0 only feeds the out-of-range flag; it does not steer
        # the measurement.
        m = tracker.measure_pair(pb, pa, 0.0)
        corr_of[tag] = m.dy
        rec = by_tag.get(tag)
        marks = (rec or {}).get('marks') or []
        # Holes climb the wall as the view rotates up, so the span is the
        # FIRST mark's y minus the LAST -- and it is only meaningful when the
        # count of marks equals the count of rounds that actually went out.
        span = (marks[0][1] - marks[-1][1]) if len(marks) >= 2 else float('nan')
        fired = (rec or {}).get('fired')
        pp = ' '.join(f'{v:+6.1f}' for v in m.per_patch_dy)
        ratio = (span / m.dy) if np.isfinite(m.dy) and abs(m.dy) > 1e-6 else float('nan')
        flag = ''
        if fired is None or fired <= 0:
            flag = '  <- counter says no rounds went out'
        elif len(marks) != fired:
            flag = f'  <- {len(marks)} marks for {fired} rounds'
        else:
            keep.append((tag, span, m.dy, ratio))
        print(f'{tag:<20}{span:10.1f}{m.dy:10.2f}  {pp}{ratio:8.2f}{flag}')

    print('\n⚠ ONLY THE ROWS WHERE THE MARK COUNT EQUALS THE ROUND COUNT ARE '
          'EVIDENCE.\n  The others have a hole the detector invented or missed, '
          'and their span\n  describes a different number of rounds than the '
          'correlation does.')

    # ── the check that separates "the correlator under-reads" from "the view
    # came back down", and it costs nothing extra ──
    #
    # Round 1 leaves at t~0, before the view has moved, so hole 1 is the wall
    # point the crosshair was on in the BEFORE frame -- screen (1720, 720).
    # If the view then rotated up by `corr` px, that same wall point appears
    # in the AFTER frame at
    #
    #     y = 720 + corr
    #
    # ⚠ THIS IS AN ABSOLUTE PREDICTION AND THE CORRELATOR CANNOT ARRANGE IT.
    # It never sees the crosshair, never sees a hole, and the number it
    # produced came from seven patches 300+ px away on both sides. Nothing
    # about the hole positions was available to it.
    print('\nWHERE HOLE 1 SHOULD BE IF THE CORRELATION IS RIGHT')
    print('  round 1 leaves before the view moves, so hole 1 marks the wall '
          'point that\n  sat under the crosshair (y=720) in the BEFORE frame. '
          'It must appear at\n  y = 720 + corr in the AFTER frame.\n')
    cy = CROSSHAIR_Y
    for bn in befores:
        tag = bn[:-len('_before.png')]
        rec, corr = by_tag.get(tag), corr_of.get(tag)
        if rec is None or corr is None or not np.isfinite(corr):
            continue
        marks = rec.get('marks') or []
        if not marks:
            continue
        pred = cy + corr
        # marks[0] is the LOWEST hole on the wall, i.e. the earliest round.
        # No selection is applied -- picking the nearest mark would be
        # circular, so the first one is taken and the miss is printed as it
        # falls.
        got = marks[0][1]
        n_exp = rec.get('fired')
        clean = (n_exp is not None and len(marks) == n_exp)
        print(f'  {tag:<20} predicted {pred:7.2f}   marks[0] {got:7.2f}   '
              f'miss {got - pred:+6.2f} px'
              f'{"   <- no mark selection needed" if clean else "   (marks do not match the round count)"}')

    if not keep:
        print('\n  ...and there are none. Nothing here is evidence yet.')
        return 0

    print(f'\nCLEAN GROUPS ({len(keep)})')
    for tag, span, dy, ratio in keep:
        print(f'  {tag:<20} wall {span:7.1f} px   correlator {dy:7.2f} px   '
              f'wall/corr {ratio:5.2f}x')
    r = np.array([k[3] for k in keep], dtype=float)
    print(f'\n  wall / correlator   mean {r.mean():.2f}x   '
          f'range {r.min():.2f}..{r.max():.2f}')
    print('\n  1.0  -> the correlator has the scale right over a whole burst, '
          'and the\n         3x lives in the per-frame chain or in the fit, '
          'not in the optics.'
          '\n  ~3   -> ONE correlation under-reads too, so the chain is '
          'innocent -- but\n         see the docstring: view recovery over the '
          '3 s settle is not excluded.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
