"""Key-triggered data feedback — captures HUD crops before/after key presses.

Listens for game keys (B, Tab, C, Z, 1, 2), waits for screen update,
then compares poller state before vs after. Saves crops for training data.

Usage:
    poller = HUDPoller()
    poller.start()
    feedback = KeyFeedback(poller)
    feedback.start()
"""
import os
import sys
import time
import threading
from datetime import datetime

import cv2
from pynput import keyboard

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from detector.cropper import win32_cap

FEEDBACK_DIR = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot')

# Delay after key press before capturing (ms)
SETTLE_MS = 200


class KeyFeedback:
    """Captures HUD state + crops around key presses for data collection."""

    def __init__(self, poller):
        self.poller = poller
        self._listener = None
        self._idx = 0
        os.makedirs(FEEDBACK_DIR, exist_ok=True)

    def start(self):
        self._listener = keyboard.Listener(on_press=self._on_press)
        self._listener.start()
        print('[KeyFeedback] Listening for B/Tab/C/Z/1/2')

    def stop(self):
        if self._listener:
            self._listener.stop()

    def _save(self, trigger, region_name, crop, pred_before, pred_after):
        """Save a crop with before/after predictions in filename."""
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        idx = self._idx
        self._idx += 1
        changed = 'changed' if pred_before != pred_after else 'same'
        fname = f'{idx:04d}_{ts}_key={trigger}_{region_name}_before={pred_before}_after={pred_after}_{changed}.png'
        cv2.imwrite(os.path.join(FEEDBACK_DIR, fname), crop)

    def _capture_feedback(self, trigger, regions):
        """Capture before state, wait for settle, capture after state + crops.

        regions: dict of {state_key: crop_rect} to capture.
            crop_rect: (y, x, h, w) for win32_cap, or None to use poller's cached crop.
        """
        # Before state
        state_before = self.poller.get_state()

        # Wait for screen to settle
        time.sleep(SETTLE_MS / 1000.0)

        # After state
        state_after = self.poller.get_state()

        # Save crops for each region
        for state_key, crop_rect in regions.items():
            pred_before = str(state_before.get(state_key, '')) or 'empty'
            pred_after = str(state_after.get(state_key, '')) or 'empty'

            if crop_rect is not None:
                crop = win32_cap(crop_rect)
            else:
                crop = self.poller._crops.get(state_key)

            if crop is not None:
                self._save(trigger, state_key, crop, pred_before, pred_after)

        # Notify check modules to update ground truth
        self.poller.notify_key(trigger.lower())

        # Log
        changes = []
        for state_key in regions:
            b = state_before.get(state_key, '')
            a = state_after.get(state_key, '')
            if b != a:
                changes.append(f'{state_key}: {b!r}->{a!r}')
        if changes:
            print(f'[KeyFeedback] {trigger}: {", ".join(changes)}')
        else:
            print(f'[KeyFeedback] {trigger}: no state change detected')

    def _on_press(self, key):
        # Get rects from poller module
        from detector.hud_poller import RECTS

        trigger = None
        regions = {}

        if hasattr(key, 'char') and key.char:
            ch = key.char.lower()

            if ch == 'b':
                # B = cycle fire mode
                trigger = 'B'
                regions = {
                    'fire_mode': RECTS['fire_mode'],
                    'weapon_1': RECTS['weapon_1'],
                    'weapon_2': RECTS['weapon_2'],
                }

            elif ch == '1':
                # 1 = switch to weapon 1
                trigger = '1'
                regions = {
                    'weapon_1': RECTS['weapon_1'],
                    'weapon_2': RECTS['weapon_2'],
                    'weapon_1_hl': None,  # use cached
                    'weapon_2_hl': None,
                    'fire_mode': RECTS['fire_mode'],
                }

            elif ch == '2':
                # 2 = switch to weapon 2
                trigger = '2'
                regions = {
                    'weapon_1': RECTS['weapon_1'],
                    'weapon_2': RECTS['weapon_2'],
                    'weapon_1_hl': None,
                    'weapon_2_hl': None,
                    'fire_mode': RECTS['fire_mode'],
                }

            elif ch == 'c':
                # C = toggle crouch
                trigger = 'C'
                regions = {
                    'posture': RECTS['posture'],
                }

            elif ch == 'z':
                # Z = toggle prone
                trigger = 'Z'
                regions = {
                    'posture': RECTS['posture'],
                }

        elif key == keyboard.Key.tab:
            trigger = 'Tab'
            regions = {
                'tab_open': RECTS['tab'],
            }

        elif key == keyboard.Key.space:
            # Space = jump, clears posture GT
            trigger = 'Space'
            regions = {
                'posture': RECTS['posture'],
            }

        if trigger:
            # Run in background to not block the key listener
            threading.Thread(
                target=self._capture_feedback,
                args=(trigger, regions),
                daemon=True,
            ).start()


def main():
    from detector.hud_poller import HUDPoller

    poller = HUDPoller()
    poller.on_change(lambda k, o, n: print(f'  [{k}] {o!r} -> {n!r}'))
    poller.start()

    feedback = KeyFeedback(poller)
    feedback.start()

    print('Running. Press game keys to collect data. Ctrl+C to stop.\n')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        feedback.stop()
        poller.stop()


if __name__ == '__main__':
    main()
