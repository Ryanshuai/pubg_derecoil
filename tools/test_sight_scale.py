"""An optic is a SCALAR, so one measured cell covers every optic. Offline.

    pixi run sight-scale

⚠ WHAT THIS GATE IS FOR. Under plan A the curve store is keyed on the sight,
and 72 of the 76 curves on disk are red-dot -- so putting any magnified optic
on any gun missed the lookup and the tool printed `no fitted curve ... NOT
compensating`. That line is ALSO what a genuinely unmeasured kit prints, which
is why it survived: "I put a 4x on and it stopped holding the gun down" and
"nobody has fired this combination" are the same sentence from the log.

Posture already had this fallback and the optic did not, for no reason either
one can state -- both are believed to scale the whole trajectory by one number.

BOTH DIRECTIONS ARE CHECKED, and the second is the one that keeps it honest. A
gate that only proves "a 4x gets compensated" is passed by falling back to ANY
curve at all, which is the 1521-against-895 failure this repository has already
paid for. So it also pins that a MEASURED cell wins, that the three axes which
change the curve's SHAPE never substitute, and that an optic with no ratio --
iron sights, an unreadable tile -- still refuses.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import config                                             # noqa: E402
from detector.weapon import Weapon                        # noqa: E402

FAILS = []

RED_DOT = 'Upper_DotSight_01_C'
AIMPOINT_2X = 'Upper_Aimpoint_C'
ACOG_4X = 'Upper_ACOG_01_C'
PM2_15X = 'Upper_PM2_01_C'      # deliberately absent from RECOIL_SIGHT_RATIO


def check(what, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {what:<54} {got!r}'
          + ('' if ok else f'  != {want!r}'))
    if not ok:
        FAILS.append(what)


def close(what, got, want, tol=0.5):
    ok = abs(got - want) <= tol
    print(f'  {"ok  " if ok else "FAIL"}  {what:<54} {got:.1f}'
          + ('' if ok else f'  != {want:.1f} (+-{tol})'))
    if not ok:
        FAILS.append(what)


def counts(name, scope='', posture='standing', **kit):
    w = Weapon()
    w.set('name', name)
    for slot in ('muzzle', 'grip', 'butt'):
        w.set(slot, kit.get(slot, ''))
    w.set('scope', scope)
    w.set('posture', posture)
    w.set_seq()
    return round(sum(w.dy_s), 1)


print('=== the store is red-dot-only, which is what makes this matter ===')
base = counts('m416', RED_DOT)
check('m416 bare at the red dot is measured', base > 0, True)

print('\n=== a magnified optic is the same curve times one number ===')
for tag, asset in (('2x', AIMPOINT_2X), ('4x', ACOG_4X)):
    r = config.RECOIL_SIGHT_RATIO[tag]
    close(f'm416 bare at {tag} = red dot x{r}', counts('m416', asset), base * r)

print('\n=== and it composes with posture, both factors on one curve ===')
r4 = config.RECOIL_SIGHT_RATIO['4x']
pf = config.POSTURE_FACTOR['crouching']
close('m416 bare crouching at 4x = red dot x posture x optic',
      counts('m416', ACOG_4X, 'crouching'), base * pf * r4)

print('\n=== a MEASURED cell beats the table, always ===')
# ⚠ THE TABLE IS A PRIOR AND IT IS KNOWN TO BE WRONG -- the mp5k's own 2x
# measures 0.882 where the derivation said 1.689. If a derivation could ever
# win over a fit, the store's best evidence would be unreachable through the
# exact optic it was gathered on.
mp5k_red = counts('mp5k', RED_DOT)
mp5k_2x = counts('mp5k', AIMPOINT_2X)
check('mp5k bare at 2x is a fitted curve, not a scaled red dot',
      abs(mp5k_2x - mp5k_red * config.RECOIL_SIGHT_RATIO['2x']) > 1.0, True)

print('\n=== an optic with no ratio derives NOTHING ===')
# `iron` (empty scope slot) and `15x` are both absent from the table on
# purpose: an empty slot is not a red dot at a third of the sensitivity, and a
# magnification nobody has priced is not one to guess at. Refusing prints one
# line and compensates nothing, which is the honest state.
check('m416 with no optic at all', counts('m416', ''), 0.0)
check('m416 through a 15x', counts('m416', PM2_15X), 0.0)

print('\n=== the three axes that change the SHAPE never substitute ===')
# A scalar cannot turn one gun's trajectory into another's, one kit's into
# another's, or one cyclic rate's into another's. Only posture and optic are
# claimed to be scalars on the same curve.
check('a gun with no curve at any optic stays at zero',
      counts('win94', RED_DOT), 0.0)
# An m416 wearing a part nothing has been fired with: same gun, same optic,
# and still nothing, because the kit changes the curve rather than scaling it.
check('an unmeasured KIT is not derived from the bare curve',
      counts('m416', RED_DOT, muzzle='Muzzle_Suppressor_Large_C',
             grip='Lower_ThumbGrip_C', butt='Stock_Heavy_C'), 0.0)

print('\n=== the ratio table itself ===')
check('red dot is the reference and is exactly 1',
      config.RECOIL_SIGHT_RATIO['red_dot'], 1.0)
# ⚠ PINNED AGAINST THE MEASUREMENT, NOT AGAINST ITSELF. 1.689 is the value the
# refuted derivation gave for the 2x; the mp5k's nulled magazines measure
# 0.882. A table that drifts back above 1.2 has been re-derived rather than
# re-measured, and the gun goes into the ground.
check('2x is the measured ratio, not the derived one',
      config.RECOIL_SIGHT_RATIO['2x'] < 1.2, True)

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
    sys.exit(1)
print('all ok')
