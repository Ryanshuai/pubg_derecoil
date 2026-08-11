#!/usr/bin/env bash
# Everything that is not measured yet, newest evidence first.
#
# ⚠ ORDER IS BY INFORMATION, NOT BY SIZE. The re-seeded cells go first because
# they are the ones whose outcome is not already known; a full sweep that puts
# them last learns nothing for hours and then finds out the seeds were wrong.
# Depth on already-good cells goes last, where losing it costs nothing.
#
# WHAT CHANGED SINCE tools/night_queue.sh (2026-08-10, same day):
#
#   m16, mk47      ⚠ NOT HERE, AND THEY WERE. tools/coverage.py reported them
#                  as "NEVER FIRED, and a spray weapon so a curve is the right
#                  shape for it" -- but both are two-round burst and single,
#                  NO FULL AUTO, so there is no continuous spray for a curve to
#                  be about. The report asked a CLASS question ('AR') about a
#                  per-WEAPON fact. 17 seed files were written off it and this
#                  queue fired them for a batch before the operator said so.
#                  config.NO_FULL_AUTO is the author now and coverage agrees:
#                  the never-fired-spray list is 0.
#   akm, uzi       FOUR SEEDS WERE 18-45% OFF what the gun's own magazines
#                  say, and those are exactly the cells that failed `agree`
#                  last run:
#                      akm muzzle-brake_ar   1260.8 seed vs 1533.9 own  -17.8%
#                      uzi bare               932.4 vs  740.2           +26.0%
#                      uzi muzzle-comp_smg    594.4 vs  458.2           +29.7%
#                      uzi muzzle-flash_smg   814.6 vs  561.6           +45.1%
#                  Re-fitted from their own data; uzi's three kit cells
#                  re-derived off the corrected bare. `cross_gun` cleared, so
#                  night.aim_below_for stops aiming them low by itself.
#   mp9            The stock cells are GONE from the plan, not skipped:
#                  ('mp9','stock') accepts uzi_stock only (operator, measured),
#                  so `compatible()` no longer offers the other two and the
#                  night harness cannot schedule what does not exist.
#
# ⚠ mp5k `bare` IS STILL NOT FIRED. 186 magazines across four optics and 12
# arms disagreeing by 344.6%: a pool to cut apart offline, not one to pour more
# into. Every other mp5k cell is here.
#
# ⚠ AND STOPPING THIS NEEDS THE PROCESS TREE. Killing the wrapper leaves the
# `pixi run night` children holding the Pico -- measured on the 2026-08-10 run,
# three orphans. `taskkill /F /T /PID <pid>` or kill the whole tree.
set -u
cd "$(dirname "$0")/.."

R1="bare,muzzle=brake_ar,muzzle=comp_ar,muzzle=flash_ar,muzzle=supp_ar"
R1="$R1,muzzle=comp_smg,muzzle=flash_smg,muzzle=supp_smg"
R1="$R1,grip=half_grip,grip=tilted_grip,grip=vert_grip"
R1="$R1,stock=heavy_stock,stock=tactical_stock"

# 0.15 of the travel, and ONLY where the curve is still `cross_gun` --
# night.aim_below_for reads that flag off the file rather than guessing from a
# list, so a gun drops back to a level aim the moment it has a fit of its own.
LOW="--aim-below 0.15"

MP5K="muzzle=comp_smg,muzzle=flash_smg,muzzle=supp_smg"
MP5K="$MP5K,grip=half_grip,grip=tilted_grip,grip=vert_grip"
MP5K="$MP5K,stock=heavy_stock,stock=tactical_stock"
MK14="bare,muzzle=brake_ar,muzzle=comp_ar,muzzle=flash_ar,muzzle=supp_ar"
MK14="$MK14,stock=heavy_stock,stock=tactical_stock"
MK14="$MK14,grip=half_grip,grip=tilted_grip,grip=vert_grip"

run () { echo; echo "=== $1"; shift; pixi run night "$@" \
         || echo "  [!] batch ended early (halt or crash) -- continuing"; }
want () { [ $# -eq 0 ] && return 0; for w in "$@"; do [ "$w" = "$B" ] && return 0; done; return 1; }

for B in reseeded thin gaps depth; do
  want "$@" || continue
  case $B in
    # ── the four cells whose seeds were provably wrong ──
    reseeded) run "RE-SEEDED: uzi (all) + akm brake_ar" \
                  --weapons uzi --configs "$R1" --mags 6
              run "RE-SEEDED: akm" \
                  --weapons akm --configs "$R1" --mags 6 ;;
    # ── guns with 0 measured cells, or 1 ──
    thin)     run "THIN: m249 mg3 vss mp9 mk14" \
                  --weapons m249,mp9 --configs "$R1" --mags 6 $LOW
              run "THIN: mg3 vss" \
                  --weapons mg3,vss --configs "$R1" --mags 6
              run "THIN: mk14" \
                  --weapons mk14 --configs "$MK14" --mags 6 $LOW ;;
    # ── cells that failed `agree` or ran one-armed ──
    gaps)     run "GAPS: scar vector m416 ace32" \
                  --weapons scar,vector,m416,ace32 --configs "$R1" --mags 6
              run "GAPS: mp5k, every cell but bare" \
                  --weapons mp5k --configs "$MP5K" --mags 6 ;;
    # ── depth on what already passed; last, because losing it costs nothing ──
    depth)    run "DEPTH: the rest" \
                  --weapons aug,groza,m762,famas,g36c,k2,qbz,js9,tommy,ump45,p90 \
                  --configs "$R1" --mags 6 ;;
  esac
done

echo
echo "=== done:  pixi run coverage     |     pixi run night --report <run>"
