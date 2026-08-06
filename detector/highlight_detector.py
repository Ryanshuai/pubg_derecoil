"""Which weapon slot is highlighted (in hand) — from the two HUD crops.

Returns 1 or 2, or 0 when the two slots score identically.

The judgement is how LIT a slot is, and nothing else. The HUD draws the held
weapon at alpha 0.80 and the stowed one at 0.405, so the brighter overlay is
the held one — a comparison between the two crops, which is why it needs no
model of what either weapon looks like.

⚠ It used to ALSO run a four-hypothesis alpha-unmix (white/red x held/stowed)
against the weapon's own icon, and that machinery is gone. It was measured on
the 254 labelled pairs in training_data/highlight_eval and contributed
NOTHING: 254/254 with it, 254/254 without, and 6x faster without (0.60 ms per
pair against 3.6). The ALIGN_JITTER knob it was wired through went with it —
that knob's own comment already recorded three jitter settings scoring
identically. Regression: temp_debug/eval_highlight_jitter.py.
"""
import cv2
import numpy as np

# 同一个算子在这里曾经有第二份逐字相同的实现（`_dewhite`）：高斯背景估计、减、
# ×2、钳位、转灰度，两边连 (31,31) 和 sigma=10 都一样。带 docstring 的那份是原件
# ——它说得出这个通道是干什么的（weapon HUD 检测的第 4 通道），而副本说不出。
#
# `detector/ads_detector.py` 早就在从那里 import 同一个函数，所以方向是现成的，
# 这里只是把最后一份副本并回去。
from dl_models.icon_merging import dewhite


def _combined_max(img_bgr):
    """How lit this slot is. Two channels, because the HUD has two ways to
    draw an active weapon: the white overlay, and the red one it uses when the
    weapon is unusable. Whichever is stronger is the answer."""
    dw = dewhite(img_bgr).astype(np.float32)
    r = img_bgr[:, :, 2].astype(np.float32)
    g = img_bgr[:, :, 1].astype(np.float32)
    return max(float(np.percentile(dw, 95)), float(np.percentile(r - g, 90)))


class HighlightDetector:

    def classify(self, crops):
        """Determine which slot is highlighted.

        crops: {'weapon_1': np.ndarray, 'weapon_2': np.ndarray}
        Returns: int (1 or 2), or 0 if can't determine.
        """
        crop1 = crops.get('weapon_1')
        crop2 = crops.get('weapon_2')
        if crop1 is None or crop2 is None:
            return 0

        s1 = _combined_max(crop1)
        s2 = _combined_max(crop2)
        if s1 == s2:
            return 0
        return 1 if s1 > s2 else 2
