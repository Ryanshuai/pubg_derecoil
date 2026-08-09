---
name: calibrate-recoil
description: Measure a weapon's real recoil off the screen and rebuild its compensation curve from it, in the training range. Use when a gun sprays despite compensation, after a game patch changes recoil, to extend coverage to a weapon that has no measured curve, or to plan an attachment campaign — which parts to measure, in what order, and how many magazines each cell needs. Also the entry point for diagnosing a calibration run that failed; most failures are state, not measurement. Not for mouse-to-view scale (K), which is calibrate_k.py.
argument-hint: "<weapon or question> - e.g. 'aug', 'measure every grip on the m762', 'do compensators really do 0.85', 'the night failed on the grip config'"
---

# Recoil Calibration

**`MODEL.md` is the law. Anything here that contradicts it is wrong and this
file is what gets corrected.** It says what is being fitted; this says how to
plan a campaign, run it, and read the failures.

    y_true(t) = y_obs(t) + y_comp(t)

Fire, measure how far the view moved, add back the compensation that was
playing. ⚠ **`y_true` is a function of TIME SINCE THE CLICK, not of bullet
number.** The per-round kicks are the shape of that curve, not its coordinate.

**Samples are never deleted and never re-collected.** Every magazine is in
`calibration/artifacts/recoil/samples/<weapon>__<config>.jsonl` with the curve
that was playing stored BY VALUE — which is what makes last week's magazine
comparable with tonight's, and why fitting is one full refit and never an
iteration on top of the last one.

---

# THE STRATEGY: singles, then probe, then expand only what bit

This is the core of the skill. Everything below it is mechanics.

## The problem is combinatorial and the naive answer is unaffordable

Five guns, every muzzle x every grip x every stock:

    full factorial     168 cells    5.8 hours
    singles only        35 cells     72 min
    singles + 1 probe per slot-pair   51 cells   ~2 hours

Measured rates behind those numbers: trigger down 3.44 s, magazine to magazine
11.0 s (so **69% of the loop is not firing**), a kit change ~45 s, per-cell
setup ~25 s.

## Round 1 — every part alone. No knowledge of the gun required

Run it blind. It needs no assumption and it is the denominator for everything
after it.

    for each slot the gun HAS: each compatible part, alone, plus bare

    m762 / aug      muzzle 4, grip 3, no stock slot   ->  8 cells
    mp5k            muzzle 3, grip 3, stock 2         ->  9
    vector          muzzle 3, grip 2, stock 2         ->  8
    vss             stock 1 (cheek_pad) only          ->  2

⚠ **Check `has_slot` before planning an axis.** m762 and AUG have NO stock
slot; VSS has neither muzzle nor lower rail (integral suppressor, no rail —
it is in kitting.py's list of 11 full-auto guns with no grip slot). An axis
that does not exist is not a cheap cell, it is four guaranteed failures and a
halted night.

## Round 2 — is the non-orthogonality a SLOT property or a PART property?

Assume the factors multiply, then go find where they do not. But the question
is sharper than "does this pair interact":

> **Do the coefficients of these two SLOTS fail to multiply — or do these two
> particular PARTS not get along?**

⚠ **ONE part-pair cannot tell those apart. Two can**, and they must share no
part, or the two interaction estimates have a common term and their difference
is an artefact.

    muzzle x grip:   brake_ar + half_grip     AND    comp_ar + tilted_grip
    muzzle x stock:  comp_smg + heavy_stock   AND    flash_smg + tactical_stock

    the two agree     -> a SLOT property. ONE number covers the whole plane
    they disagree     -> a PART property. THAT plane has to be expanded

Three slot-pairs at most per gun, two cells each — 16 cells across the roster.

## Round 3 — expand only the planes that bit

Nothing else. A plane that came back orthogonal is answered by the singles.

## ⚠ n IS NOT THE SAME IN EVERY ROUND, and this is the step that gets skipped

An interaction is a RATIO OF RATIOS, so its noise is not the cell's noise.
At the measured per-magazine CV of ~3%:

| n / cell | single | interaction `f(ab)/(f(a)f(b))` | slot-vs-part (two interactions) |
|---|---|---|---|
| 5 | 1.34% | 2.32% | 3.29% — sees only >6.6% |
| 8 | 1.06% | 1.84% | 2.60% — >5.2% |
| **12** | 0.87% | 1.50% | **2.12% — >4.2%** |
| 16 | 0.75% | 1.30% | 1.84% — >3.7% |

**Round 1 at n=5 is fine. Round 2 at n=5 is a gate that cannot see its own
question** — anything under 6.6% reads as "they agree, it is a slot property",
which is the root CLAUDE.md's self-consistent, arithmetically correct, blind
criterion. Use n=12.

## What is already known, so nobody re-measures it

⚠ **Orthogonality is a property OF THE GUN, and it already failed on one.**

    m416    slots essentially multiply                  +-4%
    vector  muzzle x grip                               -6.7%
    mp5k    muzzle x grip           +13.8%  [+11.0, +16.4]
    mp5k    all three parts         +23.6%  [+17.9, +28.7]
    mp5k    muzzle x stock           +1.2%   <- so it is the muzzle-grip PAIR,
                                                not "mp5k is odd"

Over nine cells where a whole kit and its singles were both measured, the
multiplicative assumption is off by a median **6.7%** — against **34.7%** for
the wiki's global per-part numbers, so the per-gun tier is 5x better, not a
compromise.

⚠ **Factors are never borrowed across guns.** `vert_grip` reads 0.7470 (mp5k) /
0.7723 (m762) / 0.7875 (aug) / 0.7959 (vector) — 6.5% apart with sems of 1%.
`pixi run kit` pins it.

⚠ **The wiki is not a fallback worth having in these slots.** `tactical_stock`
states −20% and measures **1.00 ± 0.01** (~25 sigma) — and it was the DEFAULT
stock part for a while, so the whole stock axis measured NOTHING until somebody
swapped in `heavy_stock` (0.8346). `laser` sits in the grip slot at 1.0058:
another identity. **The ordering is wrong too** — `thumb_grip` measures
strongest (0.7847) where the wiki ranks it second from last. Numbers live in
`data/kit_factors.json`, `src=measured` only.

## Where the agent belongs

`harness/night.py`: *"There is no agent in the middle... the model belongs at
the exits, where the frequency is low and the judgement is real."*

**Round 1 runs unattended. The agent reads it, chooses round 2's probes, feeds
them in, reads again.** Choosing is the judgement — pick the parts with the
LARGEST single effects, where an interaction is biggest and most detectable.

Ordering inside a round is arithmetic, not judgement: `pixi run gray` is the
mixed-radix Gray tour, N cells in N-1 kit changes. ⚠ `order_configs`
brute-forces 8! and only solves filled-vs-empty; `gray_order` handles a slot
with MORE THAN TWO values and is optimal by counting, because 60! is not
enumerable.

---

# Running it

## Rule 0: ask the game where it is

Most failed runs are state, not measurement. One command, drives nothing:

    pixi run ready          # running / focus / in match on the lane / Tab / panel

⚠ **If the HUD is unreadable everywhere at once, that is ONE fault, not four:
there is no HUD, because we are not in the game.** `posture ?? unreadable` +
`ammo ?? None` + an empty rack, with 「因长时间没有动作, 您已被踢出游戏」 on
screen — the dialog named its own cause and nothing was reading it.

⚠ **The idle clock that gets you kicked is the AGENT'S thinking time.** Four
kicks in one night, each preceded by a gap where I was writing analysis instead
of driving — once fifteen minutes. **Launch the next run first, analyse while
it fires.**

`calibration/state.py` is the deeper probe (ADS by two independent methods,
posture, ammo, texture gate). `--pico` opens the shared port; without it, it
does not touch it.

## The loop

| step | command | what it answers |
|---|---|---|
| 1. collect | `pixi run collect-timed --weapon aug --kit "muzzle=,grip=" --mags 8` | adds magazines to the store |
| 2. second arm | add `--from-fit --scale-sweep 1.0,0.5` | **makes the cell checkable at all** |
| 3. fit | `fit_time_curve.py --weapon aug --config bare` | one refit over everything ever stored |
| 4. ship it | same, `--write` | puts it where the RUNTIME reads |

⚠ **STEP 2 IS NOT OPTIONAL.** The licence to pool magazines is that magazines
fired under DIFFERENT curves, each with its own `y_comp` added back, estimate
the SAME `y_true`. A one-arm pool has never been checked, and `verdict.py`
fails such a cell closed — because "not checked" and "fine" are the two things
that layer exists to keep apart. **A fitter cannot fake it: it never sees which
arm a magazine came from.** Passing looks like this (m762 bare, three arms):

    delivered 1000 / 1500 / 2000 counts  ->  y_true 1322 / 1370 / 1309
    spread 1.04% against a 5% gate, and sd 45.6 / 149.2 / 12.7

⚠ Note the sd collapsing as the arm approaches correct: that is the nulling
measurement — **the better the compensation, the smaller `|y_obs|`, the more
precise the answer.** Collect on the compensated arm, not the empty one.

⚠ **STEP 4 was missing for a while.** `--from-fit` re-fits in memory, so
collection could iterate while `set_seq` — the thing that actually plays in
game — kept reading whatever file was on disk.

## A gun with no curve cannot be measured at all

Not "measured badly" — **not measured**. With no compensation the view reaches
open sky, where there is no texture, and phase correlation then returns **0
confidently**. An AUG on a zero baseline climbs 5.3 patch heights; the m762
needs 1962 counts against 1725 of pitch headroom, so it clamps mid-burst.

    pixi run kava4                                 what the seeds cover
    python tools/import_kava4.py --seed m762,aug   write them

⚠ **A seed does not have to be RIGHT, it has to be KNOWN.** `C` is read back
off the device, so `y_true = y_obs + C` is exact whatever `C` was — a wrong
baseline moves counts between the two terms and nowhere else. Same reason a
kitted cell can be seeded with an inaccurate kit factor.

⚠ **Pad the tail.** The community patterns are SHORTER than a magazine (m762
2.50 s against a 3.79 s burst), so the end of every burst — where recoil has
accumulated longest — fires uncompensated. Padding uses the median of the last
ten NON-ZERO knots: several patterns end on a sentinel (VECTORR `0.0`), and
padding with the literal last knot pads with ZERO while the report cheerfully
says "+179 padded knots".

A seed refuses to overwrite a fit, and refuses when the STORE can already fit
that cell. `[curves] ... is a SEED, not a measurement` prints once per curve,
because otherwise a guess and a measurement look identical.

## Unattended

    pixi run night --weapons ar --configs bare,grip --mags 6

It kits, fires both arms per cell, fits the pool and judges each cell against
`harness/verdict.py`. `--rejudge <run>` re-runs the verdict offline, which is
how a wrong threshold gets corrected without re-firing anything.

## It refuses to guess, and each refusal was paid for

| it checks | what fooled it | what that cost |
|---|---|---|
| the weapon | two mp5ks in the rack | read one gun's attachments, fired the other |
| the config | `--kit` asked for a stock, the gun wore the last cell's grip | 5 magazines filed under `bare` |
| the optic | `--sight` recorded the FLAG, not the readback | K wrong by ~3x, invisible downstream |
| the fire mode | `ensure_fire_mode` existed and no collection path called it | the mg3's two cyclic rates are 1.50x apart and no magazine says which |

**All of them look identical to success in the printed numbers**, and each was
caught only by asking a SECOND, INDEPENDENT source about the same object.

⚠ **ADS IS RECORDED, NOT REFUSED, and that is the correction to a row that
stood here.** `ads_frac` is `nan` on every magazine the timed path has ever
written — the grabber carries the tracker's patches and the detector reads the
screen centre, which is not among them — so a gate on it could never pass.
What exists is `Magazine.ads_end`: the two endpoints, `ensure_ads()` before and
one read at the release. It cannot see a dropout that recovers; it catches
dropping out and staying out. A magazine that ends out of ADS is stored and
flagged, never dropped. ⚠ The
clustering is NOT the backstop — it cut the mislabelled cell that day only
because a stock is worth 2x; a part worth 5% merges into its neighbour and
moves the mean with nothing to show for it.

⚠ **A freshly spawned gun is NOT bare** — PUBG bolts on whatever the backpack
holds, so strip explicitly: `--kit "muzzle=,grip="`. ⚠ **`iron` has no K**: an
empty scope slot maps to a key `RECOIL_SIGHT_PROFILES` does not hold, and such
a gun is now refused rather than analysed with the red dot's.

**`[!] REFUSING` is the feature.** Fix the game state, not the check.

# Reading the result

**The arms first, the spread second, the total last.**

| number | means |
|---|---|
| `agree_arms` / `agree_spread` | did the model's own assumption hold here |
| `n_kept` / `n_total` | how many magazines the clustering kept |
| `spread_counts` | median disagreement between the kept magazines |
| `dropped[]` | what was pushed out, and how far it sat |
| `total_counts` | y_true at the end of the span |

⚠ **A small `spread_counts` proves nothing alone** — nine magazines under one
curve agree beautifully and are still nine estimates of the same wrong thing.

**The clustering unit is a whole MAGAZINE**, because what ruins one — a hand on
the mouse, wrong posture, an attachment that did not seat, the correlator
losing the view — contaminates the entire trajectory, and that trajectory then
looks completely reasonable: smooth, monotone, right magnitude. **Point-wise
rejection cannot see it.**

## The one place the model is known to be wrong

28 magazines of m416 bare, four curve strengths spanning 3x:

    t 1.5 s      0.9%        <- the assumption is excellent here
    t <= 2.4 s   3.7-5.6%
    t >= 2.7 s   up to 15%   <- and ONLY the strongest arm falls away

**The mid-band is verified, the two ends are not**, which is why the judge's
agreement gate is set on 0.5–2.4 s. ⚠ Two claims about this were made and
withdrawn the same day — "the game has a 0.92 gain" (it is 0.98 mid-band; 0.92
was an endpoint ratio) and "y_true is an inverted U" (monotone to 2.4 s; the U
came from reading only the last point). **Both were computed correctly.** The
error was reading an aggregate that could not see the dimension it was asked
about.

---

# Traps

Each produced plausible wrong numbers rather than an error. ⚠ **Four of them
are now enforced by code and are here as SHAPES, not as chores** — the fifth
and sixth still need a human.

| trap | now |
|---|---|
| every state change is a toggle; a blind press lands wrong half the time | each is paired with a detector and watched until it agrees |
| watching by a fixed number of samples, not to a deadline — a 0.5-0.83 s window read "not in ADS", clicked again, and toggled back OUT | deadlines everywhere |
| homing to the midline TWICE per magazine 「压枪的时候会低两次头」 | once, in the loop |
| the patch height is the range: past height/2 = 128 px the correlation peak *wraps* rather than failing | `oor` records those pairs — stored, not dropped, because dropping is a fit-time decision |

⚠ **Posture can only be verified in ADS** — the icon does not render from the
hip. Order is always: read attachments (Tab) → ADS → verify posture → fire.

⚠ **Template drift is silent.** An attachment whose template stopped matching
becomes `<occupied, no template>`, and the config the samples pool under is
then a guess. `collect_timed` refuses instead of guessing. Rebuild via
`calibrate-template`, then `pixi run attachments`.

# When it breaks

| symptom | first move |
|---|---|
| every message is nonsense at once | not in the game. `pixi run ready` |
| `REFUSING: could not read the attachment slots` | the config key would be a guess. `pixi run attachments` |
| the cell fails on `agree` | one arm only. Fire a second `--scale-sweep` arm into the same config |
| the cell fails on `mags` | the main cluster is thin. Read `dropped[]` first — the outliers may be the majority |
| the cell fails on `tracking` | the correlator is not placing pairs. `detector/view_tracker.py` |
| the view flies to the sky | no curve for that cell. Seed it — see the seed section |
| the fit's total moves between runs | expected early. Every fit is a full refit, so a thin pool moves |

# Files

`MODEL.md` is the law. `calibration/samples.py` is the store (never deleted).
`collect_timed` fires into it, `fit_time_curve` clusters and fits (`--write`
ships, `--selftest` offline), `import_kava4` seeds a gun that has no curve,
`harness/night.py` runs unattended and owns `gray_order`, `verdict.py` judges a
cell, `kitting.py` puts a config on a gun and proves it, and
`data/kit_factors.json` holds the measured factors (`src=measured` only).

⚠ **A weapon with no curve in `config.CURVES_DIR` gets NO compensation** — the
honest state, not a regression. The 1184 curves fitted in the retired
coordinate were deleted rather than kept: bins anchored to the ammo counter,
played on a grid anchored to the click, is not a starting point.

⚠ **Ask `config`/`samples` for a path, never spell one.** Three paths in this
skill were still wrong after the commit that claimed to have repointed them —
a step written in Markdown is invisible to every import graph. `pixi run
pointers` is the gate.
