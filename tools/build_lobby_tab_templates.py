"""Cut lobby tab-label masks out of a full-screen lobby capture. Offline.

    pixi run python tools/build_lobby_tab_templates.py <shot.png> --tag zh
    pixi run python tools/build_lobby_tab_templates.py <shot.png> --tag zh --write
    pixi run python tools/build_lobby_tab_templates.py --verify

WHY THIS EXISTS. `detector/lobby_nav` used to name tabs BY POSITION: segment
the bright mask over the bar, call run i `TOP_TABS[i]`. That holds only while
the segment count is exactly right, and on the Chinese client it is not —
Chinese labels are narrower, so the green event icon right of 商店 fits inside
`LOBBY_TOP_BAR_ROI` and the bar segments into 8 runs where 7 are expected.
`read_page` then returns None, and `ensure_mode` → `press_play` →
`ensure_in_match` refuse to act. Measured 2026-08-05: three attempts, three
refusals, "top bar unreadable — a dialog may be over it", with no dialog over
the bar at all.

Naming by glyph instead makes the extra run a run that matches nothing, which
is what it is. It also means a game update that REORDERS the bar renames
nothing silently — the old scheme's worst failure, since every name after the
moved one would be wrong and nothing would say so.

WHAT A TEMPLATE IS HERE. The same thing as everywhere else in this repo: one
binary mask, thresholded at `LOBBY_TAB_FIND_THRESH`, matched with
TM_CCOEFF_NORMED. Not OCR. Variants follow the `<stem>.<tag>.png` convention
that `lobby_detector._load_template` and the weapon name plates already use,
and for the same reason — which language the client runs in is not a property
of this repository.

BOOTSTRAPPING, AND WHY IT IS NOT CIRCULAR. The first templates for a language
have to be cut by position, because that is all there is before templates
exist. That is safe ONLY when a human has looked at the strip and confirmed
each run is the label the position claims — `--dump` writes that strip. It was
done once for zh on docs/state_dumps/0805_091011/before.png: 开始游戏 通行证
战绩 仓库 藏匿处 工坊 商店, then the icon. Afterwards the templates are the
authority and position is not consulted again.

SELECTED vs UNSELECTED IS A SECOND RENDERING, and one variant does not cover
both. The selected tab is white text on a light box, and at the find threshold
the box's dither leaks into the mask underneath the glyphs. Measured on the two
English captures, which happen to differ only in which mode is selected:

    template cut from       scored on the other capture
    lobby.png (TRAINING)    TRAINING 0.751, runner-up 0.587 — 1.28x
    lobby.png (PLAY sel.)   NORMAL   0.737, runner-up 0.390 — 1.89x
    everything else         0.996 .. 1.000

1.28x is not a gap worth resting a refusal on. So both renderings are stored,
as tagged variants of the same label, and best-of picks whichever is up. The
tag therefore names the RENDERING, not only the language: `en` and `en_alt` are
one client in two selection states. The zh side currently has one capture, so
only 开始游戏 and 训练 are known selected — the rest will need a second capture
if they are ever read while selected.

`--verify` scores every template against every run of every capture on disk and
reports BOTH sides of the gate: the lowest winning score, and the highest score
reached by a run that matched nothing. A gate is only worth the distance
between those two.
"""
import argparse
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import cv2                                                    # noqa: E402
import numpy as np                                            # noqa: E402

from config import (LOBBY_SUB_BAR_ROI, LOBBY_TAB_FIND_THRESH,  # noqa: E402
                    LOBBY_TOP_BAR_ROI)
from detector.lobby_nav import (SUB_TABS, TAB_TMPL_DIR, TAB_TMPL_MIN,
                                TOP_TABS, bar_labels, load_tab_templates,
                                name_labels)                  # noqa: E402

BARS = (('top', LOBBY_TOP_BAR_ROI, TOP_TABS),
        ('sub', LOBBY_SUB_BAR_ROI, SUB_TABS))

# Every full-screen lobby capture the repo keeps, for --verify.
VERIFY_GLOBS = ('docs/lobby/*.png', 'docs/state_dumps/*/before.png',
                'docs/lobby/runs/*/*.png')


def _mask(gray, roi, box):
    y, _, h, _ = roi
    strip = gray[y:y + h, box['x0']:box['x1']]
    return ((strip > LOBBY_TAB_FIND_THRESH) * 255).astype(np.uint8)


def _trim(m):
    """Drop all-dark rows/cols so the template is the glyph block, not the ROI.

    Without this every template is exactly the ROI height and the vertical
    freedom matchTemplate needs to find the glyphs is gone.
    """
    ys = np.flatnonzero(m.sum(axis=1) > 0)
    xs = np.flatnonzero(m.sum(axis=0) > 0)
    if not len(ys) or not len(xs):
        return None
    return m[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1]


def cut(shot, tag, write=False, dump=None):
    img = cv2.imread(shot)
    if img is None:
        print(f'cannot read {shot}')
        return 1
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    tiles, rc = [], 0

    for bar, roi, names in BARS:
        found = bar_labels(gray, roi)
        print(f'\n{bar}: {len(found)} runs, {len(names)} names expected')
        if len(found) < len(names):
            print(f'  !! fewer runs than names — cannot cut {bar} from this '
                  f'capture, the bar is covered or the ROI is wrong')
            rc = 1
            continue
        if len(found) > len(names):
            print(f'  .. {len(found) - len(names)} extra run(s); taking the '
                  f'first {len(names)} left to right. CHECK THE STRIP.')

        for i, name in enumerate(names):
            box = found[i]
            m = _trim(_mask(gray, roi, box))
            if m is None:
                print(f'  {name:<10} empty mask — skipped')
                rc = 1
                continue
            print(f'  {name:<10} x {box["x0"]}..{box["x1"]}  mask {m.shape[1]}'
                  f'x{m.shape[0]}  ink {int(m.sum() // 255)}')
            if write:
                out = os.path.join(TAB_TMPL_DIR, f'{bar}_{name}.{tag}.png')
                os.makedirs(TAB_TMPL_DIR, exist_ok=True)
                cv2.imwrite(out, m)
            if dump:
                t = cv2.cvtColor(m, cv2.COLOR_GRAY2BGR)
                t = cv2.copyMakeBorder(t, 6, 24, 6, 6, cv2.BORDER_CONSTANT,
                                       value=(0, 0, 0))
                cv2.putText(t, name, (4, t.shape[0] - 7),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
                tiles.append(t)

    if dump and tiles:
        h = max(t.shape[0] for t in tiles)
        tiles = [cv2.copyMakeBorder(t, 0, h - t.shape[0], 0, 8,
                                    cv2.BORDER_CONSTANT, value=(40, 40, 40))
                 for t in tiles]
        cv2.imwrite(dump, cv2.resize(np.hstack(tiles), None, fx=2.0, fy=2.0,
                                     interpolation=cv2.INTER_NEAREST))
        print(f'\nstrip -> {dump}   LOOK AT IT before --write')
    if write:
        print(f'\nwrote -> {TAB_TMPL_DIR}')
    return rc


def verify():
    """Score the stored templates against every lobby capture on disk.

    Reports per capture: which names were found, the winning score of each,
    and the runner-up score, because the gate is only worth what the gap
    behind it is. A run that matches nothing is expected (the event icon) and
    is listed as `-` rather than as a failure.
    """
    tmpls = load_tab_templates()
    if not tmpls:
        print(f'no templates in {TAB_TMPL_DIR}')
        return 1

    by_bar = {}
    for key in tmpls:
        by_bar.setdefault(key.split('_', 1)[0], []).append(key)
    for bar in sorted(by_bar):
        print(f'{bar}: {len(by_bar[bar])} templates '
              f'({", ".join(sorted(k.split("_", 1)[1] for k in by_bar[bar]))})')

    shots = sorted({p for g in VERIFY_GLOBS
                    for p in glob.glob(os.path.join(ROOT, g))})
    print(f'\n{len(shots)} capture(s)\n')
    rc, worst, loudest = 0, 1.0, [0.0]
    for shot in shots:
        img = cv2.imread(shot)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        rel = os.path.relpath(shot, ROOT).replace('\\', '/')
        lines, complete = [], True
        for bar, roi, names in BARS:
            found = name_labels(gray, roi, bar, tmpls)
            got = [s for s in found if s.get('name')]
            hit = {s['name'] for s in got}
            missing = [n for n in names if n not in hit]
            if got:
                worst = min(worst, min(s['tmpl_score'] for s in got))
            # The other side of the gate: how close a run that matched nothing
            # got to being named. Only meaningful on captures where the bar is
            # actually up — elsewhere there are no runs at all.
            for s in found:
                if not s.get('name') and hit:
                    loudest[0] = max(loudest[0], s['tmpl_best'])
                    lines.append(f'      {"(unmatched)":<10} '
                                 f'best {s["tmpl_best"]:.3f}  '
                                 f'x {s["x0"]}..{s["x1"]}')
            lines.append(f'    {bar}: {len(got)}/{len(names)} named, '
                         f'{len(found) - len(got)} unmatched run(s)'
                         + (f'  MISSING {missing}' if missing else ''))
            for s in got:
                lines.append(f'      {s["name"]:<10} {s["tmpl_score"]:.3f}'
                             f'  (2nd {s["tmpl_second"]:.3f})')
            if missing:
                complete = False
        # A capture that is not the lobby has no bars to read; that is not a
        # failure of the templates, so only say so.
        print(f'{"ok " if complete else "-- "}{rel}')
        for ln in lines:
            print(ln)
    # NOT a hold-out: every lobby capture on disk has contributed a template,
    # so the winning scores are near 1.000 by construction and say nothing
    # about an unseen frame. The hold-out number is in the docstring — 0.737,
    # measured while only one of the two English captures had been cut. What
    # this report is actually good for is the OTHER side: what a run with no
    # label reaches, which no template can inflate.
    print(f'\nlowest winning score  {worst:.3f}  (self-scored, see the source)')
    print(f'highest nameless run  {loudest[0]:.3f}')
    print(f'gate                  {TAB_TMPL_MIN}')
    if worst < TAB_TMPL_MIN:
        print('!! a real label scores below the gate — it would be dropped')
        rc = 1
    if loudest[0] >= TAB_TMPL_MIN:
        print('!! something with no label reaches the gate — it would be named')
        rc = 1
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('shot', nargs='?', help='full-screen lobby capture')
    ap.add_argument('--tag', default='zh', help='UI language tag, e.g. zh, en')
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--dump', default=os.path.join(ROOT, 'temp_debug',
                                                   'lobby_tab_templates.png'))
    ap.add_argument('--verify', action='store_true')
    args = ap.parse_args()

    if args.verify:
        return verify()
    if not args.shot:
        ap.error('give a capture, or --verify')
    return cut(args.shot, args.tag, args.write, args.dump)


if __name__ == '__main__':
    sys.exit(main())
