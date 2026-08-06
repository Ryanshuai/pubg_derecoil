"""合成 HUD 图标的两件遗物 —— 一件被十处 import，一件只服务训练数据生成。

这个文件曾经是 186 行、五个函数，建模游戏**怎么把美术图合成到屏幕上**：
`alpha_blend`、`blend_tab_background`、`blend_attachment`。它们的输入是带 alpha
通道的 BGRA 图标，也就是**游戏美术资源**——而全仓库的美术图已于 2026-08-05 删除
（理由见 `detector/CLAUDE.md`：美术图是那条合成链的输入，检测器看到的是输出，拿
输入当模板实测 0.489 对 0.975）。输入类别没了，那 120 行就成了一整块死掉的子系统，
2026-08-06 删除。

剩下两件，而且它们不是同一类东西：

    dewhite            **不是 blend**，是一个检测通道：减去估计背景、提取白色信号。
                       十处 import 它（detector ×3、calibration ×3、dl_models ×1、
                       temp_debug ×3），是这个文件唯一还被广泛用到的东西。
    blend_status_bar   真正的合成，唯一调用方是 icon_layout（火力模式条的训练数据）。

⚠ **十个消费者里六个在推理侧**（detector / calibration），而这个文件在训练侧。
`dewhite` 的家其实应该在 `detector/`，搬它要动十个文件，还没做。
"""

import cv2
import numpy as np


def dewhite(img_bgr):
    """
    减去估计背景, 提取白色图标信号, 返回单通道 grayscale.

    用于 weapon HUD 检测的第 4 通道.
    """
    bg_est = cv2.GaussianBlur(img_bgr.astype(np.float32), (31, 31), 10)
    signal = np.clip((img_bgr.astype(np.float32) - bg_est) * 2, 0, 255)
    return cv2.cvtColor(signal.astype(np.uint8), cv2.COLOR_BGR2GRAY)


def blend_status_bar(canvas, icon_alpha_mask, x, y, blur_k=21, gradient=0.67):
    """
    底部状态栏 (开火模式): 模糊 + 暗化 + 白色图标叠加

    Verified on 0020/0023: gradient=0.67 (range 0.65~0.69), blur_k=21
    Bar region: y=1312~1370
    Formula: output = icon_alpha * 255 + (1 - icon_alpha) * 0.67 * blur(bg, k=21)
    """
    ih, iw = icon_alpha_mask.shape[:2]
    region = canvas[y:y + ih, x:x + iw].astype(np.float32)
    blurred = cv2.GaussianBlur(region, (blur_k, blur_k), 0)
    darkened = gradient * blurred
    alpha = icon_alpha_mask[:, :, np.newaxis]
    canvas[y:y + ih, x:x + iw] = np.clip(
        alpha * 255 + (1 - alpha) * darkened, 0, 255
    ).astype(np.uint8)
