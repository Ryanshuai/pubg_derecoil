# calibration/ — 实验层

**这里只放三样东西：这轮测什么（实验设计）、数字怎么算成结论（分析）、产物怎么落盘。**

一个 `ensure_*` 都不该有。想让游戏做点什么，去 `control/`。

判据跟 `control/CLAUDE.md` 那条是同一条，只是落在这一层：

> 这段代码需要知道「游戏里正在发生什么」吗？需要 → `control/`。

`calibration/` 需要知道的是**测量假设**（这一格要趴着、开镜、满弹匣），不是**怎么到达那个状态**。前者是实验设计，后者是驱动。

## 有机器在管

```
pixi run layering      # 第 6 条：calibration/ 不 import press.*
```

这条不是洁癖。**每一份平行驱动最后都跟它复制的那份 `control/` 版本漂开了**，而漂开的症状不是报错，是一批看起来很正常的错数字：

- `auto_calibrate` 自带的 `analyse` 把玩家自己的鼠标动作记成了后坐力，从第一帧而不是第一发起算。它跟共用版本并存了很久，两边打印的残差不可比而没人知道。
- `harvest` 手按 R 换弹之后 **把 `wait_reload()` 的返回值丢掉了**。换弹卡住 = 一个打不满的弹匣 = 一格被污染的数据，而日志一句话都不会说。同文件另外三处 `wait_reload()` 一直都在检查。

### 欠账账本是个棘轮

违规文件列在 `tools/check_layering.py` 的 `EXEMPT` / `DEBT` 里，**两者的区别是理由归谁**：

| | 理由是谁的 | 会不会过期 |
|---|---|---|
| `EXEMPT` | 代码的（装配根、被测对象本身） | 不会 |
| `DEBT` | 排期的（`docs/refactor_plan.md` 的待办） | **必须离开这张表** |

棘轮有三条分支，都验过：新文件伸手摸 `press` → 红；账本里的文件已经不 import 了但没销账 → **也红**；账本里挂着一个不存在的文件 → 红。

第二条是关键。没有它，账本会烂成永久赦免：有人修好一个文件、忘了删条目，这条规则就对那个文件永久失效，而且没人会发现。

**往 `EXEMPT` 里加东西必须写清理由。** 现有两条的理由都是「这段代码是装配根或被测对象」，不是「改起来麻烦」：

- `sweep.py` — 装配壳。`Rig` 持有那唯一一个 `Pointer` 递给各个 control 驱动，跟 `robot.py` 干的是同一件事。
- `calibrate_k.py` — **K 就是被测量本身**（「发 N counts 视角转多少」）。`ViewDriver` 每个方法都带闭环补偿，拿它测 K 是拿被测量测自己。同文件里的开镜和复位没有这个理由。
- `state.py` — 设备本身就是它的观测对象（`--pico` 报「Pico 在不在、手动上报活没活」），走驱动就成了报告驱动。它什么都不驱动。

## 分层 lint 看不见的那种

`pixi run layering` 只解析 import。所以**拿到高层对象再伸手摸它的 HAL 成员，是查不出来的**：

```python
self.rig.mouse.move(yaw, pitch)      # ✗ 比直接 import press 更难查
self.ac.pointer.drag(src, dst)       # ✗ 绕过了 _reject()
```

两处都已经清掉，各自换成了**具名的**入口：`ViewDriver.turn()`（开环转视角，故意的，注释里写死了为什么）、`InventoryControl.drag(..., verify=False)`。

第二处不只是好看：`ac.pointer.drag` 绕过 `_reject()`，而 `_reject()` 正是「往这把枪没有的槽上拖」的唯一拦截——那种拖拽会把配件掉地上，而看鼠标跟成功一模一样。

## 现有模块

| | 干什么 |
|---|---|
| **实验设计** | `harvest.py`（后坐力主 sweep）· `sweep.py`（`Rig` 装配壳 + CLI）· `weapon_axis.py`（**标杆：全文零硬件调用**）· `scan_compat.py` / `scan_fits.py`（槽位/配件兼容性）· `collect_templates.py` · `capture_ads.py` |
| **分析** | `analysis.py`（**除 numpy 什么都不拉**）· `fit_curve.py` · `analyse_factors.py` · `bullet_detect.py` |
| **落盘 / 事实存储** | `capture_run.py`（`CaptureRun` 格式）· `rpm_store.py` · `kit_facts.py` |
| **状态与库存** | `state.py`（只读探针）· `mismatch.py` |

`analysis.py` 那条「除 numpy 什么都不拉」是可验证的收益，不是形容词：以前查一个 `fit_interval` 要先 import 一个 Pico 后端、一个 torch 的火力模式检测器和 win32gui。离线回归 `pixi run analysis`（合成 trace 的属性检查 + 411 个历史弹匣回放）。

## 产物往哪写

**全部在 `docs/` 下，不在 `calibration/` 旁边。** `.gitignore` 里**故意不再有任何 `calibration/` 规则**——哪天有脚本回退写到源码旁边，`git status` 会变脏，那正是发现它的机制。

分界是**结论 vs 原始数据**，不是文件类型：

| 进 git | 不进 |
|---|---|
| `docs/recoil/weapon_rpm.json` · `docs/pitch/pitch_range.json` · `docs/compat/kit_facts.json` — 几百字节的实测**结论**，别处读取的事实 | `docs/recoil/curves/` · `docs/recoil/runs/` · `docs/k/` — 产生它们的几百兆原始记录 |

采集类产物用 `CaptureRun`（默认 `docs/runs/<kind>/<stamp>/`；`capture_ads` 和 `collect_templates` 用 `create(path=...)` 留在各自的老根目录，理由见下面 5h）。它的 `labelled()` 只返回 `LABEL_REQUESTED`，**永不返回检测器读出来的标签**——理由在 `capture_run.py` 顶部：拿被测检测器的读数当它自己的真值是循环论证，而漂移的检测器不会报错，它会给一个看起来完全合理的错答案。

`CaptureRun.load_dir(<目录>)` 读得了三种形状：现行 `manifest.json`、旧 ADS run 的 `index.jsonl`+`meta.json`、旧模板 run 的 `index.json`。旧 run 是**只读**的，`save()` 直接抛——在 867 帧不可再生的数据旁边再写一份索引就是第二个真值来源。

## 三个反复踩的坑

**一、`pixi run <task>`，别裸 `python`。** 裸 `python` 会被一个坏掉的 nsight-compute bat 劫持。

**二、占游戏焦点的东西，跑之前先跟用户说一声等确认。** 失焦会让整轮数据静默归零，而且事后看不出来。

**三、多个 agent 共用一个 Pico 串口和一个游戏窗口。** 跑之前查有没有别的 python 进程占着（`press.pico_mouse.other_agents`），**别杀别人的**。

## 待办

`docs/refactor_plan.md` 第 5 节。**5h 已完成（2026-08-03）**，第 5 节全清。

### 5h 落盘统一到 `CaptureRun`（已完成）

两个自制 run 格式收敛：`capture_ads`（原 `index.jsonl`+`meta.json`）和 `collect_templates`（原 `index.json`）现在都写 `manifest.json`。**存量一个字节没动**——`CaptureRun.load_dir()` 直接读旧格式，不做一次性转换：那 867 帧不可再生，转换只会产出一份有损副本，而原件重采要占游戏几十分钟。

三条设计判断，理由都写在代码里：

- **旧 run 的标签一律读成 `LABEL_DETECTED`。** 旧目录里没有 `source` 字段，文件本身分不出「要求并确认过」和「检测器读的」，从外面替它补上更强的那个，正是当初制造出那两个坏 run 的动作。副作用正好是想要的：两个坏 run 一经这套 API 读取，`labelled()` 就是空的，**没人需要记得是哪两个 stamp**。
- **`state=ads` 不是标签，是事实。** 它描述的是采集过程（「点了右键，这帧在 700 ms 后」），不是屏幕。`20260801_222936` 就是过程完全按写的跑、却一帧都没开镜。屏幕上到底开没开镜的真值只有人判过一次，那就是 `fit_ads_detector.py` 的 `NOT_SCOPED`/`SCOPED`——**任何采集程序都产不出它**，所以它留在消费者里是对的，不是历史包袱。
- **run 的路径本身是别的回归的真值，所以不搬家。** `tools/test_tab_open.py` 拿 `docs/ads/runs/**` 当「Tab 关着」、`docs/runs/**` 当「拍的就是 Tab 界面」。把 ADS run 挪进 `docs/runs/` 会静默地把 400 帧贴错标签。统一的是 manifest，不是路径。

第三态是这次补上的：**只有有人看过才有标签，`source` 说是谁看的。** 没人确认的意图不给标签——`capture_ads` 的镜子在装上并读回槽位时才是 `REQUESTED`，读回来跟要求不一致就是 `DETECTED`（记读到的那个），谁都没看就没有标签。`collect_templates` 的 `slots`/`rows` 是全仓最硬的真值（刷新器坐标 → 库存行 → 槽位，每一跳都用无模板的手段确认），而 `plate` / `type` **一个标签都不给**，理由在 `label_for()`。

离线回归 `pixi run runs`。

**欠账已清零**（`pixi run layering`）。`EXEMPT` 剩三条，每条都写了代码层面的理由。
