#!/usr/bin/env bash
# One gun per fresh match, three-round groups on the concrete wall, logs KEPT.
#
#   bash tools/first_shot_batch.sh akm m762 p90 mg3 groza
#
# ⚠ A FRESH MATCH PER GUN, AND THAT IS NOT CAUTION -- IT IS THE ONLY WAY THE
# SECOND GUN GETS A SURFACE. Bullet decals persist, and `hole_groups` needs the
# strip above the crosshair to be >=0.80 flat concrete. Measured tonight, same
# wall, same session:
#
#     after one gun's two groups     0.72 and 0.14  ->  REFUSING, twice
#     after quit + rejoin            0.80 and 0.80  ->  both groups landed
#
# Jump School's north face is the only large unbroken slab this repository has
# found, so there is nowhere else to move to. Reloading the world is the reset.
#
# ⚠ BOTH GROUPS FIRE UNCOMPENSATED (`--arms off,off`) ON PURPOSE. A spawned gun
# wears whatever the backpack can autofit, and a kitted config usually has no
# curve -- so an "ON" group prints `0 knots` and is uncompensated while being
# labelled otherwise. Two runs tonight were read that way before anyone noticed,
# one of them on an m416 reported as an aug. Ask for the condition you can
# actually guarantee.
#
# ⚠ AND THE LOG IS THE PRODUCT. `hole_groups` writes frames and prints numbers;
# the numbers reach no file. Three groups fired on 2026-08-11 are unrecoverable
# for that reason -- the pixels survive, the conditions do not.
set -u
cd "$(dirname "$0")/.."

STAMP=$(date +%m%d_%H%M%S)
DIR="calibration/artifacts/holes/batch_${STAMP}"
mkdir -p "$DIR"
echo "logs -> $DIR"

for w in "$@"; do
  echo
  echo "=== $w"
  pixi run python - <<'PY' 2>&1 | tail -2
import sys; sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from control.lobby import LobbyControl
with LobbyControl() as lc:
    lc.quit_game()
    print('fresh match:', lc.ensure_in_match(launch=True)['ok'])
PY
  pixi run python calibration/hole_groups.py --weapon "$w" \
      --groups 2 --arms off,off --rounds 3 --yaw-step -260 \
      2>&1 | tee "$DIR/$w.log" \
      | grep -E 'group [0-9]|holes:|ratio|REFUS|AGREE|per-shot'
done

echo
echo "=== table"
pixi run python tools/first_shot_table.py
