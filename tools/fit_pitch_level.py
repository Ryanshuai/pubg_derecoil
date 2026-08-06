"""Where should a cell aim? Measure it once, store it, stop scanning.

    pixi run python tools/fit_pitch_level.py --postures standing --bearings 4
    pixi run python tools/fit_pitch_level.py --postures standing,crouching,prone --write

WHAT IS WRONG NOW. `ViewDriver.goto_level(posture)` reads `level_up` out of
docs/pitch/pitch_range.json, and that file has NO USABLE ENTRIES — its own
`_note` says so. So goto_level returns 0, every cell falls back to
`calibrate_pitch()`, and that scan is the thing being replaced:

  * it costs ~20 s of very visible sweeping PER CELL
  * it is NOT REPEATABLE. It keeps whatever pitch happened to have texture,
    which depends on where the character is facing, so the same posture came
    back 100..1900 one run and 800..2200 the next. Two cells aimed at
    different pitches measure different recoil and nothing recorded which was
    which.
  * on a MAGNIFIED sight it fails outright — "no part of the pitch range
    tracks" — because at 4x the sweep is through scope body and blurred
    periphery. That took out every vss and every 4x cell on 2026-08-05.

WHY A STORED CONSTANT FIXES THE REPEATABILITY BY CONSTRUCTION. The bottom
clamp is absolute: push down further than the travel can be and the view is
against it, wherever it started (home_to_clamp's own argument). An offset
from that stop is therefore an absolute aim, and a CONSTANT one is the same
aim every time by definition — which is the property the scan cannot have.

WHY THE INTERSECTION AND NOT THE MEAN. The usable band moves with the
bearing, because what has texture moves with the bearing. A mean can sit
outside a band that some bearing actually has; the INTERSECTION is usable at
every bearing measured, which is the claim worth storing. If the intersection
comes out empty, that is a real answer — this scene has no pitch that works
from every direction — and it is printed rather than averaged away.

⚠ THIS DOES NOT CLAIM TO BE LEVEL, and goto_level's docstring saying "this
puts the view level" is the part that misleads. What a measurement needs is
an aim that is REPEATABLE and has TEXTURE; being level is neither necessary
nor checkable here. The removed entries came from
`probe_pitch_range.horizon_row()`, which scans the middle half of the screen
for the first row with detail and finds the COMPASS STRIP every time — it
returned row 1-24 of a 1440-tall frame. Nothing here uses it.

⚠ AND A STORED CONSTANT CAN STILL LAND SOMEWHERE FEATURELESS on a bearing
this never saw. That is caught rather than silently measured: harvest and
sweep call `tracking_confirmed()` before magazine 0 as of 2026-08-05, which
moves the view a known amount and asks whether the reading followed.

⚠ THE FIRST RUN OF THIS REPORTED "nothing tracks" AT EVERY BEARING AND I
BLAMED THE SCENE — committed it, too: "the character stands on a large flat
concrete pad, that is the scene, not the method". It was not. The rack still
held the previous run's VSS, so the red dot profile's seven patches were
sitting on that gun's integral scope body, and `ensure_weapon_in_hand` only
checked that A weapon was out. The tell was in the output the whole time —
`holding m416 in slot 2, 22 rounds`, and 22 is the VSS's magazine.

That is why `band_at` prints every reading rather than a verdict. Four words
of "nothing tracks" is what let a wrong story stand for an hour; thirty
numbers would have shown the view never moved at all, which is a different
fault from the correlator failing to follow it.
"""
import argparse
import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from calibration.sweep import Rig                                  # noqa: E402
from control.aim import BAND_STEP, BAND_MAX, BAND_TRACK_FRAC       # noqa: E402
from control.inventory import InventoryControl                     # noqa: E402
from control.session import ensure_ready                           # noqa: E402
from control.spawner import SpawnerControl                         # noqa: E402
from control.stock import ensure_weapon_in_hand                    # noqa: E402
from press.pico_mouse import other_agents                          # noqa: E402

OUT = os.path.join(ROOT, 'docs', 'pitch', 'pitch_range.json')
# Yaw between bearings, in counts. Four of these walks most of the way round.
BEARING_COUNTS = 2600


def band_at(rig, step=BAND_STEP, verbose=True):
    """Rise from the bottom clamp and report which rises tracked.
    -> (usable_counts, per_step_readings)

    Deliberately a copy of what ViewDriver.calibrate_pitch does rather than a
    call to it: that one also MOVES to the centre and stores state on the rig,
    and this wants the raw observation from several bearings before deciding
    anything.

    ⚠ IT RETURNS WHAT IT SAW, not just the verdict. `calibrate_pitch` prints
    "no part of the pitch range tracks" and nothing else, which is four words
    for thirty measurements — and on 2026-08-05 that sent an hour into
    theorising about the scene when the actual reading was available all
    along. A gate that cannot say what it saw cannot be argued with.
    """
    rig.view.home_to_clamp(+1)
    rises, usable, seen = 0, [], []
    while rises < BAND_MAX:
        prev = rig.tracker.slice_frame(rig.grab())
        rig.mouse.move(0, -step)
        got = rig.view.track_still(timeout_s=0.7, still_s=0.10, prev=prev)
        rises += step
        seen.append((rises, got))
        if abs(got) > step * BAND_TRACK_FRAC:
            usable.append(rises)
    if verbose:
        # Every reading, in rows of six, so the SHAPE is visible: a band that
        # opens and closes looks nothing like a run of zeros, and "0.0
        # everywhere" says the view never moved at all, which is a different
        # fault from "the correlator could not follow it".
        print('      commanded -%d per step, observed:' % step)
        for i in range(0, len(seen), 6):
            print('        ' + '  '.join(
                f'{r:>4}:{g:+7.1f}' for r, g in seen[i:i + 6]))
    return usable, seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--postures', default='standing')
    ap.add_argument('--bearings', type=int, default=4)
    ap.add_argument('--weapon', default='m416')
    ap.add_argument('--sight', default='red_dot',
                    help='the profile whose PATCHES do the tracking. Leave at '
                         'red_dot: its 7 patches map the band, and the band is '
                         'a property of the SCENE, not of the sight.')
    ap.add_argument('--write', action='store_true')
    a = ap.parse_args()

    busy = other_agents()
    if busy:
        print('another agent holds the game / Pico:')
        for b in busy:
            print(f'  {b}')
        return 1
    if not ensure_ready(label='fit_pitch_level')['ok']:
        print('not ready')
        return 1
    with SpawnerControl() as sc:
        ac = InventoryControl(verbose=False)
        try:
            if ensure_weapon_in_hand(ac, sc, weapon=a.weapon) is None:
                print('no weapon in hand — the tracker needs the gun out for '
                      'the same ADS the cells will use')
                return 1
        finally:
            ac.close()

    rig = Rig(a.sight)
    out = {}
    try:
        for posture in a.postures.split(','):
            if not rig.ensure_posture(posture):
                print(f'  [!] could not reach {posture} — skipping')
                continue
            bands = []
            for b in range(a.bearings):
                if b:
                    rig.view.turn(BEARING_COUNTS, 0, settle_s=0.4)
                usable, _seen = band_at(rig)
                if not usable:
                    print(f'  {posture} bearing {b}: nothing tracks')
                    bands.append(None)
                    continue
                print(f'  {posture} bearing {b}: {min(usable)}..{max(usable)} '
                      f'({len(usable)} steps)')
                bands.append((min(usable), max(usable)))
            good = [x for x in bands if x]
            if not good:
                print(f'  [!] {posture}: no bearing tracked at all')
                continue
            lo, hi = max(x[0] for x in good), min(x[1] for x in good)
            if lo >= hi:
                print(f'  [!] {posture}: the usable bands do NOT overlap '
                      f'({[f"{x[0]}..{x[1]}" for x in good]}). No single aim '
                      f'works from every bearing here — nothing stored.')
                continue
            centre = int(round(statistics.mean((lo, hi)) / 10) * 10)
            print(f'  -> {posture}: intersection {lo}..{hi}, storing '
                  f'level_up={centre} ({len(good)}/{a.bearings} bearings)')
            out[posture] = {'level_up': centre, 'band': [lo, hi],
                            'bearings': len(good), 'sight': a.sight,
                            'weapon': a.weapon}
    finally:
        rig.close()

    if not out:
        print('\nnothing to store')
        return 1
    print(f'\n{json.dumps(out, ensure_ascii=False, indent=2)}')
    if not a.write:
        print('\n(--write to store; goto_level stays disabled until you do)')
        return 0

    old = {}
    if os.path.exists(OUT):
        with open(OUT, encoding='utf-8') as f:
            old = json.load(f)
    # The underscore keys are prose that _load_pitch_range skips; keep them,
    # because the one explaining why the file was emptied is still the reason
    # not to trust horizon_row().
    keep = {k: v for k, v in old.items() if k.startswith('_')}
    keep['_measured'] = (f'level_up written by tools/fit_pitch_level.py — the '
                         f'INTERSECTION of the usable band over '
                         f'{a.bearings} bearings, not horizon_row(). '
                         f'Repeatable because it is a constant offset from the '
                         f'absolute bottom clamp; NOT claimed to be level.')
    keep.update(out)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(keep, f, ensure_ascii=False, indent=2)
    print(f'wrote {OUT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
