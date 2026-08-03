"""Photograph a state so it can be argued about later, offline.

    from control.evidence import dump_state
    dump_state(where, 'kit read empty on both slots', ac=ac)

One function, and it drives the game only as far as opening the Tab screen and
closing it again. It fires nothing, drags nothing and spawns nothing: the
whole point is to record a state, and a dump that changed the state would be
recording its own footprints.

WHY THIS IS A MODULE AND NOT A PRINT STATEMENT
----------------------------------------------
A harvest run reported

    [!] scope should be red_dot, reads ''
    [!] magazine should be ext_ar, reads ''

and saved nothing. Those two lines have at least three causes — the part
never landed, the slot template drifted, or nothing was looking at the gun —
and they read identically. Every number that separates them (which rack slot
holds which weapon; what the slot row actually looks like) was on screen at
the moment it printed. Four minutes later the game had logged us off for
inactivity and the state was gone, so answering it needed a fresh session,
a re-spawn and a re-kit. On an unattended night that is the night.

THE FRAME IS WRITTEN FIRST, BEFORE ANYTHING IS PRESSED
------------------------------------------------------
Learned the hard way inside one minute: the first version photographed only
after Tab came up, and the very first real failure it met was `the Tab screen
would not open` — which is a sentence about the screen, written while
discarding the screen. So `before.png` lands unconditionally, and it is the
one that identified the inactivity dialog above.

WHAT A READING OF '' MEANS
--------------------------
Worth stating because it looks like every other empty slot: a gun out of the
spawner ARRIVES wearing a magazine (加长快速弹匣 — the game fits it, nobody
asks). So a magazine slot reading '' is not "the fit failed". It is "this is
not a gun" — an empty rack slot, or a read of the wrong one.
"""
import json
import os
import time
from datetime import datetime

import cv2
import numpy as np

from control.inventory import InventoryControl


def full_frame():
    """The whole screen, BGR. Deliberately not a region grab: the value of a
    dump is the parts nobody thought to capture."""
    from PIL import ImageGrab
    return cv2.cvtColor(np.array(ImageGrab.grab()), cv2.COLOR_RGB2BGR)


def dump_state(where, why='', ac=None, frames=None, state=None):
    """Write the evidence for one failure. -> (dir, note)

    ac      an InventoryControl that already has the foreground. Without one a
            fresh one is built, which costs the detector construction but lets
            a bare script call this.
    frames  extra {name: image} to save alongside — the region crops the
            caller already had in hand, which are the ones a template argument
            gets settled on.
    state   anything JSON-able to record beside the pictures; the harness
            passes the measurement record.

    Never raises for a game-state problem. A dump that failed with an
    exception would replace the evidence with a traceback about collecting
    evidence, so whatever went wrong is recorded in the note and the frames
    already taken are kept.
    """
    os.makedirs(where, exist_ok=True)
    own = ac is None
    ac = ac or InventoryControl(verbose=False)
    note = {'why': why, 'ts': datetime.now().isoformat(timespec='seconds')}
    try:
        cv2.imwrite(os.path.join(where, 'before.png'), full_frame())
        opened = ac.ensure_tab(True)
        note['tab_opened'] = bool(opened)
        if opened:
            time.sleep(0.4)
            cv2.imwrite(os.path.join(where, 'tab.png'), full_frame())
            # sync() before reading. read_slots() grabs its own frame, but the
            # gun NAMES come from a detection pass, and without one `guns` is
            # still {1: None, 2: None} from the constructor — which is
            # indistinguishable in the output from a genuinely empty rack.
            if ac.sync():
                note['guns'] = dict(ac.guns)
                note['slots'] = {g: ac.read_slots(g) for g in (1, 2)}
            else:
                note['sync'] = 'the Tab screen would not sync'
            ac.ensure_tab(False)
    except Exception as e:
        note['error'] = f'{type(e).__name__}: {e}'
    finally:
        if own:
            try:
                ac.close()
            except Exception:
                pass
    for name, img in (frames or {}).items():
        if img is not None:
            cv2.imwrite(os.path.join(where, f'{name}.png'), img)
    if state is not None:
        note['state'] = _jsonable(state)
    with open(os.path.join(where, 'state.json'), 'w', encoding='utf-8') as f:
        json.dump(note, f, ensure_ascii=False, indent=1, default=str)
    return where, note


def _jsonable(obj):
    """numpy scalars and arrays out, so one unserialisable field cannot cost
    the whole note. `default=str` on the dump covers the rest."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj
