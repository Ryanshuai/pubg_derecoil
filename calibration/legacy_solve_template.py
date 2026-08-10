"""Solve an attachment's icon and alpha out of paired captures. Offline.

⚠ LEGACY (2026-08-09). Superseded by calibration/collect_intersect.py, which
photographs a part on TWO RACKED GUNS AT ONCE and intersects the crops instead
of solving `c = a*icon + (1-a)*backdrop` from equip/unequip pairs. Kept running
only until the new flow is accepted; then this file goes.

WHY IT IS BEING REPLACED, in the order the failures were found:
  - the alpha solve has no handle on the part of a scope crop that is the
    HOST GUN's rail: that hardware does not move with the view, so db = 0 and
    nothing computed from one gun's captures can separate it
  - unequip-per-background is the single largest source of lost rounds --
    "could not get it off the gun", "went to 库存 instead", 0-crop sweeps
  - `bank()` keeps ONE solve per part, ranked by reconstruction error, which
    measures how well a solve rebuilds its own captures and says nothing about
    the gun it will be matched against


    pixi run python calibration/legacy_solve_template.py calibration/artifacts/attachments/runs/<stamp>
    pixi run python calibration/legacy_solve_template.py <run> --write

Reads the (backdrop, filled) pairs calibration/legacy_collect_templates.py's
paired_sweep produces, recovers the icon and its alpha per pixel, and reports
how well the result reconstructs the captures it came from.

THE PROBLEM WITH PHOTOGRAPHING A TEMPLATE. What a slot shows is not the icon.
The panel is translucent, so every captured pixel is

    c = a*icon + (1-a)*backdrop

Storing `c` bakes in one particular scene. It then matches well against that
scene and badly against every other, which is exactly the failure
blend_attachment was written to model rather than suffer.

WHY MORE BACKGROUNDS IS BETTER, not merely sufficient. Two unknowns per pixel
and per channel share one alpha, so ONE pair already determines the answer --
and determines it with whatever antialiasing noise that pair happened to
carry, straight into the template. Six pairs make it overdetermined: the solve
below is a least squares over every pair at once, so per-capture noise averages
down instead of being copied in.

THE SOLVE, per pixel:

    subtract two captures      c_i - c_j = (1-a) * (b_i - b_j)

`icon` drops out entirely, leaving one unknown. Stacked over every pair and
every channel that is a single least squares for (1-a):

    (1-a) = SUM(dc . db) / SUM(db . db)

Pixels where the backdrops barely differ contribute almost nothing to both
sums, which is the right weighting rather than a special case -- a pixel the
scene never moved behind carries no information about transparency.

With `a` known, the icon follows from any capture, averaged over all of them:

    icon = mean_i( c_i - (1-a)*b_i ) / a

and is left undefined where a is ~0, because a fully transparent pixel has no
colour to recover. That is what the alpha channel is for.
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2
import numpy as np

TMPL_DIR = os.path.join(ROOT, 'data', 'templates', 'pubg_assets', 'Item',
                        'Attachment')

# Below this the pixel is treated as fully transparent and its colour is not
# recovered. Measured against the shipped icons, whose alpha is a hard-edged
# mask with a one-pixel feather, so a real edge pixel sits far above it.
ALPHA_FLOOR = 0.05

# A pair contributes to the alpha solve only where the two backdrops actually
# differ. In grey levels, per channel.
BG_SPREAD_MIN = 6.0


def solve(backdrops, filled):
    """-> (icon BGR float, alpha float, per-capture reconstruction error)"""
    b = np.stack(backdrops).astype(np.float32)      # (N, H, W, 3)
    c = np.stack(filled).astype(np.float32)
    n = len(b)

    # Every unordered pair, which is what makes six captures worth more than
    # two: 15 differences instead of 1.
    num = np.zeros(b.shape[1:3], np.float32)
    den = np.zeros(b.shape[1:3], np.float32)
    for i in range(n):
        for j in range(i + 1, n):
            db, dc = b[i] - b[j], c[i] - c[j]
            keep = np.abs(db) >= BG_SPREAD_MIN
            num += np.sum(dc * db * keep, axis=2)
            den += np.sum(db * db * keep, axis=2)

    one_minus_a = np.divide(num, den, out=np.ones_like(num), where=den > 1e-3)
    alpha = np.clip(1.0 - one_minus_a, 0.0, 1.0)

    # icon = mean over captures of (c - (1-a)*b) / a
    acc = np.zeros(b.shape[1:], np.float32)
    for i in range(n):
        acc += c[i] - (1.0 - alpha)[..., None] * b[i]
    prem = acc / n                                   # a * icon
    safe = np.maximum(alpha, ALPHA_FLOOR)[..., None]
    icon = np.clip(prem / safe, 0, 255)

    err = [float(np.abs(alpha[..., None] * icon
                        + (1 - alpha)[..., None] * b[i] - c[i]).mean())
           for i in range(n)]
    return icon, alpha, err


# A pixel counts as opaque when the scene behind the panel moves it less than
# this many grey levels, peak to peak, across the captures. It is a FLOOR on
# the Otsu split rather than the split itself: Otsu always returns something,
# including on a crop where every pixel is opaque and the "two classes" it
# finds are two shades of the same icon.
STABLE_FLOOR = 8.0

# How much of a row crop an icon may plausibly claim as opaque. The icon sits
# in a padded cell, so a real solve lands well under half; a solve claiming
# most of the crop has split two shades of SCENERY, which is what the alpha
# map looks like when the ten "different" backgrounds were not different —
# a turn that failed to move the view (with Tab up a turn's counts go to the
# cursor instead; see Pointer.place).
#
# Measured over the twelve parts of runs 20260805_010546 and _012155:
#
#     eleven clean solves     0.177 .. 0.332     alpha is a crisp glyph
#     uzi_stock               0.794              alpha is a noisy cloud
#
# The gap 0.33 -> 0.79 is empty, so 0.5 separates them with room either side.
# The previous ceiling was 0.9 and uzi_stock passed it. A bad solve that gets
# installed is worse than no solve at all: a template nothing can beat is how
# a wrong answer becomes a confident one.
ROW_OPAQUE_MAX = 0.5
ROW_OPAQUE_MIN = 0.02


def solve_stable(crops):
    """Recover an icon with NO paired empty capture. -> (icon, alpha, spread)

    solve() above needs the backdrop, and a 库存 row has no empty twin: the
    list closes up the moment the part leaves it, so the pixels behind a row
    are never photographed without the row. That is why the row templates in
    this repository are the SLOT-scale solves resized, and why they carry a
    systematic offset — `thumb_grip` lands first in the reference rows with a
    margin of 1.44 and still fails, because its MSE is 175 against a gate of
    150. Being right and failing an absolute threshold is what a scale error
    looks like; correct labels cannot fix it.

    What a row does have is the SAME artwork over ten different scenes, and
    that is enough for the half of the answer that matters. Opaque pixels do
    not move when the scene moves; transparent ones carry it through. So the
    per-pixel spread across the captures IS the alpha map, inverted, and the
    threshold comes from the spread's own histogram rather than a literal.

    The half it cannot give: the COLOUR of a partly transparent pixel, which
    needs the backdrop to subtract. Those pixels are marked transparent
    instead of guessed, which costs the icon its feathered edge and nothing
    else — matching is over the opaque part either way (skill Step 2, and the
    spawner buttons where the dark alpha-blended parts moved up to 86 grey
    levels and had to be excluded for exactly this reason).

    Spread is p90-p10 rather than peak-to-peak: one frame caught mid-fade
    should not condemn a pixel. The icon is the per-pixel MEDIAN for the same
    reason.
    """
    a = np.stack(crops).astype(np.float32)              # (N, H, W, 3)
    hi, lo = np.percentile(a, 90, axis=0), np.percentile(a, 10, axis=0)
    spread = (hi - lo).max(axis=2)                      # worst channel
    icon = np.median(a, axis=0)

    t, _ = cv2.threshold(np.clip(spread, 0, 255).astype(np.uint8), 0, 255,
                         cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    alpha = (spread <= max(float(t), STABLE_FLOOR)).astype(np.float32)
    return icon, alpha, spread


def rows_mode(run, man, write, paired=None, install=False):
    """Solve every 库存 row in a run at ITS OWN scale. -> exit code

    `paired` is the slot-scale result for the same keys, and it is the only
    independent check available here: the two solves share no arithmetic and
    no captures — one subtracts a photographed backdrop, the other watches
    which pixels refuse to move — so where they agree on WHICH pixels are
    opaque, both are describing the artwork rather than their own method. The
    shapes are different sizes, so the comparison resizes the slot mask up and
    reports IoU.

    A low IoU does not automatically condemn this solve. The row icon really
    is a different rendering (different size, different padding), and the
    whole reason for solving it separately is that resizing between them
    loses something. But an IoU near zero means one of the two is not looking
    at the icon at all.
    """
    # THROUGH labelled(), NOT off `entries`. The first version of this read
    # the manifest directly and it was wrong in exactly the way this whole
    # module exists to avoid. A 库存 row capture can be claimed by several
    # keys — the collector photographs the list BEFORE fitting anything, so
    # every part of the round is sitting in it at once and which row belongs
    # to which is inferred afterwards. When that inference is ambiguous the
    # run holds a contradiction, and CaptureRun.conflicts() is what finds it:
    # 70 contradicted (capture, slot) pairs in the 2026-08-04 run, seven
    # groups of keys sharing one set of pixels. angled_grip, brake_ar and
    # cheek_pad all claimed row00 of round 1.
    #
    # Solving a template from those would produce one picture and file it
    # under three names — a fabricated template, which is worse than a
    # missing one because nothing downstream can tell. labelled() drops
    # contradicted labels for precisely this reason and everything else in
    # the repository already goes through it.
    from calibration.capture_run import CaptureRun
    cr = CaptureRun.load_dir(run)
    by_key = defaultdict(list)
    for e, lab, path in cr.labelled():
        if e.get('target') == 'rows' and e.get('key'):
            by_key[e['key']].append(e['capture'])
    dropped = len({e['capture'] for e in man['entries']
                   if e.get('target') == 'rows'}) - \
        len({c for v in by_key.values() for c in v})
    if dropped > 0:
        print(f'{dropped} row capture(s) dropped: more than one key claims '
              f'them, so at most one describes the pixels (CaptureRun.'
              f'conflicts)\n')

    if not by_key:
        print('No usable `rows` captures in this run — every one of them is '
              'claimed by\nmore than one key. The crops are fine; the pairing '
              'is what was lost. Collect\nwith --targets slots,rows and check '
              'the round labelling before solving.')
        return 1

    out = os.path.join(run, 'solved_rows')
    if write:
        os.makedirs(out, exist_ok=True)
    print(f'{"key":<16}{"shots":>6}{"opaque":>8}{"cut":>7}{"px":>7}'
          f'{"IoU vs slot":>13}')
    bad, solved = 0, {}
    for key, files in sorted(by_key.items()):
        crops = [cv2.imread(os.path.join(run, f)) for f in files]
        crops = [c for c in crops if c is not None]
        if len(crops) < 3:
            print(f'{key:<16}{len(crops):>6}   — at least three scenes are '
                  f'needed before "it did not move" means anything')
            bad += 1
            continue
        shapes = {c.shape for c in crops}
        if len(shapes) > 1:
            # Same row, different crop size: the list shifted under the
            # capture. Averaging those would smear two different rows.
            print(f'{key:<16}{len(crops):>6}   — crops disagree on size '
                  f'{sorted(shapes)}')
            bad += 1
            continue
        icon, alpha, spread = solve_stable(crops)
        frac = float(alpha.mean())
        iou = ''
        if paired and key in paired:
            pa = paired[key] > 0.5
            pa = cv2.resize(pa.astype(np.uint8), (alpha.shape[1],
                                                  alpha.shape[0]),
                            interpolation=cv2.INTER_NEAREST) > 0
            mine = alpha > 0.5
            u = float((pa | mine).sum())
            iou = f'{(pa & mine).sum() / u:.3f}' if u else '—'
        # An icon that is almost all "opaque" means the split found two shades
        # of scenery, not icon vs scene. See ROW_OPAQUE_MAX.
        ok = ROW_OPAQUE_MIN <= frac <= ROW_OPAQUE_MAX
        if not ok:
            bad += 1
            iou += '  [!] implausible'
        print(f'{key:<16}{len(crops):>6}{frac:>8.3f}'
              f'{float(np.median(spread)):>7.1f}{alpha.shape[0]:>7}{iou:>13}')
        if write:
            cv2.imwrite(os.path.join(out, f'{key}.png'),
                        np.dstack([icon.astype(np.uint8),
                                   (alpha * 255).astype(np.uint8)]))
            cv2.imwrite(os.path.join(out, f'{key}_alpha.png'),
                        (alpha * 255).astype(np.uint8))
        if install and ok:
            solved[key] = np.dstack([icon.astype(np.uint8),
                                     (alpha * 255).astype(np.uint8)])

    print('\n  `opaque` is the fraction of the crop the scene could not move; '
          'that IS\n  the alpha mask. `cut` is the median spread the Otsu '
          'split ran on.\n  LOOK AT _alpha.png — a clean glyph means the '
          'solve found the icon, a\n  cloud means it found the scene.')
    if write:
        print(f'\n  -> {os.path.relpath(out, ROOT)}')
    if install:
        install_rows(solved)
    return 1 if bad else 0


def install_rows(solved):
    """Put solved row icons into the template bank as `.row` variants.

    THE STEP THAT MAKES A SOLVE COUNT, and until now it was done by hand: the
    twenty-nine `.row.png` that used to be in the bank had no command behind
    them, which is why nothing could say which run any of them came from.

    Named after the ASSET, like every other file in the bank, because that is
    what AttachmentDetector keys on — the part of the filename before the
    first dot is the asset, the rest is which rendering this picture is.

    SAVED AT THE SIZE IT WAS SOLVED AT, which is the size of the cell it was
    photographed in. Nothing here reframes or rescales it: the detector reads
    each variant at its own scale (AttachmentDetector.TMPL_OFFSETS), so the
    picture that reaches the comparison is the picture the screen drew.
    """
    from detector.attachment_catalog import ATTACHMENTS
    n, skipped = 0, []
    for key, bgra in sorted(solved.items()):
        asset = ATTACHMENTS.get(key, {}).get('asset')
        if not asset:
            # No asset name means the catalogue has no file name to give this
            # part, and inventing one here would put a template in the bank
            # under a stem nothing else agrees on.
            skipped.append(key)
            continue
        dst = os.path.join(TMPL_DIR, f'Item_Attach_Weapon_{asset}.row.png')
        cv2.imwrite(dst, bgra)
        print(f'  {key:<16} -> {os.path.basename(dst)}')
        n += 1
    print(f'\n  {n} row variant(s) installed into '
          f'{os.path.relpath(TMPL_DIR, ROOT)}')
    if skipped:
        print(f'  no `asset` in the catalogue, not installed: '
              f'{", ".join(skipped)}')
    print('  Now re-score: pixi run attachments --holdout')


def stability(run, pairs):
    """How many backgrounds are enough, measured on HELD-OUT captures.

    For k = 2..N-1: solve using k of the pairs, then reconstruct one that was
    NOT in the solve and report the error against what was actually captured.

    That number cannot be talked up by the model. Reconstructing the captures
    a solve was fitted to always looks good -- it is the residual of its own
    fit -- and synthesising captures with the compositing formula the solve
    inverts is worse still: it would only demonstrate that the arithmetic
    undoes itself, and would pass just as happily if that formula were wrong
    about the game. A capture the solve never saw is neither.

    `moved` is the second question: how far the recovered alpha shifts when
    one more background is added. When that stops moving, more captures are
    buying nothing.
    """
    print('  key              k   held-out err   alpha moved vs k-1')
    worst = 0.0
    for key, angles in sorted(pairs.items()):
        usable = [v for v in angles.values()
                  if 'backdrop' in v and 'slots' in v]
        if len(usable) < 3:
            print(f'  {key:<16} only {len(usable)} pair(s) — need at least 3 '
                  f'to hold one out')
            continue
        bg = [cv2.imread(os.path.join(run, v['backdrop'])) for v in usable]
        fg = [cv2.imread(os.path.join(run, v['slots'])) for v in usable]
        prev_a = None
        for k in range(2, len(usable)):
            icon, alpha, _ = solve(bg[:k], fg[:k])
            # The first capture the solve did not use.
            b, c = bg[k].astype(np.float32), fg[k].astype(np.float32)
            pred = alpha[..., None] * icon + (1 - alpha)[..., None] * b
            held = float(np.abs(pred - c).mean())
            moved = ('' if prev_a is None
                     else f'{float(np.abs(alpha - prev_a).mean()):.4f}')
            prev_a = alpha
            print(f'  {key:<16}{k:>3}{held:>15.2f}{moved:>22}')
            worst = max(worst, held)
        print()
    print('  held-out err is grey levels on a crop the solve never saw. It is'
          '\n  the only one of the three numbers here that a wrong compositing'
          '\n  model cannot flatter.'
          '\n  `alpha moved` near zero means another background adds nothing.')
    return 0 if worst < 3.0 else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run', nargs='?', default='')
    ap.add_argument('--write', action='store_true',
                    help='save the solved BGRA icons into the run directory')
    ap.add_argument('--rows', action='store_true',
                    help='solve the 库存 list icons at THEIR OWN scale, from '
                         'the same row over many scenes. No paired empty '
                         'capture is needed or possible — see solve_stable.')
    ap.add_argument('--install', action='store_true',
                    help='--rows only: copy the plausible solves into the '
                         'template bank as .row variants, which is the step '
                         'that makes them count. Implies --write. A solve '
                         'flagged implausible is written to the run but NOT '
                         'installed.')
    ap.add_argument('--stability', action='store_true',
                    help='how many backgrounds the solve needs, measured by '
                         'HOLD-OUT: solve on k captures, predict one that was '
                         'not used. No synthetic data and no ground truth — '
                         'compositing captures with the same formula the solve '
                         'inverts would only prove the solver can undo its own '
                         'arithmetic.')
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    run = args.run or sorted(glob.glob(
        os.path.join(ROOT, 'calibration', 'artifacts', 'attachments', 'runs', '*')))[-1]
    man = json.load(open(os.path.join(run, 'manifest.json'), encoding='utf-8'))
    print(f'run: {os.path.relpath(run, ROOT)}\n')

    pairs = defaultdict(dict)
    for e in man['entries']:
        if e.get('target') not in ('backdrop', 'slots'):
            continue
        a = e.get('angle')
        if a is None:
            continue
        pairs[e['key']].setdefault(a, {})[e['target']] = e['capture']

    if not pairs and not args.rows:
        print('No paired captures in this run. paired_sweep writes `backdrop`\n'
              'entries alongside `slots` ones with an `angle`; a run collected\n'
              'before that existed has the filled crops only, and the icon\n'
              'cannot be separated from the scene behind it.')
        return 1

    if args.rows:
        # Solve the slot scale first, unwritten, purely to have something
        # independent to check the row masks against.
        paired = {}
        for key, angles in pairs.items():
            usable = [v for v in angles.values()
                      if 'backdrop' in v and 'slots' in v]
            if len(usable) < 2:
                continue
            bg = [cv2.imread(os.path.join(run, v['backdrop'])) for v in usable]
            fg = [cv2.imread(os.path.join(run, v['slots'])) for v in usable]
            if not any(x is None for x in bg + fg):
                paired[key] = solve(bg, fg)[1]
        return rows_mode(run, man, args.write or args.install, paired,
                         install=args.install)

    if args.stability:
        return stability(run, pairs)

    out = os.path.join(run, 'solved')
    if args.write:
        os.makedirs(out, exist_ok=True)
    print(f'{"key":<16}{"pairs":>6}{"mean a":>9}{"recon err":>11}   verdict')
    bad = 0
    for key, angles in sorted(pairs.items()):
        usable = [v for v in angles.values()
                  if 'backdrop' in v and 'slots' in v]
        if len(usable) < 2:
            print(f'{key:<16}{len(usable):>6}   — at least two backgrounds are '
                  f'needed to separate the icon from the scene')
            bad += 1
            continue
        bg = [cv2.imread(os.path.join(run, v['backdrop'])) for v in usable]
        fg = [cv2.imread(os.path.join(run, v['slots'])) for v in usable]
        if any(x is None for x in bg + fg):
            print(f'{key:<16}{len(usable):>6}   — a capture is missing')
            bad += 1
            continue
        icon, alpha, err = solve(bg, fg)
        m = float(alpha.mean())
        e = float(np.mean(err))
        verdict = ('good' if e < 1.5 else
                   'usable' if e < 3 else 'WRONG — look at the alpha')
        if e >= 3:
            bad += 1
        print(f'{key:<16}{len(usable):>6}{m:>9.3f}{e:>11.2f}   {verdict}')
        if args.write:
            bgra = np.dstack([icon.astype(np.uint8),
                              (alpha * 255).astype(np.uint8)])
            cv2.imwrite(os.path.join(out, f'{key}.png'), bgra)
            cv2.imwrite(os.path.join(out, f'{key}_alpha.png'),
                        (alpha * 255).astype(np.uint8))

    print('\n  recon err <1.5 good, 1.5-3 usable, >3 means the pairs are not '
          'pairs\n  (different angle, or the equip did not land between them).'
          '\n  ALWAYS LOOK AT _alpha.png before trusting one: clean shape, '
          'bright\n  where the icon is solid, no scene bleeding through.')
    if args.write:
        print(f'\n  -> {os.path.relpath(out, ROOT)}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
