# harness → calibration 的接口契约

给正在重构 `calibration/` 的人。`harness/` 只需要下面三样，签名写死在
`harness/adapter.py`。

**状态（2026-08-03）：三个都接上了，`pixi run night` 能真跑。** 下面标"要新写"的
地方已经过时，保留是因为**接的过程中每一条都改了形状**，而改动的理由比结论有用——
见每节末尾的「实接时改了什么」。

`pixi run night --dry` 会把当前契约原样打出来（那个是活的，这份文档是解释）。

---

## 1. `measure(rigging, cell, mags) -> dict`

测一个 cell，返回**数字**，不返回结论。

| 字段 | 类型 | 含义 |
|---|---|---|
| `reached` | bool | 有没有真的到达这个 cell 标的那个枪/镜/姿势 |
| `reached_why` | str | `reached=False` 时的原因 |
| `mags_kept` | int | 活到拟合的弹匣数 |
| `rate_resid_ms` | float | 各弹匣**对射速的分歧**（ms），不是拟合残差——见下 |
| `rounds` | int | 射速拟合用了多少发 |
| `impulse_off_rounds` | float | **环外检查**：脉冲落点偏了几发。没跑检查就是 `None` |
| `ads_frac` | float | 准星判定「在瞄准」的轮询占比 |
| `track_alive_frac` | float | tracker 撑住的发数占比 |
| `curve` | list | 逐发 dy，落日志用 |

**三条硬要求：**

1. **每个字段都必须有。** `judge()` 遇到缺字段一律判失败——「没测」和「没问题」正是这一层要分开的两件事，缺省成 pass 等于把它们合并。
2. **不返回 `ok`/`usable`。** 判决在 `harness/verdict.py`，必须在测量够不着的地方。测量方自己打分就是这个项目的闭环盲区换个形态：残差近零可以是自洽的错曲线。
3. **游戏状态问题不要抛异常。** 枪没刷出来是 `reached=False` + 原因，不是 exception。异常留给 harness 自己写错了（参数不对、硬件没了），那种要让它冒上来。

`impulse_off_rounds` 是唯一的环外信号，`tools/probe_impulse_align.py` 已经实现了这个测法（放一条除某一发外全零的曲线，看画面在第几发跳）。需要的是**在每个 cell 里插一次**，不是单独跑。

### 实接时改了什么

**一、`rate_resid_ms` 原来的定义是空的。** 契约写的是"射速拟合残差"，而 `interval_from_span`
用的是**两个端点**求间隔——两点定一条线，残差按定义恒等于 0。真去实现会写出
`iv_resid = 0.0` 然后一路 pass，`RATE_RESID_MS_MAX = 12` 拦不住任何东西。事实上代码里
**就是这么写的**，而且那个 0.0 已经被存进 `weapon_rpm.json` 了。

`interval_from_span` 自己的 docstring 说了该查什么：

> 漏掉最后一次跳变会缩短跨度、读成枪更快；它表现为**同一格里各弹匣互相不一致**，
> 所以调用方存之前应当先要求一致。

没有调用方做。代价实测到了：AUG 从**单个** 81.32 ms 的弹匣存成 737.9 rpm，
而前后四个保留弹匣是 82.73–83.39（719.5–725.2）。现在这个字段是**弹匣之间的离散度**。

**二、`impulse_off_rounds` 不该是 per-cell 的。** 契约说"在每个 cell 里插一次"。做不到：
脉冲测试要放一条除某一发外全零的曲线，那不是后坐力测量，也没法在后坐力测量**期间**做。
它是 **per-session 闸门**——`pixi run impulse-ab` 跑一次，`night --impulse-off <n>` 传进来。
不传就每个 cell 都 fail closed，这是对的：时序没验过的话整夜的曲线都不值钱。

**三、`ads_frac` 取最差的那个弹匣，不取平均。** 四个干净的能把一个腰射的抬过 0.90。

**四、`track_alive_frac` 的分母必须包括被丢掉的弹匣。** 只看 `mags` 里的幸存者，
"tracker 撑住了 95%" 在丢了四个弹匣的 cell 上照样成立——它是**幸存者里的**比例。
为此 `measure_cell` 现在额外记 `mags_asked` / `mags_discarded`。
`tools/test_harness.py` 里那条 "one of five kept" 就是钉这个的。

---

## 2. `reset(rigging, level) -> bool`

回到已知状态。两档：

| level | 含义 | 现状 |
|---|---|---|
| `LIGHT = 1` | 收面板、关 Tab、**开镜**、站立 | 已接 |
| `HEAVY = 2` | 重进训练场 | **已经有了**——`RangeSession.ensure(force=True)` |

### 实接时改了什么：LIGHT 原来写反了

契约写的是"清机架、站立、**腰射**"，三条里有两条是错的：

- **没有"退出开镜"这个方法。** 右键是 toggle，全项目只有 `ensure_ads()` 会盯着它到位，
  而它只往**开镜**那个方向盯。`ensure_ads(False)` 会被当成 `tries=0`。
- **`ensure_posture` 需要开镜**——姿势图标在腰射时根本不渲染。复位到腰射会让下一格
  在读不出姿势的情况下瞎 toggle。所以 LIGHT 是**开镜**+站立，不是腰射。
  反正每个 cell 进来都会自己开镜，留着镜子什么都不亏。
- **机架故意不清。** 每个 cell 进来都 strip + 重刷，清一次要多付一个 Tab 会话；
  而且 `drop_weapon` 是这串动作里唯一能把枪扔到地上的，扔了之后下一次刷新的顶替规则就变了。

HEAVY 已经存在这件事值得说明：训练场 20 分钟踢人，`RangeSession` 到 17 分钟主动重进，
重进后背包空、机架空、人在随机位置——**那本来就是一次完整复位**，只是现在唯一的触发条件是时钟。
接上失败streak就行，不用新抽象。

循环的升级策略：第一次失败 LIGHT，第二次 HEAVY，两次都不行的 cell 问题就不在状态上了。

---

## 3. `dump(where, why, frames=None, state=None) -> path`

失败现场落盘，返回目录。

要有：失败那一帧、前后几帧、当时的状态读数、日志尾巴。

**这条是有具体来历的。** 2026-08-02 一次刷新器失败报的是

```
col1_row01 would not expand (<panel open, col1_row02 expanded, 12 entries>)
```

一个字符串，没有帧。于是诊断它需要新写一个探针 + 一次实机运行。而定案只用了三个数字——
读到 12 项、真值图 13 项、被判给的那一行只有 5 项——**这三个全都在那一帧里**。
帧存下来它是个离线问题，没存下来它是一夜。

---

## harness 这边已经写好的

| 文件 | 内容 | 离线可测 |
|---|---|---|
| `manifest.py` | 计划与结果，每个 cell 测完立刻落盘（原子写） | ✅ `pixi run harness` |
| `verdict.py` | 阈值 + `judge(rec)`，纯函数 | ✅ |
| `night.py` | 循环、重试升级、halt streak、report | 循环要等上面三个 |

阈值全部有出处，不是拍的：

| 阈值 | 值 | 来历 |
|---|---|---|
| `IMPULSE_OFF_MAX` | 0.5 发 | 实测第 12 发和第 30 发各三个弹匣，**都是 0 偏差** |
| `ADS_FRAC_MIN` | 0.90 | 实测准星 96% / 姿势图标 48%，这是给准星那条的哨兵 |
| `TRACK_ALIVE_MIN` | 0.50 | **不是目标，是已知缺陷的地板**。wrap 修好之后要往上抬 |
| `MAGS_MIN` | 3 | 一个 cell 打 5 个，tracker 现在吃掉 1~2 个 |
| `RATE_RESID_MS_MAX` | **1.0 ms** | 推出来的：间隔误差 d 会累积，第 k 发晚 `k·d/T` 发，`41×1.0/83 = 0.49` 发 = `IMPULSE_OFF_MAX`。实测尺度 0.24 ms（4 个 AUG 弹匣） |

`RATE_RESID_MS_MAX` 原来是 `import rpm_store.RESID_MS_MAX`（12 ms），理由写的是"免得两边漂开"。
**那两个不是同一个量**：12 ms 界的是"逐发计数器跳变点连成的直线的残差"，而每个跳变点本身
只定位到 ~25 ms；这里界的是"弹匣与弹匣之间的分歧"。借过来的后果是 **11.5 ms 的分歧
（打到弹匣底部差 5.5 发相位）判 usable**。写完检查的当小时被 `pixi run harness` 抓到。

现在两边各写一份（`harness/verdict.py` 和 `calibration/rpm_store.AGREE_MS`），
因为只有 `adapter.py` 能 import calibration。`tools/test_harness.py` 断言两者相等，
所以改一边是**测试红**，不是静默分歧。

---

## manifest 长什么样

```json
{"version": 1, "axis": "weapon", "created": "2026-08-03T02:11:04",
 "params": {"mags": 5, "sight": "red_dot", "postures": ["standing"]},
 "cells": [
   {"id": "aug|standing|red_dot",
    "weapon": "aug", "posture": "standing", "sight": "red_dot",
    "state": "unmeasured",
    "attempts": 0, "verdict": null, "evidence": null, "updated": null}
 ]}
```

`state` 四种：`unmeasured` / `usable` / `failed` / `skipped`。

**`unmeasured` 是重点**——它区分「跑了但废了」和「压根没跑到」。跑完才写的 report 做不到这件事，
而「没跑到」恰恰是无人值守最常见的结局（进程死了、被踢出训练场、天亮了）。

一个文件三个用途：**resume**（`pending()`）、**早上的报告**（`summary()`）、**证据索引**（每个失败 cell 指向自己的目录）。

`skipped` 不进 halt streak——它压根没被尝试过。
