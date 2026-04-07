"""Software mouse backend — moves via win32 SendInput.

Same interface as PicoMouse so they can be swapped via config.
Pico-only features (aim_mode, set_delta, patterns) are no-ops.
"""

import ctypes
import struct

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001


def _send_move(dx, dy):
    extra = ctypes.c_ulong(0)
    ii = ctypes.c_ulong(INPUT_MOUSE)
    # MOUSEINPUT: dx, dy, mouseData, dwFlags, time, dwExtraInfo
    mouse_input = struct.pack('iiIIIQ', int(dx), int(dy), 0, MOUSEEVENTF_MOVE, 0, 0)
    x = ctypes.create_string_buffer(mouse_input)
    # INPUT structure: type (DWORD) + union (MOUSEINPUT)
    inp = struct.pack('I', INPUT_MOUSE) + mouse_input
    buf = ctypes.create_string_buffer(inp)
    ctypes.windll.user32.SendInput(1, buf, ctypes.sizeof(buf))


class SoftMouse:
    """Software mouse with same API as PicoMouse."""

    def upload_pattern(self, dx_s, dy_s, t_s):
        pass  # handled by Press thread

    def clear_pattern(self):
        pass

    def set_recoil_enabled(self, enabled):
        pass

    def move(self, dx, dy):
        _send_move(dx, dy)

    def click(self, buttons=0x01, duration_ms=80):
        pass

    def set_aim_mode(self, enabled):
        pass  # no hardware aim mode

    def set_delta(self, dx, dy):
        pass  # no hardware delta storage

    def move_click(self, dx, dy, buttons=0x01, delay_ms=0, duration_ms=80):
        _send_move(dx, dy)

    def close(self):
        pass
