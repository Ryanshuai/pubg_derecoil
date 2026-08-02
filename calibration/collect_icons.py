"""Spawn attachments, photograph them against many backgrounds, name them.

For when something in the inventory cannot be detected at all. The half grip
sat in row 3 as `<occupied, no template>` for a whole calibration run: present,
visible, and invisible to find(), which reported it as "not on screen".

    pixi run python calibration/collect_icons.py --slot grip
    pixi run python calibration/collect_icons.py --keys comp_ar,vert_grip --angles 8
    pixi run python calibration/collect_icons.py --slot grip --check-only

What makes this work is that the ground truth is self-specified. The spawner is
told exactly what to produce and in what order, and 库存 fills from the top with
no gaps, so row N holds a known item even when nothing on screen can name it.
That is the one situation where a broken template cannot hide.

Two outputs, from the same captures:

  * a labelled crop per item per background, ready for `calibrate-template`
    to extract from. The Tab panel is translucent, so a template built from
    one background tracks that background; the view is turned between
    captures precisely to vary what shows through.
  * a coverage table — for each item, how many of those backgrounds the
    current templates recognised it in, and what it was mistaken for. A
    template that reads at some angles and not others is worse than one that
    never reads, and only a spread of backgrounds shows the difference.

Turning happens with Tab CLOSED. With the inventory open the mouse drives a
cursor, not the view, so a turn issued there moves nothing and every capture
comes back identical.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from detector import tab_layout
from detector.cropper import RegionGrabber
from press.pico_mouse import get_mouse
from press.pointer import ensure_focus

import spawner_control as spawner_mod
from spawner_control import SpawnerControl
from attach_control import AttachControl
from harvest import Panel
from sweep import Rig

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(os.path.dirname(HERE), 'docs', 'attachments', 'runs')

TURN_COUNTS = 900        # yaw per step; large enough to land on new scenery
PITCH_STEPS = (0, -260, 260)   # sky, level, ground — the three that differ most
SETTLE_S = 0.45


def icon_region(row):
    x0, y0, x1, y1 = tab_layout.icon_box(row, 'inventory')
    return (y0, x0, y1 - y0, x1 - x0)


class Collector:
    def __init__(self, rig, panel, sc, verbose=False):
        self.rig = rig
        self.panel = panel
        self.sc = sc
        self.ac = AttachControl(verbose=verbose)
        self.mouse = rig.mouse

    def close(self):
        for x in (self.ac,):
            try:
                x.close()
            except Exception:
                pass

    def read_inventory(self):
        """(rows, view) with Tab opened and closed around it."""
        if not self.rig.ensure_inventory_open():
            return None, None
        try:
            if not self.ac.sync():
                return None, None
            view = self.ac.look()
            rows = [(i, it) for i, it in enumerate(view.inventory)]
            return rows, view
        finally:
            self.rig.ensure_inventory_closed()

    def n_occupied(self, view):
        named = {i for i, it in enumerate(view.inventory) if it is not None}
        named |= {i for p, i in view.unknown if p == 'inventory'}
        return max(named) + 1 if named else 0

    def spawn_all(self, keys):
        if not self.panel.ensure_open():
            print("[!] spawner panel would not open")
            return False
        ok = self.sc.sync()
        if ok:
            for k in keys:
                if not self.sc.give_attachment(k):
                    print(f"[!] spawner would not produce {k}")
                    ok = False
        self.panel.ensure_closed()
        return ok

    def turn(self, yaw, pitch):
        """Change what shows through the translucent panel. Tab must be shut —
        with it open the mouse drives a cursor and the view does not move."""
        self.rig.ensure_inventory_closed()
        self.mouse.move(yaw, pitch)
        time.sleep(SETTLE_S)


def capture(collector, rows, out_dir, tag):
    """One background: grab every named row's icon and save it."""
    regions = {f'r{r}': icon_region(r) for r, _ in rows}
    grab = RegionGrabber(regions)
    try:
        if not collector.rig.ensure_inventory_open():
            return {}
        if not collector.ac.sync():
            return {}
        collector.ac.park()           # hover restyles icons and bleeds bright px
        time.sleep(0.25)
        for _ in range(4):
            f = grab.grab()
        got = collector.ac.look()
        out = {}
        for r, key in rows:
            crop = f.get(f'r{r}')
            if crop is None:
                continue
            cv2.imwrite(os.path.join(out_dir, f'{key}__{tag}__row{r:02d}.png'),
                        crop)
            item = got.inventory[r] if r < len(got.inventory) else None
            out[key] = item.key if item is not None else None
        return out
    finally:
        grab.close()
        collector.rig.ensure_inventory_closed()


def report(truth, seen_by_tag, keys):
    print("\n" + "=" * 72)
    print("COVERAGE — what the CURRENT templates make of known items")
    print("=" * 72)
    n = len(seen_by_tag)
    print(f"{'item':<14}{'seen':>8}   read as")
    print("-" * 72)
    broken = []
    for k in keys:
        reads = [seen.get(k) for seen in seen_by_tag.values()]
        hits = sum(1 for r in reads if r == k)
        others = sorted({str(r) for r in reads if r != k})
        note = ', '.join(others) if others else ''
        flag = ''
        if hits == 0:
            flag = '   <-- never recognised'
            broken.append(k)
        elif hits < n:
            flag = '   <-- background dependent'
            broken.append(k)
        print(f"{k:<14}{hits:>4}/{n:<3}   {note}{flag}")
    print()
    if broken:
        print("These need re-extracting. Hand the crops to calibrate-template:")
        print(f"  the ones named <item>__<background>__rowNN.png")
        print(f"  broken: {', '.join(broken)}")
    else:
        print("Every item read correctly against every background.")
    return broken


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--slot', default=None,
                    help="collect every spawnable attachment for this slot "
                         "(grip, muzzle, scope, stock, magazine)")
    ap.add_argument('--keys', default=None, help='explicit comma-separated keys')
    ap.add_argument('--angles', type=int, default=6,
                    help='how many backgrounds to photograph against')
    ap.add_argument('--check-only', action='store_true',
                    help='do not spawn anything; photograph what is already there')
    ap.add_argument('--out', default='')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    if args.keys:
        keys = [k.strip() for k in args.keys.split(',') if k.strip()]
    elif args.slot:
        keys = sorted(k for k, v in spawner_mod.ATTACHMENTS.items()
                      if v['slot'] == args.slot)
    else:
        print("[!] need --slot or --keys")
        return 1
    bad = [k for k in keys if k not in spawner_mod.ATTACHMENTS]
    if bad:
        print(f"[!] not spawnable: {bad}")
        return 1

    out_dir = args.out or os.path.join(
        OUT_ROOT, datetime.now().strftime('%Y%m%d_%H%M%S'))
    os.makedirs(out_dir, exist_ok=True)

    print(f"items    : {len(keys)} — {', '.join(keys)}")
    print(f"angles   : {args.angles}")
    print(f"out      : {out_dir}\n")

    rig = Rig('red_dot')
    panel = Panel(rig.mouse)
    sc = SpawnerControl(verbose=False)
    col = Collector(rig, panel, sc)

    print(">>> Stand at an item spawner with room to turn around.")
    if not ensure_focus(countdown_s=args.countdown, label='icon collection'):
        print("[!] ABORT: game not focused, and could not take the "
              "foreground. Is PUBG running?")
        return 1
    time.sleep(0.6)

    try:
        rows0, view0 = col.read_inventory()
        if view0 is None:
            print("[!] could not read the inventory")
            return 1
        base = col.n_occupied(view0)
        print(f"inventory already holds {base} row(s)")

        if not args.check_only:
            if not col.spawn_all(keys):
                print("[!] spawning was incomplete — carrying on with what "
                      "landed; the coverage table will show gaps")
            rows1, view1 = col.read_inventory()
            if view1 is None:
                print("[!] could not re-read the inventory")
                return 1
            grew = col.n_occupied(view1) - base
            if grew != len(keys):
                print(f"[!] expected {len(keys)} new rows, got {grew}. Row "
                      f"labels would be wrong, so stopping rather than "
                      f"mislabelling every crop.")
                return 1
            rows = [(base + i, k) for i, k in enumerate(keys)]
        else:
            _, view1 = col.read_inventory()
            rows = [(i, (it.key if it else f'row{i}'))
                    for i, it in enumerate(view1.inventory) if it is not None]

        print("row -> item (ground truth, from what was spawned):")
        for r, k in rows:
            print(f"  row {r:2d}  {k}")

        seen = {}
        for a in range(args.angles):
            pitch = PITCH_STEPS[a % len(PITCH_STEPS)]
            col.turn(TURN_COUNTS, pitch)
            tag = f'bg{a}'
            got = capture(col, rows, out_dir, tag)
            named = sum(1 for k, v in got.items() if v == k)
            print(f"  {tag}: {named}/{len(rows)} read correctly")
            seen[tag] = got
            col.turn(0, -pitch)          # undo the pitch, keep the yaw

        broken = report({k: k for _, k in rows}, seen, [k for _, k in rows])
        with open(os.path.join(out_dir, 'coverage.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'keys': [k for _, k in rows], 'seen': seen,
                       'broken': broken,
                       'ts': datetime.now().isoformat(timespec='seconds')},
                      f, indent=2, ensure_ascii=False)
        print(f"\n  crops + coverage.json -> {out_dir}")
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        col.close()
        rig.close()
        panel.close_grabber()
    return 0


if __name__ == '__main__':
    sys.exit(main())
