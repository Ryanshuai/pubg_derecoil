"""Pico HID Mouse — serial driver (passthrough mode).

The Pico intercepts the real mouse via PIO USB Host, forwards to PC as HID,
and adds recoil compensation when left click is held.

PC sends recoil patterns to the Pico via CDC serial.

The wire format is NOT described here. It lives in press/protocol/protocol.toml,
which generates both this file's constants and the firmware's — a summary in
this docstring would be a third copy, and it was already drifting: it listed
three of the thirteen commands.
"""

import os
import queue
import threading
import time
import struct
import psutil
import serial
import serial.tools.list_ports

import config

# The wire contract with the firmware. These used to be typed out here AND in
# press/firmware/src/main.c, kept in step by a comment; press/protocol/
# protocol.toml is now the one place they are written, and both ends are
# generated from it. CMD_*_FMT includes the opcode as its first packed field.
from press.protocol import (CMD_AIM_MODE, CMD_AIM_MODE_FMT,
                      CMD_CLICK, CMD_CLICK_FMT,
                      CMD_KEY, CMD_KEY_FMT,
                      CMD_MOVE, CMD_MOVE_FMT,
                      CMD_MOVE_CLICK, CMD_MOVE_CLICK_FMT,
                      CMD_PATTERN_CLEAR,
                      CMD_PATTERN_READ,
                      CMD_PATTERN_UPLOAD, CMD_PATTERN_UPLOAD_FMT,
                      CMD_RECOIL_ENABLE, CMD_RECOIL_ENABLE_FMT,
                      CMD_RECOIL_SIM, CMD_RECOIL_SIM_FMT,
                      CMD_SET_DELTA, CMD_SET_DELTA_FMT,
                      MAX_PATTERN_POINTS, PATTERN_POINT_FMT)

# HID usage IDs for the keys this project drives. The Pico exposes a second
# HID interface (keyboard) purely so automated calibration can reload.
#
# ⚠ NOT part of the wire contract, and deliberately not in protocol/: the
# firmware forwards `keycode` to the HID descriptor without interpreting it,
# so the two ends need not agree on these — only this file and the GAME do.
HID_KEY_B   = 0x05   # fire-mode toggle; cycles single/burst/full
HID_KEY_R   = 0x15
HID_KEY_TAB = 0x2B
HID_KEY_COMMA = 0x36  # toggles the training-range item spawner
HID_KEY_W   = 0x1A   # forward; the only thing here that MOVES the character
HID_KEY_S   = 0x16   # back
HID_KEY_C   = 0x06   # crouch toggle
HID_KEY_Z   = 0x1D   # prone toggle
HID_KEY_F   = 0x09   # pick up; also PLAY in the lobby
HID_KEY_1   = 0x1E
HID_KEY_2   = 0x1F
HID_KEY_3   = 0x20
HID_KEY_ESC = 0x29   # opens/closes the system menu
HID_KEY_M   = 0x10   # opens/closes the map; in the training range the map is
                     # also the teleporter (control/lobby.goto_range)

_instance = None


class PicoMouse:
    # ⚠ can_key / can_click STOOD HERE AND ARE GONE (2026-08-08). They let a
    # caller ask "can this backend press a key / click" instead of probing
    # with hasattr, which was the right shape while there were two backends.
    # With SoftMouse deleted they were both constant True, so the one live
    # test -- `if not rig.mouse.can_key` in collect_templates -- guarded a
    # branch that could not be reached. A declaration that cannot come out
    # False is not a declaration, it is a comment that costs an attribute
    # lookup. Bring them back the day a second backend does, not before.

    def __init__(self, port=None):
        self._port = port
        self._ser = None
        self._human = (0, 0)
        self._human_seen = False
        # Firmware chatter that is not a human-movement report. Bounded: a
        # reply nobody collects must not grow without limit, and a stale one
        # must not be mistaken for an answer -- ask() drains before it writes.
        self._replies = queue.Queue(maxsize=1024)
        self._connect()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ── human movement feedback ──
    #
    # The Pico forwards the real mouse and adds recoil compensation on top, so
    # what lands on screen is hand + compensation + recoil. Calibration knows
    # the compensation it asked for and measures the screen; without knowing
    # the hand it has to assume the hand was still, and any nudge during a
    # burst is booked as recoil. The firmware publishes a running total of the
    # passthrough movement so that term can be removed exactly — which is what
    # makes it possible to learn the curve while actually playing.

    def _read_loop(self):
        """Consume the Pico's CDC chatter. Totals are cumulative, so a missed
        or truncated line costs nothing: the next one carries the full story."""
        buf = b''
        while True:
            ser = self._ser
            if ser is None:
                time.sleep(0.05)
                continue
            try:
                chunk = ser.read(256)
            except Exception:
                time.sleep(0.05)
                continue
            if not chunk:
                continue
            buf += chunk
            if len(buf) > 4096:              # only the newest lines matter
                buf = buf[-1024:]
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                line = line.strip()
                if not line.startswith(b'[hid]'):
                    # Everything else the firmware says goes to whoever asked
                    # for it. This used to be `continue`, which meant the CDC
                    # link was write-only for anything but the human totals --
                    # and that is why the only way to find out what the
                    # firmware was doing was to fire in the game and measure
                    # the screen. See CMD_PATTERN_READ.
                    if line.startswith(b'['):
                        try:
                            self._replies.put_nowait(line.decode('ascii',
                                                                 'replace'))
                        except queue.Full:
                            pass
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                try:
                    self._human = (int(parts[1]), int(parts[2]))
                    self._human_seen = True
                except ValueError:
                    pass

    def human_totals(self):
        """(dx, dy) mouse counts the human has moved since the Pico booted.

        Cumulative and monotonic in the sense that callers should difference
        two samples; the absolute value has no meaning on its own.
        """
        return self._human

    def human_available(self):
        """False against firmware that predates the reporting, so a caller can
        say so rather than silently treating a still hand as ground truth."""
        return self._human_seen

    def _connect(self):
        """Open serial connection. Auto-detects port if not specified."""
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        port = self._port or _find_pico_port()
        try:
            self._ser = serial.Serial(port, baudrate=115200, timeout=0.1,
                                      write_timeout=0.1)
        except serial.SerialException as e:
            # One Pico, several tools, no lock. The raw error is a Windows
            # access-denied deep in pyserial, which reads like a driver fault;
            # naming the process actually holding the port is the difference
            # between "retry in a minute" and a debugging session.
            raise serial.SerialException(
                f"{e}\n"
                f"    The Pico is a single shared device and something else "
                f"already has it.\n"
                f"    Holding it now: {_other_python() or 'unknown'}\n"
                f"    Wait for it to finish rather than killing it — it is "
                f"probably another agent mid-run.") from None
        self._port = port
        print(f"[pico] connected on {port}", flush=True)

    def _write(self, data, critical=False):
        """Write with non-blocking error handling.

        ⚠ `critical` EXISTS BECAUSE THE DEFAULT IS ONLY SAFE FOR MOTION. A
        dropped MOVE is one lost frame of compensation and the next packet
        fixes it, which is what the `pass` below is for. A dropped
        CMD_RECOIL_ENABLE is the difference between a run that measures raw
        recoil and one that measures it compensated, under a filename saying
        otherwise -- and it was silent, because this swallowed the exact
        exception it names. Control-plane writes pass critical=True and the
        timeout reaches the caller.
        """
        try:
            self._ser.write(data)
        except serial.SerialTimeoutException:
            if critical:
                raise
            pass  # CDC backpressure — drop this packet, Pico will catch up
        except Exception:
            # Mark disconnected; reconnect lazily on next call to avoid blocking
            print("[pico] write failed, will reconnect on next call", flush=True)
            self._ser = None

        if self._ser is None:
            try:
                self._port = None
                self._connect()
            except Exception as e:
                print(f"[pico] reconnect failed: {e}", flush=True)

    # ⚠ THE FIRMWARE CLAMPS rather than rejects, so exceeding this loses the
    # tail of the curve with no error anywhere. That is why the number is
    # imported and not typed: it was 300 in two files held together by a
    # comment, and raising one of them would have been silent.
    MAX_POINTS = MAX_PATTERN_POINTS

    # The click-to-first-round offset, and the ~70 lines of measurement
    # behind why it is 13 and not 21 or 36, live in config.py. Kept as a
    # class attribute because callers reach it as
    # PicoMouse.RECOIL_FIRE_DELAY_MS.
    RECOIL_FIRE_DELAY_MS = config.RECOIL_FIRE_DELAY_MS

    # ...and that is the WHOLE offset. There is no half-interval term, and
    # there used to be one.
    #
    # The reasoning behind it was: the firmware spreads a bullet's compensation
    # evenly over the interval to the next bullet (get_recoil_delta in
    # pico_firmware/src/main.c) rather than delivering it as a pulse, so its
    # centre of mass sits half an interval late; subtract half an interval and
    # the ramp is centred on the shot. That is correct arithmetic about our
    # side and it silently assumed the game's recoil IS a pulse.
    #
    # It is not. A kick-profile probe fired with the compensation off and
    # folded the view's motion onto one bullet interval; it was deleted on
    # 2026-08-08 with the coordinate it imported, so THE TWELVE SAMPLES
    # BELOW ARE THE RECORD. On the AUG, across an 88 ms interval:
    #
    #     0.9  1.3  0.7  2.7  2.5  2.3  2.2  1.9  1.6  1.2  1.2  1.4
    #
    # The kick is spread across the whole interval too, and monotone -- no
    # punch, no overshoot, nothing to centre on. Both sides are ramps over the
    # same interval, both centroids sit half an interval after their start, and
    # the two halves CANCEL. Subtracting it from our side alone put every
    # bullet's compensation half an interval early.
    #
    # On the AUG that is 44 ms of an 88 ms interval, and the first round pays
    # the most: its compensation was clamped to t=0, went out over 0..80 ms and
    # was visible over 36..116 ms, while the round's own recoil did not appear
    # until 72 ms. Most of the first bullet's compensation was spent pushing
    # the view down before the round had left the barrel -- which is exactly
    # what "the first shot is not compensated" looks like from the chair.
    #
    # The residual could not see it. fit_curve measures per-bullet sums on bins
    # anchored at the first ammo change, the firmware plays on a grid anchored
    # at the click, and the fit converges to whatever nulls the sums on ITS
    # grid: a curve that is distorted and self-consistent, reporting residuals
    # of a couple of counts while the screen still jumps.

    def upload_pattern(self, dx_s, dy_s, t_s):
        """Upload the curve AS GIVEN. One knot in, one knot out.

        ⚠ IT USED TO MERGE TO ONE POINT PER BULLET, and MODEL.md §4 named this
        method as the single thing standing between the model and the
        firmware: "挡在中间的只有一处". Whatever grid the fitter produced, this
        re-binned it by `bullet_interval_s` and summed — so a 17 ms curve
        arrived at the Pico as 42 knots however carefully it had been fitted.

        The firmware never wanted that. It is a TIME-DOMAIN POLYLINE PLAYER:
        `get_recoil_delta` runs every 1 ms and spreads each knot's delta evenly
        over `dur_i = t_ms[i+1] - t_ms[i]`, with room for 300 knots
        (MAX_PATTERN_POINTS). Merging threw away the resolution it already had.

        The cost is not abstract. a deleted kick-profile probe; the 12 samples below are the record folded an AUG's
        kick onto one 88 ms interval, compensation off, 12 samples:

            0.9  1.3  0.7  2.7  2.5  2.3  2.2  1.9  1.6  1.2  1.2  1.4

        The rate varies 4x WITHIN one bullet interval, and a polyline whose
        knots are 85 ms apart is constant across that span by construction. It
        cannot follow that profile at any amplitude — the crosshair drifts
        ±10% of a round's worth inside every interval and a 42-knot curve has
        no freedom to take it out. A 17 ms grid cuts the same profile into 5
        pieces and can.

        The `bullet_interval_s` parameter that drove that merge is GONE
        (2026-08-08). It outlived the merge by one step on purpose: two
        callers passed it positionally, so dropping it silently would have
        slid `t_s` into its place. tools/check_params.py carried the debt with
        a machine-checkable exit condition -- "nobody passes a fourth
        positional argument any more" -- and this is that condition being met.
        """
        n = len(dx_s)
        if n == 0:
            return
        m_dx, m_dy, m_t = list(dx_s), list(dy_s), list(t_s)

        # Line the compensation up with the rounds; see RECOIL_FIRE_DELAY_MS.
        # Applied after the merge so per-bullet totals are untouched — this
        # moves when the compensation plays, not how much of it there is.
        offset = self.RECOIL_FIRE_DELAY_MS / 1000.0
        m_t = [t + offset for t in m_t]

        nn = min(len(m_dx), self.MAX_POINTS)
        # QUANTISE WITH A CARRY, because the wire format is int16 per knot and
        # `int()` on each one independently throws away up to a count EVERY
        # KNOT. The loss is not a rounding detail, it scales with how fine the
        # grid is:
        #
        #   m416, 895.3 counts total, measured 2026-08-08
        #     41 knots  (85 ms)   mean 21.84/knot   truncation loses  19.3  2.2%
        #    225 knots  (17 ms)   mean  3.98/knot   truncation loses  73.3  8.2%
        #
        # MODEL.md's whole point is the fine grid, so the finer the curve gets
        # the more of it this used to eat -- and it ate it SILENTLY and
        # REPEATABLY. Three fitting iterations sat at 135-146 px of residual
        # drift because the fit kept asking for 895 counts, the firmware kept
        # playing 840, and the next round's samples therefore kept measuring
        # the same 55-count shortfall. A stable, self-consistent loop that
        # never converges, which is this repository's signature failure.
        #
        # The carry is the same trick the firmware already uses per millisecond
        # (recoil_accum_x/y in get_recoil_delta): emit the integer part, keep
        # the remainder, add it to the next one. Total error over the whole
        # curve is then bounded by one count instead of by one count per knot.
        acc_x = acc_y = 0.0
        pts = []
        for j in range(nn):
            acc_x += m_dx[j]
            acc_y += m_dy[j]
            ix, iy = int(acc_x), int(acc_y)
            acc_x -= ix
            acc_y -= iy
            pts.append((ix, iy, int(m_t[j] * 1000)))
        header = struct.pack(CMD_PATTERN_UPLOAD_FMT, CMD_PATTERN_UPLOAD, nn)
        body = b''.join(struct.pack(PATTERN_POINT_FMT, *p) for p in pts)
        self._write(header + body)

    def clear_pattern(self):
        self._write(bytes([CMD_PATTERN_CLEAR]))

    # ── readback ──
    #
    # What the firmware ACTUALLY holds and ACTUALLY plays, asked directly
    # rather than inferred from the screen. Both of these are bench tests: no
    # game, no window, no HID output, and they finish in milliseconds.

    def _ask(self, payload, until, timeout=2.0):
        """Send `payload`, collect reply lines until one matches `until`.

        Drains whatever was already queued FIRST. A stale line from an earlier
        question answering this one is the classic way a readback test passes
        against firmware that never replied.
        """
        try:
            while True:
                self._replies.get_nowait()
        except queue.Empty:
            pass
        self._write(payload)
        lines, deadline = [], time.time() + timeout
        while time.time() < deadline:
            try:
                line = self._replies.get(timeout=max(0.01,
                                                     deadline - time.time()))
            except queue.Empty:
                break
            lines.append(line)
            if until(line):
                return lines
        return lines

    def read_pattern(self, timeout=2.0):
        """The stored pattern, as the firmware has it.

        -> [{'i', 't_ms', 'dx', 'dy', 'dur_ms'}, ...], or None if the firmware
        predates the readback (older builds ignore an unknown command byte).

        `dur_ms` is computed BY THE FIRMWARE, from bullet_duration(). That is
        the point: recomputing the rule here would compare this file with
        itself. The last bullet's duration is the one that has been wrong.
        """
        lines = self._ask(bytes([CMD_PATTERN_READ]),
                          lambda ln: ln.startswith('[pat] end'), timeout)
        if not any(ln.startswith('[pat]') for ln in lines):
            return None
        out = []
        for ln in lines:
            p = ln.split()
            # "[pat] i <i> <t_ms> <dx> <dy> <dur>" — seven tokens.
            if len(p) == 7 and p[1] == 'i':
                out.append({'i': int(p[2]), 't_ms': int(p[3]),
                            'dx': int(p[4]), 'dy': int(p[5]),
                            'dur_ms': int(p[6])})
        return out

    def simulate_recoil(self, iters=500, timeout=5.0):
        """Run the firmware's per-bullet jitter `iters` times over the stored
        pattern, emitting nothing. -> dict, or None on old firmware.

        {'iters', 'bullets', 'cmd_dx', 'cmd_dy', 'emit_dx', 'emit_dy'}

        emit_* are floats. The difference from cmd_* is the jitter's bias, and
        it is supposed to be zero: both jitter terms are zero-mean by
        intention, and were not by implementation for as long as anyone had
        only the screen to check them with.
        """
        lines = self._ask(struct.pack(CMD_RECOIL_SIM_FMT, CMD_RECOIL_SIM, int(iters)),
                          lambda ln: ln.startswith('[sim]'), timeout)
        for ln in lines:
            p = ln.split()
            # "[sim] <iters> <bullets> <cx> <cy> <ex> <ey>" — seven tokens.
            if len(p) == 7 and p[0] == '[sim]':
                return {'iters': int(p[1]), 'bullets': int(p[2]),
                        'cmd_dx': int(p[3]), 'cmd_dy': int(p[4]),
                        'emit_dx': int(p[5]) / 1000.0,
                        'emit_dy': int(p[6]) / 1000.0}
        return None

    def set_recoil_enabled(self, enabled):
        """Flip compensation. Raises if the packet could not be written.

        It still cannot tell you the firmware ACTED on it — read it back with
        recoil_enabled(). This only removes the case where the byte never
        left the host and nobody was told.
        """
        self._write(struct.pack(CMD_RECOIL_ENABLE_FMT, CMD_RECOIL_ENABLE,
                                1 if enabled else 0),
                    critical=True)

    def recoil_enabled(self, timeout=2.0):
        """Is compensation on, as the FIRMWARE has it? -> True/False/None.

        None means the firmware predates the readback (its `[pat] end` line
        carries no flag), which is NOT the same as False and must not be
        rounded to it: a caller that treats "cannot tell" as "disarmed" has
        rebuilt the hole this was added to close.

        Rides on CMD_PATTERN_READ rather than a command of its own, because
        the flag belongs to the same question the dump already answers —
        "what is the firmware actually going to do" — and one round trip is
        cheaper than two.
        """
        lines = self._ask(bytes([CMD_PATTERN_READ]),
                          lambda ln: ln.startswith('[pat] end'), timeout)
        for ln in lines:
            if ln.startswith('[pat] end'):
                parts = ln.split()
                # "[pat] end <0|1>" on current firmware, "[pat] end" on old.
                return bool(int(parts[2])) if len(parts) >= 3 else None
        return None

    def move(self, dx, dy):
        self._write(struct.pack(CMD_MOVE_FMT, CMD_MOVE, int(dx), int(dy)))

    def click(self, buttons=0x01, duration_ms=80):
        """-> the perf_counter instant the bytes went out.

        THE RETURN VALUE IS THE ORIGIN OF THE WHOLE MEASUREMENT. MODEL.md puts
        every sample on an axis of "how long since the click", and the firmware
        anchors the compensation curve on the same event (`fire_start_ms`, set
        when this command is processed). Two things ride on stamping it HERE
        rather than after the call returns:

          - `_write` can block on the serial port. A stamp taken afterwards
            includes that wait, and the wait is not constant.
          - The gap between this instant and the firmware's `fire_start_ms` is
            USB transit plus command processing -- small, and more importantly
            THE SAME EVERY MAGAZINE, which is what makes it cancel. See the
            note in MODEL.md: a constant offset between our origin and the
            firmware's is absorbed by the fitted curve's own shape, but only
            if it really is constant.
        """
        t = time.perf_counter()
        self._write(struct.pack(CMD_CLICK_FMT, CMD_CLICK, buttons, duration_ms))
        return t

    def set_aim_mode(self, enabled):
        self._write(struct.pack(CMD_AIM_MODE_FMT, CMD_AIM_MODE,
                                1 if enabled else 0))

    def set_delta(self, dx, dy):
        self._write(struct.pack(CMD_SET_DELTA_FMT, CMD_SET_DELTA, int(dx), int(dy)))

    def key(self, keycode, duration_ms=60):
        """Hold a key on the Pico's keyboard interface for duration_ms.

        The firmware emits a report only on press and on release, so one call
        is one keystroke — re-reporting the same keycode would look like
        auto-repeat to the host and fire the action several times.
        """
        self._write(struct.pack(CMD_KEY_FMT, CMD_KEY, int(keycode),
                                int(duration_ms)))

    def reload(self, duration_ms=60):
        """Press R. Used by automated training-range calibration."""
        self.key(HID_KEY_R, duration_ms)

    def move_click(self, dx, dy, buttons=0x01, delay_ms=0, duration_ms=80):
        if delay_ms == 0:
            dist = max(abs(int(dx)), abs(int(dy)))
            delay_ms = max(5, dist // 127 + 10)
        self._write(struct.pack(CMD_MOVE_CLICK_FMT, CMD_MOVE_CLICK,
                    int(dx), int(dy), buttons, delay_ms, duration_ms))

    def close(self):
        if self._ser:
            self._ser.close()


def _other_python():
    """Other python processes in this project, as 'pid cmdline' lines.

    psutil is imported at module level, not behind `try: import psutil /
    except ImportError: return ''`. That fallback translated "the library is
    missing" into **"nobody else is running"** — the one answer that is unsafe
    to be wrong about, since the whole point of this call is to stop two agents
    driving one Pico and one game window. psutil is a declared dependency
    (pixi.toml), so its absence is a broken environment, and `pixi run smoke`
    is where a broken environment should surface — loudly, at import, once.
    """
    me = os.getpid()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).lower()
    out = []
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if p.info['pid'] == me or 'python' not in (p.info['name'] or ''):
                continue
            cmd = ' '.join(p.info['cmdline'] or [])
            if root in cmd.lower().replace('/', os.sep):
                out.append(f"pid {p.info['pid']}: {cmd[:100]}")
        except Exception:
            continue
    return '; '.join(out)


def other_agents():
    """Other python processes running out of this project, or '' if none.

    Both the Pico and the game window are single-tenant, and neither is
    locked. Colliding shows up as symptoms rather than errors — keystrokes
    landing in someone else's UI state, a panel that "would not open" because
    another run just closed it — so a tool about to drive the game should ask
    this up front rather than diagnose it afterwards. _connect() reports the
    same thing, but only once the serial port is already contended, which is
    later than it needs to be.
    """
    return _other_python()


# TinyUSB's example VID, and the two PIDs this firmware has shipped under.
# Named rather than inline because the answer to "is the Pico on the bus" has
# to be the SAME answer everywhere: tools/smoke_check.py had its own copy of
# both literals, so a firmware built under a third PID would have made the
# cold-start check report a healthy Pico that robot.py could not open.
PICO_VID = 0xCAFE
PICO_PIDS = (0x4001, 0x4005)


def find_pico():
    """The Pico's serial port, or None. -> ListPortInfo | None

    Returns the port OBJECT, not its device string, because the callers that
    only want to report differ from the ones that want to connect -- smoke
    prints vid/pid, `_find_pico_port` wants `.device`, and neither should be
    re-scanning comports to get the other half.
    """
    for p in serial.tools.list_ports.comports():
        if p.vid == PICO_VID and p.pid in PICO_PIDS:
            return p
    return None


def _find_pico_port():
    """Auto-detect Pico CDC port by VID:PID."""
    p = find_pico()
    if p is None:
        raise RuntimeError(
            "Pico mouse not found. Check USB connection or set PICO_PORT in config.py."
        )
    return p.device


def get_mouse(port=None):
    """Return the singleton mouse. Raises if no Pico is reachable.

    ⚠ THERE IS NO SECOND BACKEND, and the choice is not an oversight. A
    SendInput one (the soft_mouse module, deleted 2026-08-08) was
    selectable via config.MOUSE_BACKEND = 'soft'. PUBG reads aiming and the
    trigger off RAW HID, so on the one game this repo drives, that backend's
    click, aim_mode and set_delta were all no-ops -- it could place the system
    cursor and nothing else. Nothing ever selected it, Pointer.__init__
    rejected it on sight, and three error messages nonetheless recommended it
    to anyone without a Pico. Offering a backend that silently does nothing is
    worse than having none.
    """
    global _instance
    if _instance is None:
        if port is None:
            from config import PICO_PORT
            port = PICO_PORT or None  # None = auto-detect in PicoMouse
        _instance = PicoMouse(port)
    return _instance
