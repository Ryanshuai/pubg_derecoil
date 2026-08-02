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
`thumb_grip` are both drifted today. See *when a thing cannot be seen* below.

**The game dresses guns by itself.** PUBG auto-fits whatever the backpack holds
onto a weapon the moment it arrives, so any slot a config does not name is not
empty — it is whatever the last strip left lying around. A "bare" run came back
wearing a cheek pad, which reduces recoil.

**Strip before spawning the next weapon.** A full rack means the incoming gun
evicts the old one onto the floor, wearing everything it had on.

**The range evicts after 20 minutes** and re-entry empties the backpack and the
rack — it is a restart, not a pause. `RangeSession` re-enters on a 17-minute
budget so it happens between weapons rather than mid-magazine, and re-stocks
afterwards; `--resume` picks up completed cells from the JSONL.

Re-entry is automated (`--session auto`, the default): `lobby_control` drives
the results screen, the lobby, an open ESC menu or a loading screen back to a
running round, polling state rather than sleeping. Measured round trip: in
11.5 s, out 7.4 s.

**The lobby only takes clicks.** The PLAY button draws an "F" hint and the code
took it at face value — three F presses, game verified frontmost, lobby
unmoved. The lobby has a real cursor sitting wherever it was left, so the
cursor has to be driven to the button rather than avoided.

**Leaving the range needs two clicks.** `LEAVE TRAINING` raises a CONFIRM /
CANCEL dialog, and that dialog reads as `FULLBLEED` — identical to a loading
screen, which wants the opposite treatment. `exit_to_lobby` asks
`leave_confirm_visible()` before it looks at the state. Symptom when this was
missing: "the exit worked, then we lost focus one step short of the lobby". Two things it cannot know,
both caught immediately afterwards by trying to open the spawner — **which**
mode it entered (F starts whatever the lobby had selected, so leave the lobby
on the training range) and **where** in the range it landed (walking to a
spawner is not automated).

**Focus is taken, not waited for.** Every tool here launches from a terminal,
so at t=0 the terminal is frontmost and the game is not — the guard fires and
the run aborts having done nothing. Do not ask a human to alt-tab; that human
is exactly what an unattended harvest exists to remove. `ensure_focus()` raises
the game window, verifies, retries, and only then falls back to the countdown.

    from press.pointer import ensure_focus, focus_keeper
    if not ensure_focus(countdown_s=args.countdown, label='...'): return 1
    time.sleep(0.6)          # the game eats input for a few frames after this

Mid-run, `focus_keeper().ok(where)` takes the foreground back — bounded at 5
regains per process, because a run that keeps losing focus has something
contending with it and every keypress in between went elsewhere. **To stop a
run by hand, Ctrl-C the terminal**: it will fight you for focus up to 5 times
before giving up.

**Focus is checked by executable, and so is the window search.** This
repository's own name contains "pubg", so a title match calls an editor window
the game — both for "am I focused" (the guard passes while the game sits in the
background) and for "which window do I raise" (it raises the editor). Matched
on `TslGame.exe` at both ends; `game_hwnd()` takes the largest visible window
of that process, since PUBG owns several and only one takes input.

## When a thing cannot be seen

A part that will not detect stops the run, and squinting at one screenshot is
the slow way to fix it. Collect instead:

    pixi run python calibration/collect_icons.py --slot grip --angles 6

It spawns one of every attachment in that slot, then photographs the inventory
against several backgrounds, turning the view between captures. Two outputs:

- **labelled crops** — `<item>__<background>__rowNN.png`, which is what
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
name it**. That is the one situation where a broken template cannot hide, and
it is why this is a collector and a self-check in the same pass. If the row
count does not grow by exactly what was ordered, it stops rather than
mislabelling every crop.

`--check-only` skips spawning and grades whatever is already in the backpack.

Turning is done with Tab shut. With the inventory open the mouse drives a
cursor rather than the view, so a turn issued there moves nothing and every
capture comes back identical.

Then hand the crops to `calibrate-template`, and re-run with `--check-only` to
confirm the coverage table went clean.

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
| `ABORT: game not focused` | the game is not running, or `game_hwnd()` returns None — check with `python -c "from press.pointer import game_hwnd; print(game_hwnd())"`. Windows can also refuse the handover; run it again |
| "not on screen" for a part that is | `state.py --tab` → `UNRECOGNISED`? then `collect_icons.py --slot <slot>` |
| "could not reach posture" | `state.py` — in ADS? inventory closed? |
| "inventory would not open/close" | `state.py`, check the `type` pixel count against its window |
| could not open the port | another tool has it; the error names the process. Wait, do not kill it |
| residual fine, gun still sprays | you optimised the endpoint — look at wander |
| a weapon measures implausibly mild | check `oor`, and whether the view hit the pitch limit |
| spawner would not sync | `state.py` — is the panel actually up? are we still in the range? |
| "in a match, but the item spawner will not open" | lobby was on the wrong mode, or the spawn point is not next to a spawner — walk there and re-run |
| re-entry never completes | `detector/lobby_detector.py` `selftest()` — the ping overlay is a user setting, and without it the detector degrades to lobby/not-lobby |

## Files

| | |
|---|---|
| `calibration/state.py` | read-only state probe — start here |
| `calibration/collect_icons.py` | spawn parts, photograph them, grade the templates |
| `calibration/harvest.py` | spawn, dress, fire, measure; the unattended loop |
| `calibration/sweep.py` | same measurement without the spawner, for a gun already in hand |
| `calibration/fit_curve.py` | residual → new curve |
| `calibration/range_session.py` | 20-minute eviction, budget and re-stock |
| `detector/lobby_detector.py` | lobby vs match, by letterbox bars and the ping overlay |
| `detector/lobby_control.py` | drives the lobby back into a match |
| `calibration/weapon_switcher.py` | weapon supply interface |
| `detector/view_tracker.py` | the measurement itself |
| `press/pointer.py` | focus: `ensure_focus`, `focus_keeper`, `game_hwnd` |
| `docs/game_quirks.md` | mechanics found by hitting them |
| `docs/recoil_observer_design.md` | why the ROI is where it is |
