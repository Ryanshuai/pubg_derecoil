"""Every captured name plate vs the templates in the repo. Offline, no game.

    pixi run name-plates

456 crops, 40 weapons, 10 backgrounds each. Green means every plate reads as
the weapon that was spawned into that rack row.

WHY THE LABELS CAN BE TRUSTED, which is the only thing that makes this a test
rather than a tautology. The corpus comes from
`collect_templates.py --plates`, and each crop is labelled by WHAT WAS
REQUESTED FROM THE SPAWNER, confirmed by an addressing chain that never reads a
plate:

    both rack rows emptied and confirmed at 0 ink
    two guns spawned in one panel visit, in a known click order
    first click -> row 1, second -> row 2, both rows then confirmed to carry ink

Grading a plate reader against labels the plate reader produced would be
circular, and that circularity is exactly what the whole collection chain was
built to avoid.

WHAT IT CATCHES. The prefix confusion this test was written for: 'M24' is a
literal prefix of 'M249', the M24 template lands on the first three glyphs of an
M249 plate, and the windowed IoU cannot see the '9' because it falls outside
the matched window. It read m249 as unnamed on 2 of 10 backgrounds and tied on
a third. TIE_MARGIN in the detector is the fix; this is what proves it stays
fixed, and would catch the next such pair when the game adds one.

It will also catch a template going stale after a game update, which is the
first rule in detector/CLAUDE.md and does not otherwise announce itself: a
drifted template does not error, it returns a confident wrong answer.

Rebuild the masks with calibration/build_name_templates.py.
"""
import collections
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import cv2                                                    # noqa: E402

from detector.weapon_template_detector import (                # noqa: E402
    TMPL_THRESHOLD, TabWeaponDetector, _template_match)

RUNS = os.path.join(ROOT, 'calibration', 'artifacts', 'attachments', 'runs')

# Below this the corpus itself is the problem, not the detector — say so
# rather than passing on three crops.
MIN_CROPS = 100


def main():
    det = TabWeaponDetector()
    n = collections.Counter()
    ok = collections.Counter()
    bad = collections.Counter()
    for man in sorted(glob.glob(os.path.join(RUNS, '*', 'manifest.json'))):
        m = json.load(open(man, encoding='utf-8'))
        for e in m['entries']:
            lab = [l for l in e.get('labels', []) if l.get('slot') == 'plate']
            if not lab:
                continue
            crop = cv2.imread(os.path.join(os.path.dirname(man), e['capture']))
            if crop is None:
                continue
            want = lab[0]['asset']
            hits = _template_match(crop, det._templates)
            got = hits[0][1] if hits and hits[0][0] >= TMPL_THRESHOLD else ''
            n[want] += 1
            ok[want] += (got == want)
            if got != want:
                bad[(want, got)] += 1

    total, right = sum(n.values()), sum(ok.values())
    print(f'{total} labelled plates, {len(n)} weapons')
    if total < MIN_CROPS:
        print(f'\n[!] only {total} crops under {os.path.relpath(RUNS, ROOT)} — '
              f'run `collect_templates.py --plates` before trusting this')
        return 1
    if not bad:
        print(f'\nall {right} read as the weapon that was spawned')
        return 0
    print(f'\n{total - right} of {total} misread:')
    for (want, got), c in bad.most_common(20):
        print(f'  {want:<10} -> {got or "<nothing>":<12} x{c}   '
              f'({ok[want]}/{n[want]} right)')
    return 1


if __name__ == '__main__':
    sys.exit(main())
