"""Weapon detector — combines HUD watermark model + Tab OCR.

Two signal sources:
  - Model: classifies weapon icon watermark on HUD (real-time, may be noisy)
  - OCR: reads weapon name text in Tab view (accurate, only when Tab open)

Priority: OCR ground truth > model prediction.
Feedback: mismatch between OCR GT and model → save crop. Hard case → save crop.
"""
import logging
import os
import sys
import time
from datetime import datetime

import cv2
import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import WEAPON_HUD_1, WEAPON_HUD_2, GUN_NAME_1, GUN_NAME_2, HARD_CASE_CONF
from detector.cropper import win32_cap
from detector.utils import load_model as _load, crop_to_tensor_4ch, img_hash as _img_hash
from dl_models.icon_layout import WEAPON_CLASSES

HL_NAMES = {0: '', 1: 'highlighted', 2: 'non-highlighted'}

# ── Screen rects ──

ICON_H = 53

def _icon_rect(hud):
    w = hud['x2'] - hud['x1']
    y = hud['y1'] + hud['icon_offset_y']
    return (y, hud['x1'], ICON_H, w)

SLOT_RECTS = {
    1: _icon_rect(WEAPON_HUD_1),
    2: _icon_rect(WEAPON_HUD_2),
}

OCR_RECTS = {
    1: (GUN_NAME_1['y1'], GUN_NAME_1['x1'],
        GUN_NAME_1['y2'] - GUN_NAME_1['y1'], GUN_NAME_1['x2'] - GUN_NAME_1['x1']),
    2: (GUN_NAME_2['y1'], GUN_NAME_2['x1'],
        GUN_NAME_2['y2'] - GUN_NAME_2['y1'], GUN_NAME_2['x2'] - GUN_NAME_2['x1']),
}

# ── Model ──

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'gun_name.pth.tar')
HEAD_SIZES = {'gun_name': len(WEAPON_CLASSES) + 1, 'highlighted': 3}

# ── Feedback ──

FEEDBACK_BASE = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'weapon')
FEEDBACK_GT_DIR = os.path.join(FEEDBACK_BASE, 'gt_mismatch')
FEEDBACK_HARD_DIR = os.path.join(FEEDBACK_BASE, 'hard_case')

# ── Logger ──
_LOG_PATH = os.path.join(FEEDBACK_BASE, 'weapon_detector.log')
os.makedirs(FEEDBACK_BASE, exist_ok=True)
_logger = logging.getLogger('weapon_detector')
_logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(_LOG_PATH, encoding='utf-8')
_fh.setFormatter(logging.Formatter('%(asctime)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
_logger.addHandler(_fh)
OCR_CONF_THRESHOLD = 0.6

# Game display name (lowercased) → internal code
# OCR can ONLY produce codes from this table; anything else is discarded.
OCR_DISPLAY_MAP = {
    # AR
    'akm': 'akm',
    'beryl m762': 'm762',
    'g36c': 'g36c',
    'm416': 'm416',
    'm16a4': 'm16',
    'scar-l': 'scar',
    'mk47 mutant': 'mk47',
    'qbz': 'qbz',
    'aug': 'aug',
    'groza': 'groza',
    'ace32': 'ace32',
    'k2': 'k2',
    'famas': 'famas',
    # SR
    'kar98k': '98k',
    'm24': 'm24',
    'awm': 'awm',
    'lynx amr': 'lynx',
    'win94': 'win94',
    'mosin nagant': 'mosin',
    # DMR
    'slr': 'slr',
    'mini14': 'mini14',
    'mn14': 'mini14',
    'mnl14': 'mini14',
    'sks': 'sks',
    'vss': 'vss',
    'qbu': 'qbu',
    'mk14': 'mk14',
    'mk12': 'mk12',
    'dragunov': 'dragunov',
    # Shotgun
    's686': 's686',
    's12k': 's12k',
    's1897': 's1897',
    'dbs': 'dbs',
    'o12': 'o12',
    # SMG
    'pp-19 bizon': 'pp19',
    'tommy gun': 'tommy',
    'ump': 'ump45',
    'micro uzi': 'uzi',
    'vector': 'vector',
    'mp5k': 'mp5k',
    'p90': 'p90',
    'js9': 'js9',
    'mp9': 'mp9',
    # LMG
    'dp-28': 'dp28',
    'm249': 'm249',
    'mg3': 'mg3',
    # Special
    'crossbow': 'crossbow',
    'mortar': 'mortar',
    'panzerfaust': 'panzerfaust',
}
# Pre-sort by key length descending for longest-match-first substring search
_OCR_KEYS_BY_LEN = sorted(OCR_DISPLAY_MAP.keys(), key=len, reverse=True)


def _edit_distance(a, b):
    """Levenshtein distance."""
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            prev, dp[j] = dp[j], min(dp[j] + 1, dp[j-1] + 1, prev + (0 if a[i-1] == b[j-1] else 1))
    return dp[n]



class WeaponDetector:
    """Unified weapon detector: model + OCR, with feedback."""

    def __init__(self, device):
        self.device = device
        self.model = _load(MODEL_PATH, HEAD_SIZES, device, in_channels=4, hidden_dim=1024)

        # Template matching (replaces OCR)

        # Template matching
        self._tmpl = {}  # code -> [(gray_template, threshold_template)]
        self._load_templates()

        # Tab OCR ground truth
        self._gt = {'weapon_1': '', 'weapon_2': ''}
        self._gt_valid = False
        self._ocr_recent = {'weapon_1': [], 'weapon_2': []}  # last N reads

        # Auto-invalidate: consecutive high-conf model disagreements with GT
        self._mismatch_streak = {'weapon_1': 0, 'weapon_2': 0}
        self.MISMATCH_STREAK_LIMIT = 1  # invalidate immediately on first high-conf mismatch
        self.MISMATCH_CONF_THRESHOLD = 0.9  # only count high-conf disagreements




    # ── Template loading ──

    OCR_TMPL_DIR = os.path.join(os.path.dirname(__file__), '..', 'training_data', 'ocr_white')
    OCR_UNMATCHED_DIR = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'weapon', 'ocr')

    def _load_templates(self):
        """Load pre-computed binary text templates."""
        import re
        if not os.path.isdir(self.OCR_TMPL_DIR):
            return
        for fname in os.listdir(self.OCR_TMPL_DIR):
            m = re.match(r'^([a-z0-9]+)\.png$', fname)
            if not m:
                continue
            code = m.group(1)
            binary = cv2.imread(os.path.join(self.OCR_TMPL_DIR, fname), cv2.IMREAD_GRAYSCALE)
            if binary is None:
                continue
            # Crop to text bounding box
            coords = cv2.findNonZero(binary)
            if coords is None:
                continue
            x, y, w, h = cv2.boundingRect(coords)
            pad = 2
            y1, y2 = max(0, y - pad), min(binary.shape[0], y + h + pad)
            x1, x2 = max(0, x - pad), min(binary.shape[1], x + w + pad)
            tmpl = binary[y1:y2, x1:x2]
            if code not in self._tmpl:
                self._tmpl[code] = []
            self._tmpl[code].append(tmpl)
        if self._tmpl:
            print(f'[Template] Loaded {len(self._tmpl)} weapons, '
                  f'{sum(len(v) for v in self._tmpl.values())} templates')

    _OPEN_KERNEL = np.ones((3, 3), np.uint8)

    @classmethod
    def _white_text_mask(cls, img_bgr):
        """Extract white text: RGB close + bright, then open to remove noise."""
        b, g, r = img_bgr[:,:,0].astype(np.float32), img_bgr[:,:,1].astype(np.float32), img_bgr[:,:,2].astype(np.float32)
        spread = np.max(np.abs(np.stack([b-g, g-r, r-b], axis=2)), axis=2)
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        out = np.zeros_like(gray)
        out[(gray > 180) & (spread < 30)] = 255
        return cv2.morphologyEx(out, cv2.MORPH_OPEN, cls._OPEN_KERNEL)

    def _template_match(self, crop, slot_id=None):
        """Match crop against all loaded templates. Returns (code, score) or ('', 0)."""
        binary = self._white_text_mask(crop)

        crop_pixels = np.count_nonzero(binary)
        if crop_pixels == 0:
            return []

        results = []
        for code, tmpls in self._tmpl.items():
            best_code_iou = -1
            for tmpl in tmpls:
                if tmpl.shape[0] > binary.shape[0] or tmpl.shape[1] > binary.shape[1]:
                    continue
                res = cv2.matchTemplate(binary, tmpl, cv2.TM_CCOEFF_NORMED)
                if res.max() < 0.5:
                    continue
                _, _, _, max_loc = cv2.minMaxLoc(res)
                tx, ty = max_loc
                th, tw = tmpl.shape[:2]
                region = binary[ty:ty+th, tx:tx+tw]
                intersection = np.count_nonzero(region & tmpl)
                union = crop_pixels + np.count_nonzero(tmpl) - intersection
                iou = intersection / max(union, 1)
                if iou > best_code_iou:
                    best_code_iou = iou
            if best_code_iou > 0:
                results.append((best_code_iou, code))
        results.sort(reverse=True)
        return results

    def _save_unmatched_crop(self, crop, slot_id, best_code, best_score):
        os.makedirs(self.OCR_UNMATCHED_DIR, exist_ok=True)
        h = _img_hash(crop)
        fname = f'unmatched_slot{slot_id}_best{best_code}_{best_score:.2f}_{h}.png'
        path = os.path.join(self.OCR_UNMATCHED_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)

    # ── OCR ──

    TMPL_THRESHOLD = 0.85  # minimum IoU to accept template match

    def _ocr_recognize(self, crop, slot_id=None):
        """Recognize weapon name from Tab text crop via template matching.

        Returns (code, iou) or ('', 0).
        """
        results = self._template_match(crop, slot_id)
        if not results:
            _logger.info(f'OCR slot{slot_id} | no match')
            return '', 0.0

        best_iou, best_code = results[0]
        second_iou, second_code = results[1] if len(results) > 1 else (0, '')

        if best_iou >= self.TMPL_THRESHOLD:
            # Multiple above threshold → save for threshold tuning
            above = [(iou, code) for iou, code in results if iou >= self.TMPL_THRESHOLD]
            if len(above) > 1:
                top_str = ', '.join(f'{c}={i:.3f}' for i, c in above[:5])
                _logger.info(f'OCR slot{slot_id} | MULTI [{top_str}] | -> {best_code} (gap={best_iou-second_iou:.3f})')
                self._save_ambiguous_crop(crop, slot_id, above)
            else:
                _logger.info(f'OCR slot{slot_id} | tmpl={best_code} iou={best_iou:.3f} 2nd={second_code}={second_iou:.3f} | -> {best_code}')
            return best_code, best_iou

        # Below threshold
        self._save_unmatched_crop(crop, slot_id, best_code, best_iou)
        _logger.info(f'OCR slot{slot_id} | best={best_code} iou={best_iou:.3f} | rejected (below {self.TMPL_THRESHOLD})')
        return '', 0.0

    def _save_ambiguous_crop(self, crop, slot_id, above):
        ambig_dir = os.path.join(self.OCR_UNMATCHED_DIR, 'ambiguous')
        os.makedirs(ambig_dir, exist_ok=True)
        h = _img_hash(crop)
        top2 = '_'.join(f'{c}{i:.2f}' for i, c in above[:2])
        fname = f'slot{slot_id}_{top2}_{h}.png'
        path = os.path.join(ambig_dir, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)

    def ocr_from_screen(self):
        """Capture and OCR both weapon name slots. Returns {1: (name, conf), 2: (name, conf)}."""
        results = {}
        for slot_id in [1, 2]:
            crop = win32_cap(OCR_RECTS[slot_id])
            results[slot_id] = self._ocr_recognize(crop, slot_id)
        return results

    # ── Tab OCR ground truth ──

    OCR_VOTE_N = 3  # vote among last N reads

    def update_ocr_cache(self):
        """Called while Tab is open. Append valid reads."""
        ocr_results = self.ocr_from_screen()
        for slot_id in [1, 2]:
            result = ocr_results[slot_id]
            if not result or len(result) < 2:
                continue
            name, conf = result
            key = f'weapon_{slot_id}'
            if name and conf > OCR_CONF_THRESHOLD:
                self._ocr_recent[key].append(name)

    def lock_ocr_gt(self):
        """Called when Tab closes. Use last read. Auto-invalidate handles errors."""
        for slot_key in ['weapon_1', 'weapon_2']:
            all_reads = self._ocr_recent[slot_key]
            _logger.info(f'VOTE {slot_key} | all={all_reads}')
            if all_reads:
                self._gt[slot_key] = all_reads[-1]
            else:
                self._gt[slot_key] = ''
            self._ocr_recent[slot_key] = []
        self._gt_valid = True
        _logger.info(f'LOCKED | weapon_1={self._gt["weapon_1"]!r} weapon_2={self._gt["weapon_2"]!r}')
        print(f'[GT weapon] locked: weapon_1={self._gt["weapon_1"]!r}, '
              f'weapon_2={self._gt["weapon_2"]!r}')

    def invalidate_gt(self, reason=''):
        """Called when weapon state may have changed (switch/pickup/drop).
        Clears GT so no more feedback until next Tab."""
        if self._gt_valid:
            self._gt_valid = False
            self._gt = {'weapon_1': '', 'weapon_2': ''}
            _logger.info(f'INVALIDATED | {reason}')
            if reason:
                print(f'[GT weapon] invalidated: {reason}')

    # ── Model classify ──

    def classify_slot(self, crop, slot_id, tab_open=False):
        """Classify weapon from HUD watermark crop.

        Returns (gun_name, hl_name).
        Saves feedback: GT mismatch or hard case (only when tab closed).
        Output priority: GT (if available) > model.
        """
        tensor = crop_to_tensor_4ch(crop, self.device)
        with torch.no_grad():
            out = self.model(tensor)

        gun_logits = out['gun_name'][0]
        gun_probs = F.softmax(gun_logits, dim=0)
        gun_conf = gun_probs.max().item()
        gun_id = gun_probs.argmax().item()
        hl_id = out['highlighted'].argmax(1).item()

        model_name = WEAPON_CLASSES[gun_id - 1] if gun_id > 0 else ''
        hl_name = HL_NAMES[hl_id]

        slot_key = f'weapon_{slot_id}'
        gt = self._gt.get(slot_key, '') if self._gt_valid else ''

        if model_name and self._gt_valid and not tab_open:
            if gt and gt != model_name:
                self._save_gt_mismatch(gt, model_name, hl_name, crop,
                                       slot_id, gun_conf, gun_probs)

                # Auto-invalidate: model disagrees with high confidence
                if gun_conf >= self.MISMATCH_CONF_THRESHOLD:
                    self._mismatch_streak[slot_key] += 1
                    if self._mismatch_streak[slot_key] >= self.MISMATCH_STREAK_LIMIT:
                        _logger.info(f'AUTO_INVALIDATE | {slot_key} gt={gt} '
                                     f'model={model_name} streak={self._mismatch_streak[slot_key]}')
                        self.invalidate_gt(f'model disagrees: gt={gt} model={model_name}')
                else:
                    self._mismatch_streak[slot_key] = 0
            elif not gt:
                pass
            else:
                self._mismatch_streak[slot_key] = 0
                if HARD_CASE_CONF[0] < gun_conf < HARD_CASE_CONF[1]:
                    self._save_hard_case(gt, model_name, hl_name, gun_conf, crop,
                                         slot_id, gun_probs)

        # Output: prefer valid GT, fallback to model
        out_name = gt if (gt and self._gt_valid) else model_name

        return out_name, hl_name

    # ── Feedback save ──

    def _save_gt_mismatch(self, ocr_name, model_name, hl_name, crop,
                          slot_id, gun_conf, gun_probs):
        """Save GT mismatch: ocr_A_dl_B_l/h_<hash>.png + log"""
        os.makedirs(FEEDBACK_GT_DIR, exist_ok=True)
        hl_tag = 'h' if hl_name == 'highlighted' else 'l'
        h = _img_hash(crop)
        fname = f'ocr_{ocr_name}_dl_{model_name}_{hl_tag}_{h}.png'
        path = os.path.join(FEEDBACK_GT_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)
            self._append_log(FEEDBACK_GT_DIR, fname, 'gt_mismatch',
                             ocr_name, model_name, hl_name, slot_id,
                             gun_conf, gun_probs)

    def _save_hard_case(self, ocr_name, model_name, hl_name, conf, crop,
                        slot_id, gun_probs):
        """Save hard case: ocr_A_dl_B_l/h_conf_<hash>.png + log"""
        os.makedirs(FEEDBACK_HARD_DIR, exist_ok=True)
        hl_tag = 'h' if hl_name == 'highlighted' else 'l'
        h = _img_hash(crop)
        fname = f'ocr_{ocr_name}_dl_{model_name}_{hl_tag}_{conf:.2f}_{h}.png'
        path = os.path.join(FEEDBACK_HARD_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)
            self._append_log(FEEDBACK_HARD_DIR, fname, 'hard_case',
                             ocr_name, model_name, hl_name, slot_id,
                             conf, gun_probs)

    def _append_log(self, log_dir, fname, reason, ocr_name, model_name,
                    hl_name, slot_id, conf, gun_probs):
        import datetime
        top_k = torch.topk(gun_probs, min(5, len(gun_probs)))
        top_items = []
        for prob, idx in zip(top_k.values, top_k.indices):
            name = WEAPON_CLASSES[idx.item() - 1] if idx.item() > 0 else 'bg'
            top_items.append(f'{name}={prob.item():.3f}')

        line = (f'{datetime.datetime.now().isoformat()} | {reason} | '
                f'slot={slot_id} hl={hl_name} | '
                f'ocr={ocr_name} dl={model_name} conf={conf:.3f} | '
                f'top5=[{", ".join(top_items)}] | '
                f'{fname}\n')
        log_path = os.path.join(log_dir, 'feedback.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(line)


# ── Module-level API (for hud_poller compatibility) ──

_instance = None

def load_model(device):
    """Init WeaponDetector. Returns the instance (used as 'model' by poller)."""
    global _instance
    _instance = WeaponDetector(device)
    return _instance

def classify_slot(model_or_instance, crop, device, slot_id=None, tab_open=False):
    """Poller calls this. model_or_instance is the WeaponDetector."""
    inst = model_or_instance
    if slot_id is None:
        slot_id = 1
    return inst.classify_slot(crop, slot_id, tab_open=tab_open)

def update_ocr_cache():
    """Called by poller while tab is open."""
    if _instance:
        _instance.update_ocr_cache()

def lock_ocr_gt():
    """Called by poller when tab closes."""
    if _instance:
        _instance.lock_ocr_gt()

def invalidate_gt(reason=''):
    """Called when weapon state may have changed."""
    if _instance:
        _instance.invalidate_gt(reason)


# ── Standalone main ──

def main():
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'Device: {device}')
    detector = WeaponDetector(device)
    print('Weapon detector ready (model + OCR).\n')

    prev_state = {1: None, 2: None}
    hz = 5

    while True:
        for slot_id in [1, 2]:
            crop = win32_cap(SLOT_RECTS[slot_id])
            gun_name, hl_name = detector.classify_slot(crop, slot_id)
            state = (gun_name, hl_name)

            if state != prev_state[slot_id]:
                prev_state[slot_id] = state
                slot_label = 'main' if slot_id == 1 else 'sub'
                if gun_name:
                    print(f'[slot {slot_id} {slot_label}] {gun_name}  ({hl_name})')
                else:
                    print(f'[slot {slot_id} {slot_label}] (empty)')

        time.sleep(1.0 / hz)


if __name__ == '__main__':
    main()
