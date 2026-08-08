---
name: calibrate-compat
description: Establish what actually fits on what — which attachment slots a weapon has, and which attachments each slot accepts — by fitting parts in the training range and reading them back. Use when a drag lands nothing and it is unclear whether the slot is missing or the part is rejected, when a weapon is new or unverified, after a patch changes loadouts, or to replace a wiki-sourced guess in detector/attachment_catalog.py with a measurement. Not for what an attachment icon looks like (calibrate-template) or where the slots are drawn (calibrate-screen).
argument-hint: "<weapon key or 'all'> - drives the training range itself"
---

# Attachment Compatibility Calibration

Turn `detector/attachment_catalog.py` from a table of wiki readings into a
table of measurements.

## The table already exists — extend it, do not start another

`detector/attachment_catalog.py` is the single source of truth. Its shape
already covers all three failure modes:

| symptom | field |
|---|---|
| the gun has no such slot; a drag there lands nothing | `SLOTS[weapon]['slots']` |
| the slot exists but rejects this part (Tommy Gun: suppressor only) | `EXCLUDE`, `GRIP_ONLY` |
| the part fits but the table never listed it, so it is never fitted | *nothing detects this — only a scan does* |

Each entry carries `conf`. `SLOTS` is now **30/30 measured** (scanned
2026-08-02, `calibration/artifacts/compat/runs/20260802_155222/`) — but only for four of the
five slots, since `scope` is unreadable, and `EXCLUDE` / `ONLY` /
`GRIP_ONLY` are **still entirely inferred**. That is the remaining work.

The first scan overturned two entries, both of the dangerous kind — a slot the
table claimed and the weapon does not have, so a drag drops the part on the
floor: `ump45` stock and `js9` grip. 28 agreed exactly, including all six
`unverified()` had flagged. Wiki-sourced and guessed entries were wrong at
about the same rate, which is the argument for measuring rather than sourcing.

Never write findings into a CLAUDE.md or a doc instead of the table. Code
reads the table; prose is invisible to `fits()`.

## What the wiki is worth

Already scraped once, and already known to be wrong in specific ways — the
module docstring records them:

- **No entry at all** for K2, FAMAS, JS9, MP9, P90, Mk12, Dragunov.
- **Contradicts this repo** on the AUG's muzzle: the wiki lists the AUG under
  Suppressor only, `weapon_attachments.WEAPON_SLOTS` has had `comp` since the
  recoil scales were calibrated. Unresolved; one drag settles it.
- It cannot ever reveal the third failure mode, because a part missing from a
  list looks identical to a part nobody added.

Use it as a prior to order the work, never as an answer. The in-game loop is
automatic, so a measurement is cheaper than an argument about a source.

## Rule: the ground truth is what you asked for

`control/spawner.py`'s `give_*` spawns any item, `InventoryControl.equip` fits it,
`tab_items.detect` / `InventoryControl.read_slots` reads it back. **Whatever was
requested is what should read back** — no labelling, no human in the loop.
That is why this can run unattended over 30 weapons.

## The shared contract — capture once, analyse many times

Driving the game is the expensive part of every calibration here: the
foreground, the one Pico, and a place in the queue behind whatever other agent
is running. Analysing captures is free and repeatable. So a run is a shared
product, and the format is code, not prose each skill copies and lets drift:
**`calibration/capture_run.py`**.

```python
run = CaptureRun.create('slot_scan', note='...')
run.add_fit(frame, name, weapon, slot, asset)   # we asked for it -> truth
run.add_observed(frame, name, weapon, read)     # a detector read it -> context
run.labelled()                                  # ONLY the first kind
```

Runs live in `calibration/artifacts/runs/<kind>/<stamp>/`. List them with
`pixi run python calibration/capture_run.py`.

**Every label carries its `source`, and that is enforced, not advised.** A
template cannot be validated against samples a template labelled; the failure
is on record in `detector/CLAUDE.md`, where a drifted `Lower_ThumbGrip_C` made
Mk12's grip read as `laser` — in-catalogue, confident, wrong. Detectors do not
announce drift, they answer plausibly. So `labelled()` returns only
`LABEL_REQUESTED`: a part fitted on purpose and confirmed, whose identity came
from the request rather than from a reading. Code wanting ground truth cannot
silently receive the other kind.

Before collecting anything from the game, check whether a run already has it.

## Step 0 — get there

`tools/drive_screen.py` (see **calibrate-screen**) handles focus, the lobby,
the TRAINING gate and opening the panels. Never ask the user to open anything.

The hardware is shared: one Pico, one game window, other agents run long
captures. `Pointer` names the pid holding the port — wait, do not kill.

## Step 1 — read slot existence off a screenshot; it is 30 spawns, not 1230 drags

`detector/slot_detector.SlotDetector` answers
`absent | empty | filled` per slot, so **which slots a weapon has** costs one
spawn and one screenshot per weapon. Verified 6/6 against the captures with
known ground truth (UZI no grip, Mk12 no stock, G36C no stock, stripped M416
all five).

Two independent judgements, because none separates all three states:

| | measure | absent | empty | filled |
|---|---|---|---|---|
| presence | Sobel p90 on the tile's **border ring** | 5.0 … 26.0 | 46.0 … 172.7 | (same as empty) |
| occupancy | Canny edges **inside** the tile | — | 0 … 71 | 202 … 885 |

Presence reads the border and ignores the fill, so it is unaffected by what is
fitted; occupancy reads the fill and ignores the border. Threshold 36 sits in
a gap of 20. Verified 28/28 slots.

The two windows this needs, and why they are two, are **calibrate-screen Step
2.5** — read it before touching the crops. In short: `HUD_REGIONS['att_*']` is
63×63 of tile interior, cut for the template matcher and correctly excluding
the border; the tile is 66×66, so that crop sits wholly inside it and cannot
see whether the tile exists. `tab_layout.slot_window()` is the outer crop and
`SlotDetector` reads it. Never widen the inner one to serve both.

**⚠ `scope` always returns `unknown`, and no threshold fixes it.** That slot
draws no tile: an empty scope on an M416 shows only backdrop and weapon body,
and a VSS — which has no scope slot — draws its integral PSO-1 in the same
place, because that optic is part of the weapon's own art. Empty-and-present
is pixel-identical to absent, and occupancy breaks too (the VSS reads 678
interior edges with nothing fitted, well past the 120 that means "filled").

So scope presence comes from a drag (Step 2), and scope *contents* from
`AttachmentDetector`, which reads the VSS correctly as empty. Nearly every
weapon has one, so little is blocked — but never let `unknown` become
`absent`.

Integral parts in general show up as weapon art rather than slot contents.
Expect the same for P90 (holo + laser + suppressor) and MP9.

## Step 2 — drags settle what the screen cannot

The screenshot answers *which slots exist*. It cannot answer *which parts a
slot accepts* — Tommy Gun's muzzle takes a suppressor and refuses a
compensator, and both make the same tile. That needs fitting.

For those, define it operationally, because the game exposes nothing else:

> A slot **accepts** a part if fitting it and reading the slot back returns it.

**Never conclude "rejected" from one failed drag** without knowing the slot is
present — a failure against an absent slot is ambiguous by construction. Step 1
first, then drags only against slots that exist. That is what turns ~1230
blind drags into a scan of the handful of slots whose contents are in doubt.

## Step 2b — the drag layer produces calibrate-template's ground truth

Fitting a part you named is the only place in this repo where an icon's
identity is **specified rather than detected**. `InventoryControl.equip(gun,
slot, att='vert_grip')` succeeded means that tile now holds the real rendering
of `vert_grip`, no matter what any template says. Screenshot it and you have a
labelled sample.

That matters because the alternative is circular: `AttachmentDetector` cannot
supply the ground truth for its own templates, and per `detector/CLAUDE.md`'s
first law a drifted template does not fail — it returns a plausible wrong name
with a healthy margin (`Lower_ThumbGrip_C` drifting made Mk12's grip read as
`laser`). Two templates are already known to have drifted and re-cutting them
needs exactly this: samples whose identity does not come from a detector.

**So the drag scan writes captures for both skills** and the expensive part is
paid once. Use `CaptureRun.add_fit(frame, name, weapon, slot, asset)` — and
call it only after the equip actually verified, since a label recording an
intention rather than an outcome is wrong exactly when the drag silently
failed.

**The screenshot scan (Step 1) does NOT produce this**, and the format says so
rather than trusting anyone to remember: it writes `LABEL_DETECTED`, so
`labelled()` returns 0 for a `slot_scan` run. Those captures are geometry
evidence only.

## Step 3 — auto-fit is a free prior, and a trap

Spawning a weapon makes PUBG fit compatible parts from the backpack
automatically. A fresh M416 came out wearing a red dot, compensator, vertical
grip, extended quickdraw and stock, none of it requested.

- **As a prior**: whatever it auto-fitted is compatible, confirmed, for free.
- **As a trap**: a "bare" spawn is not bare, so any measurement that assumes an
  empty gun is wrong. `InventoryControl.strip(2)` empties it, and verifies each
  slot reads empty afterwards.

Strip before measuring. Note the auto-fitted set first — it is free data.

## Step 4 — verify the fit, not the drag

`InventoryControl.equip` already reads the slot back and reports `verified`.
Trust that, with one caveat from `detector/CLAUDE.md`'s first law: a drifted
template reports a plausible wrong name rather than failing. `Lower_ThumbGrip_C`
drifted and made Mk12's grip read as `laser` — in-catalogue, confident, wrong.

So for a **new** finding — a fit the table did not predict — take a screenshot
too. `tools/verify_kit.py` exists for exactly this and writes the readback next
to the picture. A surprising result is precisely when the detector is least
trustworthy.

## Step 5 — write it back with its confidence

Update `SLOTS` / `EXCLUDE` / `GRIP_ONLY` / `ONLY` and set `conf='measured'`.
Keep the comment that says what was seen, in the style already there.

Record the negatives too. "Nothing of this class fits" is a measurement, and
without it the next run repeats the same 8 drags.

Then re-run `unverified()` — it is the progress bar for this skill.

## Traps

- A drag onto a full slot **replaces**; onto a slot the weapon lacks it drops
  the item on the ground or bounces back. Read the slot, not the mouse.
- The backpack fills up. `物品 N/200` in the spawner panel's top right is the
  count; `control.stock.restock` and `SpawnerControl` are the tools.
- Attachment classes are not weapon classes: `supp_ar` vs `supp_smg` look
  nearly identical and blind matching separates them at only 1.3x. Pass the
  weapon so `tab_items` can narrow the candidates — `attachment_catalog` is
  what narrows them, so a wrong table degrades the very detector verifying it.
- Vaulted weapons are in `ROSTER` but not live; gate on `is_live()`.
- Console is cp1252 here — `sys.stdout.reconfigure(encoding='utf-8')` before
  printing any Chinese item name.
- All coordinates are 3440x1440.
