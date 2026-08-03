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
        self._templates = {}      # name → (tmpl_vals, ys, xs)
        self._slot_index = {}     # slot_name → [name, ...]
        self._load_templates()

    def _load_templates(self):
        if not os.path.isdir(TMPL_DIR):
            return
        for fname in os.listdir(TMPL_DIR):
            if not fname.endswith('.png'):
                continue
            img = cv2.imread(os.path.join(TMPL_DIR, fname), cv2.IMREAD_UNCHANGED)
            if img is None or img.shape[2] != 4:
                continue
            name = fname.replace('Item_Attach_Weapon_', '').replace('.png', '')
            resized = cv2.resize(img, (TMPL_SIZE, TMPL_SIZE), interpolation=cv2.INTER_AREA)
            mask = resized[:, :, 3] > ALPHA_TH
            if int(mask.sum()) < 30:
                continue
            tmpl_bgr = resized[:, :, :3].astype(np.float32)
            ys, xs = np.where(mask)
            self._templates[name] = (tmpl_bgr[ys, xs], ys, xs)
            for slot_name, prefixes in SLOT_PREFIXES.items():
                if any(name.startswith(p) for p in prefixes):
                    self._slot_index.setdefault(slot_name, []).append(name)

    # ── scoring ──

    def score(self, crop_f, name, shifts=_SHIFTS):
        """One template against one float32 crop. -> mean squared error.

        `crop_f` is float32 already: converting once per crop rather than once
        per template is worth ~15% on its own.
        """
        tmpl_vals, ys, xs = self._templates[name]
        h, w = crop_f.shape[:2]
        cy, cx = ys + OFFSET_Y, xs + OFFSET_X
        best = None
        for sy, sx in shifts:
            ny = np.clip(cy + sy, 0, h - 1)
            nx = np.clip(cx + sx, 0, w - 1)
            se = ((crop_f[ny, nx] - tmpl_vals) ** 2).sum(axis=1)
            best = se if best is None else np.minimum(best, se)
        return float(best.mean() / 3)

    def best_two(self, crop, names, shortlist=SHORTLIST):
        """-> (name, mse, margin). Two-stage; see SHORTLIST."""
        crop_f = crop.astype(np.float32)
        coarse = sorted(((self.score(crop_f, n, shifts=((0, 0),)), n)
                         for n in names))
        top = [n for _, n in coarse[:shortlist]]
        fine = sorted(((self.score(crop_f, n), n) for n in top))
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
                name, mse, margin = self.best_two(crop, names)
                slots[slot] = None if mse > MSE_EMPTY_TH else (name, mse, margin)
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
