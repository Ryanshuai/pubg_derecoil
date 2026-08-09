#!/usr/bin/env bash
# One night per weapon, sequentially. ONE RUN PER WEAPON ON PURPOSE: `night`
# halts after 4 consecutive failures, so a single bad gun in a combined run
# would take every gun after it with it. A per-weapon run confines a halt to
# the gun that caused it, and the manifest for each is separate.
cd /d/10_projects/pubg_derecoil || exit 1

run () {
  local w="$1"; shift
  local cfgs="$1"
  echo ""
  echo "################ $w  ################"
  pixi run night --weapons "$w" --configs "$cfgs" --mags 5 2>&1
  echo "################ $w done (exit $?) ################"
}

run aug   "bare,muzzle=comp_ar,muzzle=flash_ar,muzzle=supp_ar,muzzle=brake_ar,grip=vert_grip,grip=half_grip,grip=tilted_grip"
run m416  "bare,muzzle=comp_ar,muzzle=flash_ar,muzzle=supp_ar,muzzle=brake_ar,grip=vert_grip,grip=half_grip,grip=tilted_grip,stock=heavy_stock,stock=tactical_stock"
run scar  "bare,muzzle=comp_ar,muzzle=flash_ar,muzzle=supp_ar,muzzle=brake_ar,grip=vert_grip,grip=half_grip,grip=tilted_grip"
run m762  "bare,muzzle=comp_ar,muzzle=flash_ar,muzzle=supp_ar,muzzle=brake_ar,grip=vert_grip,grip=half_grip,grip=tilted_grip"
run ace32 "bare,muzzle=comp_ar,muzzle=flash_ar,muzzle=supp_ar,muzzle=brake_ar,grip=vert_grip,grip=half_grip,grip=tilted_grip,stock=heavy_stock,stock=tactical_stock"
run groza "bare,muzzle=supp_ar"
run mg3   "bare"
# p90 REMOVED 2026-08-09: it has no attachment slots, so it cannot be
# given a red dot, and its integral optic has no K in
# RECOIL_SIGHT_PROFILES. Measuring it starts with calibrate_k, not here.
# Evidence: docs/p90_has_no_slots.png, docs/p90_no_slots_0802_scan.png

echo ""
echo "################ QUEUE COMPLETE ################"
