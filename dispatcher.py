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

from config import (
    KEY_ACTION_TABLE, DETECT_TABLE, MISMATCH_TABLE,
    MISMATCH_POLL_INTERVAL, GT_SETTLE_TIME,
)
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
        self._last_mismatch_poll = 0.0

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

                # 3. Periodic mismatch collection — DISABLED for debugging
                # self._poll_mismatch()

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

        # MISMATCH_TABLE: schedule mismatch collection (snapshot GT now)
        for entry in MISMATCH_TABLE:
            if not self._key_matches(entry, ev):
                continue
            if not self._cond_met(entry.get('cond')):
                continue
            # Use explicit gt_value if provided, otherwise snapshot from state
            if 'gt_value' in entry:
                gt_snapshot = entry['gt_value']
            else:
                gt_field = entry.get('gt_field', '')
                gt_snapshot = getattr(self.state, gt_field, None)
            target_ts = ev.ts + entry['delay'] / 1000.0
            self._pending.append((target_ts, {
                '_mismatch': True, '_gt_snapshot': gt_snapshot, **entry
            }))

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
        # Re-check condition only for future frames (delay > 0)
        # Past frames (delay < 0) were captured before state changed, don't re-check
        if entry.get('delay', 0) > 0 and not self._cond_met(entry.get('cond')):
            return
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
                    self._check_attachment_mismatch(gun_id, att, crops)
                    self.state.set_attachments(gun_id, att)
                self._apply_hw(['upload_pattern'])
            elif result_field == '_tab_calibrate':
                # tab_type returns True = Type visible = tab is open
                actual = bool(result)
                if actual != self.state.tab_open:
                    self.state.tab_open = actual

    def _poll_mismatch(self):
        """Periodic mismatch collection: while GT is valid, compare pred every 500ms."""
        now = time.perf_counter()
        if now - self._last_mismatch_poll < MISMATCH_POLL_INTERVAL / 1000.0:
            return
        if self.state.tab_open or self.state.stop_recoil:
            return
        if now - self.state.highlight_gt_ts < GT_SETTLE_TIME / 1000.0:
            return
        self._last_mismatch_poll = now

        ts, frame = self.capture.latest()
        if frame is None:
            return

        # Highlight mismatch
        gt_hl = self.state.highlight_gt
        if gt_hl and self._detectors.get('highlight'):
            crops = {r: frame[r] for r in ['weapon_1', 'weapon_2'] if r in frame}
            # Debug: check which crop is actually brighter
            from dl_models.icon_merging import dewhite
            import numpy as np
            dw1 = float(np.percentile(dewhite(crops['weapon_1']), 95))
            dw2 = float(np.percentile(dewhite(crops['weapon_2']), 95))
            pred = self._detectors['highlight'].classify(crops)
            if pred and pred != gt_hl:
                print(f'[hl_mismatch] gt={gt_hl} pred={pred} w1_dw={dw1:.0f} w2_dw={dw2:.0f} '
                      f'w1={self.state.weapon_1.name} w2={self.state.weapon_2.name}', flush=True)
                self._save_mismatch('highlight', crops, gt_hl, pred)

        # Weapon HUD mismatch
        gt_w = self.state.weapon_gt
        if any(gt_w) and self._detectors.get('weapon_hud'):
            crops = {r: frame[r] for r in ['weapon_1', 'weapon_2'] if r in frame}
            pred = self._detectors['weapon_hud'].classify(crops)
            if pred and pred != gt_w:
                self._save_mismatch('weapon_hud', crops, gt_w, pred)

    def _run_mismatch(self, target_ts, entry):
        """Run detector and save crops if GT != pred."""
        if self.state.tab_open or self.state.stop_recoil:
            return

        gt = entry.get('_gt_snapshot')
        if not gt:  # no GT at schedule time, skip
            return

        regions = entry['regions']
        crops = self.capture.get_crops(target_ts, regions)
        if crops is None:
            return

        detector = self._detectors.get(entry['detect'])
        if detector is None:
            return

        pred = detector.classify(crops)
        if pred is None or pred == gt:
            return

        detect_name = entry['detect']
        self._save_mismatch(detect_name, crops, gt, pred)

    def _save_mismatch(self, detect_name, crops, gt, pred):
        """Save mismatched crops for review.

        Filename: gt_{name}_{hl}_pred_{name}_{hl}_{hash6}.png
        """
        save_dir = os.path.join('InGameScreenshot', f'{detect_name}_mismatch')
        os.makedirs(save_dir, exist_ok=True)

        hl_gt = self.state.highlight_gt
        for region_name, crop in crops.items():
            if crop is None:
                continue
            slot = 1 if '1' in region_name else 2

            # Weapon names for this slot
            if isinstance(gt, int):
                # highlight mismatch: gt/pred are slot numbers
                w = self.state.weapon_1 if slot == 1 else self.state.weapon_2
                gt_name = w.name or '?'
                pred_name = gt_name  # weapon name doesn't change
                gt_hl = 'h' if slot == gt else 'l'
                pred_hl = 'h' if slot == pred else 'l'
            else:
                # weapon mismatch: gt/pred are name tuples
                gt_name = gt[slot - 1] or '?'
                pred_name = pred[slot - 1] or '?'
                gt_hl = 'h' if slot == hl_gt else 'l'
                pred_hl = gt_hl  # highlight doesn't change

            # Skip if both name and hl are the same
            if gt_name == pred_name and gt_hl == pred_hl:
                continue

            h = img_hash(crop)
            fname = f's{slot}_gt_{gt_name}_{gt_hl}_pred_{pred_name}_{pred_hl}_{h}.png'
            print(f'[mismatch] {fname} | gt_int={gt} pred_int={pred} slot={slot} hl_gt_state={self.state.highlight_gt}', flush=True)
            path = os.path.join(save_dir, fname)
            if not os.path.exists(path):
                cv2.imwrite(path, crop)

    def _check_attachment_mismatch(self, gun_id, detected, crops):
        """Save crop when detected attachment is invalid for this weapon."""
        from detector.weapon_attachments import validate_attachments
        w = self.state.weapon_1 if gun_id == 1 else self.state.weapon_2
        if not w.name:
            return
        filtered = validate_attachments(w.name, detected)
        save_dir = os.path.join('InGameScreenshot', 'attachment_mismatch')
        for slot_name in ('muzzle', 'grip'):
            if detected.get(slot_name) and detected[slot_name] != filtered.get(slot_name, ''):
                crop_key = f'att_{gun_id}_{slot_name}'
                crop = crops.get(crop_key)
                if crop is None:
                    continue
                os.makedirs(save_dir, exist_ok=True)
                h = img_hash(crop)
                fname = f'{w.name}_{slot_name}_det_{detected[slot_name][:8]}_{h}.png'
                path = os.path.join(save_dir, fname)
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
