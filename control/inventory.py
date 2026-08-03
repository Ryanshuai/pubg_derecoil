"""Move attachments around the Tab inventory screen, by dragging.

The hands to detector/tab_items.py's eyes. That module says what is on every
row and in every slot, and hands back the point to grab each item at; this one
turns those points into press-move-release gestures and reads the result back
to confirm they landed.

    from control.inventory import InventoryControl, at_ground, at_inv, at_slot

    ac = InventoryControl()
    ac.sync()                                # Tab open? game focused?
    view = ac.look()                         # a TabItemDetector TabView

    ac.equip(1, 'muzzle', view.find('comp_ar'))     # 库存/地面 -> gun 1
    ac.unequip(1, 'muzzle')                         # slot -> 库存
    ac.discard(view.find('uzi_stock'))              # -> the floor
    ac.stow(0)                                      # ground row 0 -> 库存

    # everything the gun can take, from both lists, in one call
    ac.build(2, weapon='m416')      # weapon= only when the plate will not read

    # or say what the gun should be WEARING and let it work out the moves
    ac.ensure_kit(2, {'muzzle': 'comp_ar', 'grip': None, 'stock': None})

    ac.transfer(1, 2)               # gun 1's parts onto gun 2
    ac.clear_ground()               # the floor into the backpack
    ac.drop_weapon(1)               # the whole gun out, wearing its parts

WHICH MOVES EXIST: see the MOVES table below. It used to be this paragraph,
which meant nothing could check it and nothing could read it. Now `_reject`
gates on it, and a caller composing a flow can ask:

    move_info(at_inv(0), at_slot(1, 'muzzle'))
    -> {'gesture': 'click', 'verified': True, 'evidence': 'measured', ...}

`evidence` is on every entry on purpose — 'measured' / 'used' / 'untested'
are not the same claim, and a table that cannot tell them apart is how
attachment_catalog shipped 6 guesses indistinguishable from 22 readings.

WHAT EVERY METHOD RETURNS: one of exactly two shapes, STEP or BATCH. See
step() and batch(). There were five before 2026-08-03 — a dict, a bare list,
a 2-tuple, another list, and ensure_kit's dict — for one idea.

Every one of those is the same primitive, `drag(src, dst)`. A location is any
of these, and an `Item` straight out of a TabView is accepted wherever one is:

    at_ground(i)     row i of 附近 / 地面        ('nearby', i)
    at_inv(i)        row i of 库存               ('inventory', i)
    at_slot(g, s)    attachment slot s of gun g  ('weapon', g, s)

A panel location with no row — at_ground(), at_inv() — means "into this
panel, anywhere", which is what makes dropping to the floor and stowing into
the backpack the same call as everything else. Those release at
tab_layout.DROP_XY, which was measured off the screen rather than derived from
a row: releasing on a row put the part on the FLOOR instead of in the pack,
and drag() could not tell because it only ever re-reads the SOURCE.

ROWS MOVE UNDER YOU
    A row index is only valid for the detection pass it came from. Pulling row
    i out shifts every row below it up by one, and an attachment displaced by a
    swap lands back in 库存 as a *new* row. So build()/run_plan() re-detect
    before each drag by default and re-find the attachment by name; passing
    redetect=None turns that off and falls back to plan_equip()'s
    descending-row ordering, which survives removals but not insertions.

VERIFICATION
    Anything with a weapon slot at either end is checked by reading that slot
    back: the target must hold the named item — or, when the item has no icon
    template, must at least have *changed* — and a slot dragged *from* must
    end up empty. Panel-to-panel drags cannot be checked here at all:
    rec['verified'] is False for those, and it is on the caller to re-detect.

    A failed drag is retried only when nothing changed, which is the one case
    where the source row is provably still where it was. If the slot changed
    into something unexpected, the retry is skipped and the record says so.
"""
import argparse
import contextlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import HUD_REGIONS
from detector.attachment_catalog import ATTACHMENTS, ROSTER, fits, has_slot, is_live
from detector.attachment_detector import SLOT_NAMES
from detector.cropper import win32_cap
from detector.tab_detector import TabTypeDetector
from detector.tab_items import TabGrabber, TabItemDetector
from detector.tab_layout import (DROP_XY, INV_ROWS, PARK_XY, att_slot_point,
                                 gun_tag_point, row_point)
from detector.weapon_template_detector import TabWeaponDetector
from press.pico_mouse import HID_KEY_1, HID_KEY_2, HID_KEY_TAB
from press.pointer import Pointer
from control.focus import game_focused, ensure_focus

PANEL_KINDS = ('nearby', 'inventory')
GUNS = (1, 2)

# Verification targets.
EMPTY = ''          # the slot must read as nothing
ANY_ITEM = '*'      # the slot must read as something, no matter what

# ════════════════════════════════════════════════════════════
# MOVES — which src -> dst pairs exist, and what is KNOWN about each
# ════════════════════════════════════════════════════════════
#
# This was prose in the module docstring, which meant nothing could check it
# and nothing could read it. Now `_reject` gates on it and another agent
# composing a flow can look the answer up:
#
#     from control.inventory import MOVES, kind_of, at_slot, at_inv
#     MOVES[(kind_of(at_inv(0)), kind_of(at_slot(1, 'muzzle')))]
#     -> {'gesture': 'click', 'verified': True, 'evidence': 'measured', ...}
#
# EVERY ENTRY CARRIES `evidence`, AND THAT IS THE POINT. attachment_catalog's
# SLOTS table shipped as 22 wiki readings, 6 guesses and 2 screenshot reads
# with 0 measured, all indistinguishable from each other, and it cost two
# entries that silently dropped attachments on the floor. A capability table
# that cannot say how it knows repeats that.
#
#     'measured'  a probe ran it and the numbers are in docs/game_quirks.md
#     'used'      no dedicated probe, but calibration runs take this path
#                 constantly and would fail loudly if it did not work
#     'untested'  believed to exist, never confirmed. transfer() refuses to
#                 default to one of these.
#
# `gesture` is the one that LANDS, which is not always the obvious one — see
# the 0/4 below. `verified` is whether *this module* can confirm the outcome
# by re-reading; a panel-to-panel move has no slot to read, so it cannot.
MOVES = {
    ('inventory', 'weapon'): {
        'gesture': 'click', 'verified': True, 'evidence': 'measured',
        'note': 'RIGHT-CLICK, not drag. The drag measured 0/4 — it does not '
                'land at all — while right-click is 4/4 at 0.35 s. It equips '
                'onto the gun IN HAND, so hold(gun) first.'},
    ('nearby', 'weapon'): {
        'gesture': 'click', 'verified': True, 'evidence': 'used',
        'note': 'same gesture as from 库存; build() fits off the ground this '
                'way on every range entry.'},
    ('weapon', 'inventory'): {
        'gesture': 'drag', 'verified': True, 'evidence': 'measured',
        'note': 'the direction that DOES drag. Right-click on a fitted part '
                'also sends it to the pack (measured 2026-08-02), which is '
                'what unequip(gesture="click") uses.'},
    ('weapon', 'nearby'): {
        'gesture': 'drag', 'verified': True, 'evidence': 'used',
        'note': 'strip(to=at_ground()) — a part straight from the slot to '
                'the floor, skipping the pack.'},
    ('gun', 'nearby'): {
        'gesture': 'click', 'verified': True, 'evidence': 'measured',
        'carries_attachments': True,
        'note': 'the whole weapon, WEARING its parts: 0.66 s by right-click '
                'vs 1.15 s by a 1621 px drag, both 1/1. Two runs confirmed '
                'rack empty, 库存 zero growth, ground +1 row. Stripping '
                'first is worse — PUBG auto-fits the pack onto the next gun '
                'to arrive, which is how a cell labelled BARE ran wearing a '
                'grip and a quickdraw magazine.'},
    ('nearby', 'inventory'): {
        'gesture': 'drag', 'verified': False, 'evidence': 'used',
        'note': 'stow(). Nothing here can confirm it — see `verified`.'},
    ('inventory', 'nearby'): {
        'gesture': 'drag', 'verified': False, 'evidence': 'used',
        'note': 'discard(). stock.tidy() drops the surplus this way and '
                'confirms by re-reading the whole panel, which is the '
                'caller-side check `verified: False` is asking for.'},
    ('weapon', 'weapon'): {
        'gesture': 'drag', 'verified': True, 'evidence': 'untested',
        'note': 'slot to slot, including gun 1 -> gun 2. The module '
                'docstring has always advertised it and nothing has ever '
                'measured it — and the neighbouring fact is discouraging: a '
                'drag INTO a weapon slot from 库存 is 0/4. If that failure '
                'is about the drop target rather than the source, this does '
                'not work either. transfer() therefore does NOT default to '
                'it; tools/probe_transfer.py is what would settle it.'},
}


def kind_of(loc):
    """The MOVES key for a location tuple. ('weapon', 1, 'muzzle') -> 'weapon'"""
    return loc[0] if isinstance(loc, (tuple, list)) and loc else None


def move_info(src, dst):
    """What is known about dragging src -> dst, or None if it is not a move."""
    return MOVES.get((kind_of(src), kind_of(dst)))


# ════════════════════════════════════════════════════════════
# The two record shapes, and nothing else
# ════════════════════════════════════════════════════════════
#
# Every method here returns one of exactly two things, so a caller composing
# them does not have to remember which. It used to have to: `drag`/`equip`/
# `unequip`/`discard`/`stow` returned a dict, `strip` a bare list, `build` a
# 2-tuple, `clear_rack` another list, and `ensure_kit` a dict with a different
# spelling again. Five shapes for one idea.
#
#   STEP   one gesture:  {ok, verified, src, dst, checks, attempts, error}
#   BATCH  several:      {ok, steps: [STEP, ...], error, ...domain extras}
#
# `ok` on a batch is every step ok AND no batch-level error, which is not the
# same as `all(s['ok'])`: a batch that never got as far as its first step has
# no failing step to point at. ensure_kit already worked this way, so this is
# that shape given a name rather than a new one.

STEP_KEYS = ('ok', 'verified', 'src', 'dst', 'checks', 'attempts', 'error')


def step(src=None, dst=None, ok=False, verified=False, error=None, **extra):
    """One gesture's record. Extras are allowed; the seven keys are not."""
    rec = {'ok': bool(ok), 'verified': bool(verified), 'src': src, 'dst': dst,
           'checks': [], 'attempts': 0, 'error': error}
    rec.update(extra)
    return rec


def batch(steps, error=None, ok=None, **extra):
    """Several gestures' record. `ok` needs every step AND no batch error.

    Pass `ok` to override with a STRONGER verdict, which is the one legitimate
    deviation and worth naming: ensure_kit decides ok by READING THE GUN BACK,
    not by whether its steps reported success. Those are not the same claim —
    a swap can report a clean drag onto a slot that ended up holding something
    else, and every step ok is exactly the case where nobody would look again.
    A weaker override is never right; if the steps failed, the batch failed.
    """
    steps = list(steps)
    rec = {'ok': (error is None and all(s.get('ok') for s in steps))
                 if ok is None else bool(ok),
           'steps': steps, 'error': error}
    rec.update(extra)
    return rec

# Dropping is verified by polling, so the drag itself does not also sit out a
# fixed settle after releasing the button — DROP_WAIT below is what used to be
# press/pointer.py's DRAG_DROP_WAIT of 0.25 s on every single drag, spent
# before the first look rather than instead of it. The window is unchanged:
# VERIFY_TIMEOUT absorbed it.
DROP_WAIT = 0.0

# Gesture timing handed to Pointer.drag. Defaults are press/pointer.py's, i.e.
# what shipped before anyone measured them; probe_drag_speed.py is what says
# whether they can come down.
DRAG_TIMING = {'drop': DROP_WAIT}

# Tab is a toggle and swallows 1/2 while it is up, so hold() has to close it,
# switch, and reopen. That used to be two fixed 0.45 s sleeps.
#
# Measured 2026-08-02 (tools/probe_toggle_latency.py, 8 cycles): the weapon
# panel is fully readable 33-38 ms after the key, and the 类型 anchor is gone
# 77-128 ms after it. So 0.45 was 4-13x what the game needs, and both waits
# are now polls -- tab_open() costs 3-6 ms a pass, which is cheaper than
# oversleeping by a single frame.
#
# Worth knowing when reading these: open and "done drawing" came out IDENTICAL
# in all 8 cycles. The weapon block does not draw progressively, so there is
# nothing to wait out past the anchor. (The spawner is the opposite case; see
# _recalibrate.)
TAB_TOGGLE_TIMEOUT = 0.5    # per press: 4x the slowest close, 13x the slowest
                            # open. Short on purpose -- ensure_tab re-presses,
                            # and a swallowed key is not cured by waiting.


VERIFY_TIMEOUT = 1.05   # the item animates into the slot; polling beats one
                        # fixed sleep long enough to cover the worst case
VERIFY_POLL = 0.08
PARK_SETTLE = 0.06      # cursor off the slot -> tooltip gone, before a read

# Releasing over a panel means "put it in this container", and WHERE inside it
# is not a row -- see tab_layout.DROP_XY, measured by holding a drag until the
# game drew its dashed accept-region. This constant survives only because
# set_rows()/DEFAULT_DROP_ROW is still the story for pick-UP points; the guess
# it used to feed (row 0, or the first empty row) is what dropped parts on the
# floor while reporting success.
DEFAULT_DROP_ROW = 0

# A dropped weapon leaves the rack over a short animation; the plate is read
# back after this. Generous, because reading it too early reports the drop as
# a failure and the caller then drops it a second time.
DROP_SETTLE = 0.5

# ensure_kit's final readback waits this long after the last step. Every step
# it ran was already polled to a verified state, so this is only covering the
# tail of the last one's icon animation -- it is harvest.KIT_SETTLE_S, kept at
# the value that run has been using, and it is the one number here nobody has
# measured.
KIT_SETTLE = 0.6


# ════════════════════════════════════════════════════════════
# Locations
# ════════════════════════════════════════════════════════════

def at_ground(row=None):
    """Row `row` of 附近 / 地面, or the panel itself when row is None."""
    return ('nearby', row)


def at_inv(row=None):
    """Row `row` of 库存, or the panel itself when row is None."""
    return ('inventory', row)


def at_slot(gun, slot):
    """Attachment slot `slot` of weapon `gun` (1 = top / key 1, 2 = bottom).

    Spelled ('weapon', ...) rather than ('slot', ...) because that is what
    TabItemDetector already stamps on every Item it finds in a gun — one
    vocabulary, so an Item can be handed straight back as a drag source.
    """
    return ('weapon', gun, slot)


def at_gun(gun):
    """The WEAPON itself in rack slot `gun`, not one of its attachment slots.

    Spelled ('gun', n) so it cannot be confused with ('weapon', n, slot),
    which is an attachment slot on that gun.

    The drag point is the boxed slot number at the left end of the row --
    tab_layout.gun_tag_point, measured off docs/tab_inventory.png. That is the
    handle for the weapon itself; the name plate beside it and the attachment
    tiles below it are not.
    """
    return ('gun', gun)


def is_gun(loc):
    return isinstance(loc, tuple) and len(loc) == 2 and loc[0] == 'gun'


def as_loc(x):
    """A location tuple out of either a location tuple or a TabView Item."""
    where = getattr(x, 'where', None)
    return where if where is not None else x


def is_slot(loc):
    return loc[0] == 'weapon'


def loc_str(loc):
    loc = as_loc(loc)
    if is_gun(loc):
        return f'gun{loc[1]}'           # the weapon, vs gun1.muzzle for a slot
    if is_slot(loc):
        return f'gun{loc[1]}.{loc[2]}'
    return f'{loc[0]}' + ('' if loc[1] is None else f'[{loc[1]}]')


def parse_loc(text):
    """Location tuple from CLI text. -> at_inv / at_ground / at_slot / at_gun

        inv:3          库存 row 3          ground / ground:0   附近
        slot:1:muzzle  gun 1's muzzle      gun:1               gun 1 ITSELF

    `gun:1` and `gun:1:muzzle` mean different things -- the whole weapon
    against one of its slots -- which is why at_gun is spelled ('gun', n) and
    a slot ('weapon', n, slot). The three-part form is kept for both spellings
    because it was already accepted.
    """
    parts = text.split(':')
    kind = parts[0].lower()
    if kind in ('slot', 'gun', 'weapon'):
        if len(parts) == 2 and kind in ('gun', 'weapon'):
            return at_gun(int(parts[1]))
        if len(parts) != 3:
            raise ValueError(f'{text!r}: expected slot:<gun>:<slot> for a '
                             f'slot, or gun:<n> for the weapon itself')
        return at_slot(int(parts[1]), parts[2])
    if kind in ('inv', 'inventory'):
        return at_inv(int(parts[1]) if len(parts) > 1 else None)
    if kind in ('ground', 'nearby', 'floor'):
        return at_ground(int(parts[1]) if len(parts) > 1 else None)
    raise ValueError(f'{text!r}: expected inv[:row], ground[:row], '
                     f'slot:<gun>:<slot> or gun:<n>')


# ════════════════════════════════════════════════════════════
# Planning — pure, no game needed
# ════════════════════════════════════════════════════════════

def loose_items(found):
    """{loc: att_key} for everything sitting in the two lists.

    Accepts a TabView or an already-flattened mapping, so a caller can plan
    straight off a detection pass or hand-build one for a test.
    """
    if not hasattr(found, 'inventory'):
        return dict(found)
    return {item.where: item.key
            for panel in ('inventory', 'nearby')
            for item in getattr(found, panel)
            if item is not None and item.key}


def plan_equip(weapon, found, current=None, replace=False):
    """Turn one detection pass into an ordered list of drags.

    weapon   ROSTER key of the gun being built, or None to skip the
             compatibility gate (then every found attachment is attempted)
    found    a TabView, or {loc: att_key} — what is loose in the two left
             panels, e.g. {('inventory', 3): 'comp_ar', ('nearby', 0): 'vert_grip'}
    current  {slot: template_name} of what the gun already wears. An occupied
             slot is left alone unless replace=True.
    replace  drag onto occupied slots too; the game swaps, and the old
             attachment lands in 库存 as a new row

    Returns (drags, skipped):
        drags    [{'att', 'src', 'slot'}, ...], sorted by source row
                 descending within each panel so that pulling one row out
                 cannot invalidate the rows of the drags still queued
        skipped  [(att, loc, reason), ...] — every candidate that was dropped,
                 so a caller can print why an attachment went unused
    """
    current = current or {}
    drags, skipped, claimed = [], [], set()

    for loc, att in loose_items(found).items():
        spec = ATTACHMENTS.get(att)
        if spec is None:
            skipped.append((att, loc, 'not in the attachment catalogue'))
            continue
        slot = spec['slot']
        if weapon is not None and not fits(weapon, att):
            reason = ('weapon has no {} slot'.format(slot)
                      if not has_slot(weapon, slot)
                      else f'{weapon} does not take {att}')
            skipped.append((att, loc, reason))
            continue
        if slot in claimed:
            skipped.append((att, loc, f'{slot} already claimed by an earlier '
                                      f'candidate'))
            continue
        if current.get(slot) and not replace:
            skipped.append((att, loc, f'{slot} already holds '
                                      f'{current[slot]}'))
            continue
        claimed.add(slot)
        drags.append({'att': att, 'src': loc, 'slot': slot})

    # Descending row order per panel: removing row i only shifts rows > i.
    def key(d):
        src = d['src']
        row = -1 if is_slot(src) or src[1] is None else src[1]
        return (src[0], -row)

    drags.sort(key=key)
    return drags, skipped


# ── The kit: what a gun should be wearing ──

# Where a part is preferred from when both lists have one. 库存 first, which is
# what TabView.find() already answers with, and it is also the cheaper source:
# a swap displaces the old part back into the panel the new one came from, so
# equipping out of 库存 keeps the floor clean.
_PANEL_RANK = {'inventory': 0, 'nearby': 1}


def _src_rank(loc):
    row = loc[1] if len(loc) > 1 and loc[1] is not None else 0
    return (_PANEL_RANK.get(loc[0], 2), row)


def slot_matches(readback, key):
    """Does a slot readback name attachment `key`?

    Two vocabularies meet here. read_slots() answers in AttachmentDetector
    template stems (Muzzle_Compensator_Large_C) and every caller speaks
    catalogue keys (comp_ar). ATTACHMENTS[key]['asset'] is the catalogue's own
    bridge between them and is what this trusts first.

    The substring fallbacks are not sloppiness, they are the template bank
    being wider than the catalogue: 'laser' is catalogued as
    Lower_LaserPointer_C and the bank ships SideRail_LaserPointer_C, so an
    exact comparison reads a fitted laser as "not a laser" and the kitter
    takes it off and puts it back on forever.

    It stays a *narrow* fallback on purpose. The pair this must never confuse
    is 扩容弹匣 against 加长快速弹匣 — weapon_axis swaps one for the other and
    calls the difference a measurement — and neither name contains the other.
    """
    if not readback:
        return False
    r = str(readback).lower()
    asset = (ATTACHMENTS.get(key) or {}).get('asset') or ''
    if asset:
        a = asset.lower()
        if r == a or a in r or r in a:
            return True
    return bool(key) and str(key).lower() in r


def _slot_order(want):
    """want's slots, in SLOT_NAMES order, with anything unrecognised last."""
    return ([s for s in SLOT_NAMES if s in want]
            + [s for s in want if s not in SLOT_NAMES])


def _kit_refuse(weapon, slot, key):
    """Why `key` cannot go in `slot` of `weapon`, or None.

    Emptying a slot is never refused: a slot the weapon does not have reads
    exactly like one that is drawn empty, so "must be empty" is already true
    there and asking for it costs nothing.
    """
    if slot not in SLOT_NAMES:
        return f'{slot!r} is not one of {SLOT_NAMES}'
    if key is None:
        return None
    spec = ATTACHMENTS.get(key)
    if spec is None:
        return f'unknown attachment {key!r}'
    if spec['slot'] != slot:
        return f'{key} is a {spec["slot"]}, not a {slot}'
    if weapon is not None:
        if weapon not in ROSTER:
            return f'unknown weapon {weapon!r}'
        if not has_slot(weapon, slot):
            return f'{weapon} has no {slot} slot'
        if not fits(weapon, key):
            return f'{weapon} does not take {key}'
    return None


def _kit_step(action, slot, key, src, was, error):
    return {'action': action, 'slot': slot, 'key': key, 'src': src,
            'was': was, 'error': error}


def plan_kit(want, worn, found=None, weapon=None):
    """The shortest way from `worn` to `want`. Pure — no game, no screen.

    want    {slot: att_key or None}. None means the slot must END UP EMPTY,
            which is not the same as leaving it alone: PUBG auto-fits whatever
            the backpack holds onto a gun the moment it arrives, so a slot
            nobody named is not empty, it is whatever the last strip left
            lying around. A run labelled BARE came back wearing a cheek pad,
            and a cheek pad reduces recoil. A slot ABSENT from `want` is
            explicitly unmanaged and this will not touch or check it.
    worn    {slot: template name} as read_slots() gives it, '' for empty
    found   what is loose in the two panels: a TabView, or {loc: att_key}.
            None means "do not check availability" — every equip is planned
            with src=None and the executor re-finds the part by name.
    weapon  ROSTER key, for the catalogue gate. Without one a drag can be
            planned onto a slot the weapon does not have, and a part released
            over a slot that is not drawn goes on the floor.

    Returns {'ok', 'steps', 'unchanged', 'missing', 'error'}:

        steps      [{'action': 'unequip'|'equip', 'slot', 'key', 'src',
                     'was', 'error'}, ...] — removals first, then fits, each
                   in SLOT_NAMES order. A step carrying an `error` is NOT
                   executable and is in the list so that the impossible slot
                   is reported rather than silently dropped.
        unchanged  slots already correct. These are never touched, which is
                   the whole point of asking before acting.
        missing    keys that are wanted, legal, and nowhere on screen
        ok         no step carries an error

    ONE ACTION PER WRONG SLOT is what "shortest" means here. A part dropped on
    an occupied slot swaps, and the displaced one goes back to the panel the
    new one came from (docs/game_quirks.md), so a replacement is one step and
    not an unequip followed by an equip. Removals go first because they are
    the steps that cannot fail for want of a part: if a fit later turns out to
    be impossible, the gun is at least in the state its `None`s asked for
    rather than half of two configurations.
    """
    loose = None if found is None else loose_items(found)
    steps, unchanged, missing = [], [], []

    for slot in _slot_order(want):
        key = want[slot]
        cur = (worn or {}).get(slot, '') or ''
        err = _kit_refuse(weapon, slot, key)
        if err:
            steps.append(_kit_step('equip' if key is not None else 'unequip',
                                   slot, key, None, cur, err))
            continue
        if key is None:
            if cur:
                steps.append(_kit_step('unequip', slot, None, None, cur, None))
            else:
                unchanged.append(slot)
            continue
        if slot_matches(cur, key):
            unchanged.append(slot)
            continue
        src = None
        if loose is not None:
            hits = sorted((loc for loc, k in loose.items() if k == key),
                          key=_src_rank)
            if not hits:
                missing.append(key)
                steps.append(_kit_step('equip', slot, key, None, cur,
                                       'not on screen'))
                continue
            src = hits[0]
        steps.append(_kit_step('equip', slot, key, src, cur, None))

    # Stable, so slots keep SLOT_NAMES order inside each half.
    steps.sort(key=lambda s: 0 if s['action'] == 'unequip' else 1)
    bad = [f'{s["slot"]}: {s["error"]}' for s in steps if s['error']]
    return {'ok': not bad, 'steps': steps, 'unchanged': unchanged,
            'missing': missing, 'error': '; '.join(bad) or None}


def kit_faults(want, worn):
    """Slots whose readback disagrees with what was asked for. [] is clean.

    -> [{'slot', 'key', 'why', 'verifiable'}, ...]

    `verifiable` is False when the wanted part has no icon template
    (brake_ar, heavy_stock, variable): the slot cannot be read as holding it,
    only as holding *something*, so the fault means "cannot be proven" rather
    than "is wrong". Both are reasons not to record a measurement, but only
    one of them is a reason to go looking for a failed drag.
    """
    out = []
    for slot in _slot_order(want):
        key = want[slot]
        cur = (worn or {}).get(slot, '') or ''
        if key is None:
            if cur:
                out.append({'slot': slot, 'key': None, 'verifiable': True,
                            'why': f'reads {cur!r}, should be empty'})
            continue
        if slot_matches(cur, key):
            continue
        spec = ATTACHMENTS.get(key) or {}
        if spec.get('asset'):
            out.append({'slot': slot, 'key': key, 'verifiable': True,
                        'why': f'reads {cur!r}'})
        else:
            out.append({'slot': slot, 'key': key, 'verifiable': False,
                        'why': f'{key} has no icon template; slot reads '
                               f'{cur!r}'})
    return out


# ════════════════════════════════════════════════════════════
# Control
# ════════════════════════════════════════════════════════════

class InventoryControl:
    """Drag attachments between the ground, the backpack and the two guns."""

    def __init__(self, backend='auto', verbose=True):
        self.pointer = Pointer(backend)
        self.items = TabItemDetector()
        self.grabber = TabGrabber()
        self.tab = TabTypeDetector()          # device=None: pixel check only
        self.ocr = TabWeaponDetector()
        self.verbose = verbose
        self.rows = {'nearby': None, 'inventory': None}
        self.guns = {1: None, 2: None}        # catalog key per weapon slot
        self.held = None                      # weapon in hand, or None if unknown
        # Overrides passed straight to Pointer.drag — the gesture's timing.
        # Every calibration run reaches the Tab screen through here, so these
        # are worth measuring rather than guessing; tools/probe_drag_speed.py
        # sweeps them and reports the fastest setting that still lands.
        self.timing = dict(DRAG_TIMING)

    def _log(self, msg):
        if self.verbose:
            print(f'[attach] {msg}', flush=True)

    def close(self):
        """Release the GDI objects TabGrabber holds open."""
        self.grabber.close()

    def can_press(self):
        """Is there a Pico, i.e. can this send KEYS as well as clicks?

        Tab, 1 and 2 are keypresses and SendInput has no key path, so without
        one this can drag but never open the screen to drag on. Checkable up
        front rather than four minutes in. Same method, same reason, as
        SpawnerControl.can_press.
        """
        return self.pointer.pico is not None

    # ── Screen state ──

    def set_rows(self, nearby=None, inventory=None):
        """Override how many rows each panel is showing.

        Only used to pick the drop point for a panel target: with a row count
        the drop lands on the first empty row instead of on top of an existing
        item. look() sets this from what it saw, so calling it by hand is only
        for driving a drag without a detection pass.
        """
        if nearby is not None:
            self.rows['nearby'] = int(nearby)
        if inventory is not None:
            self.rows['inventory'] = int(inventory)

    def tab_open(self):
        return bool(self.tab.classify({'type': win32_cap(HUD_REGIONS['type'])}))

    @contextlib.contextmanager
    def tab_up(self, restore=True):
        """Have the Tab screen up for the block, then leave it as it was found.

        The old way to be sure of the state was to force a known cycle: close
        it if it was open, open it, read, close it. Three keypresses, and with
        the sleeps those carried, 1.25 s to look at something that was very
        often already on screen -- auto_calibrate.detect_attachments did
        exactly that, and sweep.read_loadout still does.

        There is nothing to force. tab_open() answers the same question in
        3-6 ms, so this opens the screen only if it is shut and undoes only
        what it did. Already open is the free case, which is what makes it
        safe to wrap every read in.

        Yields True when the screen is up. A False must NOT be read as
        "nothing equipped": an empty slot is a legitimate answer, and the two
        would be indistinguishable downstream.
        """
        was_open = bool(self.tab_open())
        ok = was_open or self.ensure_tab(True)
        if not ok:
            self._log('Tab would not open')
        try:
            yield ok
        finally:
            if ok and restore and not was_open:
                self.ensure_tab(False)

    def loadout(self, gun=None):
        """What the guns are, and what they are wearing. Opens Tab if needed.

        -> {'guns': {1: key, 2: key}, 'slots': {1: {slot: asset}, 2: {...}}},
        or None if the screen never came up. With `gun`, just that one's dict.

        One screen block covers all of it -- both name plates and all ten
        slots are tab_blocks()['right'] -- so this is one 7 ms grab plus the
        detectors, whatever state the screen started in.
        """
        with self.tab_up() as ok:
            if not ok:
                return None
            frame = self._frame()
            self.guns = self._read_guns(frame)
            slots = self._slot_states(frame)
        out = {'guns': self.guns, 'slots': slots}
        return out if gun is None else {'gun': self.guns[gun],
                                        'slots': slots[gun]}

    def sync(self):
        """False unless the game is focused with the Tab screen up."""
        if not game_focused():
            self._log('game is not the foreground window')
            return False
        self.park()
        if not self.tab_open():
            self._log('Tab inventory is not open')
            return False
        return True

    def park(self):
        """Move the cursor off every interactive element, then let the hover
        highlight and any tooltip fade before a read.

        A no-op when the cursor is already parked, so polling a slot does not
        pay the settle time on every pass.
        """
        if self.pointer.cursor_pos() == PARK_XY:
            return
        self.pointer.move_to(*PARK_XY)
        time.sleep(PARK_SETTLE)

    def look(self):
        """Grab the Tab screen and read it. Returns a TabView.

        Also caches what it learned: which weapon is in each slot (so slot
        reads can narrow their template bank) and how many rows each list is
        showing (so a drop into a panel lands past the end of it).
        """
        frame = self._frame()
        self.guns = self._read_guns(frame)
        view = self.items.detect(frame, self.guns)
        self.set_rows(nearby=view.rows('nearby'),
                      inventory=view.rows('inventory'))
        return view

    def read_weapons(self):
        """{1: key, 2: key} off the two name plates; None where unmatched."""
        return self._read_guns(self._frame())

    def read_slots(self, gun=None):
        """What the guns are wearing, as template names ('' when empty).

        gun=None -> {1: {slot: name}, 2: {slot: name}}; gun=1|2 -> {slot: name}.
        """
        out = self._slot_states(self._frame())
        return out if gun is None else out[gun]

    # ── The primitive ──

    def drag(self, src, dst, want=None, retries=1, weapon=None, verify=True):
        """Drag whatever is at `src` onto `dst`.

        want     what the destination slot should read as afterwards. Defaults
                 to ANY_ITEM when dst is a slot; ignored when it is a panel.
        weapon   ROSTER key of the gun `dst` belongs to. Given one, a drag
                 onto a slot that weapon does not have is refused before the
                 mouse moves — an attachment released over a slot that is not
                 drawn goes back where it came from, or onto the floor.
        verify   False to send the gesture and read nothing back. Exists for
                 exactly one caller — calibration/collect_templates.py, which
                 is fitting a part in order to PHOTOGRAPH it, and whose slot
                 has no template yet by definition. Reading it back would
                 refuse every new attachment for lacking the very thing the
                 run exists to produce. It confirms with pixel change and the
                 inventory row count instead, neither of which needs a
                 template. Do not reach for this anywhere else: `_reject` and
                 the address vocabulary still apply, and skipping them is what
                 made every drop_weapon fail silently for a year.

        Returns a record:
            {'ok', 'verified', 'src', 'dst', 'checks', 'attempts', 'error'}
        ok is True when the gesture went through *and* every check that could
        be made passed. verified is False when nothing could be checked, which
        is every panel-to-panel drag.

        `src` and `dst` may be TabView Items as well as location tuples.
        """
        src, dst = as_loc(src), as_loc(dst)
        err = self._reject(src, dst, weapon)
        if err:
            self._log(f'{loc_str(src)} -> {loc_str(dst)}: refused, {err}')
            return {'ok': False, 'verified': False, 'src': src, 'dst': dst,
                    'checks': [], 'attempts': 0, 'error': err}

        checks = []
        if verify and is_slot(dst):
            checks.append((dst[1], dst[2], want or ANY_ITEM))
        if verify and is_slot(src):
            checks.append((src[1], src[2], EMPTY))
        # The pre-drag reading is needed twice: ANY_ITEM on a slot that was
        # already occupied would otherwise pass without the drag doing
        # anything, and a retry is only safe while nothing has changed.
        before = self._slot_states(self._frame()) if checks else None

        rec = {'ok': False, 'verified': bool(checks), 'src': src, 'dst': dst,
               'checks': [], 'attempts': 0, 'error': None}
        p0, p1 = self.point_of(src), self.point_of(dst)

        for attempt in range(retries + 1):
            rec['attempts'] = attempt + 1
            if not self.pointer.drag(p0, p1, **self.timing):
                rec['error'] = 'cursor placement failed'
                return rec
            if not checks:
                # Nothing on the right-hand side to read back. The gesture is
                # all this module can honestly report on.
                rec['ok'] = True
                self._log(f'{loc_str(src)} -> {loc_str(dst)}: dragged '
                          f'(unverified)')
                return rec

            results = self._await(checks, before)
            rec['checks'] = [{'gun': g, 'slot': s, 'want': w, 'seen': seen,
                              'ok': ok} for g, s, w, ok, seen in results]
            if all(r['ok'] for r in rec['checks']):
                rec['ok'] = True
                self._log(f'{loc_str(src)} -> {loc_str(dst)}: ok '
                          f'({self._checks_str(rec["checks"])})')
                return rec

            if attempt >= retries:
                break
            # Retrying is safe only if the screen is exactly as it was: then
            # the item never left, so the source row is still the source row.
            moved = [(g, s) for g, s, _, _ok, seen in results
                     if seen != before[g][s]]
            if moved:
                rec['error'] = ('drag had an effect but not the expected one; '
                                'not retrying, re-detect first')
                self._log(f'{loc_str(src)} -> {loc_str(dst)}: '
                          f'{rec["error"]} ({self._checks_str(rec["checks"])})')
                return rec
            self._log(f'{loc_str(src)} -> {loc_str(dst)}: nothing changed, '
                      f'retry {attempt + 2}/{retries + 1}')

        rec['error'] = 'verification failed'
        self._log(f'{loc_str(src)} -> {loc_str(dst)}: failed '
                  f'({self._checks_str(rec["checks"])})')
        return rec

    # ── The four directions, named ──

    def equip(self, gun, slot=None, src=None, att=None, weapon=None, retries=1,
              gesture='auto'):
        """Put the attachment at `src` into weapon `gun`'s `slot`.

        Hand it a TabView Item and everything but the gun is implied:

            ac.equip(1, view.find('comp_ar'))

        att is a catalogue key ('comp_ar'). Given one — or inferred from an
        Item — the slot is verified against that exact template rather than
        "occupied by anything", which is the only way to tell a successful
        swap from a no-op when the slot was already full.

        gesture: 'auto' right-clicks when `gun` is already in hand and drags
        otherwise; 'click' and 'drag' force one. Right-click is 5x faster and
        was the only one of the two that landed at all when measured — hold()
        first and the fast path applies. See docs/game_quirks.md.
        """
        if src is None:                 # equip(gun, item) shorthand
            src, slot = slot, None
        if att is None:
            att = getattr(src, 'key', None)
        if slot is None:
            slot = getattr(src, 'slot', None) or (ATTACHMENTS[att]['slot']
                                                  if att in ATTACHMENTS else None)
        if weapon is None:
            weapon = self.guns.get(gun)
            if weapon is None:
                self._log(f'gun{gun} is unnamed: dragging without the '
                          f'catalogue check that the slot exists')
        if slot is None:
            return {'ok': False, 'verified': False, 'src': as_loc(src),
                    'dst': None, 'checks': [], 'attempts': 0,
                    'error': 'no target slot given, and none could be inferred'}

        want = ANY_ITEM
        if att:
            spec = ATTACHMENTS.get(att)
            if spec is None:
                return {'ok': False, 'verified': False, 'src': as_loc(src),
                        'dst': at_slot(gun, slot), 'checks': [], 'attempts': 0,
                        'error': f'unknown attachment {att!r}'}
            if spec['slot'] != slot:
                return {'ok': False, 'verified': False, 'src': as_loc(src),
                        'dst': at_slot(gun, slot), 'checks': [], 'attempts': 0,
                        'error': f'{att} is a {spec["slot"]}, not a {slot}'}
            want = spec['asset'] or ANY_ITEM
            if want == ANY_ITEM:
                self._log(f'{att} has no icon template: gun{gun}.{slot} can '
                          f'only be checked for having changed, not for '
                          f'holding {att}')
        if gesture == 'auto':
            gesture = 'click' if self.held == gun else 'drag'
        if gesture == 'click':
            return self.right_click_equip(gun, slot, src, want=want,
                                          retries=retries)
        return self.drag(src, at_slot(gun, slot), want=want, retries=retries,
                         weapon=weapon)

    def right_click_equip(self, gun, slot, src, want=ANY_ITEM, retries=1):
        """Fit `src` by right-clicking it. Only reaches the HELD weapon.

        Measured 4/4 at 0.35 s against 0/4 at 1.70 s for the equivalent drag
        (tools/probe_equip_gesture.py, 2026-08-02) -- the drag is not merely
        slower here, it did not land at all. See docs/game_quirks.md.

        The target is not a parameter the game takes: right-click equips onto
        whichever weapon is in hand, so the caller has to be holding `gun`.
        hold() does that; equip(gesture='auto') calls this only when it already
        is.
        """
        src = as_loc(src)
        dst = at_slot(gun, slot)
        rec = {'ok': False, 'verified': True, 'src': src, 'dst': dst,
               'checks': [], 'attempts': 0, 'error': None,
               'gesture': 'right-click'}
        if self.held is not None and self.held != gun:
            rec['error'] = (f'right-click only reaches the held weapon '
                            f'(holding {self.held}, asked for {gun})')
            return rec

        before = self._slot_states(self._frame())
        checks = [(gun, slot, want)]
        x, y = self.point_of(src)
        for attempt in range(retries + 1):
            rec['attempts'] = attempt + 1
            self.pointer.right_click_at(x, y)
            results = self._await(checks, before)
            rec['checks'] = [{'gun': g, 'slot': s, 'want': w, 'seen': seen,
                              'ok': ok} for g, s, w, ok, seen in results]
            if all(r['ok'] for r in rec['checks']):
                rec['ok'] = True
                self._log(f'{loc_str(src)} -> {loc_str(dst)}: right-clicked '
                          f'({self._checks_str(rec["checks"])})')
                return rec
        rec['error'] = 'right-click did not land'
        return rec

    def await_tab(self, want, timeout=TAB_TOGGLE_TIMEOUT):
        """Poll until the Tab screen is open/closed as asked. -> bool

        No sleep in the loop: one pass IS the pace. tab_open() grabs a 41x18
        crop, and win32_cap is ~3-6 ms of fixed GDI overhead almost regardless
        of size, so the poll runs at roughly the monitor's own rate.
        """
        deadline = time.perf_counter() + timeout
        while True:
            if bool(self.tab_open()) == want:
                return True
            if time.perf_counter() >= deadline:
                return False

    def ensure_tab(self, want, tries=3):
        """Press Tab until the screen is `want`, re-pressing if need be. -> bool

        Two different failures, and only one of them is a timing constant:

        Tab is a toggle, so a blind press lands on the wrong state half the
        time -- that is why this reads before pressing at all.

        And a press sent immediately after the previous toggle is sometimes
        SWALLOWED. Measured: reopening right after a close timed out on the
        same iteration of a 4-round test, twice running, at exactly the
        deadline. A longer timeout cannot help when the keystroke never
        arrived, so the answer is to press again, not to wait harder.
        """
        mouse = self.pointer.pico
        if mouse is None:
            self._log('no Pico: cannot press Tab (SendInput has no key path)')
            return False
        for _ in range(tries):
            if bool(self.tab_open()) == want:
                return True
            mouse.key(HID_KEY_TAB, 60)
            if self.await_tab(want):
                return True
            self._log(f'Tab press swallowed; retrying')
        return bool(self.tab_open()) == want

    def hold(self, gun, settle=0.6):
        """Put weapon `gun` in hand, so right-click equips onto it.

        Costs a Tab close/open: the number keys are swallowed while the
        inventory is up. Worth it once per weapon, not once per attachment --
        which is why build() groups by weapon rather than interleaving.

        `settle` covers the weapon-swap animation and is still a guess; the
        two Tab waits around it are not, they are polled. See
        TAB_TOGGLE_TIMEOUT for the measurements.
        """
        if self.held == gun:
            return True
        mouse = self.pointer.pico
        if mouse is None:
            self._log('no Pico: cannot press 1/2 (SendInput has no key path)')
            return False
        was_open = bool(self.tab_open())
        if was_open and not self.ensure_tab(False):
            self._log('Tab would not close; 1/2 would be swallowed')
            return False
        mouse.key(HID_KEY_1 if gun == 1 else HID_KEY_2, 60)
        time.sleep(settle)
        if was_open and not self.ensure_tab(True):
            self._log('Tab would not reopen after the weapon switch')
            return False
        self.held = gun
        return True

    def unequip(self, gun, slot, to=None, retries=1, gesture='auto'):
        """Pull weapon `gun`'s `slot` off, into 库存 by default.

        gesture: 'auto' right-clicks when the destination IS 库存 and drags
        otherwise; 'click' and 'drag' force one.

        Right-click cannot aim. The game decides where the part goes and it
        always chooses the backpack, so anything else -- the floor, the other
        gun's slot -- is still a drag. That is the same shape as
        right_click_equip, where the target is "whatever is in hand" rather
        than a parameter.

        THE DRAG, AS CURRENTLY AIMED, DOES NOT REACH 库存. Measured twice on
        2026-08-02 (tools/probe_unequip_gesture.py): the slot empties and the
        part lands ON THE FLOOR -- 库存 +0, 附近 +1.

        The release POINT is what is wrong, not the gesture: point_of(at_inv())
        is still the point this was written against and it is off. Landing on
        an occupied first row is NOT the cause -- the same thing happens with
        the release computed onto an empty row.

        It went unnoticed for months because nothing checked. See drag(): it
        verifies the SOURCE slot and never the destination, so "the slot is
        empty now" read as "the part is in the bag now". The 枪 → 库存 拖拽是
        好的 entry in docs/game_quirks.md was measured that same way.

        TODO: re-measure the release point, then this can go back to being a
        real fallback. Until then 'auto' is not an optimisation, it is the
        only path that does what this function's name says.
        """
        dst = as_loc(to) if to is not None else at_inv()
        if gesture in ('auto', 'click') and dst[0] == 'inventory':
            rec = self.right_click_unequip(gun, slot, retries=retries)
            if rec['ok'] or gesture == 'click':
                return rec
            self._log(f'gun{gun}.{slot}: right-click did not clear the slot '
                      f'— falling back to the drag')
        return self.drag(at_slot(gun, slot), dst, retries=retries)

    def right_click_unequip(self, gun, slot, retries=1):
        """Pull a part off by right-clicking the slot. -> the drag record shape.

        The destination is NOT a parameter: right-click sends the part to the
        backpack, always. A caller that wants it on the floor has to drag.

        Verified by the slot reading EMPTY, not by the click returning. A
        right-click that lands on a slot which is already empty does nothing
        and looks identical to a successful one -- which is why `before` is
        taken and the check is `EMPTY` rather than "changed".
        """
        src = at_slot(gun, slot)
        rec = {'ok': False, 'verified': True, 'src': src, 'dst': at_inv(),
               'checks': [], 'attempts': 0, 'error': None,
               'gesture': 'right-click'}
        before = self._slot_states(self._frame())
        if not before[gun][slot]:
            rec['error'] = f'gun{gun}.{slot} is already empty'
            return rec
        checks = [(gun, slot, EMPTY)]
        x, y = self.point_of(src)
        for attempt in range(retries + 1):
            rec['attempts'] = attempt + 1
            self.pointer.right_click_at(x, y)
            results = self._await(checks, before)
            rec['checks'] = [{'gun': g, 'slot': s, 'want': w, 'seen': seen,
                              'ok': ok} for g, s, w, ok, seen in results]
            if all(r['ok'] for r in rec['checks']):
                rec['ok'] = True
                self._log(f'{loc_str(src)} -> 库存: right-clicked '
                          f'({self._checks_str(rec["checks"])})')
                return rec
        rec['error'] = 'right-click did not clear the slot'
        return rec

    def strip(self, gun, to=None, retries=1):
        """Take every attachment off `gun`. -> BATCH record.

        `worn` names what it found, so a caller can tell "nothing to do" from
        "did it and it worked" — both are ok=True with zero failures, and a
        bare list of records could not say which.

        Returned a plain list until 2026-08-03. Its one caller ignored the
        value entirely, which is how the difference stayed invisible.
        """
        worn = self.read_slots(gun)
        had = [s for s in SLOT_NAMES if worn[s]]
        return batch([self.unequip(gun, s, to=to, retries=retries)
                      for s in had], gun=gun, worn=had)

    def discard(self, src, retries=1):
        """Drop whatever is at `src` on the floor. Works from a slot too."""
        return self.drag(src, at_ground(), retries=retries)

    def drop_weapon(self, gun, retries=1, gesture='auto'):
        """Throw the gun in rack slot `gun` on the floor, WEARING its parts.

        -> {'ok', 'was', 'now', 'gesture', 'error'} — `was` is what the plate
        read before.

        gesture: 'auto' right-clicks the plate and falls back to the drag if
        the gun is still there; 'click' and 'drag' force one. Right-click is
        one press against a 1621 px drag, so it is the default -- see
        docs/game_quirks.md for the measured pair.

        Verified by re-reading the plate, not by the gesture reporting success:
        the point is inferred from the name plate's box (see at_gun) and an
        inference about where a click or a drag lands is exactly the kind of
        claim this project has been burned by. An unchanged plate means the
        gesture missed, and saying so is the difference between a fixable
        failure and a batch that measures the previous batch's guns.

        Dropping the whole weapon rather than stripping it first is deliberate
        and it is what makes the weapon axis repeatable: the parts leave with
        the gun, so the next pair arrives to an empty rack and an uncontaminated
        floor. Stripping would put them back in the backpack, where PUBG's
        auto-fit would bolt them onto the next gun -- which is how a run
        labelled BARE came back wearing Lower_Foregrip_C and a quickdraw
        magazine nobody asked for.
        """
        was = self._read_guns(self._frame()).get(gun)

        def settled():
            time.sleep(DROP_SETTLE)
            now = self._read_guns(self._frame()).get(gun)
            return now, (now is None and was is not None)

        rec, used, now, ok = None, None, was, False
        if gesture in ('auto', 'click'):
            x, y = gun_tag_point(gun)
            self.pointer.right_click_at(x, y)
            now, ok = settled()
            used = 'right-click'
            if not ok and gesture == 'auto':
                self._log(f'gun{gun}: right-click left the plate reading '
                          f'{now!r} — falling back to the drag')
        if not ok and gesture in ('auto', 'drag'):
            rec = self.drag(at_gun(gun), at_ground(), retries=retries)
            now, ok = settled()
            used = 'drag'

        if ok and self.held == gun:
            # The weapon that was in hand is on the floor. Leaving `held`
            # pointing at an empty rack slot makes the next hold() a no-op and
            # the next right-click land on whatever the game fell back to.
            self.held = None
        if not ok:
            self._log(f'gun{gun}: plate still reads {now!r} after the drop '
                      f'(was {was!r}) — the {used} did not take the weapon')
        # STEP shape plus the plate readings. It used to be a shape of its own
        # ({ok, was, now, gesture, error}) with no src/dst/verified, so a
        # caller stringing it after a drag had to special-case it -- and
        # `verified` mattered most here, because this is one of the few moves
        # that IS confirmed (by re-reading the plate) while looking panel-ish.
        return step(at_gun(gun), at_ground(), ok=ok, verified=True,
                    error=None if ok else 'weapon still in the rack',
                    attempts=(rec or {}).get('attempts', 1),
                    was=was, now=now, gesture=used, drag=rec)

    def clear_rack(self, guns=(1, 2)):
        """Empty both rack slots onto the floor. -> BATCH record.

        `dropped` is the guns it actually acted on; an empty slot is skipped,
        not failed.
        """
        out, did = [], []
        for g in guns:
            if self._read_guns(self._frame()).get(g) is None:
                continue
            out.append(self.drop_weapon(g))
            did.append(g)
        return batch(out, dropped=did)

    def stow(self, row, retries=1):
        """Pick row `row` off the ground into 库存."""
        return self.drag(at_ground(row), at_inv(), retries=retries)

    def clear_ground(self, retries=1, passes=4):
        """Put everything on the floor into the backpack. -> BATCH record.

        Repeats until a pass moves nothing, for the same reason stock.tidy()
        does: the list shows 12 rows and rows below scroll up as the ones
        above leave, so "the count went down" is not the stop condition and
        "the visible rows did not change" is.

        Always drags the TOP row. Pulling row i out shifts everything below it
        up, so row 0 is the only index that stays valid without re-reading
        between gestures -- and this re-reads anyway, because 附近 is the one
        panel other things fall into while you work (a swap displaces a part,
        a dropped gun lands there).

        `verified` on every step is False: panel to panel has no slot to read
        back. The batch as a whole IS verified, by the row count reaching zero
        -- which is the caller-side re-detection that MOVES's `verified: False`
        is asking for, done here once instead of by every caller.
        """
        out = []
        rows = None
        for _ in range(passes):
            view = self.look()
            rows = view.rows('nearby')
            if not rows:
                break
            before = tuple((getattr(i, 'key', None) or '?') if i is not None
                           else '-' for i in view.nearby)
            n = 0
            for _ in range(rows):
                rec = self.stow(0, retries=retries)
                out.append(rec)
                n += 1
                if not rec['ok']:
                    break
            after = self.look()
            rows = after.rows('nearby')
            if not rows:
                break
            cur = tuple((getattr(i, 'key', None) or '?') if i is not None
                        else '-' for i in after.nearby)
            if cur == before:
                # Nothing moved. Repeating is how tidy() once looped forever
                # against a panel that was not accepting drops.
                return batch(out, error=f'{rows} row(s) left and the panel did '
                                        f'not change — is 库存 full?',
                             rows_left=rows)
        return batch(out, error=None if not rows else
                     f'{rows} row(s) still on the ground after {passes} passes',
                     rows_left=rows or 0)

    def transfer(self, src_gun, dst_gun, slots=None, retries=1):
        """Move src_gun's attachments onto dst_gun. -> BATCH record.

        VIA THE BACKPACK, ON PURPOSE, and this is the interesting part.

        The obvious implementation is one drag per slot, straight across:
        at_slot(1, s) -> at_slot(2, s). MOVES lists that pair and marks it
        `evidence: 'untested'`, and the neighbouring measurement is why this
        does not default to it — a drag INTO a weapon slot from 库存 is 0/4,
        it does not land at all. If that failure is about the DROP TARGET
        rather than the source, the direct route fails the same way, and it
        fails SILENTLY: the part stays where it was and only the read-back
        says so. So the route taken is the one every gesture of which is
        measured: unequip (drag out, works) then equip (right-click in, 4/4).

        Twice the gestures, and worth it until somebody measures the other
        one. tools/probe_transfer.py is the probe that would; when it says
        the direct drag lands, change MOVES's evidence and switch the default
        here, not the other way round.

        Only slots dst_gun actually HAS are attempted — an attachment released
        over a slot that is not drawn goes on the floor.
        """
        if src_gun == dst_gun:
            return batch([], error='source and target are the same gun')
        loadout = self.loadout()
        if loadout is None:
            return batch([], error='the Tab screen never came up')
        worn = {s: a for s, a in loadout['slots'][src_gun].items() if a}
        dst_weapon = loadout['guns'].get(dst_gun)
        want = [s for s in (slots or SLOT_NAMES) if s in worn]
        skipped = []
        if dst_weapon is not None:
            keep = [s for s in want if has_slot(dst_weapon, s)]
            skipped = [(s, f'{dst_weapon} has no {s} slot')
                       for s in want if s not in keep]
            want = keep
        else:
            # No name, no compatibility gate. Say so rather than dragging
            # blind: the three unreadable plates (SLR / Tommy Gun / Dragunov)
            # are exactly the guns a caller is most likely to be moving parts
            # between by hand.
            self._log(f'gun{dst_gun} is unnamed — no slot check, parts may '
                      f'land on the floor')
        out = []
        for s in want:
            off = self.unequip(src_gun, s, retries=retries)
            out.append(off)
            if not off['ok']:
                continue
            item = self.look().find(worn[s])
            if item is None:
                out.append(step(at_inv(), at_slot(dst_gun, s),
                                error=f'{worn[s]} vanished after unequipping '
                                      f'— it is not in 库存 or on the floor'))
                continue
            # Item form: the slot and the catalogue key come off the Item, so
            # the read-back checks that exact template rather than "occupied".
            out.append(self.equip(dst_gun, item, weapon=dst_weapon,
                                  retries=retries))
        return batch(out, src=src_gun, dst=dst_gun, moved=want,
                     skipped=skipped)

    # ── Batch ──

    def build(self, gun, view=None, weapon=None, replace=False,
              require_weapon=True, **kw):
        """Fit `gun` with everything compatible that is loose on screen.

        The whole loop: look, plan against the catalogue, drag, re-look. Pass
        a `view` to plan off a detection pass you already have.

        weapon          ROSTER key of what is in that slot. Defaults to what
                        the name plate read, but pass it when you know — the
                        spawner just told you, say — because three plates
                        cannot be read at all: 自动装填步枪 / 汤姆逊冲锋枪 /
                        德拉贡诺夫 have English templates (SLR, Tommy Gun,
                        Dragunov) and match nothing.
        require_weapon  without a weapon key there is no compatibility gate at
                        all: `fits()` cannot run, and an attachment released
                        over a slot the gun does not have goes on the floor.
                        So an unnamed gun plans nothing unless this is False.

        -> BATCH record, with `skipped` carrying plan_equip()'s reasons so an
        attachment that went untouched says why.

        Returned a (records, skipped) TUPLE until 2026-08-03. The tuple was
        the odd one out — every other method here handed back a dict — so a
        caller stringing build() together with equip() or ensure_kit() had to
        remember which one unpacked.
        """
        view = view if view is not None else self.look()
        weapon = weapon or self.guns.get(gun)
        if weapon is None and require_weapon:
            self._log(f'gun{gun} is unnamed: nothing planned, because without '
                      f'a weapon key the catalogue cannot say which slots it '
                      f'has. Pass weapon=, or require_weapon=False.')
            return batch([], error='weapon unknown, and require_weapon is on',
                         gun=gun, weapon=None,
                         skipped=[(item.key, item.where, 'weapon unknown')
                                  for item in view.inventory + view.nearby
                                  if item is not None and item.key])
        current = {s: (it.asset if it else '')
                   for s, it in view.weapons[gun].items()}
        drags, skipped = plan_equip(weapon, view, current, replace=replace)
        self._log(f'gun{gun} ({weapon or "ungated"}): {len(drags)} to fit, '
                  f'{len(skipped)} skipped')
        return batch(self.run_plan(gun, drags, weapon=weapon, **kw),
                     gun=gun, weapon=weapon, skipped=skipped)

    def run_plan(self, gun, drags, weapon=None, redetect=True,
                 stop_on_fail=False):
        """Execute plan_equip()'s output against weapon slot `gun`.

        redetect  True (default) re-reads the screen before every drag and
                  re-finds the attachment by name, so the plan survives rows
                  reflowing underneath it — which they do the moment a swap
                  displaces something into 库存. Pass None/False to trust the
                  plan's descending-row order instead, which holds only as
                  long as nothing is inserted. A callable is used in place of
                  look() for tests.

        Returns the list of per-drag records; a record whose source vanished
        gets error='source no longer on screen' and attempts=0.
        """
        if redetect is True:
            redetect = self.look
        out = []
        for d in drags:
            src = d['src']
            if redetect:
                found = loose_items(redetect() or {})
                hits = [loc for loc, att in found.items() if att == d['att']]
                if not hits:
                    rec = {'ok': False, 'verified': False, 'src': d['src'],
                           'dst': at_slot(gun, d['slot']), 'checks': [],
                           'attempts': 0,
                           'error': 'source no longer on screen'}
                    self._log(f'{d["att"]}: {rec["error"]}, skipped')
                    out.append(rec)
                    if stop_on_fail:
                        break
                    continue
                src = hits[0]
            rec = self.equip(gun, d['slot'], src, att=d['att'], weapon=weapon)
            out.append(rec)
            if stop_on_fail and not rec['ok']:
                break
        return out

    def ensure_kit(self, gun, want, weapon=None, restock=None, retries=1,
                   settle=KIT_SETTLE, look=None):
        """Make weapon `gun` wear exactly `want`, doing as little as possible.

            ac.ensure_kit(2, {'muzzle': 'comp_ar', 'grip': None, 'stock': None})

        Declarative: the caller says what the gun should be WEARING, not which
        drags to make. This reads what it is wearing now, plans the difference
        with plan_kit(), runs it, and reads the slots back — a slot that is
        already right is not touched at all, and one that is wrong costs one
        action.

        want     {slot: att_key or None}. **None means the slot must end up
                 EMPTY**, not "leave it alone". PUBG bolts whatever is in the
                 backpack onto a gun the moment it arrives, so an unnamed slot
                 holds whatever the last strip left lying around — the first
                 BARE run this project measured was wearing a cheek pad, and a
                 cheek pad reduces recoil. A slot absent from `want` is
                 deliberately unmanaged: name it with None to force it empty.
        weapon   ROSTER key, defaulting to what the name plate read. It is the
                 catalogue gate: without it a fit can be planned onto a slot
                 the gun does not have, and that part lands on the floor.
        restock  optional hook, called with the list of keys that are wanted
                 and nowhere on screen, and expected to put them in the
                 backpack. It is called with the Tab screen CLOSED, because
                 whoever fills it needs the spawner panel, and the screen is
                 re-read afterwards. Only a hook — nothing here knows how to
                 spawn anything (control/stock.py does).
        look     detection pass to plan and re-find with; defaults to look().

        Focus is the caller's, as it is for every other method here: this
        opens the Tab screen if it is shut and closes it again if it opened
        it, but it does not check that the game is in the foreground. Come
        through ensure_focus() (control/CLAUDE.md), or sync().

        Returns the batch shape:

            {'ok', 'gun', 'weapon', 'want', 'steps': [...], 'worn',
             'unchanged', 'missing', 'bad', 'error'}

        Each entry of `steps` is the record equip()/unequip() returned, with
        the planned {'action', 'slot', 'key', 'was'} merged over it, so a
        caller can chain them with drag()/equip() records without
        special-casing this one.

        **`ok` is decided by the readback, not by the steps.** `bad` is
        kit_faults() over the final read: a drag that reported success and a
        slot that reads as something else means the slot, not the drag. A
        measurement taken on ok=False is a measurement of a configuration that
        never existed.
        """
        look = look or self.look
        out = {'ok': False, 'gun': gun, 'weapon': weapon, 'want': dict(want),
               'steps': [], 'worn': None, 'unchanged': [], 'missing': [],
               'bad': [], 'error': None}
        with self.tab_up() as up:
            if not up:
                out['error'] = 'the Tab screen would not open'
                return out
            plan, view = self._kit_plan(gun, want, weapon, look)
            weapon = out['weapon'] = weapon or self.guns.get(gun)

            if plan['missing'] and restock:
                self._log(f'not on hand: {", ".join(plan["missing"])} — '
                          f'restocking')
                if not self.ensure_tab(False):
                    out['error'] = 'Tab would not close for the restock hook'
                    return out
                restock(list(plan['missing']))
                # The hook drives the spawner, and the spawner can push a gun
                # out of the rack onto the floor. hold() is a no-op when it
                # believes the weapon is already in hand, and a stale True
                # there right-clicks the part onto the wrong gun -- so the
                # belief is dropped and re-earned with one keypress.
                self.held = None
                if not self.ensure_tab(True):
                    out['error'] = 'Tab would not reopen after the restock'
                    return out
                plan, view = self._kit_plan(gun, want, weapon, look)

            out['unchanged'] = list(plan['unchanged'])
            out['missing'] = list(plan['missing'])
            self._log(f'gun{gun} ({weapon or "ungated"}): {len(plan["steps"])} '
                      f'step(s), {len(plan["unchanged"])} slot(s) already right')

            if any(s['action'] == 'equip' and not s['error']
                   for s in plan['steps']) and not self.hold(gun):
                self._log(f'could not take gun{gun} in hand: fitting falls back '
                          f'to dragging, which is the gesture that measured '
                          f'0/4 (see docs/game_quirks.md)')

            stale = False
            for step in plan['steps']:
                rec = self._kit_run(gun, step, weapon, retries,
                                    look if stale else None, view)
                out['steps'].append(rec)
                stale = stale or rec['attempts'] > 0

            if out['steps'] and settle:
                # Every step was polled to a verified state already; this only
                # covers the tail of the last icon animation before the whole
                # gun is read as one.
                time.sleep(settle)
            out['worn'] = self.read_slots(gun)

        out['bad'] = kit_faults(want, out['worn'])
        out['ok'] = not out['bad']
        if not out['ok'] and out['error'] is None:
            out['error'] = '; '.join(f'{b["slot"]}: {b["why"]}'
                                     for b in out['bad'])
        for b in out['bad']:
            self._log(f'gun{gun}.{b["slot"]}: {b["why"]}')
        return out

    def _kit_plan(self, gun, want, weapon, look):
        """One detection pass -> (plan, view). Names the guns as a side effect."""
        view = look()
        weapon = weapon or self.guns.get(gun)
        return plan_kit(want, self._worn_of(view, gun), view, weapon), view

    def _worn_of(self, view, gun):
        """{slot: template name} for `gun`, out of a view if it carries one.

        look() already read every slot, so re-reading the screen for the same
        answer would cost a second grab. A caller that hands in some other
        detection pass still gets the right answer, off its own frame.
        """
        weapons = getattr(view, 'weapons', None)
        if weapons is None:
            return self.read_slots(gun)
        return {s: (it.asset if it is not None else '')
                for s, it in weapons[gun].items()}

    def _kit_run(self, gun, step, weapon, retries, look, view):
        """Execute one plan_kit step. -> the plan step merged with the record.

        `look` is None while the plan's own detection pass is still valid and
        a callable once anything has moved: an unequipped part lands in 库存
        as a NEW row and every row below it shifts, so a source row from
        before that is a source row for something else now.
        """
        rec = dict(step)
        # Same shape a refused drag() comes back with, so a caller reading
        # steps never has to ask whether a step ran.
        blank = {'ok': False, 'verified': False, 'dst': at_slot(gun, step['slot'])
                 if step['slot'] in SLOT_NAMES else None, 'checks': [],
                 'attempts': 0}
        if step['error']:
            return {**rec, **blank, 'error': step['error']}
        if step['action'] == 'unequip':
            return {**rec, **self.unequip(gun, step['slot'], retries=retries)}

        src = step['src']
        if look is not None or src is None:
            found = loose_items((look or (lambda: view))() or {})
            hits = sorted((loc for loc, k in found.items() if k == step['key']),
                          key=_src_rank)
            if not hits:
                return {**rec, **blank,
                        'error': f'{step["key"]} is no longer on screen'}
            src = hits[0]
        return {**rec, **self.equip(gun, step['slot'], src, att=step['key'],
                                    weapon=weapon, retries=retries)}

    # ── Geometry ──

    def point_of(self, loc):
        """Where to press or release for a location."""
        loc = as_loc(loc)
        if is_gun(loc):
            return gun_tag_point(loc[1])
        if is_slot(loc):
            _, gun, slot = loc
            return att_slot_point(gun, slot)
        kind, row = loc[0], (loc[1] if len(loc) > 1 else None)
        if row is None:
            # "Anywhere in this panel" is a RELEASE point, not a row. Rows are
            # for picking a specific item up; releasing on one put the item on
            # the floor. See tab_layout.DROP_XY for how the two points were
            # measured and why a row point cannot stand in for them.
            return DROP_XY[kind]
        return row_point(row, kind)

    # ── Internals ──

    @staticmethod
    def _reject(src, dst, weapon):
        """Why this drag must not be attempted, or None."""
        for loc, side in ((src, 'source'), (dst, 'target')):
            if is_gun(loc):
                # The weapon itself. Without this branch it fell through to
                # "is not a location" and every drop_weapon() was refused
                # before the mouse moved -- the address, the grab point and
                # the method all existed, and the validator had never been
                # told about them.
                if loc[1] not in GUNS:
                    return f'{side} gun {loc[1]} is not 1 or 2'
            elif is_slot(loc):
                _, gun, slot = loc
                if gun not in GUNS:
                    return f'{side} gun {gun} is not 1 or 2'
                if slot not in SLOT_NAMES:
                    return f'{side} slot {slot!r} is not one of {SLOT_NAMES}'
            elif loc[0] in PANEL_KINDS:
                row = loc[1] if len(loc) > 1 else None
                if row is not None and not 0 <= row < INV_ROWS:
                    return f'{side} row {row} is outside 0..{INV_ROWS - 1}'
            else:
                return f'{side} {loc!r} is not a location'
        if src == dst:
            return 'source and target are the same place'
        # The pair itself, against MOVES. Both ends being valid addresses does
        # not make the move a thing the game does -- ('gun', 'inventory') is
        # two good addresses and no such action, and without this it would go
        # out as a real 1600 px drag and be reported as a mystery failure.
        # Same shape of hole as the ('gun', n) branch above, which took a year
        # to find because the symptom was "the drag failed" rather than "that
        # is not a move".
        if move_info(src, dst) is None:
            return (f'{kind_of(src)} -> {kind_of(dst)} is not in MOVES: '
                    f'either it is not something the game does, or the table '
                    f'has not heard of it yet. Add it there, with evidence.')
        if weapon is not None:
            if weapon not in ROSTER:
                return f'unknown weapon {weapon!r}'
            if is_slot(dst) and not has_slot(weapon, dst[2]):
                return f'{weapon} has no {dst[2]} slot'
        return None

    def _frame(self):
        """A Tab-screen frame, cursor out of the way first.

        The cursor sits on the drop target the moment a drag ends, and a
        hovered slot draws a tooltip over itself.
        """
        self.park()
        return self.grabber.grab()

    def _read_guns(self, frame):
        """{1: key, 2: key} off the name plates, None where not usable.

        Anything outside the live roster becomes None on purpose: an
        unrecognised key would narrow every slot's template bank to nothing
        and read the whole gun as empty. A wider bank beats a blind one.
        """
        crops = {}
        for key in ('gun_name_1', 'gun_name_2'):
            y, x, h, w = HUD_REGIONS[key]
            crops[key] = frame[y:y + h, x:x + w]
        names = self.ocr.classify(crops)
        return {g: (n if n in ROSTER and is_live(n) else None)
                for g, n in zip(GUNS, names)}

    def _slot_states(self, frame):
        """{1: {slot: template name}, 2: {...}}, '' for an empty slot."""
        worn = self.items.read_weapons(frame, self.guns)
        return {g: {s: (it.asset if it is not None else '')
                    for s, it in slots.items()}
                for g, slots in worn.items()}

    def _await(self, checks, before, timeout=VERIFY_TIMEOUT):
        """Poll the weapon slots until every check passes, or time runs out.

        Returns [(gun, slot, want, ok, seen), ...]. ANY_ITEM additionally
        demands the slot differ from `before`: dropping onto a slot that
        already reads as *something* would otherwise pass on the strength of
        what was there before the drag, so a swap that never happened would
        report success.
        """
        deadline = time.perf_counter() + timeout
        while True:
            states = self._slot_states(self._frame())
            out = []
            for gun, slot, want in checks:
                seen = states[gun][slot]
                ok = (seen != '' and seen != before[gun][slot]
                      if want == ANY_ITEM else seen == want)
                out.append((gun, slot, want, ok, seen))
            if all(r[3] for r in out) or time.perf_counter() >= deadline:
                return out
            time.sleep(VERIFY_POLL)

    @staticmethod
    def _checks_str(checks):
        out = []
        for c in checks:
            line = f'gun{c["gun"]}.{c["slot"]}={c["seen"] or "<empty>"}'
            if not c['ok']:
                line += f' (wanted {c["want"] or "<empty>"})'
            out.append(line)
        return ', '.join(out)


# ════════════════════════════════════════════════════════════
# CLI — one drag at a time, for checking the geometry by hand
# ════════════════════════════════════════════════════════════

def dump(view, guns):
    """Print a TabView the way the CLI wants it."""
    for panel in PANEL_KINDS:
        rows = getattr(view, panel)
        n = view.rows(panel)
        print(f'{panel} ({n} rows):' if n else f'{panel}: empty')
        for i in range(n):
            item = rows[i]
            if item is not None:
                print(f'   row{i:2d} {item.key:<14} {item.zh}')
            else:
                print(f'   row{i:2d} {"?":<14} <occupied, no template>')
    for g in GUNS:
        worn = '  '.join(
            f'{s}={(view.weapons[g][s].key or view.weapons[g][s].asset) if view.weapons[g][s] else "-"}'
            for s in SLOT_NAMES)
        print(f'gun{g} {guns.get(g) or "?":<10} {worn}')


def main():
    try:            # item names are Chinese; a cp1252 console dies on 倍
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(
        description='Drag attachments around the Tab inventory screen.',
        epilog='locations: inv[:row] | ground[:row] | slot:<gun>:<slot>')
    ap.add_argument('--read', action='store_true',
                    help='read the screen and print it, drag nothing')
    ap.add_argument('--drag', nargs=2, metavar=('SRC', 'DST'))
    ap.add_argument('--equip', metavar='GUN:ATT',
                    help='find an attachment by catalog key and fit it, '
                         'e.g. 1:comp_ar')
    ap.add_argument('--build', type=int, metavar='GUN',
                    help='fit this gun with everything compatible on screen')
    ap.add_argument('--weapon', help='ROSTER key of the gun --build targets, '
                                     'when the name plate does not read')
    ap.add_argument('--replace', action='store_true',
                    help='--build swaps into occupied slots too')
    ap.add_argument('--points', action='store_true',
                    help='print every click point, no game needed')
    ap.add_argument('--rows', help='override the row counts, e.g. 5,5 '
                                   '(nearby,inventory)')
    ap.add_argument('--retries', type=int, default=1)
    ap.add_argument('--countdown', type=int, default=5)
    ap.add_argument('--backend', default='auto',
                    choices=('auto', 'pico', 'sendinput'))
    args = ap.parse_args()

    if args.points:
        for kind in PANEL_KINDS:
            pts = ' '.join(f'{i}:{row_point(i, kind)}' for i in range(INV_ROWS))
            print(f'{kind:10s} {pts}')
        for g in GUNS:
            print(f'gun{g}      ' + ' '.join(f'{s}:{att_slot_point(g, s)}'
                                             for s in SLOT_NAMES))
        return 0

    actions = [args.read, args.drag, args.equip, args.build is not None]
    if not any(actions):
        ap.error('give --read, --drag SRC DST, --equip GUN:ATT, --build GUN, '
                 'or --points')

    src = dst = None
    if args.drag:
        src, dst = parse_loc(args.drag[0]), parse_loc(args.drag[1])
        print(f'{loc_str(src)} -> {loc_str(dst)}')

    print('>>> Taking the foreground. The Tab inventory must be OPEN.')
    if not ensure_focus(countdown_s=args.countdown, label='the inventory'):
        print('[!] ABORT: could not focus the game.')
        return 1
    time.sleep(0.6)

    ac = InventoryControl(args.backend)
    try:
        if not ac.sync():
            return 1

        view = ac.look()
        dump(view, ac.guns)
        if args.rows:                   # after look(), so it really overrides
            n, i = (int(v) for v in args.rows.split(','))
            ac.set_rows(nearby=n, inventory=i)

        if args.build is not None:
            res = ac.build(args.build, view, weapon=args.weapon,
                           replace=args.replace)
            recs, skipped = res['steps'], res['skipped']
            for att, loc, why in skipped:
                print(f'  skip {att:<14} {loc_str(loc):<14} {why}')
            print()
            for r in recs:
                print(f'  {loc_str(r["src"]):<14} -> {loc_str(r["dst"]):<14} '
                      f'{"ok" if r["ok"] else r["error"]}')
            return 0 if all(r['ok'] for r in recs) else 1

        if args.equip:
            gun, key = args.equip.split(':')
            item = view.find(key)
            if item is None:
                print(f'{key} is not in either list')
                return 1
            print(f'\n{key} at {loc_str(item.where)} -> gun{gun}.{item.slot}')
            rec = ac.equip(int(gun), item, retries=args.retries)
            print(f'{rec}')
            return 0 if rec['ok'] else 1

        if not args.drag:
            return 0

        rec = ac.drag(src, dst, retries=args.retries)
        print(f'\n{rec}')
        return 0 if rec['ok'] else 1
    finally:
        ac.close()


if __name__ == '__main__':
    sys.exit(main())

