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
| **模板构建 / 审计** | `solve_template.py` · `score_attachments.py` · `build_name_templates.py` · `build_lobby_tab_templates.py` · `audit_curves.py` |
| **状态与库存** | `state.py`（只读探针）· `mismatch.py` |

**最后那一行 2026-08-06 从 `tools/` 搬过来，判据是本文件第一句的第三样东西：「产物怎么落盘」。** 它们全都 `--write` 一份检测器当事实读的模板或掩膜，也全都**不碰游戏、不碰硬件**（`press` / `control` import 数为 0，所以搬进来不动规则 6）。留在 `tools/` 的代价是实的：那一层的自我描述是「这里没有别人 import 的东西」，而 `score_attachments` 一直在 import `solve_template`，`scan_compat` / `scan_fits` 至今还 import `tools.drive_screen`——**一个声称没有出边的层长出了出边，就没人再检查它的出边。**

`drive_screen.py` **没有**跟着搬，虽然两个 calibration 模块 import 它。它整份都在做 `ensure_focus` → `ensure_in_match` → 开面板 → 验证，也就是本文件第 5 行禁止的那件事（「一个 `ensure_*` 都不该有」）。按同一条判据它该去 `control/`，不是这里。

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

## `the correlator lost the view` / `view position is no longer known` — 在**倍率镜**上，这句话是假的

**2026-08-05：`build_weapon` 从来不设 `scope`，所以每一轮采集在任何倍率下都在发红点的曲线。**

`Weapon.set_seq` 里 `factor = scope_factor * naked_scale * att_f * posture_f`，而 `scope_factor` 只有 `set('scope', …)` 会写。`build_weapon` 设 name / posture / muzzle / grip——**没有 scope**，于是它恒为 1。

PUBG 按倍率缩放开镜灵敏度：4x 下一个 count 转的角度约 1/4，抵消同样的角位移要约 4 倍 counts。所以：

| aug bare，42 发 | 发出去的曲线 | 实测真值 | 残差 |
|---|---|---|---|
| 红点 | 1741 | 1812 | **+4%** |
| **4x** | 1741（**该是 6964**） | 6347 | **+265%** |

**症状伪装成了别的东西**，这是它藏得住的原因：补偿只有 1/4 → 残差 +265% → 一梭把视角推 **2692 counts** → 参考图块（容量 **68**）wrap → 报出来是「相关器丢了视角」「俯仰带扫不出来」。**看起来像检测器坏了，其实是补偿发小了。**

⚠ **vss 是同一件事，不是特例。** 它自带的 PSO-1 在 `_SCOPE_TO_MAG` 里就是 4x。它的曲线前 22 发 324、实测 1058，比值 **3.26**。2026-08-05 为它试了 **8 次**，否掉 **7 个**解释，还写了一整节「它的曲线是外部导入从没拟合过」——**那节是错的**。被否掉的七个（别再走）：3-patch 剖面、`horizon_row()` 坏了、少传 `--home`、「昨天 red_dot 能跑」、视角朝向没纹理、开镜时不可跟踪、`pitch_range.json` 被清空。

**这也是「scope 轴是四个轴里唯一 0 次测量」的真正原因**：不是没人测，是**一测就死，而死法看起来像别的问题**。

修法取**读回来的**镜（`att['scope']`）而不是 `--sight` 请求的——装配会静默失败，而补偿要匹配枪上真有的东西。红点档不受影响（factor 1.0，`curve_sum` 一字未变）。

## 打在俯仰限位上的弹匣会流进 EMA，把曲线推到负值

**EMA 本身不是不稳的那一环。** 它是 `alpha = 1/(k+1)` 的 running mean，带 floor、`PRIOR_MAGS=5` 的先验、`ALPHA_MAX=0.5`——对 vss 实际只用 0.167。发散的是**喂进去的东西**：

```
1. 一格漂出参考范围 → reaim 失败 → tracking_lost → 正确弃格 ✓
2. 下一轮 harvest 是新进程 → tracking_lost 复位
   set_reference() 在视角当前所在处取基准 —— 而那是限位上
3. mag 0 打在限位上，视角不动 → 读出 32.1 counts / 22 发（真值 ~1058）
4. --apply 照单全收 → 下一轮残差 −85.7%
```

`tracking_lost` 守的是 **1..n 号弹匣，0 号从来没人守**，而 `tracking_confirmed()`（推一个已知量、看读数跟不跟）早就存在、用在 `recenter` 内部和两个探针里，**就是没用在取格起点**。现在 `harvest` 和 `sweep` 在打第一梭前都验一次。

兜底在 `analysis.magazine_fault`：隐含后坐力（曲线+残差 = 真实后坐力，是个物理量）**每发不得低于 `IMPLIED_PER_BULLET_MIN`**。⚠ 这个下限第一次设成 5.0 被 `pixi run analysis` 拒收——它是从四把枪外推的，**一把 LMG 都没有**，而 m249 每发 4.7、mg3 2.5–2.7。现改 2.0，余量薄（2.5 对 1.5），**是兜底不是判据**。

`docs/recoil/curves/vss_att.0802_BROKEN_negative.bak.json` 是 8 月 2 日同一个循环跑到 −307 留下的。**同一个坑踩了两次**，第二次的证据就躺在第一次的备份文件名里。

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
