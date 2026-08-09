#!/usr/bin/env bash
# The four weapons that lost cells to two faults now fixed:
#   the backpack-full stocking failure (12 rows, 9 parts held at once)
#   the capacity refusal (m416/m762 stored one round count, tonight fires another)
#
# 20 of the night's 22 `state` failures were those two. What is left is
# `kit: magazine reads ''` -- the part genuinely did not land -- which is a
# retry, not a design fault.
cd /d/10_projects/pubg_derecoil || exit 1

run () {
  echo ""
  echo "################ $1  ################"
  pixi run night --weapons "$1" --configs "$2" --mags 5 2>&1
  echo "################ $1 done (exit $?) ################"
}

AR="bare,muzzle=comp_ar,muzzle=flash_ar,muzzle=supp_ar,muzzle=brake_ar,grip=vert_grip,grip=half_grip,grip=tilted_grip"

run m416  "$AR,stock=heavy_stock,stock=tactical_stock"
run m762  "$AR"
run scar  "$AR"
run mg3   "bare"
run p90   "bare"

echo ""
echo "################ RE-RUN COMPLETE ################"
