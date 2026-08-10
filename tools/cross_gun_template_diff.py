"""Solve the same attachment on two different guns and diff the two icons.

    pixi run cross-gun
    pixi run cross-gun --save        # also write per-part diff images

WHAT THIS IS FOR. `solve_template` recovers an icon from paired captures:

    c = a*icon + (1-a)*backdrop        per pixel, per channel
    (1-a) = SUM(dc . db) / SUM(db . db)

A pixel the scene never moved behind has db = 0 and carries NO information
about transparency. Two kinds of pixel are like that, and only one of them is
handled:

    the SCENE behind the panel   moves with the view, so a sawtooth sweep
                                 (sky/ground) makes db large -- solvable
    the GUN's own hardware       does not move with the view at ANY pitch.
                                 db stays 0 forever, so no sweep separates it

The second kind is invisible to every check that looks at one gun's captures,
because within one gun it is perfectly consistent -- a stable, confident,
WRONG part of the template. The only thing that changes it is a different host
gun. So: solve the part twice, once per gun, and look at where they disagree.

    agree everywhere        the template is the attachment
    disagree at the edges   the edge pixels are the GUN, not the part

⚠ THIS IS A MEASUREMENT, NOT A FIX. It says which pixels are suspect; it does
not clean them. Nothing here writes into the template bank.

⚠ A DISAGREEMENT IS NOT AUTOMATICALLY THE GUN. Two solves also differ from
noise, from a different number of backgrounds, and from a worse conditioned
sweep. So the per-part reconstruction error is printed beside the diff: a part
whose two solves disagree by less than their own recon error has not shown
anything.
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

import calibration.legacy_score_attachments as sa
from calibration.capture_run import CaptureRun, _run_dirs
from calibration.legacy_collect_templates import gun_of

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'calibration', 'artifacts', 'debug', 'cross_gun')


def per_run_solves():
    """-> {key: [(gun, bgra, recon_err, n_pairs), ...]}, one entry per run."""
    out = collections.defaultdict(list)
    for _kind, _stamp, d in _run_dirs():
        if not os.path.exists(os.path.join(d, 'manifest.json')):
            continue
        run = CaptureRun.load_dir(d)
        # Which gun each key was worn on in THIS run. `slots` entries only --
        # a backdrop crop is the same tile with nothing in it.
        guns, pairs = {}, collections.Counter()
        for e in run.entries:
            if e.get('target') != 'slots' or e.get('angle') is None:
                continue
            g = gun_of(e)
            if g:
                guns.setdefault(e['key'], set()).add(g)
                pairs[e['key']] += 1
        try:
            solved = sa.solved_icons(d)
        except Exception:
            continue
        for key, (bgra, err) in solved.items():
            hosts = guns.get(key, set())
            # One host per (run, key) is what the planner produces. More than
            # one means this run wore the part on two guns and the solve mixed
            # them, which is not a per-gun solve and must not be reported as
            # one.
            if len(hosts) != 1:
                continue
            out[key].append((hosts.pop(), bgra, err, pairs[key]))
    return out


def edge_mask(alpha):
    """Pixels on the boundary of the opaque region. -> bool array

    The claim under test is about EDGES, so the split has to be made without
    looking at the disagreement being measured -- else it is drawn around
    whatever was found. Dilate minus erode of the alpha support, 1px each way.
    """
    solid = (alpha > 32).astype(np.uint8)
    k = np.ones((3, 3), np.uint8)
    return (cv2.dilate(solid, k) - cv2.erode(solid, k)).astype(bool)


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--save', action='store_true',
                    help=f'write diff images under {os.path.relpath(OUT)}')
    args = ap.parse_args()

    solves = per_run_solves()
    multi = {k: v for k, v in solves.items()
             if len({g for g, _b, _e, _n in v}) >= 2}
    print(f'{len(solves)} parts solved, {len(multi)} of them on 2+ guns\n')
    if not multi:
        print('nothing to compare: every part is solved on a single gun.\n'
              'collect_templates --spread 2 is what produces the second one.')
        return 1

    if args.save:
        os.makedirs(OUT, exist_ok=True)

    # ⚠ THE PAIR COUNTS ARE NOT DECORATION. The solve is a least squares over
    # every unordered pair of backgrounds, so 10 captures give 45 equations per
    # pixel and 2 give 1. A two-pair solve is one particular frame's
    # antialiasing written straight into the template, and comparing it against
    # a ten-pair solve reports a disagreement that is entirely the thin side's
    # noise -- which reads exactly like the edge contamination being hunted.
    # Without this column the first run of this tool called scope_4x (5 pairs)
    # and quickext_ar (3 pairs) edge cases when their MIDDLES disagreed too.
    print(f'{"part":15} {"h":>2} {"gun A":9} {"nA":>3} {"gun B":9} {"nB":>3} '
          f'{"|dA|":>6} {"edge":>6} {"mid":>6} {"recon":>6}  verdict')
    for key in sorted(multi):
        # One solve per gun: the best-conditioned (lowest recon error) each.
        best = {}
        for gun, bgra, err, n in multi[key]:
            if gun not in best or err < best[gun][1]:
                best[gun] = (bgra, err, n)
        # ⚠ EVERY PAIR OF HOSTS, not the first two alphabetically. With three
        # or more guns the alphabetical pair can be the two that agree, and the
        # part then reads clean while a third host it will actually be matched
        # against sits 50 grey levels away. Reported on the WORST pair, because
        # a template ships once and has to survive its furthest host.
        usable = [(g, b, e, n) for g, (b, e, n) in sorted(best.items())]
        shapes = {b.shape for _g, b, _e, _n in usable}
        if len(shapes) > 1:
            print(f'{key:15} {len(usable)} hosts, shapes differ')
            continue
        worst = None
        for i in range(len(usable)):
            for j in range(i + 1, len(usable)):
                gi, bi, ei, ni = usable[i]
                gj, bj, ej, nj = usable[j]
                d = float(np.abs(bi[:, :, 3].astype(np.float32)
                                 - bj[:, :, 3].astype(np.float32)).mean())
                if worst is None or d > worst[0]:
                    worst = (d, (gi, bi, ei, ni), (gj, bj, ej, nj))
        (_d, (ga, ba, ea, na), (gb, bb, eb, nb)) = worst
        aa, ab = ba[:, :, 3].astype(np.float32), bb[:, :, 3].astype(np.float32)
        d_alpha = np.abs(aa - ab)
        # The union of the two supports: a pixel one solve calls opaque and the
        # other calls transparent is exactly the disagreement being hunted, and
        # intersecting would throw it away.
        edge = edge_mask(aa) | edge_mask(ab)
        mid = ((aa > 32) | (ab > 32)) & ~edge
        e_val = float(d_alpha[edge].mean()) if edge.any() else 0.0
        m_val = float(d_alpha[mid].mean()) if mid.any() else 0.0
        recon = max(ea, eb)
        # An edge that disagrees more than the middle AND more than the solves'
        # own reconstruction error. Either test alone is not enough: edges
        # always carry more antialiasing noise than flat interiors.
        hot = e_val > m_val * 1.5 and e_val > recon
        # A solve standing on fewer than this many captures is one frame's
        # antialiasing, not a measurement of the part -- see the header above
        # the table. Flagged rather than dropped: the row still says what it
        # says, it just may not be about the gun.
        thin = '  THIN' if min(na, nb) < 6 else ''
        print(f'{key:15} {len(usable):2d} {ga:9} {na:3d} {gb:9} {nb:3d} '
              f'{d_alpha.mean():6.1f} '
              f'{e_val:6.1f} {m_val:6.1f} {recon:6.2f}  '
              f'{"EDGE DISAGREES" if hot else "" if e_val > recon else "within recon error"}'
              f'{thin}')
        if args.save:
            vis = np.hstack([
                cv2.cvtColor(ba[:, :, 3], cv2.COLOR_GRAY2BGR),
                cv2.cvtColor(bb[:, :, 3], cv2.COLOR_GRAY2BGR),
                cv2.applyColorMap(np.clip(d_alpha * 3, 0, 255).astype(np.uint8),
                                  cv2.COLORMAP_INFERNO)])
            cv2.imwrite(os.path.join(OUT, f'{key}__{ga}_vs_{gb}.png'),
                        cv2.resize(vis, None, fx=4, fy=4,
                                   interpolation=cv2.INTER_NEAREST))
    if args.save:
        print(f'\nalpha(A) | alpha(B) | |difference| -> {os.path.relpath(OUT)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
