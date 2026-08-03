# harness → calibration 的接口契约

给正在重构 `calibration/` 的人。`harness/` 只需要下面三样，签名已经写死在
`harness/adapter.py`，实现落在 calibration 侧，adapter 只做转接。

`pixi run night --dry` 会把这份契约原样打出来。

---

## 1. `measure(rig, ac, cell, mags) -> dict`

测一个 cell，返回**数字**，不返回结论。

| 字段 | 类型 | 含义 |
|---|---|---|
| `reached` | bool | 有没有真的到达这个 cell 标的那个枪/镜/姿势 |
| `reached_why` | str | `reached=False` 时的原因 |
| `mags_kept` | int | 活到拟合的弹匣数 |
| `rate_resid_ms` | float | 射速拟合残差 |
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

---

## 2. `reset(session, level) -> bool`

回到已知状态。两档：

| level | 含义 | 现状 |
|---|---|---|
| `LIGHT = 1` | 收面板、关 Tab、清机架、站立、腰射 | **不存在，要新写** |
| `HEAVY = 2` | 重进训练场 | **已经有了**——`RangeSession.ensure(force=True)` |

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
| `RATE_RESID_MS_MAX` | 12 ms | 直接 import `rpm_store.RESID_MS_MAX`，不重打，免得两边对「拟合算好」的定义漂开 |

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
