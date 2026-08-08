# press/ — 输出 HAL

**这一层知道设备，不知道游戏。**

一句话判据，跟 `capture/`（输入 HAL，它的镜像）是同一条：

> 这段代码原样搬到另一个游戏上还成不成立？成立 → 这一层。

`CMD_MOVE`、COM 口、HID usage ID、字节序——都成立。「开火」「换弹」「开镜」不成立，那些是 `control/`。

```
press/
  pico_mouse.py    Pico CDC 驱动 + get_mouse() 单例 + HID_KEY_*
  pointer.py       光标放置、点击、拖拽，以及转视角的相对移动
  razer_dpi.py     独立 CLI：不装 Synapse 设鼠标 DPI/轮询率
  protocol/        PC ↔ Pico 的线上契约。真相源 + 两端生成物（见它自己的 README）
  firmware/        跑在 RP2350 上的 C。protocol/ 的另一端
```

`protocol/` 和 `firmware/` **2026-08-08 从顶层搬进来**。它们跟这一层的 `.py` 一样不知道游戏，是同一层的三段：PC 侧、契约、设备侧。详见 `press/protocol/README.md` 的「一约两端」。

---

## ⚠ 只有一个后端，而这是个判断，不是历史遗留

`get_mouse()` 只会返回 `PicoMouse`。**没有软件兜底，而且不要再加一个。**

有过一个（`soft_mouse.py`，SendInput，`config.MOUSE_BACKEND = 'soft'` 选择），2026-08-08 删除。理由不是「不够好」：

> **PUBG 的瞄准和扳机走 raw HID。** 所以那个后端的 `click` / `aim_mode` / `set_delta` 在这个项目唯一驱动的那个游戏上**全是空操作**——它能挪系统光标，别的什么都做不了。

它的代价不是零，是负的：

- `Pointer.__init__` 拿到它就 raise，因为「每次点击静默地什么都不做，而这里还在打印 backend = pico」
- 与此同时**三处错误提示在教没有 Pico 的人去用它**（`calibrate_k`、`probe_lobby_transition`、`pointer` 自己）
- 一个 A/B 兜底会在**别的 agent 占着串口时**接管——「我拿不到设备，那我就去驱动那个拿到设备的人的鼠标」。2026-08-03 实际发生过：一次 verify 在 harvest 持有 COM10 时启动，降级之后继续移动光标并试图在人家的运行下面开关 Tab

**提供一个静默什么都不做的后端，比没有后端更糟。** 现在没 Pico 就在 `Pointer()` 构造时失败，消息里带着「是谁占着」。

删它带出四样东西，全是「只剩一个取值」的判据，一并删了：

| 删掉的 | 它变成了什么 |
|---|---|
| `config.MOUSE_BACKEND` | 只有一个合法值 |
| `--backend` 参数链 | `Driver` → 4 个子类 → 6 个 CLI → 10 处使用点，全为传一个常量 |
| `PicoMouse.can_key` / `can_click` | 恒 `True`，守着一个永不成立的分支 |
| `Pointer.backend` / `ViewDriver.backend` | 恒 `'pico'`，还被写进采集 metadata 当作「这次用什么采的」 |
| `Press` 线程（曾在 `press.py`，与包同名） | PC 侧的曲线播放器，忙等 + `mouse.move`。固件的 `get_recoil_delta` 每 1 ms 做同一件事，而且不吃一个核 |

> **一个只能取一个值的字段，记录不了任何东西。** 这条在这一层付过五次账，上表就是那五次。

---

## 压枪曲线在固件里播，这一层只上传

`upload_pattern()` 把曲线交给 Pico，之后**这一层不参与播放**。固件是个时域折线播放器：`get_recoil_delta` 每 1 ms 跑一次，把每个结的 delta 均摊到下一个结之间。

⚠ **`MAX_POINTS` 不是本地常量**，它 import 自 `press/protocol/`，因为固件对超长上传是**钳位而不是拒绝**：

```c
uint16_t count = (n > MAX_PATTERN_POINTS) ? MAX_PATTERN_POINTS : n;
```

只在 PC 一侧调大它，第 300 个结之后整条尾巴被丢掉，上传「成功」，而残差回来看着像是枪变了。它曾经在两个文件里各写一遍 `300`，靠一句 `# must match Pico firmware` 维系——**那句注释不是机制**。

⚠ **`upload_pattern` 曾经把曲线合并成一发一个点**（42 个结），而信封本来装得下 300 个。`MODEL.md` §4 点名这个方法是「挡在模型和固件之间的唯一一处」。信封没限制它，是发信的人自己折的。

---

## `RECOIL_FIRE_DELAY_MS` 不在这里，也不在 `protocol/`

它在 `config.py`，连同大约 70 行的测量记录（为什么是 13 而不是 21 或 36）。

判据是所有权，不是主题：**固件从没听说过这个值**。它是 PC 相对点击排时间表用的偏移，Pico 只管播它收到的东西。它以前在 `pico_mouse.py` 和 `soft_mouse.py` 里各写一个字面量 `13`，joined by `# Match PicoMouse.RECOIL_FIRE_DELAY_MS`——两个后端量的是同一个游戏的同一个物理延迟，**不可能合法地不一致，但也没有任何东西拦着它们不一致**。

同理 `HID_KEY_*` 留在 `pico_mouse.py` 而不进 `protocol/`：固件把 keycode 原样转发给 HID 描述符、不解释，要同意的是这个文件和**游戏**，不是两端。

---

## 有机器在管

```
pixi run layering          press 不许 import win32*（抢前台是闭环，control/focus.py 的事）
                           press 不许 import control（依赖单向）
                           press/protocol/ 只许 import struct
pixi run protocol-check    生成物跟 protocol.toml 漂了就 exit 1
pixi run verify-pico       固件验收，9 项，占串口不占游戏窗口
```

⚠ 上面三条**故意不写编号**。这个仓库的规则编号是散在注释和 CLAUDE.md 里人工维护的标签（6/7/9/10/11 有人引用，其余没有），没有单一真相源，也不是`RULES` 数组的下标——所以在这里安一个号，是在赌一件没有东西会验的事。判据写内容，`pixi run layering` 报错时会打印规则名。

**最后那条挡的是把「行为」放进契约。** 固件是 C，调不了 Python 函数——真放一个编码 helper 进去，Python 用它、C 那边照样手写一份，两边从「数字不一致」升级成「行为不一致」，而后者连 diff 都看不出来。

改协议的顺序、以及 `flash.py` 那两道闸（漂移 / `.uf2` 比 `protocol.h` 旧）在 `press/protocol/README.md`。

**三道新闸都做过变异测试**，因为 `tools/CLAUDE.md` 那条要求就是冲这个来的——「一个闸，如果没人能说出注入什么会让它红，它就还没被验过」：

| 注入 | 结果 |
|---|---|
| 往 `press/protocol/__init__.py` 加一行 `import json` | `layering` 红，报文件:行号 + 规则名 + 理由 |
| 手改 `protocol.h` 里的 `MAX_PATTERN_POINTS` | `protocol-check` 红，指名是哪个生成物 STALE |
| `touch protocol.h` 让它比 `.uf2` 新 | `flash.py` 退出码 1，`Nothing was flashed`，硬件一个字节没动 |

第三条尤其要留着：`--check` 只证明 `protocol.h` 和 `.toml` 一致，它对「头是新的、`.uf2` 是拿旧头编的」报绿。

---

## 跑之前

**这个 Pico 是共用的**，多个 agent 一个串口一个游戏窗口。

```python
from press.pico_mouse import other_agents
```

看到别人在跑就等，**别杀**。串口冲突会报错（好），游戏焦点冲突是静默的（坏）。

`press/razer_dpi.py` 是唯一不走 Pico 的东西：它要求鼠标**直接插 PC**（设完再插回 Pico），所以它跟这一层其余部分不共享那个串口。
