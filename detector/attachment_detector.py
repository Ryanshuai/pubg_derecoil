"""Attachment detector — template matching on Tab inventory slots.

Reads the ten weapon slots (five per gun) off a Tab frame.

THE THREE THINGS AROUND THE MATCH ARE THE MATCH. Scoring a crop against a
bank of templates always returns a winner; whether that winner means anything
is decided before and after:

  is anything drawn there   a slot the gun does not have, and an EMPTY slot,
                            both show blurred scenery, and scenery scores
                            under the empty threshold often enough to matter
  which templates to try    naming the weapon cuts the bank to what it can
                            physically hold, which is the difference between
                            Suppressor (SMG) and Suppressor (AR)
  how much it won by        a two-stage search, so the nine sub-pixel shifts
                            are paid for ten templates instead of fifty-five

Those lived in detector/tab_items.py while this file scored every one of the
55 templates blind, and the live loop -- the one that feeds recoil
compensation -- used this one. Over three reference captures the two disagreed
on 4 of 30 slots: a sniper cheek pad on a UZI, a VSS magazine on a gun that
had none (the very false positive tab_items' comments record as fixed), an SMG
suppressor read for an AR one, and a VSS cheek pad in a grip slot. Fixed in
one copy, live in the other.

So the loop is here now and tab_items calls it. `weapons` is optional and
everything still works without it, just less certainly.

ONE ASSET, SEVERAL PICTURES OF IT. A template file may carry a tag —
`Item_Attach_Weapon_Lower_Foregrip_C.solved.png` beside the untagged one —
and every variant of an asset is scored, the best standing for the asset. Same
convention as the weapon name plates, for a different reason: there the
variants are languages, here they are where the picture came from. The shipped
files are the game's own art, and the game does not draw its art unchanged —
it scales it, outlines it and blends it into a translucent panel. A picture
recovered FROM THE SCREEN (calibration/legacy_solve_template.py) is what the screen
actually shows, and it beats the art it was drawn from. The art stays because
it covers parts no capture run has reached yet.
"""
import os

import cv2
import numpy as np

from config import HUD_REGIONS
from detector.attachment_catalog import ATTACHMENTS, SLOT_NAMES, compatible  # noqa: F401
from detector.geometry import detail

# SLOT_NAMES is re-exported, not redefined -- control/kit_plan.py,
# control/inventory.py and calibration/legacy_collect_templates.py import it
# from here. It used to be a list here and a tuple everywhere else; the two
# error messages that render it ("... is not one of {SLOT_NAMES}") therefore
# now print round brackets.

TMPL_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'templates',
                        'pubg_assets', 'Item', 'Attachment')

# ⚠ A SECOND BANK, AND IT IS KEYED DIFFERENTLY ON PURPOSE. These are 库存 ROW
# icons, solved by intersection over four poses (calibration/
# collect_inventory_vlm.py), and they are named by CATALOGUE KEY -- `comp_ar
# .png` -- because the collector spawns parts by key and never learns an asset
# name. The Attachment bank is named by ASSET, which is what this detector
# indexes on, so the two are joined through ATTACHMENTS here and nowhere else.
#
# WHY A SEPARATE DIRECTORY rather than more `.tag` files next door: a row icon
# and a weapon-slot tile are two RENDERINGS of one part at two sizes, and this
# repository has twice paid for reading one with the other's geometry (0/10 at
# 63x63 against 10/10 at 80x80; and 0.861 -> 0.473 in the other direction).
# Keeping them in one directory makes that mistake a typo away.
INV_TMPL_DIR = os.path.join(os.path.dirname(__file__), '..', 'data',
                            'templates', 'pubg_assets', 'Item', 'Inventory')

# THE TWO RENDERINGS, NAMED ONCE. Every caller that knows which one it is
# reading passes one of these to `prefer`, and nothing spells a tag itself --
# the day a bank is renamed, the rename lands here and every caller follows.
#
# ⚠ THEY ARE NOT 'row' AND 'solved'. Those were the names before 2026-08-09,
# when the row bank was deleted and the slot bank became `.xsect_r*`, and BOTH
# live callers went on asking for them for a day. See _rank_variant.
INV_TAG = 'inv'             # intersected as a 库存 row, 80x80
SLOT_TAG = 'xsect_r1'       # intersected in a weapon's slot tile, 63x63


TMPL_SIZE = 48
ALPHA_TH = 150
OFFSET_Y = 8
OFFSET_X = 8
# "This tile has something in it that I can name." NOT the empty test --
# `drawn()` is, and it is the one with the evidence: of 1713 EMPTY tiles in the
# paired corpus it rejects 1689 on its own, without consulting a single
# template. This threshold only ever sees tiles drawn() has already called
# occupied, which is why it needs its own measurement rather than a guess.
#
# ⚠ 450 -> 1000 on 2026-08-09, and the old value sat INSIDE the positive class.
# Measured over every `__<slot>__<weapon>__<bg|fg>` crop in
# calibration/artifacts/attachments/runs -- 1733 occupied tiles and 1713 empty
# ones, scored through read_tile() itself:
#
#     occupied, named correctly     median  14.9   p99  521   MAX   718
#     empty AND drawn()=True (24)   min   1605   p05 2212   median 3358
#
#     TH=450    25 occupied tiles called EMPTY     0 empties kept
#     TH=800     0                                  0
#     TH=1500    0                                  0
#
# The gap 718..1605 is unoccupied, so anything in it separates perfectly on
# this corpus. 1000 is 1.4x above the worst true positive and 1.6x below the
# best false one -- margin on both sides, rather than a value tuned until one
# sample passed.
#
# ⚠ WHAT IT COST AT 450: an MP5K wearing a 4x read `scope: ''`. The bank has
# `Upper_ACOG_01_C` and it WAS the top match at 556.4 -- the detector named it
# correctly and this line threw the answer away, `ensure_sight` then refused
# the block, and the run lost its 4x cell. The operator could see the scope on
# the rail in the screenshot.
#
# ⚠ AND WHY IT BIT THE 4x FIRST: the tile is composited over the WEAPON
# RENDER, and every `scope_4x` paired capture in the corpus was taken on an
# `sks`. A template solved against one weapon's body scores worse against
# another's -- so a threshold tight enough to be weapon-specific is a
# threshold that fails whenever the gun changes. Rebuilding the template on a
# second weapon is still worth doing; it is not what makes this line correct.
MSE_EMPTY_TH = 1000

# WHERE A TEMPLATE OF A GIVEN SIZE SITS IN THE CROP IT IS READ AGAINST, and
# nothing is resized to make it fit. Resizing a template is not free and it is
# not neutral: it interpolates away exactly the antialiasing that separates
# two grey tubes, and it is silent about it.
#
# Two renderings exist and they are not one picture at two sizes:
#
#   48 in a 63 crop    the weapon tile. The icon fills the tile edge to edge;
#                      offset 8 is measured, not (63-48)//2 = 7.
#   80 in an 80 crop   the 库存 row. The icon is drawn inside padding, so the
#                      picture IS the whole cell and the offset is 0.
#
# Installing row solves under the slot geometry cost the whole row corpus:
# every margin fell to the 1.25 gate at once, 0.861 -> 0.473, spread evenly
# over parts whose templates had just been rebuilt from their own captures.
# Uniformly worse is what a geometry error looks like — a bad solve would
# have hurt some parts and spared others.
#
# An unlisted size is centred, which is a guess; the two that matter are here.
TMPL_OFFSETS = {48: (OFFSET_Y, OFFSET_X), 80: (0, 0)}

# ── the third answer ──
#
# A slot can be EMPTY, hold a part this bank can name, or hold something the
# bank cannot separate from its neighbour. Until 2026-08-04 the third case did
# not exist here: the margin was computed, returned, and never acted on, so a
# 1.02x call and a 30x call came back identically and a caller had no way to
# tell them apart without inspecting the score it was handed.
#
# WHY THAT IS NOT THE SAME AS RETURNING None. None means the slot is EMPTY,
# and control/inventory.py's ensure_kit reads an empty slot as "the drag did
# not land" and retries. Collapsing "I cannot name this" into it would make an
# ambiguous read look like a failed fit — a wrong action rather than a missing
# answer. detector/slot_detector.py learned the same lesson on the tile side
# and its docstring says it outright: do not let `unknown` collapse into
# `absent`.
#
# THE FLOOR, and what it costs, measured over 1387 ground-truth crops with the
# bank complete (calibration/legacy_score_attachments.py --margin-gate):
#
#     floor   correct kept    impostors refused
#      1.05   100.0%            6.6%
#      1.10    99.1%           13.3%
#      1.25    98.3%           27.6%
#      2.50    94.9%           83.7%
#      5.00    92.0%           92.1%   <- refusals stop rising here
#
# "Impostors" are from --no-template: drop a part from the bank, feed it its
# own crops, and count how often the rest of the bank invents a name for it
# instead of refusing. 31 of 41 parts do.
#
# 1.25 is chosen, and the reasoning is that the bank is now COMPLETE — every
# collectable attachment has a screen-solved template as of the 14-round
# collection — so a stranger arrives only from a game update or from icon
# drift, not from a known gap. Buying refusals at the price of correct reads
# is the wrong trade in that world. Above 5.0 nothing is bought at all.
#
# EVERY CORRECT READ THIS COSTS IS supp_ar. Not "mostly": all 24 of them, and
# all 13 that a 1.10 floor would cost. The AR suppressor sits at margin 1.07
# worst / 1.12 median against the SR suppressor across all 32 samples — three
# grey tubes, and the only structurally tight pair in the corpus (the rest of
# the near-misses are single-sample tails; see --confusion). So this floor is
# very nearly a single-part policy, and if it ever needs relaxing, that is the
# part to look at rather than the number.
MARGIN_MIN = 1.25

# What a slot reads as when the bank cannot separate its top two. A sentinel
# rather than a name, so `slot_matches` can never accidentally satisfy a
# wanted part with it, and never '' — that is EMPTY.
AMBIGUOUS = '?'

# Is a slot drawn at all? A slot the weapon does not have is not rendered, and
# an empty slot draws nothing either, so both show the blurred world behind the
# panel. MSE alone does not reject that: on calibration/artifacts/tab_live_aug_vss.png, whose
# second gun has no magazine, the magazine position matched
# Magazine_SR_ExtendedQuick_Mag_Vss below the 450 empty threshold and the gun
# read as wearing a magazine it did not have.
#
# Measured over three captures: drawn slots score 300..4756 (the floor is a
# suppressor, a plain grey tube), undrawn 1..14. Deliberately only a floor --
# anything above it still has to pass the MSE test.
SLOT_DETAIL_MIN = 100

# Rank un-shifted, then re-score this many properly. A shift can promote a
# template a long way: 4倍瞄准镜 does not survive a shortlist of 5, because
# un-shifted it ranks outside the top five and drops out. 8 is where both
# reference captures return the exhaustive answer; 10 is that plus margin.
SHORTLIST = 10

SLOT_PREFIXES = {
    'scope':    ('Upper_', 'SideRail_'),
    'muzzle':   ('Muzzle_',),
    'grip':     ('Lower_', 'Vector_VerGrip'),
    'magazine': ('Magazine_', 'Medium_'),
    'stock':    ('Stock_',),
}

_SHIFTS = tuple((sy, sx) for sy in (-1, 0, 1) for sx in (-1, 0, 1))


class AttachmentDetector:

    def __init__(self):
        self._templates = {}      # name → [(tmpl_vals, ys, xs), ...] variants
        self._tags = {}           # name → ['', 'xsect_r1', 'inv', ...] alongside
        self._slot_index = {}     # slot_name → [name, ...]
        self._load_templates()
        # Every tag any file on disk carries. See _rank_variant: this is what
        # separates "this asset has no such picture" from "no asset does".
        self._known_tags = {t for tags in self._tags.values() for t in tags}

    def _rank_variant(self, name, prefer):
        """Index of the variant the RANKING pass should use for this reading.

        The shortlist is ranked on one variant per asset, and which one is not
        a detail: the icon is drawn at slot size in a weapon's tile and at row
        size in the 库存 list, and a bank holding both should rank a slot crop
        against the slot picture. Ranking every crop on variant 0 cost 16 slot
        reads the day the untagged game-file icons were retired and variant 0
        silently became `.row` for half the bank.

        Falls back to 0 when THIS ASSET has no such picture, so an asset with
        one variant behaves exactly as before.

        ⚠ BUT A TAG NO FILE ANYWHERE CARRIES IS A TYPO, AND IT USED TO FALL
        BACK TOO -- which is the same silence twice over. On 2026-08-10 BOTH
        live callers named a bank that had been renamed or deleted the day
        before: `_read_row` asked for 'row' (38 pictures, deleted 08-09) and
        `read_tile` asked for 'solved' (renamed to '.xsect_r1' on 08-09).
        Neither raised. The row one cost the reference corpus 10/12 -> 1/12 and
        a night of `unknown` rows; the slot one cost nothing only because the
        sort order happened to put the right file at index 0.

        **A `prefer` that misses is indistinguishable from a caller that never
        passed one** -- so the two are separated here, where the bank's actual
        contents are known, rather than left to a reader to notice.
        """
        if not prefer:
            return 0
        if prefer not in self._known_tags:
            raise ValueError(
                f'prefer={prefer!r} names no picture in the bank; it holds '
                f'{sorted(self._known_tags)}. A tag that misses would silently '
                f'rank against variant 0 -- see _rank_variant.')
        tags = self._tags.get(name, ())
        return tags.index(prefer) if prefer in tags else 0

    def _add(self, name, img, tag):
        """One picture of one asset, at its OWN size.

        KEPT AT ITS OWN SIZE. This used to resize every template to 48x48 so
        one geometry could serve everything, which is the same mistake in the
        other direction: a row picture squeezed into the slot frame loses a
        quarter of its scale and lands 6-8 px off. A variant is read against
        the crop it was photographed from — see TMPL_OFFSETS — and one that
        does not fit is skipped rather than stretched.

        `tag` says WHICH RENDERING this is: '' for an untagged file, else the
        tag ('xsect_r1' = intersected in a weapon's slot tile on rack row 1,
        INV_TAG = intersected as an inventory row). best_two ranks on ONE of
        them and the icon is not the same size or sharpness in the two places,
        so the ranking pass has to be told which one it is reading.
        """
        if img is None or img.ndim != 3 or img.shape[2] != 4:
            return
        mask = img[:, :, 3] > ALPHA_TH
        if int(mask.sum()) < 30:
            return
        ys, xs = np.where(mask)
        if name not in self._templates:
            for slot_name, prefixes in SLOT_PREFIXES.items():
                if any(name.startswith(p) for p in prefixes):
                    self._slot_index.setdefault(slot_name, []).append(name)
        self._templates.setdefault(name, []).append(
            (img[:, :, :3].astype(np.float32)[ys, xs], ys, xs, img.shape[:2]))
        self._tags.setdefault(name, []).append(tag)

    def _load_templates(self):
        # THE SLOT BANK FIRST, and the order is load-bearing: best_two's
        # ranking pass falls back to variant 0, so whatever is appended first
        # stays the default for every caller that does not pass `prefer`.
        if os.path.isdir(TMPL_DIR):
            for fname in sorted(os.listdir(TMPL_DIR)):
                if not fname.endswith('.png'):
                    continue
                # <Asset>.png, or <Asset>.<tag>.png for a variant of the same
                # asset. Asset names carry no dot, so the first field is the
                # key.
                stem = fname[:-len('.png')].replace('Item_Attach_Weapon_', '')
                self._add(stem.split('.')[0],
                          cv2.imread(os.path.join(TMPL_DIR, fname),
                                     cv2.IMREAD_UNCHANGED),
                          stem.split('.', 1)[1] if '.' in stem else '')
        # THEN THE ROW BANK, joined key -> asset. A key with no asset entry
        # cannot be indexed here at all (this detector answers in asset names),
        # so it is skipped rather than filed under its own key -- a name no
        # caller could resolve is worse than a missing template.
        if os.path.isdir(INV_TMPL_DIR):
            for fname in sorted(os.listdir(INV_TMPL_DIR)):
                if not fname.endswith('.png'):
                    continue
                asset = ATTACHMENTS.get(fname[:-len('.png')], {}).get('asset')
                if not asset:
                    continue
                self._add(asset, cv2.imread(os.path.join(INV_TMPL_DIR, fname),
                                            cv2.IMREAD_UNCHANGED), INV_TAG)

    # ── scoring ──

    def _variant(self, crop_f, name, i, shifts):
        """One variant against one crop, at the variant's OWN scale.

        A variant taller or wider than the crop cannot be read against it and
        returns inf rather than being squeezed to fit — that is how a slot
        crop ignores the row pictures and a row crop ignores the slot ones,
        without either caller having to know which is which.
        """
        tmpl_vals, ys, xs, (th, tw) = self._templates[name][i]
        h, w = crop_f.shape[:2]
        if th > h or tw > w:
            return float('inf')
        oy, ox = TMPL_OFFSETS.get(th, ((h - th) // 2, (w - tw) // 2))
        cy, cx = ys + oy, xs + ox
        best = None
        for sy, sx in shifts:
            ny = np.clip(cy + sy, 0, h - 1)
            nx = np.clip(cx + sx, 0, w - 1)
            se = ((crop_f[ny, nx] - tmpl_vals) ** 2).sum(axis=1)
            best = se if best is None else np.minimum(best, se)
        return float(best.mean() / 3)

    def score(self, crop_f, name, shifts=_SHIFTS):
        """One asset against one float32 crop. -> mean squared error.

        The best of the asset's variants, because they are pictures of the
        same thing and the question asked of them is the same.

        `crop_f` is float32 already: converting once per crop rather than once
        per template is worth ~15% on its own.
        """
        return min(self._variant(crop_f, name, i, shifts)
                   for i in range(len(self._templates[name])))

    def best_two(self, crop, names, shortlist=SHORTLIST, prefer=None):
        """-> (name, mse, margin). Two-stage; see SHORTLIST.

        THE SHORTLIST PASS RANKS ON ONE VARIANT, THE FINE PASS SCORES THEM
        ALL. Ranking is only deciding who gets looked at properly, and it is
        the pass paid for by every template in the bank, so it runs once per
        asset — on variant 0, the untagged file, since _load_templates walks
        the directory sorted and `X.png` precedes `X.<tag>.png`. The shortlist
        is then re-scored across every variant with the nine shifts. A full
        detect costs 123 ms this way against 80 ms with no variants at all,
        where scoring every variant in both passes cost 191.

        Choosing the variant in the RANKING pass instead was tried and is
        wrong: it drops two of the twelve reference rows. Un-shifted, the
        recovered icon and the shipped art trade places on a list row, and the
        shift the fine pass adds is exactly what separates them. Same lesson
        as SHORTLIST — a cheap pass may order candidates, it may not pick
        between them.
        """
        crop_f = crop.astype(np.float32)
        coarse = sorted((self._variant(crop_f, n,
                                       self._rank_variant(n, prefer),
                                       ((0, 0),)), n)
                        for n in names)
        fine = sorted((self.score(crop_f, n), n)
                      for _, n in coarse[:shortlist])
        m1, n1 = fine[0]
        m2 = fine[1][0] if len(fine) > 1 else float('inf')
        return n1, m1, (m2 / m1 if m1 > 0 else float('inf'))

    # ── gates ──

    @staticmethod
    def drawn(crop):
        """Is there UI in this cell, or the blurred world showing through?

        Asked BEFORE matching, not after: when nothing is drawn the question
        is not which template is closest, it is that there is no box on screen
        to hold one.
        """
        return detail(crop) >= SLOT_DETAIL_MIN

    def candidates(self, slot, weapon=None):
        """Templates worth testing in this slot of this weapon.

        Naming the weapon narrows the bank to what it can physically hold.
        Without a name every template for the slot is tried, which is what
        reads an SMG suppressor onto an SKS -- two near-identical icons that a
        blind match separates on ~1.3x margin.

        A weapon key the catalogue does not know narrows the bank to NOTHING
        and would read every slot empty, so an unknown name is treated as no
        name at all. Callers passing OCR output do not have to filter first.
        """
        names = self._slot_index.get(slot, [])
        if not weapon:
            return names
        allowed = {ATTACHMENTS[k]['asset']
                   for k in compatible(weapon).get(slot, [])
                   if ATTACHMENTS[k].get('asset')}
        if not allowed:
            return names
        return [n for n in names if n in allowed]

    # ── entry points ──

    def read_tile(self, crop, slot, weapon=None):
        """One already-cut SLOT TILE -> (name, mse, margin).

        ('', inf, 0.0) when there is nothing to read: no crop, no candidate
        template for this weapon's slot, or nothing drawn in the tile.

        **This exists to carry `prefer=SLOT_TAG` so no caller has to remember
        it**, and the reason is measured, not stylistic. `prefer` picks the
        variant the RANKING pass scores, and 38 of the bank's 41 assets have
        `.row` at variant 0 — the 库存 row-size picture, which is not what a
        weapon tile draws. Ranking a tile against it is the exact regression
        `_rank_variant`'s docstring records as costing 16 slot reads.

        Four functions were doing candidates -> drawn -> best_two on a slot
        tile, and two of them passed `prefer` while two did not (2026-08-06).
        Nothing about that is visible in a result: all four return a plausible
        asset name either way. The two that agree are routed through here; the
        two that do not are a behaviour change and are NOT folded into this
        commit -- see the report.

        No thresholds are applied. MSE_EMPTY_TH and MARGIN_MIN are the
        CALLER's verdict: read_slots turns a weak match into None and an
        unseparated one into AMBIGUOUS, while slot_detector wants the raw pair.
        Baking either in here would have made this mergeable with only one of
        them.
        """
        if crop is None or crop.size == 0:
            return ('', float('inf'), 0.0)
        names = self.candidates(slot, weapon)
        if not names or not self.drawn(crop):
            return ('', float('inf'), 0.0)
        name, mse, margin = self.best_two(crop, names, prefer=SLOT_TAG)
        return (name, float(mse), float(margin))

    def classify_crop(self, crop, slot, weapon=None):
        """One slot crop -> asset name, or '' for nothing recognised.

        '' covers both "empty" and "not this weapon's slot"; use read_slots if
        the distinction matters.
        """
        # BEHAVIOUR CHANGED 2026-08-06: this used to call best_two WITHOUT
        # a slot-rendering preference, so it ranked slot tiles against whichever variant
        # happened to be index 0 — `.row`, the 库存 list picture, for 38 of the
        # bank's 41 assets. Both callers cut HUD slot tiles (capture_ads reads
        # `scope`, tools/regression_check reads `muzzle`), so the preference
        # was simply missing, not deliberately absent.
        name, mse, _ = self.read_tile(crop, slot, weapon)
        return '' if mse > MSE_EMPTY_TH else name

    def read_slots(self, frame, weapons=None):
        """-> {1: {slot: (name, mse, margin) | None}, 2: {...}}

        None means nothing was read there. The scores come out with it because
        a caller confirming a drag wants to know how close the call was.
        """
        weapons = weapons or {}
        out = {}
        for gun in (1, 2):
            weapon = weapons.get(gun)
            slots = {}
            for slot in SLOT_NAMES:
                y, x, h, w = HUD_REGIONS[f'att_{gun}_{slot}']
                name, mse, margin = self.read_tile(
                    frame[y:y + h, x:x + w], slot, weapon)
                if mse > MSE_EMPTY_TH:
                    slots[slot] = None
                elif margin < MARGIN_MIN:
                    slots[slot] = (AMBIGUOUS, mse, margin)
                else:
                    slots[slot] = (name, mse, margin)
            out[gun] = slots
        return out

    def any_drawn(self, frame):
        """Is the weapon panel PAINTED at all? -> bool.

        ⚠ "NOTHING IS FITTED" AND "THE PANEL IS NOT ON SCREEN YET" COME OUT OF
        classify() AS THE SAME ANSWER, and that is what this exists to
        separate. read_tile returns ('', inf, 0.0) when `drawn()` is False,
        read_slots turns inf into None, classify turns None into '' -- the
        same '' a genuinely empty slot produces. Downstream,
        GameState.set_attachments writes those '' onto the weapon and the
        recoil lookup keys on `bare`.

        Its caller is control/tab_watch.py, which reads the panel exactly once,
        as it closes. This is the check that says whether there was still a
        panel there to read: an alt-tab or a dialog closes it with no keypress
        to watch, and by the time that is noticed the tiles are gone.

        A tile that is merely EMPTY is still DRAWN (detector/CLAUDE.md: the
        border ring reads Sobel p90 46-173 empty against 5-26 for no tile at
        all), so this does not refuse a bare gun. It refuses a panel with no
        tiles anywhere.

        ⚠ IT ALSO REFUSES TWO GUNS THAT DRAW NO TILES AT ALL -- a pair of P90s
        would do it, since that gun has no slots and paints nothing. The
        honest outcome there is "we cannot tell", which is what the caller
        gets, rather than "it is wearing nothing", which is a claim.
        """
        for gun in (1, 2):
            for slot in SLOT_NAMES:
                y, x, h, w = HUD_REGIONS[f'att_{gun}_{slot}']
                crop = frame[y:y + h, x:x + w]
                if crop.size and self.drawn(crop):
                    return True
        return False

    def classify(self, frame, weapons=None):
        """-> {1: {slot: name}, 2: {slot: name}}, '' where nothing was read.

        Takes a FRAME, like every other detector's classify(). It used to take
        a dict of pre-cut crops, which is why control/gun.py:read_loadout --
        which passes a frame, as its neighbouring gun_det.classify(frame) does
        -- raised AttributeError on every call it ever made.
        """
        read = self.read_slots(frame, weapons)
        return {gun: {slot: (hit[0] if hit else '')
                      for slot, hit in slots.items()}
                for gun, slots in read.items()}
