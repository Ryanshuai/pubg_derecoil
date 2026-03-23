import os
import sys
import time
import threading
from collections import defaultdict
import torch
from loguru import logger

# Configure loguru before detector imports
logger.remove()
logger.add(sys.stderr, level="WARNING", format="{time:HH:mm:ss} | {message}")
for _det_name in ['weapon', 'fire_mode', 'attachment']:
    logger.add(
        os.path.join('InGameScreenshot', _det_name, f'{_det_name}.log'),
        filter=lambda record, d=_det_name: record["extra"].get("detector") == d,
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
        rotation="10 MB", encoding="utf-8",
    )

from pynput import keyboard, mouse

from detector.game_state import GameState
from detector.weapon import Weapon
from detector.weapon_dl_detector import WeaponClassifier
from detector.fire_mode_detector import FireModeDetector
from detector.posture_detector import PostureDetector
from detector.tab_scan import TabScan
from config import DETECT_TABLE, KEY_STATE_TABLE, SPECIAL_KEYS


class Robot:
    def __init__(self):
        self.state = GameState()

        # Load all detectors
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self._detectors = {
            'weapon_hud': WeaponClassifier(device, self.state),
            'fire_mode':  FireModeDetector(device, self.state),
            'posture':    PostureDetector(device, self.state),
            'tab_scan':   TabScan(device, self.state),
        }

        # Build dispatch: key_name → list of (detector, delay_sec)
        self._detect_map = defaultdict(list)
        for entry in DETECT_TABLE:
            det = self._detectors[entry['detect']]
            delay = entry['delay'] / 1000.0
            for key in entry['keys']:
                self._detect_map[key].append((det, delay))

        # Build dispatch: key_name → list of (state_field, value)
        self._state_map = defaultdict(list)
        for entry in KEY_STATE_TABLE:
            self._state_map[entry['key']].append((entry['state'], entry['value']))

        mouse.Listener(on_click=self.on_click).start()
        self.key_listener = keyboard.Listener(on_press=self.on_press)
        self.key_listener.start()
        print("init done", flush=True)

    # ── Config-driven dispatch ────────────────────────────────

    def _run_detect(self, detector, delay):
        if delay > 0:
            time.sleep(delay)
        detector.query()

    def _dispatch(self, key_name):
        """Dispatch key event through both config tables."""
        # Immediate state updates
        for state_field, value in self._state_map.get(key_name, []):
            self.state.apply(state_field, value)

        # Deferred detector queries
        for detector, delay in self._detect_map.get(key_name, []):
            threading.Thread(
                target=self._run_detect, args=(detector, delay), daemon=True
            ).start()

    def _dispatch_with_time(self, key_name):
        """Dispatch with click timestamp as value (for start_press)."""
        click_time = time.perf_counter()
        for state_field, _ in self._state_map.get(key_name, []):
            self.state.apply(state_field, click_time)

    # ── Input listeners ───────────────────────────────────────

    def on_click(self, _x, _y, button, pressed):
        if button == mouse.Button.left:
            if pressed:
                self._dispatch_with_time('left_down')
            else:
                self._dispatch('left_up')
        elif button == mouse.Button.right and pressed:
            self._dispatch('right_down')

    def on_press(self, key):
        if key == keyboard.Key.f13:
            self.shutdown()
            return False

        # Special keys
        key_name = SPECIAL_KEYS.get(key)
        if key_name:
            self._dispatch(key_name)
            return

        # Char keys
        if hasattr(key, 'char') and key.char:
            self._dispatch(key.char.lower())

    def shutdown(self):
        self.state.stop_recoil = True
        Weapon.save_scales()
        print("[shutdown] scales saved", flush=True)


if __name__ == '__main__':
    robot = Robot()
    try:
        robot.key_listener.join()
    except KeyboardInterrupt:
        robot.shutdown()
