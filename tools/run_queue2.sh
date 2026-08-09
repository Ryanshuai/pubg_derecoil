#!/usr/bin/env bash
# Phase 2, after run_queue.sh. Order and its provenance: docs/weapon_priority.md
#
# ⚠ A SEPARATE FILE, NOT AN EDIT TO run_queue.sh. bash reads a script
# incrementally, so editing one while it runs can shift the file offset under
# the interpreter and execute garbage. Phase 1 is still running.
#
# ⚠ mk14 and vss are NOT here. Neither has a Kava4 pattern, so both would fire
# uncompensated into open sky, where phase correlation returns 0 confidently.
# ⚠ m249 is NOT here either: WEAPON_RPM says 194.7, which is wrong, and the rate
# decides the burst length and the seed span.
cd /d/10_projects/pubg_derecoil || exit 1

run () {
  local w="$1"; shift
  echo ""
  echo "################ $w  ################"
  pixi run night --weapons "$w" --configs "$1" --mags 5 2>&1
  echo "################ $w done (exit $?) ################"
}

AR="bare,muzzle=comp_ar,muzzle=flash_ar,muzzle=supp_ar,muzzle=brake_ar,grip=vert_grip,grip=half_grip,grip=tilted_grip"
ARK="$AR,stock=heavy_stock,stock=tactical_stock"
SMG="bare,muzzle=comp_smg,muzzle=flash_smg,muzzle=supp_smg,grip=vert_grip,grip=half_grip,grip=tilted_grip"
SMGK="$SMG,stock=heavy_stock,stock=tactical_stock"

# sourced ranks 6 and 7
run ump45 "$SMGK"
run akm   "$AR"
# unsourced from here: class + fire rate, see docs/weapon_priority.md
run qbz   "$AR"
run g36c  "$AR"
run k2    "$AR"
run famas "$AR"
run uzi   "$SMGK"
run mp9   "$SMGK"
run js9   "$SMG"
run tommy "$SMG"

echo ""
echo "################ PHASE 2 COMPLETE ################"
