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
    end up empty. Panel-to-panel drags are checked by COUNTING ROWS
    (panel_counts), so rec['verified'] is True for those too.

    ⚠ That last sentence used to read "cannot be checked here at all ... it is
    on the caller to re-detect", and it stayed for as long as it took someone
    to notice: the row-count check landed 2026-08-04, and a docstring saying a
    result is unverified when it is verified sends the caller to re-do work
    that is already done — the opposite of the failure this file usually has,
    and just as expensive.

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
from capture.cropper import capture_screen, win32_cap
from detector.tab_detector import TabTypeDetector
from detector.tab_items import (TabGrabber, TabItemDetector, panel_rows,
                               _BY_ASSET)
from detector.tab_layout import (DROP_XY, INV_ROWS, PARK_XY, att_slot_point,
                                 gun_tag_point, row_point)
from detector.weapon_template_detector import TabWeaponDetector
from press.pico_mouse import HID_KEY_1, HID_KEY_2, HID_KEY_TAB
from control.driver import Driver
from control.focus import game_focused, ensure_focus

# ⚠ RE-EXPORTED, NOT JUST IMPORTED. Roughly forty call sites across
# calibration/ and tools/ say `from control.inventory import at_inv, at_gun`,
# and the split that moved these out (2026-08-08, this file was 3776 lines)
# is not a reason to touch any of them. The names live in control/locations.py
# now; reaching for them here still works and is not deprecated -- a driver
# handing out the vocabulary it drives with is the useful shape.
from control.locations import (                                  # noqa: F401
    ANY_ITEM, EMPTY, GUNS, MOVES, PANEL_KINDS, as_loc, at_ground, at_gun,
    at_inv, at_slot, is_gun, is_slot, kind_of, loc_str, move_info,
    panel_counts, parse_loc)
# Same reason, same day: the planners were the other pure half. harvest.py
# imports slot_matches from here and stays working.
from control.kit_plan import (                                   # noqa: F401
    kit_faults, loose_items, plan_equip, plan_kit, slot_matches)
# ⚠ PRIVATE AND STILL IMPORTED, because right_click_equip and _kit_run both
# sort candidate sources with it. `pixi run names` caught the two NameErrors
# the split left behind before either of them could reach a run -- which is
# the whole argument for that gate: an undefined name in a rarely-taken branch
# is a crash scheduled for whenever the branch is taken, and this one sits in
# the retry path of the gesture that fits attachments.
from control.kit_plan import _src_rank
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
# retry could not hide anything (measured by a scratch script that is no
# longer on disk; the numbers below are what it left):
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
#     0.05  2/3     0.15  2/3     (a scratch sweep, no longer on disk — this table is what it left)
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

# WHERE A HAND LETS GO, measured 2026-08-09: eight drags recorded at 1 kHz
# (tools/record_drag.py, kept in calibration/artifacts/drag/human/) released at
#
#     604  664  682  686  689  736  769  800        median 689
#
# 附近 spans 565..880, so a hand lands in the middle of it and NOT ONE of the
# eight came within 80 px of the right edge.
#
# ⚠ THIS REPLACES 870, WHICH WAS OUTSIDE THAT DISTRIBUTION. The reasoning for
# 870 was "附近 ends at x=880 and 库存 starts at 907, so this is the first
# column inside the target panel: crossing the divider is the whole
# requirement" — true as stated, and it bought a 104 px gesture instead of the
# 437 px one from DROP_XY. What it never checked is whether the first column
# inside a panel is as good as the middle of it, and the answer is that no
# hand ever tries. 682 is the release of the replayed recording
# (press/pointer.py HUMAN_DRAG_PATH); the median of all eight is 689 and the
# 7 px between them is not a claim.
#
# ⚠ IT IS NOT SUPPORTED BY A WIN. Back to back on the same path minutes apart,
# 12/12 with zero retries at 870 and 12/12 with zero retries at 682 — a tie,
# because the per-drag readback that makes each drag visible also puts ~123 ms
# between them, and that gap is itself known to fix drags (see DROP_WAIT).
# What 682 has is that it is where a hand goes and 870 is not, which is a
# different kind of evidence from a scoreline and worth less than one.
#
# ⚠ WHAT WOULD ACTUALLY SEPARATE THEM has not been run: bursts of twelve with
# NO read in between, which is the shape clear_inventory has and the shape the
# unexplained ~20% of non-landings live in. tools/show_drag.py has burst() for
# it. Until that runs, this is a better-motivated guess, not a fix.
NEARBY_DROP_X = 682

# Gesture timing handed to Pointer.drag. Defaults are press/pointer.py's, i.e.
# what shipped before anyone measured them. Whether they can come down is a
# sweep with a read-back at every step, not a guess.
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
    __file__))), 'calibration', 'artifacts', 'drag', 'journal.jsonl')

# Tab is a toggle and swallows 1/2 while it is up, so hold() has to close it,
# switch, and reopen. That used to be two fixed 0.45 s sleeps.
#
# Measured 2026-08-02 (tools/probe_toggle_latency.py, 8 cycles): the weapon
# panel is fully readable 33-38 ms after the key, and the 类型 anchor is gone
# 77-128 ms after it. So 0.45 was 4-13x what the game needs, and both waits
# are now polls -- is_tab_open() costs 3-6 ms a pass, which is cheaper than
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
# ⚠ ON, AND MEASURED BOTH WAYS. Switched off on 2026-08-07 and back on the
# same night, because the run settled it:
#
#     slot reads the templates could not separate
#       park ON  (runs 12, 13) : 0 and 0
#       park OFF (run 18)      : 4, across three weapons
#
# What each one starts: an unreadable slot reads as "did not land", ensure_kit
# records a false strike against a part the gun does take, the config is
# skipped, and the weapon is thrown WITHOUT BEING MEASURED. From outside that
# is a collector swapping attachments and binning guns forever, which is how
# it was reported.
#
# WHAT STAYED OFF, because the rule is per-DETECTION: panel_rows (Laplacian
# occupancy) at both drag sites, and the spawner's classify() (button icons).
# Neither can be fooled by a tooltip. Originally turned off 2026-08-07
# at the operator's call, after watching it: park() threw the cursor to
# PARK_XY (200, 1380) -- the bottom-left corner -- before reads, including in
# the instant after a drag released, and the trip back across the screen is
# the stretch where Pointer.place fights the game's cursor drift.
#
# WHAT IT WAS FOR, so the failure is recognisable rather than mysterious: a
# hovered slot tile draws a TOOLTIP over itself, and slot reads are template
# matches. With this off, a slot the cursor happens to be sitting on can read
# `?` instead of its part. ensure_kit treats `?` as "did not land", retries,
# and can burn a cell -- that is the shape of the 11 AMBIGUOUS cells on
# 2026-08-05, and control/CLAUDE.md's "读不出来 ≠ 没装上" section is the same
# subject.
#
# ⚠ SO THE THING TO WATCH IS `reads '?'` IN THE KIT LOG. If those appear,
# this is the first suspect and one line puts it back. The mitigation already
# in place is AMBIGUOUS_REREADS: an unreadable slot is re-read after a small
# view nudge, bounded at two, which is a weaker guard than not hovering in the
# first place but is not nothing.
PARK_BEFORE_READ = True

PARK_SETTLE = 0.06      # cursor off the slot -> tooltip gone, before a read

# Releasing over a panel means "put it in this container", and WHERE inside it
# is not a row -- see tab_layout.DROP_XY, measured by holding a drag until the
# game drew its dashed accept-region. The guess that used to be made here
# (row 0, or the first empty row) is what dropped parts on the floor while
# reporting success.
#
# ⚠ `DEFAULT_DROP_ROW = 0` stood here until 2026-08-07 with zero readers, and
# the sentence that had replaced this one is why: it said the constant
# "survives only because set_rows()/DEFAULT_DROP_ROW is still the story for
# pick-UP points". set_rows() is indeed the story and has three callers.
# DEFAULT_DROP_ROW had none -- the true half carried the dead half. A comment
# asserting that something is still used is not evidence that it is.

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
# ⚠ RAISED FROM 2 ON 2026-08-07, once the nudge started working. Until that
# day _nudge_backdrop moved nothing (Tab was up, so the raw counts landed on
# the cursor -- 0.29 against a noise floor of 0.32), so this number was the
# count of times the SAME picture got re-read and any value would have done.
#
# With a real backdrop change behind each retry the number finally means
# something, and 2 is too few for the tightest pair in the corpus: mp5k's
# ext_smg against quickext_smg lost a cell to `templates cannot separate` on
# the very next run, having survived two nudges. calibration/artifacts/recoil's own measurement
# says one view in seven is ambiguous for that pair, so three views leave ~0.3%
# and five leave ~0.006%.
#
# A nudge is a Tab close, 600 counts of yaw and a Tab open, call it 1.5 s. The
# thing it is spending that against is a whole cell -- 25 magazines and its
# kitting -- so the trade is not close.
AMBIGUOUS_REREADS = 4
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
# ⚠ AND AN UPPER BOUND, because the low one only separates "gun" from "empty"
# and there is a third thing this crop can be showing: the SPAWNER PANEL, which
# is drawn over the Tab screen. Measured off calibration/artifacts/drag/journal.jsonl on
# 2026-08-07, the same field on the same gestures:
#
#     empty slot                        0
#     a gun's name plate           679 - 901   (19 samples)
#     the spawner panel over it  10941 - 11250
#
# An order of magnitude clear of the plate range, so the bound is not a
# tuning question. It matters because the reading is CONFIDENT and WRONG in a
# direction that passes PLATE_INK_MIN: clear_rack sees 11250, decides there is
# a gun, drops it, gets `moved=False` because the click went into the panel,
# and returns success with nothing dropped. Three cells in a row then refused
# themselves for the magazine the clear was supposed to remove.
#
# Same shape as the two other screen-confusions this repo has paid for -- Tab
# open while sending raw counts, a latency probe tapping an empty gun -- and
# the same rule: a real reading of the wrong screen is the failure that looks
# most like success. control/CLAUDE.md states it; this is it in a number.
PLATE_INK_MAX = 2000

def _calling_frame():
    """The project frame that asked. -> 'file.py:line func' | None

    Skips this module AND the standard library. The first cut skipped only
    inventory.py and duly reported `contextlib.py:137 __enter__` for every
    churn that came through a `with` block -- a true statement naming nothing
    anybody can go and fix.

    ⚠ MODULE LEVEL, not a staticmethod, and that is not style. As a
    staticmethod it re-binds when copied onto another class, which is exactly
    what a test double does -- the offline check of this very function died on
    `takes 0 positional arguments but 1 was given`. A helper whose test cannot
    call it the way production does is a helper with no test.
    """
    import traceback, os
    skip = ('inventory.py', 'contextlib.py')
    for fr in reversed(traceback.extract_stack()[:-2]):
        base = os.path.basename(fr.filename)
        if base in skip or f'{os.sep}lib{os.sep}' in fr.filename.lower():
            continue
        return f'{base}:{fr.lineno} {fr.name}'

# Control
# ════════════════════════════════════════════════════════════

class InventoryControl(Driver):
    """Drag attachments between the ground, the backpack and the two guns."""

    def __init__(self, verbose=True):
        # ⚠ WAS `self.pointer = Pointer(backend)`, and the comment eight lines
        # below already stated the rule it broke -- "_slots is built on first
        # use ... it is the same rule the Pointer follows -- do not construct
        # what this instance may not need". The Pointer was not following it.
        # 41 construction sites, and the read-only ones (loadout, survey,
        # slot_states) took the shared serial port to look at a screenshot.
        super().__init__()
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
        # When Tab was last closed, or None once anything real has
        # happened since. See _churn.
        self._since_close = None
        # Overrides passed straight to Pointer.drag — the gesture's timing.
        # Every calibration run reaches the Tab screen through here, so these
        # are worth measuring rather than guessing: sweep them and take the
        # fastest setting that still READS BACK, never the fastest that the
        # gesture reports ok for.
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
        """Release the GDI objects TabGrabber holds open, and the Pointer.

        ⚠ Now reachable as `with InventoryControl() as ac:`. It had no
        __enter__/__exit__ at all, so all 41 construction sites hand-rolled
        their own try/finally -- and a hand-rolled one is a try/finally
        somebody can forget, which is a different failure from getting it
        wrong: nothing reports it.
        """
        super().close()
        self.grabber.close()

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

    def is_tab_open(self):
        """R — Is the Tab inventory drawn? One 41x18 crop, 3-6 ms, and NOT a
        keypress: this reads the answer, ensure_tab() acts on it.

        ⚠ THE `is_` IS LOAD-BEARING. Named `tab_open()` it read as an
        imperative, and two collectors called it where they meant
        ensure_inventory_open() -- correct-and-useless, so each aborted before
        doing anything with a message about a panel that was never asked to
        open. Every R on this class that answers yes/no carries the prefix.

        ⚠ False is not "Tab is closed". The item-spawner panel hides the
        inventory AND swallows Tab, so False there means pressing cannot
        help — which is why ensure_tab asks _blocking_screen() first.
        """
        return bool(self.tab.classify({'type': win32_cap(HUD_REGIONS['type'])}))

    @contextlib.contextmanager
    def tab_up(self, restore=True):
        """L2 — Hold the Tab screen up for the block, and leave it as found.
        Nesting is free: already-open costs nothing, and only the call that
        opened it closes it. ensure_tab() is the L1 it presses with.

        ⚠ THE RESTORE IS WHERE THE CHURN COMES FROM — 20 of the journal's 85
        churn records are one harvest session closing on itself. Hold ONE
        across the whole flow; do not wrap each read in its own.

        The old way to be sure of the state was to force a known cycle: close
        it if it was open, open it, read, close it. Three keypresses, and with
        the sleeps those carried, 1.25 s to look at something that was very
        often already on screen -- two collectors did exactly that, and both
        are gone with the modules that held them.

        There is nothing to force. is_tab_open() answers the same question in
        3-6 ms, so this opens the screen only if it is shut and undoes only
        what it did. Already open is the free case, which is what makes it
        safe to wrap every read in.

        Yields True when the screen is up. A False must NOT be read as
        "nothing equipped": an empty slot is a legitimate answer, and the two
        would be indistinguishable downstream.
        """
        was_open = bool(self.is_tab_open())
        ok = was_open or self.ensure_tab(True)
        if not ok:
            self._log('Tab would not open')
        try:
            yield ok
        finally:
            if ok and restore and not was_open:
                self.ensure_tab(False)

    # WHAT EACH READ NEEDS, AS DATA. Every unnecessary gesture found on
    # 2026-08-06 came from the same shape: a prerequisite that was satisfied at
    # the CALL SITE instead of being declared, so each helper re-established it
    # as though it were the only reader. Tab was opened five times before the
    # first click of a weapon; the cursor was thrown to the bottom-left before
    # reads that could not be fooled by a tooltip.
    #
    #   tab   True  the reading only exists on the Tab screen
    #   park  True  the reading is a TEMPLATE MATCH over a region the cursor
    #               may be sitting on, and a hovered tile draws a tooltip over
    #               itself. False for judgements a tooltip cannot change:
    #               panel_rows is Laplacian occupancy, tab_open is saturation.
    #
    # Declared here so the union can be satisfied ONCE for a whole survey, and
    # so a new reader states its needs rather than inheriting whatever the
    # previous line happened to leave behind.
    READ_PREREQS = {
        # template matches -- a hovered tile draws a tooltip over itself and
        # the match then finds the tooltip. Measured 2026-08-07: with the move
        # off, four slot reads came back "templates cannot separate" in one
        # run against zero in the two before it, and each one skipped a cell.
        'guns':  {'tab': True, 'park': True},   # name plates
        'slots': {'tab': True, 'park': True},   # slot tiles
        'loose': {'tab': True, 'park': True},   # row items
        'plate': {'tab': True, 'park': True},   # name-plate ink
        'tiles': {'tab': True, 'park': True},   # HUD attachment tiles
        # judgements a tooltip cannot change
        'rows':  {'tab': True, 'park': False},  # Laplacian occupancy count
    }

    # What survey() knows how to answer. Derived from the table above so the
    # two cannot drift: a kind with no declared prerequisites is not a kind.
    SURVEY_KINDS = tuple(READ_PREREQS)

    def _frame_for(self, *kinds):
        """A frame fit for reading `kinds`. -> the grabber's crops

        THE READ DECLARES, THIS DECIDES. Every call site used to pass
        `park=True/False` itself, which put a detection property in the hands
        of whoever happened to be grabbing -- and the default quietly carried
        it for fourteen of them. Asked for on 2026-08-07: "park 应该放到检测
        逻辑里".

        Behaviourally this changed nothing on the day it landed: an audit of
        all sixteen grabs found every one except the two panel_rows sites was
        a template match, so `park=True` had been right for them by accident.
        What it changes is that a NEW read states what it is, instead of
        inheriting whatever the previous line left behind.
        """
        bad = [k for k in kinds if k not in self.READ_PREREQS]
        if bad:
            raise ValueError(f'unknown read kind(s) {bad}; '
                             f'known: {list(self.READ_PREREQS)}')
        return self._frame(park=any(self.READ_PREREQS[k]['park']
                                    for k in kinds))

    def survey(self, *what, frame=None):
        """Everything the caller asked for, off ONE screen opening. -> dict

            s = ac.survey('guns', 'slots', 'loose')
            s['guns']    {1: 'ace32', 2: None}
            s['slots']   {1: {'muzzle': asset, ...}, 2: {...}}
            s['loose']   a TabView -- both left panels, backpack and ground

        THE CALLER SAYS WHAT IT WANTS TO KNOW; THIS DECIDES HOW MANY SCREEN
        VISITS THAT TAKES. Asked for on 2026-08-06 in exactly those terms:
        "需求方告诉你我要检测什么,然后 detector 那儿决定要不要合并检测 ...
        不能检测一个开关一次 tab".

        It costs one tab_up() and one grab no matter how much is asked for,
        because the whole Tab screen is two blocks and `frame()` already
        returns both: name plates and all ten slots are tab_blocks()['right'],
        the backpack and ground lists are ['left']. Everything below reads the
        SAME pixels -- which is also why it is more correct than separate
        calls, not just cheaper. Two reads of a screen being dragged on are
        two different screens, and nothing downstream can tell.

        Measured before this existed: 1836 real Tab key presses across the
        shared journal, 184 blocks of four or more consecutive toggles with no
        gesture between them (1477 presses, 80% of all of them). The commonest
        shape was literal alternation -- OCOCOOCOCOO -- one open-read-close per
        helper, five helpers deep, before the first click of a weapon. No
        helper was wrong on its own; there was simply nobody holding the
        screen, so each opened it as if it were the only reader.

        `frame` accepts one the caller already grabbed, same contract as
        look(): a caller holding a captured frame and letting this grab its own
        gets an answer about a different instant.
        """
        bad = [k for k in what if k not in self.SURVEY_KINDS]
        if bad:
            raise ValueError(f'survey: unknown kind(s) {bad}; '
                             f'known: {list(self.SURVEY_KINDS)}')
        want = set(what) or set(self.SURVEY_KINDS)
        with self.tab_up() as ok:
            if not ok:
                return None
            # ONE grab for the whole survey, parked only if something being
            # read actually needs it. See READ_PREREQS.
            need_park = any(self.READ_PREREQS[k]['park'] for k in want)
            frame = (self._frame_for(*want) if frame is None else frame)
            # Always read, never conditional: _read_guns is what narrows the
            # template bank for both of the others, and it is a dict lookup
            # off a frame that has already been grabbed.
            self.guns = self._read_guns(frame)
            out = {}
            if 'guns' in want:
                out['guns'] = self.guns
            if 'slots' in want:
                out['slots'] = self._slot_states(frame)
            if 'loose' in want:
                out['loose'] = self.look(frame=frame)
        return out

    def loadout(self, gun=None):
        """What the guns are, and what they are wearing. Opens Tab if needed.

        -> {'guns': {1: key, 2: key}, 'slots': {1: {slot: asset}, 2: {...}}},
        or None if the screen never came up. With `gun`, just that one's dict.

        A thin shape over survey(), which is where the one-opening rule lives.
        Kept because a caller that wants exactly this reads better for it.
        """
        s = self.survey('guns', 'slots')
        if s is None:
            return None
        out = {'guns': s['guns'], 'slots': s['slots']}
        return out if gun is None else {'gun': s['guns'][gun],
                                        'slots': s['slots'][gun]}

    def sync(self):
        """False unless the game is focused with the Tab screen up."""
        if not game_focused():
            self._log('game is not the foreground window')
            return False
        self.park()
        if not self.is_tab_open():
            self._log('Tab inventory is not open')
            return False
        return True

    def park(self):
        """Move the cursor off every interactive element before a read.

        ⚠ OFF BY DEFAULT SINCE 2026-08-07 — see PARK_BEFORE_READ. One constant,
        one line to put back.
        """
        if not PARK_BEFORE_READ:
            return
        if self.pointer.cursor_pos() == PARK_XY:
            return
        self.pointer.move_to(*PARK_XY)
        time.sleep(PARK_SETTLE)

    def frame(self):
        """One Tab-screen frame, cursor parked. -> the grabber's crops.

        Public because callers need it and were taking it anyway: a collector
        that photographs the screen and then wants it READ has to hand the
        same pixels to both, or it is describing one frame with another
        frame's answer. calibration/legacy_collect_templates.py reached through to
        `ac.grabber.grab()` for exactly this, which skips park() — and a
        hovered slot draws a tooltip over itself.
        """
        return self._frame_for('loose')

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
        frame = self._frame_for('guns', 'loose') if frame is None else frame
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
        return self._read_guns(self._frame_for('guns') if frame is None else frame)

    def plate_ink(self, gun, frame=None):
        """White-text pixels on gun `gun`'s name plate. -> int

        Not a name. It answers "is a plate drawn here at all", which the OCR
        cannot be asked, because on this screen the OCR is the thing being
        checked. See TabWeaponDetector.ink: with the rack emptied first, zero
        ink to some ink is a weapon ARRIVING, established without consulting
        any template, and that is the missing half of labelling a plate
        capture with the weapon that was requested.
        """
        frame = self._frame_for('plate') if frame is None else frame
        y, x, h, w = HUD_REGIONS[f'gun_name_{gun}']
        return self.name_template.ink(frame[y:y + h, x:x + w])

    def plate_state(self, gun, frame=None):
        """What this crop is SHOWING. -> 'panel' | 'gun' | 'empty'

        THE ONE HOLDER OF BOTH THRESHOLDS. clear_rack and drop_weapon each
        used to compare `plate_ink` against constants themselves, and they did
        not compare it against the same ones: clear_rack tested both bounds,
        drop_weapon tested neither and decided by the name template instead.
        The gap is what 2026-08-07 19:10 and 19:11 cost — see PLATE_INK_MAX.

        ⚠ THIS DOES NOT MERGE THE TWO CALLERS' DECISIONS, only the reading
        they both make, and the difference is worth stating because it is why
        `plate_ink` is still called directly nowhere else:

            'panel'   the same question for both — "am I even looking at the
                      rack?" — and the same answer, refuse. Mergeable, merged.
            'gun'/'empty'
                      NOT the same question. clear_rack asks "is there a gun
                      to drop" (presence, and ink is the right ruler because
                      the OCR is what fails). drop_weapon asks "is THIS gun
                      still there afterwards" (identity, which ink cannot
                      answer — 800 ink says a gun, not which one).

        So one reading, one classification, two decisions. Collapsing the
        second pair as well would make an unnamed gun that DID leave read as
        a clean drop, which is the failure this layer's plate rules exist to
        keep apart.
        """
        ink = self.plate_ink(gun, frame)
        if ink > PLATE_INK_MAX:
            return 'panel'
        return 'gun' if ink >= PLATE_INK_MIN else 'empty'

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
        frame = self._frame_for('loose') if frame is None else frame
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
        out = self._slot_states(self._frame_for('slots') if frame is None else frame)
        return out if gun is None else out[gun]

    # ── The primitive ──

    def drag(self, src, dst, want=None, retries=1, weapon=None, verify=True):
        """L0 — Drag whatever is at `src` onto `dst`. It DOES read back and
        retry; what it does not carry is the guards. equip()/unequip() do.

        ⚠ Into a weapon slot the drag measured 0/10 and never lands — that
        edge is not slow, it is shut. ⚠ The empty-slot check here runs only
        from the SECOND attempt, so the first gesture is covered by
        unequip()'s guard and by nothing else. And never substitute
        ac.pointer.drag: _reject() is the only thing on that release.

        want     what the destination slot should read as afterwards. Defaults
                 to ANY_ITEM when dst is a slot; ignored when it is a panel.
        weapon   ROSTER key of the gun `dst` belongs to. Given one, a drag
                 onto a slot that weapon does not have is refused before the
                 mouse moves — an attachment released over a slot that is not
                 drawn goes back where it came from, or onto the floor.
        verify   False to send the gesture and read nothing back. Exists for
                 exactly one caller — calibration/legacy_collect_templates.py, which
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
        before = self._slot_states(self._frame_for('slots')) if checks else None

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
        # What the source row held on the FIRST attempt, so a retry can tell
        # "still there" from "the list moved under me". See the guard below.
        src_key0 = None

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
            # When THIS attempt began, before any reading or gesture. See
            # _stamp: journalling happens after the poll, so a gap measured
            # there is a gap that contains its own outcome.
            t_begin = time.perf_counter()
            # Bound before the branch: the row identity is only readable when
            # there are panels to read, and an unbound name here would turn a
            # slot-destination drag into a NameError.
            src_key = '(no panel to read)'
            if panels is not None:
                # No park, same reason as _await_panel: this is panel_rows, a
                # Laplacian occupancy count, and a tooltip cannot change a row
                # count. Here it cost more than a wasted move -- park() throws
                # the cursor to PARK_XY (200, 1380) in the instant BEFORE the
                # grab, so every drag began by walking the cursor back across
                # the screen, which is exactly the stretch where Pointer.place
                # fights the game's cursor drift. Watched and reported by the
                # operator: "往左边拖东西的时候突然点了一下 ... 就拖不动东西".
                f = self._frame_for('rows')
                # ⚠ WHAT WAS ACTUALLY UNDER THE GRAB POINT. Every other field
                # on this record describes what WE did -- where the cursor was
                # placed, how many steps, how long the button was down -- and
                # on 2026-08-07 all of them came back identical for the 63
                # drags that missed and the 284 that landed: same tries, same
                # dy of 0, same 0.53 s, same 4 steps. Geometry is exonerated.
                #
                # That leaves two explanations that the journal cannot yet
                # tell apart: the game REFUSED a clean gesture, or there was
                # nothing at the grab point to move. This names the row.
                #
                # Read off the frame that was being taken anyway, so it costs
                # one template pass and no extra grab. Best effort: with
                # PARK_BEFORE_READ off the cursor may be over the row and the
                # tooltip can make it unreadable, which is itself worth
                # seeing -- an unreadable source row is not the same claim as
                # an empty one.
                # TabView keeps `inventory` and `nearby` as ROW-INDEXED
                # lists, so the row is a lookup, not a search. None means the
                # row read as empty; '?' means it was there and unreadable,
                # and those are different claims -- an unreadable row is a
                # detector problem, an empty one means the grab had nothing
                # to pick up.
                # Off `f`, the frame the row counts below are read from, so
                # this is one template pass and no extra grab. `_row_key`
                # answers '(not a row)' for a slot source; keep the
                # '(no panel to read)' this branch already set in that case,
                # since it says WHY there is no row rather than merely that
                # there is not one.
                got = self._row_key(src, frame=f)
                if got != '(not a row)':
                    src_key = got
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
            # ⚠ RETRY-SAFE: p0/p1 are reused across attempts, and that is only
            # sound while the address still resolves to the same thing. Two
            # guards make it so, one per address kind, and this is the second:
            #
            #   a SLOT source is checked above -- slots are addressed by name,
            #   so they cannot shift, but they can EMPTY, and a gesture on an
            #   empty slot drops the whole gun.
            #
            #   a ROW source is checked here -- rows are POSITIONAL. Removing
            #   a row scrolls everything below it up, so a retry at the same
            #   pixel grabs whatever slid into that place.
            #
            # The row half was the missing one, and it is not hypothetical:
            # `moved` is False for about 18% of drags that in fact landed
            # (the row poll cannot see an arrival when both lists are full,
            # among other things), and every one of those became a retry --
            # aimed, by then, at a list that had already shifted. Same shape as
            # right_click_equip's retry, which put the evicted magazine back on
            # the gun and got the combination written into kit_facts.json as
            # incompatible. Enforced by `pixi run gestures`.
            #
            # Refuse rather than re-aim: the callers that drag rows in bulk
            # (stock.tidy, clear_ground) already repeat until a pass moves
            # nothing, so declining one gesture costs a loop iteration, while
            # dragging the wrong row costs a part.
            if attempt and src_key0 and not str(src_key0).startswith('('):
                if src_key != src_key0:
                    rec['ok'] = True
                    rec['source_emptied'] = True
                    rec['error'] = (
                        f'{loc_str(src)} held {src_key0} on attempt 1 and '
                        f'reads {src_key!r} now — the list moved, so the '
                        f'previous attempt did something. Not dragging again '
                        f'at a stale row.')
                    self._log(f'{loc_str(src)} -> {loc_str(dst)}: source row '
                              f'changed {src_key0} -> {src_key} after attempt '
                              f'{attempt}; not retrying at a moved row')
                    self._journal_refusal(
                        'refused', src, dst,
                        f'source row changed {src_key0} -> {src_key} after '
                        f'attempt {attempt}',
                        by='retry guard', after_attempt=attempt)
                    return rec
            if not attempt:
                src_key0 = src_key
            gesture = self.pointer.drag(p0, p1, **self.timing)
            # The verdict is taken BEFORE the journal so that one line carries
            # the gesture and its outcome together — split across two lines,
            # a failed drop and a slow one are indistinguishable again.
            # Runs before the slot checks and gates them, because "the slot
            # emptied" is true whether the part reached the panel or the
            # floor, and only this can tell those apart.
            # ⚠ THE ROW POLL RUNS WHATEVER THE CURSOR SAID. It used to be
            # `if gesture and panels is not None`, and that one `and` is why
            # this layer could not answer its own oldest question.
            #
            # `gesture` comes from press/pointer.py, which compares the cursor
            # position AFTER the release (and after DRAG_DROP_WAIT) against the
            # drop point with PLACE_TOL = 2 px, on the stated assumption that
            # "SetCursorPos is exact". That assumption is FALSE while Tab is
            # up: raw counts land on the cursor and arrive over about a second
            # (see Pointer.place), so the check measures drift that accumulated
            # AFTER the item had already been dropped.
            #
            # Measured over the whole shared journal, 2026-08-06, labelling each
            # drag by the NEXT record's row counts:
            #
            #     said missed, actually LANDED   147     <- 98% of all failures
            #     said ok,     landed            104
            #     said ok,     actually MISSED    53     <- invisible to it
            #     said missed, actually missed      3
            #
            # So the cursor verdict was wrong 147 times out of 150 in the
            # failing direction, and it cannot see the real misses at all --
            # it never looks at the item. Every one of those 147 became a
            # retry, which is the "it drags the same thing over and over"
            # the operator kept seeing.
            #
            # And it poisoned the evidence as well as the behaviour: with the
            # poll skipped, `moved` stayed None and `poll` was journalled as
            # None, so the ONE field that records the outcome was absent from
            # exactly the records anybody would go read. Reconstructing the
            # truth needed the following record's rows_before. That is why the
            # investigation in this package's CLAUDE.md ran on mislabelled data.
            #
            # Rows are the verdict now; the cursor is a diagnostic (`failed_at`
            # and the got/held pair stay in the record). When rows CANNOT judge
            # -- both lists full, or no countable panel -- `moved` is None and
            # the old cursor-based refusal still stands, which is the right
            # fallback rather than believing a gesture nothing checked.
            moved = (self._await_panel(panels, n_src0, n_dst0)
                     if panels is not None else None)
            # ⚠ ONE READ ON THE NON-LANDING PATH, AND IT IS THE FIELD THIS
            # PACKAGE SPENT TWO INVESTIGATIONS WITHOUT. When `moved` is not
            # True, the record could not say whether the item had left; the
            # only way to find out was the NEXT record's `rows_before`, and
            # that reconstruction has three holes:
            #
            #   * the LAST drag of a burst has no next record at all
            #   * `moved=None` (both lists full, no countable panel) leaves
            #     the row counts unusable in both records
            #   * the retry guard above re-reads the row, but only when there
            #     IS a retry -- a single-attempt failure never got a second
            #     look
            #
            # 2026-08-08: of 749 floor drags, 199 said nothing about their
            # outcome, and 98% of those had in fact landed. Reconstructing
            # that needed 514 usable pairs out of 749 records; the other 235
            # are unanswerable forever.
            #
            # Reading the source row afterwards closes it in ONE record:
            #
            #   was `X`, now empty     the item left. A False verdict here is
            #                          the ROW COUNT being wrong, not a miss.
            #   was `X`, still `X`     it did not move. A real miss.
            #   was `X`, now `Y`       the list shifted, so something left.
            #
            # Only on the failing path, so the happy path pays nothing: one
            # template pass, and only when the alternative is a record that
            # cannot be interpreted at all.
            src_key_after = self._row_key(src) if moved is not True else None
            # `panels` is not passed: rows0 below already carries the only
            # thing the journal did with it -- the two row counts, or None
            # when there was no countable panel to take them from.
            self._journal(src, dst, p0, p1, attempt,
                          (n_src0, n_dst0) if panels is not None else None,
                          gesture, moved, started=t_begin,
                          src_key=src_key, src_key_after=src_key_after)
            if not gesture and not moved:
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
        """L1 — Put the attachment at `src` into weapon `gun`'s `slot`. One
        gesture at the row you name; ensure_kit() is the L2 that re-finds it.

        ⚠ gesture='drag' INTO a weapon slot has never landed — 0/10 against
        right-click 10/10 — so 'auto' takes the gun in hand first, and a
        forced 'drag' is choosing to spend 1.7 s failing. Never force it.

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
            # ⚠ TAKE THE GUN IN HAND RATHER THAN FALLING BACK TO THE DRAG.
            # This used to be a bare `'click' if self.held == gun else 'drag'`,
            # and `held` is a BELIEF, not a reading -- hold() says so itself:
            # nothing reads which weapon is actually in hand. So any moment the
            # belief was false or had been cleared (ensure_kit clears it after
            # a restock, because the spawner can push a gun out of the rack)
            # this quietly chose the drag.
            #
            # And the drag into a weapon slot is not a slower option, it is a
            # gesture that has never worked: 0/10 measured against right-click
            # 10/10 at 0.17 s (control/CLAUDE.md), 0/4 against 4/4 in the
            # earlier probe. Choosing it is choosing to fail, then to spend
            # 1.7 s failing, and a mis-aimed drag onto a slot can drop the gun.
            #
            # hold() returns immediately when the belief already matches, so
            # the common path costs nothing; when it does not match, one 1/2
            # keypress buys the gesture that works.
            if self.held != gun:
                self.hold(gun)
            if self.held == gun:
                gesture = 'click'
            else:
                gesture = 'drag'
                self._log(f'gun{gun} would not come to hand — falling back to '
                          f'the drag, which measured 0/10 into a weapon slot. '
                          f'Expect this to fail; it is logged rather than '
                          f'refused so the readback still records what the '
                          f'slot ended up holding.')
        if gesture == 'click':
            # att travels with it: without the catalogue key the retry cannot
            # re-find the row, and re-finding the row is the whole point.
            return self.right_click_equip(gun, slot, src, want=want,
                                          retries=retries, att=att)
        return self.drag(src, at_slot(gun, slot), want=want, retries=retries,
                         weapon=weapon)

    def right_click_equip(self, gun, slot, src, want=ANY_ITEM, retries=1,
                          att=None, verify=True):
        """L0 — Fit `src` by right-clicking it, reading the slot back.
        equip() is the entry point; it takes the gun in hand and passes att.

        ⚠ `verify=False` DROPS TO L0-WITHOUT-A-READBACK, and the reason it
        exists is the oldest circle in this repo. The readback is
        `_slot_states` -> SlotDetector, and SlotDetector decides `filled` by
        RECOGNISING AN ATTACHMENT -- so a part whose template is missing or
        stale reads as `empty`, this reports "did not land", and the caller
        records a compatibility fact about a part that is sitting on the gun.
        The parts that need collecting are exactly the parts with no template,
        so a template collector cannot use the readback that presumes one.
        Same shape as `unequip(known_filled=)` and `drag(verify=False)`.

        It does NOT skip the refusals: `held != gun` still refuses, and the
        name plate is still read either side, because "the click emptied the
        rack" must never be silent. It skips only the question the collector
        can answer better than SlotDetector can.

        ⚠ AND THE CALLER THEN OWES ITS OWN EVIDENCE. `collect_intersect` pays
        with two template-free readings: the row's NAME left 库存, and the
        tile of some slot on this gun changed. Neither consults an attachment
        template. A caller that cannot say something of that kind must not
        pass this -- an unverified equip returns ok=True for a click that went
        nowhere.

        ⚠ PASS `att=`. `src` is a ROW, and the first click evicts the
        incumbent into 库存, renumbering rows — without `att` the retry
        cannot re-find the part and so never re-clicks at all.
        ⚠ "Only reaches the HELD weapon" is a BELIEF: hold() reads nothing,
        and right-click measured 10/10 without it on a single-gun rack.

        Measured 4/4 at 0.35 s against 0/4 at 1.70 s for the equivalent drag
        (measured 2026-08-02) -- the drag is not merely
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

        frame = self._frame_for('slots', 'plate')
        before = self._slot_states(frame)
        plate0 = self._plate(gun, frame)
        checks = [(gun, slot, want)]
        x, y = self.point_of(src)
        for attempt in range(retries + 1):
            # ⚠ THE RETRY MUST RE-FIND THE PART. `src` is a ROW, and the first
            # click moves rows: fitting into an occupied slot evicts the
            # incumbent into the backpack, which inserts a row and shifts the
            # ones below it. Clicking the same (x, y) a second time therefore
            # aims at whatever slid into that position -- usually the part just
            # evicted -- and puts it straight back on, throwing this run's part
            # off. The readback then reports the FACTORY part in the slot and
            # the caller records "this weapon will not take ext_smg", which is
            # false and goes into kit_facts.json as a compatibility fact.
            #
            # Watched on screen 2026-08-07 and described exactly: "好像是装对
            # 了,然后又装一次,装错了,然后说找不到,把枪扔了". It also explains
            # the number this package's CLAUDE.md has been carrying unexplained
            # -- right-click into an occupied magazine slot measured 6/6 in the
            # isolated probe and 18/68 in the collector. The probe fits once.
            # The collector retries.
            if attempt:
                fresh = loose_items(self.look(frame=self._frame_for('loose'))
                                    or {})
                hits = sorted((loc for loc, k in fresh.items() if k == att),
                              key=_src_rank) if att else []
                if not hits:
                    # NOT in either panel. Either the first click landed after
                    # all and _await simply ran out of patience, or the part is
                    # gone. Ask the slot before believing the worse one --
                    # "verification was slow" and "it never went on" have been
                    # indistinguishable here, and clicking blind is how the
                    # wrong part gets fitted.
                    rec['checks'] = self._await(checks, before)
                    if all(r['ok'] for r in rec['checks']):
                        rec['ok'] = True
                        self._log(f'{loc_str(src)} -> {loc_str(dst)}: the '
                                  f'first right-click had landed; the readback '
                                  f'was just late')
                        return rec
                    # ⚠ TWO DIFFERENT FACTS, AND CONFLATING THEM WROTE
                    # FICTION INTO kit_facts.json. Without `att` this branch
                    # never searched for anything -- `hits` is `[] if not att`
                    # -- so reporting "the part is in neither panel" described
                    # a search that did not happen, and the caller records
                    # that as a COMPATIBILITY FACT about the weapon. equip()
                    # passes att; a direct caller does not, and the one who
                    # reads the log later cannot tell which happened.
                    rec['error'] = (
                        f'{att} is in neither panel and the slot does not '
                        f'hold it' if att else
                        'retry disabled: no att= was given, so the row could '
                        'not be re-found. This says nothing about whether the '
                        'part exists or whether the gun takes it.')
                    return rec
                x, y = self.point_of(hits[0])
            rec['attempts'] = attempt + 1
            self.pointer.right_click_at(x, y)
            if not verify:
                # The click went out and the plate is still read below. What is
                # NOT claimed is that anything landed -- `verified` False says
                # so, and the caller's own evidence is what decides.
                rec['checks'], rec['verified'], landed = [], False, True
            else:
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
        """R — Block until the Tab screen reads `want`, or the deadline. It
        SENDS NOTHING: False means the screen never changed, not that
        anything was attempted. ensure_tab() is what presses.

        ⚠ The bound is short on purpose — a swallowed key is not cured by
        waiting, so the answer to False is another press, not a longer wait.

        No sleep in the loop: one pass IS the pace. is_tab_open() grabs a 41x18
        crop, and win32_cap is ~3-6 ms of fixed GDI overhead almost regardless
        of size, so the poll runs at roughly the monitor's own rate.
        """
        deadline = time.perf_counter() + timeout
        while True:
            if bool(self.is_tab_open()) == want:
                return True
            if time.perf_counter() >= deadline:
                return False

    def ensure_tab(self, want, tries=3):
        """L1 — Press Tab until the screen reads `want`, re-pressing a
        swallowed key. -> bool. It RESTORES NOTHING: the caller owns the state
        afterwards, so an ensure_tab(False) in a finally shuts a screen
        somebody upstream is holding. tab_up() (L2) is the owner.

        ⚠ False can mean NOT PRESSED AT ALL — under the spawner panel or in
        the lobby the game ignores Tab, so this refuses rather than pressing.

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
        # ⚠ ASK WHAT IS ON SCREEN BEFORE PRESSING, not after the tries are
        # spent. The item-spawner panel is a menu: while it is up the game does
        # not act on Tab at all, AND is_tab_open() reads False because the
        # inventory is not drawn -- so this loop used to press (swallowed),
        # wait out await_tab, press again, and only then ask _blocking_screen.
        # Two wasted presses and two timeouts per occurrence, and it happens
        # once per spawner visit, i.e. once per weapon.
        #
        # It is also why the journal shows blocks opening `OO`: two presses in
        # a row with no state change between them, which reads as a swallowed
        # keystroke and is really a keystroke sent at a menu.
        # ⚠ AND IT RUNS ONLY WHEN A PRESS IS ABOUT TO HAPPEN. _blocking_screen
        # is a full capture_screen() plus two detectors, and this method is
        # called around every read -- putting the probe at the top would put a
        # full-screen grab on the commonest path there is, the one where the
        # screen is already in the wanted state and nothing needs doing.
        for _ in range(tries):
            if bool(self.is_tab_open()) == want:
                if pressed:
                    self._stamp('tab', None, None, gesture=True, moved=True,
                                want=want, presses=pressed)
                return True
            if not pressed:
                blocker = self._blocking_screen()
                if blocker:
                    self._log(f'not pressing Tab — {blocker}')
                    self._journal_refusal('refused', None, None, blocker,
                                          by='ensure_tab', want=want,
                                          presses=0)
                    return False
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
        ok = bool(self.is_tab_open()) == want
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
        """L0 — Press 1/2 so right-click equips onto `gun`. Nothing reads the
        HUD back: True means the key went out, not that the gun is in hand.

        ⚠ It short-circuits on the CACHED `self.held` and presses nothing, so
        anything that can move a gun — the spawner, an eviction — must set
        `ac.held = None` first. capture_ads.hold_weapon() is the checked one.

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
        was_open = bool(self.is_tab_open())
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

    def unequip(self, gun, slot, to=None, retries=1, gesture='auto',
                known_filled=False):
        """L1 — Pull weapon `gun`'s `slot` off, into 库存 by default. Proves
        the SLOT emptied, never that the part arrived — see panel_counts().

        ⚠ THE EMPTY-SLOT REFUSAL LIVES HERE. A gesture on an empty slot
        reaches the weapon row and throws the whole gun on the floor; drag()
        only checks that from its SECOND attempt, so do not call it direct.

        gesture: 'auto' right-clicks when the destination IS 库存 and drags
        otherwise; 'click' and 'drag' force one.

        Right-click cannot aim. The game decides where the part goes and it
        always chooses the backpack, so anything else -- the floor, the other
        gun's slot -- is still a drag. That is the same shape as
        right_click_equip, where the target is "whatever is in hand" rather
        than a parameter.

        THE DRAG, AS CURRENTLY AIMED, DOES NOT REACH 库存. Measured twice on
        2026-08-02: the slot empties and the
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

        ⚠ THAT LAST SENTENCE STOPPED BEING TRUE AND THE PREDICTED THING
        HAPPENED. slot_detector moved the scope position off `unknown` onto
        fill_match, so an unrecognised sight now reads `empty` and lands in the
        refusal above -- "every sight unremovable", exactly as written, and
        nobody came back here when that change was made. Measured 2026-08-09:
        aug/holo, g36c/scope_2x and k2/variable each went ON the gun and then
        could not be taken off, `slot moved 0.0, 库存 0->0`, and a whole
        collection run returned 0 crops.

        `known_filled=True` is the caller saying it put the part there itself
        and has TEMPLATE-FREE evidence of it. It skips the two refusals that
        rest on matching a template -- `empty` and AMBIGUOUS -- and skips
        NOTHING else. In particular `absent` still refuses: that one comes from
        ring_grad on the tile geometry, it does not consult a template, and it
        means this weapon has no such slot.

        ⚠ The hazard the refusals exist for is real and unchanged: a gesture at
        a slot with nothing in it reaches the weapon row and throws the gun on
        the floor, 74 parts lost across 11 runs. So `known_filled` is not "I am
        in a hurry", it is "I have a second, non-template reading". The one
        caller that passes it (collect_templates.take_off) has two: 库存 did
        not grow when the part spawned, and the tile moved. A caller that
        cannot say that must not pass it.
        """
        dst = as_loc(to) if to is not None else at_inv()
        # ⚠ SLOT -> FLOOR IN ONE MOVE IS GONE, and this refusal is the whole
        # reason `shed()` exists. That move aims a DRAG at the slot, and an
        # empty slot is not an inert target: the drag takes the WEAPON ROW
        # underneath and throws the gun out wearing everything. 74 measured
        # losses, and then again on 2026-08-09 when a collector used it to
        # "make sure the slots were empty" -- both guns on the floor, logged as
        # `emptied to the floor` because the tile did change by 25 grey levels.
        #
        # `shed()` does it in two moves that cannot: a right click (the game
        # picks the destination, nothing is aimed) then a drag off a 库存 ROW
        # (no weapon underneath a list row).
        if dst[0] == 'ground':
            why = ('slot -> floor in one move is forbidden: the drag reaches '
                   'the weapon row when the slot is empty and drops the gun. '
                   'Use shed(gun, slot) — right click, then drag the 库存 row.')
            self._log(f'gun{gun}.{slot}: {why}')
            return step(at_slot(gun, slot), dst, ok=False, verified=True,
                        error=why)
        state = self.slot_state(gun, slot)
        if state == SLOT_ABSENT or (state == SLOT_EMPTY and not known_filled):
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
        if worn.get(slot) == AMBIGUOUS and not known_filled:
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
        """L0 — Pull a part off by right-clicking the slot, verified EMPTY.
        Go through unequip(): its tile and AMBIGUOUS guards are what keep the
        gesture off an empty slot, and the check here is a TEMPLATE read,
        which docs/game_quirks.md says cannot answer that question.

        ⚠ ok=True DOES NOT MEAN THE GUN IS STILL RACKED. An empty-slot click
        drops the whole gun, and the slot then reads empty, so `cleared` is
        True either way. Only rec['gun_lost'] separates them. This is the
        clearest case of an L0 that checks — and checks the wrong object.

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
        frame = self._frame_for('slots', 'plate')
        before = self._slot_states(frame)
        plate0 = self._plate(gun, frame)
        # ⚠ THE TILE, NOT THE TEMPLATE. `before` comes from _slot_states,
        # which reads TEMPLATE names -- and docs/game_quirks.md says in so
        # many words that a template read cannot answer "is there something
        # here": a part whose icon is not in the bank, or one the panel's
        # translucency made AMBIGUOUS, both come back '' from a slot that is
        # occupied. This gate then lets the click through onto what it
        # believes is an empty slot, the click reaches the weapon row
        # underneath, and the whole gun goes on the floor -- 74 parts across
        # 11 collector runs. slot_state() is the geometric read unequip()
        # already gates on; using it here means a caller who reached past
        # unequip is covered by the same guard rather than by a weaker one.
        tile = self.slot_state(gun, slot, frame)
        if tile in (SLOT_EMPTY, SLOT_ABSENT):
            rec['error'] = (f'gun{gun}.{slot} reads {tile} by tile — a click '
                            f'here reaches the weapon row and drops the gun')
            self._journal_refusal('refused', src, at_inv(), rec['error'],
                                  by='right_click_unequip',
                                  plate=[plate0, None])
            return rec
        if not before[gun][slot]:
            rec['error'] = f'gun{gun}.{slot} is already empty'
            self._journal_refusal('refused', src, at_inv(), rec['error'],
                                  by='right_click_unequip',
                                  plate=[plate0, None])
            return rec
        checks = [(gun, slot, EMPTY)]
        x, y = self.point_of(src)
        for attempt in range(retries + 1):
            # RETRY-SAFE: `src` is a SLOT, and slots are addressed by name, not
            # by position — nothing this loop does can move one. The sibling
            # hazard is the row lists, where removing a row scrolls the ones
            # below it up and a reused point grabs whatever slid into place;
            # see drag() and right_click_equip(), both of which re-find their
            # target every attempt. Checked by `pixi run gestures`.
            #
            # The danger here is the OTHER one and it is already guarded above:
            # a slot that EMPTIED between attempts, because a gesture on an
            # empty slot reaches the weapon row and drops the whole gun. That
            # check runs before the loop, once — which is why `before` is read
            # outside it and the emptiness test is not repeated here.
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
                          f'unequip. See calibration/artifacts/drag/journal.jsonl.')
            if cleared:
                rec['ok'] = True
                self._log(f'{loc_str(src)} -> 库存: right-clicked '
                          f'({self._checks_str(rec["checks"])})')
                return rec
        rec['error'] = 'right-click did not clear the slot'
        return rec

    def strip(self, gun, to=None, retries=1):
        """L1 — Take every attachment off `gun`, one verified slot at a time.
        -> BATCH. Every slot it touched reads empty; the gun is not re-read.

        ⚠ THE DEFAULT DESTINATION IS 库存, and PUBG bolts whatever is in the
        pack onto the next gun to arrive — pass to=at_ground(), and never
        strip before drop_weapon(), which throws the gun WEARING its parts.

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
        frame = self._frame_for('slots')
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
        """L1 — One gesture: drop what is at `src` on the floor, read back by
        the row count. clear_inventory() is the loop; this is one step.

        ⚠ FROM A SLOT THIS SKIPS THE EMPTY-SLOT GUARD, which lives in
        unequip(), not here. A gesture at an empty slot reaches the weapon
        row underneath and throws the whole gun out — 74 measured losses.
        Use unequip(gun, slot, to=at_ground()) for anything on a gun.

        (This line used to read "Works from a slot too", which is exactly the
        use the paragraph above exists to refuse.)
        """
        return self.drag(src, at_ground(), retries=retries)

    def auto_equip(self, src):
        """L0 — Right-click `src` and let the GAME choose the slot. OPEN
        LOOP: True means the click was SENT, not that anything moved.

        ⚠ It never calls hold(), and the game fits onto the weapon IN HAND;
        with two guns racked, which one receives the part has never been
        measured. For a named part in a named slot use equip()/ensure_kit().

        A different action from equip(), not a convenience over it: equip()
        takes a destination and refuses without one, because it is checking
        that a named part reaches a named slot. Here the destination is the
        ANSWER — the caller is asking the game where this part belongs.

        The one caller is calibration/legacy_collect_templates.py, and its reason is
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
            f = self._frame_for('tiles') if frame is None else frame
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
        this way. calibration/legacy_collect_templates.py uses it only to re-stage a
        part it has just watched arrive, never to decide what a crop is.
        """
        item = self.look(frame).find(att)
        if item is None:
            return False
        return self.auto_equip(item.where)

    def shed(self, gun, slot, retries=1):
        """L2 — A part off a gun and onto the floor, in TWO moves. -> record

            1. RIGHT CLICK the slot   the game decides where it goes, and it
                                      always chooses 库存. Nothing is aimed at
                                      a destination, so nothing can be aimed
                                      wrong.
            2. DRAG the 库存 row      onto the floor. The source is a list row;
                                      there is no weapon underneath a list row.

        ⚠ THE ONE-MOVE VERSION IS FORBIDDEN AND unequip() NOW REFUSES IT.
        Dragging from a weapon slot to the floor aims the drag AT THE SLOT, and
        an empty slot is not an inert target -- the drag takes hold of the
        WEAPON ROW under it and throws the whole gun out, wearing everything.
        That is 74 measured losses, and it happened again on 2026-08-09 when a
        collector cleared its slots that way "to be sure they were empty": the
        guns went on the floor, the tiles changed by 25 grey levels, and the
        change was logged as `emptied to the floor`. A move that cannot tell
        success from losing the gun is not a move.

        ⚠ AND THIS DOES NOT MAKE AN EMPTY SLOT SAFE TO CLICK. A right click on
        an empty slot reaches the weapon row too -- that is the same measured
        hazard, stated in unequip() and in control/CLAUDE.md. What the two-move
        shape removes is the AIMED 1621 px drag and its release point, not the
        first gesture's exposure.

        So the caller still owes the same thing it always did: EVIDENCE that
        the slot is occupied, obtained without a template. The one this repo
        has is the autofit rule -- spawn a copy and watch 库存. If the list
        grew, the slot was full, and only then is this safe to call.

        -> {'ok', 'row', 'error'} — `row` is where it passed through 库存.
        """
        rec = self.right_click_unequip(gun, slot, retries=retries)
        # ⚠ ROW 0, NOT "the row it landed in". clear_inventory documents why
        # the top row is the only stable address -- the panel is a 12-row
        # WINDOW and rows scroll up as ones above them leave. The callers of
        # this method clear 库存 before they start, so the part just clicked
        # off is the only thing in the list and row 0 IS it.
        #
        # And if it is not -- if something else was in the list -- the cost is
        # that the other thing goes on the floor too. That is a wasted item,
        # not a lost gun, which is the whole reason this shape was chosen over
        # the one-move drag.
        out = self.discard(at_inv(0), retries=retries)
        return step(at_slot(gun, slot), at_ground(),
                    ok=bool(out.get('ok')), verified=True,
                    clicked=bool(rec.get('ok')), error=out.get('error'))

    def drop_weapon(self, gun, retries=1, gesture='auto'):
        """L2 — Throw the gun in rack slot `gun` on the floor WEARING its
        parts, confirmed by re-reading the plate ('auto' falls back from
        right click to drag). Do NOT strip() first: parts left in the pack
        get auto-fitted onto the next gun to arrive.

        ⚠ THE VERDICT IS THE NAME PLATE, and it reads None both for an empty
        rack and for a gun the templates cannot name — so an unnamed gun that
        DID leave reports failure, and 'auto' then drags at an empty rack row.
        That asymmetry against clear_rack, which reads presence off the ink,
        is deliberate and stated in plate_state: ink cannot say WHICH gun, and
        this method's question is about identity.

        ⚠ WHAT WAS NOT DELIBERATE was that the asymmetry also covered "is the
        rack even on screen". Refusing on plate_state 'panel' is the same
        decision clear_rack makes off the same reading, and this method used
        to skip it: on 2026-08-07 the spawner panel was over the rack twice,
        the right click went into the panel, `auto` then paid a 1621 px drag
        into the same panel, and the run reported `rack not empty` — a true
        sentence pointing at the wrong screen. The gun never moved and nothing
        said why. See PLATE_INK_MAX for the numbers (10941/11250 against a
        real plate's 597-901).

        It could not report a false success — `was` is None under the panel,
        so `ok` was unreachable — which is exactly why it survived: the cost
        was a misdiagnosis, not a lost gun, and misdiagnoses do not raise.

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
        frame = self._frame_for('guns', 'plate')
        was = self._read_guns(frame).get(gun)
        plate0 = self._plate(gun, frame)

        # BEFORE THE MOUSE MOVES, and off the frame already in hand — see the
        # ⚠ above. Only 'panel' is refused here: 'empty' is left alone because
        # this method's caller may be dropping a gun the templates cannot
        # name, and that is the case the ink cannot be asked about.
        if self.plate_state(gun, frame) == 'panel':
            why = (f'plate reads {plate0} ink, far above any real plate '
                   f'({PLATE_INK_MIN}..~900) — the spawner panel is over the '
                   f'rack, so every gesture at gun{gun} goes into the panel. '
                   f'Close it first.')
            self._log(f'gun{gun}: {why}')
            self._journal_refusal('refused', at_gun(gun), at_ground(), why,
                                  plate=[plate0, None], was=was, now=was,
                                  by='plate_state')
            return step(at_gun(gun), at_ground(), ok=False, verified=True,
                        error=why, was=was, now=was, gesture=None, drag=None)

        def settled():
            time.sleep(DROP_SETTLE)
            f = self._frame_for('guns', 'plate')
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
        """L1 — Drop every racked gun on the floor, ONE PASS. -> BATCH. An
        empty slot is skipped, not failed; a failed drop is not retried, and
        the rack is not re-read afterwards.

        ⚠ THE GUNS LEAVE WEARING THEIR PARTS, so this takes the run's
        attachments to the floor with them. Deliberate — see drop_weapon:
        stripping first lets PUBG auto-fit them onto the next gun.

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
            frame = self._frame_for('plate')
            state = self.plate_state(g, frame)
            if state == 'panel':
                # Not a name plate — the spawner panel is over this crop. See
                # PLATE_INK_MAX. drop_weapon refuses this too now, and this
                # check is NOT the same one made twice: it aborts the WHOLE
                # batch. A panel over gun1 is over gun2, so continuing would
                # buy a second refusal and a second frame to learn nothing.
                # The number is re-read only here, on the path that reports.
                ink = self.plate_ink(g, frame)
                self._log(f'gun {g} plate reads {ink} ink, far above any real '
                          f'plate ({PLATE_INK_MIN}..~900) — the spawner panel '
                          f'is over the rack. Close it before clearing.')
                return batch(out, dropped=did, ok=False,
                             error=f'spawner panel over the rack (ink {ink})')
            if state == 'empty':
                continue
            out.append(self.drop_weapon(g))
            did.append(g)
        return batch(out, dropped=did)

    def stow(self, row, retries=1):
        """Pick row `row` off the ground into 库存."""
        return self.drag(at_ground(row), at_inv(), retries=retries)

    def clear_ground(self, retries=1, passes=4):
        """L1 — Move the whole floor into 库存, up to `passes` passes,
        re-reading between gestures. -> BATCH. The stop condition is the ROW
        IDENTITY, not the count: the panel is a 12-row window that refills
        from below as rows leave.

        ⚠ A FULL 库存 BLINDS THE PER-DRAG CHECK — with both lists at 12 rows
        drag() stops counting and returns ok unverified, so only the
        pass-level compare catches it. clear_inventory() is the mirror.

        Repeats until a pass moves nothing, for the same reason stock.tidy()
        does: the list shows 12 rows and rows below scroll up as the ones
        above leave, so "the count went down" is not the stop condition and
        "the visible rows did not change" is.

        Always drags the TOP row. Pulling row i out shifts everything below it
        up, so row 0 is the only index that stays valid without re-reading
        between gestures -- and this re-reads anyway, because 附近 is the one
        panel other things fall into while you work (a swap displaces a part,
        a dropped gun lands there).

        `verified` is True per step now: panel to panel has no SLOT to read,
        but drag() counts the two lists' rows (panel_counts), which is what
        caught "12 dragged, 0 moved". The batch is verified a second way, by
        the row count reaching zero -- keep both: a per-drag count cannot see
        a row that left and came back, and the total cannot say which drag.
        """
        out = []
        rows = None
        for _ in range(passes):
            view = self.look()
            rows = view.rows('nearby')
            if not rows:
                break
            # `i.key`, not `getattr(i, 'key', None)`: Item declares key in
            # __slots__ and __init__ always assigns it. The `or '?'` DOES
            # carry weight and stays — key is None for a template whose asset
            # has no catalogue entry, which is exactly the row a collection
            # run is here to photograph.
            before = tuple((i.key or '?') if i is not None
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
            cur = tuple((i.key or '?') if i is not None
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
        """L1 — Throw all of 库存 on the floor, up to `passes` passes.
        -> BATCH. Always row 0 and re-read each pass: rows scroll up, so a
        row index from one detection pass is stale by the next gesture.

        ⚠ A DRAG CAN REPORT SUCCESS HAVING MOVED NOTHING — measured at 12
        rows dragged, 12 `dragged`, 0 items moved. The pass-level row
        compare is the verdict here, never the steps. clear_ground() mirrors.

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
            before = tuple((i.key or '?') if i is not None
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
            cur = tuple((i.key or '?') if i is not None
                        else '-' for i in after.inventory)
            if cur == before:
                return batch(out, error=f'{rows} row(s) left and the panel did '
                                        f'not change — the drops are not '
                                        f'landing', rows_left=rows)
        return batch(out, error=None if not rows else
                     f'{rows} row(s) still in 库存 after {passes} passes',
                     rows_left=rows or 0)

    def transfer(self, src_gun, dst_gun, slots=None, retries=1):
        """L1 — Move src_gun's attachments onto dst_gun, via 库存. -> BATCH.
        Every half is slot-verified; the finished kit on dst_gun is not.

        ⚠ BROKEN AS OF 2026-08-07, and it has no callers to have noticed:
        `worn` holds ASSET names off loadout(), while TabView.find() compares
        `Item.key`, the catalogue key — disjoint namespaces, so find() is
        always None and every slot takes the 'vanished after unequipping'
        branch. Fix before use; the shape below is what it is meant to do.

        ⚠ IT UNEQUIPS FIRST, so a failed second half leaves src_gun BARE and
        the part loose in 库存, where PUBG bolts it onto the next gun that
        arrives. ensure_kit() is the one that decides ok by reading the gun.

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
        one. THE EXPERIMENT THAT WOULD, spelled out because no file holds it
        any more: drag slot->slot directly, N times, and read BOTH ends back
        -- source empty AND destination filled. Reading only the source
        cannot tell "it moved" from "it fell on the floor". If that lands,
        change MOVES's evidence and switch the default here, not the other
        way round.

        Only slots dst_gun actually HAS are attempted — an attachment released
        over a slot that is not drawn goes on the floor.
        """
        if src_gun == dst_gun:
            return batch([], error='source and target are the same gun')
        loadout = self.loadout()
        if loadout is None:
            return batch([], error='the Tab screen never came up')
        # ⚠ ASSET NAMES, AND find() COMPARES CATALOGUE KEYS. loadout() hands
        # back what _slot_states read -- 'Muzzle_Compensator_Large_C' -- while
        # TabView.find matches Item.key, which is 'comp_ar'. The two sets are
        # disjoint, so find() was ALWAYS None and every slot took the
        # "vanished after unequipping" branch: source gun stripped bare, parts
        # loose in 库存, target gun wearing nothing, and no rollback. It had
        # zero callers, which is the only reason it never cost a run.
        worn = {s: (_BY_ASSET.get(a) or a)
                for s, a in loadout['slots'][src_gun].items() if a}
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
        """L1 — Fit `gun` with everything compatible that is loose on screen.
        Every drag is verified; the GUN never is. ensure_kit() is the L2.

        ⚠ IT ONLY ADDS. An occupied slot is SKIPPED, not replaced, unless
        replace=True, and PUBG has already auto-fitted the pack onto the gun
        — so ok=True is compatible with a gun wearing parts nobody asked for,
        and with nothing having been fitted at all (a part skipped at plan
        time produces no step, so it cannot make `ok` false).

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
        """L1 — Run plan_equip()'s drags against weapon slot `gun`. Equips
        only: it can add a part, never take one off, and never re-reads.

        ⚠ RETURNS A BARE LIST, the one method here that is neither STEP nor
        BATCH — an all-failed list is still truthy, so read every rec['ok'].
        build() wraps it in batch(); ensure_kit() decides ok by the readback.

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
        """L2 — Make weapon `gun` wear exactly `want`, as few moves as
        possible. `ok` is the READBACK; build() reports on its drags alone.

        ⚠ ok=False can mean UNREADABLE, not unfitted: an AMBIGUOUS slot buys
        only two backdrop nudges, and eleven cells of the 2026-08-05
        factorial were binned with the part fitted the whole time.

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
                # ⚠ TAKE THE GUN IN HAND *HERE*, WHILE TAB IS STILL SHUT.
                #
                # hold() presses 1/2, and those keys are swallowed while the
                # inventory is up -- so it brackets itself with a close and a
                # reopen. Called at its old site below, after the reopen on the
                # next line, that bracket is a second round trip through a
                # state we are standing in right now: journal blocks of
                # `False -> True -> True -> False -> True`, 49 of them, five
                # Tab presses to accomplish one keypress.
                #
                # Pressing here costs nothing extra. `held` was just cleared,
                # so the press was going to happen either way; hold() sees
                # was_open False, skips both toggles, and the call below then
                # returns on `self.held == gun` without touching Tab at all.
                #
                # Measured over the whole shared journal before this change:
                # 1679 Tab rows / 1836 real presses, 975 of them (58%) inside
                # blocks that ended in the state they began in.
                self.hold(gun)
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

            # ⚠ THE CONDITION USED TO BE `rec['attempts'] > 0`, i.e. re-read
            # only after a step that had to RETRY -- and that is backwards.
            # What moves the rows is a step that WORKS: an equip takes its
            # part out of the inventory list and everything below it scrolls
            # up (stock.py says so where tidy explains why it repeats). So the
            # positions this plan holds are stale from the first success on,
            # and every later step aims at a row that has shifted -- at
            # nothing, or at a different part.
            #
            # Reported on 2026-08-07 from watching it: "每次背包里要先看一下,
            # 再说拖动还是装上什么的,不然拖的都不对呢,对着空拖". It also
            # matches this repo's own standing rule, 拖拽一次一验 -- verifying
            # only after a whole burst turns a timing problem into what looks
            # like a geometry one.
            #
            # Now: re-read after ANY step that did something. A step that
            # failed outright changed nothing and does not need it.
            stale = False
            for step in plan['steps']:
                rec = self._kit_run(gun, step, weapon, retries,
                                    look if stale else None, view)
                out['steps'].append(rec)
                stale = (stale or rec['attempts'] > 0
                         or bool(rec.get('ok')))

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
        # ⚠ TAB MUST COME DOWN FIRST, and until 2026-08-07 it did not, so this
        # function moved NOTHING and said it had. Measured by
        # mean absolute pixel change of the slot block over 600 counts of
        # yaw, four rounds each -- and the `still` arm is what makes it an
        # answer rather than a number:
        #
        #     still (no input at all)      0.32     <- the noise floor
        #     nudge (Tab up, as it was)    0.29     <- below the floor
        #     turn  (same counts, Tab down) 22.78   <- 70x the floor
        #
        # The docstring above already named the reason -- with Tab up the raw
        # counts land on the CURSOR -- and drew the wrong conclusion from it:
        # that the move should be small, rather than that it does not happen.
        # ViewDriver.turn() closes Tab for exactly this reason.
        #
        # So the AMBIGUOUS re-read loop in ensure_kit was re-reading the SAME
        # picture twice and reporting the same ambiguity, which is why every
        # vector and mp5k magazine cell died at "holds something the templates
        # cannot separate". That is the same eleven cells this repo lost on
        # 2026-08-05 -- the fix written then was correct and never ran.
        #
        # The Tab toggle is the cost, and it is paid only here, on a path that
        # otherwise loses a whole cell.
        was_up = bool(self.is_tab_open())
        if was_up and not self.ensure_tab(False):
            self._log('could not lower the Tab screen to move the backdrop')
            return False
        mouse.move(counts, 0)
        time.sleep(NUDGE_SETTLE_S)
        if was_up and not self.ensure_tab(True):
            self._log('the backdrop moved but the Tab screen would not reopen')
            return False
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
            # the floor, so anywhere past the divider is the request.
            #
            # ⚠ "A RELEASE ONLY HAS TO CROSS IT" STOOD HERE AND IS WITHDRAWN.
            # It is true about the panel and false as a design rule: it put the
            # release on 870, the first column inside 附近, which no hand ever
            # goes near — eight recorded drags released at 604..800, median
            # 689. See NEARBY_DROP_X for the distribution and for how weak the
            # evidence separating the two points still is.
            #
            # Y IS FREE, which took three wrong answers to establish. Measured
            # in game 2026-08-04, always reading back after every single drag:
            #
            #   (870, y of the grabbed row) onto an OCCUPIED 附近 row   5/5
            #   (870, y of the first EMPTY row)                        6/6
            #   (744, 570) the old fixed point                        21/21
            #
            # Those three are about y and they survive the move to 682 — x is
            # the only thing that changed and all three held x fixed.
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

    def _frame(self, park=True):
        """A Tab-screen frame, cursor out of the way first.

        THE RULE, so a new call site does not inherit the move by accident:
        park belongs to the DETECTION, not to the grab. Park when the read is a
        template match over a region the cursor could be sitting on --
        _slot_states, _read_guns, items.detect -- because a hovered tile draws
        a tooltip over itself and the match then finds the tooltip. Do NOT park
        for reads that cannot be fooled by one: panel_rows is a Laplacian
        occupancy count and tab_open is a saturation test.

        Audited 2026-08-06 across all sixteen call sites: the only two that fed
        nothing but panel_rows were drag()'s before-count and _await_panel's
        poll, and both now pass park=False.

        The cursor sits on the drop target the moment a drag ends, and a
        hovered slot draws a tooltip over itself.

        `park=False` GRABS WITHOUT MOVING THE CURSOR, and there is exactly one
        caller: _await_panel, which is counting ROWS. panel_rows() is a
        Laplacian occupancy test, not a template match -- the tooltip that
        park() exists to hide cannot change a row count, so the move buys
        nothing there and is not free:

          it yanks the cursor to PARK_XY (200, 1380) in the instant after the
          button came up, on EVERY poll iteration, while the drop is still
          settling. The operator watched it happen and called it as the cause
          of drops not landing -- "你点那一下,导致很多这个拖不到地上".

        That is a hypothesis, not a measurement, and it is being changed
        because the move was unjustified HERE regardless of whether it is the
        cause. If landing rates do not move, park() was innocent and the
        journal will say so: `moved` and `poll` are now recorded on every drag.
        """
        if park:
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

    def _stamp(self, kind, src, dst, attempt=0, started=None, **fields):
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
        # ⚠ `started` IS WHEN THE GESTURE BEGAN, not when it was journalled,
        # and the difference is not cosmetic. A drag is stamped AFTER its
        # verification poll, so without this its gap_s carries its own poll
        # time -- 0.1 s when the drop landed, 1.1 s when it did not. Analysed
        # on 2026-08-07 that produced a perfect step: 276 drags under 1.2 s
        # with ZERO failures, 28 over 1.8 s with zero successes. The step was
        # the poll, not the game. A field used to predict an outcome must not
        # be measured after that outcome is known.
        #
        # ⚠ `t` BELOW IS STILL STAMPED AT THE END, so the gap between two
        # RECORDS reproduces that artefact exactly — and did, on 2026-08-09,
        # when `t[n] - t[n-1]` gave 616 drags under 1.5 s at 100% and none
        # above 1.9 s. Same step, same cause, and it took a second night to
        # recognise because the warning above is attached to the field that
        # was FIXED rather than to the one that still has the property.
        # `t` stays wall-clock because it is what lines up this journal with
        # another agent's; the rule is that `gap_s` is the only spacing this
        # file is allowed to reason from.
        now = time.perf_counter() if started is None else started
        prev = _LAST_GESTURE_END[0]
        rec = {'kind': kind, 't': round(time.time(), 3), 'pid': PID,
               'proc': PROC,
               'src': None if src is None else loc_str(src),
               'dst': None if dst is None else loc_str(dst),
               'attempt': attempt + 1,
               'gap_s': None if prev is None else round(now - prev, 3)}
        rec.update(fields)
        rec.update(self._churn(kind, fields, now))
        journal(rec)
        _LAST_GESTURE_END[0] = time.perf_counter()

    # A close followed by an open this fast, with no gesture between, cannot
    # have had anything happen out there. Measured 2026-08-06 across the shared
    # journal: 767 close-then-open pairs, of which 162 were hold()'s legitimate
    # bracket (the 1/2 keypress lives inside it) and 276 were real spawner
    # visits at 5-15 s. The remaining 329 had a MEDIAN GAP OF 0.69 s -- 658 Tab
    # presses and 169 s of wall clock spent leaving the inventory and coming
    # straight back.
    CHURN_S = 2.0

    def _churn(self, kind, fields, now):
        """Flag a leave-and-return that accomplished nothing. -> dict

        WHAT THIS ADDS OVER COUNTING IT AFTERWARDS IS THE CALLER. The pattern
        was measurable from the journal already -- what was not measurable was
        WHO did it, because a Tab row records the press and not the code that
        asked for it. Every fix tonight had to be guessed at from block shapes;
        this turns the next one into a lookup.

        Emitted as fields on the offending OPEN row rather than as a row of its
        own, so the evidence and the event cannot drift apart the way
        `cum_counts` and the curve did.
        """
        if kind != 'tab':
            # Any real gesture means the trip out was for something.
            self._since_close = None
            return {}
        if not fields.get('want'):
            # Stamp WHO closed it, here, while the stack still says so.
            self._since_close = (now, _calling_frame())
            return {}
        t0, who_closed = self._since_close or (None, None)
        self._since_close = None
        if t0 is None or now - t0 >= self.CHURN_S:
            return {}
        # BOTH SIDES. The first version recorded only who reopened, and that
        # is the half that cannot be acted on: "harvest.py:735 apply reopened
        # it" does not say whether apply was wrong to want it or whether
        # something else was wrong to shut it. The pair does.
        return {'churn': {'gap_s': round(now - t0, 3),
                          'closed_by': who_closed, 'by': _calling_frame()}}


    def _journal(self, src, dst, p0, p1, attempt, rows0, gesture,
                 moved, started=None, src_key='(not recorded)',
                 src_key_after=None):
        """One line for a drag: the gesture, the geometry and the outcome.

        `src_key_after` is the source row re-read AFTER a drag that did not
        report landing, and it is what makes one record self-contained — see
        drag(). It is None on the landing path, where it would only cost a
        template pass to confirm what the row count already said.
        """
        # Pointer.__init__ creates last_drag as {} and drag() only ever
        # reassigns it to a dict, so neither a getattr default nor an `or {}`
        # can fire. Reading it directly is also the thing that would BREAK
        # loudly if that ever stopped being true, which is the point: the
        # guard's only real effect was to make a missing recorder look like a
        # drag with no geometry.
        d = self.pointer.last_drag
        self._stamp(
            'drag', src, dst, attempt, started=started,
            want={'grab': list(p0), 'release': list(p1)},
            got={'grab': d.get('grab'), 'held': d.get('held'),
                 'release': d.get('release')},
            place={'grab': d.get('grab_place'), 'dst': d.get('dst_place')},
            steps=d.get('steps'), drag_s=round(d.get('s') or 0.0, 3),
            # How far the cursor kept moving AFTER the button came up. The two
            # endpoints were both already recorded and nobody could compare
            # them without hand arithmetic per record, so the quantity that
            # decides `failed_at` was never plottable. It is the drift itself,
            # not either position, that says whether PLACE_TOL = 2 px is a
            # sane bound while Tab is up -- and the answer so far is no: the
            # failing records sit at 4-5 px held, 9 px after release.
            drift=(None if not (d.get('held') and d.get('release')) else
                   [d['release'][0] - d['held'][0],
                    d['release'][1] - d['held'][1]]),
            tab_open=bool(self.is_tab_open()),
            gesture=bool(gesture), failed_at=d.get('failed_at'),
            rows_before=list(rows0) if rows0 else None,
            src_key=src_key,
            # ⚠ THE PAIR IS THE POINT. `src_key` alone says what was at the
            # grab point; the two together say whether it is still there, and
            # that is the question every reader of this file has actually had.
            src_key_after=src_key_after,
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

    def _row_key(self, src, frame=None):
        """What the ROW at `src` holds right now. -> key | None | '?' | '(…)'

        The three answers are three different claims and the journal needs all
        of them apart: `None` means the row read as EMPTY, `'?'` means
        something is there and the templates could not name it (a detector
        problem, not a game one), and a parenthesised string means the read
        itself could not be attempted.

        `frame=None` grabs one. Callers already holding the frame they read
        the row counts off should pass it -- that is one template pass instead
        of a grab plus a pass.
        """
        try:
            kind, idx = (src if isinstance(src, tuple) else (None, None))[:2]
            if kind not in ('inventory', 'ground') or not isinstance(idx, int):
                return '(not a row)'
            v = self.look(frame=self._frame_for('rows') if frame is None
                          else frame)
            lst = v.inventory if kind == 'inventory' else v.nearby
            it = lst[idx] if 0 <= idx < len(lst) else None
            return None if it is None else (it.key or '?')
        except Exception as e:                  # noqa: BLE001 — recorded, not raised
            return f'(read failed: {type(e).__name__})'

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
            # No park: see _frame. Counting rows does not care about a
            # tooltip, and moving the cursor here moves it out from under a
            # drop that has not finished landing.
            f = self._frame_for('rows')
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
            states = self._slot_states(self._frame_for('slots'))
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

    ac = InventoryControl()
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

