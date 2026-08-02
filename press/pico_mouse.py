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

# HID usage IDs for the keys this project drives. The Pico exposes a second
# HID interface (keyboard) purely so automated calibration can reload.
HID_KEY_R   = 0x15
HID_KEY_TAB = 0x2B
HID_KEY_COMMA = 0x36  # toggles the training-range item spawner
HID_KEY_C   = 0x06   # crouch toggle
HID_KEY_Z   = 0x1D   # prone toggle
HID_KEY_F   = 0x09   # pick up
HID_KEY_1   = 0x1E
HID_KEY_2   = 0x1F
HID_KEY_3   = 0x20

_instance = None


class PicoMouse:
    def __init__(self, port=None):
        self._port = port
        self._ser = None
        self._human = (0, 0)
        self._human_seen = False
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
                if not line.startswith(b'[hid]'):
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

    # Pre-fire lead: fraction of bullet_interval to shift pattern earlier.
    # Compensates Pico-click-to-game-recoil latency (~5-20ms USB + frame).
    # Proportional to RPM so fast guns (Vector) and slow guns (AKM) get
    # consistent lead relative to their fire rate.
    RECOIL_LEAD_FRAC = 0.30  # 30% of one bullet interval

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

        # Apply lead shift after merge (preserves per-bullet delta totals)
        lead = bullet_interval_s * self.RECOIL_LEAD_FRAC
        m_t = [max(0, t - lead) for t in m_t]

        nn = min(len(m_dx), self.MAX_POINTS)
        header = struct.pack('<BH', CMD_PATTERN_UPLOAD, nn)
        body = b''.join(
            struct.pack('<hhH', int(m_dx[j]), int(m_dy[j]), int(m_t[j] * 1000))
            for j in range(nn)
        )
        self._write(header + body)

    def clear_pattern(self):
        self._write(bytes([CMD_PATTERN_CLEAR]))

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


def _find_pico_port():
    """Auto-detect Pico CDC port by VID:PID."""
    for p in serial.tools.list_ports.comports():
        if p.vid == 0xCAFE and p.pid in (0x4001, 0x4005):
            return p.device
    raise RuntimeError(
        "Pico mouse not found. Check USB connection or set PICO_PORT in config.py."
    )


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
