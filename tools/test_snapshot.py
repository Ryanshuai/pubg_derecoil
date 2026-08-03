"""Sidecars, and the ground-truth assertions they buy. Offline.

    pixi run snaps

Two things are checked, and the second is the one that matters.

FIRST, the format refuses to guess. A label with no `source` is rejected
rather than defaulted, because there is no safe default for "did anyone
actually look" — defaulting to REQUESTED is what turned two ADS runs into
confidently-wrong ground truth (calibration/capture_run.py's docstring has the
account), and defaulting to DETECTED would quietly downgrade real truth.

SECOND, THE ASSERTION CAN FAIL. A regression check that cannot fail is worth
nothing, and "3/3 crops read correctly" looks identical whether the checker is
discriminating or returning the expected answer by construction. So this feeds
each checker a crop it should NOT match and requires a mismatch.

That is the whole difference the sidecar buys. tools/regression_check.py's
older pass compares against last time, which catches a library bump; this
compares against the truth, which catches a wrong answer. detector/CLAUDE.md's
first iron law is that template drift is SILENT — a drifted template returns a
plausible in-catalogue answer, not an error — and only the second kind of
check sees that.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import cv2
import numpy as np

from detector.snapshot import (BadLabel, DETECTED, KIND_CROP, REQUESTED,
                               read_sidecar, readings, sidecar_path, snap,
                               truth)

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {name:<54} {got!r}'
          + ('' if ok else f'  != {want!r}'))
    if not ok:
        FAILS.append(name)


SCRATCH = os.path.join(ROOT, 'docs', 'tab', 'truth', '_selftest')

print('\n=== the sidecar round-trips, and keeps what it was not told to read ===')
img = np.zeros((8, 8, 3), np.uint8)
p = os.path.join(SCRATCH, 'x.png')
meta = snap(img, p, kind=KIND_CROP, screen='tab', roi=(1, 2, 3, 4),
            parent='00_baseline.png', source='a test',
            labels=[{'target': 'weapon_name', 'value': 'g36c',
                     'source': REQUESTED},
                    {'target': 'weapon_name', 'value': 'sks',
                     'source': DETECTED}],
            state={'gun': 'g36c'}, anything='kept verbatim')
back = read_sidecar(p)
check('written and read back', back == meta, True)
check('roi survives', back['roi'], [1, 2, 3, 4])
check('an unknown key is kept, not dropped', back['anything'], 'kept verbatim')
check('state is dumped, not interpreted', back['state'], {'gun': 'g36c'})
check('sidecar sits beside the image',
      os.path.basename(sidecar_path(p)), 'x.json')

print('\n=== truth() never hands back a detector reading ===')
# The same reason CaptureRun.labelled() refuses: a detector's own answer
# cannot be the truth it is judged against.
check('truth() returns only REQUESTED',
      [l['value'] for l in truth(back)], ['g36c'])
check('readings() returns only DETECTED',
      [l['value'] for l in readings(back)], ['sks'])
check('truth() filters by target', truth(back, 'tab_open'), [])
check('no labels at all -> no truth', truth({'labels': []}), [])
check('no sidecar -> no truth', truth(None), [])

print('\n=== a label that cannot say who looked is REFUSED ===')
for bad in ({'target': 'weapon_name', 'value': 'g36c'},          # no source
            {'target': 'weapon_name', 'source': REQUESTED},      # no value
            {'value': 'g36c', 'source': REQUESTED},              # no target
            {'target': 'w', 'value': 'v', 'source': 'assumed'}):  # bad source
    try:
        snap(img, os.path.join(SCRATCH, 'bad.png'), labels=[bad])
        got = 'ACCEPTED'
    except BadLabel:
        got = 'refused'
    check(f'{bad}', got, 'refused')

for f in ('x.png', 'x.json', 'bad.png', 'bad.json'):
    try:
        os.remove(os.path.join(SCRATCH, f))
    except OSError:
        pass
try:
    os.rmdir(SCRATCH)
except OSError:
    pass

print('\n=== the real crops on disk read correctly ===')
from tools.regression_check import _checkers, check_labels

ok, bad, unchecked = check_labels()
check('every ground-truth crop passes', bad, [])
check('and there is at least one', ok > 0, True)
check('none of them went unchecked', unchecked, 0)

print('\n=== ...AND THE CHECK CAN FAIL. This is the point. ===')
# "3/3 correct" reads the same whether the checker discriminates or just
# returns whatever was expected. Feed each one a crop that is NOT its answer
# and require a mismatch. Without this the whole pass is decoration.
CROPS = os.path.join(ROOT, 'docs', 'tab', 'truth')
cks = _checkers()
g1 = cv2.imread(os.path.join(CROPS, 'tab_inventory_gun_name_1.png'))
g2 = cv2.imread(os.path.join(CROPS, 'tab_inventory_gun_name_2.png'))
ty = cv2.imread(os.path.join(CROPS, 'tab_inventory_type.png'))
if g1 is None or g2 is None or ty is None:
    check('the truth crops are on disk', False, True)
else:
    check('gun 1 reads g36c', cks['weapon_name'](g1), 'g36c')
    check('gun 2 reads sks', cks['weapon_name'](g2), 'sks')
    check('the two plates do NOT read the same',
          cks['weapon_name'](g1) != cks['weapon_name'](g2), True)
    # A wrong label must be caught, not absorbed.
    check("labelling gun 1 as 'sks' would FAIL",
          cks['weapon_name'](g1) == 'sks', False)
    check('the 类型 header reads open', cks['tab_open'](ty), True)
    # Black is what that region holds when Tab is shut. If this said True the
    # tab_open checker would pass anything.
    check('a black crop reads shut',
          cks['tab_open'](np.zeros_like(ty)), False)

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS[:6])}'
          + (' ...' if len(FAILS) > 6 else ''))
    sys.exit(1)
print('all ok')
