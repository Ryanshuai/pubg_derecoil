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

_instance = None


class PicoMouse:
    def __init__(self, port):
        self._ser = serial.Serial(port, baudrate=115200, timeout=0.1)

    def upload_pattern(self, dx_s, dy_s, t_s):
        """Upload recoil pattern. dx_s/dy_s are float lists, t_s in seconds."""
        n = len(dx_s)
        header = struct.pack('<BH', CMD_PATTERN_UPLOAD, n)
        body = b''
        for dx, dy, t in zip(dx_s, dy_s, t_s):
            t_ms = int(t * 1000)
            body += struct.pack('<hhH', int(dx), int(dy), t_ms)
        self._ser.write(header + body)

    def clear_pattern(self):
        self._ser.write(bytes([CMD_PATTERN_CLEAR]))

    def set_recoil_enabled(self, enabled):
        self._ser.write(struct.pack('<BB', CMD_RECOIL_ENABLE, 1 if enabled else 0))

    def move(self, dx, dy):
        """Inject a mouse move (dx, dy in counts)."""
        self._ser.write(struct.pack('<Bhh', CMD_MOVE, int(dx), int(dy)))

    def click(self, buttons=0x01, duration_ms=80):
        """Inject a mouse click. buttons: bit0=left, bit1=right, bit2=middle."""
        self._ser.write(struct.pack('<BBH', CMD_CLICK, buttons, duration_ms))

    def set_aim_mode(self, enabled):
        """Enable/disable aim mode. When enabled, real left clicks are suppressed
        and Pico applies stored delta + click on left press."""
        self._ser.write(struct.pack('<BB', CMD_AIM_MODE, 1 if enabled else 0))

    def set_delta(self, dx, dy):
        """Update aim delta stored in Pico (in counts). Called every frame."""
        self._ser.write(struct.pack('<Bhh', CMD_SET_DELTA, int(dx), int(dy)))

    def move_click(self, dx, dy, buttons=0x01, delay_ms=0, duration_ms=80):
        """Inject move + delayed click. Pico moves first, clicks after delay_ms.
        If delay_ms=0, auto-calculates from move distance."""
        if delay_ms == 0:
            # Estimate move time: distance / MAX_MOVE_PER_MS (127 counts/ms) + margin
            dist = max(abs(int(dx)), abs(int(dy)))
            delay_ms = max(5, dist // 127 + 10)
        self._ser.write(struct.pack('<BhhBHH', CMD_MOVE_CLICK,
                        int(dx), int(dy), buttons, delay_ms, duration_ms))

    def close(self):
        self._ser.close()


def _find_pico_port():
    """Auto-detect Pico CDC port by VID:PID."""
    for p in serial.tools.list_ports.comports():
        if p.vid == 0xCAFE and p.pid in (0x4001, 0x4005):
            return p.device
    raise RuntimeError(
        "Pico mouse not found. Check USB connection or set PICO_PORT in config.py."
    )


def get_mouse(port=None):
    """Return singleton PicoMouse. Auto-detects port if not specified."""
    global _instance
    if _instance is None:
        if port is None:
            from config import PICO_PORT
            port = PICO_PORT or _find_pico_port()
        _instance = PicoMouse(port)
    return _instance
