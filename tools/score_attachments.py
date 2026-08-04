"""Every attachment template against every ground-truth crop. Offline.

    pixi run attachments                 # score what the detector loads today
    pixi run attachments --holdout       # score with the run under test excluded
    pixi run attachments --write         # rebuild the .solved templates, then score

A template is only good if it beats every OTHER template on its own target,
so this scores the whole bank against the whole labelled corpus and reports
the MARGIN as well as the hit — a thin margin is a future misread, and a
missing template does not read as nothing, it reads as the nearest neighbour,
confidently. `Lower_ThumbGrip_C` drifted and every thumb grip in the corpus
reads `laser`: in-catalogue, confident, wrong.

WHERE THE TRUTH COMES FROM. `CaptureRun.labelled()`, which returns only parts
that were fitted on purpose and confirmed without consulting a template. A
detector's own reading cannot judge the detector, so nothing else is eligible;
see calibration/capture_run.py.

WHAT --write BUILDS. calibration/collect_templates.py photographs each part
over ten backgrounds AND photographs the empty slot behind it, and
tools/solve_template.py inverts the compositing to recover the icon and its
alpha. That recovered picture is what the screen actually draws, which the
shipped game art is not: the art is scaled, outlined and blended into a
translucent panel before a single pixel of it reaches the screen. The solve
lands as a `.solved` variant beside the art rather than over it — the art
still covers parts no run has reached.

    training_data/pubg_assets/Item/Attachment/
        Item_Attach_Weapon_Lower_Foregrip_C.png          the game's art
        Item_Attach_Weapon_Lower_Foregrip_C.solved.png   recovered from screen

TWO WAYS TO FLATTER A TEMPLATE, both refused here:

  scoring it on the captures it was solved from    --holdout excludes, per run,
                                                   every template solved out of
                                                   that same run
  scoring only the parts that have templates       every labelled crop is
                                                   scored; a part with no
                                                   template counts as a miss,
                                                   because in the field it is
                                                   one
"""
import argparse
import glob
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2
import numpy as np

from calibration.capture_run import CaptureRun
from detector.attachment_catalog import ATTACHMENTS
import detector.attachment_detector as ad
from detector.tab_items import ROW_MSE_MAX, ROW_MARGIN_MIN
from tools.solve_template import solve

RUNS = os.path.join(ROOT, 'docs', 'attachments', 'runs')
TMPL_DIR = os.path.join(ROOT, 'training_data', 'pubg_assets', 'Item',
                        'Attachment')
TAG = 'solved'

# The solve reconstructs the captures it was fitted to; above this the pairs
# are not pairs (a different angle, or the equip did not land between them) and
# the recovered alpha carries scenery. Same three bands solve_template prints.
RECON_MAX = 3.0

# A solved icon is 63x63 — the whole slot tile — and the icon is drawn inside
# it at the offset the detector already matches at. Cutting to that window is
# what makes a solved template interchangeable with a shipped one, and it
# drops the tile's own border, which the solve reads as opaque because it does
# not move with the scene behind it.
CUT = (ad.OFFSET_Y, ad.OFFSET_Y + ad.TMPL_SIZE,
       ad.OFFSET_X, ad.OFFSET_X + ad.TMPL_SIZE)

ASSET = {k: v['asset'] for k, v in ATTACHMENTS.items() if v.get('asset')}
KEY_OF = {v: k for k, v in ASSET.items()}


# ── the corpus ──

def samples():
    """Every ground-truth attachment crop on disk. -> [(run, sample), ...]

    `rows` crops come from the 库存 list and are 80x80; the detector's own
    reader resizes them to the slot size, so they arrive here the same way.
    Both are collected because the same artwork at two sizes is two different
    matches: Stock_SniperRifle_CheekPad_C has passed in a slot while failing
    in a row.
    """
    out = []
    for d in sorted(glob.glob(os.path.join(RUNS, '*'))):
        if not os.path.exists(os.path.join(d, 'manifest.json')):
            continue
        run = CaptureRun.load_dir(d)
        for e, lab, path in run.labelled():
            target = e.get('target')
            if target not in ('slots', 'rows'):
                continue
            slot = (lab['slot'] if target == 'slots'
                    else ATTACHMENTS.get(lab['asset'], {}).get('slot'))
            if not slot:
                continue
            parts = os.path.basename(path).split('__')
            out.append({'run': run.stamp, 'target': target, 'path': path,
                        'key': lab['asset'], 'slot': slot,
                        'weapon': parts[2] if target == 'slots'
                                  and len(parts) > 2 else None})
    return out


def crop_of(s):
    img = cv2.imread(s['path'])
    if img is None:
        return None
    if s['target'] == 'rows':
        img = cv2.resize(img, (63, 63), interpolation=cv2.INTER_AREA)
    return img


# ── the bank ──

def solved_icons(run_dir):
    """Solve every paired key in one run. -> {key: (bgra48, recon_err)}"""
    run = CaptureRun.load_dir(run_dir)
    pairs = defaultdict(dict)
    for e in run.entries:
        if e.get('target') in ('backdrop', 'slots') and e.get('angle') is not None:
            pairs[e['key']].setdefault(e['angle'], {})[e['target']] = e['capture']
    out = {}
    for key, angles in pairs.items():
        usable = [v for v in angles.values()
                  if 'backdrop' in v and 'slots' in v]
        if len(usable) < 2 or key not in ASSET:
            continue
        bg = [cv2.imread(os.path.join(run_dir, v['backdrop'])) for v in usable]
        fg = [cv2.imread(os.path.join(run_dir, v['slots'])) for v in usable]
        if any(x is None for x in bg + fg):
            continue
        icon, alpha, err = solve(bg, fg)
        y0, y1, x0, x1 = CUT
        bgra = np.dstack([icon.astype(np.uint8),
                          (alpha * 255).astype(np.uint8)])[y0:y1, x0:x1]
        out[key] = (bgra, float(np.mean(err)))
    return out


def bank(exclude=()):
    """The best solved icon per key, from every run but the excluded ones.

    -> {key: (bgra48, err, stamp)}
    """
    best = {}
    for d in sorted(glob.glob(os.path.join(RUNS, '*'))):
        stamp = os.path.basename(d)
        if stamp in exclude or not os.path.exists(
                os.path.join(d, 'manifest.json')):
            continue
        for key, (bgra, err) in solved_icons(d).items():
            if err >= RECON_MAX:
                continue
            if key not in best or err < best[key][1]:
                best[key] = (bgra, err, stamp)
    return best


def detector_with(icons):
    """A detector holding the shipped art plus these solved icons.

    Built by loading the real one and adding variants, so the matching code
    under test is the matching code that ships.
    """
    det = ad.AttachmentDetector()
    for key, entry in icons.items():
        bgra = entry[0] if isinstance(entry, tuple) else entry
        mask = bgra[:, :, 3] > ad.ALPHA_TH
        if int(mask.sum()) < 30:
            continue
        name = ASSET[key]
        ys, xs = np.where(mask)
        vals = bgra[:, :, :3].astype(np.float32)[ys, xs]
        if name not in det._templates:
            slot = ATTACHMENTS[key]['slot']
            det._slot_index.setdefault(slot, []).append(name)
        det._templates.setdefault(name, []).append((vals, ys, xs))
    return det


# ── scoring ──

def read(det, s):
    """What this detector makes of one sample. -> (key or '', margin)

    THE TWO TARGETS ARE READ ON DIFFERENT TERMS, and scoring them alike would
    misreport both. A weapon slot is read knowing which slot it is and, when
    the plate was OCR'd, which gun holds it, so the bank narrows to what can
    physically go there; a 库存 row is a blind match against every template
    there is, and pays for that with a far tighter MSE ceiling and a margin
    floor. Those are tab_items' terms, imported rather than restated.

    Deliberately NOT narrowed by the label: using the answer to pick the
    candidates would score a bank that cannot exist at read time.
    """
    crop = crop_of(s)
    if crop is None:
        return '', 0.0
    if not det.drawn(crop):
        return '', 0.0
    if s['target'] == 'rows':
        name, mse, margin = det.best_two(crop, list(det._templates))
        if mse > ROW_MSE_MAX or margin < ROW_MARGIN_MIN:
            return '', margin
        return KEY_OF.get(name, name), margin
    names = det.candidates(s['slot'], s['weapon'])
    if not names:
        return '', 0.0
    name, mse, margin = det.best_two(crop, names)
    if mse > ad.MSE_EMPTY_TH:
        return '', 0.0
    return KEY_OF.get(name, name), margin


def score(rows, title):
    """-> (hits, total). Prints one per-key table per target."""
    print(f'\n{"=" * 78}\n{title}')
    hits = total = 0
    for target in ('slots', 'rows'):
        mine = [r for r in rows if r[0]['target'] == target]
        if not mine:
            continue
        per = defaultdict(lambda: {'n': 0, 'ok': 0, 'as': defaultdict(int),
                                   'margins': []})
        for s, got, margin in mine:
            p = per[s['key']]
            p['n'] += 1
            if got == s['key']:
                p['ok'] += 1
                p['margins'].append(margin)
            else:
                p['as'][got or '<nothing>'] += 1
        h = sum(p['ok'] for p in per.values())
        t = sum(p['n'] for p in per.values())
        hits, total = hits + h, total + t
        how = ('slot known, weapon narrows the bank, MSE < '
               f'{ad.MSE_EMPTY_TH}' if target == 'slots' else
               f'blind against every template, MSE < {ROW_MSE_MAX} and '
               f'margin > {ROW_MARGIN_MIN}')
        print(f'\n{target}  {h}/{t} = {h / t:.3f}   ({how})')
        print(f'  {"key":<15}{"hit":>9}{"margin min":>12}   read instead')
        for key in sorted(per):
            p = per[key]
            m = f'{min(p["margins"]):.2f}' if p['margins'] else '—'
            wrong = ', '.join(f'{k}x{n}' if n > 1 else k
                              for k, n in sorted(p['as'].items()))
            print(f'  {key:<15}{p["ok"]:>4}/{p["n"]:<4}{m:>12}   {wrong}')
    return hits, total


def collect(det, corpus):
    return [(s,) + read(det, s) for s in corpus]


# ── writing ──

def write_bank(icons):
    n = 0
    for key, (bgra, err, stamp) in sorted(icons.items()):
        dst = os.path.join(TMPL_DIR,
                           f'Item_Attach_Weapon_{ASSET[key]}.{TAG}.png')
        cv2.imwrite(dst, bgra)
        print(f'  {key:<15} <- {stamp}  recon {err:.2f}')
        n += 1
    print(f'\n  {n} solved template(s) -> '
          f'{os.path.relpath(TMPL_DIR, ROOT)}/*.{TAG}.png')
    missing = sorted(set(ASSET) - set(icons))
    if missing:
        print(f'\n  no solved template, still on the shipped art '
              f'({len(missing)}): {", ".join(missing)}')
    no_art = sorted(k for k, v in ATTACHMENTS.items()
                    if not v.get('asset') and k not in icons)
    if no_art:
        print(f'  no picture at all ({len(no_art)}): {", ".join(no_art)}'
              f'\n  -- these read as their nearest neighbour, not as nothing.')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--write', action='store_true',
                    help='rebuild the .solved templates from every run first')
    ap.add_argument('--holdout', action='store_true',
                    help='score each run against a bank solved WITHOUT it')
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    if args.write:
        print('rebuilding solved templates')
        write_bank(bank())
        print()

    corpus = samples()
    if not corpus:
        print(f'no ground-truth crops under {os.path.relpath(RUNS, ROOT)}')
        return 1
    runs = sorted({s['run'] for s in corpus})
    print(f'{len(corpus)} ground-truth crops from {len(runs)} run(s), '
          f'{len(set(s["key"] for s in corpus))} attachment(s)')

    hits, total = score(collect(ad.AttachmentDetector(), corpus),
                        'AS THE DETECTOR LOADS IT')

    if args.holdout:
        rows = []
        for run in runs:
            mine = [s for s in corpus if s['run'] == run]
            det = detector_with(bank(exclude=(run,)))
            rows += collect(det, mine)
        h, t = score(rows, 'HELD OUT — no template solved from the run it scores')
        print('\n  The held-out number is the one a solved template cannot '
              'flatter.\n  A gap between the two means a template is '
              'reproducing its own\n  captures rather than the icon behind '
              'them.')
        hits, total = h, t

    print()
    return 0 if hits == total else 1


if __name__ == '__main__':
    sys.exit(main())
