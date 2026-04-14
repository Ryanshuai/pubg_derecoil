"""Dispatcher — reads config tables, matches key events to frames, runs detectors.

Three responsibilities:
1. Key actions:  KEY_ACTION_TABLE → state changes + hardware
2. Detections:   DETECT_TABLE → find frame → run detector → write result
3. Mismatches:   MISMATCH_TABLE → compare GT vs pred → save crops
"""
import os
import time
import threading
from collections import deque

from config import KEY_ACTION_TABLE, DETECT_TABLE, MISMATCH_TABLE
from detector.weapon import Weapon
from press.pico_mouse import get_mouse
from detector.utils import img_hash
import cv2
import numpy as np


MISMATCH_DIR = os.path.join('InGameScreenshot', 'highlight_mismatch')


class Dispatcher:
    """Main logic loop. Consumes key events, drives detections."""

    def __init__(self, state, capture, poller):
        self.state = state
        self.capture = capture
        self.poller = poller
        self._detectors = {}  # name -> detector instance, set by register()
        self._pending = deque()  # (target_ts, detect_entry)
        self._running = False
        self._thread = None

    def register(self, name, detector):
        """Register a detector instance by name."""
        self._detectors[name] = detector

    # ── Thread lifecycle ──

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def join(self):
        if self._thread:
            self._thread.join()

    # ── Main loop ──

    def _loop(self):
        while self._running:
            try:
                # 1. Process key events
                for ev in self.poller.pop_events():
                    self._handle_key(ev)

                # 2. Process pending detections (target_ts reached)
                self._process_pending()

            except Exception as e:
                print(f"[dispatch] {e}", flush=True)

            time.sleep(0.01)

    # ════════════════════════════════════════════════════════════
    # Key event handling
    # ════════════════════════════════════════════════════════════

    def _handle_key(self, ev):
        """Process a single KeyEvent: actions + schedule detections."""
        # Shutdown
        if ev.key == 'f13' and ev.event == 'press':
            self._shutdown()
            self._running = False
            return

        # DETECT_TABLE: schedule detections using CURRENT state
        for entry in DETECT_TABLE:
            if not self._key_matches(entry, ev):
                continue
            if not self._cond_met(entry.get('cond')):
                continue
            target_ts = ev.ts + entry['delay'] / 1000.0
            self._pending.append((target_ts, entry))

        # MISMATCH_TABLE: schedule mismatch collection (also before state mutation)
        for entry in MISMATCH_TABLE:
            if not self._key_matches(entry, ev):
                continue
            if not self._cond_met(entry.get('cond')):
                continue
            target_ts = ev.ts + entry['delay'] / 1000.0
            self._pending.append((target_ts, {'_mismatch': True, **entry}))

        # KEY_ACTION_TABLE: state changes + hardware (AFTER scheduling detections)
        for entry in KEY_ACTION_TABLE:
            if not self._key_matches(entry, ev):
                continue
            if not self._cond_met(entry.get('cond')):
                continue
            self._apply_state(entry.get('state', []))
            self._apply_hw(entry.get('hw', []))

    def _key_matches(self, entry, ev):
        """Check if a table entry matches this key event."""
        entry_key = entry['key']
        entry_event = entry.get('event', 'press')
        if entry_event != ev.event:
            return False
        # Combo key: ('alt', 'tab')
        if isinstance(entry_key, tuple):
            trigger = entry_key[-1]
            mods = set(entry_key[:-1])
            return ev.key == trigger and mods <= ev.held_keys
        return ev.key == entry_key

    def _cond_met(self, cond):
        """Evaluate condition string against state."""
        if not cond:
            return True
        s = self.state
        for part in cond.split('&&'):
            part = part.strip()
            negate = part.startswith('!')
            attr = part.lstrip('!')
            val = getattr(s, attr, False)
            if negate:
                if val:
                    return False
            else:
                if not val:
                    return False
        return True

    # ════════════════════════════════════════════════════════════
    # State + Hardware
    # ════════════════════════════════════════════════════════════

    def _apply_state(self, state_list):
        """Apply state changes from KEY_ACTION_TABLE entry."""
        s = self.state
        for item in state_list:
            if isinstance(item, tuple) and len(item) == 2:
                attr, val = item
                # Method call: ('set_active_by_key', 1)
                method = getattr(s, attr, None)
                if callable(method):
                    method(val)
                else:
                    setattr(s, attr, val)
            elif isinstance(item, tuple) and len(item) == 1:
                # Toggle: ('toggle_aim',)
                method = getattr(s, item[0], None)
                if callable(method):
                    method()

    def _apply_hw(self, hw_list):
        """Apply hardware actions."""
        for action in hw_list:
            try:
                m = get_mouse()
                if action == 'recoil_off':
                    m.set_recoil_enabled(False)
                elif action == 'recoil_on':
                    m.set_recoil_enabled(True)
                elif action == 'upload_pattern':
                    w = self.state.active
                    if len(w.dy_s) == 0 or self.state.stop_recoil:
                        m.clear_pattern()
                    else:
                        m.upload_pattern(w.dx_s, w.dy_s, w.t_s, w.bullet_interval_s)
                elif action == 'shutdown':
                    self._shutdown()
                    self._running = False
            except Exception as e:
                print(f"[hw] {action}: {e}", flush=True)

    # ════════════════════════════════════════════════════════════
    # Pending detections
    # ════════════════════════════════════════════════════════════

    def _process_pending(self):
        """Check pending detections, run those whose target_ts has arrived."""
        now = time.perf_counter()
        remaining = deque()
        ran = False
        for target_ts, entry in self._pending:
            if now < target_ts:
                # Not ready yet, keep
                if now < target_ts + 1.0:  # drop if >1s stale
                    remaining.append((target_ts, entry))
                continue
            # Ready — find frame and run
            if entry.get('_mismatch'):
                self._run_mismatch(target_ts, entry)
            else:
                self._run_detect(target_ts, entry)
                ran = True
        self._pending = remaining
        if ran:
            self.state.print_status()

    def _run_detect(self, target_ts, entry):
        """Find frame at target_ts, run detector, write result to state."""
        regions = entry['regions']
        crops = self.capture.get_crops(target_ts, regions)
        if crops is None:
            return

        detect_name = entry['detect']
        detector = self._detectors.get(detect_name)
        if detector is None:
            return

        result = detector.classify(crops)
        if result is None:
            return

        # Write result to state — direct attribute assignment
        result_field = entry.get('result')
        if result_field and result is not None:
            setattr(self.state, result_field, result)

            # Side effects after specific result writes
            if result_field in ('weapon_gt', 'weapon_pred'):
                self.state.sync_weapons()
                self._apply_hw(['upload_pattern'])
            elif result_field == 'highlight_pred':
                self.state.set_active_by_detect(result)
                self._apply_hw(['upload_pattern'])
            elif result_field == 'posture':
                self.state.set_posture(result)
                self._apply_hw(['upload_pattern'])
            elif result_field == 'fire_mode':
                self.state.set_fire_mode(result)
                self._apply_hw(['upload_pattern'])
            elif result_field == 'attachments':
                for gun_id, att in result.items():
                    self.state.set_attachments(gun_id, att)
                self._apply_hw(['upload_pattern'])
            elif result_field == '_tab_calibrate':
                # tab_type returns True = Type visible = tab is open
                actual = bool(result)
                if actual != self.state.tab_open:
                    print(f"[tab] calibrate: {self.state.tab_open} → {actual}", flush=True)
                    self.state.tab_open = actual

    def _run_mismatch(self, target_ts, entry):
        """Run highlight detection and save if GT != pred."""
        regions = entry['regions']
        crops = self.capture.get_crops(target_ts, regions)
        if crops is None:
            return

        detector = self._detectors.get(entry['detect'])
        if detector is None:
            return

        pred = detector.classify(crops)
        if pred is None:
            return

        gt_value = entry.get('gt_value', 0)
        if pred != gt_value:
            self._save_mismatch(crops, gt_value, pred)

    def _save_mismatch(self, crops, gt, pred):
        """Save mismatched highlight crops for review."""
        os.makedirs(MISMATCH_DIR, exist_ok=True)
        for region_name, crop in crops.items():
            if crop is None:
                continue
            slot = 1 if '1' in region_name else 2
            gt_hl = 'h' if slot == gt else 'l'
            pred_hl = 'h' if slot == pred else 'l'
            w = self.state.weapon_1 if slot == 1 else self.state.weapon_2
            wname = w.name or 'unknown'
            h = img_hash(crop)
            fname = f'{wname}_gt_{gt_hl}_pred_{pred_hl}_{h}.png'
            path = os.path.join(MISMATCH_DIR, fname)
            if not os.path.exists(path):
                cv2.imwrite(path, crop)

    # ════════════════════════════════════════════════════════════
    # Shutdown
    # ════════════════════════════════════════════════════════════

    def _shutdown(self):
        try:
            m = get_mouse()
            m.set_recoil_enabled(False)
            m.clear_pattern()
            m.set_aim_mode(False)
            m.set_delta(0, 0)
        except Exception:
            pass
        Weapon.save_scales()
        print("[shutdown] scales saved", flush=True)
