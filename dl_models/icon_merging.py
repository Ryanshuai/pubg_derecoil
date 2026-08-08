"""合成 HUD 图标的两件遗物 —— 一件被十处 import，一件只服务训练数据生成。

这个文件曾经是 186 行、五个函数，建模游戏**怎么把美术图合成到屏幕上**：
`alpha_blend`、`blend_tab_background`、`blend_attachment`。它们的输入是带 alpha
通道的 BGRA 图标，也就是**游戏美术资源**——而全仓库的美术图已于 2026-08-05 删除
（理由见 `detector/CLAUDE.md`：美术图是那条合成链的输入，检测器看到的是输出，拿
输入当模板实测 0.489 对 0.975）。输入类别没了，那 120 行就成了一整块死掉的子系统，
2026-08-06 删除。

剩下两件，而且它们不是同一类东西：

    dewhite            **不是 blend**，是一个检测通道：减去估计背景、提取白色信号。
                       六处 import 它（detector ×3、calibration ×3），是这个文件
                       现在**唯一**的内容。

⚠ **2026-08-08，`blend_status_bar` 也删了**：它唯一的调用方 `icon_layout.py` 是
火力模式 CNN 的训练数据合成，而那个 CNN 在 859 张语料上只裁决了 2 张，跟它的
4 MB checkpoint、391 MB 背景板和整条 torch 依赖一起退场了（数字在
`detector/fire_mode_detector.py` 顶部）。**同一个形状第三次**：美术图删了 →
`blend_attachment` 那 120 行死了；模型删了 → `blend_status_bar` 死了。
**合成代码的寿命等于它那类输入的寿命。**

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
