---
name: calibrate-recoil
description: Measure a weapon's real recoil off the screen and rebuild its compensation curve from it, in the training range. Use when a gun sprays despite compensation, after a game patch changes recoil, to extend coverage to a weapon that has no measured curve, or to test what an attachment actually does. Also the entry point for diagnosing a calibration run that failed — most failures are state, not measurement. Not for mouse-to-view scale (K), which is calibrate_k.py.
argument-hint: "<weapon or question> - e.g. 'aug', 'do compensators really do 0.85', 'the night failed on the grip config'"
---

# Recoil Calibration

**`MODEL.md` is the law. Anything here that contradicts it is wrong and this
file is what gets corrected.** It says what is being fitted; this says how to
run it and what the failures look like from the chair.

The screen is the sensor. Fire, measure how far the view moved, add back the
compensation that was playing, and what is left is the weapon's own recoil:

    y_true(t) = y_obs(t) + y_comp(t)

⚠ **`y_true` IS A FUNCTION OF TIME SINCE THE CLICK, NOT OF BULLET NUMBER.**
The per-round kicks are the SHAPE of that curve, not its coordinate. Everything
downstream follows from that one sentence, and this file was rewritten on
2026-08-08 because it used to assume the other thing.

## What changed, in one table

Read this if you have used this skill before. The commands are different
because the questions are.

| gone | why | now |
|---|---|---|
| `harvest.py --weapons aug` | binned view motion into 42 bullet buckets | `pixi run collect-timed --weapon aug` |
| `fit_curve.py --apply` (EMA) | blended each round into the last curve | `fit_time_curve.py --weapon aug`, a full refit |
| "has this cell converged" | there are no rounds to converge over | "are there enough samples", which is a count |
| residual / wander per bullet | both are bucket quantities | cluster spread in counts, and the arms agreeing |
| `pixi run impulse-ab` | checked two grids shared an origin | there is one origin: the click, which we send |

**Samples are never deleted and never re-collected.** Every magazine ever fired
is in `calibration/artifacts/recoil/samples/<weapon>__<config>.jsonl` with the curve that was
playing stored BY VALUE. That is what makes a magazine fired last week
comparable with one fired tonight, and it is why fitting is one full refit
rather than an iteration on top of the last one.

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

### If the HUD is unreadable, LOOK AT THE SCREEN before believing any message

`posture ?? unreadable` + `ammo ?? None` + an empty rack is not four faults, it
is one: **there is no HUD, because we are not in the game.** Every downstream
message is then a true sentence about the wrong subject. 2026-08-06, verbatim:

```
[stock] spawned 1 in 2 clicks: vss
[!] vss is not in the rack, and both slots are empty — the spawn did not land.
[!] posture unreadable (want standing)
```

The screen at that moment said 「因长时间没有动作, 您已被踢出游戏」 — the AFK
kick. **The dialog names its own cause and nothing was reading it.** One
capture answers it, and costs nothing:

```
pixi run python control/lobby.py state       # error / disconnected / lobby / in_game
```

⚠ **The idle clock that gets you kicked is the AGENT'S thinking time, not the
game's.** Four kicks in one night, and before each one the gap was me writing
analysis instead of driving: 14:33:36 a run ended, 14:49 the next began,
**fifteen minutes of nothing**. The firing itself never triggered it. So:
**launch the next run first, analyse while it fires.** Standing in the range
idle is the one thing a human player never does.

## The loop

| step | command | what it answers |
|---|---|---|
| 1. collect | `pixi run collect-timed --weapon aug --mags 6` | adds magazines to the store |
| 2. second arm | same, `--no-comp` (or `--scale 0.5`) | **makes the cell checkable at all** |
| 3. fit | `pixi run python calibration/fit_time_curve.py --weapon aug` | one refit over everything ever stored |
| 4. upload + verify | fit again after firing more | the spread should tighten and the arms should still agree |

⚠ **STEP 2 IS NOT OPTIONAL AND IT IS NOT A LUXURY.** The model's licence to
pool magazines is that magazines fired under DIFFERENT curves, each with its
own `y_comp` added back, estimate the SAME `y_true`. A pool with one arm has
never been checked, and `harness/verdict.py` fails such a cell closed on
`agree` — deliberately, because "not checked" and "fine" are the two things
that layer exists to keep apart. A fitter cannot fake it: it never sees which
arm a magazine came from.

`collect-timed` takes the gun **already in hand**. No spawning, no kitting —
that machinery is the single largest source of wasted runs and has nothing to
do with whether the model works. `--weapon` names what is held and the HUD
detector is asked to agree; a disagreement stops the run rather than labelling
the samples with the name that was typed.

For a whole roster unattended, that is the night loop instead:

    pixi run night --weapons ar --configs bare,grip --mags 6

It kits, fires both arms per cell, fits the pool and judges each cell against
`harness/verdict.py`. `--rejudge <run>` re-runs the verdict over a finished
run's records offline, which is how a wrong threshold gets corrected without
re-firing anything.

## Read the numbers in this order

**The arms first, the spread second, the total last.**

| number | where | what it means |
|---|---|---|
| `agree_arms` / `agree_spread` | the cell record | did the model's own assumption hold here |
| `n_kept` / `n_total` | `fit()` | how many magazines the clustering kept |
| `spread_counts` | `fit()` | median disagreement between the kept magazines |
| `dropped[]` | `fit()` | what was pushed out, and how far it sat |
| `total_counts` | `fit()` | y_true at the end of the span |

⚠ **A small `spread_counts` proves nothing on its own.** Nine magazines fired
under one curve will agree with each other beautifully and still be nine
estimates of the same wrong thing. That is what `agree_arms` is for, and it is
why it is read first.

### The clustering is per MAGAZINE, not per point

**There are always outliers and they are almost never scattered.** What ruins a
magazine — a hand on the mouse, the wrong posture, an attachment that did not
go on, dropping out of ADS mid-burst, the correlator losing the view —
**contaminates the whole trajectory**, not a few points on it. And the
contaminated trajectory looks completely reasonable: smooth, monotone, the
right order of magnitude. Point-wise outlier rejection cannot see it. Only
comparing whole magazines against each other can.

So `fit()` resamples each magazine onto a common grid, clusters the resulting
vectors, and fits the largest cluster. `dropped[]` says what fell out and how
far it sat from the centre, because **a gate that cannot say what it refused
cannot be retuned** — and this repo has a rule about gates auditing away the
data you would need to retune them.

## The one place the model is known to be wrong

MODEL.md §5之二, and it is worth reading before trusting a number from the ends
of a burst. 28 magazines of m416 bare, four curve strengths spanning 3x:

```
    t         spread across the four arms
   1.5 s      0.9%        ← the assumption is excellent here
   ≤ 2.4 s    3.7–5.6%
   ≥ 2.7 s    up to 15%   ← and ONLY the strongest arm falls away
```

So: **the mid-band is verified, the two ends are not.** The judge's agreement
gate is set on 0.5–2.4 s for exactly that reason — judging the model on the one
region MODEL.md says is unexplained would be judging it where nobody knows the
answer.

⚠ Two claims about this were made and withdrawn the same day, and they are kept
in MODEL.md as samples rather than deleted: "the game has a 0.92 gain" (it is
0.98 mid-band; 0.92 was an endpoint ratio) and "y_true is an inverted U" (it is
monotone to 2.4 s; the U was an artefact of reading only the last point).
**Both numbers were computed correctly.** The error was reading an aggregate
that could not see the dimension it was being asked about.

## Traps

Every one of these produced plausible wrong numbers rather than an error, and
none of them was fixed by the model change.

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

**Home to the midline between magazines.** The view never ends where it
started, and it accumulates. PUBG clamps pitch, and at the limit the view stops
moving: a magazine fired there measures near-zero recoil and reports nothing
wrong. `goto_midline` shoves past the bottom clamp by a known multiple of the
travel and comes back up half — the travel is a stored per-(sight, posture)
constant, so the dip is two mouse moves and **not** a measurement.

⚠ **One homing per magazine, not two.** `collect_timed` used to home once in
setup and again in the loop, so the first magazine dipped the view twice.
Reported from the chair on 2026-08-08: 「压枪的时候会低两次头」. The loop's is
the one that must stay — every magazine has to start at the midline because the
burst walks the view up from wherever it begins.

**The patch height is the measurable range.** One shot's recoil lands in a
single frame, so the peak frame carries the whole per-bullet kick. Wrap limit
is height/2 = 128 px. A bare m762 peaks at 80 px, a kitted AUG at 49. Past the
limit the correlation peak *wraps* rather than failing — off by a whole patch,
83 counts. `oor` records the pairs where that happened; they are stored, not
dropped, because dropping is a fit-time decision.

**Template drift is silent.** An attachment whose template no longer matches
becomes `<occupied, no template>`: it has no key, `find()` cannot see it, and
the config the samples are pooled under is then a guess. `collect_timed`
refuses rather than guessing — **the config is the key every magazine gets
pooled under, and a wrong one merges two different guns.**

## When a thing cannot be seen

A part that will not detect stops the run, and squinting at one screenshot is
the slow way to fix it. Collect instead:

    pixi run python calibration/collect_templates.py --slot grip --targets rows

It spawns one of every attachment in that slot, then photographs the inventory
against several backgrounds, turning the view between captures. Two outputs:

- **labelled crops** — `<item>__rowNN__<weapon>__<background>.png`, which is what
  `calibrate-template` extracts from. The panel is translucent, so a template
  built from a single background tracks that background; varying the scene is
  the point, not a nicety.
- **a coverage table** — for each item, how many backgrounds the current
  templates read it in, and what it was mistaken for. An item that reads at
  some angles and not others is worse than one that never reads, and only a
  spread of backgrounds separates the two.

The labels are trustworthy because the ground truth is self-specified: the
spawner is told what to produce and in what order, and 库存 fills from the top
with no gaps, so row N holds a known item **even when nothing on screen can
name it**. If the row count does not grow by exactly what was ordered, it stops
rather than mislabelling every crop.

`--check-only` skips spawning and grades whatever is already in the backpack.

Turning is done with Tab shut. With the inventory open the mouse drives a
cursor rather than the view, so a turn issued there moves nothing and every
capture comes back identical.

## When it breaks

| symptom | first move |
|---|---|
| every message is nonsense at once | not in the game. `python control/lobby.py state` |
| `REFUSING: could not read the attachment slots` | the config key would be a guess. Fix the template, not the run — `pixi run attachments` |
| the cell fails on `agree` | one arm only. Fire `--no-comp` into the same config |
| the cell fails on `samples` | the main cluster is thin. Read `dropped[]` before firing more — it may be that most magazines are the outliers |
| the cell fails on `tracking` | the correlator is not placing pairs. `detector/view_tracker.py`; the anchor should hold until the displacement approaches half a patch |
| the fit's total moves a lot between runs | expected early. Every fit is a full refit, so a thin pool moves; it stops moving as the pool grows |
| `pixi run night` cannot start | it opens with the five-leg gate. The message names which leg |

## Files

| | |
|---|---|
| `MODEL.md` | **the law.** What is fitted, in what coordinate, and what that deleted |
| `calibration/samples.py` | the store. Never deleted, never re-collected |
| `calibration/collect_timed.py` | fire into the store. One magazine at a time |
| `calibration/fit_time_curve.py` | cluster and fit. `--selftest` is offline |
| `harness/night.py` | the unattended loop |
| `harness/verdict.py` | whether a cell is usable. Numbers against thresholds |
| `control/kitting.py` | put a config on a gun and prove it. Moved out of calibration/ on 2026-08-08 |
| `calibration/sweep.py` | `Rig` — the assembly shell, and nothing else now |
| `press/pico_mouse.py` | `upload_pattern` uploads the curve AS GIVEN, one knot in, one knot out |

⚠ **The 1184 old curves are gone.** Fitted in the retired coordinate and
deleted on 2026-08-08 rather than kept: a curve fitted on bins anchored to the
ammo counter, played back on a grid anchored to the click, is not a starting
point. `detector/weapon.py` reads `config.CURVES_DIR` (**`data/curves/`**,
and it is IN git — the previous home was under the wholesale-ignored `docs/`,
which is how a cleanup took 40 weapons' curves with nothing to restore from).
A weapon with no curve there simply gets no compensation — the honest state,
not a regression.

⚠ **Ask `config`/`samples` for a path, do not spell one.** Every artifact
path in this skill was `calibration/artifacts/...` until the 2026-08-08 move to
`calibration/artifacts/`, and three of them were still wrong after the commit
that claimed to have repointed the skills — a step written in Markdown is
invisible to every import graph and to `pixi run layering`.
