---
name: calibrate-recoil
description: Measure a weapon's real recoil off the screen and rebuild its compensation curve from it, in the training range. Use when a gun sprays despite compensation, after a game patch changes recoil, to extend coverage to a weapon that has no measured curve, or to test what an attachment actually does. Also the entry point for diagnosing a calibration run that failed — most failures are state, not measurement. Not for mouse-to-view scale (K), which is calibrate_k.py.
argument-hint: "<weapon or question> - e.g. 'aug', 'do compensators really do 0.85', 'harvest failed at the grip config'"
---

# Recoil Calibration

The screen is the sensor. Fire a magazine with compensation ON, measure how far
the view still moved, and the leftover **is** the correction — per bullet, no
model in between.

    true_recoil[b] = compensation_applied[b] + residual_measured[b]

Everything below exists because some part of that sentence turned out to be
harder than it reads.

## Rule 0: ask the game where it is, before running anything

Most failed runs are state, not measurement — wrong gun in the slot, an
attachment that will not read back, not in ADS, the panel already open, evicted
from the range. Every one of those is answerable in five seconds and used to
cost a five-minute run to discover.

    pixi run python calibration/state.py            # no keys pressed, safe any time
    pixi run python calibration/state.py --tab      # inventory too; open Tab yourself first
    pixi run python calibration/state.py --pico     # also opens the port

Run it **before** and **after** anything that fails. It reports focus (by
executable), the spawner panel, the inventory, ADS by two independent methods,
posture, ammo, and the per-patch texture gate.

One device, several tools, no lock: if another agent is mid-run the port is
taken. `state.py` without `--pico` does not touch it.

## The loop

| step | command | what it answers |
|---|---|---|
| 1. measure | `calibration/harvest.py --weapons aug --configs both --mags 3` | how far off is the curve now |
| 2. fit | `calibration/fit_curve.py --jsonl <out> --weapon aug` | dry run: what would change |
| 3. apply | same, `--apply` | writes the curve, backs up the old one |
| 4. verify | step 1 again | residual should fall to ~0 **and wander should shrink** |

Step 4 is not optional. Step 3 can improve the residual while making the gun
spray worse — that has happened, see *the wrong objective* below.

`weapon_curve_kava4/` is **not in git**. The backup fit_curve writes is the
only way back.

## Read the numbers in this order

**Wander first, residual second.** `residual` is the cumulative view offset at
the end of a magazine — one number, easy to zero, and it says nothing about
where the bullets went. Two curves with identical residual can group very
differently. The quantity that matters is how far the impact point wanders
*during* the magazine, `max|cum|` over the per-bullet residuals.

Measured on the AUG when a scalar was tuned to fix the total: endpoint improved
from +43.7 to −22.1 counts while wander got **worse**, 44 → 76. Rebuilding per
bullet instead took wander 76 → 16.

Then check, in this order:

- `oor` — frames past the correlator's range. Non-zero means readings wrapped
  and the magazine is suspect. Should be 0.
- `hand=net/abs` — how much the human moved the mouse. Net is subtracted out;
  a large `abs` with a small net still means a noisier run.
- `mad` — cross-patch agreement, sub-pixel when healthy (0.4–0.8 px). This is
  the evidence the matching is not inventing motion.
- spread across magazines — the game's own per-shot randomness is ±5% of a
  magazine, so ~3 magazines resolve a posture factor and ~7 are needed to tune
  a scale to 2%.

## Traps

Every one of these produced plausible wrong numbers rather than an error.

**Every state change in this game is a toggle.** Comma opens *and* closes the
spawner, Tab the inventory, right-click ADS. Pressing one blind lands in the
wrong state half the time and nothing reports it. Each is paired with a
detector and *watched* until it agrees — never "press, wait 0.5 s, assume".

**Watch to a deadline, do not sample a fixed number of times.** The posture
icon takes ~0.85 s to appear after a right-click, longer right after firing.
Sampling a 0.5–0.83 s window read nothing, concluded "not in ADS", clicked
again — and toggled back out. Self-destructing loop, visible only as a scope
flickering on screen.

**The posture icon only renders in ADS.** So it is also an ADS indicator, which
is convenient, but it means posture cannot be verified from the hip. Order is
always: read attachments (Tab) → ADS → verify posture → fire. `ads_detector`
reads the crosshair instead and is faster; the two disagreeing means
mid-transition.

**Recentre between magazines.** The view never ends where it started — it is
off by exactly the residual — and it accumulates. PUBG clamps pitch, and at the
limit the view stops moving: a magazine fired there measures near-zero recoil
and reports nothing wrong. Must be done in ADS, since a mouse count buys a
third as much rotation from the hip.

**The patch height is the measurable range.** One shot's recoil lands in a
single frame, so the peak frame carries the whole per-bullet kick. Wrap limit
is height/2 = 128 px. A bare m762 peaks at 80 px, a kitted AUG at 49. Past the
limit the correlation peak *wraps* rather than failing — off by a whole patch,
83 counts.

**Template drift is silent.** An attachment whose template no longer matches
becomes `<occupied, no template>`: it has no key, `find()` cannot see it, and
the symptom is "not on screen" for something plainly on screen. `half_grip` and
`thumb_grip` are both drifted today. Use `calibrate-template` to re-extract.

**The game dresses guns by itself.** PUBG auto-fits whatever the backpack holds
onto a weapon the moment it arrives, so any slot a config does not name is not
empty — it is whatever the last strip left lying around. A "bare" run came back
wearing a cheek pad, which reduces recoil.

**Strip before spawning the next weapon.** A full rack means the incoming gun
evicts the old one onto the floor, wearing everything it had on.

**The range evicts after 20 minutes** and re-entry empties the backpack and the
rack. `RangeSession` re-enters on a budget; `--resume` picks up completed cells
from the JSONL.

**Focus is checked by executable.** This repository's own name contains "pubg",
so a title match calls an editor window the game.

## What is measured, and what is still assumed

Measured on the AUG, red dot, standing, 3 magazines per cell:

| | measured | model |
|---|---|---|
| compensator | 0.787 ± 0.009 | 0.850 |
| vertical grip | 0.786 ± 0.008 | 0.850 |
| both | 0.598 ± 0.007 | 0.7225 |
| interaction | −3.4% (1.8σ) | none |

The two attachments are equally effective to within 0.03%, and they compose
multiplicatively — so attachments can be measured **one at a time** against
bare rather than as a grid, 13 cells instead of 54. The individual values are
what the model has wrong, not the structure.

Still assumed, still untested: that those numbers are the same on every weapon.
One weapon cannot answer it. That is what a full `--weapons ar` run is for.

Posture factors, by contrast, came out right: 0.805 and 0.564 measured against
presets of 0.800 and 0.550.

## When it breaks

| symptom | first command |
|---|---|
| anything at all | `calibration/state.py` |
| "not on screen" for a part that is | `state.py --tab`, look for `UNRECOGNISED` → `calibrate-template` |
| "could not reach posture" | `state.py` — in ADS? inventory closed? |
| "inventory would not open/close" | `state.py`, check the `type` pixel count against its window |
| could not open the port | another tool has it; the error names the process. Wait, do not kill it |
| residual fine, gun still sprays | you optimised the endpoint — look at wander |
| a weapon measures implausibly mild | check `oor`, and whether the view hit the pitch limit |
| spawner would not sync | `state.py` — is the panel actually up? are we still in the range? |

## Files

| | |
|---|---|
| `calibration/state.py` | read-only state probe — start here |
| `calibration/harvest.py` | spawn, dress, fire, measure; the unattended loop |
| `calibration/sweep.py` | same measurement without the spawner, for a gun already in hand |
| `calibration/fit_curve.py` | residual → new curve |
| `calibration/range_session.py` | 20-minute eviction; `AutoSession` is a stub |
| `calibration/weapon_switcher.py` | weapon supply interface |
| `detector/view_tracker.py` | the measurement itself |
| `docs/game_quirks.md` | mechanics found by hitting them |
| `docs/recoil_observer_design.md` | why the ROI is where it is |
