"""HUD state poller — round-robin detection across all HUD elements.

5 detectors staggered at 40ms intervals = 5Hz total, each detector ~1Hz.
GPU load spread evenly, no spikes.

Delegates all detection logic to individual detector modules.
Poller only handles: scheduling, state management, constraint feedback.

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
from datetime import datetime

import cv2
import torch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# ── Import detector modules ──
from detector import (
    weapon_detector, fire_mode_detector, posture_detector,
    tab_detector, attachment_detector, weapon_name_detector,
)
from detector.cropper import win32_cap
from config import ATTACHMENT_SLOTS
from weapon import (
    single_guns, full_guns, single_burst_guns,
    single_full_guns, single_burst_full_guns,
)

# ── Fire mode constraints per weapon ──
WEAPON_FIRE_MODES = {}
for g in single_guns:
    WEAPON_FIRE_MODES[g] = {'single', 'single_bot'}
for g in full_guns:
    WEAPON_FIRE_MODES[g] = {'full'}
for g in single_burst_guns:
    WEAPON_FIRE_MODES[g] = {'single', 'single_bot', 'burst2', 'burst3'}
for g in single_full_guns:
    WEAPON_FIRE_MODES[g] = {'single', 'single_bot', 'full'}
for g in single_burst_full_guns:
    WEAPON_FIRE_MODES[g] = {'single', 'single_bot', 'burst2', 'burst3', 'full'}
WEAPON_FIRE_MODES['mg3'] = {'full', 'high'}
WEAPON_FIRE_MODES['mp9'] = {'single', 'single_bot', 'burst2', 'full'}
WEAPON_FIRE_MODES['p90'] = {'full'}

# ── Screen rects (for crop caching) ──
RECTS = {
    'weapon_1': weapon_detector.SLOT_RECTS[1],
    'weapon_2': weapon_detector.SLOT_RECTS[2],
    'fire_mode': fire_mode_detector.SLOT_RECT,
    'posture': posture_detector.SLOT_RECT,
    'tab': tab_detector.SLOT_RECT,
}

SLOT_NAMES = ['scope', 'muzzle', 'grip', 'magazine', 'stock']

FEEDBACK_DIR = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot')


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
        self.ocr_detector = weapon_name_detector.WeaponNameDetector()

        # Feedback
        os.makedirs(FEEDBACK_DIR, exist_ok=True)
        self._feedback_idx = 0

        # Last crops for constraint feedback
        self._crops = {}

        # Tab ground truth: locked when tab closes
        self._tab_gt = {'weapon_1': '', 'weapon_2': ''}

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

    # ── Constraint feedback (log only, no correction) ──

    def _save_feedback(self, violation, crops_to_save, predictions):
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        idx = self._feedback_idx
        self._feedback_idx += 1
        for crop_name, crop in crops_to_save.items():
            if crop is not None:
                pred = predictions.get(crop_name, 'unk')
                fname = f'{idx:04d}_{ts}_{violation}_{crop_name}={pred}.png'
                cv2.imwrite(os.path.join(FEEDBACK_DIR, fname), crop)
        pred_str = ', '.join(f'{k}={v}' for k, v in predictions.items())
        print(f'[Feedback] {violation} | {pred_str}')

    def _check_constraints(self):
        s = self.state

        # 1. Highlight mutex
        if s['weapon_1_hl'] == 'highlighted' and s['weapon_2_hl'] == 'highlighted':
            self._save_feedback('both_highlighted', {
                'weapon_1': self._crops.get('weapon_1'),
                'weapon_2': self._crops.get('weapon_2'),
            }, {
                'weapon_1': f'{s["weapon_1"]}_{s["weapon_1_hl"]}',
                'weapon_2': f'{s["weapon_2"]}_{s["weapon_2_hl"]}',
            })

        # 2. No weapon = no fire mode
        if not s['weapon_1'] and not s['weapon_2'] and s['fire_mode']:
            self._save_feedback('fire_mode_without_weapon', {
                'weapon_1': self._crops.get('weapon_1'),
                'weapon_2': self._crops.get('weapon_2'),
                'fire_mode': self._crops.get('fire_mode'),
            }, {
                'weapon_1': s['weapon_1'] or 'empty',
                'weapon_2': s['weapon_2'] or 'empty',
                'fire_mode': s['fire_mode'],
            })

        # 3. Fire mode vs active weapon
        active_weapon = ''
        if s['weapon_1_hl'] == 'highlighted':
            active_weapon = s['weapon_1']
        elif s['weapon_2_hl'] == 'highlighted':
            active_weapon = s['weapon_2']

        if active_weapon and s['fire_mode']:
            valid_modes = WEAPON_FIRE_MODES.get(active_weapon)
            if valid_modes and s['fire_mode'] not in valid_modes:
                self._save_feedback('invalid_fire_mode', {
                    'weapon_1': self._crops.get('weapon_1'),
                    'weapon_2': self._crops.get('weapon_2'),
                    'fire_mode': self._crops.get('fire_mode'),
                }, {
                    'weapon_1': f'{s["weapon_1"]}_{s["weapon_1_hl"]}',
                    'weapon_2': f'{s["weapon_2"]}_{s["weapon_2_hl"]}',
                    'fire_mode': s['fire_mode'],
                    'active_gun': active_weapon,
                })

        # 4. Tab open → other detections unreliable
        if s['tab_open'] and (s['fire_mode'] or s['posture']):
            self._save_feedback('detect_during_tab', {
                'tab': self._crops.get('tab'),
                'fire_mode': self._crops.get('fire_mode'),
                'posture': self._crops.get('posture'),
            }, {
                'tab_open': str(s['tab_open']),
                'fire_mode': s['fire_mode'] or 'empty',
                'posture': s['posture'] or 'empty',
            })

        # 5. Weapon detection vs Tab OCR ground truth
        if not s['tab_open']:
            for slot_id in ['weapon_1', 'weapon_2']:
                gt = self._tab_gt.get(slot_id, '')
                det = s[slot_id]
                if gt and det and gt != det:
                    self._save_feedback('weapon_mismatch_vs_tab', {
                        slot_id: self._crops.get(slot_id),
                    }, {
                        slot_id: det,
                        'tab_gt': gt,
                    })

    # ── Detectors (delegate to modules) ──

    def _detect_weapon(self):
        for slot_id in [1, 2]:
            crop = win32_cap(weapon_detector.SLOT_RECTS[slot_id])
            self._crops[f'weapon_{slot_id}'] = crop
            gun_name, hl_name = weapon_detector.classify_slot(
                self.weapon_model, crop, self.device)
            self._update(f'weapon_{slot_id}', gun_name)
            self._update(f'weapon_{slot_id}_hl', hl_name)

    def _detect_fire_mode(self):
        crop = win32_cap(fire_mode_detector.SLOT_RECT)
        self._crops['fire_mode'] = crop
        name = fire_mode_detector.classify(self.fire_mode_model, crop, self.device)
        self._update('fire_mode', name)

    def _detect_posture(self):
        crop = win32_cap(posture_detector.SLOT_RECT)
        self._crops['posture'] = crop
        name = posture_detector.classify(self.posture_model, crop, self.device)
        self._update('posture', name)

    def _detect_tab(self):
        crop = win32_cap(tab_detector.SLOT_RECT)
        self._crops['tab'] = crop
        was_open = self.state['tab_open']
        is_open = tab_detector.classify(self.tab_model, crop, self.device)
        self._update('tab_open', is_open)

        # Tab just closed → lock OCR result as ground truth
        if was_open and not is_open:
            ocr = self.ocr_detector.detect_from_screen()
            for slot_id in [1, 2]:
                name, conf = ocr[slot_id]
                if name and conf > 0.6:
                    self._tab_gt[f'weapon_{slot_id}'] = name
            print(f'[TabGT] weapon_1={self._tab_gt["weapon_1"]!r}, '
                  f'weapon_2={self._tab_gt["weapon_2"]!r}')

    def _detect_attachments(self):
        if not self.state['tab_open']:
            return
        for gun_id in [1, 2]:
            result = attachment_detector.detect_all_slots(
                self.attach_model, gun_id, self.device)
            self._update(f'attachments_{gun_id}', result)

    # ── Round-robin loop ──

    DETECTORS = ['weapon', 'fire_mode', 'posture', 'tab', 'attachment']

    def _loop(self):
        idx = 0
        dispatch = {
            'weapon': self._detect_weapon,
            'fire_mode': self._detect_fire_mode,
            'posture': self._detect_posture,
            'tab': self._detect_tab,
            'attachment': self._detect_attachments,
        }
        n = len(self.DETECTORS)
        while self._running:
            name = self.DETECTORS[idx % n]
            try:
                dispatch[name]()
            except Exception as e:
                print(f'[HUDPoller] {name} error: {e}')
            idx += 1
            if idx % n == 0:
                try:
                    self._check_constraints()
                except Exception as e:
                    print(f'[HUDPoller] constraint check error: {e}')
            time.sleep(self.INTERVAL)

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
