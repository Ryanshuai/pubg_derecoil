"""Weapon DL classifier — real-time HUD watermark recognition.

Classifies weapon name from HUD icon crop. No highlight detection
(moved to highlight_detector.py).
"""
import os

import torch
import torch.nn.functional as F

from detector.utils import load_model as _load, crop_to_tensor_4ch
from dl_models.icon_layout import WEAPON_CLASSES

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'gun_name.pth.tar')
HEAD_SIZES = {'gun_name': len(WEAPON_CLASSES) + 1, 'highlighted': 3}


class WeaponClassifier:

    def __init__(self, device):
        self.device = device
        self.model = _load(MODEL_PATH, HEAD_SIZES, device, in_channels=4, hidden_dim=1024)

    def classify(self, crops):
        """Classify weapon names from crop dict.

        crops: {'weapon_1': np.ndarray, 'weapon_2': np.ndarray}
        Returns: (name_1, name_2) tuple of weapon name strings, 0 if unknown.
        """
        results = []
        for key in ['weapon_1', 'weapon_2']:
            crop = crops.get(key)
            if crop is None:
                results.append('')
                continue

            t = crop_to_tensor_4ch(crop, self.device)
            with torch.no_grad():
                out = self.model(t)

            probs = F.softmax(out['gun_name'][0], dim=0)
            gun_id = probs.argmax().item()
            name = WEAPON_CLASSES[gun_id - 1] if gun_id > 0 else ''
            results.append(name)

        return tuple(results)
