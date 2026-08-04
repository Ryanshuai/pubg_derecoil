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

THE 库存 ROWS ARE REPORTED SEPARATELY AND THE REASON IS NOT SYMMETRY. Their
run labels do not survive inspection. Take docs/attachments/runs/20260803_103108,
one part per round, one row each:

    row  label says     templates read     mse   margin
      2  cheek_pad      ext_ar              12    10.5
      7  comp_ar        ext_ar              20     6.7
     11  variable       vert_grip           13     9.1

An MSE of 12 at ten times its runner-up is what a correct match looks like —
the slot corpus scores 7..17 — so those crops are not pictures of what the
label says. Nor is it a constant shift that could be undone: the game inserts
a new part into its OWN sort order, and the collector had assumed the newest
row was the last one (fixed since, in ff047bc). So the row corpus is scored
and printed, never deleted, and never added into the headline number. The
trustworthy row truth is the twelve hand-read rows of docs/tab_inventory.png.
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
from detector.tab_layout import icon_box
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

# docs/tab_inventory.png, read off the screenshot by eye — the only 库存 truth
# in the repository that no collector produced and no template touched. Two
# rows hold the same part, which is a fact about the screenshot.
REF_SHOT = os.path.join(ROOT, 'docs', 'tab_inventory.png')
REF_ROWS = ['scope_2x', 'scope_4x', 'red_dot', 'holo', 'ext_sr', 'quickext_sr',
            'flash_sr', 'laser', 'thumb_grip', 'thumb_grip', 'flash_smg',
            'duckbill']


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


def reference_rows():
    """The hand-read 库存 rows. -> [sample] with the crop already cut."""
    frame = cv2.imread(REF_SHOT)
    if frame is None:
        return []
    out = []
    for i, key in enumerate(REF_ROWS):
        x0, y0, x1, y1 = icon_box(i, 'inventory')
        out.append({'run': 'tab_inventory.png', 'target': 'reference rows',
                    'path': None, 'crop': frame[y0:y1, x0:x1], 'key': key,
                    'slot': ATTACHMENTS[key]['slot'], 'weapon': None})
    return out


def crop_of(s):
    img = s['crop'] if s.get('crop') is not None else cv2.imread(s['path'])
    if img is None:
        return None
    if s['target'] != 'slots':
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
    if s['target'] != 'slots':
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


# Which targets count towards the verdict. `rows` is scored and printed but
# excluded: see the module docstring — its labels are contradicted by the
# pixels, and averaging a broken corpus into a good one hides both.
COUNTED = ('slots', 'reference rows')

# A RATCHET, NOT A TARGET. Full marks are not reachable today and pretending
# otherwise would make this task permanently red, which is how a check stops
# being read. What is not reachable, measured 2026-08-03:
#
#   scope_2x             6 of 16, eaten by scope_6x. scope_6x's own solve is
#                        the weakest in the bank (recon 2.06, from the only
#                        run that has it) and the two reticle icons differ in
#                        little more than a number.
#   thumb_grip           2 reference rows: the solved icon is a SLOT-scale
#                        picture and a list row renders the art smaller
#   5 stray <nothing>    one each of angled_grip, cheek_pad, half_grip, holo,
#                        supp_smg
#
# Going ABOVE the baseline fails too, on purpose: it means one of the above
# was fixed and this comment is now a lie. Re-measure, then raise the numbers.
BASELINE = {'slots': (749, 760), 'reference rows': (10, 12)}


def score(rows, title):
    """-> {target: (hits, total)}. Prints one per-key table per target."""
    print(f'\n{"=" * 78}\n{title}')
    totals = {}
    for target in ('slots', 'reference rows', 'rows'):
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
        totals[target] = (h, t)
        how = ('slot known, weapon narrows the bank, MSE < '
               f'{ad.MSE_EMPTY_TH}' if target == 'slots' else
               f'blind against every template, MSE < {ROW_MSE_MAX} and '
               f'margin > {ROW_MARGIN_MIN}')
        note = ('' if target in COUNTED else
                '  NOT COUNTED — labels contradicted, see the module docstring')
        print(f'\n{target}  {h}/{t} = {h / t:.3f}   ({how}){note}')
        print(f'  {"key":<15}{"hit":>9}{"margin min":>12}   read instead')
        for key in sorted(per):
            p = per[key]
            m = f'{min(p["margins"]):.2f}' if p['margins'] else '—'
            wrong = ', '.join(f'{k}x{n}' if n > 1 else k
                              for k, n in sorted(p['as'].items()))
            print(f'  {key:<15}{p["ok"]:>4}/{p["n"]:<4}{m:>12}   {wrong}')
    return totals


def verdict(totals):
    """Compare against the ratchet. -> exit code"""
    bad = []
    print()
    for target, (want, n) in BASELINE.items():
        h, t = totals.get(target, (0, 0))
        if t != n:
            bad.append(f'{target}: corpus is {t} crops, the baseline was '
                       f'measured on {n} — re-measure before reading the hits')
        elif h < want:
            bad.append(f'{target}: {h}/{t}, DOWN from {want}/{n} — a template '
                       f'regressed')
        elif h > want:
            bad.append(f'{target}: {h}/{t}, UP from {want}/{n} — raise the '
                       f'baseline and rewrite what it says is unreachable')
        else:
            print(f'  OK   {target}  {h}/{t}, at the baseline')
    for line in bad:
        print(f'  [!]  {line}')
    return 1 if bad else 0


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
    corpus += reference_rows()

    got = score(collect(ad.AttachmentDetector(), corpus),
                'AS THE DETECTOR LOADS IT')

    if args.holdout:
        rows = []
        for run in runs:
            mine = [s for s in corpus if s['run'] == run]
            det = detector_with(bank(exclude=(run,)))
            rows += collect(det, mine)
        # The reference rows belong to no run, so nothing about them is held
        # out and re-scoring them would only repeat the pass above.
        got = score(rows, 'HELD OUT — no template solved from the run it '
                          'scores') | {k: v for k, v in got.items()
                                       if k == 'reference rows'}
        print('\n  The held-out number is the one a solved template cannot '
              'flatter.\n  A gap between the two means a template is '
              'reproducing its own\n  captures rather than the icon behind '
              'them.')

    return verdict(got)


if __name__ == '__main__':
    sys.exit(main())
