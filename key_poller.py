"""KeyPoller — keyboard + mouse input with timestamps.

All input via GetAsyncKeyState polling (5ms). No hooks, no GIL issues.
"""
import ctypes
import time
import threading
from collections import namedtuple

from config import POLL_VK_MAP

KeyEvent = namedtuple('KeyEvent', ['key', 'event', 'ts', 'held_keys'])

# VK codes for mouse buttons
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02


class KeyPoller:
    """Produces KeyEvent items into a queue. Pure polling, no hooks."""

    def __init__(self):
        self._queue = []
        self._lock = threading.Lock()
        self._held_keys = set()
        self._prev_state = {}
        self._get_key = ctypes.windll.user32.GetAsyncKeyState
        self._running = False
        self._thread = None
        self.left_held = False  # exposed for aim assist

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def join(self):
        if self._thread:
            self._thread.join()

    def pop_events(self):
        with self._lock:
            events = self._queue
            self._queue = []
        return events

    def _emit(self, key, event):
        ts = time.perf_counter()
        held = frozenset(self._held_keys)
        with self._lock:
            self._queue.append(KeyEvent(key, event, ts, held))

    def _poll_loop(self):
        while self._running:
            try:
                # Keyboard
                for vk, key_name in POLL_VK_MAP.items():
                    pressed = bool(self._get_key(vk) & 0x8000)
                    was = self._prev_state.get(vk, False)
                    if pressed and not was:
                        self._held_keys.add(key_name)
                        self._emit(key_name, 'press')
                    elif not pressed and was:
                        self._held_keys.discard(key_name)
                        self._emit(key_name, 'release')
                    self._prev_state[vk] = pressed

                # Mouse left
                left = bool(self._get_key(VK_LBUTTON) & 0x8000)
                self.left_held = left

                # Mouse right
                right = bool(self._get_key(VK_RBUTTON) & 0x8000)
                was_right = self._prev_state.get(VK_RBUTTON, False)
                if right and not was_right:
                    self._emit('right', 'press')
                elif not right and was_right:
                    self._emit('right', 'release')
                self._prev_state[VK_RBUTTON] = right

            except Exception as e:
                print(f"[poll] {e}", flush=True)
            time.sleep(0.005)
