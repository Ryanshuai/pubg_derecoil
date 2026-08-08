# calibration/ — 实验层

**这里只放三样东西：这轮测什么（实验设计）、数字怎么算成结论（分析）、产物怎么落盘。**

一个 `ensure_*` 都不该有。想让游戏做点什么，去 `control/`。

⚠ **「不该有」指的是不该自己实现，不是不该调。** 这一层每个驱动游戏的入口（`collect_timed` / `collect_templates` / `scan_compat` / `scan_fits` / `calibrate_k` / `capture_ads`，以及 `harness/night.py`）开场统一调 `control.session.ensure_ready()`——那是 `control/` 的东西，正是 `pixi run layering` 第 9 条要求的方向。它是**五步**：进程起着 → 焦点 → 在局内 → Tab 收起 → 刷新器面板收起。

⚠ **「走到 200m 靶道」以前是这里的第六步，2026-08-08 搬进了 `LobbyControl.ensure_in_match()`。** 它不是「游戏听不听得见我」而是「有没有人会撞我」：出生点是主场地，人多的服上有车穿过，而**被撞掉的弹匣不会自己报告**——轨迹里只是混进了别人的物理，每一道闸照样绿。搬家的理由是这一层付的账：`AutoSession.enter()` 回到局内**不经过 `ensure_ready`**，于是那个「已经在靶道了」的标志留着不动，下一次 `ensure_ready` 跳过一次从未发生的传送，**一轮 harvest 的后半程 45 分钟全在车流里打完**。现在**移动角色的模块就是知道角色被移动过的模块**，`ensure_ready` 只把 `range_name` 透传下去。

判据跟 `control/CLAUDE.md` 那条是同一条，只是落在这一层：

> 这段代码需要知道「游戏里正在发生什么」吗？需要 → `control/`。

`calibration/` 需要知道的是**测量假设**（这一格要趴着、开镜、满弹匣），不是**怎么到达那个状态**。前者是实验设计，后者是驱动。

## 有机器在管

```
pixi run layering      # 第 6 条：calibration/ 不 import press.*
```

这条不是洁癖。**每一份平行驱动最后都跟它复制的那份 `control/` 版本漂开了**，而漂开的症状不是报错，是一批看起来很正常的错数字：

- `auto_calibrate` 自带一份 `analyse`，把玩家自己的鼠标动作记成了后坐力，从第一帧而不是第一发起算。它跟共用版本并存了很久，两边打印的数不可比而没人知道。**两个文件 2026-08-08 都随旧坐标删掉了**，这条留着是因为形状会复发：并行实现漂开的症状不是报错，是一批看起来很正常的错数字。
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
| **实验设计** | `collect_timed.py`（打进样本库；拿**已经在手上**的枪，不刷不装）· `sweep.py`（只剩 `Rig` 装配壳）· `scan_compat.py` / `scan_fits.py`（槽位/配件兼容性）· `collect_templates.py` · `capture_ads.py` |
| **分析** | `samples.py`（样本库，**永不删除**）· `fit_time_curve.py`（聚类 + 一次性全量拟合，`--selftest` 离线）· `bullet_detect.py` |
| **落盘 / 事实存储** | `capture_run.py`（`CaptureRun` 格式）· `rpm_store.py` · `kit_facts.py` |
| **模板构建 / 审计** | `solve_template.py` · `score_attachments.py` · `build_name_templates.py` · `build_lobby_tab_templates.py` · `build_weapon_hud_bank.py` · `audit_curves.py` |
| **状态与库存** | `state.py`（只读探针）· `mismatch.py` · `scan_slot_bleed.py` |
| **后坐力事实** | `build_kit_factors.py`（→ `data/kit_factors.json`，`pixi run kit-factors`）· `probe_hole_pattern.py` |

**最后那一行 2026-08-06 从 `tools/` 搬过来，判据是本文件第一句的第三样东西：「产物怎么落盘」。** 它们全都 `--write` 一份检测器当事实读的模板或掩膜，也全都**不碰游戏、不碰硬件**（`press` / `control` import 数为 0，所以搬进来不动规则 6）。留在 `tools/` 的代价是实的：那一层的自我描述是「这里没有别人 import 的东西」，而 `score_attachments` 一直在 import `solve_template`，`scan_compat` / `scan_fits` 至今还 import `tools.drive_screen`——**一个声称没有出边的层长出了出边，就没人再检查它的出边。**

### 2026-08-08 又搬了四个，同一条判据，外加一条新的

`build_kit_factors` · `build_weapon_hud_bank` · `scan_slot_bleed` · `probe_hole_pattern`。前三个是 2026-08-06 那批的直接续集（`--write` 一份别人当事实读的产物，不碰游戏不碰硬件）；`probe_hole_pattern` 是**测量本身**——弹孔散布是整条标定链之外唯一的那个数，链内每个数都是拿链自己的输出判的。

**新的那一条判据，是被一次搬错买来的：**

> **规则 6 的检查器只解析 import 语句。一条 `sys.path.insert` 拼出来的边它看不见。**

`probe_ammo_ocr` 搬过来又搬回去了。AST 上它只 import `config` + `detector`，跟上面三个长得一样干净；而它的 `--selftest` 里有 `sys.path.insert(ROOT/'tools')` + `from collect_ammo_digits import validate`，`collect_ammo_digits` import `press`。**搬进来那一刻规则 6 就破了，而 `pixi run layering` 是绿的。**

`fit_pitch_level` / `probe_pitch_range` 那一对同理留在 `tools/`：`fit_pitch_level` 直接 import `press.pico_mouse.other_agents`，那是「有没有别的 agent 占着 Pico」的守卫，而 `ensure_ready` **不含**这一项——为了搬家删掉它就是真丢一道闸。

⚠ 收益是立刻兑现的，而且正说明为什么该搬：`check_params` 扫 `control/calibration/harness/press/detector`，**不扫 `tools/`**。`probe_hole_pattern` 一进来就被咬出一个没人读的 `mag_size`。**它在 `tools/` 里躺着的时候，没有任何机器在看它。**

`drive_screen.py` **没有**跟着搬，虽然两个 calibration 模块 import 它。2026-08-07 之前的理由是「它整份都在做 `ensure_focus` → `ensure_in_match` → 开面板 → 验证」——**那半句现在不成立了**：焦点和局内那两条腿已经从 `drive()` 里拿掉，挪进它自己的 `main()`（整个五步 `ensure_ready`，并且把「正在拍的那块屏幕」那条腿关掉，否则会把要拍的东西关了）。`drive()` 现在只剩「开这一块屏、验它开了、拍、关」。

⚠ 它挂在规则 9 的 DEBT 上挂了一年，**理由是假的**：那条写着「`scan_compat` 一轮调 `drive()` 30 次，放 `ensure_ready` 会传送 30 次」。`scan_compat` import 的是 `SCREENS` 不是 `drive`，全仓 `grep 'drive('` 只有一个调用方（它自己的 `main`），而且 `scan_compat` 2026-08-06 起就已经开场调 `ensure_ready` 了。三条全是一次 grep 就能验的，一条都没验过。**账本里的理由是一个关于代码的断言**，棘轮只验「这个文件还违不违规」，没有任何东西验那段散文。

### `calibration/` 内部一律写 `calibration.X`（规则 10）

裸的 `from sweep import Rig` 和 `from calibration.sweep import Rig` 并存会**把文件加载两遍**，两个名字、两份类、两份模块级常量。2026-08-07 实测：`harness/adapter.py` 建的是 `calibration.sweep.Rig`，交给当时的 `calibration/harvest.py`，而后者持有的是 `sweep.Rig`——**整个无人值守夜晚两个 `Rig` 类不是同一个**。鸭子类型把它藏到有人问身份为止，而那时症状说不出成因。

58 处已改（`import rpm_store` 要写成 `from calibration import rpm_store`，否则局部名不再绑定）。`sys.path.insert` 那几行**故意留着**——删它是另一件更险的事，而只要它们在，规则 10 就是唯一挡着第二份 `sweep.py` 的东西。

`analysis.py` 那条「除 numpy 什么都不拉」是可验证的收益，不是形容词：以前查一个 `fit_interval` 要先 import 一个 Pico 后端、一个 torch 的火力模式检测器和 win32gui。离线回归 `pixi run analysis`（合成 trace 的属性检查 + 411 个历史弹匣回放）。

## 产物往哪写

**全部在 `docs/` 下，不在 `calibration/` 旁边。** `.gitignore` 里**故意不再有任何 `calibration/` 规则**——哪天有脚本回退写到源码旁边，`git status` 会变脏，那正是发现它的机制。

**`docs/` 整个不进 git**（`.gitignore` 第 19 行就一个 `docs`），2.7 GB，0 个文件被跟踪。

⚠ 这里原来写着一张「结论进 git、原始数据不进」的分界表，点名 `docs/recoil/weapon_rpm.json` 等三个文件在 git 里。**那从来没成立过**——查一次 `git ls-files docs/` 就知道是 0。2026-08-05 改成实际情况。

代价是清醒的：**`weapon_rpm.json`、`pitch_range.json`、`kit_facts.json` 这些几百字节的实测结论没有版本历史，删了就没了**，而它们正是别处当事实读取的东西。哪天要给它们上版本，得单独开一条 `!docs/**/xxx.json` 的例外，而不是把 `docs` 整个放进来。

采集类产物用 `CaptureRun`（默认 `docs/runs/<kind>/<stamp>/`；`capture_ads` 和 `collect_templates` 用 `create(path=...)` 留在各自的老根目录，理由见下面 5h）。它的 `labelled()` 只返回 `LABEL_REQUESTED`，**永不返回检测器读出来的标签**——理由在 `capture_run.py` 顶部：拿被测检测器的读数当它自己的真值是循环论证，而漂移的检测器不会报错，它会给一个看起来完全合理的错答案。

`CaptureRun.load_dir(<目录>)` 读得了三种形状：现行 `manifest.json`、旧 ADS run 的 `index.jsonl`+`meta.json`、旧模板 run 的 `index.json`。旧 run 是**只读**的，`save()` 直接抛——在 867 帧不可再生的数据旁边再写一份索引就是第二个真值来源。

`labelled()` 还会挡掉**自相矛盾**的标签（`conflicts()`）：同一个截图文件被两条 entry 说成不同的东西时，至多一条描述的是磁盘上的像素，而文件本身分不出是哪条。这不是假想——`collect_templates` 的库存行图叫 `row00__sks__lbg0.png`，名字里没有轮次，于是多轮 run 的后一轮直接覆盖前一轮的文件，两条 entry 却都留着。7 个 run、130 个文件、**580 条标签**，有一个文件被 12 个配件同时声称。文件名已经加上轮次了；存量的图一张没删，`entries` 照样列出来，只是 `labelled()` 不再把它们当真值发出去。

## 三个反复踩的坑

**一、`pixi run <task>`，别裸 `python`。** 裸 `python` 会被一个坏掉的 nsight-compute bat 劫持。

**二、占游戏焦点的东西，跑之前先跟用户说一声等确认。** 失焦会让整轮数据静默归零，而且事后看不出来。

**三、多个 agent 共用一个 Pico 串口和一个游戏窗口。** 跑之前查有没有别的 python 进程占着（`press.pico_mouse.other_agents`），**别杀别人的**。

## `the gun left rack slot N during "<step>"` — 已解决 2026-08-04

采集器最大的单一失效来源，11 个 run、74 次，一次废掉一个件。**根因是读数器，不是拖拽**：

AKM 自己的弹匣画在它的 magazine 贴片框里，裸枪也有 395 个 Canny 边缘（阈值 120），所以那个槽**永远读 `filled`**。`strip` 于是对着已经空了的槽再拉一次，而空槽上的手势会打到底下的武器行，把整枪扔地上。

三个假设先后被自己的数据否掉（重试打空槽、空槽画水印、内容读回能拦），最后是人眼盯屏看出来的：**配件依次落地之后，又拉了一遍，把枪拉下去了。**

判据换成**正向识别配件**（`detector/slot_detector.py` 有数字和取舍），`scope` 位也一起接上——它以前恒 `unknown` 而 `unequip` 放行 `unknown`，是同一条路径的另一半。

## 后坐力分析：坐标换了，这一节的旧内容整体退场

**规格在 `MODEL.md`，它是主法则。** 拟合的是 `y_true(t)`——**时间**的函数，不是
弹号的函数。这一节原来有 150 行讲弹桶、`np.interp` 边界、末发离散、EMA 的三种
被否掉的改法，`analyse()` 2026-08-08 删掉之后**它们描述的东西已经不存在了**。

不是「过时了懒得改」，是**它们问的问题没有指称对象了**：

| 旧问题 | 现在 |
|---|---|
| 第 k 发的位移记进了第几个桶 | 没有桶 |
| `np.interp` 的 `right=` 钳位把末发分给谁 | 不存在 |
| 火循环 `span/(n−1)` 的 5% 误差累积成 2 发 | 不出现在模型里 |
| 弹药计数器和点击两个原点差 `W = 13 ms` | 只剩点击一个，而且是本仓库自己发的 |
| 这一格收敛了吗 | **整个消失**。剩下的只有「样本够不够」，那是个可以直接数的量 |

⚠ **有两条教训跟坐标无关，所以搬到这里留着**，它们是这一层反复付账的那两个
形状：

**一、`build_weapon` 从来不设 `scope`，于是每一轮在任何倍率下都在发红点的曲线。**
`Weapon.set_seq` 里 `factor = scope_factor * ...`，而 `scope_factor` 只有
`set('scope', …)` 会写。症状**伪装成了检测器故障**：补偿只有 1/4 → 视角被推
2692 counts → 参考图块（容量 68）wrap → 报「相关器丢了视角」。**看起来像检测器
坏了，其实是补偿发小了。** 这是「症状指向错误的方向」在这一层最贵的一次。

**二、聚合量看不见它要管的那个维度**，同一个晚上犯了三次，每次那个数都是**算
对的**：读了按大小排序的表头当分布（真值中位 0.78 而不是 0.93–0.99）、用端点
比值当增益（中段其实 0.98 不是 0.92）、只看末点就说 `y_true` 是倒 U（t ≤ 2.4 s
其实单调）。三次的完整记录在 `MODEL.md` §5之二和 §6，留着当样本。

⚠ **`docs/recoil/curves/` 里 1184 条旧曲线 2026-08-08 全部删除**，不是归档。
一条在「按弹药计数器分桶」的坐标里拟合、却在「按点击对齐」的网格上播放的曲线，
不是一个可以继续迭代的起点。`detector/weapon.py` 现在读 `curves_time/`，那里没有
曲线的枪就**不压枪**——这是诚实的状态，不是回归。


## 库存行采集不碰模板，也不碰枪（`rows_only`）

`collect_templates.py --targets rows` 现在走一条独立的路：**清空架子和库存 → 刷一个件 → `inv_rows`（纯 Laplacian）确认库存正好一行 → 拍 10 个背景 → 下一个**。身份来自「只刷了一样东西」，跟 `one_part` 是同一条规则，去掉了那把逼出检测器依赖的枪。

**为什么必须独立。** 行采集原来寄生在 `one_part` 里：装上件 → 拆下来 → 拍那一行。于是每一张行图都要过 `SlotDetector`，而它的占用判据是**模板识别**——**要采模板的件恰好是采不了的件**。2026-08-05 那轮 12 个件死了 11 个，一行日志就是整个循环：

```
quick_smg  hit='Magazine_QuickDraw_Medium_C'  mse=192  gate=150  state='empty'
           "still wearing ['magazine'] after the strip"
```

认出来了，但 192 过不了 150 的闸门 → 槽读 `empty` → `strip` 跳过 → 枪上还戴着弹匣 → 新刷的件被顶进库存 → 放弃。这个循环上一次是靠**保留游戏美术图**垫过去的，而美术图已经删了（那是对的，它是合成的输入不是输出）——所以改成让采集路径根本不进那个循环。

两个附带的坑，都已修：

- **`--targets rows` 单独跑必然 0 crops**。`plan_rounds` 只在目标含 `slots` 时返回 `fit=True`，而 `fit=False` 的一轮把**空的** `rows` 列表喂给 `sweep`，什么都拍不到。这条路存在多久就坏了多久。
- **库存清空要验证，不能假设**。`clear_inventory` 的拖拽会不落地，而它不读回；一次没清干净，后面每个件都是「库存里有两行」，身份就认不出来了。现在 `CLEAR_TRIES` 次清空、每次用 `inv_rows` 核对，核不过就跳过这个件而不是采一批说不清是谁的图。

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
