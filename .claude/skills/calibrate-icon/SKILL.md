---
name: calibrate-icon
description: Calibrate a game HUD icon's position, scale, blend mode and alignment from screenshots. Agent identifies the icon, determines where it appears on screen, runs template matching, analyzes how it's composited, and verifies alignment. Also supports extracting new icon templates from with_ui/no_ui screenshot pairs.
argument-hint: "<screenshot_path_or_folder> - describe what icon to calibrate/extract"
---

# Icon Calibration Skill

Calibrate the position, scale, blend mode, and alignment of a game UI icon from screenshots.

## Step 0: Check existing config

Before doing anything, check `config.py` for existing region definitions (FIRE_MODE, POSTURE, WEAPON_HUD_1, etc.) and `icon_merging.py` for known blend formulas. Don't rediscover what's already known.

Also check `ASSET_DIR` in config.py to see if icon templates already exist for this icon type.

## Two workflows

Depending on whether icon templates already exist:

- **Workflow A**: Icon assets exist (weapons, attachments) → template matching to find position/scale/blend
- **Workflow B**: No icon assets, need to extract from screenshots (fire mode, posture, custom icons) → use with_ui/no_ui pairs to extract alpha masks

---

## Workflow A: Template matching (icon assets exist)

### A1: Overview scan — find regions of interest

Read the with-UI screenshot to see the full game screen. Identify all visible HUD regions.

If a no-UI screenshot with the same prefix exists, diff them:

```bash
python "${CLAUDE_SKILL_DIR}/scripts/diff_overview.py" <with_ui_path> <no_ui_path> --save-dir temp_debug
```

### A2: Crop and identify — what icon is this?

Crop the region of interest and **look at it** (use Read tool). Identify the icon and find the matching asset.

Icon library locations:
- Weapons: `training_data/pubg_assets/Item/Weapon/Main/*_C_w.png`
- Attachments: `training_data/pubg_assets/Item/Attachment/*.png`
- Posture: `training_data/pubg_assets/posture/*.png`
- Fire mode: `training_data/pubg_assets/fire_mode/*.png`

### A3: Precise template matching

```bash
python "${CLAUDE_SKILL_DIR}/scripts/search_icon.py" <icon_path> <screenshot_path> <x1> <y1> <x2> <y2> --save-dir temp_debug
```

Use a tight search box (add ~50px margin around expected area). Parse JSON output.

- Score >0.99 = excellent, >0.98 = good, <0.95 = suspicious (wrong icon?)

### A4: Analyze blend mode

```bash
python "${CLAUDE_SKILL_DIR}/scripts/analyze_blend.py" <icon_path> <with_ui_path> <no_ui_path> <x> <y> <scale_pct>
```

Interpreting results:
- `reconstruction_error < 3`: excellent
- `reconstruction_error 3~10`: acceptable
- `reconstruction_error > 10`: wrong blend mode — try status_bar mode or check for shadow/blur

### A5: Verify alignment

```bash
python "${CLAUDE_SKILL_DIR}/scripts/search_icon.py" <icon_path> <screenshot_path> <x1> <y1> <x2> <y2> --verify <no_ui_path> --alpha <alpha> --save-dir temp_debug
```

mean_diff < 3 = good alignment.

---

## Workflow B: Extract templates from screenshots (no existing assets)

Use this when there are no icon assets to match against — you need to CREATE templates by comparing with_ui vs no_ui screenshots.

### B1: Identify the blend mode

The game uses different compositing for different HUD areas:

| HUD area | Blend mode | Formula |
|----------|-----------|---------|
| Weapon HUD (right) | alpha | `result = alpha * color + (1-alpha) * bg` |
| Tab attachments | alpha + darken | `result = alpha * icon + (1-alpha) * (0.37*blur(bg)+44)` |
| Status bar (fire mode, posture) | status_bar | `result = alpha * 255 + (1-alpha) * gradient * blur(bg, k)` |

Check `icon_merging.py` for the exact formula and verified parameters (blur_k, gradient).

### B2: Extract templates

For **simple alpha blend** (weapon icons, posture):
```bash
python "${CLAUDE_SKILL_DIR}/scripts/extract_template.py" --mode alpha \
    --with-ui <with_ui.png> --no-ui <no_ui.png> \
    --region <x1>,<y1>,<x2>,<y2> \
    --output <template.png> --save-dir temp_debug
```

For **status bar blend** (fire mode — blur+darken+white overlay):
```bash
python "${CLAUDE_SKILL_DIR}/scripts/extract_template.py" --mode status_bar \
    --with-ui <with_ui.png> --no-ui <no_ui.png> \
    --region <x1>,<y1>,<x2>,<y2> \
    --blur-k 17 --gradient 0.65 \
    --output <template.png> --save-dir temp_debug
```

For **batch extraction** (multiple screenshots → multiple templates):
```bash
python "${CLAUDE_SKILL_DIR}/scripts/extract_template.py" --mode status_bar \
    --with-ui a_with_ui.png b_with_ui.png c_with_ui.png \
    --no-ui a_no_ui.png b_no_ui.png c_no_ui.png \
    --region <x1>,<y1>,<x2>,<y2> \
    --blur-k 17 --gradient 0.65 \
    --output-dir templates/ --save-dir temp_debug
```

Check `recon_error` in the output:
- `< 1.5`: excellent, blend formula is correct
- `1.5 ~ 3`: acceptable
- `> 3`: blend params may be off — try different blur_k/gradient values

### B3: Verify extracted templates visually

Always **view the extracted alpha** (use Read tool on the `_alpha.png` visualization in save-dir). Check:
- Icon shape is clean, not noisy
- Bright pixels = active icon, dim pixels = watermark states
- No background leakage

### B4: Calibrate blend parameters (if needed)

If recon_error is too high, grid-search for better parameters. Use Python to sweep blur_k and gradient, compute recon_error for each, pick the best. See `icon_merging.py` for the blend formula.

---

## Step 6: Output calibration parameters

Summarize in a format ready for `config.py`:

```python
ICON_NAME = {
    'x1': <left>, 'x2': <right>,
    'y1': <top>,  'y2': <bottom>,
}

# If template matching was used:
ICON_NAME_BLEND = {
    'formula': 'alpha * color + (1-alpha) * background',
    'alpha_highlighted': <value>,
    'alpha_non_highlighted': <value>,
    'color': '<white/red/bgr tuple>',
}

# If status_bar mode:
ICON_NAME_BLEND = {
    'formula': 'alpha * 255 + (1-alpha) * gradient * blur(bg, k)',
    'blur_k': <value>,
    'gradient': <value>,
}
```

Save extracted BGRA templates to `training_data/pubg_assets/<icon_type>/`.

## Notes

- Icon images are BGRA. The alpha channel is the icon mask/opacity.
- Check config.py FIRST for existing regions — don't rediscover known positions.
- Check icon_merging.py for known blend formulas and verified parameters.
- **Icon scaling MUST use `cv2.INTER_NEAREST`**, not bilinear. The game uses nearest-neighbor, bilinear looks too smooth and won't match.
- All coordinates are for **3440x1440** resolution. Do NOT use 3840x2160 or any other resolution — this has caused bugs before.
- Multiple icons may share alignment rules. Point out patterns.
- Some icons have multiple states (highlighted/watermark, different fire modes). Extract each separately.
- When extracting from multiple screenshots showing different states, look at which parts are bright vs dim to identify active vs watermark regions.
- Attachment icons have a **black outline** around them (dilate alpha → blur → blend black). See `blend_attachment()` in icon_merging.py.
- Posture icons have **NO blur/darken** background — they use simple alpha blend directly on the game scene.
- Fire mode icons sit on a **blur+darken status bar** — must use `blend_status_bar()` formula.
- Always save intermediate visualizations to temp_debug for the user to verify.
- Do NOT delete existing files in temp_debug.
