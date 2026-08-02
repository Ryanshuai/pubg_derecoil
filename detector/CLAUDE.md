# detector/ — 检测层

从屏幕像素得到游戏状态。上游是 `screen_capture` / `cropper` 给的 crop，下游是 `robot`（压枪）和 `calibration/attach_control`（自动装配件）。

三种节奏，混淆了会付出代价：

| | 谁 | 频率 | 预算 |
|---|---|---|---|
| 常驻 | `HUD_REGIONS` 里的一切（weapon/fire_mode/posture/ammo） | 每帧 ~144fps | 微秒级 |
| 事件触发 | Tab 界面、spawner 界面 | 按键时一次 | 几十毫秒可接受 |
| 状态轮询 | 在不在局内（`lobby_detector`） | 1–2 Hz | 几十毫秒可接受 |

**不要把事件触发的区域塞进 `config.HUD_REGIONS`。** 那套每帧都抓，而 DXGI 后端只能用**一个** bounding box。「附近」栏在 x=576，加进去会把 bbox 从 x=937 一路拉到 576，变成每帧多拷 46 万像素——只为一个按住 Tab 才存在的面板。事件触发的用自己的 grabber（见 `tab_items.TabGrabber`）。

---

## 干活之前先问：现在在局内吗

`lobby_detector.py` + `lobby_control.py`（**已有，别重造**）。测量值全在 `docs/lobby/README.md`。

```python
from detector.lobby_control import LobbyControl
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

**别用固定 `sleep` 等游戏。** 结算页自己走 ~18 秒、大厅→局内要过匹配+加载，时长都不是常数。`lobby_control` 全程轮询，`EXIT_TIMEOUT`/`ENTER_TIMEOUT` 是放弃阈值不是预期耗时。`Pointer` 懒构造，只读状态不会去占 Pico 串口。

---

## 还要问：焦点在游戏上吗

`press/pointer.raise_game()`（**已有，别重造**）。

从终端拉起的工具，t=0 时焦点在终端不在游戏，第一次 `game_focused()` 必然 False。`harvest.py` 靠 `--countdown` 让人手动切窗口——那就是那些 run 至今不能真正无人值守的原因。

```python
from press.pointer import ensure_focus, focus_keeper
if not ensure_focus(countdown_s=6):    # 抢→验→重试3次→倒计时兜底
    return 1
time.sleep(0.6)                        # 切前台后头几帧游戏不收输入
...
if not focus_keeper().ok('mag 3'):     # 跑到一半掉了就抢回来，上限 5 次
    break
```

**倒计时是退路，不是手段。** 全项目已接：harvest / sweep / collect_icons /
spawner_control / attach_control / lobby_control。

三个坑：

- **裸 `SetForegroundWindow` 会被拒。** Windows 只允许当前前台进程交出焦点，别的进程调它只会让任务栏闪一下。`raise_game()` 用 `AttachThreadInput` 借前台线程的输入队列绕过去；不模拟 ALT 键，因为 ALT 在这游戏里是自由视角。
- **调完必须再验一次 `game_focused()`。** 它可以不报错地失败。
- **焦点不是拿到一次就固定的。** 终端会反复抢回去，期间发出的按键直接丢失，症状是「spawner 面板打不开」，而脚本第一行明明打印了 `focused=True`。关键操作要**每次重试前重新抢**，别只在开头抢一次。

窗口有焦点 ≠ 游戏在收输入：标题匹配在大厅、加载页、结算页全都成立。焦点检查之外还要过上面那关 `lobby_control`。

## 第一铁律：模板漂移是静默的

检测器**不会**因为模板过时而报错。它会给出一个看起来完全合理的错答案。

实例：`Lower_ThumbGrip_C` 与当前游戏画的拇指握把已经对不上，于是 Mk12 的握把槽读成 `laser`——也是握把、也在候选里、margin 还不低。下游 `attach_control` 靠读回槽位确认装配成功，拿到这个结果会误判失败并重试。

这条是 2026-08-01 端到端验证时偶然撞见的，不是任何机制报出来的。**游戏每次更新都会产生这类漂移**，所以：

- 改完检测器，跑参考截图对比，别信"看起来没问题"
- 报告准确率时必须说明用的是哪个样本集，以及集里有没有难例
- 发现漂移 → 见下方「坏了找谁」

## 第二铁律：几何声明要端到端验证

坐标不能目测。验证方式是**让模板匹配去证明**：坐标对了，装了东西的槽全部认出、空槽全部判空、MSE 有量级差距；坐标偏了，模板匹配会先劣化再失败。

`temp_debug/verify_tab_slots.py` 是这个套路的样板。标定图标几何用 `temp_debug/calib_inv_icon.py`：拿已知答案的样本扫参数，按"认对几个"排序，MSE 只用来打破平局。

---

## 踩过的坑（都有实测数字）

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
| `docs/lobby/*.png` | 5 张：大厅 / 训练场 / 训练场+Tab / 正式局结算 / ESC 菜单 | 在不在局内的回归，`tools/verify_lobby_detector.py` 五条全过。每张的 `bar_max`/`ping_frac` 实测值见 `docs/lobby/README.md` |
| `docs/ads/runs/**/*.jpg` | 610 张全屏帧（本为 ADS 采集） | 顺带是弹药数字的离线回归集：`tools/probe_ammo_ocr.py` 在 921 张里读出 869，其余 52 张确实没数字 |
| `docs/ads/runs/*/index.jsonl` | 每帧标了 scope / state / t_ms / 槽位实读 asset | 开镜检测评测集，492 帧。**别照 `state` 当真值**：`20260801_222936` 的 `state=ads` 其实是按住右键的肩瞄、从未开镜；`20260802_015545` 整轮在错的槽位上。两个 run 的 `meta.json` 里都写了原因，`calibration/fit_ads_detector.py` 顶部的 `NOT_SCOPED` / `SCOPED` 是修正后的真值 |

槽位坐标固定：枪没有的槽只是**不画边框**，不会挪位（UZI 无 grip / Mk12 无 stock 实拍确认）。所以拖拽目标坐标是安全的。

## 坏了找谁

| 现象 | skill |
|---|---|
| 图标认不出 / 认错 | `calibrate-icon`（位置、尺度、alpha、混合）或 `calibrate-template`（重提模板 + 全集混淆检查） |
| 枪名、`类型` 之类 UI 文字读不出 | `calibrate-template` |
| 行距、槽位、面板位置对不上 | `calibrate-screen` |

游戏机制类的坑（切枪退出开镜、姿势图标只在开镜时渲染等）记在 `docs/game_quirks.md`，不要在这里重复。

## 当前已知缺陷

- `ammo_detector` 的十个字模全部采自**三位数**（150..121）。两位、一位实测逐个读对，但它们从没被单独重采过；哪天游戏改成按位数用不同字号，会先在这里翻车，重跑一次 `--verify` 就能看出来。
- `ads_detector` 的 492 帧全部来自 **Kar98k、同一片场地、全程未开火**。没验过的：开火时后坐力抖动会不会糊掉准星（最可能翻车的一条，压枪场景恰恰全程在开火）、其他枪的腰射准星是否同形、载具/趴姿等会改准星的状态、以及 4 倍以下的其他红点变体。要上压枪主循环，先补一组**开火中**的帧
- `lobby_detector` 的四态里，**加载页一张样本都没有**——`FULLBLEED` 现在被结算页和退出确认框覆盖，它对加载页的判定仍是照定义推的（活体转移里观测到了，没存图）。另外 **正式局的 ESC 菜单**没采过，`leave_entry_confirmed()` 届时会失配、拒绝点击，所以**正式局现在退不出来**（训练场可以）。补齐跑 `pixi run python tools/probe_lobby_transition.py`，全程截图落 `docs/lobby/runs/<n>/`。`lobby_control.py` 顶部的 `OBSERVED DURATIONS` 三项也还是空的
- `Lower_ThumbGrip_C`、`Stock_UZI_C` 已漂移
- 枪口制退器、重型枪托、多倍率混合瞄具**没有模板**（游戏后加的）
- `attachment_catalog.unverified()` 列出的 6 把枪槽位仍是推断：dragunov / famas / js9 / k2 / mp9 / p90

后两类都能自动闭环解决：`spawner_control.give_*` 能刷出任意物品，`attach_control` 能装，`tab_items.detect` 能读回——**给什么就该读出什么，ground truth 是自己指定的**。这个自检还没人写。
