"""Software mouse backend — moves via win32 SendInput.

Same interface as PicoMouse so they can be swapped via MOUSE_BACKEND.

Recoil compensation runs in a Press thread that is started on left-mouse-down
and stopped on left-mouse-up. Left button is polled internally so this backend
does not depend on KeyPoller.

Limitations vs PicoMouse:
  - SendInput is ignored by games using raw input (PUBG by default). Verify
    that the game is configured to accept SendInput.
  - 5ms left-button poll → ~5ms extra trigger latency.
  - aim_mode / set_delta / click are no-ops (Pico-only features).
"""

import ctypes
import threading
import time

from press.press import Press

# Win32 SendInput types. ULONG_PTR is 8 bytes on 64-bit and forces 8-byte
# alignment of the INPUT union — naive struct.pack drops the 4-byte padding
# after `type` and SendInput silently does nothing.
_LONG = ctypes.c_long
_DWORD = ctypes.c_ulong
_ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ('dx', _LONG),
        ('dy', _LONG),
        ('mouseData', _DWORD),
        ('dwFlags', _DWORD),
        ('time', _DWORD),
        ('dwExtraInfo', _ULONG_PTR),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [('mi', _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [('type', _DWORD), ('u', _INPUT_UNION)]


_INPUT_MOUSE = 0
_MOUSEEVENTF_MOVE = 0x0001
_SEND_INPUT_SIZE = ctypes.sizeof(_INPUT)
_SendInput = ctypes.windll.user32.SendInput

VK_LBUTTON = 0x01
_get_key = ctypes.windll.user32.GetAsyncKeyState

# Match PicoMouse.RECOIL_FIRE_DELAY_MS, and see the long comment there for why
# there is no half-interval term: the game's recoil is spread over the bullet
# interval too, so both centroids move together and cancel. Subtracting it from
# our side alone put every bullet half an interval early.
RECOIL_FIRE_DELAY_MS = 13


def _send_move(dx, dy):
    inp = _INPUT(
        type=_INPUT_MOUSE,
        u=_INPUT_UNION(mi=_MOUSEINPUT(int(dx), int(dy), 0, _MOUSEEVENTF_MOVE, 0, 0)),
    )
    _SendInput(1, ctypes.byref(inp), _SEND_INPUT_SIZE)


class SoftMouse:
    """Software mouse with same API as PicoMouse — except for keys.

    ⚠ **The API is NOT the same, and this is the one place that difference is
    written down.** SendInput has no key path here, so there is no `key()` and
    Tab / 1 / 2 cannot be sent: this backend can drag, but it cannot open the
    screen there is anything to drag on.

    Callers used to discover that by asking `hasattr(mouse, 'key')`, i.e. by
    the method's ABSENCE, which is unreadable from either class. `can_key`
    states it, and it is the same capability `InventoryControl.can_press()` and
    `SpawnerControl.can_press()` answer from further up.
    """

    can_key = False

    def __init__(self):
        self._dx_s = []
        self._dy_s = []
        self._t_s = []
        self._bullet_interval = 0.1
        self._enabled = True
        self._press_thread = None
        self._prev_left = False
        self._running = True
        self._watch = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch.start()
        print("[soft_mouse] active (SendInput backend)", flush=True)

    # ── Pattern management ──

    def upload_pattern(self, dx_s, dy_s, t_s, bullet_interval_s=0.1):
        """Save pattern. Takes effect on next left-button press."""
        offset = RECOIL_FIRE_DELAY_MS / 1000.0
        self._dx_s = list(dx_s)
        self._dy_s = list(dy_s)
        self._t_s = [float(t) + offset for t in t_s]
        self._bullet_interval = bullet_interval_s

    def clear_pattern(self):
        self._dx_s = []
        self._dy_s = []
        self._t_s = []

    def set_recoil_enabled(self, enabled):
        self._enabled = bool(enabled)
        if not enabled:
            self._stop_press()

    # ── Direct mouse actions ──

    def move(self, dx, dy):
        _send_move(dx, dy)

    def click(self, buttons=0x01, duration_ms=80):
        pass  # Pico-only

    def set_aim_mode(self, enabled):
        pass  # Pico-only

    def set_delta(self, dx, dy):
        pass  # Pico-only

    def move_click(self, dx, dy, buttons=0x01, delay_ms=0, duration_ms=80):
        _send_move(dx, dy)

    def close(self):
        self._running = False
        self._stop_press()

    # ── Internal: left-button watcher ──

    def _watch_loop(self):
        while self._running:
            left = bool(_get_key(VK_LBUTTON) & 0x8000)
            if left and not self._prev_left:
                self._start_press()
            elif not left and self._prev_left:
                self._stop_press()
            self._prev_left = left
            time.sleep(0.005)

    def _start_press(self):
        if not self._enabled or not self._t_s:
            return
        self._stop_press()
        self._press_thread = Press(self._dx_s, self._dy_s, self._t_s)
        self._press_thread.start()

    def _stop_press(self):
        t = self._press_thread
        if t is not None:
            t.stop()
            self._press_thread = None
