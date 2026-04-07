import os
import sys
import time
import threading
import queue
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
from human_detection.detect import CrosshairDetector, TargetDetector, win32_cap, SCREEN_W, CX, CY
from press.pico_mouse import get_mouse
from config import DETECT_TABLE, KEY_STATE_TABLE, SPECIAL_KEYS
import config
import cv2


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

        # Build dispatch maps. Keys: 'tab' (single) or ('alt','tab') (combo)
        def _parse_key(k):
            combo = (k,) if isinstance(k, str) else tuple(k)
            return combo[-1], frozenset(combo[:-1])

        self._detect_map = defaultdict(list)
        for entry in DETECT_TABLE:
            det, delay = self._detectors[entry['detect']], entry['delay'] / 1000.0
            for key in entry['keys']:
                last, mods = _parse_key(key)
                self._detect_map[last].append((mods, det, delay))

        self._state_map = defaultdict(list)
        for entry in KEY_STATE_TABLE:
            last, mods = _parse_key(entry['key'])
            self._state_map[last].append((mods, entry['state'], entry['value']))

        # Aim assist
        self._xhair = CrosshairDetector()
        self._target = TargetDetector()
        self._last_delta = (0, 0)
        self._aim_thread = threading.Thread(target=self._aim_loop, daemon=True)
        self._aim_thread.start()

        # Serial queue: all state.apply() calls run sequentially off the hook thread
        self._cmd_q = queue.Queue()
        threading.Thread(target=self._cmd_worker, daemon=True).start()

        self._left_held = False
        self._held_keys = set()  # currently held key names for combo detection
        mouse.Listener(on_click=self.on_click).start()
        self.key_listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)
        self.key_listener.start()
        print("init done", flush=True)

    # ── Command worker (serializes state updates off hook thread) ──

    def _cmd_worker(self):
        while True:
            fn, args = self._cmd_q.get()
            try:
                fn(*args)
            except Exception as e:
                print(f"[cmd] {e}", flush=True)

    # ── Aim assist loop ────────────────────────────────────

    RECOIL_TYPES = set()
    AIM_MAX_SPEED = 80       # max counts per frame for rifle aim nudge

    @property
    def _aim_mode(self):
        """Return aim mode: 'sp' for sniper, 'recoil' for rifles, None otherwise."""
        if not self.state.aim_enabled or self.state.tab_open:
            return None
        wtype = self.state.active.type
        if wtype == 'sp':
            return 'sp'
        if wtype in self.RECOIL_TYPES:
            return 'recoil'
        return None

    def _aim_loop(self):
        """Background loop: updates cv2 window and runs detection when aim is enabled."""
        win_visible = False
        last_mode = None
        while True:
            mode = self._aim_mode

            # Show/hide window on aim_enabled toggle
            if mode and not win_visible:
                cv2.namedWindow('detect', cv2.WINDOW_NORMAL)
                cv2.moveWindow('detect', SCREEN_W + 50, 50)
                cv2.resizeWindow('detect', self._target.crop_w, self._target.crop_h)
                win_visible = True
            elif not self.state.aim_enabled and win_visible:
                cv2.destroyWindow('detect')
                cv2.waitKey(1)
                win_visible = False

            if not self.state.aim_enabled:
                if mode != last_mode:
                    last_mode = mode
                time.sleep(0.1)
                continue

            t0 = time.perf_counter()
            x0 = self._target.crop_x0
            y0 = self._target.crop_y0

            if mode != last_mode:
                # Toggle Pico aim_mode only on sp enter/exit
                if mode == 'sp' or last_mode == 'sp':
                    try:
                        get_mouse().set_aim_mode(mode == 'sp')
                    except Exception as e:
                        print(f"[aim] set_aim_mode failed: {e}", flush=True)
                last_mode = mode
                self._last_delta = (0, 0)
                print(f"[aim] mode={mode or 'OFF'}, "
                      f"type={self.state.active.type}, "
                      f"name={self.state.active.name}", flush=True)

            if mode:
                self._detectors['posture'].collect_tick()
                try:
                    self._target.query()
                except Exception as e:
                    print(f"[aim] detect failed: {e}", flush=True)
                    time.sleep(1)
                    continue
                frame = self._target.frame

                # Crosshair: sniper detects dynamically, rifle uses screen center
                if mode == 'sp':
                    self._xhair.query(full_frame=frame, frame_x0=x0, frame_y0=y0)
                    xh_x, xh_y = self._xhair.x, self._xhair.y
                else:
                    xh_x, xh_y = CX, CY

                xh_cx = xh_x - x0
                xh_cy = xh_y - y0
                cv2.drawMarker(frame, (xh_cx, xh_cy),
                               (255, 255, 0), cv2.MARKER_CROSS, 30, 2)

                # Draw target
                if self._target.target_body:
                    bx1, by1, bx2, by2, bconf = self._target.target_body
                    cv2.rectangle(frame, (bx1, by1), (bx2, by2), (0, 255, 0), 2)
                    cv2.putText(frame, f'{bconf:.2f}', (bx1, by1 - 6),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                if self._target.best_head:
                    hx1, hy1, hx2, hy2, hconf = self._target.best_head
                    cv2.rectangle(frame, (hx1, hy1), (hx2, hy2), (0, 0, 255), 2)

                # Determine if target is valid for this mode
                dx, dy = 0, 0
                target_valid = False
                if self._target.x is not None:
                    if mode == 'sp':
                        target_valid = True
                    elif mode == 'recoil' and self._target.target_body:
                        bconf = self._target.target_body[4]
                        target_valid = bconf >= config.CONF_BODY_RECOIL

                if target_valid:
                    tx = self._target.x - x0
                    ty = self._target.y - y0
                    cv2.drawMarker(frame, (tx, ty), (0, 255, 255),
                                   cv2.MARKER_CROSS, 20, 2)
                    dx = self._target.x - xh_x
                    dy = self._target.y - xh_y
                    cv2.line(frame, (xh_cx, xh_cy), (tx, ty), (0, 165, 255), 2)
                    cv2.putText(frame, f'delta ({dx:+d},{dy:+d})',
                                (tx + 20, ty + 20),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1)

                # Send delta to Pico
                cpp = config.COUNTS_PER_PIXEL * self.state.active.scope_factor
                if mode == 'sp':
                    # Sniper: continuous tracking
                    new_delta = (int(dx * cpp), int(dy * cpp))
                    if new_delta != self._last_delta:
                        self._last_delta = new_delta
                        try:
                            get_mouse().set_delta(*new_delta)
                        except Exception:
                            pass
                elif mode == 'recoil' and target_valid and self._left_held:
                    # Rifle: move toward target, capped by max speed (only while firing)
                    mdx, mdy = int(dx * cpp), int(dy * cpp)
                    dist = max(abs(mdx), abs(mdy), 1)
                    if dist > self.AIM_MAX_SPEED:
                        scale = self.AIM_MAX_SPEED / dist
                        mdx = int(mdx * scale)
                        mdy = int(mdy * scale)
                    try:
                        get_mouse().move(mdx, mdy)
                    except Exception:
                        pass
            else:
                time.sleep(0.05)
                continue

            dt = time.perf_counter() - t0
            fps = 1.0 / dt if dt > 0 else 0
            label = f'{fps:.0f} FPS [{mode}] {self.state.active.type}/{self.state.active.name}'
            cv2.putText(frame, label,
                        (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

            cv2.imshow('detect', frame)
            cv2.waitKey(1)

    # ── Config-driven dispatch ────────────────────────────────

    def _run_detect(self, detector, delay, key_name=None):
        if delay > 0:
            time.sleep(delay)
        detector.query(key_name=key_name)

    def _match(self, entries):
        """Filter entries by held modifiers. Longest combo wins over single keys."""
        ok = [e for e in entries if e[0] <= self._held_keys]
        if not ok:
            return []
        best = max(len(e[0]) for e in ok)
        return [e for e in ok if len(e[0]) == best] if best > 0 else ok

    def _dispatch(self, key_name):
        # If any combo matches across both maps, suppress single-key entries everywhere
        all_entries = self._state_map.get(key_name, []) + self._detect_map.get(key_name, [])
        combo_hit = any(e[0] and e[0] <= self._held_keys for e in all_entries)

        for mods, field, value in self._match(self._state_map.get(key_name, [])):
            if combo_hit and not mods:
                continue
            self._cmd_q.put((self.state.apply, (field, value)))
        for mods, det, delay in self._match(self._detect_map.get(key_name, [])):
            if combo_hit and not mods:
                continue
            threading.Thread(target=self._run_detect, args=(det, delay, key_name), daemon=True).start()

    def _dispatch_with_time(self, key_name):
        click_time = time.perf_counter()
        for _, field, _ in self._match(self._state_map.get(key_name, [])):
            self._cmd_q.put((self.state.apply, (field, click_time)))

    # ── Input listeners ───────────────────────────────────────

    def _key_name(self, key):
        return SPECIAL_KEYS.get(key) or (key.char.lower() if hasattr(key, 'char') and key.char else None)

    def on_click(self, _x, _y, button, pressed):
        if button == mouse.Button.left:
            self._left_held = pressed
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
        key_name = self._key_name(key)
        if key_name:
            self._held_keys.add(key_name)
            self._dispatch(key_name)

    def on_release(self, key):
        key_name = self._key_name(key)
        if key_name:
            self._held_keys.discard(key_name)

    def shutdown(self):
        self.state._set_stop_recoil(True)
        try:
            m = get_mouse()
            m.clear_pattern()
            m.set_aim_mode(False)
            m.set_delta(0, 0)
        except Exception:
            pass
        Weapon.save_scales()
        print("[shutdown] scales saved", flush=True)


if __name__ == '__main__':
    robot = Robot()
    try:
        robot.key_listener.join()
    except KeyboardInterrupt:
        robot.shutdown()
