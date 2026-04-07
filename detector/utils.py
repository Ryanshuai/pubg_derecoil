"""Shared utilities for all detectors."""
import cv2
import numpy as np
import torch
from dl_models.train import MultiHeadMobileNet
from dl_models.icon_merging import dewhite


def img_hash(img, length=8):
    """Content hash — MD5 of downscaled pixels, truncated to `length` hex chars."""
    import hashlib
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    resized = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA)
    return hashlib.md5(resized.tobytes()).hexdigest()[:length]


def load_model(path, head_sizes, device, in_channels=3, hidden_dim=128):
    """Load a MultiHeadMobileNet model from checkpoint."""
    model = MultiHeadMobileNet(head_sizes, in_channels=in_channels, hidden_dim=hidden_dim).to(device)
    model.load_state_dict(torch.load(path, map_location=device, weights_only=True))
    model.eval()
    return model


def crop_to_tensor(crop, device):
    """BGR uint8 (H,W,3) -> (1,3,H,W) float32 tensor."""
    t = torch.from_numpy(
        crop.transpose(2, 0, 1).astype(np.float32) / 255.0
    )
    return t.unsqueeze(0).to(device)


def crop_to_tensor_4ch(crop, device):
    """BGR uint8 (H,W,3) -> (1,4,H,W) float32 tensor (BGR + dewhite)."""
    dw = dewhite(crop)
    bgrd = np.dstack([crop, dw])
    t = torch.from_numpy(
        bgrd.transpose(2, 0, 1).astype(np.float32) / 255.0
    )
    return t.unsqueeze(0).to(device)
