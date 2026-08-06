"""Pico HID Mouse — serial driver (passthrough mode).

The Pico intercepts the real mouse via PIO USB Host, forwards to PC as HID,
and adds recoil compensation when left click is held.

PC sends recoil patterns to the Pico via CDC serial.

Protocol (PC → Pico):
  [0x10][len_lo][len_hi] + len×6 bytes   upload recoil pattern
      each point: [dx_i16_le][dy_i16_le][t_ms_u16_le]
  [0x11]                                  clear pattern
  [0x12][0/1]                             enable/disable recoil
"""

import os
import queue
import threading
import time
import struct
import serial
import serial.tools.list_ports

CMD_PATTERN_UPLOAD = 0x10
CMD_PATTERN_CLEAR  = 0x11
CMD_RECOIL_ENABLE  = 0x12
CMD_MOVE           = 0x13
CMD_CLICK          = 0x14
CMD_MOVE_CLICK     = 0x15
CMD_AIM_MODE       = 0x16
CMD_SET_DELTA      = 0x17
CMD_KEY            = 0x18
CMD_PATTERN_READ   = 0x19
CMD_RECOIL_SIM     = 0x1A

# HID usage IDs for the keys this project drives. The Pico exposes a second
# HID interface (keyboard) purely so automated calibration can reload.
HID_KEY_B   = 0x05   # fire-mode toggle; cycles single/burst/full
HID_KEY_R   = 0x15
HID_KEY_TAB = 0x2B
HID_KEY_COMMA = 0x36  # toggles the training-range item spawner
HID_KEY_C   = 0x06   # crouch toggle
HID_KEY_Z   = 0x1D   # prone toggle
HID_KEY_F   = 0x09   # pick up; also PLAY in the lobby
HID_KEY_1   = 0x1E
HID_KEY_2   = 0x1F
HID_KEY_3   = 0x20
HID_KEY_ESC = 0x29   # opens/closes the system menu

_instance = None


class PicoMouse:
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

    def _write(self, data):
        """Write with non-blocking error handling."""
        try:
            self._ser.write(data)
        except serial.SerialTimeoutException:
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

    MAX_POINTS = 300  # must match Pico firmware MAX_PATTERN_POINTS

    # How long after the click the pattern should START. Positive = later.
    #
    # This used to be RECOIL_LEAD_FRAC = 0.30, shifting the pattern 30% of a
    # bullet interval EARLIER, on the reasoning that USB and frame latency put
    # the compensation behind the recoil. Both the sign and the units were
    # wrong, and it was never measured.
    #
    # Write P for the input-and-render delay (command issued -> that command
    # visible on screen), C for the capture delay (visible -> we notice), W for
    # the weapon's own trigger-to-round delay, T for the bullet interval.
    #
    # Measured on the AUG, 2026-08-02, red dot, training range:
    #
    #   tools/probe_input_latency.py   L = P + C     = 38 ms   (n=44, sd 4.8)
    #   tools/probe_shot_latency.py    S = W + P + C = 51 ms   (n=36, sd 8.1)
    #                                  W = S - L     = 13 ms
    #
    # S is taken from the AMMO COUNTER, not from the view starting to move.
    # Both were recorded and the counter is the sound one: it changes as a
    # step, so the first frame that shows it is the answer, while the recoil
    # ramps in (0.9 counts in the first 7 ms of a bullet, 2.7 in the middle) so
    # any motion threshold fires a frame or two late. The measured gap between
    # them was one-sided -- 7 taps of 16 landed in the same frame and NOT ONE
    # put the recoil first -- which is the shape of a detection bias, not of
    # two events happening at different times. They are simultaneous.
    #
    # That simultaneity is load-bearing twice over. It is why fit_curve can
    # anchor its bins on the first counter change and have the capture latency
    # cancel out; and it is why the derivation below closes.
    #
    # The firmware schedules pattern point k at t_k after the click, and those
    # counts reach the screen at t_k + P. Round k's recoil reaches the screen
    # at W + P + k*T. Setting them equal:
    #
    #     t_k = W + k*T
    #
    # P and C are GONE. Neither the render pipeline nor the capture chain
    # enters the offset at all -- only the weapon's own delay does. The same
    # cancellation covers the spread: the firmware pours point k out over
    # [t_k, t_k + T], which lands on screen over [W + k*T, W + (k+1)*T], and
    # that is exactly the window the round's own recoil occupies.
    #
    # Two earlier values were wrong for two different reasons, and both looked
    # like "the first shot is not compensated":
    #
    #   RECOIL_LEAD_FRAC = 0.30 shifted the pattern EARLIER by a fraction of an
    #   interval. Wrong sign, wrong units, never measured.
    #
    #   36 ms came from S = 72, measured with a coarse motion threshold on the
    #   ramping recoil. Re-measured off the counter it is 51, and W is 13.
    #
    # Milliseconds, not a fraction of the interval: USB transport, input
    # sampling and a fire animation do not get faster because the gun does.
    RECOIL_FIRE_DELAY_MS = 13

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
    # It is not. tools/probe_kick_profile.py fires with the compensation off
    # and folds the view's motion onto one bullet interval. On the AUG, twelve
    # samples across an 88 ms interval:
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

    def upload_pattern(self, dx_s, dy_s, t_s, bullet_interval_s=0.1):
        """Upload pattern merged to one point per bullet.

        Groups sample points by bullet time windows (from RPM),
        sums dx/dy per bullet. Lead shift applied after merge.
        """
        n = len(dx_s)
        if n == 0:
            return
        m_dx, m_dy, m_t = [], [], []
        bullet = 0
        sum_dx, sum_dy = 0.0, 0.0
        for i in range(n):
            # Start new bullet when t_s crosses next bullet boundary
            while t_s[i] >= (bullet + 1) * bullet_interval_s and i > 0:
                m_dx.append(sum_dx)
                m_dy.append(sum_dy)
                m_t.append(bullet * bullet_interval_s)
                sum_dx, sum_dy = 0.0, 0.0
                bullet += 1
            sum_dx += dx_s[i]
            sum_dy += dy_s[i]
        # Flush last bullet
        if sum_dx != 0 or sum_dy != 0:
            m_dx.append(sum_dx)
            m_dy.append(sum_dy)
            m_t.append(bullet * bullet_interval_s)

        # Line the compensation up with the rounds; see RECOIL_FIRE_DELAY_MS.
        # Applied after the merge so per-bullet totals are untouched — this
        # moves when the compensation plays, not how much of it there is.
        offset = self.RECOIL_FIRE_DELAY_MS / 1000.0
        m_t = [t + offset for t in m_t]

        nn = min(len(m_dx), self.MAX_POINTS)
        header = struct.pack('<BH', CMD_PATTERN_UPLOAD, nn)
        body = b''.join(
            struct.pack('<hhH', int(m_dx[j]), int(m_dy[j]), int(m_t[j] * 1000))
            for j in range(nn)
        )
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
        lines = self._ask(struct.pack('<BH', CMD_RECOIL_SIM, int(iters)),
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
        self._write(struct.pack('<BB', CMD_RECOIL_ENABLE, 1 if enabled else 0))

    def move(self, dx, dy):
        self._write(struct.pack('<Bhh', CMD_MOVE, int(dx), int(dy)))

    def click(self, buttons=0x01, duration_ms=80):
        self._write(struct.pack('<BBH', CMD_CLICK, buttons, duration_ms))

    def set_aim_mode(self, enabled):
        self._write(struct.pack('<BB', CMD_AIM_MODE, 1 if enabled else 0))

    def set_delta(self, dx, dy):
        self._write(struct.pack('<Bhh', CMD_SET_DELTA, int(dx), int(dy)))

    def key(self, keycode, duration_ms=60):
        """Hold a key on the Pico's keyboard interface for duration_ms.

        The firmware emits a report only on press and on release, so one call
        is one keystroke — re-reporting the same keycode would look like
        auto-repeat to the host and fire the action several times.
        """
        self._write(struct.pack('<BBH', CMD_KEY, int(keycode),
                                int(duration_ms)))

    def reload(self, duration_ms=60):
        """Press R. Used by automated training-range calibration."""
        self.key(HID_KEY_R, duration_ms)

    def move_click(self, dx, dy, buttons=0x01, delay_ms=0, duration_ms=80):
        if delay_ms == 0:
            dist = max(abs(int(dx)), abs(int(dy)))
            delay_ms = max(5, dist // 127 + 10)
        self._write(struct.pack('<BhhBHH', CMD_MOVE_CLICK,
                    int(dx), int(dy), buttons, delay_ms, duration_ms))

    def close(self):
        if self._ser:
            self._ser.close()


def _other_python():
    """Other python processes in this project, as 'pid cmdline' lines."""
    try:
        import psutil
    except ImportError:
        return ''
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
    """Return singleton mouse backend (PicoMouse or SoftMouse per config)."""
    global _instance
    if _instance is None:
        from config import MOUSE_BACKEND
        if MOUSE_BACKEND == 'soft':
            from press.soft_mouse import SoftMouse
            _instance = SoftMouse()
        else:
            if port is None:
                from config import PICO_PORT
                port = PICO_PORT or None  # None = auto-detect in PicoMouse
            _instance = PicoMouse(port)
    return _instance
