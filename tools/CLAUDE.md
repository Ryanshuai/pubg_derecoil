# tools/ — 脚本层

一次性的调查、离线回归、实机探针。**这里没有别人 import 的东西**，所以门槛最低——也因此最容易长出第五份「按键→等→读回」。

**这一层的唯一纪律：先找，再写。** 下面第一张表就是「先找」的清单。

## 跑之前：两样东西是共用的

多个 agent 共用**一个 Pico 串口**和**一个游戏窗口**。

- 标 🎮 的会抢游戏前台，标 🔌 的会占那个串口。**跑前先跟用户说一声等确认**——失焦会让别人正在采的一整轮数据静默归零，而且事后看不出来。
- 先 `from press.pico_mouse import other_agents` 查有没有人在跑，**别杀别人的进程**。
- 标 📄 的随时能跑：不碰游戏、不碰硬件、只读 `docs/` 下的存图。

---

## 要做 X，别自己写，用 Y

每一行都是审出来的重复，不是假想的。

| 你想做 | 别自己写 | 用这个 |
|---|---|---|
| 抢游戏焦点 | `SetForegroundWindow` / 按窗口**标题**判游戏 | `control.focus.ensure_focus(countdown_s=6)`，之后 `time.sleep(0.6)`；跑到一半用 `focus_keeper().ok('mag 3')` |
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
| 确认在局内 | 自己看像素 | `LobbyControl.ensure_in_match()` |
| 截一张全屏 | `PIL.ImageGrab` / 自己建 bettercam | `detector.cropper.capture_screen()`；要区域用 `win32_cap(box)` / `ScreenBuffer` |
| 截图时避开 hover 高亮 | 自己 `move_cursor` + sleep | `control.spawner.shoot_parked(settle=…)` |
| 读弹药数 | 自己二值化 + 连通域 | `detector.ammo_detector.AmmoDetector`（**`None` 不是 0**） |
| 读枪名 / 配件 | 自己 `matchTemplate` | `detector.tab_items.detect(frame, {1: 'g36c'})`——**能传枪名就传** |
| 判某枪有没有某个槽 | 拖一次看掉不掉 | `detector.slot_detector.SlotDetector`（`scope` 恒 `unknown`，别塌成 `absent`） |
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

## 有机器在管

```
pixi run layering        # 8 条规则，只解析 import，不跑任何东西
```

跟 `tools/` 有关的是**规则 7**：除了 `detector/spawner_detector.py`（定义 `ICON_BOX`）和 `tools/test_frames.py`（`anchor_box` 的测试），谁都不许 import `SPAWNER_ICON_*`。

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
| `audit_curves.py` | 每条曲线 vs 它最老的备份 | 📄 |

**离线回归**（改完代码就跑）

`analysis` · `abs-offset` · `fire` · `frames` · `harness` · `kit` · `locations` · `lobby-detector` · `panel-state` · `recenter` · `runs` · `snaps` · `spawner-plan` · `stocktake-test` · `tab-open` · `tab-watch`

⚠ **仍然没有 task 的一个**：`regression_check.py`。它不是不该有，是现在会红——baseline 存的是 52 帧而 `docs/` 下现在有 177 帧全屏图，而 `collect()` 里的 `ViewTracker` 跨帧持有 `prev_patches`，所以新帧插进排序序列会改掉每一帧的前序帧。420 条差异**全部**落在 `view_tracker/` 下，其余检测器逐字段一致——这是语料变了，不是行为漂了。要么重存 baseline，要么让 tracker 每帧独立，在那之前它不该进 task 假装绿。

`lobby-detector` 和 `stocktake-test` 是 2026-08-03 补的：它们一直存在且一直是绿的，只是没有入口。**没人跑的测试会烂掉，然后作为「覆盖率」被算进去**——比没有它更糟。

**实机探针**（🎮🔌，跑前必须问用户）

| 主题 | 脚本 |
|---|---|
| 后坐力 / 时序 | `probe_recenter` `probe_kick_profile` `probe_impulse_align` `probe_impulse_ab` `probe_shot_latency` `probe_input_latency` `probe_pitch_range` `probe_ammo_during_fire` |
| Tab / 配件 | `probe_click_speed` `probe_drag_speed` `probe_equip_gesture` `probe_unequip_gesture` `probe_drop_weapon` `probe_gun_grab` `probe_rack_cycle` `probe_slot_boxes` `probe_tab_watch_live` `probe_toggle_latency` `probe_transfer` |
| 刷新器 | `probe_spawn_wait` `probe_spawner_layers` `probe_spawner_layout` `probe_submenu_hover` `scrape_spawner` |
| 大厅 | `probe_lobby_transition` |
| 采集 / 验收 | `collect_ammo_digits` · `verify_kit` · `verify_refactor` |

**离线分析**（吃已存图，📄）

`probe_ammo_ocr`（`--confusion` / `--selftest`）· `probe_gun_name_ocr`（`--variants`）· `probe_tab_anchor` · `probe_backpack_slot` · `probe_button_icons` · `probe_icon_threshold` · `probe_mask_diff` · `probe_panel_state` · `probe_submenu_rows` · `probe_submenu_thresh` · `probe_lobby_nav` · `probe_capture_recovery`

**一次性调查**（结论已进代码/文档，跑它只是复现历史）

`probe_spawner_layout`（`find_menu` 已降级成兜底）· `probe_submenu_thresh` / `probe_submenu_rows` / `probe_mask_diff` / `probe_icon_threshold`（阈值已固化成常量）· `verify_refactor`（2026-08-02 那次重构的验收）

> 这一组**不要拿来当模板抄**。它们的写法反映的是还没有 `control/` 的世界。

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
| 某把枪的槽位跟 catalogue 对不上 | `tools/probe_slot_boxes.py <weapon> --strip`（**刷出来的枪不是裸枪**） |
| 拖拽 / 右键落不下去 | `tools/probe_equip_gesture.py` / `probe_unequip_gesture.py`——两个都读回验证，不看鼠标 |
| 「category colN_rowM does not exist」 | `pixi run panel-state`；再看 `probe_submenu_hover.py`（光标停在类别行会吃掉第一项） |
| 游戏更新后面板坐标全错 | `tools/scrape_spawner.py` 重采 → `docs/spawner/layout.json` → `pixi run spawner-plan` 会红出差在哪 |
| 检测器整体还活着吗 | `pixi run smoke`；改完检测器 `python tools/regression_check.py --compare` |
| DXGI 抓帧突然全黑 / 尺寸不对 | `tools/probe_capture_recovery.py`（不需要游戏） |
| Tab 状态跟屏幕不一致 | 离线 `pixi run tab-open` / `tab-watch`；实机 `tools/probe_tab_watch_live.py` |
| 分层被破坏 | `pixi run layering` |

## 一个容易认错的东西

`harness/` **不是**给 `tools/` 用的测试脚手架。它是无人值守整夜标定的上层（manifest / verdict / night loop），而且 layering 规则 5 禁止它 import `detector`。`tools/` 的样板不能往那里收，会造出反向依赖。
