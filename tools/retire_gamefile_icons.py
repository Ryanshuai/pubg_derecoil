"""Move the game-file icons out of the bank, leaving the photographed ones.

    pixi run python tools/retire_gamefile_icons.py            # what would move
    pixi run python tools/retire_gamefile_icons.py --apply
    pixi run python tools/retire_gamefile_icons.py --restore

Each asset can hold three pictures: the icon extracted from the game files
(`<asset>.png`), and two photographed off the screen by collect_templates --
`<asset>.solved.png` for the slot tile and `<asset>.row.png` for the inventory
row. The reader only ever sees screen crops, so the extracted render is the one
picture in the bank that comes from a different domain: another resolution,
another light, no panel behind it.

It is also the OLDEST. The extracts are dated 2026-03-18, before update 41.1
removed the Angled Foregrip and added a Tilted Grip -- so at least one of them
is a picture of an item the game no longer has, sitting in the bank under a key
the spawner still fills with something else.

⚠ IT IS NOT A SPARE COPY. AttachmentDetector.best_two ranks its shortlist on
variant 0, and variant 0 is the untagged file because the loader walks the
directory sorted. Taking these out changes which picture decides who gets
scored properly, for every asset at once. That is a measurement, not a tidy-up:
run `tools/score_attachments.py` before and after and compare against its
BASELINE ratchet.

NOTHING IS DELETED. Files move to a subdirectory, which the loader does not
walk (os.listdir, no recursion), so they are out of the bank and still on disk.

⚠ AN ASSET NEEDS BOTH PHOTOGRAPHS BEFORE ITS ART CAN GO, and that rule was
learned the expensive way. Retiring on `.solved` alone scored better on the
corpus and then BROKE THE COLLECTOR: a uzi's own quick-draw magazine measured
175 against a solved-only template, over SlotDetector's gate of 150, so the slot
read `empty`, `strip` skipped it, the magazine stayed on the gun, and every
magazine part that round was bounced into 库存 under "the slot reads empty, so
the autofit rule is what does not hold here". Four parts died that way in one
run (docs/attachments/runs/20260804_224201).

That is a circle -- the weak templates can only be replaced by running the
collector, and the collector cannot run with them. The way out is ordered: keep
the art, collect BOTH `.solved` and `.row`, then retire and re-measure.

An asset with NO photograph at all is left alone for the same reason one step
further along: removing its only picture blinds the reader to that part.
"""
import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detector.attachment_detector import TMPL_DIR

RETIRED = os.path.join(TMPL_DIR, '_gamefile_icons')
VARIANTS = ('.solved.png', '.row.png')


def survey():
    """-> (movable, orphans). Both are lists of asset stems."""
    files = [f for f in os.listdir(TMPL_DIR) if f.endswith('.png')]
    stems = set()
    shots = {}
    for f in files:
        for suf in VARIANTS:
            if f.endswith(suf):
                shots.setdefault(f[:-len(suf)], []).append(suf)
                break
        else:
            stems.add(f[:-4])
    movable = sorted(s for s in stems if len(shots.get(s, ())) == len(VARIANTS))
    thin = sorted(s for s in stems if 0 < len(shots.get(s, ())) < len(VARIANTS))
    orphans = sorted(s for s in stems if s not in shots)
    return movable, thin, orphans, shots


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--restore', action='store_true')
    args = ap.parse_args()

    if args.restore:
        if not os.path.isdir(RETIRED):
            print('nothing retired')
            return 0
        back = sorted(os.listdir(RETIRED))
        for f in back:
            shutil.move(os.path.join(RETIRED, f), os.path.join(TMPL_DIR, f))
        os.rmdir(RETIRED)
        print(f'restored {len(back)} game-file icon(s) to the bank')
        return 0

    movable, thin, orphans, shots = survey()
    print(f'{len(movable)} asset(s) have BOTH photographs and would give up '
          f'their game-file icon')
    if thin:
        print(f'\n{len(thin)} have only ONE — LEFT IN. Collect the missing '
              f'variant first; the docstring says what a thin template did to '
              f'the collector:')
        for st in thin:
            miss = [v for v in VARIANTS if v not in shots[st]]
            print(f'  {st:<52} has {shots[st][0]:<12} needs {miss[0]}')
    if orphans:
        print(f'\n{len(orphans)} have NO photograph — LEFT IN, the extract is '
              f'their only picture:')
        for st in orphans:
            print(f'  {st}')

    if not args.apply:
        print(f'\n--apply to move them to {os.path.basename(RETIRED)}/, then '
              f'`pixi run attachments` and compare the ratchet.')
        return 0

    os.makedirs(RETIRED, exist_ok=True)
    for s in movable:
        src = os.path.join(TMPL_DIR, f'{s}.png')
        shutil.move(src, os.path.join(RETIRED, f'{s}.png'))
    print(f'\nmoved {len(movable)} icon(s) -> {RETIRED}')
    print('now: pixi run python tools/score_attachments.py')
    return 0


if __name__ == '__main__':
    sys.exit(main())
