"""Shared utilities for all detectors."""
import numpy as np
import torch
from dl_models.train import MultiHeadMobileNet
from dl_models.icon_merging import dewhite


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


def classify_single(model, crop, device, head_name, class_list):
    """Classify a crop with a single-head model. Returns class name or ''."""
    t = crop_to_tensor(crop, device)
    with torch.no_grad():
        out = model(t)
    idx = out[head_name].argmax(1).item()
    return class_list[idx - 1] if idx > 0 else ''
