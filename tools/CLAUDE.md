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

这条是 2026-08-04 用一次实机付账买来的：`probe_pitch_range.py` 检查了焦点、拿到 True，然后**对着大厅界面**把三个姿势的状态机整个跑了一遍，三次打印 `posture unreadable`。那句话是真的——那张屏幕上确实没有姿势图标，因为它压根没有 HUD。加上局内判断之后失败**正好前进一步**：在局内了，但手上没枪，因为刚进训练场是空手。

两次都是**已经写好、只是没被调用**的检查。

`ensure_ready` 自己调 `ensure_focus`，所以这是**替换不是追加**。要跳过某一项就传 `match=False` / `tab=False` / `panel=False`——**为了让红的跑绿而跳过，就是在重建上面那个失败**。它不管枪和配件：那是实验的事，走 `control.stock.restock` / `ac.ensure_kit`。

**规则 9 管着**（`pixi run layering`）。判据是「调了 `ensure_focus` 却没调 `ensure_ready`」——不是「import 了 control」，那会误伤半个目录的离线回归（试过，48 个假阳性）。抢前台是驱动游戏唯一诚实的声明。

账本跟规则 6 同构，也是**棘轮**：`READY_EXEMPT` 理由归代码、不过期；`READY_DEBT` 理由归排期、**必须离开这张表**，修好了不销账照样报错。存量 30 个探针挂在 DEBT 上，每次绿跑都会把剩余条数打出来。**新脚本从第一行就受管。**

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
| 确认游戏能被驱动 | `ensure_focus` 就开跑 | `control.session.ensure_ready()`——焦点 + 局内 + Tab + 面板，见上一节 |
| 确认在局内 | 自己看像素 | `LobbyControl.ensure_in_match()` |
| 刷一把枪并装好镜子 | `give_many` + 自己开合面板 | `sc.ensure_panel(True)` → `sync()` → `give_many` → `finally ensure_panel(False)`，配件走 `restock` + `ac.ensure_kit(n, {'scope': 'red_dot'})`。**漏掉 `ensure_panel(True)` 的话 `collapse_all()` 是对着关着的面板收的，等于没收** |
| 截一张全屏 | `PIL.ImageGrab` / 自己建 bettercam | `detector.cropper.capture_screen()`；要区域用 `win32_cap(box)` / `ScreenBuffer` |
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
| 「先随便试试」 | 写进 `temp_debug/`，**不要写进 `tools/`**。那里是探索现场且**永不删除**；`tools/` 是有人会依赖的东西 |

`temp_debug/` 里有跟 `tools/` 同名的旧副本（`verify_lobby_detector.py`、`probe_spawner_layout.py`）。**同名一律以 `tools/` 为准**——temp_debug 那份 verify 只有 3 个 CASE 且会打印一份过期的缺口清单。

---

## 探针的判据必须能**否定**——「屏幕变了没有」通常不能

2026-08-05，`probe_ads_after_reload.py` 第一版问的是「点击之后准星分数动了没有」，报出 **16/16 全部生效**。而**换弹动画本身**每帧让那个分数动 **+7**（实测 60.8 → 111.2，前 187 ms，无任何输入）。**在一个持续变化的过程里问「变了没有」，答案恒为是。**

救回来的是**右键 toggle 自己的算术**：四次点击从已知起点出发，全部生效就必然交替（`True,False,True,False`）。实测四个弹匣全是 `False,False,True,False`——**前两次被吃**。分界随后钉到 2.0–2.3 s（0/4 → 3/4 → 4/4），而**生效时只要 102–104 ms**，对着 2.5 s 的 `ADS_WATCH_S`：它从来不短，是在等一个没人成功请求过的状态变化。

同一天同一课的另一面：`tab_open` 判据被一棵树击穿，因为「亮像素够多 + 底够暗」这两条**树同时满足**，而 970 张语料里没有树。换成**饱和度**（字是纯白 0.000，世界最低 0.030）之后两类完全分开。

**写探针之前问一句：我这个判据，在「不该成立」的那一侧长什么样？** 答不上来就还没有判据，只有一个指标。

## 有机器在管

```
pixi run layering        # 9 条规则
```

跟 `tools/` 有关的有两条。**规则 9** 是上面那节（`ensure_ready`），它是唯一一条**读调用而不是读 import** 的——import 表达不了「开跑之前先确认游戏能被驱动」。

**规则 7**：除了 `detector/spawner_detector.py`（定义 `ICON_BOX`）和 `tools/test_frames.py`（`anchor_box` 的测试），谁都不许 import `SPAWNER_ICON_*`。

理由是实的：那四个常量唯一的用处就是算那个包围盒，而唯一 import 它们的调用方**把算术手抄了一遍并漏掉 `max(0, ...)` 钳位**。规则实测过会咬人，报文件、行号、符号名和修法。

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

`analysis` · `abs-offset` · `attachments` · `drag-log` · `fire` · `frames` · `harness` · `kit` · `locations` · `lobby-detector` · `panel-state` · `recenter` · `runs` · `snaps` · `spawner-plan` · `stocktake-test` · `tab-open` · `tab-watch`

`drag-log` 读的是 `control/inventory.py` 每次**手势**都追加的 `docs/drag/journal.jsonl`（**常开**，几百字节一次）。它把三类候选原因写在同一行里——手势（定位重放次数、抓/放点偏差）、状态（两栏行数 + 轮询序列 / 槽位读回 / 枪名板墨迹）、时序（距上次手势多久）——因为「有时候扔不到地上」是关于**差异**的问题，布尔量答不了。

**2026-08-05 之前只记拖拽，而那是错的一半。** 会赔掉一把枪的是**右键**：打在空槽（或光标漂走了的槽）上会穿到下面的武器行，整枪掉地上——11 轮采集赔了 74 件，一行日志都没留，因为 `right_click_equip` / `right_click_unequip` / `auto_equip` / `drop_weapon` 全都直接调 Pointer。现在六种都写，靠 `kind` 分：

| `kind` | 什么 |
|---|---|
| `drag` | 按下-移动-松手，原来那条 |
| `click` | 右键：装、卸、以及采集器的 `auto_equip` |
| `drop` | 主动把整枪扔出机架 |
| `refused` | 这一层**拒绝发出**的手势 + 是哪道闸拦的。没发生的失败也是证据 |
| `tab` / `hold` | 开关 Tab、切枪——**手势之间发生的事**，只记拖拽的日志永远看不到 |

**要 grep 的词是 `gun_lost`。** 右键卸配件之后槽位读回是空的——**枪掉了也是空的**，两者从调用方看一模一样，只有枪名板墨迹（`plate`，前后两个数）能分开。汇总会把这类单独提到最前面。

每行带 `pid` / `proc` / `t`（墙钟）：这个文件是**多个 agent 共用**的，没有 pid 就没法分段，而 `gap_s` 是进程内的 `perf_counter` 差，跨进程读是废话。`--pid` / `--kind` / `--guns` 三个过滤器就是为这个加的。汇总按 landed/missed 对比三类原因、按突发内序号统计（gap > 5 s 断段、**按 pid 分组**），并列出「每次失手前的两个手势」——那正是 `control/CLAUDE.md` 里那条未破案线索缺的一半。

⚠ `tools/drag_log.py` **一个项目内模块都不 import**，是故意的：要调试的那次运行往往正是死在 import 上的。代价是它自带一份 `PLATE_INK_MIN`，由 `pixi run locations` 钉住不许漂。

`attachments` 是 2026-08-03 补的：每一个配件模板 vs 每一张真值裁图，**报 margin 不只报命中**。`--write` 先从配对捕获反解模板再评分，`--holdout` 用「不含该 run 解出的模板」的库去评该 run 的样本——两个数相等才说明模板重建的是图标而不是它自己那批截图。

⚠ **仍然没有 task 的一个**：`regression_check.py`。它不是不该有，是现在会红——baseline 存的是 52 帧而 `docs/` 下现在有 177 帧全屏图，而 `collect()` 里的 `ViewTracker` 跨帧持有 `prev_patches`，所以新帧插进排序序列会改掉每一帧的前序帧。420 条差异**全部**落在 `view_tracker/` 下，其余检测器逐字段一致——这是语料变了，不是行为漂了。要么重存 baseline，要么让 tracker 每帧独立，在那之前它不该进 task 假装绿。

`lobby-detector` 和 `stocktake-test` 是 2026-08-03 补的：它们一直存在且一直是绿的，只是没有入口。**没人跑的测试会烂掉，然后作为「覆盖率」被算进去**——比没有它更糟。

**实机探针**（🎮🔌，跑前必须问用户）

| 主题 | 脚本 |
|---|---|
| 后坐力 / 时序 | `probe_recenter` `probe_kick_profile` `probe_impulse_align` `probe_impulse_ab` `probe_shot_latency` `probe_input_latency` `probe_pitch_range` `probe_ammo_during_fire` |
| Tab / 配件 | `probe_click_speed` `probe_drag_speed`（`--panel` 测面板到面板那条）`probe_human_drag`（录真人拖拽，不碰鼠标） `probe_drag_cursor`（光标为什么不待在放好的位置；A/B 段会按住左键） `probe_equip_gesture` `probe_unequip_gesture` `probe_drop_weapon` `probe_gun_grab` `probe_rack_cycle` `probe_slot_boxes` `probe_tab_watch_live` `probe_toggle_latency` `probe_transfer` |
| 刷新器 | `probe_spawn_wait` `probe_spawner_layers` `probe_submenu_hover` `scrape_spawner` |
| 大厅 | `probe_lobby_transition` |
| 采集 / 验收 | `collect_ammo_digits` · `verify_kit` |

**离线分析**（吃已存图，📄）

`probe_ammo_ocr`（`--confusion` / `--selftest`）· `probe_gun_name_ocr`（`--variants`）· `probe_tab_anchor` · `probe_backpack_slot` · `probe_button_icons` · `probe_icon_threshold` · `probe_mask_diff` · `probe_panel_state` · `probe_lobby_nav` · `probe_capture_recovery`

## 删探针之前：先查有没有代码在引用它

**这一节 2026-08-06 换掉了一张叫「一次性调查」的表**，那张表点名六个脚本「结论已进代码/文档，跑它只是复现历史」，读起来像一张删除清单。照着删会删掉两个**还在承重**的：

- `probe_icon_threshold` 是 `.claude/skills/calibrate-template` 的一个步骤。**技能里写的步骤在任何 import 图里都看不见**，和 `slot_window` 那次同一个形状。
- `probe_mask_diff` 被 `detector/spawner_layout.py:159` 用一句 `See tools/probe_mask_diff.py` 指着，那行上面就是它测出来的 ~75。**删掉探针，那个常量就只剩一个数字，没有出处。**

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

---

## 坏了找谁

| 症状 | 先看 |
|---|---|
| 面板打不开 / 点击没反应 | 焦点。`tools/focus_trace.py --raise` |
| 「could not focus the game」但游戏就在屏幕上 | `focus_trace.py --windows`——PUBG 有好几个窗口，只有最大的那个收输入 |
| 刷完固件游戏不见了 | `focus_trace.py --windows`：**有 hwnd** = 只是最小化，`ensure_focus()` 就够；**返回 None** = 进程真没了 |
| 刚刷完固件，敢不敢开始标定 | `pixi run verify-pico`（不占游戏窗口） |
| 压枪偏了但残差看起来正常 | `pixi run impulse-ab` / `tools/probe_impulse_align.py` |
| 弹药数读不出来或读错 | `tools/probe_ammo_ocr.py` + `--confusion`；重建模板 `tools/collect_ammo_digits.py --write` |
| 枪名认不出来（中文客户端） | `tools/probe_gun_name_ocr.py --variants` |
| 配件认不出来或认错 | `pixi run attachments`（全量真值 + margin）；重建模板 `calibration/score_attachments.py --write`，单个 run 看解算质量 `calibration/solve_template.py <run>` |
| 某把枪的槽位跟 catalogue 对不上 | `tools/probe_slot_boxes.py <weapon> --strip`（**刷出来的枪不是裸枪**） |
| 拖拽 / 右键落不下去 | `tools/probe_equip_gesture.py` / `probe_unequip_gesture.py`——两个都读回验证，不看鼠标 |
| 右键完枪没了 / 一轮采集颗粒无收 | **`pixi run drag-log --guns`**。右键卸配件之后槽位是空的、记录报成功，枪掉了长得一模一样——日志里 `plate` 从 679–901 掉到 0 就是枪走了，汇总会标 ⚠GUN LOST。同时看 `refused` 那几行：哪道闸拦下了同一个手势 |
| 别的 agent 的运行挂了要查 | 同一个 `docs/drag/journal.jsonl`，**常开且带 pid**。`pixi run drag-log --pid <他的pid> --all`。别先去问他打了什么日志——静默失败的运行不会打日志 |
| 扔东西扔不掉 / 库存清不空 | **先 `pixi run drag-log`**——每次手势都记了几何、行数、距上次多久，它会告诉你是「光标没到位」「松手落在原栏」还是「手势干净但游戏没接」，三类要三种修法。然后 `probe_drag_speed.py --panel`（**扫描循环里绝不能有 `look()`**，间隔会掩盖被测变量）；跟真人比对用 `probe_human_drag.py`。悬崖值和那条固件线索都在 `control/CLAUDE.md` |
| 光标 SetCursorPos 之后跑掉了 | `tools/probe_drag_cursor.py`——三段分别排除「固件在注入」「click 报告带位移」「游戏在松手时归位」。答案是第三类的变种：Tab 开着时转视角的 raw counts 打在光标上 |
| 「category colN_rowM does not exist」/「would not expand」 | **先看视角朝哪。** 面板半透明，对着天空时读回会在全折叠的面板上报假状态——2026-08-04 修掉了驱动路径上的识别，但 `read()` / `find_menu` 本身仍然如此，诊断用它们时要记得。然后 `pixi run panel-state`、`probe_submenu_hover.py`（光标停在类别行会吃掉第一项） |
| 面板坐标要重新量 | `tools/scrape_spawner.py`。**条目几何也是常量**（`SUBMENU_ENTRY_DY/PITCH/CLICK_DX`），2026-08-04 拿 42 张展开图验到 3.1 px 以内；游戏更新后连它一起重量 |
| 游戏更新后面板坐标全错 | `tools/scrape_spawner.py` 重采 → `docs/spawner/layout.json` → `pixi run spawner-plan` 会红出差在哪 |
| 检测器整体还活着吗 | `pixi run smoke`；改完检测器 `python tools/regression_check.py --compare` |
| DXGI 抓帧突然全黑 / 尺寸不对 | `tools/probe_capture_recovery.py`（不需要游戏） |
| Tab 状态跟屏幕不一致 | 离线 `pixi run tab-open` / `tab-watch`；实机 `tools/probe_tab_watch_live.py` |
| 分层被破坏 | `pixi run layering` |

## 一个容易认错的东西

`harness/` **不是**给 `tools/` 用的测试脚手架。它是无人值守整夜标定的上层（manifest / verdict / night loop），而且 layering 规则 5 禁止它 import `detector`。`tools/` 的样板不能往那里收，会造出反向依赖。
