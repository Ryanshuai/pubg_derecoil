"""Cursor placement, clicks and drags on the game's UI screens — plus the
relative moves that aim, which are the same device and nothing like the same
gesture. See Pointer.move() vs Pointer.move_to().

Placement is always SetCursorPos: the spawner and Tab screens are UI, where
the game's cursor follows the system cursor. The *button* is a different
matter — it goes through the Pico as a real HID report, which the game sees
even under raw input. There is no fallback: a SendInput backend existed until
2026-08-08, and under raw input its clicks and turns reached the game as
nothing at all. See press/pico_mouse.get_mouse().

Focus is not handled here: taking and holding the game window is its own
closed loop, so it lives in control/focus.py. Call ensure_focus() from there
before driving anything through this module.
"""
import ctypes
import time

# ⚠ FOUR _MOUSEEVENTF_* CONSTANTS STOOD HERE (2026-08-08). They fed the
# `else:` half of click / _press / _release -- a mouse_event() software path
# taken when there was no Pico. `self.pico` cannot be falsy any more (the
# constructor raises instead of handing back a Pointer with no device), so
# those branches were unreachable and these were their only readers.

MOVE_WAIT = 0.12       # cursor settle before the button goes down

# A UI click is three waits, and every one of them is on the critical path of
# every calibration run: the spawner alone fires a dozen per kit.
#
# Measured 2026-08-02 (right-click equips READ BACK off the weapon slot --
# the gesture reporting ok proves nothing here). settle held at 5/5 all the way down to 0, hold_ms down
# to 10 ms, after down to 0.02 -- but after=0 lands 0/5, because the click is
# handed to the Pico asynchronously and returning immediately races the CDC
# write. The values here sit one notch above each measured floor.
#
# Only settle and after cost wall clock. pico.click() returns at once and the
# firmware times the hold itself, so hold_ms buys nothing back -- it is kept
# short only so `after` can be, without a click returning mid-press.
CLICK_SETTLE = 0.015   # cursor placed -> button down (was 0.12)
CLICK_HOLD_MS = 20     # button held down, per click (was 80)
CLICK_AFTER_S = 0.035  # after the release, before returning (was 0.09)

# ── Drag timings ─────────────────────────────────────────────────────────
# A drag is press -> travel -> release with the button held the whole way.
# The Pico cannot simply "hold": CMD_CLICK carries a duration. It is fully
# asynchronous though — the firmware only stores an end time and keeps the
# button down until then — so an arbitrary hold is one CMD_CLICK plus a re-arm
# every DRAG_REARM_S (each re-arm pushes inject_end_ms out, with no release in
# between), and the release is a CMD_CLICK of duration 0, which expires on the
# firmware's very next report.
# THE TWO WAITS WERE GUESSES AND THEY DOMINATED THE GESTURE. 120 ms before the
# first move and 140 ms before the release is 260 ms of the old 420, and the
# hand measurement says the game wants neither: a real drag moves 3-20 ms after
# the button goes down and releases 2-5 ms after the last move. These are those
# numbers with room, not the numbers themselves.
DRAG_GRAB_WAIT = 0.04   # button down -> first move: the UI has to latch the
                        # item under the cursor before it will follow
#
# THE STEP IS A DISTANCE, NOT A COUNT, and the count is what it used to be.
# DRAG_STEPS = 10 interpolated positions is fine over a slot-to-slot hop and
# absurd over the 1600 px from 库存 to 附近: 160 px per update, which the game
# reads as the cursor leaving rather than as a drag, and the item is let go
# somewhere in the middle. `the drops are not landing`, from the outside.
#
# MEASURED AGAINST A HAND (a scratch recorder, gone; 34 real drags of
# 390-464 px, cursor sampled at 1 kHz): median 18-25 px per position update,
# max 51, arriving every 7.7 ms. So a step of 24 px every 8 ms IS the human
# gesture, and the same 1600 px now takes ~67 updates instead of 10.
# THE CLIFF IS MEASURED. 104 px 库存 -> 附近, three drags per step size, each
# one read back off the screen before the next (2026-08-04):
#
#     15 px/step  3/3        52 px/step  1/3
#     21 px/step  3/3       104 px/step  0/3   (one jump)
#     35 px/step  3/3
#
# So the game accepts up to ~35 px between positions and starts dropping the
# item past that — which lands exactly where the hand does: 34 recorded human
# drags moved a median of 18-25 px per update and never more than 51.
#
# 32 sits under the cliff with room. It is also why the old DRAG_STEPS=10 was
# not merely slow but WRONG: ten steps over the 1600 px crossing is 160 px
# each, three times past the cliff, so that drag could never have worked.
DRAG_STEP_PX = 32       # px per interpolated position
DRAG_STEPS_MIN = 4      # 4 x 26 px covers the 104 px hop and stays under it
DRAG_STEPS_MAX = 120    # ~3800 px, longer than any drag on a 3440-wide screen
DRAG_STEP_WAIT = 0.008  # measured: a real mouse reports every ~7.7 ms, and the
                        # game renders at 144 Hz (6.9 ms), so sending faster
                        # than this cannot put more positions on screen
DRAG_HOVER_WAIT = 0.04  # at the target, before the button comes up
DRAG_DROP_WAIT = 0.25   # after release, before the screen is read back
DRAG_HOLD_MS = 400      # Pico hold per arm; must exceed DRAG_REARM_S by a lot
DRAG_REARM_S = 0.15     # so a dropped CDC packet still leaves 250 ms of hold

# ── A recorded hand, replayed ─────────────────────────────────────────────
#
# (ms since press, x, y) from one real drag, 库存 -> 附近, recorded at 1 kHz by
# tools/record_drag.py on 2026-08-09 and kept whole. Eight were recorded; this
# is the quickest (376 px in 114 ms, 3.3 px/ms) and its release sits on the
# median of the eight.
#
# WHY A PATH AND NOT THREE MORE CONSTANTS. The interpolated gesture is
# uniform: every step the same size, the same 8 ms apart. A hand is not — this
# one accelerates 1, 4, 8, 14, 24, 32, 41, 49, 53 px per update and then brakes
# 51, 45, 33, 21 into the drop. ⚠ ITS PEAK STEP IS 53 px, which is past the
# "52 px -> 1/3" cliff that DRAG_STEP_PX = 32 was set to stay under, and it
# lands anyway. So that cliff was measured on UNIFORM steps and does not
# describe a step arriving inside a ramp; do not read the two as the same
# number.
#
# WHAT IT IS AND IS NOT EVIDENCE FOR, because the temptation is to read the
# 12/12 as a win: replaying this scored 12/12 with zero retries on 库存 ->
# 附近, and the shipped interpolated gesture at the shipped release point
# scored 12/12 with zero retries on the same path minutes earlier. THE TWO ARE
# TIED. What actually moved between those runs is the release point, which the
# recording is the evidence for (see NEARBY_DROP_X). This path is here because
# it is the gesture that was run when the release point was validated, not
# because it beat anything.
HUMAN_DRAG_PATH = [
    (0.00, 1058, 200), (3.07, 1058, 199), (11.04, 1057, 198),
    (19.93, 1053, 198), (27.07, 1045, 198), (34.83, 1031, 198),
    (42.90, 1007, 198), (50.88, 975, 199), (58.86, 934, 200),
    (66.99, 885, 200), (75.13, 832, 201), (82.92, 781, 203),
    (90.92, 736, 205), (100.17, 703, 206), (107.87, 682, 208),
]

# Past this, the recording is not replayed and drag() interpolates instead.
#
# ⚠ THIS BOUND IS THE WHOLE REASON THE PATH IS SAFE AS A DEFAULT. Replaying is
# an affine SCALING onto the actual endpoints, so a 15-update recording
# stretched over the 1122 px slot -> 库存 crossing puts 158 px between
# positions — five times DRAG_STEP_PX and past the 104 px spacing that measured
# 0/3. The recording is 376 px long; anything near that replays, anything
# longer gets the uniform gesture, which is the one with 224/225 behind it on
# exactly those long edges.
PATH_MAX_TRAVEL_PX = 450

# ── Making a placement stick ──────────────────────────────────────────────
#
# SetCursorPos wins against nothing else touching the mouse. It loses against
# raw HID reports still in flight, which is the normal state right after a view
# turn — see Pointer.place() for the measurement. Both limits are attempt
# counts rather than deadlines so that a quiet cursor costs one read.
PLACE_TRIES = 6         # free placement: 6 x MOVE_WAIT = 0.72 s worst case,
                        # and the observed drift settles inside 1 s
PLACE_TRIES_HELD = 3    # with a button down: 3 x DRAG_HOVER_WAIT = 0.12 s,
                        # which must stay well under DRAG_HOLD_MS (0.4 s) or
                        # the hold expires and the item drops in mid-travel
PLACE_TOL = 2           # px; SetCursorPos is exact, so this only absorbs a
                        # DPI-scaling rounding, not real motion

# ── making the game SEE the drag ──────────────────────────────────────────
#
# The travel is SetCursorPos, which does not go through the Pico, and the
# firmware only emits a HID report when something changed:
#
#   pico_firmware/src/main.c, send_hid_output()
#   if (mx == 0 && my == 0 && rdx == 0 && rdy == 0 && !buttons_changed) return;
#
# So a whole drag reaches the game's raw input as exactly two reports — button
# down, button up, no motion between them. From there it is a CLICK, not a
# drag, and the item is never picked up. The UI cursor still tracks
# SetCursorPos (hover highlights follow it, and the release point reads back
# correct), which is why every gesture-level number looks perfect on a drag
# that did nothing.
#
# Measured over 28 logged drags before this existed: 9 landed, 9 missed, and
# the misses were "released clean, nothing arrived" every time — placement 1/1
# and zero offset at both ends on all 18. Failures got worse deeper into a
# burst (position 1: 5/8 landed, positions 3-4: 0/3), which is what an
# accidental cause looks like: something else has to supply the motion report,
# and back-to-back drags give it fewer chances.
#
# ⚠ TESTED AND IT IS NOT THE CAUSE. Default 0. The reasoning above is sound
# and the mechanism is real — the reports genuinely are not sent — but the
# game does not need them to accept a drag. A/B, alternating arms so burst
# position could not confound it:
#
#   one drag per staging      nudge=0  8/8      nudge=2  7/8
#   six drags back to back    nudge=0  11/12    nudge=2  11/12
#
# Kept as a parameter, and kept documented, so that the next person to notice
# that `send_hid_output` swallows the travel does not spend the evening on it
# again. What the burst run DID show is where to look next: the misses are all
# the FIRST drag of a burst, and only from the third burst onwards — by which
# point the 附近 list has passed its 12-row window. Position in the burst and
# the state of the destination list, not the reports.
DRAG_NUDGE_COUNTS = 0

# ── Getting hold of the device ────────────────────────────────────────────
PICO_RETRIES = 3         # A brief retry only covers the CDC port still closing
PICO_RETRY_S = 1.0       # behind a run of this tool. It is deliberately short:
                         # this Pico is shared with other agents, and a locked
                         # port usually means one of them is using it right now
                         # — that is a reason to get out of the way, not to
                         # wait longer and take it the moment they finish.


class NoPico(RuntimeError):
    """No Pico, so nothing the caller sends would reach the game as raw input."""


class _POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


def move_cursor(xy):
    """Absolute cursor placement. Used to park the cursor off a UI element
    before a screenshot — whatever is under it draws a hover highlight."""
    ctypes.windll.user32.SetCursorPos(int(xy[0]), int(xy[1]))


def cursor_pos():
    """Where the system cursor is. -> (x, y)

    MODULE LEVEL, and that is the whole point: `Pointer.cursor_pos` needs a
    Pointer, and constructing one TAKES THE PICO — a serial port shared with
    every other agent driving this game. So a script that only wants to read
    the cursor cannot afford the object, and three of them
    each wrote their own `POINT` struct rather than pay for it. The sharpest
    case was a probe that recorded a HUMAN dragging: touching the device is
    the one thing it must not do. `snap_on_key` is the one still here.

    Symmetric with `move_cursor` above, which exists for the same reason —
    park the cursor before a screenshot without owning a device.
    """
    pt = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


class Pointer:
    """Cursor placement, buttons, drags — and relative aiming moves.

    Two different mice live behind one object, and mixing them up is the
    mistake to avoid: move_to() is the SYSTEM CURSOR, which the UI screens
    follow, while move() is RAW MOTION, which turns the character's view. The
    game reads them off separate paths, which is why SendInput is good enough
    for the first and close to useless for the second.
    """

    def __init__(self):
        # ⚠ NO FALLBACK, AND NO `backend` PARAMETER. Both existed until
        # 2026-08-08: 'auto' would catch a failure here and drive the cursor
        # through SendInput instead.
        #
        # It had to go for a reason stronger than "SendInput does not work on
        # PUBG" (it does not -- the game reads the trigger and aiming off raw
        # HID). "Unplugged" and "someone else has it" are not the same
        # problem, and the fallback gave them the same answer: I cannot have
        # the device, so I will drive the mouse of whoever does. That is worse
        # than failing -- park() alone moves the cursor, and the run being
        # disturbed is mid-magazine with no way to tell its numbers just went
        # wrong.
        #
        # Seen 2026-08-03: a verify run started while a harvest held COM10,
        # fell back to SendInput, and went on to move the cursor and try to
        # toggle Tab under it. The check below survives that incident; the
        # fallback it used to guard does not.
        from press.pico_mouse import get_mouse, other_agents
        try:
            self.pico = get_mouse()
        except Exception as e:
            busy = other_agents()
            if busy:
                raise RuntimeError(
                    f'the Pico is held by another agent ({busy}). Wait for '
                    f'it, and do not kill it. ({e})') from e
            raise
        # ⚠ `self.backend` STOOD HERE as `'pico' if self.pico else
        # 'sendinput'` and is gone (2026-08-08). With one backend left it
        # was the constant 'pico', and it was being written into capture
        # metadata as if it distinguished runs.
        # What the last placement, click and drag actually did. Read by
        # InventoryControl's gesture journal; a gesture that "failed" and one
        # that was never really sent look identical from outside, and these are
        # the numbers that tell them apart.
        #
        # `last_click` was the missing one, and it is the expensive one: a
        # right-click aimed at an attachment slot lands on the WEAPON ROW when
        # the cursor is off, and that throws the whole gun on the floor. The
        # drag path aborts on a bad placement and says so; the click path only
        # printed a warning, so the one gesture that can lose a weapon was also
        # the one that left no record of where it actually went.
        self.last_place = {}
        self.last_click = {}
        self.last_drag = {}

    @classmethod
    def opened(cls, retries=PICO_RETRIES, retry_s=PICO_RETRY_S):
        """A Pointer with a Pico behind it, retried, and fatal if it never arrives.

        Two separate lessons. The retry: the CDC port stays locked for about a
        second after a previous run exits, so "busy" arrives as
        PermissionError and is indistinguishable from "unplugged" — one run
        died that way seconds after a successful one.

        The refusal: falling back to SendInput used to be a printed warning
        that the run then sailed straight past, into the operator prompt, ready
        to spend four minutes producing frames the game never acted on. A
        degraded backend is not a degraded run here, it is a worthless one.

        ⚠ THAT HALF IS NOW PLAIN Pointer()'s JOB. The SendInput backend was
        deleted on 2026-08-08 (PUBG reads raw HID, so its clicks and view
        moves were ignored no matter who sent them), and with it the
        `--backend sendinput` escape hatch this method used to refuse. What
        is left here that Pointer() does not do is THE RETRY, which is a
        different lesson and still a live one.
        """
        for i in range(retries):
            try:
                return cls()
            except Exception as e:
                last = e
            if i + 1 < retries:
                print(f'[pointer] no Pico yet — retrying in {retry_s:g}s '
                      f'({i + 1}/{retries - 1})', flush=True)
                time.sleep(retry_s)
        raise NoPico(
            f'no Pico after {retries} tries. There is no software fallback: '
            f'the game reads raw input, so a synthetic right-click is ignored '
            f'and every "ads" frame would be hip fire. This Pico is shared — '
            f'if the last error says access denied, another agent is mid-run, '
            f'so wait for it rather than killing it. Otherwise check the '
            f'cable.\n    last error: {last}') from last

    def move_to(self, x, y):
        ctypes.windll.user32.SetCursorPos(int(x), int(y))

    def move(self, dx, dy):
        """Relative motion — the mouse as an AIMING device, not as a cursor.

        Nothing to do with move_to(). That places the system cursor so a UI
        click lands on a widget; this rotates the character's view, and the
        game reads the two off different paths — which is why SetCursorPos is
        enough for the Tab screen and useless for turning.

        ⚠ TURNING HAS NO SOFTWARE PATH AT ALL. Before this was press/'s
        job, it was a ctypes.windll.user32.mouse_event copied into whichever
        tool needed to turn the view -- bypassing this layer entirely and
        using the legacy API. That copy was replaced by a SendInput fallback
        here, and the fallback was deleted on 2026-08-08: PUBG takes raw HID
        for aiming, so every one of those paths turned the view by exactly
        zero degrees while appearing to work.
        """
        if not dx and not dy:
            return
        # The firmware accumulates the delta and drains it at 127/report, so
        # an arbitrarily large jump is safe to send in one packet.
        self.pico.move(int(dx), int(dy))

    def click(self, buttons=0x01, hold_ms=CLICK_HOLD_MS, after=0.0):
        """Press and release wherever the cursor already is.

        Split out of click_at because not every button press is a UI click.
        ADS is the case that forced it: the right button toggles the sight,
        the cursor's position is irrelevant, and placing it first would be a
        SetCursorPos nobody asked for in the middle of a capture.

        `after` only means anything on the Pico path — see click_at, where the
        measurement behind it is written down.
        """
        self.pico.click(buttons, hold_ms)
        if after:
            time.sleep(after)

    def cursor_pos(self):
        # 方法保留是因为这一层的每个调用点都已经握着一个 Pointer；实现只有
        # 模块级那一份，见上面 cursor_pos() 里为什么它必须能脱离对象存在。
        return cursor_pos()

    def place(self, x, y, settle=MOVE_WAIT, tries=PLACE_TRIES):
        """Put the system cursor at (x, y) and see that it STAYS. -> bool

        SetCursorPos is instant; raw HID reports that arrive AFTER it are not,
        and they move the cursor again. The Pico is a passthrough, so reports
        keep arriving from whatever the firmware was last asked to send —
        measured on the Tab screen:

            move(900,0) with Tab ALREADY up   ->  cursor drift (450, 0)
            same move with Tab shut first     ->  cursor drift (0, 0)

        With Tab up the game spends raw motion on the CURSOR; with Tab shut it
        spends it on the view. `turn()` shuts Tab before moving for exactly
        that reason, but the close can be swallowed (docs/game_quirks.md) and
        then a turn's worth of counts lands on the cursor — arriving, crucially,
        over the following second rather than at once.

        That is why the FIRST drag of a sequence lands and the second does not:
        the placement is checked once, 120 ms after it is made, and the counts
        still in flight push the cursor off between the check and the press.
        Run 20260805_010546 lost its last five parts that way, each one to
        `drag aborted before press` or a release 76 px short of the target.

        Re-placing until a read agrees drains what is in flight instead of
        waiting a fixed time for it — a fixed sleep has to be longer than the
        worst case to work at all, and this returns as soon as it is quiet.
        """
        for i in range(tries):
            self.move_to(x, y)
            time.sleep(settle)
            gx, gy = self.cursor_pos()
            off = (gx - x, gy - y)
            if abs(off[0]) <= PLACE_TOL and abs(off[1]) <= PLACE_TOL:
                self.last_place = {'want': (x, y), 'tries': i + 1, 'ok': True,
                                   'off': off}
                return True
        self.last_place = {'want': (x, y), 'tries': tries, 'ok': False,
                           'off': off}
        return False

    def click_at(self, x, y, settle=CLICK_SETTLE, buttons=0x01,
                 hold_ms=CLICK_HOLD_MS, after=CLICK_AFTER_S):
        """Click at (x, y). buttons=0x02 is the right button.

        Right-click is a real gesture on the Tab screen, not a curiosity: it
        equips the item under the cursor straight onto the gun, which is one
        click where a drag is a press-travel-release with a settle at each end.

        Returns whether the cursor was where it was told to be when the button
        went down. THE CLICK IS STILL SENT when it was not, which is a
        deliberate difference from drag() — that one aborts before the press.
        The asymmetry is not defended, it is merely undisturbed: a click at an
        unverified position is how a gesture aimed at an attachment slot
        reaches the weapon row underneath and drops the gun. What changed is
        that the position is now RECORDED (`last_click`, and from there the
        gesture journal), so a run that loses a weapon says where the cursor
        was instead of leaving it to be re-derived.
        """
        ok = self.place(x, y, settle)
        self.last_click = {'want': (x, y), 'place': dict(self.last_place),
                           'got': self.cursor_pos(), 'buttons': buttons,
                           'ok': ok}
        if not ok:
            print(f'[pointer] warning: cursor landed at {self.cursor_pos()}, '
                  f'wanted {(x, y)} — something is still moving it')
        # This is click() plus the placement. The old SendInput branch capped
        # its hold at 60 ms; no caller has ever passed hold_ms, so at the
        # 20 ms default the cap never applied and dropping it changes nothing
        # any measurement here was taken with.
        self.click(buttons, hold_ms, after=after)
        return ok

    def right_click_at(self, x, y, **kw):
        """Right-click — on the Tab screen, "equip this"."""
        kw.setdefault('buttons', 0x02)
        return self.click_at(x, y, **kw)

    # ── Drag ──

    def _press(self, buttons=0x01):
        self.pico.click(buttons, DRAG_HOLD_MS)

    def _release(self, buttons=0x01):
        self.pico.click(buttons, 0)   # ends the hold on the next report

    def drag(self, src, dst, settle=MOVE_WAIT, steps=None,
             grab=DRAG_GRAB_WAIT, hover=DRAG_HOVER_WAIT,
             drop=DRAG_DROP_WAIT, buttons=0x01,
             nudge=DRAG_NUDGE_COUNTS, path=HUMAN_DRAG_PATH):
        """Press at `src`, travel to `dst`, release there.

        `steps` defaults to one interpolated position every DRAG_STEP_PX, so a
        long drag is not a sequence of jumps — see that constant, which was
        measured off a human hand rather than chosen.

        `path` REPLAYS a recorded gesture instead of interpolating one, and
        DEFAULTS TO ONE — HUMAN_DRAG_PATH, a real hand at 1 kHz. It is scaled
        onto `src` and `dst`, so the release point stays this call's parameter
        rather than whatever the recording ended on; `steps` is then ignored
        and the waits come from the recording rather than DRAG_STEP_WAIT.
        Travels longer than PATH_MAX_TRAVEL_PX fall back to interpolation —
        see that constant, it is what keeps this default from putting 158 px
        between positions on the long crossings.

        Pass path=None for the uniform gesture. `steps` only means anything
        then.

        False means the cursor did not go where it was told — another process
        moved it, or the coordinate is off-screen. The button is always
        released, including on an exception, because a stuck-down button in a
        UI screen leaves an item glued to the cursor.

        A True return says only that the gesture was performed; whether the
        game accepted the drop is for the caller to read back off the screen.
        """
        sx, sy = int(src[0]), int(src[1])
        tx, ty = int(dst[0]), int(dst[1])
        if path and ((tx - sx) ** 2 + (ty - sy) ** 2) ** 0.5 > PATH_MAX_TRAVEL_PX:
            path = None     # too long to scale a 15-update recording onto
        if path:
            # (ms, x, y) -> (wait_s, x, y), affinely mapped so the recording's
            # first sample lands on src and its last on dst. SCALED, not
            # translated: translating pins the start and leaves the end
            # wherever the hand finished, so the final position has to jump to
            # dst — which is the one part of the gesture this is trying to
            # reproduce faithfully. Scaling keeps the shape and the timing and
            # gives up only the absolute travel.
            t0, x0, y0 = float(path[0][0]), int(path[0][1]), int(path[0][2])
            x1, y1 = int(path[-1][1]), int(path[-1][2])
            fx = (tx - sx) / (x1 - x0) if x1 != x0 else 0.0
            fy = (ty - sy) / (y1 - y0) if y1 != y0 else 0.0
            plan, prev = [], t0
            for ms, x, y in path[1:]:
                plan.append((max(0.0, (float(ms) - prev) / 1000.0),
                             round(sx + (int(x) - x0) * fx),
                             round(sy + (int(y) - y0) * fy)))
                prev = float(ms)
            if plan:
                plan[-1] = (plan[-1][0], tx, ty)
            steps = len(plan)
        elif steps is None:
            dist = ((tx - sx) ** 2 + (ty - sy) ** 2) ** 0.5
            steps = int(min(DRAG_STEPS_MAX,
                            max(DRAG_STEPS_MIN, dist / DRAG_STEP_PX)))

        t_start = time.perf_counter()
        self.last_drag = {'src': (sx, sy), 'dst': (tx, ty), 'steps': steps,
                          'grab': None, 'held': None, 'release': None,
                          'grab_place': None, 'dst_place': None, 'ok': False,
                          'failed_at': None, 's': 0.0}

        # Settle before the press, not merely wait: see place(). One check at a
        # fixed delay is what let a turn's leftover counts push the cursor off
        # between the check and the button going down.
        if not self.place(sx, sy, settle):
            self.last_drag.update(grab_place=dict(self.last_place),
                                  grab=self.cursor_pos(),
                                  failed_at='before press',
                                  s=time.perf_counter() - t_start)
            print(f'[pointer] drag aborted before press: cursor at '
                  f'{self.cursor_pos()}, wanted {(sx, sy)} — it will not stay '
                  f'put, so something is still sending motion')
            return False
        self.last_drag.update(grab_place=dict(self.last_place),
                              grab=self.cursor_pos())

        self._press(buttons)
        last_arm = time.perf_counter()
        try:
            time.sleep(grab)
            for i in range(1, steps + 1):
                if path:
                    wait, px_, py_ = plan[i - 1]
                else:
                    f = i / steps
                    wait = DRAG_STEP_WAIT
                    px_ = round(sx + (tx - sx) * f)
                    py_ = round(sy + (ty - sy) * f)
                self.move_to(px_, py_)
                # A raw report so the game's input layer sees motion while the
                # button is down. See DRAG_NUDGE_COUNTS — without it the whole
                # travel is invisible to raw input and the gesture reads as a
                # click. Alternating sign keeps the net displacement at zero.
                if nudge:
                    self.pico.move(nudge if i % 2 else -nudge, 0)
                time.sleep(wait)
                now = time.perf_counter()
                if now - last_arm >= DRAG_REARM_S:
                    self.pico.click(buttons, DRAG_HOLD_MS)
                    last_arm = now
            # Hold the target the same way the source is held. A release 76 px
            # short of the drop point puts the item back in the column it came
            # from, and reads as "the drop did not land" rather than as a
            # cursor problem. Re-arm first and keep the settle short: place()
            # must not outlast DRAG_HOLD_MS or the button comes up mid-travel.
            self.pico.click(buttons, DRAG_HOLD_MS)
            last_arm = time.perf_counter()
            self.place(tx, ty, settle=hover, tries=PLACE_TRIES_HELD)
            self.last_drag.update(dst_place=dict(self.last_place),
                                  held=self.cursor_pos())
            time.sleep(hover)
        finally:
            self._release(buttons)
        time.sleep(drop)

        got = self.cursor_pos()
        self.last_drag.update(release=got, s=time.perf_counter() - t_start)
        if abs(got[0] - tx) > PLACE_TOL or abs(got[1] - ty) > PLACE_TOL:
            self.last_drag['failed_at'] = 'after release'
            print(f'[pointer] drag released at {got}, not {(tx, ty)}')
            return False
        self.last_drag['ok'] = True
        return True
