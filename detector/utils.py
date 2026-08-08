"""Shared utilities for all detectors.

⚠ THIS FILE USED TO PULL IN torch. `load_model` built the MultiHeadMobileNet
that FireModeDetector loaded, and `crop_to_tensor_4ch` fed it; both went on
2026-08-08 with the model itself (the numbers are in
detector/fire_mode_detector.py's docstring — the net answered 2 frames in 859).

That is why one shared helper module importing torch mattered: EVERY detector
imports this file, so a torch import here put torch on the import path of the
offline regression suite, of tools/ scripts that only wanted img_hash, and of
anything that touched a detector at all. One line, whole-graph reach.
"""
import cv2


def img_hash(img, length=6):
    """Content hash — MD5 of downscaled pixels, truncated to `length` hex chars."""
    import hashlib
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    resized = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA)
    return hashlib.md5(resized.tobytes()).hexdigest()[:length]
