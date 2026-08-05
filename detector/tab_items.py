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

from detector.attachment_detector import AttachmentDetector
from detector.attachment_catalog import ATTACHMENTS
from detector.tab_layout import (PANELS, INV_ROWS, icon_box, row_point,
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

    # ── panels ──
    #
    # The metric is AttachmentDetector.best_two(). A list row and a weapon slot
    # differ in which templates they try and how strict they are about the win,
    # never in how a template is scored -- so there is one implementation and
    # this asks it, rather than holding a copy that can drift from what the
    # live recoil loop reads through.

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
        # AT 80x80, ITS OWN SIZE. This used to resize to 63x63 so the row would
        # fit the slot geometry the whole bank was once read with, and that
        # became wrong the moment row pictures were stored at 80x80: an 80x80
        # template does not fit a 63x63 crop, so `_variant` correctly skipped
        # every one of them and the row was judged by the SLOT rendering of a
        # squashed picture. Measured on a live 库存 holding 10 known items
        # (2026-08-05, temp_debug/tab_now.png):
        #
        #             correct   MSE (gate 150)   margin (gate 1.25)
        #   63x63       0/10        255..1147          1.03..2.07
        #   80x80      10/10          1.3..68.5        2.57..205
        #
        # Nothing was wrong with the templates. `pixi run attachments` scored
        # rows 930/1050 all the way through, because tools/score_attachments.py
        # HAD been moved to 80x80 and this reader had not — so the ratchet was
        # green on a path the game never takes. If this line comes back, that
        # gate will not notice.
        #
        # prefer='row': this cell is an inventory ROW, and the bank holds a
        # picture taken as one. See _rank_variant.
        name, mse, margin = self._det.best_two(cell, self._all, prefer='row')
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
        """AttachmentDetector.read_slots, wrapped as Items.

        The gate, the candidate narrowing and the two-stage search are all
        over there now; this adds the address a caller needs to click.
        """
        return {gun: {slot: (Item(hit[0], slot, ATT_SLOT_XY[f'att_{gun}_{slot}'],
                                  ('weapon', gun, slot), hit[1], hit[2])
                             if hit else None)
                      for slot, hit in slots.items()}
                for gun, slots in self._det.read_slots(frame, weapons).items()}

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

def row_icons(frame, n, panel='inventory'):
    """The first `n` row icons of a panel, copied. [crop, ...]"""
    out = []
    for i in range(n):
        x0, y0, x1, y1 = icon_box(i, panel)
        out.append(frame[y0:y1, x0:x1].copy())
    return out


def panel_rows(frame, panel='inventory'):
    """How many rows of a list hold something. -> int

    The occupancy half of _read_row and nothing else: the Laplacian test that
    separates an icon from the blurred world behind the panel, with no
    template match on top. Two reasons that matters, and they are the same
    reason from both ends —

      it is ~1 ms rather than ~40, so a drag can afford to read it back
      it answers for an item this repo has NO icon for, which is exactly the
      case a template-collection run is in

    Lists fill from the top with no gaps, so this is also where the next drop
    lands. Same value TabView.rows() reports, arrived at without naming
    anything.
    """
    n = 0
    for i in range(INV_ROWS):
        x0, y0, x1, y1 = icon_box(i, panel)
        if y1 > frame.shape[0] or x1 > frame.shape[1]:
            break
        cell = frame[y0:y1, x0:x1]
        if cell.size == 0:
            break
        gray = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY)
        if float(cv2.Laplacian(gray, cv2.CV_32F).var()) >= ROW_DETAIL_MIN:
            n = i + 1
    return n


def inserted_row(before, after, change_min=6.0):
    """Which row is NEW between two readings of a panel. -> index | None

    Frames in, an index out -- no game, no device, and no template. It reads
    an icon only as pixels that did or did not move.

    The game sorts its lists, so a new item is INSERTED rather than appended:
    rows above the insertion are untouched and everything below shifts down by
    one. That is what makes the index findable without naming anything --

        the insertion point is the first row whose picture changed

    and every row after it is just its predecessor, moved.

    Taking `len(after) - 1` instead assumed the new row was the last one. It is
    not, and a template collection run built on that assumption photographed
    the same leftover row seven times under seven different part names.
    """
    for i, (b, a) in enumerate(zip(before, after)):
        d = np.abs(b.astype(np.float32) - a.astype(np.float32)).mean()
        if d >= change_min:
            return i
    # Nothing above moved, so the new row is the first one only `after` has --
    # the genuinely-appended case, which happens when the sort puts it last.
    return len(before) if len(after) > len(before) else None


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
