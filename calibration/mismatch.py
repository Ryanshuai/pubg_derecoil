"""Training-data collection: save crops where ground truth and prediction differ.

This rides along on the live control loop because that is the only place the
two halves meet — the frame and the GT the operator just asserted by pressing
a key. It is NOT part of the loop's job. It used to live inside the dispatcher
(now control/match.py), which made that file both a real-time control loop and
a data labeller, and left disk writes sitting next to the code that has to hit
a firing window.

What it produces is deliberate training data, not noise:
    InGameScreenshot/highlight_mismatch/
    InGameScreenshot/weapon_hud_mismatch/
    InGameScreenshot/attachment_mismatch/
Those directories are the point of this module. Nothing here filters them
down — a disagreement is exactly the case worth keeping.
"""
import os

import cv2

from config import MISMATCH_POLL_INTERVAL, GT_SETTLE_TIME
from detector.utils import img_hash


class MismatchCollector:
    """Compares detector output against asserted GT and writes the crops.

    Holds references to the live `state`, `capture` and the dispatcher's
    detector registry — the registry is mutated in place by register(), so
    detectors added after construction are still visible here.
    """

    def __init__(self, state, capture, detectors):
        self.state = state
        self.capture = capture
        self.detectors = detectors
        self._last_poll = 0.0

    def poll(self, now):
        """Periodic mismatch collection: while GT is valid, compare pred every 500ms."""
        if now - self._last_poll < MISMATCH_POLL_INTERVAL / 1000.0:
            return
        if self.state.tab_open or self.state.stop_recoil:
            return
        if now - self.state.highlight_gt_ts < GT_SETTLE_TIME / 1000.0:
            return
        self._last_poll = now

        ts, frame = self.capture.latest()
        if frame is None:
            return

        # Highlight mismatch
        gt_hl = self.state.highlight_gt
        if gt_hl and self.detectors.get('highlight'):
            crops = {r: frame[r] for r in ['weapon_1', 'weapon_2'] if r in frame}
            # Debug: check which crop is actually brighter
            from dl_models.icon_merging import dewhite
            import numpy as np
            dw1 = float(np.percentile(dewhite(crops['weapon_1']), 95))
            dw2 = float(np.percentile(dewhite(crops['weapon_2']), 95))
            pred = self.detectors['highlight'].classify(crops)
            if pred and pred != gt_hl:
                print(f'[hl_mismatch] gt={gt_hl} pred={pred} w1_dw={dw1:.0f} w2_dw={dw2:.0f} '
                      f'w1={self.state.weapon_1.name} w2={self.state.weapon_2.name}', flush=True)
                self.save('highlight', crops, gt_hl, pred)

        # Weapon HUD mismatch
        gt_w = self.state.weapon_gt
        if any(gt_w) and self.detectors.get('weapon_hud'):
            crops = {r: frame[r] for r in ['weapon_1', 'weapon_2'] if r in frame}
            pred = self.detectors['weapon_hud'].classify(crops)
            if pred and pred != gt_w:
                self.save('weapon_hud', crops, gt_w, pred)

    def run_scheduled(self, target_ts, entry):
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

        detector = self.detectors.get(entry['detect'])
        if detector is None:
            return

        pred = detector.classify(crops)
        if pred is None or pred == gt:
            return

        self.save(entry['detect'], crops, gt, pred)

    def save(self, detect_name, crops, gt, pred):
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

    def check_attachment(self, gun_id, detected, crops):
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
