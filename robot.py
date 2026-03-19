import os
import threading
import time
from datetime import datetime
from pynput import keyboard, mouse
import cv2

from press import Press
from weapon import Weapon, can_full_guns
from detector.cropper import win32_cap
from detector.hud_poller import HUDPoller
import config
from config import SCREEN_W, SCREEN_H

SCREENSHOT_DIR = 'in_game_screenshot'


class Robot:
    def __init__(self):
        self.weapon_1 = Weapon()
        self.weapon_2 = Weapon()
        self.weapon = self.weapon_1  # active weapon reference
        self.stop_press = False
        self.running = True

        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

        # ── HUD Poller (round-robin detection) ──
        self.poller = HUDPoller()
        self.poller.on_change(self._on_hud_change)
        self.poller.start()

        # ── Mouse hook ──
        self.mouse_listener = mouse.Listener(on_click=self.on_click)
        self.mouse_listener.start()

        # ── Keyboard hook ──
        self.key_listener = keyboard.Listener(on_press=self.on_press)
        self.key_listener.start()

        print("init done", flush=True)

    # ── HUD state change handler ─────────────────────────────
    def _on_hud_change(self, key, old, new):
        """Called by poller when any HUD state changes."""

        # Weapon name changed
        if key == 'weapon_1':
            self.weapon_1.set('name', new)
            self.weapon_1.set_seq()
            if new:
                print(f'[weapon 1] {new}', flush=True)

        elif key == 'weapon_2':
            self.weapon_2.set('name', new)
            self.weapon_2.set_seq()
            if new:
                print(f'[weapon 2] {new}', flush=True)

        # Highlight changed → switch active weapon
        elif key == 'weapon_1_hl' and new == 'highlighted':
            self.weapon = self.weapon_1
            self.stop_press = False
            print(f'[active] weapon 1 ({self.weapon_1.name})', flush=True)

        elif key == 'weapon_2_hl' and new == 'highlighted':
            self.weapon = self.weapon_2
            self.stop_press = False
            print(f'[active] weapon 2 ({self.weapon_2.name})', flush=True)

        # Fire mode changed
        elif key == 'fire_mode':
            self.weapon_1.set('fire_mode', new)
            self.weapon_2.set('fire_mode', new)
            self.weapon_1.set_seq()
            self.weapon_2.set_seq()
            if new:
                print(f'[fire_mode] {new}', flush=True)

        # Posture changed → update recoil (crouch has different pattern)
        elif key == 'posture' and new:
            self.weapon_1.set('posture', new)
            self.weapon_2.set('posture', new)
            self.weapon_1.set_seq()
            self.weapon_2.set_seq()
            print(f'[posture] {new}', flush=True)

        # Tab state
        elif key == 'tab_open':
            if new:
                self.stop_press = True
                print('[tab] opened', flush=True)
            else:
                self.stop_press = False
                print('[tab] closed', flush=True)

        # Attachments changed
        elif key in ('attachments_1', 'attachments_2'):
            gun_id = key[-1]
            weapon = self.weapon_1 if gun_id == '1' else self.weapon_2
            if isinstance(new, dict):
                for slot, val in new.items():
                    if slot == 'scope':
                        weapon.set('scope', val)
                    elif slot == 'muzzle':
                        weapon.set('muzzle', val)
                    elif slot == 'grip':
                        weapon.set('grip', val)
                    elif slot == 'stock':
                        weapon.set('butt', val)
                weapon.set_seq()
                attached = [f'{s}={v}' for s, v in new.items() if v]
                if attached:
                    print(f'[attach {gun_id}] {", ".join(attached)}', flush=True)

    # ── Keyframe screenshot (F5) ─────────────────────────────
    def _save_keyframe(self):
        screenshot = win32_cap((0, 0, SCREEN_H, SCREEN_W))
        ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
        filename = f'{ts}.png'
        path = os.path.join(SCREENSHOT_DIR, filename)
        cv2.imwrite(path, screenshot)
        print(f"[screenshot] {filename}", flush=True)

    # ── Mouse hook ────────────────────────────────────────────
    def on_click(self, x, y, button, pressed):
        if button == mouse.Button.right and pressed:
            self.stop_press = False

        if button == mouse.Button.left and pressed:
            if not self.stop_press and len(self.weapon.dy_s) > 0:
                # Only compensate in full/high auto mode
                if self.weapon.fire_mode in ('full', 'high'):
                    self.press = Press(self.weapon.dx_s, self.weapon.dy_s, self.weapon.t_s)
                    self.press.start()

        if button == mouse.Button.left and not pressed:
            if hasattr(self, 'press'):
                self.press.stop()

    # ── Keyboard hook ─────────────────────────────────────────
    def _reload_seq(self):
        self.weapon_1.bullet_calculator.counts_per_unit = config.COUNTS_PER_RECOIL_UNIT
        self.weapon_2.bullet_calculator.counts_per_unit = config.COUNTS_PER_RECOIL_UNIT
        self.weapon_1.set_seq()
        self.weapon_2.set_seq()

    def on_press(self, key):
        if key == keyboard.Key.f13:
            self.shutdown()
            return False  # stops pynput listener → unblocks join()

        if key == keyboard.Key.f5:
            threading.Thread(target=self._save_keyframe, daemon=True).start()

        if key == keyboard.Key.up:
            config.COUNTS_PER_RECOIL_UNIT += 0.05
            self._reload_seq()
            print(f"[scale] {config.COUNTS_PER_RECOIL_UNIT:.2f}", flush=True)

        if key == keyboard.Key.down:
            config.COUNTS_PER_RECOIL_UNIT = max(0.05, config.COUNTS_PER_RECOIL_UNIT - 0.05)
            self._reload_seq()
            print(f"[scale] {config.COUNTS_PER_RECOIL_UNIT:.2f}", flush=True)

        if hasattr(key, 'char') and key.char:
            ch = key.char
            if ch == 'g' or ch == '5':
                self.stop_press = True

    def shutdown(self):
        print('[robot] shutting down...', flush=True)
        self.stop_press = True
        self.running = False
        self.poller.stop()
        self.mouse_listener.stop()


if __name__ == '__main__':
    robot = Robot()
    try:
        robot.key_listener.join()
    except KeyboardInterrupt:
        robot.shutdown()
