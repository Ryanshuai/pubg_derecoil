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
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import HUD_REGIONS
from detector.attachment_catalog import ATTACHMENTS, ROSTER, fits, has_slot
from detector.attachment_detector import (AMBIGUOUS, AttachmentDetector,
                                         SLOT_NAMES)
from detector.slot_detector import (SlotDetector, ABSENT as SLOT_ABSENT,
                                    EMPTY as SLOT_EMPTY)
from detector.cropper import capture_screen, win32_cap
from detector.tab_detector import TabTypeDetector
from detector.tab_items import TabGrabber, TabItemDetector, panel_rows
from detector.tab_layout import (DROP_XY, INV_ROWS, PARK_XY, att_slot_point,
                                 gun_tag_point, row_point)
from detector.weapon_template_detector import TabWeaponDetector
from press.pico_mouse import HID_KEY_1, HID_KEY_2, HID_KEY_TAB
from press.pointer import Pointer
from control.focus import game_focused, ensure_focus

PANEL_KINDS = ('nearby', 'inventory')


_LAST_GESTURE_END = [None]   # perf_counter when the previous gesture returned

# Which process wrote a line. The journal is a SHARED file — several agents
# drive this one game in turn — so without these a run's lines are interleaved
# with someone else's and `gap_s` (a per-process perf_counter difference) reads
# as nonsense across the seam. `t` is wall clock so lines from two processes
# can be ordered against each other and against a run directory's timestamps.
PID = os.getpid()
PROC = os.path.basename(sys.argv[0] or 'python')


# One roll, kept. Covering clicks and toggles as well as drags multiplied the
# line rate several times over, and this file is append-only and always on, so
# it needs an end. 8 MB is roughly 30k gestures — several long collector runs —
# and the previous 8 MB stays as `.1`, which is what a post-mortem two runs
# later actually needs.
JOURNAL_MAX_BYTES = 8 << 20


def journal(rec):
    """Append one gesture record. Never raises — a log must not fail a gesture."""
    try:
        os.makedirs(os.path.dirname(DRAG_LOG), exist_ok=True)
        # RACY BETWEEN AGENTS, and deliberately not locked: two processes can
        # both decide to roll and one loses a line. A lock file shared across
        # agents is a way for a stuck run to block a healthy one from moving
        # the mouse, which is a far worse failure than a missing log line.
        if os.path.getsize(DRAG_LOG) > JOURNAL_MAX_BYTES:
            os.replace(DRAG_LOG, DRAG_LOG + '.1')
    except OSError:
        pass
    try:
        with open(DRAG_LOG, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')
    except Exception:
        pass


def panel_counts(src, dst):
    """Which lists can be counted to see whether this drag landed.

    -> (source panel or None, destination panel) | None

    `dst` with no row means "anywhere in this list", and a list fills from the
    top with no gaps, so its row count answers "did something arrive". That is
    the ONLY reading available for a drop into a panel, and it is available
    whatever the source is — which matters, because the source tells you much
    less than it appears to:

        unequip() releases a slot onto the floor and verifies the SLOT IS
        EMPTY. It is empty either way. docs/game_quirks.md has the record: the
        part reached the floor instead of the backpack and the slot check
        passed, for months.

    So the source is returned only when it is itself a list row (then its
    departure is a second, independent signal), and the destination always.
    """
    if is_slot(dst) or is_gun(dst) or dst[0] not in PANEL_KINDS:
        return None
    if len(dst) > 1 and dst[1] is not None:
        return None
    src_panel = (src[0] if src[0] in PANEL_KINDS and len(src) > 1
                 and src[1] is not None else None)
    return (src_panel, dst[0])
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

# THE GAME NEEDS TIME AFTER THE BUTTON COMES UP, and this is that time.
#
# It was 0.0 for a while, on the reasoning that a drop is verified by polling
# so the gesture need not also sit out a fixed settle — the wait was "spent
# before the first look rather than instead of it", absorbed by
# VERIFY_TIMEOUT. That reasoning has a hole and the hole is the common case:
#
#   A drag whose destination is a PANEL rather than a slot has nothing to
#   read back. drag() returns `dragged (unverified)` the instant the button
#   is up, and the next gesture starts hauling the cursor away before the
#   game has taken the item. Every such drop is lost, and every one of them
#   reports success.
#
# THE FIX WAS THE READBACK, NOT THIS NUMBER — and that is the useful part.
# Once a panel drop is verified (see is_panel_drop / _await_panel), the poll
# itself holds the cursor still while it grabs frames, which is exactly what
# the settle was for. Swept again with retries=0, six drags per value, so a
# retry could not hide anything (temp_debug/floor_drag_params.py):
#
#     0.00  6/6    0.10  6/6    0.20  6/6
#     0.05  5/6    0.15  6/6    0.25  5/6
#
# Flat — AND THAT SWEEP WAS WRONG, in a way worth keeping because it is the
# second time the same mistake produced a confident number. Its loop called
# look() before each drag to check 库存 was not empty, and look() is a ~123 ms
# full detection pass during which the cursor does not move. It fed the very
# quantity it was varying back in as a constant.
#
# The honest measurement is the real caller, where nothing separates one drag
# from the next. clear_inventory over six attachments, counting RETRIES (each
# one is a first attempt that did not land):
#
#     drop = 0.10    5 retries on 6 drags
#     drop = 0.25    1 retry  on 6 drags
#
# So 0.25 it is. The readback is what makes the gesture correct — it retries
# until the row is gone — but the wait is what makes it correct on the FIRST
# try, and a retry costs a full second.
#
# MEASURED 2026-08-04, twelve attachments in 库存, clear_inventory() on each:
#
#     drop = 0.0     库存 12 -> 12,  附近 0 -> 0     nothing landed, 0/12
#     drop = 0.25    库存 12 ->  0,  附近 0 -> 12    everything landed, 12/12
#
# and swept, three consecutive drags per value with no read between them,
# which is the only arrangement that fails — a screenshot between two drags
# IS the wait:
#
#     0.00  1/3     0.10  2/3     0.20  3/3
#     0.05  2/3     0.15  2/3     (temp_debug/sweep_drop_wait.py)
#
# 0.20 is the floor and this is one run on one machine, so 0.25 is the value.
#
# The symptom is `N row(s) left and the panel did not change — the drops are
# not landing`, and it is worth knowing what it costs upstream: a 库存 that
# will not empty is a 库存 that fills, and a FULL 库存 makes the spawner
# silently deliver nothing. Three template-collection rounds were lost to it
# before anyone looked at the drag itself.
#
# NOT FIXED BY A SHORTER PATH. DROP_XY['nearby'] is 437 px from 库存 row 0,
# and releasing straight left instead — same y, 230 px — is what a human does
# (34 recorded drags, horizontal to within 10-33 px). Swept the same way it
# was no better at any wait and worse at 0.20 (2/3 against 3/3), so the fixed
# point stays. The distance was never the problem; the missing readback was.
DROP_WAIT = 0.25

# 附近 ends at x=880 and 库存 starts at 907, so this is the first column
# inside the target panel: crossing the divider is the whole requirement.
NEARBY_DROP_X = 870

# Gesture timing handed to Pointer.drag. Defaults are press/pointer.py's, i.e.
# what shipped before anyone measured them; probe_drag_speed.py is what says
# whether they can come down.
DRAG_TIMING = {'drop': DROP_WAIT}

# ── the gesture journal ───────────────────────────────────────────────────
#
# One JSON line per GESTURE, appended, always on. It exists because "sometimes
# it does not land on the floor" is not answerable from a boolean: the record
# has to carry what was DIFFERENT about the ones that failed. So every line has
# the three families of candidate cause side by side —
#
#   the gesture   where the cursor was placed, and how many placement attempts
#                 that took (Pointer.place). For a drag, both ends.
#   the state     row counts in both lists before and the poll sequence after
#                 (a drag), or the slot readback (a click), or the name-plate
#                 ink before and after (anything aimed at a gun)
#   the timing    seconds since the previous gesture, and how long this took
#
# IT WAS DRAGS ONLY UNTIL 2026-08-05, and that was the wrong half. The gesture
# that costs a WEAPON is the right click: aimed at a slot with nothing in it —
# or at a slot the cursor drifted off — it reaches the weapon row underneath
# and throws the whole gun on the floor, wearing its parts. 74 of those across
# 11 collector runs, and not one left a line here, because right_click_equip /
# right_click_unequip / auto_equip / drop_weapon all went straight to the
# Pointer. Now every one of them writes, with `kind` saying which:
#
#   drag      press-travel-release, the original record
#   click     a right click, with what the slot read afterwards
#   drop      drop_weapon: the whole gun out, plate ink before and after
#   refused   a gesture this layer REFUSED to send, and why. The near-misses
#             are evidence too — an unequip that declined an empty slot is the
#             guard that saved a gun, and it is invisible in a log of actions.
#
# Reading it is `pixi run drag-log`. It is small (a few hundred bytes a
# gesture) and it is the only place these are written down together; every
# earlier attempt to explain a failure had to guess at one of them.
#
# WRITTEN BY WHOEVER IS DRIVING, which is the other reason it is always on:
# several agents share this game, and when someone else's run comes back empty
# the question is what their gestures did — not what they logged, since a run
# that fails silently logs nothing. Every line carries `pid` and `proc`.
DRAG_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))), 'docs', 'drag', 'journal.jsonl')

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
# tail of the last one's icon animation. It came over from harvest, which kept
# an unread copy of this and two spawner timings until 2026-08-03; this is now
# the only one, and it is the one number here nobody has measured.
KIT_SETTLE = 0.6

# When a slot readback comes back AMBIGUOUS, how many times to move the world
# behind the translucent panel and read again, and how far to move it.
#
# The panel is translucent, so a slot icon is composited over whatever the
# world shows behind it, and a dark backdrop collapses the margin between
# neighbouring parts. Measured on a vector wearing ext_smg, same gun, same
# slot, one turn apart: a dark view read `quick_smg` at mse 267.7 / margin
# 1.021, and six ordinary views read `ext_smg` at mse 88..164 / margin
# 1.67..2.74. Two re-reads is enough for that spread; the loop stops as soon
# as the ambiguity clears, so the usual cost is zero.
#
# Yaw is small on purpose: with Tab up, raw counts land partly on the CURSOR
# rather than only on the view (see _nudge_backdrop).
AMBIGUOUS_REREADS = 2
NUDGE_COUNTS = 600
NUDGE_SETTLE_S = 0.35

# How long gun_slot() watches for the slot boxes to be drawn. The Tab panel
# fades in, so the answer right after it opens is "no gun" for a few frames on
# a rack that plainly holds one. Generous because the cost of waiting is a few
# frames and the cost of answering early is a round thrown away.
GUN_SLOT_WATCH_S = 1.2

# "IS A GUN DRAWN IN THIS RACK ROW" — white-text-mask pixels over gun_name_N
# with Tab up. MEASURED 2026-08-03:
#
#   empty rack row     0        6 of 6 samples, exactly zero
#   a gun is racked    679-901  13 samples
#
# One frame carries its own control: an akm read 682 on row 1 and 0 on row 2 —
# same frame, same scenery, one row occupied and one not.
#
# 200 sits ~3.4x under the lowest real plate and far above the zero floor. The
# question this answers is the one the OCR cannot be asked on this screen,
# because the OCR is what is being checked.
#
# Lives here rather than in a calibration script because control/ acts on it —
# clear_rack decides whether to drop a gun by this number. A second copy in a
# caller would drift, and the failure would be silent both ways.
PLATE_INK_MIN = 200


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

    `verifiable` is False when the wanted part has no icon template: the slot
    cannot be read as holding it, only as holding *something*, so the fault
    means "cannot be proven" rather than "is wrong". Both are reasons not to
    record a measurement, but only one of them is a reason to go looking for a
    failed drag.

    As of 2026-08-03 no attachment in the catalogue is in that state — the
    three that were (brake_ar, heavy_stock, variable, all added to the game
    after this repo's art dump) now carry icons recovered off the screen by
    calibration/solve_template.py. The branch stays for the next one the game adds.
    """
    out = []
    for slot in _slot_order(want):
        key = want[slot]
        cur = (worn or {}).get(slot, '') or ''
        if key is None:
            if cur == AMBIGUOUS:
                # "Something is there and the bank cannot name it" is not
                # evidence that the slot is occupied — a translucent panel over
                # a dark backdrop drags an EMPTY tile's best match under
                # MSE_EMPTY_TH with no margin, and out comes the sentinel. Same
                # cause as the fitted case below; the `key is None` branch was
                # simply missed when that was fixed, and it cost two of three
                # mk14 EMA passes to `muzzle should be empty, reads '?'`.
                out.append({'slot': slot, 'key': None, 'verifiable': False,
                            'why': f'reads {cur!r} — cannot tell an occupied '
                                   f'slot from a dark backdrop; wanted empty'})
            elif cur:
                out.append({'slot': slot, 'key': None, 'verifiable': True,
                            'why': f'reads {cur!r}, should be empty'})
            continue
        if slot_matches(cur, key):
            continue
        if cur == AMBIGUOUS:
            # Occupied, and the bank cannot separate its top two candidates
            # (AttachmentDetector.MARGIN_MIN). That is the same KIND of fault
            # as a part with no template: the slot cannot be read as holding
            # this, only as holding something. NOT verifiable, so ensure_kit
            # reports it rather than treating it as a drag that missed and
            # dragging again.
            #
            # ⚠ This used to end "— a retry cannot improve a reading", and
            # that is false: the panel is TRANSLUCENT, so re-reading against a
            # different backdrop can and does resolve it (see ensure_kit's
            # AMBIGUOUS_REREADS). Re-DRAGGING still cannot, which is the part
            # that was right. `verifiable: False` is what tells the two apart.
            out.append({'slot': slot, 'key': key, 'verifiable': False,
                        'why': f'holds something the templates cannot '
                               f'separate; wanted {key}'})
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
        # NOT an OCR, whatever it gets called elsewhere: it matches a
        # binary mask of the plate against one stored mask per weapon.
        # There is no character recognition anywhere in this project, and
        # the old name `self.ocr` invited callers to expect a reader that
        # generalises to text it has never seen. It does not.
        self.name_template = TabWeaponDetector()
        # Built on first use. Every gesture aimed at a slot consults it, but a
        # caller that only reads loadouts never does, and it is the same rule
        # the Pointer follows -- do not construct what this instance may not
        # need. See slot_states() for what it is for.
        self._slots = None
        self.verbose = verbose
        self.rows = {'nearby': None, 'inventory': None}
        self.guns = {1: None, 2: None}        # catalog key per weapon slot
        self.held = None                      # weapon in hand, or None if unknown
        # The poll sequence _await_panel records, consumed and cleared by
        # _journal. Declared here rather than sprung into existence on first
        # use: _journal ran `getattr(self, 'last_poll', None)` because a drag
        # journalled before any panel-to-panel wait would otherwise raise, and
        # a getattr on YOUR OWN attribute is a note saying the object has no
        # settled shape. Giving it a home is the fix; the getattr only hid it.
        self.last_poll = None
        # Overrides passed straight to Pointer.drag — the gesture's timing.
        # Every calibration run reaches the Tab screen through here, so these
        # are worth measuring rather than guessing; tools/probe_drag_speed.py
        # sweeps them and reports the fastest setting that still lands.
        self.timing = dict(DRAG_TIMING)
        # Built on first failure only: ensure_tab succeeds nearly
        # always, and loading three templates for a diagnosis that
        # never runs is pure start-up cost.
        self._spawner_screen = None
        self._lobby_screen = None

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

    def frame(self):
        """One Tab-screen frame, cursor parked. -> the grabber's crops.

        Public because callers need it and were taking it anyway: a collector
        that photographs the screen and then wants it READ has to hand the
        same pixels to both, or it is describing one frame with another
        frame's answer. calibration/collect_templates.py reached through to
        `ac.grabber.grab()` for exactly this, which skips park() — and a
        hovered slot draws a tooltip over itself.
        """
        return self._frame()

    def look(self, frame=None):
        """Grab the Tab screen and read it. Returns a TabView.

        Also caches what it learned: which weapon is in each slot (so slot
        reads can narrow their template bank) and how many rows each list is
        showing (so a drop into a panel lands past the end of it).

        `frame` reads a frame the caller ALREADY HAS instead of grabbing a new
        one. Not an optimisation: a caller holding a captured frame and asking
        this to grab its own gets an answer about a different instant, and on
        a screen that is being dragged on that is a different screen. It also
        skips park(), so the second grab can land while the cursor is over a
        slot. Both were live in collect_templates, which reached past this
        method into `ac.items.detect(...)` to get it.
        """
        frame = self._frame() if frame is None else frame
        self.guns = self._read_guns(frame)
        view = self.items.detect(frame, self.guns)
        self.set_rows(nearby=view.rows('nearby'),
                      inventory=view.rows('inventory'))
        return view

    def read_weapons(self, frame=None):
        """{1: key, 2: key} off the two name plates; None where unmatched.

        `frame` names the guns in a frame the caller already holds, for the
        same reason look() takes one. collect_templates went to
        `ac.name_template.classify({...})` to get this, cutting the two plate regions by
        hand — which is this method's body, minus the roster filter that stops
        an unrecognised name narrowing every slot's template bank to nothing.
        """
        return self._read_guns(self._frame() if frame is None else frame)

    def plate_ink(self, gun, frame=None):
        """White-text pixels on gun `gun`'s name plate. -> int

        Not a name. It answers "is a plate drawn here at all", which the OCR
        cannot be asked, because on this screen the OCR is the thing being
        checked. See TabWeaponDetector.ink: with the rack emptied first, zero
        ink to some ink is a weapon ARRIVING, established without consulting
        any template, and that is the missing half of labelling a plate
        capture with the weapon that was requested.
        """
        frame = self._frame() if frame is None else frame
        y, x, h, w = HUD_REGIONS[f'gun_name_{gun}']
        return self.name_template.ink(frame[y:y + h, x:x + w])

    def slot_states(self, gun, frame=None):
        """{slot: absent|empty|filled|unknown} for `gun`. IS THERE A PART HERE.

        The other slot reader on this class, read_slots(), answers WHICH part.
        Both go through the same template bank now; they still differ in what
        they will say, and the difference is the point:

          read_slots   names it, so it can be wrong about WHICH -- and reports
                       AMBIGUOUS when two templates are within MARGIN_MIN.
          slot_states  only ever says whether ANY part is there, and adds the
                       tile's border ring on top: a slot the weapon does not
                       have reads `absent` rather than `empty`.

        Anything asking "is it safe to send a gesture at this slot" wants THIS
        one. See unequip() for what the wrong answer costs.

        ⚠ IT USED TO READ TILE GEOMETRY ONLY, which cost 74 guns, and a part
        with no template now reads `empty`. Both in slot_detector's docstring.
        """
        if self._slots is None:
            self._slots = SlotDetector()
        frame = self._frame() if frame is None else frame
        # The weapon narrows the bank to what it can physically hold, which is
        # what stops an SMG suppressor being read onto an SKS. `self.guns` is
        # whatever the last look() cached and None is a legitimate answer --
        # candidates() treats an unknown key as no key and tries the whole
        # slot, which is looser in one direction only.
        return self._slots.classify(frame, gun, (self.guns or {}).get(gun))

    def slot_state(self, gun, slot, frame=None):
        """One slot's tile state. -> absent|empty|filled|unknown"""
        return self.slot_states(gun, frame).get(slot)

    def read_slots(self, gun=None, frame=None):
        """What the guns are wearing, as template names ('' when empty).

        gun=None -> {1: {slot: name}, 2: {slot: name}}; gun=1|2 -> {slot: name}.

        `frame` for the reason look() takes one: a caller holding a frame and
        asking this to grab its own gets an answer about a different instant.
        It matters most when this is read ALONGSIDE slot_states(), which has
        taken a frame all along — the two readers are only worth comparing
        when they are describing the same screen.
        """
        out = self._slot_states(self._frame() if frame is None else frame)
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
            self._journal_refusal('refused', src, dst, err, by='_reject')
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

        # A PANEL DESTINATION IS READ BACK TOO, by counting rows.
        #
        # This branch used to return `dragged (unverified)` the moment the
        # button came up — the one path in this layer that reported success
        # without looking, and therefore the one place where a broken gesture
        # could not be noticed. It hid a drag that landed NOTHING for as long
        # as it existed: 12 attachments, 12 `dragged`, 0 items moved.
        #
        # There is no slot to classify here, but there is a fact that settles
        # it: lists fill from the top with no gaps, so the source list loses a
        # row or the destination gains one. panel_rows() reads that off the
        # ink alone, ~1 ms, no template — which also means it works while
        # photographing an attachment this repo cannot yet name.
        #
        # Deliberately EITHER side: a 12-row window over a fuller pack does
        # not shrink when one item leaves (rows scroll up), and a full
        # destination does not grow. One of the two moves in every case that
        # is not both at once.
        panels = panel_counts(src, dst)
        rec = {'ok': False, 'verified': bool(checks) or panels is not None,
               'src': src, 'dst': dst, 'checks': [], 'attempts': 0,
               'error': None}
        p0 = self.point_of(src)
        p1 = self.point_of(dst, from_y=p0[1])

        for attempt in range(retries + 1):
            rec['attempts'] = attempt + 1
            # A RETRY IS ONLY SAFE WHILE NOTHING HAS CHANGED. The note above
            # this loop has said so since it was written and nothing enforced
            # it: `before` and `p0` are computed once, outside, and every
            # retry re-sends the same gesture at the same point without asking
            # whether that point still holds what it held.
            #
            # For a WEAPON SLOT source that is not a wasted click, it is a lost
            # gun. The first attempt empties the slot; the readback misses it
            # (a panel row count that could not see the arrival); the retry
            # then puts a gesture on an EMPTY slot, which reaches the weapon
            # row underneath, and both of the weapon row's gestures throw the
            # whole gun on the floor — right click 1/1, drag-left 1/1, both
            # measured. unequip() guards exactly this and its guard runs ONCE,
            # before this call, so the retry walks around it.
            #
            # Measured 2026-08-04: 74 occurrences across 11 runs of
            # collect_templates, one part lost each time and a whole round of
            # 31 in the worst. The screen at the failure shows the three
            # attachments strip() removed sitting in 附近 with the AKM listed
            # right after them — three drags that worked, and the gun taken by
            # the retry of the last one.
            #
            # THE CHECK IS ON THE GRAB, NOT THE RELEASE, and that is not a
            # detail: a right click has no release point and drops the gun
            # just the same. What makes the gesture dangerous is where it
            # STARTS.
            #
            # Costs nothing on the path that works — retries are already the
            # exception, and the first attempt is covered by unequip's guard.
            if attempt and is_slot(src):
                now = self.slot_state(src[1], src[2])
                if now in (SLOT_EMPTY, SLOT_ABSENT):
                    # The slot emptied, so the previous attempt DID move the
                    # part. Reported as ok: what the caller asked for has
                    # happened. `source_emptied` says the destination was
                    # never confirmed, for a caller that needs to know.
                    rec['ok'] = True
                    rec['source_emptied'] = True
                    rec['error'] = (f'{loc_str(src)} is {now} — the previous '
                                    f'attempt moved it and the readback missed '
                                    f'it. Not dragging again: a gesture on an '
                                    f'empty slot drops the gun.')
                    self._log(f'{loc_str(src)} -> {loc_str(dst)}: source is '
                              f'{now} after attempt {attempt}, so it moved; '
                              f'not retrying into the weapon row')
                    self._journal_refusal(
                        'refused', src, dst,
                        f'source slot reads {now} after attempt {attempt}',
                        by='retry guard', after_attempt=attempt,
                        plate=[self._plate(src[1]), None])
                    return rec
            if panels is not None:
                f = self._frame()
                n_src0 = panel_rows(f, panels[0]) if panels[0] else 0
                n_dst0 = panel_rows(f, panels[1])
                # BOTH LISTS FULL: the count cannot answer. A panel is a
                # 12-row WINDOW over a longer pack, so a full destination does
                # not grow when something arrives and a full source does not
                # shrink when something leaves. Rather than fail a drag that
                # probably worked, drop back to reporting the gesture — the
                # same weaker claim this branch made before it could count,
                # and it says so out loud instead of returning a wrong verdict.
                if n_dst0 >= INV_ROWS and (not panels[0]
                                           or n_src0 >= INV_ROWS):
                    self._log(f'{loc_str(src)} -> {loc_str(dst)}: both lists '
                              f'are full at {INV_ROWS} rows — the row count '
                              f'cannot see this drag, not verifying it')
                    panels = None
                    rec['verified'] = bool(checks)
            gesture = self.pointer.drag(p0, p1, **self.timing)
            # The verdict is taken BEFORE the journal so that one line carries
            # the gesture and its outcome together — split across two lines,
            # a failed drop and a slow one are indistinguishable again.
            # Runs before the slot checks and gates them, because "the slot
            # emptied" is true whether the part reached the panel or the
            # floor, and only this can tell those apart.
            moved = (self._await_panel(panels, n_src0, n_dst0)
                     if gesture and panels is not None else None)
            self._journal(src, dst, p0, p1, panels, attempt,
                          (n_src0, n_dst0) if panels is not None else None,
                          gesture, moved)
            if not gesture:
                rec['error'] = 'cursor placement failed'
                return rec
            if panels is not None:
                rec['checks'] = [{'panel': panels, 'from': (n_src0, n_dst0),
                                  'ok': moved}]
                if not moved:
                    rec['error'] = 'nothing arrived in the target list'
                    if attempt >= retries:
                        break
                    self._log(f'{loc_str(src)} -> {loc_str(dst)}: nothing '
                              f'arrived, retry {attempt + 2}/{retries + 1}')
                    continue
                if not checks:
                    rec['ok'] = True
                    self._log(f'{loc_str(src)} -> {loc_str(dst)}: moved')
                    return rec
            if not checks:
                # Neither a slot nor two panels — nothing this module can read.
                rec['ok'] = True
                self._log(f'{loc_str(src)} -> {loc_str(dst)}: dragged '
                          f'(unverified)')
                return rec

            rec['checks'] = self._await(checks, before)
            if all(r['ok'] for r in rec['checks']):
                rec['ok'] = True
                self._log(f'{loc_str(src)} -> {loc_str(dst)}: ok '
                          f'({self._checks_str(rec["checks"])})')
                return rec

            if attempt >= retries:
                break
            # Retrying is safe only if the screen is exactly as it was: then
            # the item never left, so the source row is still the source row.
            moved = [(c['gun'], c['slot']) for c in rec['checks']
                     if c['seen'] != before[c['gun']][c['slot']]]
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
            self._journal_refusal('refused', src, dst, rec['error'],
                                  by='right_click_equip', held=self.held)
            return rec

        frame = self._frame()
        before = self._slot_states(frame)
        plate0 = self._plate(gun, frame)
        checks = [(gun, slot, want)]
        x, y = self.point_of(src)
        for attempt in range(retries + 1):
            rec['attempts'] = attempt + 1
            self.pointer.right_click_at(x, y)
            rec['checks'] = self._await(checks, before)
            landed = all(r['ok'] for r in rec['checks'])
            # The plate is re-read whatever the verdict: this click is aimed at
            # a PANEL row, so it should not be able to touch the weapon at all,
            # and an equip that quietly emptied the rack is exactly the kind of
            # thing that gets blamed on the spawner three steps later.
            self._journal_click(src, dst, attempt, landed, checks=rec['checks'],
                                plate=[plate0, self._plate(gun)],
                                held=self.held)
            if landed:
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
        # ONLY THE PRESSES ARE JOURNALLED, never the free case. This is called
        # around every read, so a line per call would bury the log; a line per
        # PRESS is rare and is the interesting event anyway. A swallowed press
        # is the documented start of the cursor-drift chain — Tab stays up, the
        # next turn()'s raw counts land on the cursor instead of the view, and
        # the drag after that releases short (see Pointer.place).
        pressed = 0
        for _ in range(tries):
            if bool(self.tab_open()) == want:
                if pressed:
                    self._stamp('tab', None, None, gesture=True, moved=True,
                                want=want, presses=pressed)
                return True
            mouse.key(HID_KEY_TAB, 60)
            pressed += 1
            if self.await_tab(want):
                self._stamp('tab', None, None, gesture=True, moved=True,
                            want=want, presses=pressed)
                return True
            # A THIRD FAILURE, AND PRESSING AGAIN CANNOT FIX IT: something
            # else owns the screen. The item-spawner panel is a menu, and
            # while it is up the game does not act on Tab at all -- so the
            # loop above will spend all its tries and report "swallowed",
            # which reads as a timing problem and is not one. Ask what is
            # actually there before pressing a fourth time.
            blocker = self._blocking_screen()
            if blocker:
                self._log(f'Tab did not register — {blocker}')
                self._journal_refusal('refused', None, None, blocker,
                                      by='ensure_tab', want=want,
                                      presses=pressed)
                return False
            self._log(f'Tab press swallowed; retrying')
        ok = bool(self.tab_open()) == want
        self._stamp('tab', None, None, gesture=True, moved=ok, want=want,
                    presses=pressed,
                    failed_at=None if ok else 'presses swallowed')
        return ok

    def _blocking_screen(self):
        """What is on screen instead of the inventory. -> str | None

        Only screens that SWALLOW Tab belong here. It is a diagnosis, not a
        repair: getting back into a match is LobbyControl's job and closing the
        spawner panel is SpawnerControl's, and doing either from here would be
        a second copy of that driver. Naming the cause in the log is what turns
        a twenty-minute hunt into one line.

        THE TWO CAUSES, most common first:

          not in a match   the game drops to the lobby on its own -- an idle
                           kick, a disconnect, a training round ending -- and
                           every key after that goes to a menu. Found exactly
                           this way: three runs of "Tab press swallowed" while
                           the screen showed the lobby with a BEGINNER
                           TRAINING popup over it.
          spawner panel    comma opens a menu the game does not act on Tab
                           beneath. Pressing Tab again cannot help.

        Both read as "swallowed" from inside the retry loop, which is what
        made them expensive: a timing word for a state problem.
        """
        frame = capture_screen()
        if frame is None:
            return None
        if self._lobby_screen is None:
            from detector.lobby_detector import LobbyDetector
            self._lobby_screen = LobbyDetector()
        try:
            st = self._lobby_screen.state()
            if not getattr(st, 'playable', True):
                return (f'the game is not in a match ({st.name}); every '
                        f'key goes to a menu. control.lobby.LobbyControl.'
                        f'ensure_in_match() walks back in.')
        except Exception:
            pass
        if self._spawner_screen is None:
            from detector.spawner_detector import SpawnerDetector
            self._spawner_screen = SpawnerDetector()
        if self._spawner_screen.ready and self._spawner_screen.classify(frame):
            return ('the item-spawner panel is up and the game does not '
                    'act on Tab beneath it. SpawnerControl.ensure_panel(False).')
        return None

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
            self._journal_refusal('refused', None, at_gun(gun),
                                  'no Pico: 1/2 cannot be pressed', by='hold')
            return False
        was_open = bool(self.tab_open())
        if was_open and not self.ensure_tab(False):
            self._log('Tab would not close; 1/2 would be swallowed')
            self._journal_refusal('refused', None, at_gun(gun),
                                  'Tab would not close, so 1/2 is swallowed',
                                  by='hold', held=self.held)
            return False
        mouse.key(HID_KEY_1 if gun == 1 else HID_KEY_2, 60)
        time.sleep(settle)
        if was_open and not self.ensure_tab(True):
            self._log('Tab would not reopen after the weapon switch')
            self._journal_refusal('refused', None, at_gun(gun),
                                  'Tab would not reopen after the switch',
                                  by='hold', held=self.held)
            return False
        # UNVERIFIED, and journalled anyway. Nothing here reads which weapon is
        # actually in hand — the key was sent and `held` is now a belief. It
        # earns a line because of what it does to the NEXT gesture: a swallowed
        # 1/2 leaves the wrong gun in hand, and the right-click equip that
        # follows fits the part onto that one and reports a clean miss on this
        # one. It is also the thing that happens BETWEEN two bursts of drags,
        # which is the half the drag investigation is missing (control/CLAUDE.md).
        self._stamp('hold', None, at_gun(gun), gesture=True, moved=None,
                    was_open=was_open, held=self.held)
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

        AN EMPTY SLOT IS NOT AN INERT TARGET, so this refuses to act on one.
        Either gesture aimed at a slot with nothing in it reaches the WEAPON
        ROW underneath, and both of the weapon row's gestures throw the whole
        gun on the floor with its attachments -- right click 1/1, drag-left
        1/1, both measured (docs/game_quirks.md, drop_weapon). The two
        outcomes are indistinguishable from the caller: same gesture, same
        record, the difference is only on screen.

        Measured the hard way. A collector cleared its target slot blind and
        threw away its own host gun on every round -- 18 parts collected
        nothing, three runs, while every log line looked healthy. Switching
        the blind clear from a right click to a drag did not help, because the
        hazard is the SLOT, not the gesture: 11 of the next run's 35 misses
        were the same loss one gesture along.

        `empty` and `absent` are refusals; `filled` and `unknown` go ahead.
        Only a positive "there is nothing there" blocks, because the `scope`
        position draws no tile and reads `unknown` forever -- refusing that
        would make every sight unremovable.
        """
        dst = as_loc(to) if to is not None else at_inv()
        state = self.slot_state(gun, slot)
        if state in (SLOT_EMPTY, SLOT_ABSENT):
            self._log(f'gun{gun}.{slot}: reads {state}, not clicking or '
                      f'dragging it — that gesture reaches the weapon row and '
                      f'drops the gun')
            self._journal_refusal('refused', at_slot(gun, slot), dst,
                                  f'slot reads {state}', by='unequip',
                                  slot_state=state,
                                  plate=[self._plate(gun), None])
            return step(at_slot(gun, slot), dst, ok=False, verified=True,
                        error=f'slot reads {state}', slot_state=state)
        # THE TILE IS NOT THE ONLY READER, and on the state that costs a gun it
        # is the WEAKER one. Reproduced 2026-08-04 on an AKM, same slot, one
        # unequip apart:
        #
        #     real ext. quickdraw mag   tile filled   content mse  32.2  margin 3.38
        #     whatever is there after   tile filled   content mse 346.4  margin 1.14
        #
        # The tile cannot separate those — edges 413 against a 120 threshold,
        # max brightness 157 against a corpus where filled starts at 143 — so
        # the driver gestured again, the gesture landed on a slot holding
        # nothing it could name, and the whole gun went on the floor. That path
        # is 74 lost parts across 11 collector runs.
        #
        # The CONTENT reader can separate them, and only since MARGIN_MIN
        # existed: 346/1.14 is refused as AMBIGUOUS, where before it would have
        # been named `Magazine_Extended_Large_C` — a different part, stated
        # confidently.
        #
        # THREE STATES, NOT TWO, and the middle row is the one that must not be
        # lost:
        #     tile filled + a name        act
        #     tile filled + reads empty   act — a part with no template is
        #                                 invisible to the matcher, and six
        #                                 grips once stayed on a gun for
        #                                 exactly that reason. The tile wins.
        #     tile filled + AMBIGUOUS     refuse. Something is drawn there and
        #                                 nothing knows what; a gesture aimed
        #                                 by that belief costs the weapon.
        worn = self.read_slots(gun)
        if worn.get(slot) == AMBIGUOUS:
            self._log(f'gun{gun}.{slot}: the tile says filled but the '
                      f'templates cannot name what is in it — not gesturing. '
                      f'That is the state that drops the gun.')
            self._journal_refusal('refused', at_slot(gun, slot), dst,
                                  'slot content is ambiguous', by='unequip',
                                  slot_state=state, content=AMBIGUOUS,
                                  plate=[self._plate(gun), None])
            return step(at_slot(gun, slot), dst, ok=False, verified=True,
                        error='slot content is unreadable (ambiguous)',
                        slot_state=state, content=AMBIGUOUS)
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
        frame = self._frame()
        before = self._slot_states(frame)
        plate0 = self._plate(gun, frame)
        if not before[gun][slot]:
            rec['error'] = f'gun{gun}.{slot} is already empty'
            self._journal_refusal('refused', src, at_inv(), rec['error'],
                                  by='right_click_unequip',
                                  plate=[plate0, None])
            return rec
        checks = [(gun, slot, EMPTY)]
        x, y = self.point_of(src)
        for attempt in range(retries + 1):
            rec['attempts'] = attempt + 1
            self.pointer.right_click_at(x, y)
            rec['checks'] = self._await(checks, before)
            cleared = all(r['ok'] for r in rec['checks'])
            # THIS is the gesture that loses weapons, so the plate either side
            # of it is the whole reason the journal covers clicks. The slot
            # reading EMPTY afterwards is true both when the part came off and
            # when the gun it was on went to the floor; only the plate tells
            # those apart, and the caller cannot see it at all.
            plate1 = self._plate(gun)
            lost = bool(plate0 and plate0 >= PLATE_INK_MIN
                        and (plate1 or 0) < PLATE_INK_MIN)
            self._journal_click(src, at_inv(), attempt, cleared,
                                checks=rec['checks'], plate=[plate0, plate1],
                                gun_lost=lost)
            if lost:
                # REPORTED, NOT ACTED ON. `cleared` is True in this state —
                # the slot really is empty — so returning ok=True is what this
                # has always done and callers are written against it. Saying so
                # is the change; deciding what a lost weapon means (re-rack?
                # abandon the round?) belongs to the caller that knows.
                rec['gun_lost'] = True
                self._log(f'gun{gun}: plate ink {plate0} -> {plate1} across '
                          f'that right-click — THE WEAPON LEFT THE RACK, and '
                          f'the empty slot below reads exactly like a clean '
                          f'unequip. See docs/drag/journal.jsonl.')
            if cleared:
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

        WHICH SLOTS TO PULL COMES FROM THE TILES, not from read_slots(). Two
        failures follow from asking a template that question, and both have
        been seen on this gun:

          a part with NO template is invisible, so it never comes off. That is
          every part a template collector exists to photograph, and it is why
          six grips in a row stayed on a gun that reported itself stripped.

          a template firing on an EMPTY slot puts a gesture on it, and a
          gesture on an empty slot drops the whole gun (see unequip). unequip
          now refuses those, so the cost is a wasted read rather than a lost
          weapon -- but asking the right reader in the first place means the
          refusal never has to fire.

        `worn` still reports the NAMES, from read_slots, because that is what
        a caller wants in the record. What is acted on is the tile.
        """
        # ONE FRAME FOR BOTH READERS. They used to grab their own, so `states`
        # and `named` could describe different instants -- which is exactly the
        # pair whose disagreement decides whether a gesture goes out.
        frame = self._frame()
        states = self.slot_states(gun, frame)
        named = self.read_slots(gun, frame)
        # `unknown` no longer means scope: that position is read like every
        # other slot now (slot_detector). It survives as the answer nothing
        # here should turn into a gesture, so the second branch is kept for a
        # reader that genuinely cannot tell and a template that can.
        had = [s for s in SLOT_NAMES
               if states.get(s) == 'filled'
               or (states.get(s) == 'unknown' and named.get(s))]
        return batch([self.unequip(gun, s, to=to, retries=retries)
                      for s in had], gun=gun,
                     worn=[s for s in had if named.get(s)] or had,
                     states={s: states.get(s) for s in had})

    def discard(self, src, retries=1):
        """Drop whatever is at `src` on the floor. Works from a slot too."""
        return self.drag(src, at_ground(), retries=retries)

    def auto_equip(self, src):
        """Right-click `src` and let the GAME choose the slot. -> True if sent.

        A different action from equip(), not a convenience over it: equip()
        takes a destination and refuses without one, because it is checking
        that a named part reaches a named slot. Here the destination is the
        ANSWER — the caller is asking the game where this part belongs.

        The one caller is calibration/collect_templates.py, and its reason is
        the reason this cannot be equip(): it is photographing parts whose
        templates do not exist yet, so it cannot name what it is holding, and
        naming a slot for it would mean trusting the row order. The game knows
        both. Right-clicking gets the part placed, and WHICH SLOT LIT UP then
        identifies the part, via the catalogue, with no template read.

        Verification is the caller's, deliberately: what counts as "it landed"
        differs by what the caller already knows, and the only check available
        here would be an icon-template match — exactly what that caller must
        not depend on.

        Right-click and not a drag, because 库存 -> gun measured 0 landings out
        of 4 by drag against 4/4 by right click (docs/game_quirks.md). The
        weapon must be IN HAND for the click to reach it; see hold().
        """
        loc = as_loc(src)
        if loc[0] not in PANEL_KINDS or len(loc) < 2 or loc[1] is None:
            # A panel with no row is a RELEASE point, not a thing to click,
            # and a slot or a gun is not a source for this. Named rather than
            # attempted: point_of() would happily hand back the panel's drop
            # point and the click would land on nothing.
            self._log(f'auto_equip needs a specific 库存/附近 row, not {loc!r}')
            self._journal_refusal('refused', loc, None,
                                  'not a specific 库存/附近 row',
                                  by='auto_equip')
            return False
        x, y = self.point_of(loc)
        self.pointer.right_click_at(x, y)
        # `moved` is None and that is the honest answer: this method verifies
        # NOTHING by design (see the docstring — its caller is photographing a
        # part it cannot name). The journal still gets the geometry, which is
        # the half that can be checked without a template, and it is the only
        # trace the template collector's main gesture leaves anywhere.
        self._journal_click(loc, None, 0, None, held=self.held)
        return True

    def gun_slot(self, frame=None, timeout=GUN_SLOT_WATCH_S):
        """Which rack slot holds a gun. -> 1 | 2 | None

        A racked gun DRAWS the boxes for the slots it owns; an empty rack slot
        draws nothing. AttachmentDetector.drawn answers exactly that, and it is
        a much weaker claim than "a part is in there" -- no name, no template,
        no icon match.

        Not the name plate. That OCR read None on two plainly-racked guns and
        cost a cell (calibration/harvest.Kitter.find_gun), and this question
        does not need a name.

        Callers were hardcoding 2. The spawner does not promise a slot -- an
        empty rack takes the first gun into slot 1, and re-entering the range
        empties the rack -- so `read_slots(2)` was reading a slot with no gun
        in it and answering '' for every attachment, which reads exactly like
        "the part never arrived".

        WATCHED, not sampled once -- the same rule every toggle here follows.
        The Tab panel fades in, so a single grab taken the instant it opens
        catches the slot boxes before they are drawn and answers "no gun". The
        version this replaces got away with it only because its caller happened
        to flush three frames first; moving it here without the watch made it
        return None on a gun that was plainly racked.

        Pass `frame` to ask about one you already hold, in which case there is
        nothing to wait for and it is read once.
        """
        deadline = time.perf_counter() + (0 if frame is not None else timeout)
        while True:
            f = self._frame() if frame is None else frame
            for gun in GUNS:
                for slot in SLOT_NAMES:
                    y, x, h, w = HUD_REGIONS[f'att_{gun}_{slot}']
                    if AttachmentDetector.drawn(f[y:y + h, x:x + w]):
                        return gun
            if time.perf_counter() >= deadline:
                return None
            time.sleep(0.08)

    def auto_equip_key(self, att, frame=None):
        """Find `att` in 库存 and right-click it. -> True if the click went.

        The lookup DOES read a template, and that is a real limitation rather
        than an oversight: a part with no icon in the catalogue cannot be found
        this way. calibration/collect_templates.py uses it only to re-stage a
        part it has just watched arrive, never to decide what a crop is.
        """
        item = self.look(frame).find(att)
        if item is None:
            return False
        return self.auto_equip(item.where)

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
        frame = self._frame()
        was = self._read_guns(frame).get(gun)
        plate0 = self._plate(gun, frame)

        def settled():
            time.sleep(DROP_SETTLE)
            f = self._frame()
            now = self._read_guns(f).get(gun)
            return now, (now is None and was is not None), self._plate(gun, f)

        rec, used, now, ok, plate1 = None, None, was, False, plate0
        if gesture in ('auto', 'click'):
            x, y = gun_tag_point(gun)
            self.pointer.right_click_at(x, y)
            now, ok, plate1 = settled()
            used = 'right-click'
            # THE NAME IS NOT THE VERDICT HERE and the plate is why this line
            # exists: `was`/`now` come from the plate TEMPLATE, which answers
            # None both for an empty rack and for a gun it cannot name — so a
            # drop that never happened on an unrecognised weapon reads as a
            # clean one. The ink separates them (PLATE_INK_MIN), and it is in
            # the journal even though this method still decides by the name.
            # kind='drop', NOT 'click', and no `gun_lost` — losing the weapon
            # is the request here. `gun_lost` stays greppable for the accidents
            # only; a field that means two opposite things is worse than none.
            self._journal_click(at_gun(gun), at_ground(), 0, ok, kind='drop',
                                plate=[plate0, plate1], was=was, now=now,
                                via='right-click')
            if not ok and gesture == 'auto':
                self._log(f'gun{gun}: right-click left the plate reading '
                          f'{now!r} — falling back to the drag')
        if not ok and gesture in ('auto', 'drag'):
            # The drag writes its own 'drag' line from inside drag(); this one
            # is the OUTCOME, which that line cannot carry — the row count it
            # verifies against says something arrived on the floor, not that
            # the rack emptied.
            rec = self.drag(at_gun(gun), at_ground(), retries=retries)
            now, ok, plate1 = settled()
            used = 'drag'
            self._stamp('drop', at_gun(gun), at_ground(), gesture=True,
                        moved=ok, plate=[plate0, plate1], was=was, now=now,
                        via='drag', failed_at=None if ok else 'rack not empty')

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

        "IS THERE A GUN HERE" IS THE INK, NOT THE NAME. This asked
        _read_guns() until 2026-08-03, which OCRs the name plate and returns
        None for anything outside the live roster or anything whose template
        has drifted -- and a None slot was SKIPPED. So the guns this could not
        name were exactly the guns it would not clear, and the template
        collector that exists to fix unreadable plates was the first caller to
        be bitten: it cleared the rack, believed it, and racked its new pair on
        top of whatever the OCR had failed to see.

        plate_ink has no such failure mode. It counts near-white achromatic
        pixels: 0 on an empty row, 679-901 with a gun, across 19 measured
        samples including one frame carrying both.
        """
        out, did = [], []
        for g in guns:
            if self.plate_ink(g) < PLATE_INK_MIN:
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

    def clear_inventory(self, retries=1, passes=4):
        """Put everything in 库存 on the floor. -> BATCH record.

        The mirror of clear_ground(), and it exists because a FULL 库存 stops
        the spawner delivering anything at all. That failure is silent from the
        outside: the spawner reports a successful click (it only ever means the
        cursor reached the right entry), the item never arrives, and every read
        afterwards describes a screen nothing happened on. A template collector
        spent three runs photographing an empty rack that way.

        Same shape as clear_ground for the same measured reason: the panel is a
        12-row WINDOW, not the pack, so rows below scroll up as the ones above
        leave and "the count went down" is not a stop condition. "The visible
        rows did not change" is. Always drags the TOP row, which is the only
        index that stays valid without re-reading between gestures.

        Whether the drop lands is read back by the row count, not by watching
        the cursor -- 'the drag missed' and 'it landed and the window refilled
        from below' look identical mid-gesture.
        """
        out = []
        rows = None
        for _ in range(passes):
            view = self.look()
            rows = view.rows('inventory')
            if not rows:
                break
            before = tuple((getattr(i, 'key', None) or '?') if i is not None
                           else '-' for i in view.inventory)
            for _ in range(rows):
                rec = self.discard(at_inv(0), retries=retries)
                out.append(rec)
                if not rec['ok']:
                    break
            after = self.look()
            rows = after.rows('inventory')
            if not rows:
                break
            cur = tuple((getattr(i, 'key', None) or '?') if i is not None
                        else '-' for i in after.inventory)
            if cur == before:
                return batch(out, error=f'{rows} row(s) left and the panel did '
                                        f'not change — the drops are not '
                                        f'landing', rows_left=rows)
        return batch(out, error=None if not rows else
                     f'{rows} row(s) still in 库存 after {passes} passes',
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
        # AN AMBIGUOUS READ IS NOT A FAILED FIT — RE-READ IT AGAINST ANOTHER
        # BACKDROP. The Tab panel is translucent, so a slot icon is composited
        # over whatever the world is showing behind it, and a dark backdrop
        # collapses the margins between neighbouring parts. Measured 2026-08-05
        # on a vector wearing ext_smg, same gun, same slot, one turn apart:
        #
        #     dark backdrop      best quick_smg  mse 267.7   margin 1.021
        #     six other views    best ext_smg    mse  88..164  margin 1.67..2.74
        #
        # The part was on the gun the whole time. `kit_faults` said "a retry
        # cannot improve a reading" and that is exactly what a retry does here
        # — the same nudge `GunDriver.ensure_posture` already uses when the
        # posture icon will not read, and for the same reason: move what is
        # BEHIND the thing, then ask again.
        #
        # This cost eleven cells of the 2026-08-05 factorial. Every vector
        # config and three mp5k configs were abandoned as "would not take
        # ext_smg" while the magazine was fitted correctly.
        #
        # Only the UNVERIFIABLE faults are retried. A slot that reads a
        # different part by name is a real disagreement and re-reading it says
        # nothing new.
        for _ in range(AMBIGUOUS_REREADS):
            if not any(not b['verifiable'] for b in out['bad']):
                break
            if not self._nudge_backdrop():
                break
            out['worn'] = self.read_slots(gun)
            out['bad'] = kit_faults(want, out['worn'])
        out['ok'] = not out['bad']
        if not out['ok'] and out['error'] is None:
            out['error'] = '; '.join(f'{b["slot"]}: {b["why"]}'
                                     for b in out['bad'])
        for b in out['bad']:
            self._log(f'gun{gun}.{b["slot"]}: {b["why"]}')
        return out

    def _nudge_backdrop(self, counts=NUDGE_COUNTS):
        """Move what is behind the translucent panel. -> True if it moved.

        Yaw only, and small. The Tab screen stays up: PUBG keeps rendering the
        world behind it and a mouse move still turns the view, which is the
        whole point — the icons do not change, their backdrop does.

        ⚠ WHILE TAB IS UP, RAW COUNTS LAND ON THE CURSOR, not only on the view
        (control/CLAUDE.md: move(900,0) with Tab open drifts the cursor 450 px
        over the following second). So this is deliberately small and every
        caller that clicks afterwards goes through Pointer.place(), which
        replays SetCursorPos until the position reads back.
        """
        mouse = self.pointer.pico
        if mouse is None:
            return False
        mouse.move(counts, 0)
        time.sleep(NUDGE_SETTLE_S)
        return True

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

    def point_of(self, loc, from_y=None):
        """Where to press or release for a location.

        `from_y` is the y of the point the gesture STARTS at, used only for a
        panel release: 附近 takes the item anywhere past the divider, so the
        shortest gesture is straight left at the height it was grabbed.
        """
        loc = as_loc(loc)
        if is_gun(loc):
            return gun_tag_point(loc[1])
        if is_slot(loc):
            _, gun, slot = loc
            return att_slot_point(gun, slot)
        kind, row = loc[0], (loc[1] if len(loc) > 1 else None)
        if row is None:
            # "Anywhere in this panel" is a RELEASE point, not a row.
            #
            # For 库存 that has to be the measured constant. Releasing on a
            # row there put the item on the FLOOR instead of in the pack --
            # twice, cleanly reproduced -- and DROP_XY exists because of it.
            #
            # 附近 IS DIFFERENT AND THE DIFFERENCE IS ONLY IN X. The panel is
            # the floor, so anywhere past the divider is the request; a release
            # only has to CROSS it. 附近 ends at 880 and 库存 starts at 907, so
            # 870 is the first column inside, and the gesture from 库存 row 0
            # is 104 px instead of DROP_XY's 437 — a quarter of the travel on a
            # drag clear_inventory performs twelve times in a row.
            #
            # Y IS FREE, which took three wrong answers to establish. Measured
            # in game 2026-08-04, always reading back after every single drag:
            #
            #   (870, y of the grabbed row) onto an OCCUPIED 附近 row   5/5
            #   (870, y of the first EMPTY row)                        6/6
            #   (744, 570) the old fixed point                        21/21
            #
            # An earlier sweep had "release on an occupied row" failing 1 in 3
            # and a whole theory was built on it. The theory was an artefact of
            # how it was measured: those drags were fired CONSECUTIVELY with
            # one read at the end, and consecutive drags fail for a reason that
            # has nothing to do with where they land — see DROP_WAIT. Same
            # points, read back one at a time, land every time.
            #
            # So this returns the row's own y, which is what a person does:
            # 34 recorded human drags were horizontal to within 10-33 px, and
            # not one of them was aimed at an empty row.
            if kind == 'nearby':
                return (NEARBY_DROP_X, from_y if from_y is not None
                        else DROP_XY[kind][1])
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
        names = self.name_template.classify(crops)
        return {g: (n if n in ROSTER else None) for g, n in zip(GUNS, names)}

    def _slot_states(self, frame):
        """{1: {slot: template name}, 2: {...}}, '' for an empty slot."""
        worn = self.items.read_weapons(frame, self.guns)
        return {g: {s: (it.asset if it is not None else '')
                    for s, it in slots.items()}
                for g, slots in worn.items()}

    def _stamp(self, kind, src, dst, attempt=0, **fields):
        """The half of a journal line that every gesture has. See DRAG_LOG.

        `gap_s` is the point of the whole exercise. "Sometimes the second drop
        does not land" is a claim about SEQUENCE, and the only way a log can
        answer it is by carrying how long ago the previous gesture ended — a
        record of one gesture can never show what a run of them does.

        SINCE THE PREVIOUS GESTURE, not the previous drag, and that widening
        is the point of stamping clicks too: the collector's drags are
        separated by equips, weapon switches and panel toggles, and a gap that
        only counted drags described a sequence that never happened.
        """
        now = time.perf_counter()
        prev = _LAST_GESTURE_END[0]
        rec = {'kind': kind, 't': round(time.time(), 3), 'pid': PID,
               'proc': PROC,
               'src': None if src is None else loc_str(src),
               'dst': None if dst is None else loc_str(dst),
               'attempt': attempt + 1,
               'gap_s': None if prev is None else round(now - prev, 3)}
        rec.update(fields)
        journal(rec)
        _LAST_GESTURE_END[0] = time.perf_counter()

    def _journal(self, src, dst, p0, p1, panels, attempt, rows0, gesture,
                 moved):
        """One line for a drag: the gesture, the geometry and the outcome."""
        # Pointer.__init__ creates last_drag as {} and drag() only ever
        # reassigns it to a dict, so neither a getattr default nor an `or {}`
        # can fire. Reading it directly is also the thing that would BREAK
        # loudly if that ever stopped being true, which is the point: the
        # guard's only real effect was to make a missing recorder look like a
        # drag with no geometry.
        d = self.pointer.last_drag
        self._stamp(
            'drag', src, dst, attempt,
            want={'grab': list(p0), 'release': list(p1)},
            got={'grab': d.get('grab'), 'held': d.get('held'),
                 'release': d.get('release')},
            place={'grab': d.get('grab_place'), 'dst': d.get('dst_place')},
            steps=d.get('steps'), drag_s=round(d.get('s') or 0.0, 3),
            gesture=bool(gesture), failed_at=d.get('failed_at'),
            rows_before=list(rows0) if rows0 else None,
            poll=self.last_poll if moved is not None else None,
            moved=moved)
        self.last_poll = None

    def _journal_click(self, src, dst, attempt, moved, checks=None,
                       plate=None, kind='click', **extra):
        """One line for a right click. `moved` is the READBACK, not the click.

        A right click always "succeeds" — the button goes down and comes up
        wherever the cursor happens to be — so the only interesting fields are
        where it went and what changed. `place.grab.ok` false means the cursor
        would not stay put and the click landed somewhere unverified, which on
        a slot-aimed gesture is the state that drops the gun.
        """
        c = self.pointer.last_click          # see _journal: always a dict
        pl = c.get('place') or {}
        self._stamp(
            kind, src, dst, attempt,
            want={'grab': list(c.get('want') or ())},
            got={'grab': c.get('got')},
            place={'grab': pl},
            gesture=True,
            failed_at=None if c.get('ok', True) else 'cursor would not stay',
            moved=moved, checks=checks, plate=plate, **extra)

    def _journal_refusal(self, kind, src, dst, why, **extra):
        """A gesture this layer declined to send. See DRAG_LOG on `refused`.

        Logged for the same reason a near-miss is logged in aviation: the
        guards here (an empty slot, an unreadable slot, a move that is not in
        MOVES) each stand in front of a failure that costs a weapon, and a run
        that ends with fewer parts than it started needs to show which guard
        fired as much as which gesture went out.
        """
        self._stamp(kind, src, dst, gesture=False, moved=None,
                    failed_at=why, **extra)

    def _plate(self, gun, frame=None):
        """Name-plate ink for `gun`, or None if it cannot be read right now.

        The one number that answers "is the gun still there" without a
        template — see PLATE_INK_MIN. Journalled either side of any gesture
        aimed at a gun or one of its slots, because `右键完枪没了` is exactly a
        plate going 679-901 -> 0 and nothing else in a record shows it.
        """
        try:
            return int(self.plate_ink(gun, frame))
        except Exception:
            return None

    def _await_panel(self, panels, n_src0, n_dst0, timeout=VERIFY_TIMEOUT):
        """Poll until a row leaves the source list or arrives in the target.

        Either direction counts; see the comment in drag(). Polling rather
        than one sleep for the same reason every other check here polls: the
        item animates, and the worst case is much longer than the usual one.
        """
        src_p, dst_p = panels
        end = time.time() + timeout
        # Every reading, not just the verdict. A drop that lands at 0.9 s and
        # one that never lands both return through the same boolean, and the
        # difference between "too slow" and "did not happen" is only visible
        # in the sequence. Read back off `poll` in the drag journal.
        self.last_poll = []
        t0 = time.time()
        while True:
            f = self._frame()
            n_dst = panel_rows(f, dst_p)
            n_src = panel_rows(f, src_p) if src_p is not None else None
            self.last_poll.append([round(time.time() - t0, 3), n_src, n_dst])
            if n_dst > n_dst0:
                return True
            if n_src is not None and n_src < n_src0:
                return True
            if time.time() >= end:
                return False
            time.sleep(VERIFY_POLL)

    def _await(self, checks, before, timeout=VERIFY_TIMEOUT):
        """Poll the weapon slots until every check passes, or time runs out.

        Returns the CHECK RECORDS themselves — [{'gun', 'slot', 'want', 'ok',
        'seen'}, ...] — and not the tuples it used to. All three callers
        (`drag`, `right_click_equip`, `right_click_unequip`) immediately
        rebuilt exactly this dict from those tuples, in three verbatim copies,
        and the shape is not private to them: it goes into `rec['checks']`
        that callers read, into `_checks_str`, and into the journal. A
        conversion that every caller performs identically is not a
        conversion, and a fourth caller inventing its own key names would
        break `_checks_str` and the journal at once.

        ANY_ITEM additionally demands the slot differ from `before`: dropping
        onto a slot that already reads as *something* would otherwise pass on
        the strength of what was there before the drag, so a swap that never
        happened would report success.
        """
        deadline = time.perf_counter() + timeout
        while True:
            states = self._slot_states(self._frame())
            out = []
            for gun, slot, want in checks:
                seen = states[gun][slot]
                ok = (seen != '' and seen != before[gun][slot]
                      if want == ANY_ITEM else seen == want)
                out.append({'gun': gun, 'slot': slot, 'want': want,
                            'ok': ok, 'seen': seen})
            if all(r['ok'] for r in out) or time.perf_counter() >= deadline:
                return out
            time.sleep(VERIFY_POLL)

    @staticmethod
    def _checks_str(checks):
        out = []
        for c in checks:
            # Two shapes: a slot readback, and the row count of a panel drop.
            if 'panel' in c:
                src, dst = c['panel']
                out.append(f'{src}/{dst} rows were {c["from"]}')
                continue
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

