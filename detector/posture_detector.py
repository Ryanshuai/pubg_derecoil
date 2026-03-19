"""Real-time posture detector.

Captures posture icon region from screen, classifies standing/crouching/prone.
Saves hard cases (low confidence) for later labeling.
"""
import os
import sys
import time
import cv2
import torch
import torch.nn.functional as F

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import POSTURE, HARD_CASE_CONF
from detector.cropper import win32_cap
from detector.utils import load_model as _load, crop_to_tensor_4ch, img_hash as _img_hash
from dl_models.icon_layout import POSTURE_CLASSES

# Screen rect: (y, x, h, w) for win32_cap
SLOT_RECT = (POSTURE['y1'], POSTURE['x1'],
             POSTURE['y2'] - POSTURE['y1'],
             POSTURE['x2'] - POSTURE['x1'])

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'dl_models', 'posture.pth.tar')
HEAD_SIZES = {'posture': len(POSTURE_CLASSES) + 1}

# Hard case feedback
FEEDBACK_DIR = os.path.join(os.path.dirname(__file__), '..', 'InGameScreenshot', 'posture')


def load_model(device):
    return _load(MODEL_PATH, HEAD_SIZES, device, in_channels=4)


def classify(model, crop, device):
    """Classify posture. Saves crop when confidence < threshold."""
    global _feedback_idx

    t = crop_to_tensor_4ch(crop, device)
    with torch.no_grad():
        out = model(t)

    logits = out['posture'][0]
    probs = F.softmax(logits, dim=0)
    conf = probs.max().item()
    idx = probs.argmax().item()
    name = POSTURE_CLASSES[idx - 1] if idx > 0 else ''

    # Hard case: model is uncertain (not bg, not confident) → save for labeling
    if HARD_CASE_CONF[0] < conf < HARD_CASE_CONF[1] and name:
        os.makedirs(FEEDBACK_DIR, exist_ok=True)
        h = _img_hash(crop)
        fname = f'{name}_{h}.png'
        path = os.path.join(FEEDBACK_DIR, fname)
        if not os.path.exists(path):
            cv2.imwrite(path, crop)

    return name


def main():
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'Device: {device}')
    model = load_model(device)
    print(f'Model loaded. Classes: {POSTURE_CLASSES}')
    print('Detecting...\n')

    prev = None
    hz = 5

    while True:
        crop = win32_cap(SLOT_RECT)
        name = classify(model, crop, device)

        if name != prev:
            prev = name
            label = name if name else '(none)'
            print(f'[posture] {label}')

        time.sleep(1.0 / hz)


if __name__ == '__main__':
    main()
