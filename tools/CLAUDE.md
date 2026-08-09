# tools/ — 脚本层

一次性的调查、离线回归、实机探针。**这里没有别人 import 的东西**，所以门槛最低——也因此最容易长出第五份「按键→等→读回」。

**这一层的唯一纪律：先找，再写。** 下面第一张表就是「先找」的清单。

## 跑之前：两样东西是共用的

多个 agent 共用**一个 Pico 串口**和**一个游戏窗口**。

- 标 🎮 的会抢游戏前台，标 🔌 的会占那个串口。**跑前先跟用户说一声等确认**——失焦会让别人正在采的一整轮数据静默归零，而且事后看不出来。
- 先 `from press.pico_mouse import other_agents` 查有没有人在跑，**别杀别人的进程**。
- 标 📄 的随时能跑：不碰游戏、不碰硬件、只读 `docs/` 下的存图。

---

## 第一行永远是这句

```python
from control.session import ensure_ready

if not ensure_ready(label='the pitch probe')['ok']:
    return 1
```

**`ensure_focus` 不够，而且它的不够看起来像够。** 焦点 = 四项里的一项：

| 检查 | 不做会怎样 |
|---|---|
| 焦点 | 鼠标键盘打进当前前台那个窗口 |
| **在局内** | 大厅 / 加载页 / ESC 菜单 / 结算页**全都匹配窗口标题、全都吞按键**，而每个驱动都报成功 |
| **Tab 关着** | `1`/`2` 切枪键被吞（`docs/game_quirks.md`），脚本以为拿了枪，其实空手 |
| **刷新器面板关着** | 面板对世界是模态的：HUD 在、人不动，视角类探针测的是一张静止的屏幕，还会报一个漂亮的零 |

第五样**跟着「在局内」一起来**，不是单独一项：`ensure_in_match` 走进局之后自己把角色传送到 200m 靶道（`range_name='200m'`，传 `None` 关）。出生点是主场地，人多的服上有车穿过，而**被撞掉的弹匣不会自己报告**——轨迹里混进了别人的物理，下游每一道闸照样绿。传送失败会让整个 `ensure_ready` 红，`rec['range']` 说卡在开图/点击/关图哪一步。

这条是 2026-08-04 用一次实机付账买来的：`probe_pitch_range.py` 检查了焦点、拿到 True，然后**对着大厅界面**把三个姿势的状态机整个跑了一遍，三次打印 `posture unreadable`。那句话是真的——那张屏幕上确实没有姿势图标，因为它压根没有 HUD。加上局内判断之后失败**正好前进一步**：在局内了，但手上没枪，因为刚进训练场是空手。

两次都是**已经写好、只是没被调用**的检查。

`ensure_ready` 自己调 `ensure_focus`，所以这是**替换不是追加**。要跳过某一项就传 `match=False` / `tab=False` / `panel=False`——**为了让红的跑绿而跳过，就是在重建上面那个失败**。它不管枪和配件：那是实验的事，走 `control.stock.restock` / `ac.ensure_kit`。

**规则 9 管着**（`pixi run layering`）。判据是「调了 `ensure_focus` 却没调 `ensure_ready`」——不是「import 了 control」，那会误伤半个目录的离线回归（试过，48 个假阳性）。抢前台是驱动游戏唯一诚实的声明。

账本跟规则 6 同构，也是**棘轮**：`READY_EXEMPT` 理由归代码、不过期；`READY_DEBT` 理由归排期、**必须离开这张表**，修好了不销账照样报错。**2026-08-07 起 `READY_DEBT` 是空的**——存量 31 个探针全部还清，最后一个是 `drive_screen.py`，而它挂了一年的那条理由（「`scan_compat` 一轮调 `drive()` 30 次」）**一次 grep 就能证伪**：`scan_compat` import 的是 `SCREENS`，`drive()` 全仓只有一个调用方。**账本里的理由是一个关于代码的断言，而只有「还违不违规」那一半有机器在验。** 新脚本从第一行就受管。

---

## 要做 X，别自己写，用 Y

每一行都是审出来的重复，不是假想的。

| 你想做 | 别自己写 | 用这个 |
|---|---|---|
| 抢游戏焦点 | `SetForegroundWindow` / 按窗口**标题**判游戏 / 后面补一个 `time.sleep(0.6)` | `control.focus.ensure_focus(countdown_s=6)`——**它自己会 settle**；跑到一半用 `focus_keeper().ok('mag 3')` |
| 开/关 Tab | `mouse.key(HID_KEY_TAB, 60)` + `sleep` + 截图 | `InventoryControl.ensure_tab(want)` / `with ac.tab_up():` |
| 开/关刷新器面板 | `mouse.key(HID_KEY_COMMA, 60)` + `sleep` | `SpawnerControl.ensure_panel(want)` / `panel_open()` |
| 判「Tab 开没开」 | 自己数亮像素 / 自己算 luma | `detector.tab_detector.TabTypeDetector` |
| 算刷新器图标包围盒 | `min/max` 一遍 `SPAWNER_ICON_ANCHORS` | `detector.spawner_detector.ICON_BOX`（**规则 7 拦着**） |
| 拿刷新器菜单坐标 | `find_menu(截图)` | `control.spawner.builtin_layout()`——`find_menu` 是兜底不是主路 |
| 刷一堆东西 | 逐个 `give_*` + 自己折叠 | `SpawnerControl.give_many([...])` |
| 想先看它要点哪 | 开着游戏试 | `sc.plan(keys)`，**纯离线**：`python control/spawner.py --plan comp_ar supp_ar` |
| 装/卸配件 | `ac.pointer.drag(src, dst)` | `ac.equip` / `unequip` / `ensure_kit` |
| 扔整把枪 | 先 `strip()` 再扔 | `ac.drop_weapon(n)`——配件跟着走 |
| 打一个弹匣 | `mouse.click(0x01, N)` + 自己 while 轮询 | `FireDriver.fire_magazine()` |
| 换弹 | 按 `HID_KEY_R` + sleep | `FireDriver.top_up()` → `(rounds, reload_s)` |
| 开镜 | `mouse.click(0x02, 60)` + 自己的 `ADS_SETTLE_S` | `GunDriver.ensure_ads()` |
| 转视角 | `rig.mouse.move(yaw, pitch)` | `ViewDriver.turn()`（开环）/ `recenter()`（闭环） |
| 切枪 | `mouse.key(HID_KEY_1, 60)` | `InventoryControl.hold(n)` |
| 确认游戏能被驱动 | `ensure_focus` 就开跑 | `control.session.ensure_ready()`——进程 + 焦点 + 局内 + Tab + 面板，见上一节 |
| 确认在局内 | 自己看像素 | `LobbyControl.ensure_in_match()`——**顺带把人放到 200m 靶道** |
| 刷一把枪并装好镜子 | `give_many` + 自己开合面板 | `sc.ensure_panel(True)` → `sync()` → `give_many` → `finally ensure_panel(False)`，配件走 `restock` + `ac.ensure_kit(n, {'scope': 'red_dot'})`。**漏掉 `ensure_panel(True)` 的话 `collapse_all()` 是对着关着的面板收的，等于没收** |
| 截一张全屏 | `PIL.ImageGrab` / 自己建 bettercam | `capture.cropper.capture_screen()`；要区域用 `win32_cap(box)` / `ScreenBuffer` |
| 截图时避开 hover 高亮 | 自己 `move_cursor` + sleep | `control.spawner.shoot_parked(settle=…)` |
| 读弹药数 | 自己二值化 + 连通域 | `detector.ammo_detector.AmmoDetector`（**`None` 不是 0**） |
| 读枪名 / 配件 | 自己 `matchTemplate` | `detector.tab_items.detect(frame, {1: 'g36c'})`——**能传枪名就传** |
| 判某枪有没有某个槽 | 拖一次看掉不掉 | `detector.slot_detector.SlotDetector`（**占用判据是模板识别，不是边缘计数**；`scope` 位不画 tile，存在性仍不可读） |
| 判某枪能不能装某件 | 抄 wiki | `attachment_catalog.fits(weapon, key)` / `compatible(weapon)` |
| 库存盘点 / 补货 | 自己数行 | `control.stock.restock(ac, sc, want)` |
| 查有没有别的 agent 占 Pico | `tasklist` / 直接开串口 | `press.pico_mouse.other_agents()` |
| 落一次采集的产物 | `mkdir docs/<新名字>/<时间戳>` | `calibration.capture_run.CaptureRun` |
| 复位固件压枪 | `finally: set_recoil_enabled(True)` | 什么都不写——`Rig.close()` 一定会关，那些 `True` 是死语句 |

三条值得单独记，因为它们的失败**看起来像成功**：

- **`ac.pointer.drag` 绕过 `_reject()`**，而那是「往这把枪没有的槽上拖」的唯一拦截。那种拖拽会把配件掉地上，看鼠标跟成功一模一样。
- **按窗口标题判游戏会认错**：仓库叫 `pubg_derecoil`，编辑器或终端显示路径就命中。`control.focus` 匹配 exe 前缀。
- **`1`/`2` 切枪键在 Tab 开着时会被吞**，所以按键必须被 Tab 关/开夹住。`InventoryControl.hold()` 做了这件事。

---

## 什么时候才该新建文件

**一句话判据：我要的答案，已有脚本换个参数能不能打印出来？能 → 加参数。**

三条可判定的：

1. **它能不能喂已存图跑完（不要游戏不要硬件）？**
   能 → 先去找同主题的离线脚本，几乎一定已经有一份。真要新建，**必须同时加 pixi task**——没有 task 的离线脚本没人跑，一定会烂。
2. **它要不要驱动游戏？**
   要 → 先问「这个动作 `control/` 有没有具名入口」。
   - **有** → 脚本只写实验设计，驱动一律调 `control/`。
   - **没有** → **该加的是 `control/` 的方法，不是这里的脚本。** `tools/` 里出现第二份「按键→等→读回」的循环，就是错的。
3. **产物是一次性结论还是反复跑的断言？**
   一次性 → 结论落进代码常量或 `docs/`，脚本顶部写死「结论已进 X，本文件仅供复现」。
   反复跑 → 挂 pixi task。

一律不要新建的情况：

| 你想 | 实际该做 |
|---|---|
| 换一把枪/一个阈值跑同一个实验 | 给已有脚本加 `--weapon` / `--part` 参数 |
| 只想看一眼当前状态 | `control/spawner.py`、`control/inventory.py`、`control/lobby.py`、`control/stock.py`、`calibration/state.py` 都有 CLI |
| 只想存一张截图 | `tools/snap_on_key.py` |
| 只想验证检测器还准不准 | `python tools/regression_check.py --compare` |
| 「先随便试试」 | 写进 `D:\agent-space\`，**不要写进 `tools/`**。那是探索现场；`tools/` 是有人会依赖的东西 |

⚠ **`temp_debug/` 2026-08-08 没有了。** 它当过八个月的探索现场，规矩是「永不删除」，于是长到 52 项，而其中 18 项问的是**已经被删掉的那套坐标**里的东西——EMA、收敛窗、逐发误差地板、`harvest` 的日志。那些不是过时，是**问题本身不再有指称对象**，而「永不删除」这条规矩恰好保证了没人会去分辨这一点。

活着的 36 项归档在 `docs/attic/temp_debug/`，判据一句话：**这个问题，对着今天的代码还问得出来吗**。

⚠ **搬完才发现，四条引用它的注释在我动手之前就已经是悬空的**——`record_human_drag.py`（`control/CLAUDE.md`，34 次真人拖拽的出处）、`verify_tab_slots.py` / `calib_inv_icon.py` / `eval_highlight_jitter.py`（`detector/CLAUDE.md`）。**那四个文件从来不在那 52 项里面。** 归档没有制造这个问题，只是让它露出来了；`pixi run names` 的悬空引用检查故意不管 `temp_debug/`，所以那八个月里没有任何东西在看着它们。

---

## 它该在这一层，还是在 `calibration/`

判据是 `calibration/CLAUDE.md` 第一句的三样（这轮测什么 / 数字怎么算成结论 / 产物怎么落盘），**加上一条硬约束**：

> **规则 6 —— `calibration/` 不 import `press`。而这条要连着 `sys.path` 一起看。**

2026-08-08 搬走四个（2026-08-06 那批同一个判据的续集）：

| 搬去 `calibration/` | 它 `--write` 了什么，或者它是什么的测量 |
|---|---|
| `build_kit_factors` | `data/kit_factors.json`，`detector/weapon_attachments` 每次 import 都读。`pixi run kit-factors` |
| `build_weapon_hud_bank` | `weapon_hud_bank.npz`，跟已经在那边的 `build_name_templates` / `build_lobby_tab_templates` 是同一族 |
| `scan_slot_bleed` | `detector/slot_detector.py:52` 那个数的出处；`calibration/scan_bare_tiles` 本来就在指着它 |
| `probe_hole_pattern` | 弹孔散布——**整条标定链之外唯一的那个测量**，链内的每个数都是拿链自己的输出判的 |

**两组留下了，理由是同一条，值得写死：**

- **`fit_pitch_level` + `probe_pitch_range`。** 它俩是一对（量出俯仰行程 → 存成每格该瞄哪），而 `fit_pitch_level` import `press.pico_mouse.other_agents`——那是「有没有别的 agent 占着 Pico」的守卫，`ensure_ready` **不含**这一项，删掉就是真丢一道闸。搬一半比不搬更糟。
- **`probe_ammo_ocr`。** 它一度搬过去又搬回来了：AST 上它只 import `config` + `detector`，看起来干净，但 `--selftest` 里一句 `sys.path.insert(ROOT/'tools')` + `from collect_ammo_digits import validate`，而 `collect_ammo_digits` import `press`。

> **规则 6 的检查器只解析 import 语句，所以一条 `sys.path` 拼出来的边它看不见。** 这跟下面那条「拿到高层对象再摸它的 HAL 成员查不出来」是同一个洞的另一半：**把一个文件搬进 `calibration/` 之前，先 grep 它自己的 `sys.path.insert`。**

⚠ 顺带一个副作用，它自己就是搬家的理由：`check_params` 扫的是 `control/calibration/harness/press/detector`，**不扫 `tools/`**。`probe_hole_pattern` 一进 `calibration/` 就被咬出一个没人读的 `mag_size`（`fire_magazine()` 自己数弹）。**它在 `tools/` 里躺着的时候没有任何东西在看。**

---

## 探针的判据必须能**否定**——「屏幕变了没有」通常不能

2026-08-05，ADS-after-reload 那个探针（脚本 2026-08-08 删了，教训留着）第一版问的是「点击之后准星分数动了没有」，报出 **16/16 全部生效**。而**换弹动画本身**每帧让那个分数动 **+7**（实测 60.8 → 111.2，前 187 ms，无任何输入）。**在一个持续变化的过程里问「变了没有」，答案恒为是。**

救回来的是**右键 toggle 自己的算术**：四次点击从已知起点出发，全部生效就必然交替（`True,False,True,False`）。实测四个弹匣全是 `False,False,True,False`——**前两次被吃**。分界随后钉到 2.0–2.3 s（0/4 → 3/4 → 4/4），而**生效时只要 102–104 ms**，对着 2.5 s 的 `ADS_WATCH_S`：它从来不短，是在等一个没人成功请求过的状态变化。

同一天同一课的另一面：`tab_open` 判据被一棵树击穿，因为「亮像素够多 + 底够暗」这两条**树同时满足**，而 970 张语料里没有树。换成**饱和度**（字是纯白 0.000，世界最低 0.030）之后两类完全分开。

**写探针之前问一句：我这个判据，在「不该成立」的那一侧长什么样？** 答不上来就还没有判据，只有一个指标。

## 有机器在管

```
pixi run layering        # 15 条规则
pixi run params          # 死参数 + 私有调用点实参个数
pixi run protocol-check  # PC/固件两端的生成物跟 protocol.toml 漂了没有
```

跟 `tools/` 有关的有两条。**规则 9** 是上面那节（`ensure_ready`），它是唯一一条**读调用而不是读 import** 的——import 表达不了「开跑之前先确认游戏能被驱动」。

**规则 7**：除了 `detector/spawner_detector.py`（定义 `ICON_BOX`）和 `tools/test_frames.py`（`anchor_box` 的测试），谁都不许 import `SPAWNER_ICON_*`。

理由是实的：那四个常量唯一的用处就是算那个包围盒，而唯一 import 它们的调用方**把算术手抄了一遍并漏掉 `max(0, ...)` 钳位**。规则实测过会咬人，报文件、行号、符号名和修法。

**规则 10**（2026-08-07）：`calibration/` 的模块只有一个名字 `calibration.X`。裸的 `from sweep import Rig` 会让文件被加载两遍、产生两个 `Rig` 类——实测无人值守夜晚就是这个状态。这条也管 `tools/`，因为一半探针 import calibration。

**规则 15**（2026-08-08）：**占着屏幕的脚本,每个 `while` 循环都必须能被人打断。**

判据:循环的 **test 或 body** 里必须提到时钟(`perf_counter` / `timeout` / `elapsed` / …)**或者**调 `focus_keeper()`。两样都没有 → 红。范围是 `tools/` `calibration/` `harness/`,触发条件是文件调了 `ensure_ready` / `ensure_focus`——**抢前台就是「我要占屏幕」这句声明**(跟规则 9 同一个判据)。

⚠ **它是一次实机付账买来的。** 2026-08-08 晚,`probe_delivery_path.py --hold-sweep` 卡在

```python
prev = None
while prev is None:
    _t, f = grabber.grab_timed()
    prev = rig.tracker.slice_frame(f) if f is not None else None
```

**八分钟**,一边占着前台一边动光标,而且这一段就在按下鼠标键之前。`slice_frame` 只要认不出 patch 就返回 `None`——对着空白天空就够了——**所以退出条件要由「世界」来提供,而循环对等多久没有任何意见。** 只能从另一个会话把进程杀掉。

⚠ **逃生阀本来就存在,那个文件只是从来没调。** `control.focus.focus_keeper().ok(tag)` 在人把前台抢走 `MAX_REGAINS` 次之后返回 `False`,它自己的 docstring 就写着理由:「either something is contending, or a human is trying to get out. **Both mean stop**」。**一个从不发问的循环,没法用「回答」来叫停。**

三个判定,每个都是一次误报买来的:

| | |
|---|---|
| **body 也算,不只是 test** | 第一版只看 test,报了 28 处,其中大半是 `control/` 里 `while True:` 而 body 第一行就是 `if elapsed > timeout: return`——**正确的代码**。读 body 之后降到 9 处,9 处全是真的 |
| **`break` 不算逃生阀** | 九个违规者**一个 break 都没有**,所以今天不损失什么;而认 break 等于把 `if x: break` 当成终止证明,**而 x 恰好就是那个「世界要提供的条件」** |
| **只管脚本层** | `control/` 有几十个 `while True`,它们都在带 timeout 的驱动里,`_await_frame` 本来就查 `focus_keeper`。这条规矩管的是**拥有一次运行**的那一层 |

自证 **6/6,3 条必须咬**。棘轮两个方向都实测过:新写一个占前台+无界循环的脚本 → **当场红**;往 `ESCAPE_DEBT` 里塞一个不违规的文件或一个不存在的文件 → **也红**。存量 8 个在账本里,每次跑都打印。

**规则 14**（2026-08-08）：**一个语句块里不许两次进背包,中间只有纯计算。** 管所有层。判据反着写:**中间只要有任何一个不是内建/容器方法的调用,它就闭嘴**——所以只有「什么都没发生」才报。反过来列一张「必须在背包外做的事」的白名单是会烂的,这张是语言封闭的。

三个判定,每个都是一次假阳性买来的:

| | |
|---|---|
| `ensure_tab(False)` **不是**进入 | `restock` 读完架子关 Tab 去开刷新器,那是相反的动作 |
| 被调函数**自己第一个 tab 事件**决定它算哪类 | `hold` 先 `ensure_tab(False)`(1/2 会被吞)——它要背包**关着**,所以 `ensure_kit` 调完它再开回来是对的,不是 churn |
| 比较**在同一个语句块列表内**,不是按行号 | 这条是仓库自己教的:规则第一版咬了 `if a.kit:` 里的 `tab_up` 和 `if lo is None:` 里的 `read_loadout`——**两条互斥的路,永远不会都跑**。按块比等于免费拿到这个判断,不需要控制流分析 |

解析**只在一个文件内、只跳一跳**。按函数名做传递闭包试过,当场塌掉:`__exit__` → `close` → `ensure_tab` 染了 **711** 个名字,`main` / `read` / `get` 都在里面——**名字跨模块不是身份**。一个文件之内它才是,而那也正好是失效发生的射程(`read_config` 和 `read_sight` 是兄弟,在 `main()` 里被连着调)。

自证 **10/10,其中 3 条必须咬**,每次跑都打印——它没有账本,所以没有别的东西能证明它还会响。端到端也验了:把 `collect_timed` 的两个读函数改回各自开背包,**真实文件上报 2 处**,还原后绿。

规则 6 的欠账账本（`EXEMPT` / `DEBT`）是个**棘轮**：`DEBT` 里的文件如果已经不违规了但没销账，**也报错**。没有这条，账本会烂成永久赦免。往 `EXEMPT` 里加东西必须写清理由，而且理由要归**代码**（装配根、被测对象本身），不能归排期。

`layering` 只解析 import，所以**拿到高层对象再摸它的 HAL 成员查不出来**（`rig.mouse.move(...)`、`ac.pointer.drag(...)`）。这类要靠人看，见上面那张表。

---

## 脚本分组

**常驻工具**

| 脚本 | 干什么 | |
|---|---|---|
| `check_layering.py` (`pixi run layering`) | 分层规则 | 📄 |
| `smoke_check.py` (`pixi run smoke`) | 冷启动：编译全部、建全部检测器、抓帧、找 Pico | 📄 |
| `regression_check.py --compare` | 检测器全量回归，扫 `docs/**/*.png` 里的全屏图 | 📄 |
| `verify_pico.py` (`pixi run verify-pico`) | 固件验收。**每次刷完固件、每次标定之前跑** | 🔌 |
| `snap_on_key.py` | 按键存全屏 + 光标 sidecar，不用 alt-tab | 🎮 |
| `focus_trace.py` | 谁在占前台；`--windows` 列游戏的所有窗口 | 🎮 |

**离线回归**（改完代码就跑）

`analysis` · `abs-offset` · `attachments` · `drag-log` · `fire` · `frames` · `gestures` · `harness` · `highlight` · `kit` · `locations` · `lobby-detector` · `one-gun` · `panel-state` · `placement` · `pointers` · `recenter` · `runs` · `snaps` · `spawner-plan` · `stocktake-test` · `tab-open` · `tab-watch`

⚠ **`pixi run one-gun` 是 2026-08-08 加的,因为「架子上冒出第二把枪」在两天里咬了两次,而第一次只修了代码没配闸。** 它锁两条独立的路:`ensure_weapon_in_hand` 在枪**已经在架上但拿不到手**时必须拒绝(以前落进刷新分支,还把矛盾打印在同一行——`no mp5k in the rack (holds {1: 'mp5k'})`);`collect_timed.read_config` 在架上**有第二把枪**时必须拒绝(它读 1 号槽,扳机打手里那把,两把枪时屏幕上没有东西能证明是同一把)。

16 条用例,其中「空架子必须还能刷」那两条是防「无条件返回 None 也能通过」的。**把修复注掉重跑会红 2 条,验过。**

---

**读数据的探针**（不占游戏窗口,不改任何东西）

| 脚本 | 干什么 |
|---|---|
| `probe_mp5k_cube.py` (`pixi run cube`) | mp5k 2×2×2 八格,**在同一个 t 上**读,并逐格对 `data/kit_factors.json` 那套已退场的弹桶坐标。正交性带 bootstrap CI。隔离掉的样本单列 |

⚠ **`cube` 里那一列 08-05 的旧值不是装饰。** 它是**另一套代码、另一天、另一个坐标系**测的同样八格,所以是这个仓库拿得到的最强的第二独立来源——而它已经赚回本钱了:`grip-vert_grip` 读出 0.482 而旧表说 0.747,差 55%,查下去是**五梭打的是另一把枪**。八格里七格两条线差 ≤3.7%,那种一致性要两边同时朝一个方向错才伪造得出来。

`pointers` 是 2026-08-08 补的,判据一句话:**散文里写的每一条路径,必须指进一个存在的目录。**

那天一次重构把测量产物从 `docs/` 挪进 `calibration/artifacts/`、把签入的资产挪进 `data/`,commit message 写着已经把 skill 重新指过去了,而**四十一条引用还在旧布局上**——其中三条是承重的:`calibrate-recoil` 指着样本库的旧地址;`timing-analyst` 管那个旧地址叫「现在唯一的逐帧原始数据」,而那个 agent 的整套纪律就是读它;还有一条说 `detector/weapon.py` 读一个旧的曲线目录,而它读的是 `config.CURVES_DIR`——**错两遍**。三条的原文在 `tools/check_pointers.py` 的文件头里。

⚠ **那几条死路径故意不在这里写全**,因为这道闸会咬——它第一次跑就咬了本段的初稿。verbatim 的记录归被测者自己(`tools/*.py` 不在扫描范围内),散文只指过去。**一个能咬自己文档的闸,比一个需要人记得绕开它的闸更可信。**

**`layering` / `surface --check` / `params` 全程绿,而且永远会绿:一个写在 Markdown 里的步骤,对每一张 import 图都是隐形的。** 这跟本文件「删探针之前先查有没有代码在引用它」是同一条——`probe_icon_threshold` 的唯一引用方是一个 skill 的步骤。

判据是**父目录**不是文件,因为一半的路径是某一步即将创建的产物(`--shoot baseline`)、另一半带占位符或通配。但**父目录不存在的路径不可能是产物**——没有东西会往一棵没了的树里写。这恰好、且仅仅是那个失效模式,而且可判定。⚠ 它**看不见**的两样写在文件头:指进一个存在但错误的目录,以及移动了的函数/参数/task 名。

`plan-order` 是 2026-08-08 补的,钉的是**一个夜跑为了走完自己的计划要付多少次装配**。每格都把每个受控槽 pin 死(`want_for` 把不填的强制空),所以格与格之间没有依赖,**顺序纯粹是账单**:

```
人手打出来的顺序   bare muzzle grip stock muzzle+grip …      13 次换件
plan_cells 排完    bare muzzle muzzle+grip grip grip+stock … 7 次换件
```

2^3 全因子 **13 → 7,少 46%**,而且 7 是暴力枚举 8! 得到的下界——是最优,不只是更好。

⚠ **判据是那个数,不是那个序列。** 钉住具体排列的话,任何等价好的重排都会红,而代价悄悄翻倍却能绿——两头都反了。所以它算的是「相邻格之间换几个槽」,而下界是**当场暴力算的**,不是从实现里抄来的常量。

⚠ **两条负例是它自己咬出来的**:`parse_config(c) or frozenset()` 把 `None` 吞了,于是「名字不认识就别排」那道闸永远不响,一个垃圾名被当成 `bare` 排进计划;还有按**值**过滤候选(`[c for c in configs if c != start]`)会把重复项一起删掉——**规划器把两格变成一格,不出声**。现在按索引做。

值多少,是量的不是断言的(`journal.jsonl`,2026-08-08):**1115 次打向枪槽的手势换来 789 次落位(1.41 次/件),21% 的装配要重试。** 装配是这个项目最大的单一废跑来源,所以少 46% 的装配就是少 46% 次「抽到那一下没落位」。

⚠ **它不在 `tools/test_harness.py` 里,而那才是第一眼会去找的地方——因为那个文件 import 不进来。** 它跟 `harness/verdict.py` 要 `CLUSTER_MIN` 和 `AGREE_SPREAD_MAX`,而两个都没了(变成 `MAGS_MIN` / `RATE_RESID_MS_MAX`,`n_kept` 变成 `mags_kept`,两臂一致检查换成了弹速检查,还多出第五项 impulse)。**这是既有破损,`pixi run harness` 现在是红的**,而修它要先裁一个谁都不该单方面裁的矛盾:根 `CLAUDE.md` 说 verdict 第 4 项是「不同曲线臂必须给同一个 `y_true`」并且说 impulse 探针已删,而 `verdict.py` 第 4 项**就是** impulse。

`placement` 是 2026-08-08 补的，钉的是「进局要不要传送」那张表（`ensure_in_match` 的 `range_name`）。**十一例里六例是「不该传送」的**，因为一个只验「该传的时候传了」的闸门，在「永远传」下面也全绿——而永远传就是那个操作员点名要停掉的开销（每把枪一次开图/读/关图）。注入的 bug 各自被咬：把跳过分支拿掉 → **第 3b、4、2 三例红**（实测）；把 `entered` 换成 `actions > 0`（代码注释里点名的那个弱写法）→ 第 3、5、5b 三例红。

⚠ **同一天晚些时候规矩缩成了一条子句，而这个闸门是那次变更唯一看得见的地方**：传送 ⟺ 这次调用自己走进了这一局，`_PLACED` 那个进程级标志连同 `placed_at` / `forget_placement` 一起删了。**第 4 例因此翻了向**（already in, fresh process：从「传送」变成「SKIP」），第 5c 和第 8 例**删掉了——不是因为变绿，是因为没有指称对象了**：5c 问的是「already in 那条路重不重试」而那条路不再传送，第 8 例问的是「离开局内会不会清标志」而没有标志。全文在 `control/CLAUDE.md`。

`gestures` 是 2026-08-07 补的棘轮，判据一句话：**重试循环里的手势坐标必须在循环内算，否则写下 `RETRY-SAFE: <为什么这里不会移位>`**。

同一个 bug 类那一晚咬了三次——`ensure_kit` 一次规划全部步骤再顺序执行、`Kit._swap_back` 读一次背包再逐行点、`right_click_equip` 循环外算一次坐标再重试。**三次的表面症状完全相同**（「拖不上去」）**，三次的日志几何字段完全干净**（定位一次到位、落点零偏差），因为手势本来就没问题，移位的是**目标**。最贵的一次把顶下来的原弹匣装回了枪上，然后把那个组合当成不兼容写进了 `kit_facts.json`。

**症状指向错误的方向，所以这条不能靠人看。** 它当场抓到三处，其中 `drag()` 缺的正是行地址那一半守卫（槽位那一半 2026-08-04 就有了）。

`highlight` 是 2026-08-06 补的，理由跟 `lobby-detector` / `stocktake-test` 那次一模一样，只是这次是**量出来的**：全目录查了一遍「除了自己还有谁写过这个名字」，92 个脚本里引用为零的就它一个。它绿着（254/254，26 把枪），而且在每次按键的路径上——**一个没有 task 的离线闸门，跟没有这个闸门的区别只是你以为它在**。

`drag-log` 读的是 `control/inventory.py` 每次**手势**都追加的 `calibration/artifacts/drag/journal.jsonl`（**常开**，几百字节一次）。它把三类候选原因写在同一行里——手势（定位重放次数、抓/放点偏差）、状态（两栏行数 + 轮询序列 / 槽位读回 / 枪名板墨迹）、时序（距上次手势多久）——因为「有时候扔不到地上」是关于**差异**的问题，布尔量答不了。

**2026-08-05 之前只记拖拽，而那是错的一半。** 会赔掉一把枪的是**右键**：打在空槽（或光标漂走了的槽）上会穿到下面的武器行，整枪掉地上——11 轮采集赔了 74 件，一行日志都没留，因为 `right_click_equip` / `right_click_unequip` / `auto_equip` / `drop_weapon` 全都直接调 Pointer。现在六种都写，靠 `kind` 分：

| `kind` | 什么 |
|---|---|
| `drag` | 按下-移动-松手，原来那条 |
| `click` | 右键：装、卸、以及采集器的 `auto_equip` |
| `drop` | 主动把整枪扔出机架 |
| `refused` | 这一层**拒绝发出**的手势 + 是哪道闸拦的。没发生的失败也是证据 |
| `tab` / `hold` | 开关 Tab、切枪——**手势之间发生的事**，只记拖拽的日志永远看不到 |

**汇总第一段现在是「东西走了没有」**（2026-08-08 加），因为它决定后面每一段问的问题成不成立。后面每个桶问的都是「这次拖拽为什么没成」，而这一段问的是**它到底成没成**：

```
did the item leave?  (source row re-read after the drag)
   N  LEFT ANYWAY — 拖成了，判据错了。这里重试打的是移位后的列表
   M  STILL THERE — 真没成
```

靠的是 `src_key_after`：**没报落地时**把源行再读一次。在它之前只能拿下一条记录的 `rows_before` 反推，而 749 条里 **235 条永远判不了**（突发最后一次没有下一条、`moved=None` 时两条的行数都不可用）。⚠ **旧记录不带这个字段，所以它们不出现在这一段里**——那是诚实的状态，不是漏统计：**它们确实无法判定。**

**要 grep 的词是 `gun_lost`。** 右键卸配件之后槽位读回是空的——**枪掉了也是空的**，两者从调用方看一模一样，只有枪名板墨迹（`plate`，前后两个数）能分开。汇总会把这类单独提到最前面。

每行带 `pid` / `proc` / `t`（墙钟）：这个文件是**多个 agent 共用**的，没有 pid 就没法分段，而 `gap_s` 是进程内的 `perf_counter` 差，跨进程读是废话。`--pid` / `--kind` / `--guns` 三个过滤器就是为这个加的。汇总按 landed/missed 对比三类原因、按突发内序号统计（gap > 5 s 断段、**按 pid 分组**），并列出「每次失手前的两个手势」——那正是 `control/CLAUDE.md` 里那条未破案线索缺的一半。

⚠ `tools/drag_log.py` **一个项目内模块都不 import**，是故意的：要调试的那次运行往往正是死在 import 上的。代价是它自带一份 `PLATE_INK_MIN`，由 `pixi run locations` 钉住不许漂。

`attachments` 是 2026-08-03 补的：每一个配件模板 vs 每一张真值裁图，**报 margin 不只报命中**。`--write` 先从配对捕获反解模板再评分，`--holdout` 用「不含该 run 解出的模板」的库去评该 run 的样本——两个数相等才说明模板重建的是图标而不是它自己那批截图。

⚠ **仍然没有 task 的一个**：`regression_check.py`。它不是不该有，是现在会红——baseline 存的是 52 帧而 `docs/` 下现在有 177 帧全屏图，而 `collect()` 里的 `ViewTracker` 跨帧持有 `prev_patches`，所以新帧插进排序序列会改掉每一帧的前序帧。420 条差异**全部**落在 `view_tracker/` 下，其余检测器逐字段一致——这是语料变了，不是行为漂了。要么重存 baseline，要么让 tracker 每帧独立，在那之前它不该进 task 假装绿。

`lobby-detector` 和 `stocktake-test` 是 2026-08-03 补的：它们一直存在且一直是绿的，只是没有入口。**没人跑的测试会烂掉，然后作为「覆盖率」被算进去**——比没有它更糟。

**实机探针**（🎮🔌，跑前必须问用户）

⚠ **2026-08-08 这张表从 30 行砍到 8 行。** 判据在下一节，一句话是「它跟那份已经写下来的记录还差什么」。剩下的都不是「一个问题一个脚本」，而是**测量机器**：跑一次要几十分钟、产出一份别处当事实读的文件、或者答的是一个会随游戏更新变化的量。

| 主题 | 脚本 | 为什么它不是「临时写一个」 |
|---|---|---|
| 后坐力 / 时序 | `probe_shot_latency` `probe_input_latency` | 点击→后坐力、命令→视角动。`press/pico_mouse.py` 的 `L = 38 ms` 出自后者，而固件时序全压在这两个数上 |
| 俯仰 | `probe_pitch_range`（815 行）+ `fit_pitch_level` | 量到夹逼、存成每格该瞄哪；`control/aim.py` / `stock.py` / `harness/adapter.py` 都读它的产物 |
| 姿势 | `probe_posture_trace`（449 行） | 姿势图标什么时候可读，六个视角 4834 样本。`config.py` 三条 `retry_ms` 的出处 |
| Tab / 面板时序 | `probe_toggle_latency`（456 行） | 每块屏开/关到底多久，`control/inventory.py` 和 `detector/spawner_detector.py` 的等待常量都来自它 |
| 背包 | `probe_backpack_depth` | 背包比读得到的 12 行深不深；`control/stock.py` 引两次 |
| 刷新器 | `scrape_spawner`（346 行） | 展开全部 21 个类别拍图 → `calibration/artifacts/spawner/layout.json`。**游戏更新后重量坐标唯一的路** |
| 大厅 | `probe_lobby_transition` | 大厅→局内端到端，全程截图落 `calibration/artifacts/lobby/runs/`；加载页和正式局菜单的语料缺口靠它补 |
| 采集 / 验收 | `collect_ammo_digits`（637 行）· `verify_kit` | 前者是无人值守采 0–9 模板的整套机器（起始值推断 + 交叉验证 + 错位整轮作废），不是一个循环 |

**离线分析**（吃已存图，📄）

`probe_ammo_ocr`（`--confusion` / `--selftest` / `--extract --write` 建模板）· `probe_gun_name_ocr`（`--variants`，中文客户端）· `probe_tab_anchor`（Tab 锚点是语言相关的）· `probe_capture_recovery`（DXGI 抓帧全黑，不需要游戏）

## 删探针之前：**问它跟那份写下来的记录还差什么**

**这一节 2026-08-06 换掉了一张叫「一次性调查」的表**，那张表点名六个脚本「结论已进代码/文档，跑它只是复现历史」，读起来像一张删除清单。当时的反驳是「有两个还在承重」：`probe_icon_threshold` 是 calibrate-template 的一步，`probe_mask_diff` 被 `detector/spawner_layout.py` 指着，**「删掉探针，那个常量就只剩一个数字，没有出处」**。

**2026-08-08 这两个都删了，因为那句话检查一下就是假的。** 出处不在探针里，在 `calibration/artifacts/spawner/README.md`：§3 有整张表（真实展开 513–21180 px、噪声 0–4 px、下限是 `col1_row10` 以及为什么），§4 有三条灰度测量和 0.989–1.000 / 0.000 的分离度。**探针能打印的每一个数都已经在那份 README 里，连它排除了什么都写了。**

> **一个测量的出处是那份记下了它的文档，不是那份能把它重跑一遍的脚本。** 脚本只有在**记录不完整**、或者**要拿新语料重跑**的时候才是出处。两条都不成立，它就只是一份用四十行代码写的重复。

而它们里面有两个**已经跑不起来了**——`temp_debug/` 2026-08-08 归档，`probe_icon_threshold` 和 `probe_button_icons` 的输出目录和 fixture 都在里面。**一个跑不起来的「出处」比没有出处更糟**，因为名字还在，没人会去点开。

同一天删掉的六个，理由分三类：

| 删掉的 | 类 |
|---|---|
| `probe_icon_threshold`（57 行） `probe_button_icons`（99） | 结论全在 `calibration/artifacts/spawner/README.md` §4，**而且 fixture 随 `temp_debug/` 没了** |
| `probe_mask_diff`（60） | 结论全在 `calibration/artifacts/spawner/README.md` §3 |
| `probe_backpack_slot`（47） | 结论是 `control/stock.py` 的 `BACKPACK_DETAIL_MIN`，输出目录也在 `temp_debug/` |
| `probe_lobby_nav`（72） | **它自己的 docstring 就列着替代品**：live 是 `control/lobby.py state\|mode`，离线闸门是 `pixi run lobby-detector` |
| `shoot_lobby`（78） | 唯一的消费者是 `probe_lobby_nav` |

**留下来的小脚本，留的理由必须是上面那两条之一。** 这一轮之后 `tools/` 里几乎没有小脚本了——见下一节。

真正查得准的一条命令，判据是「除了它自己，还有谁写过这个名字」：

```bash
grep -rn "probe_xxx" --include=*.py --include=*.md . ~/.claude/skills
```

2026-08-06 全量跑过一次，结论跟直觉相反：**`tools/` 里被任何东西引用为零的脚本只有一个**（`eval_highlight`，而它自己的 docstring 就在论证该留），其余全被引用着，而且**最强的引用来自代码注释而不是文档**——`press/pico_mouse.py` 引 `probe_input_latency` 存 `L = 38 ms`，`detector/slot_detector.py` 引 `scan_slot_bleed`，`control/stock.py` 引 `probe_backpack_depth` 两次。探针从来不被 import，所以「谁 import 我」对这个目录是个**恒零的量具**；对它们成立的问题是**「哪个常量的出处是我」**。

同一次跑下来实际删掉的四个，理由各不相同，都不是「看起来旧」：

| 删掉的 | 理由 |
|---|---|
| `verify_refactor`（326 行） | 2026-08-02 那次重构的验收，那次重构早就完了；占游戏 + Pico，所以没人会顺手跑 |
| `probe_spawner_layout`（77） | `find_menu` 已降级成兜底，而「趁面板开着拍一张」现在是 `drive_screen.py spawner --shoot` |
| `probe_submenu_thresh`（58） | 结论是 `detector/spawner_layout.py:151` 的 `SUBMENU_THRESH = 215` |
| `probe_submenu_rows`（79） | **两处独立地跑不起来**：默认取 `sorted(glob)[-1]`，而 `_verify_0802` 排在数字后面且没有 baseline；打中文标签时 cp1252 崩。中文客户端之后它就没绿过 |

> 剩下的实机探针**不要拿来当模板抄**——它们的写法反映的是还没有 `control/` 的世界。但那是**别抄**，不是**该删**。

### 2026-08-08 又跑了一次，这次删掉两个，判据各不相同

上面那条 grep 现在**还是那个恒零的量具**，所以这次配了第二条，它才是抓到东西的那条：**这个脚本 `subprocess` 或 `import` 的东西，还在不在磁盘上。**

| 删掉的 | 理由 |
|---|---|
| `expand_kits`（310 行） | 第 272 行起一个 `calibration/harvest.py` 子进程，而 `harvest` **2026-08-08 随弹桶坐标删了**。它整套「按 (weapon, muzzle, grip) 定位格子」的前提是 harvest/harness 的 cell 寻址，那套寻址也没了。**它跑不起来，而且跑起来也没有指称对象** |
| `probe_unequip_where`（159 行） | 跟 `probe_unequip_gesture` **是同一个探针**：同一个问题（右键把件卸到哪）、同样的三个读回（槽位裁图 / 库存行数 / 附近行数）、同一天写的，晚 4 小时。留下的那个有 `--gesture click\|drag`、被 `control/inventory.py` 引着、写在上面那张表里 |

第二个正是本文件第一句要防的那件事——**「先找，再写」**——而它躲过了一次全量引用审计，因为**两份重复实现的引用数都可以是零**。所以那条 grep 判据要补一句：

> **零引用不等于该删，但「零引用 + 有个同名近邻已经在表里」等于该查。**

### 然后同一天第三次，一次删掉 24 个 —— 判据换成了「能不能临时生成」

前两次都在问「还有谁引用它」。**那个问题问错了**，而且上面那张 grep 表自己就说了原因：探针从来不被 import，引用只能来自注释。于是每个探针都有一条注释指着它，每个都「被引用着」，**这个判据从来没否定过任何东西**。

换成的判据只有一句：

> **这个脚本，一个 agent 现在需要它的时候能不能几分钟重写出来？能 → 它不是资产，是一份被存下来的一次性提问。**

对这个目录，答案几乎总是「能」，而且原因写在本文件开头那张表里：**`control/` 现在给每个动作都发了具名入口**（`ensure_tab` / `give_many` / `fire_magazine` / `ensure_ads` / `equip` / `unequip`……）。一个「按键→等→读回」的探针因此就是十几行胶水。**那些具名入口本身，就是让探针变成可丢弃品的东西。**

**而那个测量并没有丢**——查过每一条引用才删的，24 个探针的 19 条代码注释里，**每一条都把数字连同方法一起写下来了**：

```
control/spawner.py     0.30 delivered 5/5, 0.15 delivered 2/5 with ok=True
spawner_layout.py      44.25 px sd 0.43 / 50.70 sd 0.17 / 237.00 sd 0.00
control/inventory.py   still 0.32（噪声底）/ nudge 0.29 / turn 22.78
press/pointer.py       Tab 开着 move(900,0) → 光标漂 450；Tab 关着 → 0
```

删的时候把每条引用改写成**测量而不是文件名**——「谁测的」换成「怎么测的、测出什么」。`pixi run names` 逼着做完这件事：它把 19 条悬空路径全报出来了，一条都漏不掉。

⚠ **有两处不是这样，单独处理了：**

- **`probe_transfer` 问的问题从来没答过。** `control/inventory.py` 和 `locations.py` 的注释写的是「它是**将会**回答这个的探针」。删掉它就是删掉一个没跑过的实验设计——所以那两处改成**把实验本身写出来**：直拖 slot→slot N 次，**两端都读回**，源槽空 **且** 目标槽满（只读源槽分不出「移过去了」和「掉地上了」）。
- **`control/spawner.py` 有一段公开别名**（`click_category` / `click_entry` / `read`）**存在的唯一理由是那两个刷新器探针**。别名留着了，但注释里加了一句：如果没有新的调查来用它，它们就是死重。**一个「为探针存在」的 API，在探针被删之后不会自己消失。**

删完 `tools/` 从 86 个 .py 降到 50，其中 30 个挂着 pixi task。**剩下的 20 个不挂 task 的，留的理由必须能一句话说清**——见上面那两张表的「为什么它不是临时写一个」那一列。

### 第四次，判据落到闸门自己身上：**它有没有能力红**

「挂着 pixi task」是上面用的**代理指标**，不是判据。判据对探针和对闸门给出的答案不一样：

| | 删掉它，损失是什么 |
|---|---|
| 探针 | **零**。数已经写在使用点的注释里，而 `control/` 有具名入口，要重测几分钟就能重搭 |
| 闸门 | **它还在跑这件事**。语料还在 `docs/` 里，但没有东西再拿它比对了 |

而这个仓库最贵的教训是漂移**不报错**，它给一个看起来完全合理的错答案。所以对闸门，「脚本能不能重写」问错了——脚本不是资产，**跑**才是。

能判它死刑的只有一条，而且能实测：**把它的被测对象弄坏，它红不红。**

2026-08-08 对 30 个闸逐个做了变异测试（`literal` 扰动数值字面量 + `branch` 把 `if X` 反成 `if not (X)`，逐个改、逐个跑、逐个还原）。**27 个证明能红**，剩下三个各有各的原因，**只有一个真的该删**：

| | |
|---|---|
| `report_goto_paths` **删了** | `main()` **无条件 `return 0`**。它挂着 task、每次都跑、永远绿，**构造上不可能红**。而它的结论（面板每列各留一个展开，代价取决于方向）早就整段写在 `docs/game_quirks.md` 里。它跟前面删掉的 32 个探针是同一个形状，**只是穿了一件 task 的外衣** |
| `stocktake-test` `kit` | 有 `sys.exit(1)` 机制，只是 30 次变异没打中那条路径。**不是同一回事** |

⚠ **它不是叶子，而这本身是个发现**：`test_spawner_plan.py` **import** 了它的 `classify`——本文件开头写着「这里没有别人 import 的东西」，而那句话有一个反例，就是这个。所以是**搬函数、删壳**：`classify` 进了唯一钉它的地方，报告那层没了。代价写进了 `game_quirks.md`：现在没有任何东西在看那份 live log——**在此之前也没有，只是当时看起来有。**

**这一轮真正的产出不是删掉一个文件，是判据自身连着瞎了三次，每次的沉默都长得像答案：**

| 盲区 | 它把什么伪装成了「没问题」 |
|---|---|
| 拿「有没有 task」当判据 | 一个不可能红的闸门 |
| 只扰动数值常量 | 纯逻辑闸（状态机没有阈值可破）。`snaps` 报 `GREEN through 0 mutation(s)`——**它一次都没变异成功**，换分支反转一击就红 |
| 把 `config.py` 算进被测对象 | 它的 import 期自检一反转，**每个 import config 的闸都死**——五条假红，证明的是「config 坏了它会死」，不是「它在检查自己的被测对象」 |
| 反转 `if __name__ == '__main__'` | 让模块 import 时就跑 `main()`，又是五条假红 |

**判据也需要一个能否定它的东西。** 这跟本文件那条「探针的判据必须能否定」是同一句话，只是这次落在判据自己身上。

### ⚠ 改源码再读行为的东西，会被 `__pycache__` 骗，而且骗得很安静

跑完变异测试之后 `pixi run detect-retry` 从 `6/6` 掉到 `5/6`，而 `git diff control/match.py` 是**空的**，`sed` 打出来的那一行也确实是 `every / 1000.0`。源码没问题，跑出来的行为有问题。

真相在时间戳上：

```
control/__pycache__/match.cpython-312.pyc   11:16:41.442
control/match.py（还原后）                   11:16:41.613     ← 晚 171 ms
```

**Python 的 pyc 失效判据是源文件 mtime，而 pyc 头里只存整秒。** 同一秒内改回去，缓存被判定仍然有效，**变异后的字节码就留在盘上了**——那个 pyc 里 `1000.0` 和变异值 `100007` 同时存在，跑的是后者。

代价不是一个 FAIL，是**每一个后续结论都可能是脏的**：一次「同秒还原」会把污染的字节码留给下一个闸。修法只有一条，写进任何做这类 A/B 的脚本里：

> **改过源码之后，`find . -name __pycache__ -type d -exec rm -rf {} +`，别指望 mtime。**

这条不只管变异测试。**任何「改一个常量 → 跑一遍 → 改回来 → 再跑一遍」的对照实验都吃这个亏**，而且它的症状正是本仓库反复付账的那个形状：**没有报错，只有一个自洽的错数字。**

---

> 落地一条可执行的：**一个闸，如果没人能说出「注入什么会让它红」，它就还没被验过。** 这个仓库里已经有四个闸自带这个答案——`params`（24 例里 9 例必须咬）、`surface-check`（14 例里 8 例必须回 R）、`placement`（拿掉跳过分支 → 第 2 例红）、`kit-factors`（种一行 `derived` 进去验两侧）。**它们是唯一不需要外部变异测试就能自证的。新写闸门照这个写。**

⚠ 另外六个（`probe_recenter` / `probe_kick_profile` / `probe_impulse_align` / `probe_impulse_ab` / `test_analysis` / `test_ema_window`）2026-08-08 已经从工作区消失，但**还挂在 git index 里**——`git status` 报 ` D` / `MD`。它们不在这张表里，因为删它们的是坐标退场那一步，不是这次审计。

---

## 坏了找谁

| 症状 | 先看 |
|---|---|
| 面板打不开 / 点击没反应 | 焦点。`tools/focus_trace.py --raise` |
| 「could not focus the game」但游戏就在屏幕上 | `focus_trace.py --windows`——PUBG 有好几个窗口，只有最大的那个收输入 |
| 刷完固件游戏不见了 | `focus_trace.py --windows`：**有 hwnd** = 只是最小化，`ensure_focus()` 就够；**返回 None** = 进程真没了 |
| 刚刷完固件，敢不敢开始标定 | `pixi run verify-pico`（不占游戏窗口） |
| 压枪偏了但看起来正常 | `pixi run python calibration/fit_time_curve.py --weapon <w>`，读 `dropped[]` 和 `agree_spread`。**先看臂之间对不对得上**——只有一条曲线臂的池子从来没被验过 |
| 弹药数读不出来或读错 | `tools/probe_ammo_ocr.py` + `--confusion`；重建模板 `tools/collect_ammo_digits.py --write` |
| 枪名认不出来（中文客户端） | `tools/probe_gun_name_ocr.py --variants` |
| 配件认不出来或认错 | `pixi run attachments`（全量真值 + margin）；重建模板 `calibration/score_attachments.py --write`，单个 run 看解算质量 `calibration/solve_template.py <run>` |
| 槽位反复报「templates cannot separate」 | 面板半透明，槽位图标合成在世界之上，暗背景会把相邻件的余量压塌——修法是 `_nudge_backdrop` 换个背景再读。⚠ 那段代码 **2026-08-07 之前一直在空转**（Tab 开着发 raw counts 全打在光标上，0.29 对噪声底 0.32）。要再验一次就**必须带一条 `still` 对照臂**，否则只答得出「变了没有」，答不了「到底动没动」。数字在 `control/CLAUDE.md` |
| 一整批 invocation 全报 `[0/N cells landed]` | **先看有没有 Traceback，别看实验。** 多 agent 共用这个仓库，`control/` 里一瞬间的语法错会让每个 invocation 死在 import 上，而干跑闸只数落地格子、区分不出「实验失败」和「代码 import 不进来」。2026-08-07 撞过一次：`control/fire.py` 的一个 docstring 收尾吞掉了整行 `g = cv2.cvtColor(`，等去修的时候另一个 agent 已经修回去了 |
| 某把枪的槽位跟 catalogue 对不上 | `calibration/scan_compat.py`（30 把枪 268 秒）。⚠ **刷出来的枪不是裸枪**——PUBG 会把背包里能装的自动装上，要先 strip |
| 拖拽 / 右键落不下去 | **先分清「报了失败」和「真没落地」**：日志里 `moved` 是三态，`true` 落地 / `false` 验过没动 / **`null` 压根没验证**。2026-08-08 之前 27–43% 的记录是第三种，而其中 **98% 东西已经在地上了**（判据在光标偏 2 px 时跳过读回）。修完是 1%。所以 `moved=null` 不是失败，是**没有答案**。然后才是 `pixi run drag-log` 和那张 2×2（右键 10/10、库存→枪拖拽 0/10——⚠ 那两个数是**方向**的测量，仍然有效）|
| 右键完枪没了 / 一轮采集颗粒无收 | **`pixi run drag-log --guns`**。右键卸配件之后槽位是空的、记录报成功，枪掉了长得一模一样——日志里 `plate` 从 679–901 掉到 0 就是枪走了，汇总会标 ⚠GUN LOST。同时看 `refused` 那几行：哪道闸拦下了同一个手势 |
| 别的 agent 的运行挂了要查 | 同一个 `calibration/artifacts/drag/journal.jsonl`，**常开且带 pid**。`pixi run drag-log --pid <他的pid> --all`。别先去问他打了什么日志——静默失败的运行不会打日志 |
| 扔东西扔不掉 / 库存清不空 | **先 `pixi run drag-log`**——每次手势都记了几何、行数、距上次多久，它会告诉你是「光标没到位」「松手落在原栏」还是「手势干净但游戏没接」，三类要三种修法。⚠ **但先看 `moved` 是不是 `null`**：那是「没验证」不是「失败」，而历史上它占了失败记录的绝大多数（上一行）。悬崖值在 `control/CLAUDE.md`。要重扫手势时长：**扫描循环里绝不能有 `look()`**，那个间隔会掩盖被测变量 |
| 光标 SetCursorPos 之后跑掉了 | **已经答了**，在 `press/pointer.py` 的 `move_cursor` 注释里：Tab 开着时转视角的 raw counts 打在光标上，而且是**陆续到达**的（`move(900,0)` Tab 开着漂 450，Tab 关着漂 0）。那次是三段分别排除「固件在注入」「click 报告带位移」「游戏在松手时归位」测出来的 |
| 「category colN_rowM does not exist」/「would not expand」 | **先看视角朝哪。** 面板半透明，对着天空时读回会在全折叠的面板上报假状态——2026-08-04 修掉了驱动路径上的识别，但 `read()` / `find_menu` 本身仍然如此，诊断用它们时要记得。然后 `pixi run panel-state`。⚠ **光标停在类别行会吃掉那个子菜单的第一项**，所以读回之前先把光标 park 到面板外 |
| 面板坐标要重新量 | `tools/scrape_spawner.py`。**条目几何也是常量**（`SUBMENU_ENTRY_DY/PITCH/CLICK_DX`），2026-08-04 拿 42 张展开图验到 3.1 px 以内；游戏更新后连它一起重量 |
| 游戏更新后面板坐标全错 | `tools/scrape_spawner.py` 重采 → `calibration/artifacts/spawner/layout.json` → `pixi run spawner-plan` 会红出差在哪 |
| 检测器整体还活着吗 | `pixi run smoke`；改完检测器 `python tools/regression_check.py --compare` |
| DXGI 抓帧突然全黑 / 尺寸不对 | `tools/probe_capture_recovery.py`（不需要游戏） |
| Tab 状态跟屏幕不一致 | `pixi run tab-open` / `tab-watch`（都是离线；实机对照那个探针 2026-08-08 删了，它是十几行「开关 Tab、每次读屏对一次」） |
| 分层被破坏 | `pixi run layering` |

## 一个容易认错的东西

`harness/` **不是**给 `tools/` 用的测试脚手架。它是无人值守整夜标定的上层（manifest / verdict / night loop），而且 layering 规则 5 禁止它 import `detector`。`tools/` 的样板不能往那里收，会造出反向依赖。
