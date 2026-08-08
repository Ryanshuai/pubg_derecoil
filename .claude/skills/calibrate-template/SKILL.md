---
name: calibrate-template
description: Give a detector a picture it can match and prove it beats every near neighbour — icons, rendered UI text and HUD digits, plus where an icon is drawn and how it is blended. Use when a detector stops recognising something, after a game update or language switch, when a new weapon or attachment reads as unknown, or to add a template variant. For row/slot geometry use calibrate-screen.
argument-hint: "<what to calibrate> - a screenshot, a run directory, or open the screen in game"
---

# Template Calibration

Get a detector a picture it can match, then prove it beats every near
neighbour. Three kinds of target, one pipeline:

| kind | example | extract by | score by |
|---|---|---|---|
| **icon** | attachments, spawner buttons, posture | threshold the opaque part, or unmix the blend | MSE, `TM_CCOEFF_NORMED` |
| **text** | weapon name plate, `Type` / `类型` | white-achromatic mask | windowed IoU |
| **digit** | the ammo counter | one height window, no morphology | IoU |

Always `pixi run python <script>` — a bare `python` here is hijacked by a
broken nsight-compute shim. `detector/CLAUDE.md` holds the per-detector
measurements and is already loaded; this file is the procedure, not a copy.


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

Runs live in `docs/runs/<kind>/<stamp>/`, except the two whose directory is
itself ground truth for another regression (`docs/ads/runs/`,
`docs/attachments/runs/` — see `capture_run.py`). List every one of them with
`pixi run python calibration/capture_run.py`, and open any of them, in any of
the three on-disk shapes, with `CaptureRun.load_dir(<directory>)`.

A run captured before this format reads back with **zero** ground truth on
purpose: its files carry no `source`, so nothing in them separates a confirmed
request from a detector's reading, and the format must not invent the stronger
of the two.

**Every label carries its `source`, and that is enforced, not advised.** A
template cannot be validated against samples a template labelled; the failure
is on record in `detector/CLAUDE.md`, where a drifted `Lower_ThumbGrip_C` made
Mk12's grip read as `laser` — in-catalogue, confident, wrong. Detectors do not
announce drift, they answer plausibly. So `labelled()` returns only
`LABEL_REQUESTED`: a part fitted on purpose and confirmed, whose identity came
from the request rather than from a reading. Code wanting ground truth cannot
silently receive the other kind.

Before collecting anything from the game, check whether a run already has it.

## Rule 0 — prefer an icon over text

Icons survive a language switch; text does not, and every threshold measured
on one language is wrong for the other. `spawner_detector` identifies its
screen from three button glyphs and works in every language;
`tab_detector` uses the `Type` string's pixel count, and those bounds are
*silently wrong* under 类型. Use text only when the thing genuinely is text.

## Step 0a — after a patch: is it still the same item?

A key resolves to a POSITION in a spawner category — the drive path reads no
text, deliberately. Add or remove an entry and everything below it moves;
**replace one in place and nothing moves at all**: the collector photographs
the new item, files it under the old key, and every readback stays consistent
forever. 41.1 swapped the Angled Foregrip for the Tilted Grip keeping the list
length and the label 斜向握把, and four months of "angled_grip" recoil numbers
were another part's.

**The check: spawn one of every entry in the category into a level-3 backpack,
Tab, capture the FULL screen, and read the names.** The 库存 list prints the
item name beside each row, so one screenshot names the whole category in order.
Use `capture.cropper.capture_screen()` — `InventoryControl.frame()` is a
banded grab that cuts the labels off, and a wrong crop invents things.

A part scoring `<nothing>` on *every* sample in `pixi run attachments` is the
free version of the same alarm; three did, and all three were stale art rather
than a swapped item.

**If an item was replaced**, rename the key and keep its POSITION —
`attachment_catalog.RENAMED` + `canonical()` keep stored labels resolving.
Never rewrite labels in stored manifests: they are pictures of the right item
under an old name.

**There is no game-file art left, and none may come back.** Every 2026-03-18
extract was deleted on 2026-08-05. The extract is the INPUT to the game's
compositing and a detector only ever sees the OUTPUT, so it is not a spare
copy — it can win the fine pass on crops it describes worse. Measured twice,
on two different surfaces: attachments read `light_grip` / `comp_sr` /
`scope_15x` as nothing at all until the art left, and the weapon HUD scored
0.489 on art against 0.975 on captures. A new item's template comes from the
screen or it does not exist yet.

The cost is real and was accepted: an asset holding only `.solved` has no
row-scale picture, so `scope_6x` and `uzi_stock` lost their 库存 rows outright.
Collect `.row` for them (see `calibration/score_attachments.py: BASELINE`).

## Step 0 — check what is known

`config.py` for regions (`HUD_REGIONS`, `SPAWNER_ICON_*`, `TAB_COUNT_*`) and
`dl_models/icon_merging.py` for blend formulas — each function there carries
its verified parameters in its docstring. `config.ASSET_DIR` maps icon type →
directory, but not every directory is listed (`posture/`, `ammo/`, `lobby/`
are not), so also `ls docs/training_data/pubg_assets/`. Existing captures:
`docs/attachments/runs/*/`, `docs/spawner/runs/*/`, `docs/tab_inventory*.png`.

## Step 1 — get the pictures

Which path applies is decided by what is already in hand:

| in hand | path |
|---|---|
| only the game, and a batch to do | **A — collect** |
| a `with_ui` / `no_ui` pair | **B — unmix** |
| a template, and the question is where/how it is drawn | **C — locate** |

Park the cursor first (`press.pointer.move_cursor`) — hover restyles icons and
text and bleeds bright pixels past their bounds. Take several captures with
**different scenes behind the panel** wherever the UI is translucent: that is
both the opacity test in Step 2 and what stops a one-off antialiasing artefact
being baked in.

### Path A — collect from the game

**Check the runs before driving the game** — see the shared contract above.
`CaptureRun.labelled()` is the source of samples here; a compatibility drag
scan (**calibrate-compat** Step 2b) fits named parts and captures each one,
which is the same batch this step would otherwise collect at full price.

`slot_scan` runs are the counter-example and will hand you nothing:
`labelled()` returns 0 for them, because those weapons wear whatever PUBG
auto-fitted and only the detector under test can name it.

`calibration/collect_templates.py` spawns known items and photographs the Tab
screen against many backgrounds. Ground truth is self-specified, so it can
label a target whose template is the broken thing — which hand-cropping cannot.

```bash
pixi run python calibration/collect_templates.py --plan --all      # no game needed
pixi run python calibration/collect_templates.py --all --targets slots,plate,type
pixi run python calibration/collect_templates.py --slot grip --targets rows
pixi run python calibration/collect_templates.py --plates          # every weapon's plate
pixi run python tools/collect_ammo_digits.py --write               # the ten ammo digits
```

Targets: `slots` (icons fitted to the gun), `rows` (库存 list), `plate`,
`type`. A run lands in `docs/attachments/runs/<stamp>/manifest.json` — **start
from `facts.bad`**, one entry per target with no template, never matched, or
matched on some backgrounds only, each carrying the region and the crops. Runs
written before 2026-08-03 carry an `index.json` instead; `CaptureRun.load_dir`
reads either.

Only `slots` and `rows` come back from `labelled()`. `plate` and `type` are
captured but carry NO label — see `label_for()` for why, and note that the
plate one is a real hole rather than an oversight: nothing reads the gun back
without using the very OCR under test, so a spawn that silently produced
nothing would photograph the previous weapon under the new name.

Collect `slots,rows` together before calling a template fixed: same artwork,
different sizes and blends, and one can pass while the other fails —
`Stock_SniperRifle_CheekPad_C` matches in a weapon slot but not in a list row.

### Path B — unmix a with_ui / no_ui pair

For a target that is *entirely* alpha-blended with no opaque part to keep. The
formula is the game's, not the icon's:

| HUD area | function | formula |
|---|---|---|
⚠ **下表里那些函数名指的是 2026-08-06 已删除的代码**（`dl_models/icon_merging.py` 的 `alpha_blend` / `blend_tab_background` / `blend_attachment`）：它们的入参是带 alpha 的美术图，而美术图 2026-08-05 已全仓库删除，函数因此零调用方。**公式是实测的、仍然有效**——名字当标签读，别去 import。

| Weapon HUD (right) | `alpha_blend` | `a*strength*fg + (1-a*strength)*bg` |
| Tab panel background | `blend_tab_background` | `blur(bg, k=41) * 0.49` |
| Tab slot, occupied | `blend_attachment` | `a*icon + (1-a)*(0.37*blur(bg,k=49,σ=8) + 44)`, 63×63 with the 2px bevel cropped |
| Tab slot, empty | `blend_attachment(…, None)` | `0.50 * blur(bg)` |
| Status bar (fire mode) | `blend_status_bar` | `a*255 + (1-a)*gradient*blur(bg,k)` |
| Posture | `alpha_blend` | plain alpha, **no** blur/darken |

```bash
pixi run python "${CLAUDE_SKILL_DIR}/scripts/extract_template.py" --mode alpha \
    --with-ui <a.png> --no-ui <b.png> --region <x1>,<y1>,<x2>,<y2> \
    --output <t.png> --save-dir docs/<icon_type>/
# fire mode: --mode status_bar --blur-k 21 --gradient 0.67
```

**The script's status_bar defaults (`blur_k=17, gradient=0.65`) are stale** —
the first guess. `blend_status_bar` was later verified at **21 / 0.67**
(gradient 0.65~0.69, bar y=1312~1370). Pass them explicitly; a `recon_error`
from the defaults is not evidence about the icon. Batch form takes several
`--with-ui`/`--no-ui` files plus `--output-dir`.

`recon_error` <1.5 excellent, 1.5~3 acceptable, >3 means wrong params — or a
wrong region, or a mismatched pair, both far likelier than a new discovery. If
a search really does move the parameters, update `icon_merging.py`'s docstring
and this file together, naming the frames. **Always view the `_alpha.png`**:
clean shape, bright = active, dim = watermark, no background leaking in.

Path A cannot feed this — unmixing needs the UI off and it cannot be turned off
mid-run. What a collector gives instead is the same icon over many *different*
backgrounds, which separates artwork from scene the other way round, and is
what `blend_attachment` was verified against.

### Path C — where it is drawn and how it is composited

```bash
S="${CLAUDE_SKILL_DIR}/scripts"; D=docs/<icon_type>/
pixi run python "$S/diff_overview.py" <with_ui> <no_ui> --save-dir $D
# crop the region and LOOK at it (Read tool) before matching
pixi run python "$S/search_icon.py" <icon> <shot> <x1> <y1> <x2> <y2> --save-dir $D
pixi run python "$S/analyze_blend.py" <icon> <with_ui> <no_ui> <x> <y> <scale_pct>
pixi run python "$S/search_icon.py" <icon> <shot> <x1> <y1> <x2> <y2> \
    --verify <no_ui> --alpha <alpha> --save-dir $D
```

Tight search box, ~50px margin. Score >0.99 excellent, <0.95 suspicious (wrong
icon?); `reconstruction_error` <3 excellent, >10 wrong blend mode; `mean_diff`
<3 good alignment. Report as `config.py` would take it: an `x1/x2/y1/y2` dict
plus a `_BLEND` dict naming the formula and its parameters.

Path C also produces what Path B needs as input, so a B that reconstructs badly
is usually a C that was never done.

## Step 2 — cut it

**icon.** First find which pixels are stable, across those different-scene
captures (docs/spawner/README.md §4 is a worked example):

- *achromatic?* `|max-min|` over BGR ≤2 → a grey or binary template is safe.
- *opaque?* spread across scenes. The spawner buttons' bright pixels moved ≤6 grey levels; their dark parts moved up to **86** — those are alpha-blended and carry the scene through, so a template including them tracks the background and matches nowhere else.

Threshold at whatever isolates the opaque part (200 for those buttons → a flat
~221 glyph), save the binary mask, match with `TM_CCOEFF_NORMED` in a small
window around a fixed anchor.

**text.** Near-white and achromatic where the background is neither;
`_white_text_mask()` in `weapon_template_detector.py` is the reference
(`gray>180`, max channel spread <30, `MORPH_OPEN 3x3`). **Look at the PNG**:
broken strokes = threshold too high, glyphs bridged = too low or the kernel too
small. Crop tight — padding scores against pixels the template cannot explain.
`tools/probe_gun_name_ocr.py --extract <png>:<gun>:<key>[:<tag>] --write` does
the cut, from a full screen or from a `plate__*.png` straight out of a
`--plates` run; `--variants` lists what is stored.

**digit.** Segmentation is a height window and nothing else; the measurements
are in `detector/CLAUDE.md`. `tools/probe_ammo_ocr.py --extract <shot> --write`,
or `tools/collect_ammo_digits.py --write` to fire a magazine and self-label.

## Step 3 — variants, never overwrites

A template for another language is a *variant*. All variants are matched every
frame and the best wins, so a mid-run language switch still reads with no flag
anywhere:

```
docs/training_data/ocr_white/slr.png      sole or default
docs/training_data/ocr_white/slr.cn.png   自动装填步枪
docs/training_data/ocr_white/slr.en.png   SLR
```

~1 ms per extra template over a 250x45 plate, on Tab frames only. Templates
live in `docs/training_data/ocr_white/` (plates) and `docs/training_data/pubg_assets/`
(icons and digits, by subdirectory).

## Step 4 — score against everything, not just itself

A template is good only if it beats every *other* template on its own target.
Run the whole set against the whole set and read the **margin**, not the top
score — a thin margin is a future misread.

- text: correct match ≥0.85 (`TMPL_THRESHOLD`)
- icon: report separation the way docs/spawner/README.md §4 does — the spawner anchors score 0.989–1.000 on 24 positives and 0.000 on negatives, hence 0.55 with room both sides
- digit: `probe_ammo_ocr.py --confusion`, then the offline sweep with no flags, then `--selftest`

**A missing template does not read as nothing — it reads as the nearest
neighbour, confidently.** Before the ammo set was complete every `3` read as
`8` and every `9` as `0`, self-consistent enough to look like a real result.
A low threshold is not lenient, it is wrong.

Two text traps, both already paid for:

- **IoU must be windowed.** The game prints `Micro UZI 冲锋枪` where the template is only `Micro UZI`. Dividing by every white pixel on the plate charged the template for glyphs it never covered and scored the correct UZI at 0.575 — under threshold, so the gun read as unnamed. Windowed: 0.995.
- **But not by the template alone.** That gives any *subset* template full marks: on the SKS plate it lifts the wrong `k2` to 0.877 against the right answer's 0.959. Keeping the window's own pixels in the denominator holds that gap at 0.959 vs 0.728.

## Step 5 — re-measure whatever the template feeds

Detectors that count ink rather than match shape need new bounds when their
target changes. Measure across several frames (the string may animate in),
min/max, widen, and put the measured range in the config comment. Bounds too
tight fail *open*: the screen reads "not up" and the caller silently does
nothing. `collect_templates.py --targets type` measures this as a by-product —
add `type` to whatever run is happening anyway rather than doing a separate one.

Then list every text-keyed detector in its module docstring, so the next
language switch is a checklist and not a debugging session.

## Traps

- Extract with the cursor parked, from more than one background.
- Weapon plates get truncated by the game — template only the part that always renders.
- Icon scaling MUST be `cv2.INTER_NEAREST`; the game scales that way.
- Icons are BGRA, alpha is the mask. Attachment icons carry a **black outline** (dilate 1px → blur σ=1 → max → blend black), see `blend_attachment()`.
- Multi-state icons (highlighted / watermark, each fire mode) are separate templates; bright vs dim in one capture is what tells them apart.
- The posture icon renders **only in ADS**, so its with_ui/no_ui pair must be captured while aiming.
- `IMREAD_GRAYSCALE` does not guarantee one channel — anything importing ultralytics replaces `cv2.imread`. Guard loads with `if img.ndim == 3: img = img[:, :, 0]`.
- All coordinates are **3440x1440**.
- Visualisations go under `docs/<icon_type>/`; scratch output goes to `docs/debug/`. There is no `temp_debug/` any more — it was a never-delete scratch dir that grew for eight months until a third of it asked questions about a coordinate system that had been removed.
- PowerShell's `Get-Content`/`Set-Content` mojibake UTF-8; use the Edit tool or Python for files with Chinese.
