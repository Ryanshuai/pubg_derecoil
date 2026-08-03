# control/ — 驱动层

驱动游戏。每个动作都是 **observe → act → verify** 的闭环：读屏幕拿当前状态，发动作，再读回来确认落地了。

**发出去的动作不算数，读回来的才算。** 这是这一层唯一的铁律，下面所有东西都是它的推论。

## 三层分工

| 层 | 职责 | 一句话判据 |
|---|---|---|
| `detector/` | frame → 语义 | 喂一张 PNG、没游戏没硬件能跑完吗？能就放这 |
| `press/` | HAL，只知道设备 | 需要知道「游戏里正在发生什么」吗？不需要就放这 |
| `control/` | 闭环驱动 | 需要。放这 |

依赖单向：`control → detector`、`control → press`。反过来一律不行。这条有 lint 管着：

```
pixi run layering      # 只解析 import，不跑任何东西
```

## 模块地图

| 模块 | 干什么 | 入口 |
|---|---|---|
| `focus.py` | 抢并保持游戏前台 | `ensure_focus()` / `focus_keeper()` |
| `lobby.py` | 大厅 ↔ 局内转移 | `LobbyControl.ensure_in_match()` |
| `spawner.py` | 训练场刷新器面板，刷枪/配件/装备 | `SpawnerControl.give()` |
| `match.py` | 对局内实时回路（按键→状态→硬件） | `Dispatcher`，由 `robot.py` 装配 |
| `tab_watch.py` | Tab 界面：开没开、枪上装了什么（**只读**） | `TabWatch`，由 `Dispatcher` 持有 |
| `inventory.py` | Tab 界面：拖配件、装卸、扔枪（**驱动**） | `InventoryControl` |
| `stock.py` | 背包盘点：看有什么、多的扔掉、缺的刷出来 | `restock(ac, sc, want)` |
| `aim.py` | 视角指哪、怎么闭环弄回去 | `ViewDriver` |
| `gun.py` | 把角色推到测量假设的状态并验证（开镜/姿势/火力模式） | `GunDriver` |
| `fire.py` | 打一个弹匣，把游戏说的话带回来 | `FireDriver` |

`tab_watch` 和 `inventory` 都是 Tab 界面，分工是**读 vs 写**：前者归实时回路，每 tick 被动观察，从不发指令；后者是标定/自动化主动去改东西。别把 `inventory` 塞进 `Dispatcher`——它会发拖拽，实时回路里没人想要这个。

---

## 先决条件：焦点，每次都要

**驱动任何东西之前先 `ensure_focus()`。** 游戏不在前台时驱动鼠标，等于往当前前台的窗口里乱点乱打。

```python
from control.focus import ensure_focus, focus_keeper

if not ensure_focus(countdown_s=6):    # 抢→验→重试3次→倒计时兜底
    return 1
time.sleep(0.6)                        # 切前台后头几帧游戏不收输入
...
if not focus_keeper().ok('mag 3'):     # 跑到一半掉了就抢回来，上限 5 次
    break
```

三个坑：

- **裸 `SetForegroundWindow` 会被拒。** Windows 只允许当前前台进程交出焦点。`raise_game()` 用 `AttachThreadInput` 借前台线程的输入队列绕过去；不模拟 ALT 键，因为 ALT 在这游戏里是自由视角。
- **调完必须再验一次 `game_focused()`。** 它可以不报错地失败——曾经有个 run 第一行打印 `focused=True`，然后打不开刷新器面板。
- **焦点不是拿到一次就固定的。** 终端会反复抢回去。**关键操作要每次重试前重新抢**，别只在开头抢一次。

**最小化不用管，`raise_game()` 自己会还原。** 刷 Pico 固件会把游戏丢回桌面——但游戏没退，只是最小化了，就是任务栏那个头盔图标。`raise_game()` 里 `IsIconic` → `SW_RESTORE` 干的正是点那个图标的事，实测 `minimised=True focused=False` → `minimised=False focused=True`（2026-08-03）。所以刷完固件直接 `ensure_focus()` 就行，不需要人点。

**分得开的两种情况**，别猜：

| `game_hwnd()` | 意思 | 怎么办 |
|---|---|---|
| 返回 hwnd，`IsIconic` 为真 | 最小化，进程活着 | `raise_game()`，够了 |
| 返回 `None` | 进程真没了 | 抢焦点帮不上，得重开游戏 |

窗口回来之后游戏在哪个界面是另一回事——刷完固件常常掉回大厅，接着走 `LobbyControl.ensure_in_match()`。

窗口有焦点 ≠ 游戏在收输入：标题匹配在大厅、加载页、结算页全都成立。焦点之外还要过 `LobbyControl` 那关。

---

## 状态：两种，别混

**屏幕瞬时状态**（大厅在不在结算画面、刷新器哪列展开了）——**现测，永不缓存**。

`LobbyControl.state()` 直接透传 `det.state()`，`SpawnerControl.sync()` 每次重读。这是对的，别改成缓存。危险写法是 `self._expanded = True`（我点过所以它是开的）——游戏会掉线、会弹窗、会被另一个 agent 抢走，缓存必然骗你。

**游戏世界状态**（当前枪、配件、火力模式）——在 `detector/game_state.py`，不在这一层。因为它不是每帧可观测，且 `robot` 和多个 control 要读同一份。**写入由观测驱动，不由「我发过这个动作」驱动。**

最后那句是 2026-08-02 补上的，因为 `state.tab_open` 违反了它整整一年：按 Tab 就把缓存布尔量取反（`toggle_tab_open`），300 ms 后再读屏幕纠正。那 300 ms 里它是猜的，而十几条 `cond: '!tab_open'` 拿它当闸门——包括压枪开不开。**按键被吞**（见 `docs/game_quirks.md`）就会让它反过来且无人察觉。现在归 `TabWatch` 管，只有看过屏幕才动。离线回归 `pixi run tab-watch`，实机 `tools/probe_tab_watch_live.py`（20/20 与屏幕一致）。

---

## 用法

### 确保在局内

```python
from control.lobby import LobbyControl
with LobbyControl() as lc:
    if not lc.ensure_in_match()['ok']:
        return                # 大厅/加载/结算/菜单，任意态都会自己走到局内
```

测量值和状态机全在 `docs/lobby/README.md`。两个坑：

- **ESC 菜单的像素探针全都说「在局内」**。不查 `SYSTEM MENU` 标题就会返回 `playable=True`，而按键全被菜单吃掉。
- **别用固定 `sleep` 等游戏。** 结算页 ~18 秒、匹配+加载不是常数。`EXIT_TIMEOUT`/`ENTER_TIMEOUT` 是放弃阈值，不是预期耗时。

### 刷东西 — 三层，按需要进入

**要一堆东西，用 L2 批量入口**（绝大多数情况用这个）：

```python
from control.spawner import SpawnerControl
with SpawnerControl() as sc:
    sc.give_many(['m416', 'comp_ar', 'vert_grip', 'red_dot', 'backpack3'])
```

`give_many` 先 `plan()`：**gear 排最前**（它盲驱动，需要全折叠态），其余按类别聚合，面板每个类别只开一次、末尾统一收一次。9 件东西跨 5 个类别，类别点击从 ~18 次降到 ~6 次。

**想先看它要干什么**（不碰游戏）：

```python
for s in sc.plan(keys):
    print(s['kind'], s['key'], s['category'], s['index'], s['times'])
```

**要自己控节奏，用 L1**：

```python
sc.sync(need_cols=(1, 2))
st = sc.read()                     # PanelState，一帧读出，不需要基线
print(st)                          # <panel open, col2_row03 expanded, 7 entries>
rec = sc.goto(2, 3)                # 最短路径到这个节点
sc.click_entry(rec['entries'][4])  # 直接点第 5 项
sc.collapse_all()                  # 回根（现在是可选的，不是每次必须）
```

`read()` 是**绝对**的：一次截图读出「面板开着吗 / 哪个类别展开着 / 有哪些子项」，不跟任何基线比，所以序列中间读和开头读一样可信。原理和 42 帧真值验证见 `docs/spawner/README.md` 第 3b 节，回归测试是 `pixi run panel-state`（离线）。

**要手动救场，用 L0**：`click_category(col, row)` / `click_entry(entry)` 只发点击，不读不验。

三个坑：

- `sync()` 会**反复读到布局不再变化**才返回。面板渐进绘制，证明它开着的三个按钮图标比最后一列画得早——一开就读会稳定地少一列。实测出来的，别改成读一次。
- **类别列表只能在折叠态读。** 展开态下 `find_menu` 在 3~14 行之间乱跳（子菜单的居中行混进行检测）。但读一次就够——类别行的 y 不随展开变化，实测 42/42 帧。
- 刷出规则（空栏进 1 格 / 满栏顶掉的枪掉地上 / 配件进背包）见 `docs/game_quirks.md`。

### 装卸配件、扔枪

```python
from control.inventory import InventoryControl, at_gun, at_ground, at_inv, at_slot

ac = InventoryControl()
with ac.tab_up():                      # 已开就免费，只关自己开的
    ac.loadout()                       # {'guns':…, 'slots':…}，53ms，任意状态进
    ac.hold(1)                         # 先拿在手上，右键才够得着
    ac.equip(1, view.find('comp_ar'))  # gesture='auto'：在手就右键，否则拖
    ac.unequip(1, 'muzzle')            # 卸用拖拽（往左拖）
    ac.drop_weapon(1)                  # 整枪连配件扔地上
    ac.clear_rack()                    # 两格都清空
```

**装用右键、卸用拖拽**：实测右键 4/4、0.35s，而库存→枪的拖拽 **0/4，根本落不下去**；反方向（枪→库存）拖拽是好的。数字在 `docs/game_quirks.md`。

**`drop_weapon(gun)` 扔的是整把枪，配件跟着走。** 目标是枪械行左端那个方框数字（`at_gun(n)` → `gun_tag_point`，实测 (2237,145)/(2237,447)）。两个手势都行，**默认右键**：

| 手势 | 落位 | 端到端 |
|---|---|---|
| 右键点一下（默认） | 1/1 | **0.66 s** |
| 向左拖 1621 px | 1/1 | 1.15 s |

两次实测（2026-08-02，同一把 aug 带 4 件）都是：机架清空、**库存零增长**（配件确实跟着走了，没掉回背包）、地面多一行。`gesture='auto'` 先右键、不成再拖；`'click'` / `'drag'` 强制其一。

为什么不先 `strip()` 再扔：拆下来的配件会回到背包，而 PUBG 会把背包里能装的**自动装到下一把枪上**——一轮标着 BARE 的数据就是这么带着握把和快拉弹匣跑完的。整枪扔出去，下一对枪才是干净的。

**能力矩阵是数据不是散文**：`MOVES[(src_kind, dst_kind)]` 说得出手势、能不能读回验证、以及**证据等级**（`measured` / `used` / `untested`）。`_reject()` 拿它当闸门——两端都是合法地址不代表这个动作存在，`('gun','inventory')` 就是两个好地址加一个不存在的动作。

**每个方法只返回两种形状之一**：`step()`（一个手势）或 `batch()`（一批，带 `steps`）。以前是五种。`ensure_kit` 的 `ok` 由读回决定，比「每步都 ok」更强，用 `batch(ok=...)` 表达。

`transfer(1, 2)` **故意走背包中转**：直拖 slot→slot 那条边在 `MOVES` 里标着 `untested`，而隔壁实测是「库存拖进枪槽 0/4」。要定它跑 `tools/probe_transfer.py`。

地址体系（`pixi run locations` 有 81 项离线回归）：

| 地址 | 是什么 |
|---|---|
| `at_gun(n)` | 枪**本身**（`('gun', n)`）|
| `at_slot(n, s)` | 那把枪的某个配件槽（`('weapon', n, s)`）|
| `at_inv(i)` / `at_ground(i)` | 库存 / 附近的第 i 行，`i=None` 表示「这个面板里随便哪」|

⚠ `at_gun(1)` 和 `at_slot(1,…)` 是**两种不同地址**，别混。`_reject()` 一度不认识 `('gun', n)`，于是每次扔枪都在鼠标动之前就被拒（报的却是「拖拽失败」），而地址、抓取点、方法全都写好了——那个 bug 活到有人写离线测试为止。

### 对局内回路

`match.py` 的 `Dispatcher` 由 `robot.py` 装配，不要单独 new。它读 `config` 里的表：`KEY_ACTION_TABLE`（按键→状态+硬件）、`DETECT_TABLE`（调度检测器）、`MISMATCH_TABLE`（调度训练数据采集）。

**加新行为优先改配置表，不要往 `_loop` 里塞分支。**

采集本身在 `calibration/mismatch.py`——这个回路要赶发 pattern 的时机，不该在里面写 PNG。

---

## 共用硬件

多个 agent 共用**一个 Pico 串口**和**一个游戏窗口**。跑之前查有没有别的 python 进程占着，**别杀别人的**：

```python
from press.pico_mouse import other_agents
```

`Pointer` 是懒构造的——只读状态的调用方不会去占串口。别在构造函数里提前建它。

要跑占用游戏焦点的东西，**先跟用户说一声等确认**。失焦会让整轮数据静默归零，而且看不出来。

---

## 坏了找谁

| 症状 | 先看 |
|---|---|
| 面板打不开 / 点击没反应 | 焦点。`tools/focus_trace.py` |
| 「category col3_row06 does not exist」 | `sync()` 读早了，看 `EXPECTED_ROWS` |
| 「stuck expanded」但菜单明明关了 | 基线漂移（东西掉地上改变了半透明面板背后的像素），见 `collapse()` 的注释 |
| 退不出训练场 | `leave_entry_confirmed()` 验字形失配。正式局的菜单没采过，**拒点是故意的** |
| 分层被破坏 | `pixi run layering` |

## 待办

- ~~**Tab 界面还没有对应的 control。**~~ **✅ 2026-08-02 完成**：`calibration/attach_control.py` → `control/inventory.py`，`AttachControl` → `InventoryControl`。它本来就只依赖 detector/press/control，搬过来 `pixi run layering` 一次通过。标定用的批量流程（`build` / `run_plan` / `plan_equip`）跟着一起走了——它们是纯函数，跟驱动放一起比拆开更好用。
- **`goto()` 的 `path` 字段还在攒数据。** 它不预设这个菜单是手风琴还是多开——直接点目标再读回，`path='direct'` 说明是手风琴（1 击），`path='cleared-first'` 说明不是（3 击）。跑够了把结论写进 `docs/game_quirks.md`。
