"""Build and audit the ammo-digit templates, entirely offline.

    python tools/probe_ammo_ocr.py                        # read every capture
    python tools/probe_ammo_ocr.py --extract <png>=<label>   # dry run
    python tools/probe_ammo_ocr.py --extract <png>=<label> --write
    python tools/probe_ammo_ocr.py --confusion            # digit separability
    python tools/probe_ammo_ocr.py --bench                # per-frame cost

`label` is what the HUD prints in that capture, left to right: `--extract
shot.png=40` cuts two templates, digit_4 and digit_0. The count of glyphs
found must equal the label's length or nothing is written, which is what stops
a mislabelled capture from installing a wrong template silently.

The scan is the regression: it reads the ammo strip in every full-screen
capture under docs/ and temp_debug/ and prints what came out, including the
glyphs that matched nothing. An unreadable glyph is the signal that a digit is
still missing from the set — not that the detector is broken.
"""
import glob
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from config import HUD_REGIONS
from detector.ammo_detector import (ASSETS_DIR, CANVAS_H, CANVAS_W,
                                    AmmoDetector, _best_iou, _place, segment)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def captures():
    paths = sorted(glob.glob(os.path.join(ROOT, 'docs', '**', '*.jpg'),
                             recursive=True))
    paths += sorted(glob.glob(os.path.join(ROOT, 'docs', '**', '*.png'),
                              recursive=True))
    paths += sorted(glob.glob(os.path.join(ROOT, 'temp_debug', 'screen_*.png')))
    return paths


def ammo_crop(frame):
    y, x, h, w = HUD_REGIONS['ammo']
    return frame[y:y + h, x:x + w]


def scan():
    det = AmmoDetector()
    print(f'templates installed: {det.digits_known or "none"}')
    if not det.digits_known:
        print('  nothing to match against — run --extract first')

    values, reasons = Counter(), Counter()
    n_frames = 0
    weak = []
    for path in captures():
        frame = cv2.imread(path)
        if frame is None or frame.shape[:2] != (1440, 3440):
            continue
        n_frames += 1
        r = det.read(ammo_crop(frame))
        if r['value'] is None:
            reasons[r['reason'].split(' iou')[0]] += 1
            if r['glyphs']:
                weak.append((min(g['iou'] for g in r['glyphs']), path, r))
        else:
            values[r['value']] += 1

    print(f'\n{n_frames} full-screen captures')
    print(f'  read     {sum(values.values()):4d}   '
          f'values {dict(sorted(values.items()))}')
    print(f'  unread   {sum(reasons.values()):4d}')
    for reason, c in reasons.most_common():
        print(f'      {c:4d}  {reason}')

    if weak:
        weak.sort()
        print(f'\n  worst unmatched glyphs (a missing digit looks like this):')
        for iou, path, r in weak[:8]:
            got = ''.join(str(g['digit']) for g in r['glyphs'])
            scores = ' '.join(f"{g['digit']}@{g['iou']:.2f}" for g in r['glyphs'])
            print(f'      {os.path.relpath(path, ROOT):<58} '
                  f'best-guess {got}  [{scores}]')
        _dump_unmatched(weak)
    return 0


def _dump_unmatched(weak, limit=24):
    """A sheet of the glyphs that matched nothing, so the missing digits can
    be identified by eye and extracted."""
    seen, tiles = set(), []
    for iou, path, r in weak:
        frame = cv2.imread(path)
        if frame is None:
            continue
        for x, g in segment(ammo_crop(frame)):
            k = cv2.resize(g * 255, (12, 24)).tobytes()
            if k in seen:
                continue
            seen.add(k)
            tiles.append((g, path))
            if len(tiles) >= limit:
                break
        if len(tiles) >= limit:
            break
    if not tiles:
        return
    cols = min(len(tiles), 8)
    rows = (len(tiles) + cols - 1) // cols
    pad = 6
    sheet = np.zeros((rows * (CANVAS_H + pad) + pad,
                      cols * (CANVAS_W + pad) + pad), np.uint8)
    for i, (g, _) in enumerate(tiles):
        r_, c = divmod(i, cols)
        y = pad + r_ * (CANVAS_H + pad)
        x = pad + c * (CANVAS_W + pad)
        sheet[y:y + CANVAS_H, x:x + CANVAS_W] = _place(g) * 255
    out = os.path.join(ROOT, 'temp_debug', 'ammo_unmatched.png')
    cv2.imwrite(out, cv2.resize(sheet, None, fx=4, fy=4,
                                interpolation=cv2.INTER_NEAREST))
    print(f'      {len(tiles)} distinct unmatched shapes -> '
          f'{os.path.relpath(out, ROOT)}')


def extract(spec, write=False, force=False):
    if '=' not in spec:
        print(f'{spec!r}: expected <png>=<label>, e.g. shot.png=40')
        return 1
    path, label = spec.rsplit('=', 1)
    if not label.isdigit():
        print(f'{label!r} is not a digit string')
        return 1
    frame = cv2.imread(path if os.path.isabs(path) else os.path.join(ROOT, path))
    if frame is None:
        print(f'cannot read {path}')
        return 1

    found = segment(ammo_crop(frame))
    print(f'{os.path.basename(path)}: {len(found)} glyph(s), label {label!r}')
    for x, g in found:
        print(f'    x={x:3d}  {g.shape[1]}x{g.shape[0]}  '
              f'{int(g.sum())} px')
    if len(found) != len(label):
        print(f'  MISMATCH: {len(found)} glyphs vs {len(label)} label chars — '
              f'nothing written')
        return 1

    os.makedirs(ASSETS_DIR, exist_ok=True)
    plan = []
    for (x, g), ch in zip(found, label):
        out = os.path.join(ASSETS_DIR, f'digit_{ch}.png')
        exists = os.path.exists(out)
        plan.append((out, g, ch, exists))
        print(f'  {ch} -> {os.path.relpath(out, ROOT)}'
              f'{"   (exists)" if exists else ""}')

    clash = [p for p in plan if p[3]] if not force else []
    if clash:
        print('  already installed; pass --force to replace')
        return 1
    if not write:
        print('  dry run; pass --write to save')
        return 0
    for out, g, ch, _ in plan:
        cv2.imwrite(out, g * 255)
    det = AmmoDetector()
    got = det.read(ammo_crop(frame))
    print(f'  written. digits now {det.digits_known}; '
          f'this capture reads {got["value"]} '
          f'({got["reason"] or "clean"})')
    return 0 if str(got['value']) == label.lstrip('0') or \
        got['value'] == int(label) else 1


def confusion():
    """Every template against every other. The gap between a digit's self-score
    and its best impostor is what MIN_MARGIN has to clear."""
    det = AmmoDetector()
    ds = det.digits_known
    if len(ds) < 2:
        print(f'only {len(ds)} template(s) — nothing to confuse')
        return 1
    t = det._templates
    print('     ' + ''.join(f'{d:>7}' for d in ds))
    worst = (1.0, None)
    for a in ds:
        row = [f'{_best_iou(t[a], t[b]):.3f}' for b in ds]
        print(f'  {a}  ' + ''.join(f'{v:>7}' for v in row))
        off = [(_best_iou(t[a], t[b]), b) for b in ds if b != a]
        top = max(off)
        if 1.0 - top[0] < worst[0]:
            worst = (1.0 - top[0], (a, top[1], top[0]))
    a, b, s = worst[1]
    print(f'\n  tightest pair: {a} vs {b} at {s:.3f} — margin {worst[0]:.3f} '
          f'against a perfect self-match')
    return 0


def bench():
    import time
    det = AmmoDetector()
    frame = None
    for path in captures():
        f = cv2.imread(path)
        if f is not None and f.shape[:2] == (1440, 3440):
            frame = f
            break
    if frame is None:
        print('no capture to bench on')
        return 1
    crop = ammo_crop(frame).copy()
    for _ in range(20):
        det.read(crop)
    t0 = time.perf_counter()
    n = 500
    for _ in range(n):
        det.read(crop)
    dt = (time.perf_counter() - t0) / n * 1000
    print(f'{dt:.3f} ms per read  ({len(det.digits_known)} templates, '
          f'{len(segment(crop))} glyphs)')
    return 0


def selftest():
    """Does the collector's anchor check actually reject a shifted sequence?

    The whole safety argument for collect_ammo_digits.py is that a dropped or
    doubled round gets caught before anything is written. That claim is worth
    testing against captures with known contents rather than trusting — a
    validate() that silently passes everything looks identical to one that
    works, right up until it installs a wrong digit.
    """
    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    from collect_ammo_digits import validate

    def state(rel):
        frame = cv2.imread(os.path.join(ROOT, rel))
        if frame is None:
            # Say which frame and why, rather than dying on None[y:y+h]. The
            # 40-round fixture lived in temp_debug/, which was archived to
            # docs/attic/ on 2026-08-08 WITHOUT its screenshots — so this
            # refuses until someone re-grabs a frame reading 40.
            raise SystemExit(
                f'[!] selftest fixture missing: {rel}\n'
                f'    Re-grab a full-screen shot whose ammo counter reads 40 '
                f'(pixi run python tools/snap_on_key.py), drop it under '
                f'docs/, and point this line at it.')
        glyphs = segment(ammo_crop(frame))
        return [None, [[_place(g)] for _, g in glyphs]]

    s40 = state('temp_debug/screen_3440x1440_20260802_022612.png')
    s5 = state('calibration/artifacts/ads/runs/20260802_015545/iron/ads_v0_t0040.jpg')
    s8 = state('calibration/artifacts/ads/runs/20260801_222936/iron/ads_v0_t0040.jpg')

    cases = [
        ('truthful: 40 labelled 40',            [s40], 40, False),
        ('off by one: 40 labelled 41',          [s40], 41, True),
        ('wrong width: 40 labelled 100',        [s40], 100, True),
        ('truthful: 8 labelled 8',              [s8], 8, False),
        ('wrong digit: 8 labelled 6',           [s8], 6, True),
        ('gap in run: 8,5 labelled 8,7',        [s8, s5], 8, True),
        ('truthful pair: 5 then 5',             [s5, s5], 5, True),
    ]
    det = AmmoDetector()
    fails = 0
    for name, states, start, want_bad in cases:
        bad = validate(det, states, start)
        ok = bool(bad) == want_bad
        fails += not ok
        verdict = 'OK  ' if ok else 'FAIL'
        print(f'  {verdict} {name:<34} '
              f'{"rejected" if bad else "accepted":<9}'
              f'{"  " + bad[0][:70] if bad else ""}')
    print(f'\n{"all cases behaved" if not fails else f"{fails} case(s) wrong"}')
    return 1 if fails else 0


def main():
    argv = sys.argv[1:]
    if '--selftest' in argv:
        return selftest()
    if '--extract' in argv:
        i = argv.index('--extract')
        return extract(argv[i + 1], write='--write' in argv,
                       force='--force' in argv)
    if '--confusion' in argv:
        return confusion()
    if '--bench' in argv:
        return bench()
    return scan()


if __name__ == '__main__':
    sys.exit(main())
