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
recovered FROM THE SCREEN (tools/solve_template.py) is what the screen
actually shows, and it beats the art it was drawn from. The art stays because
it covers parts no capture run has reached yet.
"""
import os

import cv2
import numpy as np

from config import HUD_REGIONS
from detector.attachment_catalog import ATTACHMENTS, compatible

SLOT_NAMES = ['scope', 'muzzle', 'grip', 'magazine', 'stock']

TMPL_DIR = os.path.join(os.path.dirname(__file__), '..', 'training_data',
                        'pubg_assets', 'Item', 'Attachment')

TMPL_SIZE = 48
ALPHA_TH = 150
OFFSET_Y = 8
OFFSET_X = 8
MSE_EMPTY_TH = 450

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
# bank complete (tools/score_attachments.py --margin-gate):
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
# panel. MSE alone does not reject that: on docs/tab_live_aug_vss.png, whose
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
        self._tags = {}           # name → ['', 'solved', 'row', ...] alongside
        self._slot_index = {}     # slot_name → [name, ...]
        self._load_templates()

    def _rank_variant(self, name, prefer):
        """Index of the variant the RANKING pass should use for this reading.

        The shortlist is ranked on one variant per asset, and which one is not
        a detail: the icon is drawn at slot size in a weapon's tile and at row
        size in the 库存 list, and a bank holding both should rank a slot crop
        against the slot picture. Ranking every crop on variant 0 cost 16 slot
        reads the day the untagged game-file icons were retired and variant 0
        silently became `.row` for half the bank.

        Falls back to 0, so an asset with only one picture behaves exactly as
        before and a caller that does not know its context loses nothing.
        """
        if prefer:
            tags = self._tags.get(name, ())
            if prefer in tags:
                return tags.index(prefer)
        return 0

    def _load_templates(self):
        if not os.path.isdir(TMPL_DIR):
            return
        for fname in sorted(os.listdir(TMPL_DIR)):
            if not fname.endswith('.png'):
                continue
            img = cv2.imread(os.path.join(TMPL_DIR, fname), cv2.IMREAD_UNCHANGED)
            if img is None or img.shape[2] != 4:
                continue
            # <Asset>.png, or <Asset>.<tag>.png for a variant of the same
            # asset. Asset names carry no dot, so the first field is the key.
            stem = fname[:-len('.png')].replace('Item_Attach_Weapon_', '')
            name = stem.split('.')[0]
            resized = (img if img.shape[:2] == (TMPL_SIZE, TMPL_SIZE) else
                       cv2.resize(img, (TMPL_SIZE, TMPL_SIZE),
                                  interpolation=cv2.INTER_AREA))
            mask = resized[:, :, 3] > ALPHA_TH
            if int(mask.sum()) < 30:
                continue
            tmpl_bgr = resized[:, :, :3].astype(np.float32)
            ys, xs = np.where(mask)
            if name not in self._templates:
                for slot_name, prefixes in SLOT_PREFIXES.items():
                    if any(name.startswith(p) for p in prefixes):
                        self._slot_index.setdefault(slot_name, []).append(name)
            self._templates.setdefault(name, []).append((tmpl_bgr[ys, xs],
                                                         ys, xs))
            # WHICH RENDERING this variant is: '' for the untagged file, else
            # the tag ('solved' = photographed in a weapon's slot tile, 'row' =
            # photographed as an inventory row). best_two ranks on ONE of them
            # and the icon is not the same size or sharpness in the two places,
            # so the ranking pass has to be told which one it is reading.
            self._tags.setdefault(name, []).append(
                stem.split('.', 1)[1] if '.' in stem else '')

    # ── scoring ──

    def _variant(self, crop_f, name, i, shifts):
        tmpl_vals, ys, xs = self._templates[name][i]
        h, w = crop_f.shape[:2]
        cy, cx = ys + OFFSET_Y, xs + OFFSET_X
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
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        return float(cv2.Laplacian(gray, cv2.CV_32F).var()) >= SLOT_DETAIL_MIN

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

    def classify_crop(self, crop, slot, weapon=None):
        """One slot crop -> asset name, or '' for nothing recognised.

        '' covers both "empty" and "not this weapon's slot"; use read_slots if
        the distinction matters.
        """
        if crop is None or crop.size == 0:
            return ''
        names = self.candidates(slot, weapon)
        if not names or not self.drawn(crop):
            return ''
        name, mse, _ = self.best_two(crop, names)
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
                crop = frame[y:y + h, x:x + w]
                names = self.candidates(slot, weapon)
                if crop.size == 0 or not names or not self.drawn(crop):
                    slots[slot] = None
                    continue
                # prefer='solved': these crops ARE slot tiles, and the bank
                # holds a picture taken in one.
                name, mse, margin = self.best_two(crop, names, prefer='solved')
                if mse > MSE_EMPTY_TH:
                    slots[slot] = None
                elif margin < MARGIN_MIN:
                    slots[slot] = (AMBIGUOUS, mse, margin)
                else:
                    slots[slot] = (name, mse, margin)
            out[gun] = slots
        return out

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
