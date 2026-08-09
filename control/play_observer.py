"""边玩边观测 —— 人自己按扳机的那一梭，也记下来。

`calibration/collect_timed.py` 那条路是机器自己发 click、自己数帧、自己停：
`control/fire.py:fire_magazine()` 从头到尾知道扳机什么时候下去，因为扳机是它
按的。实战里按扳机的是人，所以那条路一梭都采不到，而 `robot.py` 到今天为止
**只播不测** —— 它装了七个检测器、一个抓帧环，没有一个 ViewTracker。

这个类补上那一半：被动地看着人打，一梭结束交出一个 `MagazineResult`。

## 为什么这条路在物理上成立

Pico 是 USB 直通，**人手的每一个 count 都必然经过它**。所以 `human` 不是要估
计的量，是可观测量 —— 固件 `main.c:455-463` 的注释就是这个模块的意图声明：

    Reporting it lets the PC subtract it exactly -- and without that the curve
    can only ever be learned while sitting perfectly still, which defeats the
    point of learning it from real play.

`y_obs = Σ dy_px/K + human` 里那一项从四个月前写下来就是为了这一刻。

## ⚠ 而它此刻的状态是「符号已验，幅度未验」

MODEL.md §3：`|slope|` 0.0464 px/count 对 red_dot 的 K
（`config.RECOIL_SIGHT_PROFILES`），**差 33 倍**。
操作员 2026-08-09 判定那次测量废于工况（探针要求慢速小幅，而实际是猛甩），
**据此决定按 `counts = dy/K + human` 正常推进，不重测**。

这是一个基于工况证词的判断，不是一次测量。写在这里是因为**这个模块是它第一
个承重的地方** —— 台上 131146 个帧间隔里只有 2 个非零，而这条路上每一梭都非
零。如果那个判断错了，症状不会是异常，是一批看起来完全正常的 `y_true`。

## 这一层不落盘，也不分析

`on_magazine(result, meta)` 交出去就完。落盘归 `calibration/`（分层规则：
`control -> detector, control -> press`，反向没有边），判「这一梭能不能用」
归 `harness/verdict.py` —— 那条规矩是「取测量的人不判测量」。

## ⚠ 实战梭的元数据全部是检测器读的，没有第二个来源

台上每一格都读回配件、读回镜位、读回架子，因为闸门是「同一个东西两个独立
说法」。**打架中途开不了 Tab**，所以这里的 weapon / attachments / posture 全
部只有检测器一个说法。`meta['source'] = 'detected'` 是这个事实的记录，不是
装饰 —— `CaptureRun.labelled()` 用同一个区分挡掉过一整批坏标签。

所以这些梭**不进主库**。谁调用谁决定写哪儿，而这个模块拒绝替它决定。
"""
import time
from collections import deque

from daemon_loop import DaemonLoop
from capture.cropper import make_grabber
from detector.view_tracker import ViewTracker, MagazineRecorder

# 和 fire.py 同一个数，同一个理由：第一发的踢腿要有个地方去量。开火之后才开始
# 记，第一发就跟「开火前视角在干什么」共用一个窗口 —— 曲线曾因此给自己的第一
# 发写了 -0.6 counts，而拟合读成「这一发几乎不踢」，写回去之后下一次测量同意了。
PREFIRE_FRAMES = 3

# 松开扳机多久算一梭结束。人会点射，而点射之间的间隔比这短的时候，把它们并成
# 一梭是错的 —— 固件的 `firing` 一松就停播曲线（main.c:405），所以两次点射之间
# y_comp 是断的，而并成一梭的记录说它是连的。宁可切碎。
RELEASE_GAP_S = 0.08

# 一梭的上限。比任何一把枪的弹匣都长（m416 3.81 s，mp5k 2.99 s），存在的理由
# 只是防止一个卡住的左键把 recorder 撑到 max_frames。
MAX_BURST_S = 8.0

# 短于这个的不交出去。不是「质量不够」—— 是 `MagazineRecorder.finish()` 至少
# 要两帧才有一个 dy_px，而单发点射在 GDI 的 48 fps 下可能只有一帧。
MIN_FRAMES = 4


class PlayObserver(DaemonLoop):
    """看着人打，一梭一个 `MagazineResult`。不落盘、不判优劣、不碰游戏。

    poller       —— `capture.key_poller.KeyPoller`，左键状态的来源
    on_magazine  —— `(MagazineResult, meta: dict) -> None`，一梭结束时调用
    meta_fn      —— `() -> dict`，**在扳机按下那一刻**取的上下文快照
    mouse        —— 有 `human_totals()` 的对象；None 就自己去要

    ⚠ 左键读的是 `KeyPoller.left_held`，不是固件的 `firing`。

    固件有 `firing`（`main.c:102`，它就是曲线播放的开关）而且它是唯一一个和
    `human_total` 共时钟的信号，但 `[hid] x y now` 三个字段里没有它 —— PC 拿
    不到。`left_held` 是 5 ms 轮询的 `GetAsyncKeyState`，和固件的 `firing`
    之间差着 USB 转发 + Windows 输入队列 + 轮询周期，**而那个差没有测过**。

    台上的原点是本仓库自己发的 click（`fire.py` 里 `t0 = perf_counter()` 紧跟
    `mouse.click()`），这里的原点是人按下之后 PC 察觉到的时刻。两个都是 PC 的
    `perf_counter`，但**不是同一段路**。M = 20 ms 是台上那条路测的。

    这是这个模块已知最大的一个未测量，写在这里而不是留给下一个人去发现。
    """

    def __init__(self, poller, on_magazine, meta_fn=None, curve_fn=None,
                 mouse=None, tracker=None, grabber=None, focus_fn=None):
        super().__init__()
        self.poller = poller
        self.on_magazine = on_magazine
        self.meta_fn = meta_fn
        # `curve_fn(refresh)` -> 固件里那条曲线。**空闲时刷新，开火时只取缓存**：
        # 一次 read_pattern 是 40 行串口往返，在开火那一刻做就是丢掉第一发的帧。
        self.curve_fn = curve_fn
        self._curve = []
        self.tracker = tracker or ViewTracker()
        # 注入 grabber 是为了离线自检 —— `capture.cropper.StillGrabber` 喂存好的
        # 帧，整条路（左键沿 → 预抓 → finish → meta）就能在没有游戏的情况下跑。
        self._grabber = grabber
        self._own_grabber = grabber is None
        self._paced = False
        # ⚠ 失焦时抓到的不是游戏，而左键在别的窗口里也照样按得下去。
        #
        # 台上那条路整轮独占前台（`control/focus.py:take_foreground()` 存在的
        # 理由就是「把人从回路里删掉」），实战这条路正相反 —— 人会 alt-tab 去看
        # 一眼别的东西，而在那边点一下鼠标就会被记成一梭。那一梭的 dy_px 是浏览
        # 器滚动，weapon 却是游戏里最后拿的那把枪：**记录描述的对象不是被测量的
        # 那个对象**，而且它不抛异常、印出来的数也完全正常。
        self.focus_fn = focus_fn
        self.n_unfocused = 0
        self._mouse = mouse
        self._human_fn = None
        self._human_ok = False
        # 开火前的 patch。存切好的而不是整帧：一帧是全屏，一个 patch 组是几个
        # 256 px 的条，而这个环一直在转。
        self._prefire = deque(maxlen=PREFIRE_FRAMES)
        self._rec = None
        self._t0 = None
        self._meta = None
        # ⚠ push 成功的每一帧的时刻，**全部 n 个**。
        #
        # 不能用 `finish()` 的 `res.ts`：那里 `i` 从 1 起（view_tracker.py:293），
        # 所以它是每个帧**对**的后一帧，只有 n-1 个。台上存进 Magazine.t 的是
        # `measure()` 的 `kept` —— 全部 n 个帧时刻，`dy_px[i]` 描述 t[i]→t[i+1]。
        # 拿 res.ts 当 t 就是把每一段位移标在它的**结束**时刻上，在 100 fps 下
        # 是 10 ms 的系统性偏移，而 M = 20 ms。残差看起来会完全正常。
        self._kept = []
        self._last_held = 0.0
        self.n_bursts = 0
        self.n_short = 0

    # ── 装配 ──

    def _ensure_mouse(self):
        """晚绑定，而且**拿不到就说出来**。

        `human_available()` 在旧固件上是 False，而它存在的全部理由就是让调用方
        能说「我不知道手动没动」而不是把静止当真值。全仓至今没有一个调用方用
        过它 —— 这是第一个。
        """
        if self._human_fn is not None:
            return
        if self._mouse is None:
            from press.pico_mouse import get_mouse
            self._mouse = get_mouse()
        fn = getattr(self._mouse, 'human_totals', None)
        avail = getattr(self._mouse, 'human_available', None)
        self._human_ok = bool(avail()) if callable(avail) else False
        if fn is None:
            print('[play] 固件不上报 human —— 这一轮的每一梭都会把手的动作'
                  '记成后坐力。不采。', flush=True)
            self._human_fn = False
            return
        if not self._human_ok:
            print('[play] Pico 还没发过一行 [hid]；先按原样采，收到第一行'
                  '之前的梭在 meta 里标 human_ok=False', flush=True)
        self._human_fn = fn

    def _ensure_grabber(self):
        if self._grabber is None:
            self._grabber, self._paced = make_grabber(self.tracker.regions())
        if self.focus_fn is None:
            from control.focus import game_focused
            self.focus_fn = game_focused

    # ── 主循环 ──

    def _loop(self):
        self._ensure_mouse()
        if self._human_fn is False:
            return
        self._ensure_grabber()
        while self._running:
            try:
                self._tick()
            except Exception as e:
                # 一梭观测炸了不该带走 robot 的压枪。但也不许静默 —— 一个
                # 采不到东西的观测器和一个正常但没人开火的观测器，日志里
                # 长得一模一样。
                print(f'[play] tick failed: {e!r}', flush=True)
                self._reset()
                time.sleep(0.2)

    def _tick(self):
        frame = self._grabber.grab()
        ts = time.perf_counter()
        held = bool(getattr(self.poller, 'left_held', False))
        focused = True
        if self.focus_fn is not None:
            try:
                focused = bool(self.focus_fn())
            except Exception:
                focused = True     # 问不出来就别替它回答「没焦点」

        if not focused:
            # 作废，不是保存。半梭在游戏里、半梭在别的窗口里的记录，比没有记录
            # 更坏 —— 它看起来完全正常。
            if self._rec is not None:
                self.n_unfocused += 1
                self._reset()
            self._prefire.clear()
            time.sleep(0.05)
            return

        if held:
            self._last_held = ts
            if self._rec is None:
                self._start(ts)
            if self._rec.push(ts, frame):
                self._kept.append(ts)
            if ts - self._t0 > MAX_BURST_S:
                self._finish(ts)
            return

        if self._rec is not None:
            # 松开之后再收一小段：曲线停了，但视角还在往回落，而 hold_s 之后
            # 那几帧正是「固件停播」这件事在屏幕上的样子。
            if self._rec.push(ts, frame):
                self._kept.append(ts)
            if ts - self._last_held >= RELEASE_GAP_S:
                self._finish(ts)
            return

        p = self.tracker.slice_frame(frame)
        if p is not None:
            self._prefire.append((ts, p))
        # 空闲分支是唯一允许碰串口的地方。曲线在这里刷新，开火时直接用。
        if self.curve_fn is not None:
            try:
                self._curve = self.curve_fn(True)
            except Exception as e:
                print(f'[play] curve refresh failed: {e!r}', flush=True)
        if not self._paced:
            time.sleep(0.002)

    def _start(self, ts):
        self._rec = MagazineRecorder(
            self.tracker, human_fn=self._human_fn or None)
        self._kept = []
        # 预抓的帧先进去，然后才是 t0 —— 顺序反了 recorder 里的时间就不是单调的。
        for pts, patches in self._prefire:
            if self._rec.push_patches(pts, patches):
                self._kept.append(pts)
        self._n_prefire = len(self._kept)
        self._t0 = ts
        # ⚠ 快照取在**按下那一刻**，不是结束时。人在一梭里换枪、趴下、开镜都
        # 是几百毫秒的事，而结束时读到的那个说的是别的对象。
        try:
            self._meta = dict(self.meta_fn()) if self.meta_fn else {}
        except Exception as e:
            self._meta = {'meta_error': repr(e)}
        # 缓存值，不刷新 —— 见 curve_fn 的注释。空的意味着「这一梭没压枪」，
        # 而那**可能是假的**（刷新还没轮到），所以它连着 curve_fresh 一起记。
        self._meta.setdefault('curve', list(self._curve))
        self._meta['curve_fresh'] = bool(self._curve)

    def _finish(self, ts):
        rec, t0, meta = self._rec, self._t0, self._meta
        # ⚠ 读在 _reset() **之前**。第一版把 n_prefire 读在后面，而 _reset 清空
        # 那个 deque —— 落盘的每一梭都说自己有 0 个预抓帧，而它们其实都在里面。
        # 一个安静的错记录，自检抓到的第一个。
        kept, n_pre = list(self._kept), getattr(self, '_n_prefire', 0)
        self._reset()
        n = len(kept)
        if n < MIN_FRAMES:
            self.n_short += 1
            return
        try:
            result = rec.finish()
        except Exception as e:
            print(f'[play] finish failed after {n} frames: {e!r}', flush=True)
            return
        meta = dict(meta or {})
        meta.update({
            # 谁说的。台上是「要求并读回」，这里只有检测器一个说法。
            'source': 'detected',
            't0': t0,
            # ⚠ 全部 n 个帧时刻，绝对值。落盘那一层减 t0 —— 和台上
            # `measure()` 的 `kept` 是同一个东西，理由在 _reset 里 self._kept。
            'frame_ts': kept,
            'hold_s': round(self._last_held - t0, 4),
            'span_s': round(ts - t0, 4),
            'n_frames': n,
            'n_prefire': n_pre,
            # False 意味着这一梭的 human_dy 全是 0 而**手可能动了**。
            'human_ok': self._human_ok,
        })
        self.n_bursts += 1
        try:
            self.on_magazine(result, meta)
        except Exception as e:
            print(f'[play] on_magazine failed: {e!r}', flush=True)

    def _reset(self):
        self._rec = None
        self._t0 = None
        self._meta = None
        self._prefire.clear()

    def close(self):
        if not self._own_grabber:
            return                 # 注入的 grabber 归注入的人关
        g, self._grabber = self._grabber, None
        if g is not None and hasattr(g, 'close'):
            try:
                g.close()
            except Exception:
                pass
