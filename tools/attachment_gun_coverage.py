"""Which attachment templates have only ever been photographed on ONE gun.

    pixi run att-coverage
    pixi run att-coverage --slot grip      # keys containing 'grip'

A slot crop is the icon composited into THAT WEAPON's tile. The tile geometry
is the same everywhere, so in principle the host gun should not matter -- but
that is a claim, and until a part has been photographed in two different
weapons' racks nothing in this repo has ever checked it. The tile is
translucent: `blend_attachment` carries 0.37*blur(background) through from
whatever sits behind it, and behind it is a different weapon silhouette.

The parsing lives in `calibration/collect_templates.py` (`gun_of`,
`slot_coverage`) because that is the module that WRITES the name and the field.
This file only reports; a second copy of the naming convention here is exactly
the drift that would make the two disagree silently.

ROWS ARE EXCLUDED AND THAT IS NOT AN OVERSIGHT. A 库存 row is the part lying
in the backpack with no weapon involved, so "which gun" has no referent there;
counting rows as gun coverage would report the uzi_stock corpus as broad when
every one of its samples is the same rendering. The row bank is scored by
`pixi run attachments`, which is where it belongs.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from calibration.legacy_collect_templates import slot_coverage
from detector.attachment_catalog import ATTACHMENTS, ROSTER, fits


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--slot', help='only assets whose key contains this')
    args = ap.parse_args()

    per_asset = slot_coverage()
    if args.slot:
        per_asset = {k: v for k, v in per_asset.items() if args.slot in k}
    if not per_asset:
        print('no ground-truth slot samples on disk')
        return 1

    guns = sorted({g for v in per_asset.values() for g in v})
    print(f'{len(per_asset)} parts photographed on {len(guns)} of '
          f'{len(ROSTER)} guns: {" ".join(guns)}\n')
    print(f'{"asset":16} {"guns":>4} {"crops":>6} {"could":>6}  seen on')
    rows = sorted(per_asset.items(),
                  key=lambda kv: (len(kv[1]), -sum(kv[1].values())))
    for asset, counts in rows:
        seen = ' '.join(f'{g}x{n}' for g, n in
                        sorted(counts.items(), key=lambda kv: -kv[1]))
        # How many hosts EXIST for this part -- the ceiling on the column to
        # its left. A part on 1 gun out of 1 possible is finished; a part on 1
        # out of 22 has never been asked the question.
        could = sum(1 for w in ROSTER if fits(w, asset)) if asset in ATTACHMENTS else 0
        print(f'{asset:16} {len(counts):4d} {sum(counts.values()):6d} '
              f'{could:6d}  {seen}')

    lonely = sorted(a for a, c in per_asset.items()
                    if len(c) == 1
                    and sum(1 for w in ROSTER if fits(w, a)) > 1)
    print(f'\n{len(lonely)} parts stand on ONE gun while more than one could '
          f'host them:\n  {" ".join(lonely)}')
    print('\nextend with:  pixi run python calibration/collect_templates.py '
          '--all --spread 2 --plan')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
