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
from human_detection.detect import CrosshairDetector, TargetDetector
from config import DETECT_TABLE, KEY_STATE_TABLE, SPECIAL_KEYS
import config


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

        # Aim assist
        self._xhair = CrosshairDetector()
        self._target = TargetDetector()
        self._last_delta = (0, 0)
        self._aim_thread = threading.Thread(target=self._aim_loop, daemon=True)
        self._aim_thread.start()

        mouse.Listener(on_click=self.on_click).start()
        self.key_listener = keyboard.Listener(on_press=self.on_press)
        self.key_listener.start()
        print("init done", flush=True)

    # ── Aim assist loop ────────────────────────────────────

    @property
    def _aim_active(self):
        """Aim assist active when current weapon is a bolt-action sniper."""
        return self.state.active.type in ('sp', 'dmr')

    def _aim_loop(self):
        """Background loop: always updates cv2 window, detects when sniper is active."""
        import cv2
        from human_detection.detect import win32_cap, SCREEN_W, CX, CY

        cv2.namedWindow('detect', cv2.WINDOW_NORMAL)
        cv2.moveWindow('detect', SCREEN_W + 50, 50)
        cv2.resizeWindow('detect', self._target.crop_w, self._target.crop_h)

        last_aim = False
        while True:
            t0 = time.perf_counter()
            x0 = self._target.crop_x0
            y0 = self._target.crop_y0

            aim = self._aim_active and not self.state.tab_open
            if aim != last_aim:
                last_aim = aim
                print(f"[aim] mode={'ON' if aim else 'OFF'}, "
                      f"type={self.state.active.type}, "
                      f"name={self.state.active.name}", flush=True)
                try:
                    from press.pico_mouse import get_mouse
                    get_mouse().set_aim_mode(aim)
                except Exception as e:
                    print(f"[aim] set_aim_mode failed: {e}", flush=True)

            if aim:
                self._target.query()
                frame = self._target.frame
                self._xhair.query(full_frame=frame, frame_x0=x0, frame_y0=y0)

                # Draw crosshair
                xh_cx = self._xhair.x - x0
                xh_cy = self._xhair.y - y0
                cv2.drawMarker(frame, (xh_cx, xh_cy),
                               (255, 255, 0), cv2.MARKER_CROSS, 30, 2)
                off_x = self._xhair.x - CX
                off_y = self._xhair.y - CY
                cv2.putText(frame, f'xhair/{self._xhair.method} ({off_x:+d},{off_y:+d})',
                            (xh_cx + 20, xh_cy - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)

                # Draw target
                if self._target.target_body:
                    bx1, by1, bx2, by2, bconf = self._target.target_body
                    cv2.rectangle(frame, (bx1, by1), (bx2, by2), (0, 255, 0), 2)
                if self._target.best_head:
                    hx1, hy1, hx2, hy2, hconf = self._target.best_head
                    cv2.rectangle(frame, (hx1, hy1), (hx2, hy2), (0, 0, 255), 2)

                # Compute delta and send to Pico every frame
                dx, dy = 0, 0
                if self._target.x is not None:
                    tx = self._target.x - x0
                    ty = self._target.y - y0
                    cv2.drawMarker(frame, (tx, ty), (0, 255, 255),
                                   cv2.MARKER_CROSS, 20, 2)
                    dx = self._target.x - self._xhair.x
                    dy = self._target.y - self._xhair.y
                    cv2.line(frame, (xh_cx, xh_cy), (tx, ty), (0, 165, 255), 2)
                    cv2.putText(frame, f'delta ({dx:+d},{dy:+d})',
                                (tx + 20, ty + 20),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1)

                # Send delta to Pico (counts, not pixels) only when changed
                cpp = config.COUNTS_PER_PIXEL
                new_delta = (int(dx * cpp), int(dy * cpp))
                if new_delta != self._last_delta:
                    self._last_delta = new_delta
                    try:
                        from press.pico_mouse import get_mouse
                        get_mouse().set_delta(*new_delta)
                    except Exception:
                        pass
            else:
                # Not ADS: just capture and show
                frame = win32_cap(x0, y0, self._target.crop_w, self._target.crop_h)

            dt = time.perf_counter() - t0
            fps = 1.0 / dt if dt > 0 else 0
            cv2.putText(frame, f'{fps:.0f} FPS',
                        (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

            cv2.imshow('detect', frame)
            cv2.waitKey(1)

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
        self.state._set_stop_recoil(True)
        Weapon.save_scales()
        print("[shutdown] scales saved", flush=True)


if __name__ == '__main__':
    robot = Robot()
    try:
        robot.key_listener.join()
    except KeyboardInterrupt:
        robot.shutdown()
