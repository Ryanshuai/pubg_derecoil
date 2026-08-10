---
name: calibrate-gun-tag
description: Extend and re-verify GunTagDetector — the boxed slot number 「1」「2」 drawn in the Tab weapon rack — by cross-checking it against the weapon name plate read off the SAME frame, and sending only the disagreements to a human eye. Use when the panel-open judgement fires on a frame with no panel, when a loadout read is attempted on an empty rack, after a patch moves or restyles the rack rows, or to grow the 38-frame corpus past its one gun / one session / one map limit. Not for the bottom-right HUD weapon ghost (that is weapon_hud_detector) and not for the plate template itself (calibrate-template).
argument-hint: "<a directory of saved Tab frames, or nothing to use the tab_watch sink>"
---

# Gun-Tag Calibration (the boxed 「1」「2」 in the rack)

`detector/gun_tag_detector.py` answers **"is the boxed slot number painted?"**,
which is sharper than "is the inventory open": the box is drawn only when the
panel is up **and** a gun occupies that slot — the actual precondition for
reading a loadout. Criterion is white-on-dark, jointly, in a 43×35 rectangle.
Its measurements live in that file's docstring. **This file is the procedure.**

Always `pixi run python <script>`. Offline gate: `pixi run gun-tag`.

---

## Rule 0 — the second witness is the plate's NAME, not its ink

The name plate `gun_name_N` sits at x 2275..2525, immediately right of the tag
box at x 2216..2259, **in the same horizontal band and inside the same grabbed
rectangle** (`tab_blocks()['right']` = y 123, x 2219, 557×629). Both are
painted under the same condition: panel up **and** a gun in that slot.

> **So the plate is an independent witness on the same frame, at the same
> moment, for free.** No second grab, no Tab toggle, no "did anything change
> in between".

That is the same property `gun_tag_detector`'s own docstring gives as the
reason it lives on this block: *"Reading both off one frame makes 'the record
describes the object that was measured' true by construction."* This skill just
uses the other half of that rectangle.

⚠ **The witness is `TabWeaponDetector.classify`, not `.ink`.** Measured on the
38-frame corpus:

| second witness | agreement with the tag |
|---|---|
| `classify` names a gun | **38/38** — 16 panel frames all read `sks`, 22 world frames all read `''` |
| `ink > 0` | **36/38** — two world frames read ink **8132** and **9234** |

Those two are the exact failure the tag detector was built to replace: the
play log recorded ink 11248 on bare sky, a false positive *bigger* than a real
plate's few hundred. **Ink is not independent — it fails the way the refuted
criteria fail. A name is: sky has white, but sky does not spell SKS.**

Witness ranking for this screen:

| | strength | note |
|---|---|---|
| the spawn request | strongest | only exists if you collected on purpose |
| **the plate's name, same frame** | independent and machine-checkable | the default; costs nothing |
| your eye on a contact sheet | last | it is what labelled the existing 38, and it is why the filenames were wrong on 8 of them |

**A frame the request or the plate already settled is never relabelled by eye.**
The eye adjudicates disagreements; it does not vote on agreements.

---

## Step 1 — ⚠ strip the anchor strip before pasting anything back

`control/tab_watch.py:_compose` saves **the 「类型」 anchor strip laid above the
panel block**, so files in the sink are **587** rows tall where
`tab_blocks()['right']` declares **557**. Paste one at the block origin without
removing the strip and every row inside shifts by 30 px: 「类型」 lands in the
tag box, the plate window lands on the row below, and **both detectors return
confident, plausible numbers off the wrong pixels.**

Measured, same 170 frames, same code, only this line different:

```
pasted whole   80 / 340 slot-readings "disagree"    <- all fake
strip removed   2 / 340                             <- both real
```

```python
BH = tab_blocks()['right'][2]        # 557
body = crop[crop.shape[0] - BH:]     # 0 rows off a bare block, 30 off a composed one
```

⚠ **`gun_tag_detector._selftest` pastes with no strip handling**, and is right
only because its 38-frame corpus predates `_compose`. Anything new that reads
the sink must do the subtraction, and must derive it from `tab_blocks()` rather
than from the literal 30.

---

## Step 2 — cross-check whatever frames exist

```
pixi run python .claude/skills/calibrate-gun-tag/scripts/cross_check.py
pixi run python .claude/skills/calibrate-gun-tag/scripts/cross_check.py <dir> --out <review_dir>
```

Default source is `calibration/artifacts/robot/tab` — every Tab press in a play
session lands a block there, **and the plate names the gun on each one**, so the
corpus grows from ordinary play at zero collection cost. It was 170 frames when
this was written, 132 of them newer than the corpus and unlabelled.

It prints the 2×2 per slot and writes contact sheets plus a seeded
`verdicts.jsonl` for the disagreements only.

**What the first run bought** (170 frames, 340 slot-readings):

```
slot 1   no tag / no name  94     tag + name  74     disagree 2
slot 2   no tag / no name 112     tag + name  58     disagree 0
```

⚠ **Those 58 slot-2 positives close the corpus's biggest documented hole.** The
detector's docstring says *"slot 2 was empty throughout — so the corpus says
nothing about a second gun"*. It does now, and nobody had to open the game.

---

## Step 3 — adjudicate the disagreements by eye

Read the sheets with the **Read** tool. Each row is `[tag box | name plate]`
side by side with the tag's two numbers, because **the verdict is about whether
those pixels are a slot number, and a bool cannot be argued with.**

Three verdicts, and there is deliberately no `relabel`:

| verdict | means | consequence |
|---|---|---|
| `tag_false_positive` | the box holds something that is not a slot number | the frame joins the corpus as a **negative** — it is the sample the criterion cannot yet refute |
| `tag_false_negative` | a slot number is painted and the tag missed it | a threshold question; go and see which half refused (`score()` returns both) |
| `plate_cannot_read` | a number IS drawn but the plate has no template for that gun | not a tag finding — send it to `calibrate-template`. Chinese-only names (slr / tommy / dragunov) are the known set |

Write one sentence of reason saying **what you saw**, not what you concluded.
Unsure is legal: leave `verdict: null` and say why.

**The first run's two disagreements are the worked example.** Slot 1 on
`0809_161916_197_press` and `_263_release`: `white 86`, `median_v 12.0`, plate
ink `0`. The box holds a **green 「En in 🕐 22d」 event-timer pill** — white
glyphs on a dark background, which satisfies **both** halves of the criterion
while being nothing to do with a weapon rack. `tag_false_positive`.

> A one-gun / one-session / one-map corpus could not contain it, and 38 frames
> of clean separation said nothing about it. **The gap between "the thresholds
> sit in an empty gap" and "the criterion is right" is a UI element nobody had
> photographed.**

---

## Step 4 — grow the corpus in the direction it is thin

The detector names its own limits: **one gun, one session, one map, slot 2
empty throughout**, and its frames are 3 columns short of the box. Slot 2 is now
covered (Step 2). What is still uncollected, in order of how cheaply it breaks
things:

1. **Other white-on-dark UI in that rectangle** — the timer pill proves the
   class is non-empty. Menus, event banners, tooltips, killfeed.
2. **A second gun and a night map** — the corpus's panel V is 29..51 and its
   world V is 112..205; a night map moves the world half toward the panel half,
   and `DARK_V_MAX = 80` is the threshold that gap is holding up.
3. **Mid-transition frames.** The tag and the plate are painted by the same
   panel, but not provably in the same frame. Every `press`/`release` pair in
   the sink is a candidate; a systematic tag-before-plate ordering would make
   the second witness lag by a frame, and this cross-check is what would show it.

Collect by driving the game only for 2 and 3. For 1, the frames arrive by
themselves — the sink fills whenever anyone plays.

---

## Step 5 — the gates

- `pixi run gun-tag` is the offline gate, **38 frames labelled by eye**. Adding
  a frame means adding it to `calibration/artifacts/gun_tag/` under a
  `panel__` / `world__` prefix — and the label is the prefix, **never the
  filename tab_watch chose**, which is what it *believed*, wrong on 8 of 38.
- ⚠ **The gate checks margin, not just verdict**, and any new frame must keep
  both thresholds strictly between the two classes. A frame that makes
  `WHITE_MIN` or `DARK_V_MAX` stop separating is not a frame to drop — it is
  the finding, and the criterion has to change instead.
- Both sides. Adding only negatives makes the gate stricter and quieter; adding
  only positives makes it looser. Report the two ranges every time.
- After any "edit a constant → run → edit back → run":
  `find . -name __pycache__ -type d -exec rm -rf {} +`.

---

## Pointers

| | |
|---|---|
| `detector/gun_tag_detector.py` | the criterion, the four refuted alternatives, the 38-frame numbers, `score()` returning both halves |
| `detector/tab_layout.py` | `gun_tag_box` / `gun_tag_point`, and why the rectangle is a constant and not re-derived from the point |
| `control/tab_watch.py` | `_compose` (the strip), the sink path, and why the grab happens before anything is decided |
| `detector/weapon_template_detector.py` | the witness — `classify`, `ink`, and why `ink` is the weaker question |
| `detector/CLAUDE.md` | 第三铁律 (a gate only refutes what its corpus holds) — the timer pill is a fresh instance |
| `calibrate-template` | plate templates, including the Chinese-only names |
| `calibrate-screen` | if the rows themselves moved |
