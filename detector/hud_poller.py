"""HUD state poller — round-robin detection across all HUD elements.

5 detectors staggered at 40ms intervals = 5Hz total, each detector ~1Hz.
GPU load spread evenly, no spikes.

Delegates detection to individual detector modules.
Usage:
    poller = HUDPoller()
    poller.start()
    state = poller.get_state()
    poller.stop()
"""
import os
import sys
import time
import threading
import torch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# ── Import detector modules ──
from detector import (
    weapon_detector, fire_mode_detector, posture_detector,
    tab_detector, attachment_detector,
)
from detector.cropper import win32_cap
from config import ATTACHMENT_SLOTS

# ── Screen rects (for crop caching) ──
RECTS = {
    'weapon_1': weapon_detector.SLOT_RECTS[1],
    'weapon_2': weapon_detector.SLOT_RECTS[2],
    'fire_mode': fire_mode_detector.SLOT_RECT,
    'posture': posture_detector.SLOT_RECT,
    'tab': tab_detector.SLOT_RECT,
}

SLOT_NAMES = ['scope', 'muzzle', 'grip', 'magazine', 'stock']



class HUDPoller:
    """Round-robin HUD state poller. Delegates to individual detector modules."""

    INTERVAL = 0.04  # 40ms between detectors

    def __init__(self):
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        # Load models via each detector's load_model()
        self.weapon_model = weapon_detector.load_model(self.device)
        self.fire_mode_model = fire_mode_detector.load_model(self.device)
        self.posture_model = posture_detector.load_model(self.device)
        self.tab_model = tab_detector.load_model(self.device)
        self.attach_model = attachment_detector.load_model(self.device)

        # Current state
        self.state = {
            'weapon_1': '', 'weapon_1_hl': '',
            'weapon_2': '', 'weapon_2_hl': '',
            'fire_mode': '',
            'posture': '',
            'tab_open': False,
            'attachments_1': {s: '' for s in SLOT_NAMES},
            'attachments_2': {s: '' for s in SLOT_NAMES},
        }
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._callbacks = []


    def on_change(self, callback):
        """Register callback(key, old_val, new_val) for state changes."""
        self._callbacks.append(callback)

    def get_state(self):
        with self._lock:
            return dict(self.state)

    def _update(self, key, value):
        with self._lock:
            old = self.state[key]
            if old != value:
                self.state[key] = value
                for cb in self._callbacks:
                    try:
                        cb(key, old, value)
                    except Exception:
                        pass

    # ── Detectors (delegate to modules) ──

    def _detect_weapon(self):
        for slot_id in [1, 2]:
            crop = win32_cap(weapon_detector.SLOT_RECTS[slot_id])

            prev_name = self.state[f'weapon_{slot_id}']
            prev_hl = self.state[f'weapon_{slot_id}_hl']
            gun_name, hl_name = weapon_detector.classify_slot(
                self.weapon_model, crop, self.device, slot_id,
                tab_open=self.state['tab_open'])
            self._update(f'weapon_{slot_id}', gun_name)
            self._update(f'weapon_{slot_id}_hl', hl_name)

            # NOTE: weapon name change does NOT invalidate GT.
            # GT exists to correct model errors — if model flickers, GT stays.

    def _detect_fire_mode(self):
        crop = win32_cap(fire_mode_detector.SLOT_RECT)

        name = fire_mode_detector.classify(self.fire_mode_model, crop, self.device)
        self._update('fire_mode', name)

    def _detect_posture(self):
        crop = win32_cap(posture_detector.SLOT_RECT)

        name = posture_detector.classify(self.posture_model, crop, self.device)
        self._update('posture', name)

    def _detect_tab(self):
        crop = win32_cap(tab_detector.SLOT_RECT)

        was_open = self.state['tab_open']
        is_open = tab_detector.classify(self.tab_model, crop, self.device)
        self._update('tab_open', is_open)

        # Tab just opened → invalidate old GT (weapons may change in Tab)
        if not was_open and is_open:
            weapon_detector.invalidate_gt('tab opened')

        # Tab just closed → lock cached OCR as ground truth
        if was_open and not is_open:
            weapon_detector.lock_ocr_gt()

    def _detect_weapon_name(self):
        """Template match weapon names while Tab is open."""
        if self.state['tab_open']:
            weapon_detector.update_ocr_cache()

    def _detect_attachments(self):
        if not self.state['tab_open']:
            return
        for gun_id in [1, 2]:
            result = attachment_detector.detect_all_slots(
                self.attach_model, gun_id, self.device)
            self._update(f'attachments_{gun_id}', result)

    # ── Round-robin loop ──

    # (name, interval_ms, phase_ms) — each detector runs at its own frequency
    DETECTORS = [
        ('weapon',        200,   0),
        ('fire_mode',     200,  40),
        ('posture',       200,  80),
        ('tab',            20, 120),
        ('weapon_name',    10,  10),
        ('attachment',    200, 160),
    ]

    def _loop(self):
        dispatch = {
            'weapon': self._detect_weapon,
            'fire_mode': self._detect_fire_mode,
            'posture': self._detect_posture,
            'tab': self._detect_tab,
            'weapon_name': self._detect_weapon_name,
            'attachment': self._detect_attachments,
        }
        # Initialize next-run times with staggered phase
        now = time.monotonic()
        schedule = {}
        for name, interval_ms, phase_ms in self.DETECTORS:
            phase = phase_ms / 1000.0
            schedule[name] = {
                'interval': interval_ms / 1000.0,
                'next': now + phase,
                'fn': dispatch[name],
            }

        while self._running:
            now = time.monotonic()
            for name, s in schedule.items():
                if now >= s['next']:
                    try:
                        s['fn']()
                    except Exception as e:
                        print(f'[HUDPoller] {name} error: {e}')
                    s['next'] = now + s['interval']
            time.sleep(0.01)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print('[HUDPoller] Started (5 detectors, 40ms stagger)')

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        print('[HUDPoller] Stopped')


def main():
    poller = HUDPoller()

    def on_change(key, old, new):
        print(f'  [{key}] {old!r} -> {new!r}')

    poller.on_change(on_change)
    poller.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        poller.stop()


if __name__ == '__main__':
    main()
