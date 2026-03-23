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
