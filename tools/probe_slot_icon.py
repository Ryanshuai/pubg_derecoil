"""Photograph one weapon's attachment slots and print what the reader says.

    pixi run python tools/probe_slot_icon.py --gun 1
    pixi run python tools/probe_slot_icon.py --gun 1 --slot magazine

Neither half is enough on its own, which is the whole point:

  the readback   confident and in-catalogue even when it is wrong. A drifted
                 thumb-grip template once read Mk12's grip as `laser`, and an
                 unrecognised part reads `empty` by design (slot_detector's
                 deliberate trade), so "nothing there" and "nothing I know" are
                 the same answer.
  the picture    settles it in one glance and cannot be automated into the
                 loop, because the thing being checked IS the automation.

Written 2026-08-08 for the magazine slot specifically. That slot is
deliberately OUT of the recoil config key -- the templates separate it worst,
and with it in the key one gun produced two filenames an hour apart -- so
`magazine` is the one slot where a wrong reading leaves NO trace anywhere. It
currently reads `'?'` on the mp5k while every burst fires 40 rounds against a
30-round base magazine, and a program cannot close that: the value exists in
one place (`magazine_size`) and the thing that would contradict it exists in
zero.

Writes a full screenshot plus a blown-up strip of the five tiles, so the
answer survives the session that asked the question.
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from control.session import ensure_ready
from control.inventory import InventoryControl
from detector.tab_layout import SLOT_NAMES, slot_tile_box
from detector.attachment_detector import AttachmentDetector
from detector.attachment_catalog import ATTACHMENTS

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'calibration', 'artifacts', 'kit_checks')
ZOOM = 4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gun', type=int, default=1)
    ap.add_argument('--slot', default=None,
                    help='one of %s; default is all five' % (SLOT_NAMES,))
    ap.add_argument('--spawn', default=None,
                    help='put this weapon in the rack first if none is there')
    ap.add_argument('--countdown', type=int, default=4)
    a = ap.parse_args()

    slots = (a.slot,) if a.slot else SLOT_NAMES
    if not ensure_ready(label='photographing the slot tiles',
                        countdown_s=a.countdown)['ok']:
        return 1

    os.makedirs(OUT, exist_ok=True)
    stamp = datetime.now().strftime('%m%d_%H%M%S')
    by_asset = {v['asset']: k for k, v in ATTACHMENTS.items()
                if isinstance(v, dict) and v.get('asset')}

    if a.spawn:
        # Entering the training range EMPTIES the rack, and ensure_ready
        # re-enters whenever it finds the game in the lobby -- which it did on
        # the first run of this probe, after the game had exited and relaunched
        # on its own. Spawning here is also the RIGHT subject for the question
        # this was written for: what does the spawner HAND OUT.
        from control.spawner import SpawnerControl
        from control.stock import ensure_weapon_in_hand
        with InventoryControl() as ac, SpawnerControl(verbose=False) as sc:
            sc.ensure_panel(False)
            got = ensure_weapon_in_hand(ac, sc, weapon=a.spawn)
        print(f'  {a.spawn} in rack slot {got}')

    # ⚠ ONE FRAME FOR THE PICTURE AND THE READING, and the first version of
    # this took three: ac.frame(), then survey()'s own grab, then an
    # ImageGrab for the crops. The Tab panel is TRANSLUCENT, so the backdrop
    # moves between grabs and the same tile scores differently -- which is how
    # the probe reported `readback: quick_smg` next to a table whose best
    # candidate was quickext_smg at an MSE above the empty gate. Those were
    # two screens, and the disagreement was manufactured by the probe.
    #
    # A tool built to catch "the record describes a different object than the
    # one measured" had the disease. It is that easy.
    with InventoryControl() as ac:
        with ac.tab_up():
            ac.park()          # a hovered tile draws a tooltip over itself
            from PIL import ImageGrab
            full = cv2.cvtColor(np.array(ImageGrab.grab()), cv2.COLOR_RGB2BGR)
            s = ac.survey('guns', 'slots')

    weapon = (s.get('guns') or {}).get(a.gun)
    print(f'\ngun{a.gun} name plate reads: {weapon!r}')

    from detector.attachment_detector import MSE_EMPTY_TH, MARGIN_MIN
    det = AttachmentDetector()
    tiles = []
    for name in slots:
        y, x, h, w = slot_tile_box(a.gun, name)
        tile = full[y:y + h, x:x + w]
        tiles.append((name, tile))
        cands = det.candidates(name, weapon)
        # read_tile's own answer ON THIS FRAME, then the verdict read_slots
        # would build from it. Reproduced here rather than read out of survey()
        # so the number and the picture describe one instant.
        win, mse, margin = det.read_tile(tile, name, weapon)
        if mse > MSE_EMPTY_TH:
            verdict = f'<empty>  (mse {mse:.1f} > gate {MSE_EMPTY_TH})'
        elif margin < MARGIN_MIN:
            verdict = f"'?'  ambiguous (margin {margin:.2f} < {MARGIN_MIN})"
        else:
            verdict = f'{by_asset.get(win, win)}  (mse {mse:.1f}, ' \
                      f'margin {margin:.2f})'
        # Every candidate scored, not just the winner: what a human wants to
        # see next to the picture is the RUNNER-UP, because a confident wrong
        # answer and a right one look identical until you know what it beat.
        scored = sorted((det.score(tile.astype(np.float32), n), n)
                        for n in cands)[:4]
        print(f'\n  {name:9} verdict: {verdict}')
        for m, n in scored:
            print(f'      {by_asset.get(n, n):22} mse {m:9.1f}')

    # One strip, zoomed, in slot order, so the tiles can be compared to each
    # other as well as to the names above.
    pad = 8
    hgt = max(t.shape[0] for _, t in tiles) * ZOOM + 34
    wid = sum(t.shape[1] * ZOOM + pad for _, t in tiles) + pad
    strip = np.zeros((hgt, wid, 3), np.uint8)
    cx = pad
    for name, t in tiles:
        big = cv2.resize(t, (t.shape[1] * ZOOM, t.shape[0] * ZOOM),
                         interpolation=cv2.INTER_NEAREST)
        strip[30:30 + big.shape[0], cx:cx + big.shape[1]] = big
        cv2.putText(strip, name, (cx, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 1, cv2.LINE_AA)
        cx += big.shape[1] + pad

    p_strip = os.path.join(OUT, f'slots_gun{a.gun}_{stamp}.png')
    p_full = os.path.join(OUT, f'slots_gun{a.gun}_{stamp}_full.png')
    cv2.imwrite(p_strip, strip)
    cv2.imwrite(p_full, cv2.resize(full, (full.shape[1] // 2,
                                          full.shape[0] // 2)))
    print(f'\n  tiles     -> {p_strip}')
    print(f'  full      -> {p_full}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
