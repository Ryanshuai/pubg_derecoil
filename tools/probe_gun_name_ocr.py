"""Score every weapon-name template against the name plates in a capture.

Why: TabWeaponDetector's IoU divides by the white pixels of the *whole* crop,
so a name longer than its template scores low no matter how well the template
fits. 'Micro UZI 冲锋枪' is 2044 white px against a 1175 px template — 0.57,
under the 0.85 threshold, and the gun reads as unnamed.

This prints both metrics side by side for every template, so the fix can be
judged on whether a windowed IoU keeps the right answer on top with a margin,
rather than on whether it rescues the one gun that prompted it.

    python tools/probe_gun_name_ocr.py                     # both references
    python tools/probe_gun_name_ocr.py <shot.png> ...

Three plates cannot be matched under a Chinese client, and no metric fixes
them: slr, tommy and dragunov hold English templates (SLR, Tommy Gun,
Dragunov) while the game prints 自动装填步枪, 汤姆逊冲锋枪 and 德拉贡诺夫.
They need a second template cut from a Chinese capture — not a replacement,
since the English one is still right when the client is English:

    python tools/probe_gun_name_ocr.py --extract shot.png:1:slr:cn          # dry
    python tools/probe_gun_name_ocr.py --extract shot.png:1:slr:cn --write
    python tools/probe_gun_name_ocr.py --variants                # what is installed

A tagged file is another variant of the same weapon, so both languages match
at once and nothing has to be configured or deleted.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from config import HUD_REGIONS
from detector.weapon_template_detector import (TabWeaponDetector,
                                               _white_text_mask)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# What the two reference captures actually hold, for judging the ranking.
TRUTH = {'tab_inventory.png': ('g36c', 'sks'),
         'tab_inventory_2.png': ('uzi', 'mk12')}


METRICS = ('whole', 'window', 'cover')


def score_all(crop, templates, rank='cover'):
    """[({metric: score}, code), ...], best first by `rank`.

    whole   inter / (all crop white + template - inter)   — what ships today
    window  inter / (white inside the match window + template - inter)
    cover   inter / template                              — recall of the GT

    `whole` is the one that breaks: crop white counts every glyph on the
    plate, so a name longer than its template is penalised for text the
    template was never meant to cover. `cover` divides by the template alone
    and so cannot see the extra text at all; `window` still charges for crop
    pixels *under* the template, which is what stops a short template from
    scoring full marks on a denser blob.
    """
    binary = _white_text_mask(crop)
    crop_px = np.count_nonzero(binary)
    if crop_px == 0:
        return []
    out = []
    for code, tmpls in templates.items():
        best = None
        for tmpl in tmpls:
            if tmpl.shape[0] > binary.shape[0] or tmpl.shape[1] > binary.shape[1]:
                continue
            res = cv2.matchTemplate(binary, tmpl, cv2.TM_CCOEFF_NORMED)
            if res.max() < 0.5:
                continue
            _, _, _, (tx, ty) = cv2.minMaxLoc(res)
            th, tw = tmpl.shape[:2]
            win = binary[ty:ty + th, tx:tx + tw]
            inter = np.count_nonzero(win & tmpl)
            tmpl_px = np.count_nonzero(tmpl)
            s = {'whole': inter / max(crop_px + tmpl_px - inter, 1),
                 'window': inter / max(np.count_nonzero(win) + tmpl_px - inter, 1),
                 'cover': inter / max(tmpl_px, 1)}
            if best is None or s[rank] > best[rank]:
                best = s
        if best:
            out.append((best, code))
    out.sort(key=lambda r: -r[0][rank])
    return out


def extract(spec, write=False, force=False):
    """Cut a name-plate template out of a capture.

    spec is 'png:gun:code' or 'png:gun:code:tag'. With a tag it writes
    <code>.<tag>.png, which the loader takes as another variant of the same
    weapon rather than a replacement — so adding the Chinese plate leaves the
    English one working, and the game can be switched either way.
    """
    # Split from the RIGHT, and only where the gun field lands on a digit: a
    # Windows path carries its own colon (D:/runs/plate.png), and splitting
    # from the left makes the drive letter the filename.
    for n in (3, 2):
        parts = spec.rsplit(':', n)
        if len(parts) == n + 1 and parts[1].isdigit():
            path, gun, code = parts[:3]
            tag = parts[3] if n == 3 else None
            break
    else:
        print(f'{spec!r}: expected png:gun:code[:tag]')
        return 1

    frame = cv2.imread(path)
    if frame is None:
        print(f'cannot read {path}')
        return 1
    y, x, h, w = HUD_REGIONS[f'gun_name_{int(gun)}']
    # An image already the size of a plate IS one — collect_templates.py writes
    # plate__<weapon>__bgN.png at exactly that size, and indexing into it would
    # run off the end. `gun` is then only there to satisfy the spec format.
    plate = frame if frame.shape[:2] == (h, w) else frame[y:y + h, x:x + w]
    mask = _white_text_mask(plate)
    ys, xs = np.where(mask)
    if len(ys) == 0:
        print(f'{path} gun{gun}: no white text on the plate')
        return 1
    tight = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    fname = f'{code}.{tag}.png' if tag else f'{code}.png'
    out = os.path.join(ROOT, 'training_data', 'ocr_white', fname)
    det = TabWeaponDetector()
    have = len(det._templates.get(code, []))
    print(f'{code}: {tight.shape[1]}x{tight.shape[0]}, '
          f'{np.count_nonzero(tight)} px -> {os.path.relpath(out, ROOT)} '
          f'({have} variant{"s" if have != 1 else ""} already)')
    if os.path.exists(out) and not force:
        print(f'  {fname} exists; give it a tag or pass --force to replace it')
        return 1
    if not write:
        print('  dry run; pass --write to save')
        return 0

    cv2.imwrite(out, tight)
    fresh = TabWeaponDetector()
    got = _score_one(frame, int(gun), fresh)
    print(f'  written, {code} now has {len(fresh._templates[code])} variants. '
          f'plate reads {got or "<nothing>"}')
    return 0 if got == code else 1


def _score_one(frame, gun, det):
    y, x, h, w = HUD_REGIONS[f'gun_name_{gun}']
    ranked = score_all(frame[y:y + h, x:x + w], det._templates, rank='window')
    return ranked[0][1] if ranked else ''


def main():
    argv = sys.argv[1:]
    if '--extract' in argv:
        i = argv.index('--extract')
        return extract(argv[i + 1], write='--write' in argv,
                       force='--force' in argv)
    if '--variants' in argv:
        det = TabWeaponDetector()
        multi = {c: v for c, v in det._templates.items() if len(v) > 1}
        print(f'{len(det._templates)} weapon codes, '
              f'{sum(len(v) for v in det._templates.values())} templates')
        for code, tmpls in sorted(multi.items()):
            print(f'  {code:<10} ' + '  '.join(f'{t.shape[1]}x{t.shape[0]}'
                                               for t in tmpls))
        if not multi:
            print('  every code has exactly one template — no language '
                  'variants installed yet')
        return 0

    shots = [a for a in argv if not a.startswith('-')] or [
        os.path.join(ROOT, 'docs', n) for n in TRUTH]
    det = TabWeaponDetector()
    crops = []
    for path in shots:
        frame = cv2.imread(path)
        if frame is None:
            print(f'cannot read {path}')
            continue
        name = os.path.basename(path)
        truth = TRUTH.get(name, ('?', '?'))
        for gun in (1, 2):
            y, x, h, w = HUD_REGIONS[f'gun_name_{gun}']
            crops.append((f'{name} gun{gun}', truth[gun - 1],
                          frame[y:y + h, x:x + w]))

    verdict = {}
    for metric in METRICS:
        print(f'\n{"=" * 66}\nrank by {metric}')
        misses = 0
        worst = 1.0
        for label, want, crop in crops:
            ranked = score_all(crop, det._templates, rank=metric)
            if not ranked:
                print(f'  {label}: no white text')
                misses += 1
                continue
            top_s, top_c = ranked[0]
            runner = ranked[1][0][metric] if len(ranked) > 1 else 0.0
            margin = top_s[metric] - runner
            ok = top_c == want
            misses += not ok
            worst = min(worst, margin)
            print(f'  {label:<26} want={want:<7} '
                  f'{"OK " if ok else "MISS"} got={top_c:<8} '
                  f'score={top_s[metric]:.3f} margin={margin:+.3f}')
            for s, code in ranked[:3]:
                print(f'       {code:<9} ' +
                      '  '.join(f'{m}={s[m]:.3f}' for m in METRICS) +
                      ('   <- truth' if code == want else ''))
        verdict[metric] = (misses, worst)

    print(f'\n{"=" * 66}')
    for metric, (misses, worst) in verdict.items():
        print(f'  {metric:<7} {misses} misranked, tightest margin {worst:+.3f}')
    return 1 if verdict['cover'][0] else 0


if __name__ == '__main__':
    sys.exit(main())
