"""KeyPoller — keyboard + mouse input with timestamps.

Keyboard: GetAsyncKeyState polling (5ms), no hooks, no GIL issues.
Mouse: pynput listener for left/right click.
All events pushed to a queue for the Dispatcher to consume.
"""
import ctypes
import time
import threading
from collections import namedtuple

from pynput import mouse

from config import POLL_VK_MAP

KeyEvent = namedtuple('KeyEvent', ['key', 'event', 'ts', 'held_keys'])
# key: str key name
# event: 'press' or 'release'
# ts: time.perf_counter()
# held_keys: frozenset of currently held key names (for combo matching)


class KeyPoller:
    """Produces KeyEvent items into a queue."""

    def __init__(self):
        self._queue = []  # simple list, dispatcher pops from it
        self._lock = threading.Lock()
        self._held_keys = set()
        self._prev_state = {}
        self._get_key = ctypes.windll.user32.GetAsyncKeyState
        self._running = False
        self._kb_thread = None
        self._mouse_listener = None
        self.left_held = False  # exposed for aim assist

    # ── Thread lifecycle ──

    def start(self):
        self._running = True
        self._kb_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._kb_thread.start()
        self._mouse_listener = mouse.Listener(on_click=self._on_click)
        self._mouse_listener.start()

    def stop(self):
        self._running = False
        if self._mouse_listener:
            self._mouse_listener.stop()

    def join(self):
        if self._kb_thread:
            self._kb_thread.join()

    # ── Event queue ──

    def pop_events(self):
        """Return and clear all pending events. Called by Dispatcher."""
        with self._lock:
            events = self._queue
            self._queue = []
        return events

    def _emit(self, key, event):
        ts = time.perf_counter()
        held = frozenset(self._held_keys)
        with self._lock:
            self._queue.append(KeyEvent(key, event, ts, held))

    # ── Keyboard polling ──

    def _poll_loop(self):
        while self._running:
            try:
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
            except Exception as e:
                print(f"[kb_poll] {e}", flush=True)
            time.sleep(0.005)

    # ── Mouse listener ──

    def _on_click(self, _x, _y, button, pressed):
        try:
            if button == mouse.Button.left:
                self.left_held = pressed
            elif button == mouse.Button.right:
                if not pressed:
                    self._emit('right', 'release')
        except Exception as e:
            print(f"[mouse] {e}", flush=True)
