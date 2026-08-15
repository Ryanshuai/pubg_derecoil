#!/usr/bin/env bash
# The night's schedule: every full-auto gun, every round-1 cell.
# Not a driver -- every line is a `pixi run night`.
#
# 130 of the roster's 135 round-1 cells, across 23 guns. At the measured rates
# (magazine to magazine 11.0 s, per-cell setup ~25 s, a kit change ~45 s) and
# --mags 6 that is about 2.3 min a cell, so roughly 5 hours.
#
# ⚠ IT ONLY SCHEDULES CELLS THAT CAN REACH THE TRIGGER. `collect_into_store`
# refuses a cell with NEITHER a curve on disk NOR enough in the store to fit
# one -- correctly, because uncompensated the view walks into open sky, where
# phase correlation returns 0 CONFIDENTLY and the magazine is lost with every
# gate green. A batch holding four such cells does not measure them badly, it
# HALTS. That is what cost the 2026-08-10 run its first batch.
#
# All 130 were made reachable on 2026-08-10 by seeding, in three tiers, each
# weaker than the one above it and each named in the file it writes:
#
#   import_kava4        that gun's own community pattern      5 guns
#   estimate_cell       that gun's own bare x a kit factor    56 cells
#   estimate_cell       ANOTHER gun's bare, counts unscaled   11 guns (bare)
#                       --donor, marked `cross_gun`
#
# ⚠ FIVE CELLS ARE DELIBERATELY ABSENT AND THIS SAYS WHICH. mk14's three sniper
# muzzles and uzi_stock on mp9/uzi have never been measured on ANY gun, so
# there is no factor to borrow and estimate_cell refused rather than
# substituting 1.0. They are unreachable tonight; listing them would spend four
# failures and stop a batch. A plan that quietly bounds its own coverage reads
# afterwards as "we covered everything".
#
# WHY EACH GROUP IS ITS OWN RUN: night.py HALTS on HALT_STREAK failures in a
# row, which is right for one batch and wrong for a night. One gun that will
# not spawn should not cost the others. Every batch RESUMES if re-run.
#
#   bash tools/night_queue.sh              all of it
#   bash tools/night_queue.sh ar smg       only these groups
#
# ⚠ Run `pixi run ready` first. Most failed runs are state, not measurement.
set -u
cd "$(dirname "$0")/.."

# One string for the whole roster: plan_cells drops the slots a gun does not
# have (control.kitting.supported_configs), so groza takes 2 cells from this
# line and ace32 takes 10. laser / light_grip / thumb_grip are absent on
# purpose -- laser measures 1.0058, an identity, and the other two were not
# asked for (tools/coverage.py: NOT_WORTH_FIRING).
R1="bare,muzzle=brake_ar,muzzle=comp_ar,muzzle=flash_ar,muzzle=supp_ar"
R1="$R1,muzzle=comp_smg,muzzle=flash_smg,muzzle=supp_smg"
R1="$R1,grip=half_grip,grip=tilted_grip,grip=vert_grip"
R1="$R1,stock=heavy_stock,stock=tactical_stock"

# ⚠ 0.15 OF THE TRAVEL, and it applies ONLY where the curve is missing or
# `cross_gun` -- night.aim_below_for reads that flag off the file rather than
# guessing from a list, and a gun drops back to a level aim by itself the
# moment it has a fit of its own. The flag is inherited: a kit cell priced off
# a borrowed bare is just as unscaled as its parent.
LOW="--aim-below 0.15"

# The 11 guns whose bare curve is another gun's, plus everything derived from
# it. Split from the measured roster ONLY so the low aim is scoped to them.
NEW="akm,famas,g36c,js9,k2,m249,mp9,qbz,tommy,ump45,uzi"

# ⚠ mp5k `bare` IS NOT FIRED. Its pool holds 186 magazines across four optics
# and several generations of badly wrong curves, and it came back 12 arms
# disagreeing by 344.6%. That is a pool to cut apart offline
# (fit_time_curve --sight), not a cell to pour more magazines into. Every OTHER
# mp5k cell is in the smg batch as usual.
MP5K_MINUS_BARE="muzzle=comp_smg,muzzle=flash_smg,muzzle=supp_smg"
MP5K_MINUS_BARE="$MP5K_MINUS_BARE,grip=half_grip,grip=tilted_grip,grip=vert_grip"
MP5K_MINUS_BARE="$MP5K_MINUS_BARE,stock=heavy_stock,stock=tactical_stock"

# ⚠ mk14 AND uzi/mp9 CARRY A REDUCED CONFIG LIST, for the five cells named at
# the top. Everything else about them is fired normally.
MK14="bare,muzzle=brake_ar,muzzle=comp_ar,muzzle=flash_ar,muzzle=supp_ar,stock=heavy_stock,stock=tactical_stock,grip=half_grip,grip=tilted_grip,grip=vert_grip"
NO_UZI_STOCK="bare,muzzle=comp_smg,muzzle=flash_smg,muzzle=supp_smg,grip=half_grip,grip=tilted_grip,grip=vert_grip,stock=heavy_stock,stock=tactical_stock"

run () { echo; echo "=== $1"; shift; pixi run night "$@" || echo "  [!] batch ended early (halt or crash) -- continuing to the next"; }
want () { [ $# -eq 0 ] && return 0; for w in "$@"; do [ "$w" = "$BATCH" ] && return 0; done; return 1; }

for BATCH in measured_ar new_ar measured_smg new_smg mp5k lmg dmr; do
  want "$@" || continue
  case $BATCH in
    # The guns whose round 1 is already complete get their cells re-fired for
    # depth, and the fit is a full refit over the whole accumulated pool, so
    # every magazine counts. They go FIRST because they are the ones known to
    # spawn, kit and read back -- a systemic fault shows up here in 20 minutes
    # instead of three hours in.
    measured_ar)  run "AR, already measured  (ace32 aug groza m416 m762 scar)" \
                      --weapons ace32,aug,groza,m416,m762,scar --configs "$R1" --mags 6 ;;
    new_ar)       run "AR, borrowed bare     (akm famas g36c k2 qbz)" \
                      --weapons akm,famas,g36c,k2,qbz --configs "$R1" --mags 6 $LOW ;;
    measured_smg) run "SMG, already measured (p90 vector)" \
                      --weapons p90,vector --configs "$R1" --mags 6 ;;
    new_smg)      run "SMG, borrowed bare    (js9 mp9 tommy ump45 uzi)" \
                      --weapons js9,tommy,ump45 --configs "$R1" --mags 6 $LOW
                  run "SMG, borrowed bare    (mp9 uzi, no uzi_stock cell)" \
                      --weapons mp9,uzi --configs "$NO_UZI_STOCK" --mags 6 $LOW ;;
    mp5k)         run "mp5k, every cell but bare" \
                      --weapons mp5k --configs "$MP5K_MINUS_BARE" --mags 6 ;;
    lmg)          run "LMG                   (mg3 m249)" \
                      --weapons mg3 --configs bare --mags 6
                  run "LMG, borrowed bare    (m249)" \
                      --weapons m249 --configs "$R1" --mags 6 $LOW ;;
    dmr)          run "DMR                   (vss mk14)" \
                      --weapons vss --configs "$R1" --mags 6
                  run "DMR, seeded           (mk14, no sniper muzzles)" \
                      --weapons mk14 --configs "$MK14" --mags 6 $LOW ;;
  esac
done

echo
echo "=== done. The morning read:"
echo "     pixi run coverage --cells"
echo "     pixi run night --report calibration/artifacts/nights/<ts>"
