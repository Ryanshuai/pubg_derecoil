"""Fill 库存 with a known batch of parts and photograph it, for a VLM to read.

    pixi run rows-batch --batch 0          # spawn batch 0 and shoot it
    pixi run rows-batch --plan             # what the batches are; no game

WHY A MODEL AND NOT A TEMPLATE. The list row carries the part's NAME in white
text, and that text is the one thing on this screen that is not composited:
measured on a live frame, the glyphs are 255 with a channel spread of 0 while
the background behind them sits at 87-130 and moves with the view. So the name
is legible in a way the icon is not -- but reading it needs an OCR the repo
does not have, and a template bank for the names is the same chicken-and-egg
as the icons (a template must exist before it can name the thing it was built
from).

A vision model has no such bootstrap. It reads the frame the way the operator
does. So this file does the half that needs the game -- put a KNOWN set of
parts in the list, take one clean frame -- and the naming happens outside it.

⚠ THE POINT IS THE CROSS-CHECK, NOT THE READING. The batch is spawned from
catalogue keys, so the program already knows WHICH parts are in the list; what
it does not know is WHICH ROW each landed in, because the game inserts into
its own sort order (`legacy_collect_templates` has seven crops filed under
seven wrong names from assuming otherwise). The model supplies the row order.
The two are then required to agree AS SETS -- every key spawned appears
exactly once among the names read, and nothing else does. A batch that fails
that test is thrown away rather than relabelled: a disagreement means either a
part did not arrive or a row was misread, and neither is repairable by
guessing which.

⚠ ONE FRAME, CURSOR PARKED. A hovered row draws a tooltip over its own
neighbours. `InventoryControl.frame()` parks for its own reasons and that is
relied on here.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from detector.attachment_catalog import ATTACHMENTS
from detector.tab_layout import icon_box
from control.session import ensure_ready
from control.spawner import SpawnerControl
from control.inventory import InventoryControl
from control.kitting import BACKPACK
from capture.cropper import capture_screen
from calibration.sweep import Rig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'calibration', 'artifacts', 'rows_vlm')

# ⚠ THIRTEEN, the operator's count, and it is now MEASURED rather than taken
# on trust -- see MAX_ROWS below. It disagreed with tab_layout.INV_ROWS (12)
# and the operator was right.
BATCH = 13
# ⚠ MEASURED, AND tab_layout.INV_ROWS IS WRONG. That constant says 12 with the
# comment "rows visible at 1440p before scrolling" and no measurement behind
# it. Batch 0's frame settles it: row 12 holds Choke (detail 3088) and rows 13
# and 14 are drawn, on-screen and EMPTY (0.5 / 0.8). So the list shows at least
# 13 -- the operator's number -- and 12 was cropping the last part off every
# batch. INV_ROWS is not changed from here: it also addresses drags and the
# panel-row count in control/, so it moves in its own change with its own gate.
MAX_ROWS = 15                   # 15 is where icon_box runs off a 1440 screen
ROW_DETAIL_MIN = 100.0          # occupied 678-5228, empty 0.5-0.8
CLEAR_TRIES = 4


def batches(size=BATCH):
    """Every catalogue key, in fixed chunks. -> [[key, ...], ...]

    Sorted by key so the batches are the same on every machine and every run:
    a batch that reshuffles cannot be re-shot after a bad read.
    """
    keys = sorted(ATTACHMENTS)
    return [keys[i:i + size] for i in range(0, len(keys), size)]


def rows_held(frame):
    """How many 库存 rows have something in them. TEMPLATE-FREE.

    Laplacian variance per icon box: measured occupied 678-5228, empty 0.5-0.8,
    so the gate at 100 sits in a gap three orders wide. No template is
    consulted, which is the point -- a part whose template is missing or stale
    is exactly the part this collector exists to photograph, and a
    template-based count would read it as an empty list.
    """
    n = 0
    for i in range(MAX_ROWS):
        x0, y0, x1, y1 = icon_box(i, 'inventory')
        if y1 > frame.shape[0] or x1 > frame.shape[1]:
            break
        g = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        if cv2.Laplacian(g, cv2.CV_64F).var() > ROW_DETAIL_MIN:
            n = i + 1
    return n


def clear_verified(ac, tries=CLEAR_TRIES):
    """Empty 库存 and PROVE it. -> bool

    ⚠ THE PROOF IS THE POINT, and it is the operator's correction: a batch
    cannot go into a list that still holds the last one. Two ways this bites,
    and neither announces itself:

        the list overflows   13 + 13 = 26 rows into a window that draws ~15,
                             so the tail of batch N+1 is never photographed
        nothing spawns       a FULL 库存 makes the spawner silently produce
                             NOTHING (docs/game_quirks.md). Three template runs
                             died that way, and the only sign in the log was
                             `no bare host gun` several steps later.

    `clear_inventory` drags, and drags land ~93% of the time -- so calling it
    once and moving on is a coin flip, not a clearing. This counts the rows
    back with the template-free judgement and retries until they are gone.
    """
    for attempt in range(1, tries + 1):
        ac.clear_inventory()
        held = rows_held(capture_screen())
        if not held:
            print(f'库存 empty (verified, {attempt} pass'
                  f'{"" if attempt == 1 else "es"})')
            return True
        print(f'[!] 库存 still holds {held} row(s) after pass {attempt}')
    print(f'[!] ABORT: 库存 would not clear in {tries} passes. Spawning into a '
          f'full list produces nothing and says nothing.')
    return False


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--batch', type=int, help='which chunk (0-based)')
    ap.add_argument('--keys', help='explicit comma-separated keys instead')
    ap.add_argument('--plan', action='store_true', help='print and exit')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    chunks = batches()
    if args.plan:
        print(f'{len(ATTACHMENTS)} parts in {len(chunks)} batches of {BATCH} '
              f'(measured: the list draws 13 occupied rows)')
        for i, c in enumerate(chunks):
            print(f'  {i}: {" ".join(c)}')
        return 0

    keys = ([k.strip() for k in args.keys.split(',') if k.strip()]
            if args.keys else
            chunks[args.batch] if args.batch is not None else None)
    if not keys:
        ap.error('give --batch, --keys or --plan')
    unknown = [k for k in keys if k not in ATTACHMENTS]
    if unknown:
        ap.error(f'not in the catalogue: {", ".join(unknown)}')
    # ⚠ THAT DISAGREEMENT IS SETTLED, and how it was settled is the reusable
    # part. The operator said 13, tab_layout said 12, and the resolution was
    # not to pick one: spawn all 13, save the FULL SCREEN, and let the frame
    # answer. It did -- row 12 holds a part, rows 13-14 are drawn and empty.
    # Clamping the batch to 12 would have "settled" it by throwing away the
    # only evidence that could.

    ready = ensure_ready(label='row batch', countdown_s=args.countdown)
    if not ready['ok']:
        print(f'[!] ABORT: not ready — failed at {ready["failed"]!r}')
        return 1

    stamp = time.strftime('%Y%m%d_%H%M%S')
    os.makedirs(OUT, exist_ok=True)
    rig = Rig('red_dot')
    sc, ac = SpawnerControl(verbose=False), InventoryControl(verbose=False)
    try:
        # A backpack first: without one the parts do not refuse to spawn, they
        # go somewhere else. Then clear, so the list holds THIS batch and
        # nothing left over -- the set cross-check depends on it.
        rig.gun.ensure_inventory_closed()
        sc.give_many([BACKPACK])
        # ⚠ `ac.is_tab_open()` ASKS whether the panel is up; it does not put it
        # up, and ensure_ready leaves Tab SHUT on purpose -- so polling it here
        # is correct-and-useless and the batch aborted before spawning
        # anything. `ensure_inventory_open` is the action.
        if not rig.gun.ensure_inventory_open():
            print('[!] the inventory would not open')
            return 1
        ac.clear_rack()
        if not clear_verified(ac):
            return 1

        rig.gun.ensure_inventory_closed()
        rec = sc.give_many(list(keys))
        if not rec.get('ok'):
            print(f'[!] the spawner refused: {rec.get("error")}')
        time.sleep(1.0)
        if not rig.gun.ensure_inventory_open():
            print('[!] the inventory would not reopen after spawning')
            return 1
        # ⚠ FULL SCREEN, NOT `ac.frame()`. That one is a BANDED grab -- it
        # cuts a strip and the row LABELS fall outside it, while
        # `icon_box()` returns full-screen coordinates. Cropping one with the
        # other does not fail, it silently returns a different rectangle: the
        # first run of this file photographed the 附近 list lying on the floor
        # and filed it as the backpack. calibrate-template's Step 0a says this
        # in as many words and it was not followed.
        #
        # ac.frame() is still called first, for the cursor park it does.
        ac.frame()
        time.sleep(0.3)
        frame = capture_screen()

        shot = os.path.join(OUT, f'{stamp}__rows.png')
        cv2.imwrite(shot, frame)
        # ⚠ CROP WHAT IS OCCUPIED, not range(INV_ROWS). That constant is 12
        # and the list holds 13, so the last part of every batch lost its
        # crop -- silently, because a short list of crops looks exactly like a
        # short batch. `held` is the template-free count.
        held = rows_held(frame)
        rows = []
        for i in range(held):
            x0, y0, x1, y1 = icon_box(i, 'inventory')
            cell = os.path.join(OUT, f'{stamp}__row{i:02d}.png')
            cv2.imwrite(cell, frame[y0:y1, x0:x1])
            rows.append({'row': i, 'icon': os.path.basename(cell),
                         'box': [x0, y0, x1, y1]})
        # ⚠ THE COUNT IS A CROSS-CHECK, not a log line. Rows held and parts
        # spawned are two independent statements about the same batch: one
        # comes from the spawner's own record, the other from the screen. They
        # disagree when a part did not arrive, when the list was not empty
        # first, or when the batch overflowed the window -- and a disagreement
        # makes the row->key mapping unrecoverable, so it is reported here
        # rather than discovered later as a mislabelled template.
        if held != len(keys):
            print(f'[!] {len(keys)} parts spawned but {held} rows are '
                  f'occupied — this batch is VOID. The names cannot be '
                  f'matched to the keys when the counts disagree.')
        meta = {'stamp': stamp, 'spawned': list(keys), 'rows': rows,
                'rows_held': held, 'counts_agree': held == len(keys),
                'shot': os.path.basename(shot),
                'note': 'names are read from the shot by a vision model; the '
                        'set of names MUST equal `spawned` or the batch is void'}
        with open(os.path.join(OUT, f'{stamp}__meta.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(meta, f, indent=1, ensure_ascii=False)
        print(f'spawned {len(keys)}: {" ".join(keys)}')
        print(f'shot    {os.path.relpath(shot, ROOT)}')
        print(f'meta    {os.path.relpath(os.path.join(OUT, stamp + "__meta.json"), ROOT)}')
    finally:
        try:
            ac.close()
        except Exception:
            pass
        rig.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
