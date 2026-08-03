---
name: calibrate-screen
description: Calibrate a game UI screen's interactive layout — where its rows, slots, list entries and weapon positions are, and how to tell the screen is even up. Use when a new screen or tab needs to be driven by clicks/drags (spawner panel, Tab inventory, a settings tab), or when an existing layout stopped matching after a game update. Produces a detector/<screen>_layout.py, an anchor check, and a docs/<screen>/README.md of the measurements. Not for what a single icon, string or digit looks like and where it is composited — that is calibrate-template.
argument-hint: "<screen name> - the skill opens the screen itself; or point at saved screenshots"
---

# Screen Layout Calibration

Work out where everything clickable on a game screen is, well enough that another module can drive it blind.

Reference implementations, read whichever is closest before starting:
`detector/spawner_layout.py` (columns of variable-length lists),
`detector/tab_layout.py` (fixed slot grid), `docs/spawner/README.md` (what a
finished write-up looks like).

## What this produces

1. `detector/<screen>_layout.py` — geometry only: takes a full-screen BGR frame, returns coordinates. No clicking, no game state.
2. An **anchor check** — `<Screen>Detector.classify(frame)` answering "is this screen even up?", so a caller fails loudly instead of clicking into whatever *is* on screen.
3. Constants in `config.py` under a named section, with the measurement behind each number in the comment.
4. `docs/<screen>/README.md` — the numbers, the separations they achieved, and the traps found.

## Rule: detect the structure, don't hardcode it

Hardcoded row coordinates rot on the next patch, silently. Find rows by projection and keep what is *regularly spaced* — that rejects sliders, buttons and titles without knowing where they sit. If a list can grow or shrink, its length must come from the screen, never from a table.

Where a table (a catalogue, an index) does drive clicks, **verify the count against the screen before clicking** and fail on a mismatch. See `SpawnerControl.spawn(expect=...)`.

## Step 0 — get the game to the screen

Do not ask the user to open it. Every screen here is reachable from a cold
lobby, and a run that needs a human to click first can never be unattended.

```bash
pixi run python tools/drive_screen.py list
pixi run python tools/drive_screen.py tab --shoot baseline    # docs/tab/baseline.png
pixi run python tools/drive_screen.py spawner --keep-open     # to work on it live
```

`drive_screen` is the chain, and each link fails in its own way:

| link | what it does | why it is not optional |
|---|---|---|
| `ensure_focus()` | takes the foreground | a keypress sent to the terminal is silently lost; the symptom is "the panel did not open" |
| `LobbyControl.ensure_in_match()` | lobby → training range | TAB in the lobby does nothing, and the ESC menu eats every key while every pixel probe still says "in a match" |
| close-if-open | forces a known state | a toggle key on an unknown state lands on the opposite of what you wanted |
| press → wait → **verify** | opens it | "press and assume" is how a whole run silently measures the game world |
| park the cursor | see Step 1 | |
| press → **verify closed** | puts it back | a panel left open breaks whatever runs next |

**Registering a new screen** means adding one `Screen(...)` to `SCREENS` in
`tools/drive_screen.py`: the key that toggles it, an anchor function (Step 2),
a park point, and the render wait. If it has no anchor yet, do Step 2 first —
without one, `drive_screen` cannot tell "opened" from "pressed a key at the
game world".

**Entering the training range is gated, deliberately.** `press_play()` refuses
to click until the mode tab reads `TRAINING`, because that one button starts
whatever the sub bar has selected, and a real match **cannot currently be
left** (`leave_entry_confirmed()` only knows the training-range ESC menu). See
the Lobby section of `docs/lobby/README.md`.

**The hardware and the window are shared.** One Pico, one game window, and
other agents run long captures on both. `Pointer` reports which pid holds the
port; wait for it rather than killing it.

## Step 1 — capture a clean baseline

```bash
python tools/scrape_spawner.py --layout-only    # closest existing example
```

Two things ruin a baseline:

- **Cursor hover.** Whatever the cursor rests on draws a highlight, which bleeds bright pixels past the element's real bounds. It shifted a whole column's left edge by 49px on the spawner. Park the cursor off the panel (`press.pointer.move_cursor`) before every screenshot, including the baseline.

  On a bar of tabs it is worse than noise: hover lights a tab to **exactly** the brightness that marks the selected one, so an unparked shot makes wherever the mouse happens to rest look selected. The moment that bites is the read-back verifying a click landed — the cursor is by definition sitting on the tab just clicked, so a click that did nothing reads as success. Every grab parks first; see `LobbyControl._grab_parked`.
- **Live scene behind a translucent panel.** Clouds and foliage flicker across any brightness threshold. Confine every projection and diff to the panel's own boxes; on the spawner, whole-window diffs carried 355–1161 px of pure noise, per-column-box diffs carried 0–4.

## Step 2 — find an anchor for "is this screen up?"

Pick UI furniture that exists on **no other screen** (buttons, a header). Then check it is usable:

- **Achromatic?** `|max-min|` over B,G,R on bright pixels. ≤2 means grey, so a grey-level template is safe.
- **Opaque?** Same pixels across ≥3 screenshots with *different scenes behind them*. Bright parts of the spawner buttons moved ≤6 grey levels; their dark parts moved up to 86 — those are alpha-blended and must stay out of the template.
- Threshold at whatever separates the opaque part, mask it, `matchTemplate` in a small window around a fixed anchor.

`tools/probe_button_icons.py` does exactly these measurements; copy it.

Report the separation, not just "it works": the spawner anchor scores 0.989–1.000 on 24 positives and 0.000 on gameplay negatives, hence a 0.55 threshold.

**Measure the negatives on real frames, not on screens you picked.** The Tab
anchor counted bright pixels in the `类型` header and looked flawless — lobby,
results, ESC menu and gameplay all measured exactly 0. Swept over 96 real ADS
captures it had a false positive, and nine frames measured exactly 738 = the
entire 41×18 crop saturated: that header sits over the training range's bright
sky, which ADS magnifies into it. `docs/ads/runs/**/*.jpg` is 893 frames of
free negatives; use them.

**A pixel count is not an anchor.** It cannot separate "the glyph is drawn"
from "everything here is white". Glyph IoU can, and bounds that failure by
construction — a saturated crop matches every template pixel but fills the
union too, capping it at `|template|/|crop|` ≈ 0.28. Measured: open
0.922–1.000, closed 0.000–0.352.

**Do not reach for `TM_CCORR_NORMED`.** Tried on this exact anchor, it
inverted the problem: negatives 0.985–0.999 against positives 0.887–1.000. A
normalised correlation over a dark or flat window says nothing about whether
the strokes are there.

**UI text anchors need one template per language.** The same header renders
`类型` in the old captures and `Type` in the current client. Each scores the
other at 0.27 — below the brightest negative — so a single-language anchor
reads "closed" forever after a language switch, silently. Score the best of
several masks: `training_data/pubg_assets/tab/type_header_{zh,en}.png`,
rebuilt by `tools/probe_tab_anchor.py --write`.

## Step 2.5 — one element usually needs two windows

"What is in it" and "is it there at all" are different measurements, and a
crop tuned for one answers the other wrongly. Expect to define both.

| window | crop | question | method |
|---|---|---|---|
| **inner** | hugs the content, no border | what is this? | template match |
| **outer** | content + border + surrounding background | does it exist? | gradient on the border ring |

The Tab screen's attachment slots are the worked example — geometry in
`detector/tab_layout.py` (`slot_tile_box`, `slot_window`), judgements in
`detector/slot_detector.py`. Keep that split: a `*_layout` module returns
coordinates, a `*_detector` module turns pixels into state.


`HUD_REGIONS['att_*']` is 63×63 and stops deliberately short of the tile edge,
because it was cut for the template matcher and border pixels belong to no
icon — feeding them into an MSE adds a term that moves with whatever is behind
the panel.

That same crop cannot answer presence. The tile is 66×66, so the interior sits
wholly *inside* it and sees only flat fill: **an empty slot and a slot that was
never drawn measure identically** (std 0.9–3.0 vs 2.1–3.3, edges 0 vs 0). That
reads as a clean negative — "these are indistinguishable, the approach is
dead" — and it is an artefact of the window. Pad out to include the background
and they separate 6× (contrast −0.2…1.7 absent vs 10.7…42.5 present).

So:

- **Measure the border ring, and only the ring.** Not the interior — that
  holds arbitrary content which says nothing about whether the element exists,
  and including it makes the reading depend on what happens to be there. Not a
  wide surround either. A band a few px either side of the border: Sobel
  magnitude, 90th percentile. `SlotDetector.ring_grad` scored **absent
  5.0–26.0 vs present 46.0–172.7** over 24 slots, zero errors, and is nearly
  blind to contents — a stripped M416 reads 260/260/278/260 where a fully
  fitted one reads 260/260/318/260.
- **Gradient, not Canny.** Canny's fixed hysteresis returned exactly 0 on a
  real slot whose tile sat on bright sand at nearly its own brightness. The
  border was there, just low-contrast, and hysteresis quantised it away.
  Sobel keeps it at 46. A judgement that reads a hard 0 on a present element
  is not conservative, it is broken.
- **Never widen the inner window to serve both.** It degrades the matcher for
  a question it was never meant to answer. Define a second window and leave
  the first alone.
- **Do not put the outer window in `HUD_REGIONS`.** Per `detector/CLAUDE.md`
  the per-frame capture box must not grow for an event-driven check. Derive it.
- **Some elements have no border to measure.** The scope slot draws no tile at
  all: empty-and-present is pixel-identical to absent, and the weapon's own
  art shows through, so a VSS reads 678 interior edges with nothing fitted.
  That is a property of the UI, not a threshold to tune — return `unknown` and
  answer it another way. Never let `unknown` collapse into `absent`.

When a measurement says two states are indistinguishable, **check the window
before believing it**. Ask what the crop can physically see: if it lies
entirely inside the thing being tested, it cannot see whether the thing is
there. Both dead ends here — "edges read 0 for both" and "Canny reads 0 on a
real slot" — looked like clean negative results.

**One threshold per judgement, one name.** Replacing the contrast test with
the ring test left both `TAB_SLOT_PRESENT_MIN` definitions in `config.py`; the
later one silently shadowed the new value, 36.0 became 6.0, and an absent
stock at 8.3 read as present. Regression caught it — grep the name after any
constant swap.

## Step 3 — find the structure geometrically

Text projection is the workhorse. `detector/spawner_layout.segments()` is reusable: runs of a profile above a threshold, with small-gap merging and min/max length filters.

- **Columns**: sum the bright mask down the y axis, segment with a large `gap` so glyphs in a column merge but columns stay apart.
- **Rows**: sum across x inside each column, segment.
- **Which columns are real lists**: the ones whose row pitch is *constant*. On the spawner this alone rejected the sensitivity sliders (pitch alternating 36/49) and the bottom-right buttons, and dropped the panel title, with nothing hardcoded.
- **Click point**: derive from the column's median left edge, not per-row — one hover-lit row must not move the whole column.

## Step 4 — expanded / nested state

If a row expands into a submenu, three things bite:

- **Entries look different from headers.** Submenu entries are *centred* in their tile; category headers hang off the left behind a chevron. That difference separates them without counting anything.
- **Borders bridge rows.** Tiles have borders, and on a long list the border lines connect adjacent rows into one unbroken projection band. Measure the border grey against the text grey — on the spawner 200–202 vs 238, so raising the threshold to 215 split them.
- **Short labels break vertically.** `K2` has no continuous horizontal stroke, so its projection came out as four fragments of height 4/4/1/2, all below the minimum row height, and the entry vanished. Merge gaps of a few px (`gap=6`).

## Step 4.5 — drop targets only exist while a drag is in flight

If the screen accepts drag-and-drop, **you cannot find the drop zones in a static screenshot.** They are not drawn until something is being carried. A baseline shot shows list rows and panel chrome; it does not show where a release actually counts, and the two are not the same rectangle.

The measurement is manual and takes ten seconds:

1. Pick the item up in the game and drag it toward the target panel.
2. **Stop. Do not release.**
3. Screenshot while the button is still down — `tools/snap_on_key.py` polls a hotkey with `GetAsyncKeyState`, so it shoots without stealing focus, and it stamps the **cursor position** into the filename and a JSON sidecar.
4. Release wherever you like; the shot is already taken.

The game draws a dashed border around every zone that would accept the drop. Two shots — one hovering each panel — give you both rectangles and both a known-good release coordinate, measured rather than inferred.

**Why this earns its own step:** the release point is the one coordinate on a drag-and-drop screen that no amount of structure detection can find. Rows, columns and pitch all come out of the text projection in Step 3; the drop zone does not, because it is invisible until asked for.

**What it costs to skip.** PUBG's inventory drag released at the icon-column centre of a computed row — a point comfortably inside the panel's detected bounds — and the part landed on the floor instead of in the backpack. It stayed wrong for months because the verification only re-read the SOURCE slot: the slot emptied, so it passed. Reuse of a template-matching centre as a release point is the specific trap; those are two different questions about the same panel.

> **Verify the destination, not just the source.** A drag that empties the source and loses the item looks exactly like one that worked. Count rows in the destination panel before and after.

## Step 5 — state change detection

To tell "expanded" from "collapsed", or "did my click land": diff the **text mask**, inside the affected box only. Do not use a row-coordinate signature — three submenu entries at pitch 45 against a list pitch of 43 produced a signature 3px from the collapsed one and read as "nothing happened" while the menu was plainly open.

Quote the measured separation in the config comment. Spawner: real change 489–21096 px, noise 0–4 px, threshold 200.

## Step 6 — verify against something independent

Detected counts must agree with a catalogue, or with a second capture:

```bash
python -c "from control.spawner import check_against_run; \
           check_against_run('docs/spawner/runs/<stamp>')"
```

This is how the crossbow quiver was found: 握把 expanded to 7 entries where `attachment_catalog.ATTACHMENTS` accounted for 6, which had silently shifted every later index down one. A count check catches exactly the class of bug that otherwise shows up as "it spawned the wrong thing" hours later.

When a screen's list and a code table disagree, **fix the mapping in the driver, not the catalogue** if the catalogue is about semantics (what fits what) rather than menu order — see `SPAWNER_EXTRAS`.

## Step 7 — write it down

`docs/<screen>/README.md`: every threshold with the measurement that justifies it, the traps hit, and the commands to re-run offline. Keep the captures under `docs/`, not `temp_debug/` — later work builds on them.

Any game behaviour discovered along the way (a click having a side effect, a key being swallowed by the UI) goes in `docs/game_quirks.md`, not here.

## Traps that cost time on the spawner

- Screenshot with the cursor parked, always.
- A projection strip that runs too wide picks up whatever dialog is next to the bar as an extra "row". The lobby's sub bar found a sixth tab with more ink than CUSTOM — it was the daily popup, 1961px away. Bound the strip and check the label *count* against what you expect.
- Console here is cp1252: `print()` of any Chinese label raises `UnicodeEncodeError` in the logging, not the logic. `sys.stdout.reconfigure(encoding='utf-8', errors='replace')`.
- Diff inside the UI's own boxes, never the whole window.
- Verify a click by reading the screen back; a click that silently did nothing looks identical to success.
- Long submenus may be cut off by a scrollbar. Check whether the last entry is really the last one before trusting a count.
- The game truncates its own long labels (`后坐补偿器 (突击步枪、精确射手...`). That is not a detection bug.
- PowerShell's `Get-Content`/`Set-Content` read UTF-8 source as system ANSI and will mojibake every Chinese comment in the file. Use the Edit tool or Python for any file with non-ASCII.
- All coordinates are **3440x1440**. Never 3840x2160.
- Do NOT delete anything in `temp_debug/`.
