"""Read the Tab screen: what is in the lists, what is on the guns.

This is the eyes for whatever does the equipping. Given one full-screen frame
with Tab open it answers three questions:

    which item is on each row of 库存 (inventory)
    which item is on each row of 附近 (ground loot, only there when loot is)
    what is fitted in each of the two weapons' five slots

and for every hit it hands back the screen point to grab it at, so the caller
never has to know the layout.

    from detector.tab_items import TabItemDetector

    det  = TabItemDetector()
    view = det.detect(frame)              # frame: full-screen BGR, 3440x1440

    view.inventory      # [Item | None] x12, index == row number, top first
    view.nearby         # [Item | None] x12
    view.weapons        # {1: {'muzzle': Item|None, ...}, 2: {...}}

    view.find('comp_ar')          # -> Item on whichever list holds it, or None
    view.equipped(1, 'muzzle')    # -> Item | None

Each Item carries:

    key     catalog key, e.g. 'comp_ar'      (None if it matched a template
            that has no catalog entry, e.g. a crossbow part)
    asset   template name, e.g. 'Muzzle_Compensator_Large_C'
    slot    'scope' | 'muzzle' | 'grip' | 'magazine' | 'stock'
    zh      the in-game Chinese name, for logging
    point   (x, y) to press the mouse down on — a list row's icon, or the
            centre of a weapon slot
    where   ('inventory', row) | ('nearby', row) | ('weapon', 1|2, slot)
    mse     match error; lower is better
    margin  runner-up's error over this one; >1.5 is a comfortable win

A row holding something this repo has no template for — a weapon, a backpack, a
med kit, or an attachment added after the template pack was built — comes back
as None rather than as a wrong guess, and its (panel, row) is listed in
`view.unknown`. An empty row past the end of a list is neither: it is None and
absent from `unknown`, so the caller can tell "nothing here" from "something I
cannot name".

Naming the weapons is worth doing. On the reference captures it is what turns
SKS/muzzle from Suppressor (SMG) into Suppressor (AR) — the two icons differ by
almost nothing and a blind match picks between them on a 1.3x margin.

Known gaps, all of them stale templates rather than geometry:

  Lower_ThumbGrip_C     no longer matches the drawn 拇指握把. Costs 2 inventory
                        rows on capture 1 and the grip slot of anything wearing
                        one (Mk12 on capture 2 reads 'laser' instead).
  Stock_UZI_C           misses the 折叠式枪托 fitted to the Micro UZI, so that
                        stock slot reads empty.
  ...CheekPad_C         matches in a weapon slot but not in a list row.

  no template at all    枪口制退器 (muzzle brake), 重型枪托 (heavy stock),
                        多倍率混合瞄具 (variable scope) — added to the game
                        after this pack was built.

Re-extract those with the calibrate-template skill (its scripts/ holds
extract_template.py) and the misses go away; nothing else here changes.
"""
import os
import sys

# Running this file directly (the self-check below) puts detector/ on the path,
# not the repo root, so the absolute imports would not resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from detector.attachment_detector import (AttachmentDetector, SLOT_NAMES,
                                          OFFSET_Y, OFFSET_X, MSE_EMPTY_TH)
from detector.attachment_catalog import ATTACHMENTS, compatible
from detector.tab_layout import (PANELS, INV_ROWS, ICON_W, icon_box, row_point,
                                 ATT_SLOT_XY)
from config import HUD_REGIONS, SCREEN_W, SCREEN_H

# A list row is judged against every template at once, not per slot, so its
# threshold has to be tighter than the weapon slots' MSE_EMPTY_TH=450: a row
# whose item has no template will still find *something* within 450. On the
# reference captures every true hit scored <= 130 and the best wrong answer
# scored 169, so 150 separates them without clipping a real match.
ROW_MSE_MAX = 150
ROW_MARGIN_MIN = 1.25    # runner-up must be this much worse to trust the win

# Is there a row here at all? Past the end of a list — and everywhere in 附近
# when nothing is on the ground — the cell shows the blurred world instead of
# UI. Pixel variance does not separate the two (blurred scenery is colourful),
# but high-frequency detail does: an icon has hard edges, the blur has none.
# Measured over both captures: occupied rows 702..6393, empty 0..2.
ROW_DETAIL_MIN = 100

# The same question for a weapon slot, and it needs asking for the same reason:
# a slot the weapon does not have is not drawn, and an *empty weapon slot*
# draws nothing at all, so both show blurred scenery. MSE alone does not
# reject that — on docs/tab_live_aug_vss.png, whose second weapon slot is
# empty, the magazine position matched Magazine_SR_ExtendedQuick_Mag_Vss at
# under the 450 empty threshold, and the gun read as wearing a VSS magazine it
# did not have.
#
# Measured over the three captures: slots holding something score 300..4756
# (the floor is a suppressor, a plain grey tube), slots not drawn score 1..14.
# A slot that is drawn but empty is not in that sample; the gate is deliberately
# only a floor, so anything above it still has to pass the MSE test below.
SLOT_DETAIL_MIN = 100

# asset name -> catalog key
_BY_ASSET = {a['asset']: k for k, a in ATTACHMENTS.items() if a.get('asset')}


class Item:
    __slots__ = ('key', 'asset', 'slot', 'zh', 'point', 'where', 'mse', 'margin')

    def __init__(self, asset, slot, point, where, mse, margin):
        self.key = _BY_ASSET.get(asset)
        self.asset = asset
        self.slot = slot
        self.zh = ATTACHMENTS[self.key]['zh'] if self.key else asset
        self.point = point
        self.where = where
        self.mse = mse
        self.margin = margin

    def __repr__(self):
        return (f'<Item {self.key or self.asset} {self.slot} @{self.where} '
                f'mse={self.mse:.0f} x{self.margin:.1f}>')


class TabView:
    """Everything read off one Tab frame."""

    def __init__(self, inventory, nearby, weapons, unknown):
        self.inventory = inventory
        self.nearby = nearby
        self.weapons = weapons
        self.unknown = unknown       # [('inventory', row), ...] occupied but unnamed

    def find(self, key, panel=None):
        """First Item with this catalog key, searching 库存 then 附近."""
        for name in (('inventory', 'nearby') if panel is None else (panel,)):
            for item in getattr(self, name):
                if item is not None and item.key == key:
                    return item
        return None

    def equipped(self, gun, slot):
        return self.weapons[gun][slot]

    def rows(self, panel):
        """How many rows the panel is showing, named or not.

        Lists fill from the top with no gaps, so this is one past the last
        occupied row — which is where a drop lands when something is being put
        into the panel rather than onto a particular row.
        """
        occupied = {i for p, i in self.unknown if p == panel}
        occupied |= {i for i, item in enumerate(getattr(self, panel))
                     if item is not None}
        return max(occupied) + 1 if occupied else 0

    def __repr__(self):
        n_inv = sum(i is not None for i in self.inventory)
        n_gnd = sum(i is not None for i in self.nearby)
        return (f'<TabView inventory={n_inv} nearby={n_gnd} '
                f'unknown={len(self.unknown)}>')


class TabItemDetector:
    """Template-matches the Tab screen's lists and weapon slots."""

    def __init__(self, detector=None):
        self._det = detector or AttachmentDetector()
        self._all = list(self._det._templates)
        # slot of each template, so a list hit reports where it would go
        self._slot_of = {}
        for slot, names in self._det._slot_index.items():
            for n in names:
                self._slot_of[n] = slot

    # ── scoring ──

    _SHIFTS = tuple((sy, sx) for sy in (-1, 0, 1) for sx in (-1, 0, 1))

    def _score(self, crop_f, name, shifts=_SHIFTS):
        """AttachmentDetector's own metric, over one template.

        `crop_f` is float32 already — converting it once per row rather than
        once per template is worth ~15% on its own.
        """
        tmpl_vals, ys, xs = self._det._templates[name]
        h, w = crop_f.shape[:2]
        cy, cx = ys + OFFSET_Y, xs + OFFSET_X
        best = None
        for sy, sx in shifts:
            ny = np.clip(cy + sy, 0, h - 1)
            nx = np.clip(cx + sx, 0, w - 1)
            se = ((crop_f[ny, nx] - tmpl_vals) ** 2).sum(axis=1)
            best = se if best is None else np.minimum(best, se)
        return float(best.mean() / 3)

    def _best_two(self, crop, names, shortlist=10):
        """Best and runner-up, two-stage.

        Scoring every template at all nine sub-pixel shifts costs 9x what the
        answer needs, so rank once un-shifted and re-score only the shortlist
        properly.

        The shortlist has to be generous: a shift can promote a template a long
        way. 4倍瞄准镜 does not survive a shortlist of 5 — un-shifted it ranks
        outside the top five and drops out. 8 is where both reference captures
        come back identical to the exhaustive answer; 10 is that plus margin,
        and costs 3 ms over 8 against the 170 ms it saves over 55.
        """
        crop_f = crop.astype(np.float32)
        coarse = sorted(((self._score(crop_f, n, shifts=((0, 0),)), n)
                         for n in names))
        top = [n for _, n in coarse[:shortlist]]
        fine = sorted(((self._score(crop_f, n), n) for n in top))
        m1, n1 = fine[0]
        m2 = fine[1][0] if len(fine) > 1 else float('inf')
        return n1, m1, (m2 / m1 if m1 > 0 else float('inf'))

    # ── panels ──

    def _read_row(self, frame, panel, i):
        x0, y0, x1, y1 = icon_box(i, panel)
        if y1 > frame.shape[0] or x1 > frame.shape[1]:
            return None, False
        cell = frame[y0:y1, x0:x1]
        if cell.size == 0:
            return None, False
        gray = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY)
        occupied = float(cv2.Laplacian(gray, cv2.CV_32F).var()) >= ROW_DETAIL_MIN
        if not occupied:
            return None, False
        cell = cv2.resize(cell, (63, 63), interpolation=cv2.INTER_AREA)
        name, mse, margin = self._best_two(cell, self._all)
        if mse <= ROW_MSE_MAX and margin >= ROW_MARGIN_MIN:
            return Item(name, self._slot_of.get(name, '?'), row_point(i, panel),
                        (panel, i), mse, margin), True
        return None, True

    def _read_panel(self, frame, panel):
        items, unknown = [], []
        for i in range(INV_ROWS):
            item, occupied = self._read_row(frame, panel, i)
            items.append(item)
            if item is None and occupied:
                unknown.append((panel, i))
        return items, unknown

    # ── weapons ──

    @staticmethod
    def _drawn(crop):
        """Is there UI in this cell, or is it the blurred world showing through?

        The one test that separates them; see SLOT_DETAIL_MIN. Applied before
        matching rather than after, because the answer is not "which template
        is closest" — there is no box on screen to hold a template at all.
        """
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_32F).var()) >= SLOT_DETAIL_MIN

    def _candidates(self, slot, weapon):
        """Templates worth testing in this slot.

        Naming the weapon narrows the bank to what it can physically hold,
        which is what separates Suppressor (SMG) from Suppressor (AR) — two
        near-identical icons that a blind match picks between on ~1.3x margin.
        """
        names = self._det._slot_index.get(slot, [])
        if not weapon:
            return names
        allowed = {ATTACHMENTS[k]['asset']
                   for k in compatible(weapon).get(slot, [])
                   if ATTACHMENTS[k].get('asset')}
        return [n for n in names if n in allowed] if allowed else []

    def read_weapons(self, frame, weapons=None):
        """Just the two guns' slots — {1: {slot: Item|None}, 2: {...}}.

        Same as detect().weapons, skipping the two lists. Worth having on its
        own for confirming a drag landed: only the right-hand side changes, and
        reading the lists again would cost 24 more template searches per poll.

        `weapons` must hold catalog keys or None. An unrecognised key narrows
        every template bank to nothing and reads every slot as empty, so a
        caller passing OCR output has to filter it against ROSTER first.
        """
        return self._read_weapons(frame, weapons)

    def _read_weapons(self, frame, weapons=None):
        weapons = weapons or {}
        out = {}
        for gun in (1, 2):
            weapon = weapons.get(gun)
            slots = {}
            for slot in SLOT_NAMES:
                region = f'att_{gun}_{slot}'
                y, x, h, w = HUD_REGIONS[region]
                crop = frame[y:y + h, x:x + w]
                names = self._candidates(slot, weapon)
                if crop.size == 0 or not names or not self._drawn(crop):
                    slots[slot] = None
                    continue
                name, mse, margin = self._best_two(crop, names)
                slots[slot] = (None if mse > MSE_EMPTY_TH else
                               Item(name, slot, ATT_SLOT_XY[region],
                                    ('weapon', gun, slot), mse, margin))
            out[gun] = slots
        return out

    # ── entry point ──

    def detect(self, frame, weapons=None):
        """Read one full-screen Tab frame.

        weapons: optional {1: 'g36c', 2: 'sks'} — catalog keys for what is in
        each weapon slot. Supplying them restricts each slot's template bank to
        attachments that weapon can actually take, which fixes the confusable
        pairs; without them the slots are still read, just less certainly.
        """
        inv, u1 = self._read_panel(frame, 'inventory')
        gnd, u2 = self._read_panel(frame, 'nearby')
        return TabView(inv, gnd, self._read_weapons(frame, weapons), u1 + u2)


# ════════════════════════════════════════════════════════════
# Capture
# ════════════════════════════════════════════════════════════

def tab_blocks():
    """The two screen rectangles Tab detection reads, as (y, x, h, w).

    Everything needed spans x 576..2848 but only 4.4% of the pixels in it, and
    it splits cleanly down the middle: the two lists on the left, both weapons
    on the right. Grabbing them as two blocks copies 15.7% of the screen
    instead of the 46.5% a single bounding box would.
    """
    xs0, ys0, xs1, ys1 = [], [], [], []
    for panel in PANELS:
        for i in range(INV_ROWS):
            x0, y0, x1, y1 = icon_box(i, panel)
            xs0.append(x0), ys0.append(y0), xs1.append(x1), ys1.append(y1)
    left = (min(ys0), min(xs0), max(ys1) - min(ys0), max(xs1) - min(xs0))

    rs = [r for n, r in HUD_REGIONS.items()
          if n.startswith('att_') or n.startswith('gun_name')]
    y0 = min(y for y, _, _, _ in rs)
    x0 = min(x for _, x, _, _ in rs)
    right = (y0, x0,
             max(y + h for y, _, h, _ in rs) - y0,
             max(x + w for _, x, _, w in rs) - x0)
    return {'left': left, 'right': right}


class TabGrabber:
    """Grabs just the Tab screen, into a full-screen-sized frame.

    detect() works in screen coordinates, so rather than hand it crops this
    blits the two blocks into a reused full-size buffer — the returned array
    can be indexed exactly like a real screenshot, and only the Tab regions in
    it are live.

    Measured (GDI, 3440x1440): 13.2 ms per grab, against 22.6 ms letting
    RegionGrabber band all 36 regions itself (it clusters by y only, and these
    all share a y range, so they collapse into one 46.5% box) and 61.7 ms for a
    full-screen BitBlt.

    Do NOT add these regions to config.HUD_REGIONS. That set is grabbed every
    frame, and the nearby list at x=576 would drag the DXGI bounding box left
    from x=937 — a permanent per-frame cost for a panel that only exists while
    Tab is held.
    """

    def __init__(self, only=None):
        """only: which of tab_blocks() to grab; None means all of them.

        `only=('right',)` grabs the two weapon panels alone — both name plates
        and all ten attachment slots, nothing else. That is the whole input to
        _read_guns and _slot_states, so anything asking "what are the guns
        wearing" pays for one block instead of two. The other block is the
        库存/附近 lists, needed only when reading loose items.
        """
        from detector.cropper import RegionGrabber
        self._blocks = tab_blocks()
        if only is not None:
            missing = set(only) - set(self._blocks)
            if missing:
                raise KeyError(f'no such tab block: {sorted(missing)}')
            self._blocks = {k: v for k, v in self._blocks.items() if k in only}
        # One grabber per block: a single one would re-merge them by y.
        self._grabbers = {k: RegionGrabber({k: v})
                          for k, v in self._blocks.items()}
        self._buf = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)

    def grab(self):
        for key, g in self._grabbers.items():
            y, x, h, w = self._blocks[key]
            self._buf[y:y + h, x:x + w] = g.grab()[key]
        return self._buf

    def close(self):
        for g in self._grabbers.values():
            g.close()
        self._grabbers = {}


if __name__ == '__main__':
    # Item names are Chinese; a Windows console defaults to cp1252 and would
    # die on the first 倍.
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shots = sys.argv[1:] or [os.path.join(root, 'docs', 'tab_inventory.png'),
                             os.path.join(root, 'docs', 'tab_inventory_2.png')]
    det = TabItemDetector()
    for path in shots:
        frame = cv2.imread(path)
        if frame is None:
            print(f'cannot read {path}')
            continue
        # The two reference captures, so the catalog-narrowed path is exercised.
        known = {'tab_inventory.png': {1: 'g36c', 2: 'sks'},
                 'tab_inventory_2.png': {1: 'uzi', 2: 'mk12'}}
        view = det.detect(frame, known.get(os.path.basename(path)))
        print(f'\n=== {os.path.basename(path)} ===  {view}')
        for panel in ('inventory', 'nearby'):
            rows = getattr(view, panel)
            if not any(r is not None for r in rows) and not any(
                    p == panel for p, _ in view.unknown):
                continue
            print(f'  {panel}:')
            for i, item in enumerate(rows):
                if item is not None:
                    print(f'    row{i:2d} {item.zh}   ({item.key}) '
                          f'mse={item.mse:.0f} x{item.margin:.1f} @{item.point}')
                elif (panel, i) in view.unknown:
                    print(f'    row{i:2d} <occupied, no template>')
        for gun in (1, 2):
            got = {s: (it.key or it.asset) for s, it in view.weapons[gun].items()
                   if it is not None}
            print(f'  weapon {gun}: {got}')
