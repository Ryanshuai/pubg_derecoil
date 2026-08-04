# detector/ — 检测层

从屏幕像素得到游戏状态。上游是 `screen_capture` / `cropper` 给的 crop，下游是 `robot`（压枪）和 `control/inventory`（驱动 Tab 界面）。

三种节奏，混淆了会付出代价：

| | 谁 | 频率 | 预算 |
|---|---|---|---|
| 常驻 | `HUD_REGIONS` 里的一切（weapon/fire_mode/posture/ammo） | 每帧 ~144fps | 微秒级 |
| 事件触发 | Tab 界面、spawner 界面 | 按键时一次 | 几十毫秒可接受 |
| 状态轮询 | 在不在局内（`lobby_detector`） | 1–2 Hz | 几十毫秒可接受 |

**不要把事件触发的区域塞进每帧抓的那一套。** DXGI 后端只能用**一个** bounding box，所以一个远处的区域不是只花它自己那点面积——它花的是**中间的一切**。事件触发的用自己的 grabber（见 `tab_items.TabGrabber`，`only=('right',)` 只要两把枪那块）。

这条 2026-08-02 之前一直被 `HUD_REGIONS` 自己违反着，代价实测：

| 每帧抓什么 | 区域数 | bbox | 拷贝耗时 | 占 8.3ms 预算 |
|---|---|---|---|---|
| 只要游戏 HUD | 5 | 1641×136（4.5%） | 0.80 ms | 9.5% |
| 连 Tab 一起（旧） | 18 | 2077×1275（**53.5%**） | 7.12 ms | **85.4%** |

游戏 HUD 在屏幕底部（y≈1262–1398）、Tab 面板在顶部（y≈123–680），两者放一起 bbox 高度从 136 撑到 1275。**p99 是 11.14 ms，超过 8.3 ms 的帧预算**——每百帧必掉一帧，实测跑出 115 fps（目标 144）。拆开后 124 fps。

所以现在 `config.py` 分成两份：`FRAME_REGIONS` 是每帧抓的，`TAB_REGIONS` 按需抓（`control/tab_watch.py`）。`HUD_REGIONS` 仍然是**坐标总表**，谁都可以从里面查坐标——只是别再把它整个丢给 `ScreenCapture`。

---

## 干活之前先问：现在在局内吗

`lobby_detector.py` + `control/lobby.py`（**已有，别重造**）。测量值全在 `docs/lobby/README.md`。

```python
from control.lobby import LobbyControl
with LobbyControl() as lc:
    if not lc.ensure_in_match()['ok']:
        return                      # 大厅/加载/结算/菜单，任意态都会自己走到局内
```

四态，**只有 `IN_GAME` 的 `playable` 是 True**：

| 状态 | 怎么来的 | playable |
|---|---|---|
| `LOBBY` | 右黑边纯黑（大厅永远 16:9 letterbox，没有铺满模式） | False |
| `IN_GAME` | 铺满 + ping 叠层在 + 没有 ESC 菜单 | **True** |
| `MENU` | 同上，但 SYSTEM MENU 标题匹配上了 | False |
| `FULLBLEED` | 铺满但没 ping：加载页、或 ping 叠层被关了 | False |

**两个能坑死人的地方：**

- **ESC 菜单的像素探针全都说「在局内」**。画面在渲染、ping 在、`TIME/JOINED` 也在，`bar_max=44` / `ping_frac=0.089` 跟正常局内没区别。不查 `SYSTEM MENU` 标题就会返回 `playable=True`，而按键全被菜单吃掉——`harvest.py` 撞上这个整轮静默作废。
- **`LEAVE TRAINING` 正下方一个 pitch（85px）就是 `EXIT TO DESKTOP`**。所以 `click_leave()` 强制先跑 `leave_entry_confirmed()` 验字形，失配就拒绝点。正式局的菜单没采过，届时会失配、拒点——**这是故意的，不是 bug**，但意味着正式局现在退不出来。

**别用固定 `sleep` 等游戏。** 结算页自己走 ~18 秒、大厅→局内要过匹配+加载，时长都不是常数。`control/lobby.py` 全程轮询，`EXIT_TIMEOUT`/`ENTER_TIMEOUT` 是放弃阈值不是预期耗时。`Pointer` 懒构造，只读状态不会去占 Pico 串口。

---

## 还要问：焦点在游戏上吗

`control/focus.py`（**已有，别重造**）。

从终端拉起的工具，t=0 时焦点在终端不在游戏，第一次 `game_focused()` 必然 False。`harvest.py` 靠 `--countdown` 让人手动切窗口——那就是那些 run 至今不能真正无人值守的原因。

```python
from control.focus import ensure_focus, focus_keeper
if not ensure_focus(countdown_s=6):    # 抢→验→重试3次→倒计时兜底
    return 1
time.sleep(0.6)                        # 切前台后头几帧游戏不收输入
...
if not focus_keeper().ok('mag 3'):     # 跑到一半掉了就抢回来，上限 5 次
    break
```

**倒计时是退路，不是手段。** 全项目已接：harvest / sweep / collect_templates /
control/spawner.py / control/inventory.py / control/lobby.py。

三个坑：

- **裸 `SetForegroundWindow` 会被拒。** Windows 只允许当前前台进程交出焦点，别的进程调它只会让任务栏闪一下。`raise_game()` 用 `AttachThreadInput` 借前台线程的输入队列绕过去；不模拟 ALT 键，因为 ALT 在这游戏里是自由视角。
- **调完必须再验一次 `game_focused()`。** 它可以不报错地失败。
- **焦点不是拿到一次就固定的。** 终端会反复抢回去，期间发出的按键直接丢失，症状是「spawner 面板打不开」，而脚本第一行明明打印了 `focused=True`。关键操作要**每次重试前重新抢**，别只在开头抢一次。

窗口有焦点 ≠ 游戏在收输入：标题匹配在大厅、加载页、结算页全都成立。焦点检查之外还要过上面那关 `control/lobby.py`。

## 第一铁律：模板漂移是静默的

检测器**不会**因为模板过时而报错。它会给出一个看起来完全合理的错答案。

实例：`Lower_ThumbGrip_C` 与当前游戏画的拇指握把已经对不上，于是 Mk12 的握把槽读成 `laser`——也是握把、也在候选里、margin 还不低。下游 `control/inventory.py` 靠读回槽位确认装配成功，拿到这个结果会误判失败并重试。

这条是 2026-08-01 端到端验证时偶然撞见的，不是任何机制报出来的。**游戏每次更新都会产生这类漂移**，所以：

- 改完检测器，跑 `pixi run attachments`（全量真值 + margin），别信"看起来没问题"
- 报告准确率时必须说明用的是哪个样本集，以及集里有没有难例
- 发现漂移 → 见下方「坏了找谁」

### 配件图标：装的是「屏幕上的样子」，不是游戏美术资源

`training_data/pubg_assets/Item/Attachment/` 现在有两种文件，**同一个 asset 的多个变体全部参与匹配，最好的那个代表它**（跟枪名板的多语言变体同一套机制，理由不同）：

| | 哪来的 |
|---|---|
| `Item_Attach_Weapon_<Asset>.png` | 游戏解包美术 |
| `Item_Attach_Weapon_<Asset>.solved.png` | 从屏幕反解出来的 |

**为什么美术资源天生对不上**：游戏画一个图标要先缩放、加黑描边、再混进半透明面板（`blend_attachment`）。美术图是这条链的**输入**，检测器看到的是**输出**。`collect_templates.py` 的 `paired_sweep` 拍同一个槽的空/满两张，`solve_template.py` 逐像素最小二乘反解出 `icon` 和 `alpha`，那才是屏幕上的东西。

实测（`pixi run attachments`，760 张 `LABEL_REQUESTED` 槽位样本，16 个 run）：

| | 准确率 | margin 中位 | 修好的 |
|---|---|---|---|
| 只有美术图 | 659/734 = **0.898** | 3.9 | — |
| 加 solved 变体 | 701/734 = **0.955** | 10.9 | `thumb_grip` 0/25→25/25、`angled_grip` 0/16→15/16、`comp_sr` 31/33→33/33 |
| 再补上三个「没有美术图」的 | **749/760 = 0.986** | 11.2 | `brake_ar` 0/18→18/18、`heavy_stock` 0/10→10/10、`variable` 0/10→10/10 |

**最后那一行的三个件，两个的数据早就在磁盘上躺着，是 `asset: None` 把它们锁住的。** 目录里 `asset` 为 None 表示「本仓库没有它的模板」，而建库代码按 asset 名给文件命名——于是最需要反解图标的三个件，恰好是建库时被静默跳过的三个。现在它们的 stem 是**我们自己起的**（`Muzzle_Brake_Large_C` / `Stock_Heavy_C` / `Upper_Variable_C`，目录里标着 `# recovered`），只要前缀对得上 `SLOT_PREFIXES` 就够——游戏在这套美术资源之后才加的件，本来就没有官方文件名可抄。

`asset: None` 从来不是中性的：**没有模板的件不读成未知，读成最近邻**。`variable` 稳定读成 `scope_6x`，10/10。

**hold-out 数字一模一样（701/734）**：`--holdout` 对每个 run 用「不含该 run 解出的模板」的库去评它自己的样本。两个数相等说明模板重建的是图标，不是它自己那批截图。另一条独立证据：193140 和 211051 两个 run 各自解出的同一个图标，在 alpha>0.5 的像素上逐点差 **0.08–0.18 灰阶**，alpha 差 0.006。

还没做完的：

- `scope_2x` 仍有 6/16 被 `scope_6x` 吃掉。`scope_6x` 的解是全库最弱的一个（recon 2.06，只有一个 run 有它），而两个镜子的图标差别几乎只有一个数字。
- `bullet_loops` `choke` `duckbill` `light_grip` `quickext_smg` `scope_8x` `tactical_stock` 没进过配对采集，还在用美术图。前三个 ROSTER 里没有活枪能穿，采不了。

`supp_ar` / `supp_sr` 的 margin 只有 1.09 / 1.68——同族三根灰管子，靠枪名收窄候选才分得开。**能传枪名就传**。

**代价是一次 Tab 读取从 80 ms 涨到 123 ms**（`tab_items.detect`：10 个槽 + 两栏 24 行盲匹配；`read_slots` 单独是 15.5 → 29.5 ms）。模板数 55 → 85。两阶段匹配里**只有粗排省下来了**：粗排每个 asset 只跑无 tag 的那张，精排（9 偏移）才跑全部变体。**反过来不行**——让粗排挑变体会丢掉 12 行参考里的 2 行：无偏移时美术图和 solved 在列表行上会换位，而精排加的那一个像素偏移正是分开它们的东西。跟 `SHORTLIST` 是同一条教训：便宜的那一趟可以排序，不可以定案。

## 第二铁律：几何声明要端到端验证

坐标不能目测。验证方式是**让模板匹配去证明**：坐标对了，装了东西的槽全部认出、空槽全部判空、MSE 有量级差距；坐标偏了，模板匹配会先劣化再失败。

`temp_debug/verify_tab_slots.py` 是这个套路的样板。标定图标几何用 `temp_debug/calib_inv_icon.py`：拿已知答案的样本扫参数，按"认对几个"排序，MSE 只用来打破平局。

---

## 踩过的坑（都有实测数字）

**库存行的模板还是老的一套，槽位的改进没跟过去。**
行图标是同一份美术在**另一个尺寸和内边距**下渲染的（`temp_debug/calib_inv_icon.py` 标的就是这个几何），而 solved 模板是从**槽位**的混合里解出来的等效图标，换个尺度就带系统偏差。实测 `docs/tab_inventory.png` 12 行人工真值：solved 变体把 `thumb_grip` 从 MSE 441 压到 175（row9 已经是第一名，margin 1.44），但 `ROW_MSE_MAX=150` 正好卡在外面，所以那两行仍然读不出来，**10/12**。

要补齐得从**行**的捕获里解，而不是把槽位模板拿去缩放。行捕获没有配对的空行，但一行在 10 个背景下拍过——跨背景不动的像素就是不透明像素，这条路不需要配对。前提是重采一轮：见下面那条 rows 真值的坑。

**列表行位置：用图标，别用文字带。**
标签会折行，且折行时**不在行内垂直居中**。用文字带测「附近」栏读出 15px 偏差和一个假的 66px pitch；用图标块测得到真值：首行 y=199，pitch 81.55，两个面板共用。

**空行判据：用高频细节，不是方差。**
列表末尾之后显示的是模糊的游戏画面，颜色丰富所以**方差很高**，`std` 判据会把空行全判成有物品。Laplacian 方差干净分离：有物品 702–6393，空 0–2，阈值取 100。

**候选收窄比什么优化都值钱。**
`tab_items.detect(frame, {1:'g36c', 2:'sks'})` 传了枪名，就用 `attachment_catalog` 把候选缩到该枪装得上的配件。这一项把 SKS 的消音器从 `supp_smg` 纠正成 `supp_ar`——两个图标几乎一样，盲匹配只有 1.3x margin。**能传就传。**

**两阶段匹配的 shortlist 不能小。**
先不带偏移粗排、再对前 N 名做完整 9 偏移评分，是 3.5 倍加速。但**偏移能把模板从尾部一路提上来**：shortlist=5 时 4 倍镜静默消失。8 是两张参考图都能复现全量结果的下限，现取 10。改这个值必须重跑对比。

**区域抓取按 y 聚类。**
`cropper._cluster` 只按 y 分带，所以 x 分散但 y 重叠的区域会聚成一个巨大的 band。Tab 的 36 个区域实际只占屏幕 4.4%，直接丢给 `RegionGrabber` 却抓了 46.5%。手动按 x 拆成左右两块降到 15.7%（23ms → 15ms）。

**全屏操作先想清楚是不是真需要全屏。**
`SpawnerDetector` 原本对整个 3440×1440 做灰度+阈值，只为用 3 个小窗口。挪进窗口后 27.4ms → 0.47ms，结果逐位一致。

**`IMREAD_GRAYSCALE` 不保证是灰度。**
ultralytics 会把 `cv2.imread` 换成自己默认 `IMREAD_COLOR` 的包装，进程里只要有人 import 过它，读模板就回来 3 通道，`findNonZero`/`boundingRect` 当场炸。`ammo_detector` 因此在 `smoke_check`（import ultralytics）里 FAIL，而单独跑的脚本一路绿灯。所有读模板的地方都要 `if img.ndim == 3: img = img[:, :, 0]`——`weapon_template_detector` 早就这么防了。

**弹药数字：高度就是全部分割逻辑。**
856 张全屏实测，数字**无一例外** 17-18 宽、**37 高**，顶边恒在 y=1323。同一条带里其它亮东西（弹匣图标条纹、HUD 下划线）最高 4px，所以一个高度窗口就把字形和家具分干净，不需要任何形态学。字形帧间逐像素一致，模板 IoU 在这里是精确解。

**弹药数字是居中的，不是右对齐。** 中心恒在 x=1719（1 位、2 位实测同心）。三位数（100 发弹鼓）向两侧长到 1686..1752，仍在 `HUD_REGIONS['ammo']`(1670..1760) 内——按右边缘锚定的裁剪会切掉首位。

**穷举搜索先证明它有用。**
`highlight_detector._align` 曾套一个 5×5 jitter 循环 = 50 次 matchTemplate。在 254 对标注样本上 jitter=2/1/0 **准确率完全相同**，关掉快 2.6 倍。旋钮保留为 `ALIGN_JITTER`，理由和唯一该开启的场景写在常量注释里。

---

## 开镜检测：用 `ads_detector`，别再自己造

`detector/ads_detector.py` 回答「现在开镜了没有」，**单帧、0.32 ms、可以每帧跑**：

```python
from detector.ads_detector import AdsDetector
ads = AdsDetector()
ads.scoped(frame)     # True / False
ads.score(frame)      # 原始余量，日志里记下来能看出是不是勉强过线
```

原理是**准星的缺席**：未开镜时屏幕正中画准星，开镜后中心什么都不画——没有任何镜子的准心长在同一位置、同一形状。所以问「准星还在吗」，用否定来回答开镜。

实测 492 帧带标签（拟合用的 run 有 6 档镜子，评测集里有它没见过的 4x/8x）：

| | n | 最差 | 中位 |
|---|---|---|---|
| 未开镜 | 344 | 20.8 | 83.3 |
| 已开镜 | 148 | 1.5 | −7.4 |

阈值 10，零错误，余量 14×。延迟在 40–150 ms 之间：点右键后 40 ms 的帧仍读「未开镜」（toggle 还没生效），150 ms 的帧全部读「已开镜」。

**优先于姿势图标。** 图标有无是这个项目原有的开镜指示（见 `docs/game_quirks.md`，`sweep.Rig.ensure_ads()` 在用）。同一批数据上图标即使到 700 ms 仍漏 5%，而且它那条「~0.85 s 才出现」只在**开火之后**成立——静止空载时 150 ms 就画出来了。两条方向相反（图标在 = 开镜，准星在 = 未开镜），要更稳可以组合成「确信开镜 / 确信没开 / 存疑」三态。

三个坑，改这个检测器前先读：

- **必须两套模板。** 腰射准星四刻度在 ±56 px，按住右键瞄准时会**收紧**。只匹配宽的那个，肩瞄会被读成开镜——而肩瞄恰恰不是开镜，`docs/ads/runs/20260801_222936` 整整 64 帧就是这个错误的现成负样本集。
- **判据是「五个部件里最弱的那个」，不是「中心亮不亮」。** 要求四臂全亮才排除得掉镜子自己的准心（镜子只点亮中心）：3x 上这一项把最差情况从 53.9 压到 0.03。
- **必须用相对量**（部件均值减环形背景）。绝对亮度版本直接失效：开镜后画面被放大、边缘更锐，逐帧 dewhite 的亮点数能到 3667，而未开镜的低对比背景下反而可能是 0。

**不要把中心 crop 塞进 `config.HUD_REGIONS`。** 屏幕中心在 y≈650–790，而 HUD 那一带是 y≈1301–1440。加进去会把 DXGI 的单一 bounding box 从 y=1301 一路拉到 y=650，每帧多拷半屏——只为一个 140×140 的窗口。`AdsDetector` 自己从全帧切，或者给它单独的 grabber。

模板在 `training_data/pubg_assets/ads_crosshair.npz`，重新拟合和复现上表都是 `pixi run fit-ads`（`--eval-only` 只评测）。数据用 `pixi run capture-ads` 采（见下方资产表）。

## 配件槽三态：用 `slot_detector`，别再自己造

`detector/slot_detector.py` 回答「这把枪**有没有**这个槽、槽里**有没有**东西」——不回答装的是哪个（那是 `AttachmentDetector`）：

```python
from detector.slot_detector import SlotDetector
slots = SlotDetector()
slots.classify(frame, 2)     # {'grip': 'absent', 'muzzle': 'empty', ...}
slots.present(frame, 2)      # 有槽位的集合（不含 unknown）
```

**为什么要它**：往枪没有的槽上拖配件，东西会掉地上，而「这枪没这个槽」和「这个槽不收这个配件」看鼠标是一模一样的。`attachment_catalog.SLOTS` 本该防住，但它 22 条抄 wiki、6 条纯猜、2 条读截图，**实测 0 条**。有了这个检测器，「哪些槽存在」是一次刷枪加一张截图，不是一整个拖拽矩阵。

**三态两判据，看的是不同像素：**

| 判据 | 看哪 | absent | empty | filled |
|---|---|---|---|---|
| 存在性 Sobel p90 | tile **边框环** | 5.0–26.0 | 46.0–172.7 | 同 empty |
| 有无内容 Canny | tile **内部** | — | 0–71 | 202–885 |

阈值 36（空档 20）/ 120。7 张已知真值的图上 **28/28 全对**。

三个坑，改之前先读：

- **只测边框，别测内部。** 内部是图标，跟「槽在不在」毫无关系，算进去会让判据随装了什么漂移。只取边框环之后，剥光的 M416 读 260/260/278/260，装满的读 260/260/318/260——几乎不动。
- **用梯度，不用 Canny 判存在。** Canny 的滞后阈值在 VSS 的弹匣槽上读出**恰好 0**，而那是个真实存在的槽——tile 压在亮沙地上、跟背景几乎同亮度，边界被量化没了。Sobel 幅值读 46。**在真实存在的元素上读出硬 0 的判据不是保守，是坏了。**
- **`scope` 恒返回 `unknown`，调参救不了。** 那个位置**根本不画 tile**：M416 的空 scope 只有背景和枪身，而没有 scope 槽的 VSS 在同一位置画着它自带的固定 PSO-1（那是**枪的美术资源**）。空槽和无槽逐像素相同，连「装没装」也一起坏（VSS 空槽读 678 内部边缘，远超判 filled 的 120）。scope 的存在性只能靠拖拽，内容交给 `AttachmentDetector`（它把 VSS 正确读成空）。**别让 `unknown` 塌成 `absent`。** 固定配件普遍如此，P90 / MP9 预计一样。

几何在 `tab_layout.slot_tile_box` / `slot_window`（tile 66×66，起点比 `HUD_REGIONS['att_*']` 左上各偏 1px），判据在这里。`HUD_REGIONS['att_*']` 那个 63×63 是**故意**切在 tile 内侧的——它是给模板匹配用的，边框像素不属于任何图标——**不要为了这个检测器去加宽它**。

活体跑单枪 `tools/probe_slot_boxes.py <weapon> --strip`，离线跑 `python detector/slot_detector.py <shot>`。**刷出来的枪不是裸枪**：PUBG 会把背包里能装的自动装上，所以要 `--strip`。

补齐整张表见 `calibrate-compat` skill。

## 弹药计数：用 `ammo_detector`，别再自己造

`detector/ammo_detector.py` 读弹匣里还剩几发，**单帧、0.18 ms、可以每帧跑**：

```python
from detector.ammo_detector import AmmoDetector
ammo = AmmoDetector()
ammo.classify(crops)          # int | None
ammo.read(crop)['glyphs']     # 每个字形的 digit / iou / margin，排错用
```

**`None` 不是 0。** 空弹匣照样画 `0`；`None` 表示数字没读出来（开背包、收枪、动画中），当成 0 会以为刚打空一梭。

`sweep.py` 一直只把这块区域当**变化信号**（二值化看像素动没动），能说「打了一发」「换弹完成」，说不出还剩几发。读出数值它才是计数器：一个点射到底出了几发、某次后坐力采样属于第几发、这把枪的弹匣是目录说的 30 还是扩容后的 40。

**分割逻辑就是高度。** 856 张全屏实测，数字无一例外 17-18 宽、**37 高**，顶边恒在 y=1323；同条带里其它亮东西（弹匣图标条纹、HUD 下划线）最高 4 px。一个高度窗口就分干净，不需要任何形态学。**居中不是右对齐**，中心恒在 x=1719，三位数向两侧长到 1686..1752——按右边缘锚定的裁剪会切掉首位。

实测（`tools/collect_ammo_digits.py --verify`，M249 满弹 150 逐发打到 1）：

| 位数 | 值域 | 读对 |
|---|---|---|
| 三位 | 150..100 | 51/51 |
| 两位 | 99..10 | 90/90 |
| 一位 | 9..1 | 9/9 |

零未读、零错读。**字形宽度不随位数变化**——十个模板全部从三位数采，两位、一位照样逐个读对。（0 采不到：打空瞬间游戏自动换弹，计数从 1 直接跳回满。`0` 这个字形在 150/140/.../10 里已覆盖几十次。）

**`MIN_IOU` 必须高，低阈值不安全。** 模板缺失时数字**不会**读成 `None`，而是被最像的模板自信地吃掉：全套齐之前每个 `3` 都稳定读成 8（0.748）、每个 `9` 都读成 0（0.800），自洽得像真结果。真匹配最差 0.968，最强冒名对是 6 vs 9 的 0.869，所以阈值 0.90。

重建模板：`pixi run python tools/collect_ammo_digits.py --write`——自己刷 M249、点 30 发，`150..121` 含全部 10 个数字。GT 不用标：计数每发降 1，第 k 个读数就是 `起始-k`；起始值**推断而非假设**（PUBG 会把背包里配件自动装上，M249 可能 100 也可能 150，靠已装模板分辨，分不出就中止），每个状态再用已装模板交叉验证，错位整轮作废。校验器有离线自测 `probe_ammo_ocr.py --selftest`（7 例，含错位一格和序列断档）。改完必须重跑 `--confusion` 和离线全量 `probe_ammo_ocr.py`。

**采集器踩过两个坑，都是「同一个读数被当成两个」：** 精确字节比对认不出同一数字（抗锯齿随背景变），要用 IoU + 容差（`SAME_IOU=0.92`，真匹配 0.98+，最近的异类 0.79）；HUD 换数字时会有一两帧只画出 1-2 个字形，得**按持续时间确认状态**（`MIN_STATE_S=15ms`，过渡帧约 2 ms，最短真实读数 80 ms）。这两个各自都能让 14 次点射报出 34 个状态，把之后每一个标签都推错一格。

## 验证资产

| 资产 | 内容 | 用途 |
|---|---|---|
| `docs/tab_inventory.png` | G36C + SKS，库存 12 行满 | Tab 检测回归，12 行真值见 `temp_debug/calib_inv_icon.py` |
| `docs/tab_inventory_2.png` | Micro UZI + Mk12，「附近」栏有物品 | 地面栏 + 槽位渲染规则 |
| `docs/training_epuipment.png` | spawner 面板 | `SpawnerDetector` 正例 |
| `training_data/highlight_eval/` | 260 高亮 + 439 非高亮，带标签 | `temp_debug/eval_highlight_jitter.py` 配对评测。`errors_v4/` 是空的——**这个集里没有难例** |
| `docs/spawner/runs/` | spawner 全部分类的菜单截图 | 游戏当前物品清单的事实来源 |
| `docs/compat/runs/<stamp>/` | 30 把枪各一张全屏 + `summary.json` | 槽位几何回归（`scan_compat.py --report <stamp>`）。**不是模板真值集**：枪上装的是 PUBG 自动配的，没人指定过，只能靠被测检测器认——拿它标模板是循环论证 |
| `docs/attachments/runs/<stamp>/` | 配件采集：槽位配对图（空/满同角度）、库存行图、枪名板 | 配件模板的真值集，`pixi run attachments` 吃它。**槽位那半可信，库存行那半不可信**——见下 |
| `docs/lobby/*.png` | 5 张：大厅 / 训练场 / 训练场+Tab / 正式局结算 / ESC 菜单 | 在不在局内的回归，`tools/verify_lobby_detector.py` 五条全过。每张的 `bar_max`/`ping_frac` 实测值见 `docs/lobby/README.md` |
| `docs/ads/runs/**/*.jpg` | 610 张全屏帧（本为 ADS 采集） | 顺带是弹药数字的离线回归集：`tools/probe_ammo_ocr.py` 在 921 张里读出 869，其余 52 张确实没数字 |
| `docs/ads/runs/*/index.jsonl` | 每帧标了 scope / state / t_ms / 槽位实读 asset | 开镜检测评测集，492 帧。**别照 `state` 当真值**：`20260801_222936` 的 `state=ads` 其实是按住右键的肩瞄、从未开镜；`20260802_015545` 整轮在错的槽位上。两个 run 的 `meta.json` 里都写了原因，`calibration/fit_ads_detector.py` 顶部的 `NOT_SCOPED` / `SCOPED` 是修正后的真值。**用 `CaptureRun.load_dir()` 读就不会踩**：旧 run 的标签一律降级为 `LABEL_DETECTED`，`labelled()` 对它们返回空，`state` 只作为「采集过程干了什么」的事实存在，不冒充「屏幕上是什么」 |

槽位坐标固定：枪没有的槽只是**不画**，不会挪位（UZI 无 grip / Mk12 无 stock 实拍确认）。所以拖拽目标坐标是安全的。

⚠ 这句话原来写的是「不画边框」，误导性很强：**空槽同样不画边框**，它画的是一整块浅色 tile，而没有的槽是纯背景。所以「有没有边框」区分不了 absent 和 empty，得测 tile 边框环的梯度——见上面 `slot_detector` 一节。

## 坏了找谁

| 现象 | skill |
|---|---|
| 图标、UI 文字（枪名、`类型`）、HUD 数字（弹药）认不出或认错 | `calibrate-template`（重提模板 + 全集混淆检查；也管图标的位置、尺度、alpha、混合） |
| 行距、槽位、面板位置对不上 | `calibrate-screen` |
| 某把枪能装什么配件（槽位存在性、某槽只收部分配件） | `calibrate-compat` |

游戏机制类的坑（切枪退出开镜、姿势图标只在开镜时渲染等）记在 `docs/game_quirks.md`，不要在这里重复。

## 当前已知缺陷

- **`posture_detector` 在浅色木质背景上读不出来，而它的标注集里一张这种背景都没有。** 判据是 `bright = (V > 180) & (S < 80)`——**绝对亮度加低饱和**，训练场那些浅木板两条全中。膨胀（Canny 5 / Sobel 3）再把板缝和图标连成**一个连通域**，于是 `argmax(sizes)` 挑中的是木头。实测那张裁图（`docs/posture/fail_0804/`）：

  | | |
  |---|---|
  | 过「亮+低饱和」闸的像素 | 2003 / 4356 = **46%** |
  | 最大连通域 | 1403 px（66×66 裁图的 32%，图标不可能这么大）|
  | 三个模板的 IoU | prone **0.268** / standing 0.144 / crouching 0.092，阈值 0.32 |

  prone 是最高的那个——**人确实趴下了，是检测器认不出来**，而 `ensure_posture` 因此整格丢弃。2026-08-04 那轮姿势轴 4 把枪 6 格死于 `posture unreadable`。

  **标注集看不见这个失败**：`training_data/Manual/posture/` 1714 张（standing 833 / crouching 432 / **prone 10** / bg 439）实测 **1698/1714 = 99.07%**，全过。这跟 `ads_detector` 那条「492 帧全来自同一片场地」是同一个病——**准确率数字只对采样过的背景成立**。所以别拿一张失败样本去改一个 99% 的检测器，先攒一批：`GunDriver.dump()` 已改成编号不覆盖（原来每次写同一个文件名，六次失败只剩最后一张）。

  另外 **prone 只有 10 个样本**，而它正是反复出问题的那一类。

- `ammo_detector` 的十个字模全部采自**三位数**（150..121）。两位、一位实测逐个读对，但它们从没被单独重采过；哪天游戏改成按位数用不同字号，会先在这里翻车，重跑一次 `--verify` 就能看出来。
- `ads_detector` 的 492 帧全部来自 **Kar98k、同一片场地、全程未开火**。没验过的：开火时后坐力抖动会不会糊掉准星（最可能翻车的一条，压枪场景恰恰全程在开火）、其他枪的腰射准星是否同形、载具/趴姿等会改准星的状态、以及 4 倍以下的其他红点变体。要上压枪主循环，先补一组**开火中**的帧
- `lobby_detector` 的六态里，**加载页一张样本都没有**——`FULLBLEED` 现在被结算页和退出确认框覆盖，它对加载页的判定仍是照定义推的（活体转移里观测到了，没存图）。另外 **正式局的 ESC 菜单**没采过，`leave_entry_confirmed()` 届时会失配、拒绝点击，所以**正式局现在退不出来**（训练场可以）。补齐跑 `pixi run python tools/probe_lobby_transition.py`，全程截图落 `docs/lobby/runs/<n>/`。`control/lobby.py` 顶部的 `OBSERVED DURATIONS` 三项也还是空的
- ~~`Lower_ThumbGrip_C`、`Stock_UZI_C` 已漂移~~ 两个都有 solved 变体了，槽位上 25/25 和 10/10。**`thumb_grip` 在库存行里仍然读不出来**（上面那条）。
- ~~枪口制退器、重型枪托、多倍率混合瞄具没有模板~~ 三个都有了，槽位上 18/18、10/10、10/10。
- ✅ **~~刷新器在连续多轮里会点错类别行~~ —— 那条诊断是假的，真因是读回在亮背景上说谎。已修（2026-08-04）。**

  原来的说法是「上一轮展开的类别没收回，下一轮按全折叠坐标去点」。面板当时**是全折叠的**，有截图。真正发生的事：半透明面板 = `blur(bg)*0.49`，背景一亮行判据就被冲掉，而 `read()` 不报「读不了」，它给一个**格式完全正确的假状态**。

  A/B，同一份代码、同一个坐标、同一次点击，只差视角朝哪：

  | 视角 | `sc.read()` | `click_category(1,3)` 之后 |
  |---|---|---|
  | **天空** | `col1_row02 expanded, **2 entries**` | `[]` |
  | **地面** | `all collapsed` ✓ | `[(1, 3, 7)]` ✓ DMR 正好 7 项 |

  没有哪个类别是 2 项。**代价是整轮静默作废**：`collect_templates --all` 那轮 12 轮死了 8 轮，全是 `never clicked: sks`，而肇事者是**另一个脚本（俯仰探针）把视角留在了天上**。

  **修法：驱动路径上一个识别都不做。** 三处都是「正确的办法早就在文件里，只是没被这条路用」：

  | 位置 | 原来 | 现在 |
  |---|---|---|
  | 条目坐标 | `_spawn` 点 `entries[index-1]`（识别结果） | `entry_point()` 常量算：`cat_y + 44.25 + k*50.70`，x 为列左 +237 |
  | 展开验证 | `_click_await` 轮询 `read()` 直到列出条目 | **帧差**：点击前后数列内改变的像素（原本只用在装备那一支，注释写着「the scene behind a translucent panel does not do on its own」） |
  | `expect` 计数 | 拿识别出的条目数当闸 | 不再当闸——能查它的只有会说谎的那个读回。目录过期是慢事实，归 `tools/scrape_spawner.py` 显式核对 |

  常量不是新猜的：`SUBMENU_ENTRY_DY/PITCH/CLICK_DX` 从一开始就在 `spawner_layout` 里，注释甚至写着「that is why the spawner does not need a screenshot per click」。2026-08-04 拿 `docs/spawner/runs/` 里**全部 42 张类别展开图**重新验过：三列、每个类别、每一条目都吻合 `cat_y + 44 + k*50.72`，**最大误差 3.1 px**，条目中心 x = 列左 +252.7（三列一致）。

  **验收是对着天空刷东西**：`give_many(['m416','red_dot','comp_ar'])` 8 次点击 3/3 落地，枪架从空变 m416。这正是原来必死的姿势。

  `read()` / `expansions()` 还在，但只做诊断和恢复，不在驱动路径上。离线 `pixi run panel-state` 永远看不见这类问题，因为它喂的是存图。
- **背包清不空的时候，往后每一轮都会连带废掉。** 同一轮日志里 `库存 still holds 12 row(s) — the drops are not landing`，然后三轮 `no bare host gun`。往 附近 栏扔东西的释放点是固定 y（`DROP_XY`，落在第 4、5 行之间），列表一长就落在已有物品上。`control/inventory.py` 的注释里记着这件事的来龙去脉，那个文件 2026-08-03 深夜正在被改。
- **库存行（`rows`）的真值集是坏的，一共 930 条里没有一条能用。** 两个独立的原因：
  - 文件名 `row00__sks__lbg0.png` 不含轮次，多轮 run 里后一轮直接覆盖前一轮的文件，而两轮的 manifest 条目都还在。7 个 run、130 个文件、**580 条标签**，其中一个文件被 12 个不同配件同时声称。`CaptureRun.conflicts()` 现在能查出来，`labelled()` 不再发出这类标签；采集端的文件名已加轮次。
  - 剩下的 350 条是**行号错位**：游戏把新件插进它自己的排序，采集器当时假设最新的在最后（ff047bc 修的）。证据是拿已验证 0.955 的模板库去读——`row2` 标 `cheek_pad` 读出 `ext_ar` MSE=12 margin=10.5，`row11` 标 `variable` 读出 `vert_grip` MSE=13 margin=9.1。MSE 12 配 10 倍 margin 是正确匹配的样子，不是巧合。**不是统一偏移，改不回来，只能重采。**
  - 这批图**没删也不会删**，`pixi run attachments` 照样打印它们的得分，只是不计入总数。现在唯一可信的行真值是 `docs/tab_inventory.png` 那 12 行（人工读的）。
  - **重采就能补齐，命令是一条**（12 轮 / 38 个件 / 约 40–50 分钟，占游戏前跟用户说一声）：

    ```
    pixi run python calibration/collect_templates.py --all --targets slots,rows
    pixi run python tools/score_attachments.py --write     # 反解 + 重建 + 评分
    pixi run attachments --holdout                          # 留一验收
    ```

    `bullet_loops` / `choke` / `duckbill` 会被跳过——ROSTER 里没有活枪能穿，`--plan` 会直接说。**采完 baseline 会变**（`score_attachments.py: BASELINE` 是棘轮，变好也会红），按它打印的数字更新，并把那段「够不着的清单」改成新的事实。
  - 行模板本身还是从**槽位**尺度解出来的，换到列表尺度带系统偏差（`thumb_grip` 在行里 MSE 175，卡在 `ROW_MSE_MAX=150` 外面）。重采之后可以从**行**的捕获直接解：行没有配对的空行，但同一行拍了 10 个背景，**跨背景不动的像素就是不透明像素**，这条路不需要配对。
- `attachment_catalog.SLOTS` 的 **`scope` 那一项仍是推断**。2026-08-02 全量扫过 30 把枪（`calibration/scan_compat.py`，run 在 `docs/compat/runs/20260802_155222/`），另外四个槽全部实测，`unverified()` 已清空；但 scope 槽**不画 tile**，存在性读不出来，`SlotDetector` 在那里返回 `unknown`。要确认得靠装一个瞄具。
- `EXCLUDE` / `ONLY` / `GRIP_ONLY` **一条都没实测**。「某个槽只收部分配件」（汤姆逊枪口只收消音）读不出来——收与不收留下的是同一个空 tile，只能逐个拖。

后两类都能自动闭环解决：`control/spawner.py` 的 `give_*` 能刷出任意物品，`control/inventory.py` 能装，`tab_items.detect` 能读回——**给什么就该读出什么，ground truth 是自己指定的**。

槽位存在性这一半已经写了：`calibration/scan_compat.py`，30 把枪 268 秒，纠正了 2 条会让拖拽静默落空的错条目（`ump45` 的 stock、`js9` 的 grip，两个位置都根本不画 tile）。配件级的那一半（哪个槽收哪些件）还没写，见 `calibrate-compat` skill。
