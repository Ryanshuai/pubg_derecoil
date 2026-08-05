"""Which weapon is drawn in the HUD — by matching real screen captures.

    from detector.weapon_hud_detector import WeaponHudDetector
    det = WeaponHudDetector()
    det.classify({'weapon_1': crop, 'weapon_2': crop})   # ('aug', 'm416')
    det.read(crop)                                       # ('aug', 6.31) name+margin

Replaces the CNN that used to answer this (dl_models/gun_name.pth.tar). That
model read an AUG as a JS9 on 77 saved frames -- the two are both bullpups and
their silhouettes overlap at IoU 0.549 -- and its class list had drifted eight
weapons away from the roster, including calling the Kar98k `98k`, a name
nothing downstream recognises.

HOW IT WORKS, and every step of it was measured against the alternative:

  feature    dewhite (background subtracted, see dl_models/icon_merging) then
             divided by the crop's own p99. The HUD draws the icon at alpha
             0.80 selected / 0.405 not, so raw intensity is not comparable
             between frames and the normalised signal is.
  bank       REAL CAPTURES, 48 per weapon, not one averaged template. One
             median template scores 0.871; the exemplars score 0.975, because
             the spread across alphas and backgrounds is real signal about how
             the game actually draws, and averaging destroys it.
  PCA 64d    not only cheaper but BETTER: 0.962 at full 10918d against 0.975
             at 64d, the difference being noise the projection discards. Also
             53 MB -> 3 MB and a 170x smaller matmul.
  verdict    cosine to the nearest exemplar of each weapon, with a margin gate.

The game's own extracted art was tried first and scores 0.489. It is the INPUT
to the game's compositing and the detector only ever sees the output -- the
same finding as the attachment bank, and the reason no game-file art is left
anywhere in this tree.

⚠ THE BANK ONLY KNOWS WEAPONS IT HAS FRAMES OF. A weapon with no captures
cannot be read, and will be answered as the nearest weapon that does have
them, confidently. That is what MARGIN_MIN is for: a weapon the bank has never
seen tends to tie its neighbours, and a tie returns '' rather than a guess.
Frame counts and the gaps are printed by tools/build_weapon_hud_bank.py.
"""
import os

import cv2
import numpy as np

DIMS = 64
BANK_PATH = os.path.join(os.path.dirname(__file__), '..', 'training_data',
                         'weapon_hud_bank.npz')

# Cosine gap between the best weapon and the runner-up. Measured over 715
# hold-out frames: correct reads sit at a median gap of 0.198, wrong ones at
# 0.011 -- an 18x separation, which is why a gate here converts most of the
# residual error into a refusal instead of a confident wrong answer. Set at
# the wrong-read median rather than lower: this reader feeds recoil
# compensation, where naming the wrong gun is worse than naming none.
MARGIN_MIN = 0.011


def feature(bgr):
    """-> flat float32, or None if there is no signal in the crop.

    Deliberately the same quantity the bank was built from; anything that
    changes here has to rebuild the bank, which is why both live in this file's
    module namespace rather than being written out twice.
    """
    if bgr is None or bgr.ndim != 3:
        return None
    f = bgr.astype(np.float32)
    sig = np.clip(f - cv2.GaussianBlur(f, (31, 31), 10), 0, 255) * 2
    sig = np.clip(sig, 0, 255)
    g = cv2.cvtColor(sig.astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
    p = float(np.percentile(g, 99))
    if p < 1e-3:
        return None
    return (g / p).ravel()


class WeaponHudDetector:
    """Reads weapon names from the two HUD icon crops."""

    def __init__(self, path=BANK_PATH, margin_min=MARGIN_MIN):
        self.margin_min = margin_min
        self._ok = False
        if not os.path.isfile(path):
            return
        d = np.load(path, allow_pickle=False)
        self.mean = d['mean']
        self.basis = d['basis']
        proj = d['proj'].astype(np.float32)
        self.proj = proj / np.maximum(
            np.linalg.norm(proj, axis=1, keepdims=True), 1e-6)
        self.codes = [str(c) for c in d['codes']]
        idx = {c: i for i, c in enumerate(self.codes)}
        self.owner = np.array([idx[str(l)] for l in d['labels']], np.int32)
        self._ok = True

    @property
    def ready(self):
        """False when the bank file is missing. A missing bank reads '' for
        everything rather than raising -- the same shape a missing template
        bank has elsewhere, so a caller does not have to special-case it."""
        return self._ok

    def scores(self, crop):
        """One HUD crop -> {weapon: cosine to its nearest exemplar}.

        The whole ranking, for callers that are asking about a PARTICULAR
        weapon rather than "which is it" -- capture_ads checks whether the gun
        it just spawned actually reached a HUD slot, and a winner-take-all
        answer cannot tell "slot 2 holds something else" from "slot 2 holds
        the right gun but scored second".
        """
        if not self._ok:
            return {}
        f = feature(crop)
        if f is None or f.shape[0] != self.basis.shape[1]:
            return {}
        q = (f - self.mean) @ self.basis.T
        n = float(np.linalg.norm(q))
        if n < 1e-6:
            return {}
        sim = self.proj @ (q / n)
        best = np.full(len(self.codes), -1.0, np.float32)
        np.maximum.at(best, self.owner, sim)
        return {c: float(best[i]) for i, c in enumerate(self.codes)}

    def read(self, crop):
        """One HUD crop -> (name, margin). '' when nothing is separable."""
        s = self.scores(crop)
        if not s:
            return ('', 0.0)
        best = np.array([s[c] for c in self.codes], np.float32)
        order = np.argsort(-best)
        margin = float(best[order[0]] - best[order[1]]) if len(order) > 1 \
            else float('inf')
        if margin < self.margin_min:
            return ('', margin)
        return (self.codes[order[0]], margin)

    def classify(self, crops):
        """crops: {'weapon_1': ndarray, 'weapon_2': ndarray} -> (name1, name2).

        Signature matches the classifier this replaces, so robot.py's
        dispatcher registration is the only line that had to change.
        """
        return tuple(self.read(crops.get(k))[0] if crops.get(k) is not None
                     else '' for k in ('weapon_1', 'weapon_2'))
