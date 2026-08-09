"""把实战里观测到的一梭写进影子库。

`control/play_observer.py` 看着人打，交出 `(MagazineResult, meta)`；这里把它
变成 `samples.Magazine` 写盘。**只做转换和落盘，不判优劣** —— 判「这一梭能不能
用」归 `harness/verdict.py`，那条规矩是「取测量的人不判测量」。

## 为什么是 `PLAY_DIR` 而不是 `SAMPLE_DIR`

格式一模一样，根不一样。理由不是「实战梭更脏」，是**它们说不清自己是什么**：

    台上          打架中
    读回配件      开不了 Tab
    读回镜位      开不了 Tab
    读回架子      开不了 Tab

台上每一格的闸门都是「同一个东西两个独立说法，对不上就拒绝」。实战里第二个说
法根本不存在，只有检测器一个。`meta['source'] == 'detected'` 记的就是这件事，
而 `CaptureRun.labelled()` 用同一个区分挡掉过一整批坏标签。

格式相同是刻意的：哪天它们够格进主库，那是**移动文件**，不是转换格式。

## ⚠ 这些梭现在还不能喂拟合，而且原因不止一个

1. **`y_true(t)` 固定**是 MODEL.md §2.1 的**前提**，不是结论。台上姿态/配件/
   镜子/满弹匣全是控制变量；实战里每梭都在变。一批混着不同 `y_true` 的梭，
   聚类看起来会完全正常。
2. **「聚类取最大簇」的前提是大多数梭干净。** 台上成立。实战里被打断、被撞、
   掉出 ADS、目标在动导致人手大幅修正 —— 干净的可能是少数，而 5 对 5 等量
   分裂时「取最大簇」等于掷硬币（`calibration/CLAUDE.md`）。
3. **MODEL.md §4.1 那道唯一的外部检验会失效。** 它靠逐梭轮换不同强度的曲线，
   各自加回 `y_comp` 之后必须给同一个 `y_true` —— 拟合器伪造不了。实战默认是
   单臂的，而 `verdict.py` 明写「一条臂 = 没验过 ≠ 通过」。

**所以第一个该拿它们干的事是验证，不是拟合**：用台上拟的曲线预测实战的
`y_obs`，看对不对得上。那验的正是第 1 条 —— 台上的 `y_true` 在实战里还是不是
同一条 —— 而那个问题**只能在实战里回答**。
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from calibration import samples as S
from config import RECOIL_COMP_LAG_MS, RECOIL_SIGHT_PROFILES


def to_magazine(result, meta):
    """`(MagazineResult, meta)` -> `samples.Magazine`。不写盘。

    `meta` 认这些键（缺的都有诚实的默认值，**没有一个是猜的**）:

        weapon config posture sight fire_mode magazine_size
        curve            —— 固件**读回**的时刻表，不是上传值
        t0 hold_s human_ok source note

    ⚠ `curve` 必须是读回值。`collect_timed.py:850` 那行原来是
    `curve = rig.arm(w)`，改成 `rig.mouse.read_pattern()` 是因为上传的和设备里
    实际会播的不是同一个东西 —— `upload_pattern` 的折叠把每一个负偏移都塌成
    `curve[0]['t_ms'] == 0`。拿上传值当记录，就是记录描述了另一个对象。
    """
    t0 = meta.get('t0') or 0.0
    # ⚠ `meta['frame_ts']`，**不是 `result.ts`**。
    #
    # `MagazineRecorder.finish()` 的 `res.ts` 从 `i=1` 起（view_tracker.py:293），
    # 所以它是每个帧**对**的后一帧，只有 n-1 个。台上存进 `Magazine.t` 的是
    # `collect_timed.measure()` 的 `kept` —— 全部 n 个帧时刻，而 `dy_px[i]` 描述
    # `t[i] → t[i+1]`。拿 res.ts 当 t，等于把每一段位移标在它的**结束**时刻上：
    # 100 fps 下 10 ms 的系统性偏移，而 M = 20 ms。残差看起来会完全正常。
    #
    # 自检 `tools/test_play_observer.py` 的「dy_px 比 t 少一个」就是这一条。
    ts_abs = list(meta.get('frame_ts') or result.ts)
    # 相对**点击**，和 fire.py:189 同一个原点。预抓的那几帧因此是负的，这是对的:
    # 它们本来就在扳机之前。
    t = [float(x) - float(t0) for x in ts_abs]

    sight = meta.get('sight') or ''
    # 存 live 值，和 analysis_k() 读的是同一张表 —— 存一个和分析时不同的 K，正是
    # samples.analysis_k 那段数出来的 35% 污染。
    k = (RECOIL_SIGHT_PROFILES.get(sight) or {}).get('K') or 0.0

    curve = list(meta.get('curve') or [])
    span = (max(t) - min(t)) if len(t) > 1 else 0.0

    note = meta.get('note') or ''
    # ⚠ 这一段是记录，不是装饰。一梭在库里躺三个月之后，「它是怎么来的」只剩
    # 这一行 —— 而实战梭和台上梭长得一模一样。
    tags = [f"play source={meta.get('source', '?')}",
            f"human_ok={meta.get('human_ok')}",
            f"frames={meta.get('n_frames', len(t))}",
            f"prefire={meta.get('n_prefire', 0)}",
            f"span_s={meta.get('span_s')}"]
    # ⚠ `comp_enabled=False` 有两个成因，而它们在库里长得一模一样：这一梭真没压
    # 枪，或者曲线缓存还没刷新到。后者是这个模块自己的缺陷，不是那一梭的性质。
    if not curve and not meta.get('curve_fresh', True):
        tags.append('curve=STALE(未刷新，不是没压枪)')
    if meta.get('meta_error'):
        tags.append(f"meta_error={meta['meta_error']}")
    note = ' '.join(tags) + ((' | ' + note) if note else '')

    return S.Magazine(
        weapon=meta.get('weapon') or '',
        sight=sight,
        K=float(k),
        config=dict(meta.get('config') or {}),
        posture=meta.get('posture') or 'standing',
        curve=curve,
        comp_enabled=bool(curve),
        t=[float(x) for x in t],
        dy_px=[float(x) for x in result.dy],
        human_dy=[float(x) for x in result.human_dy],
        oor=[bool(x) for x in result.out_of_range],
        magazine_size=int(meta.get('magazine_size') or 0),
        hold_s=float(meta.get('hold_s') or 0.0),
        # ⚠ 漏掉这一行，一把枪的两个自动档就落进同一个文件。mg3 的两个自动档
        # 循环射速差 **1.50 倍**，而 `path_for` 正是靠 `fire_tag(weapon,
        # fire_mode)` 把它们分开的。`calibration/CLAUDE.md` 记着这个失败：
        # 「`ensure_fire_mode` 早就写好，采集路径一次都没调过 —— 而没有一梭记得
        # 自己是哪一档」。清死代码时发现的：`_play_meta` 一直在返回它，这里一直
        # 没接。
        fire_mode=meta.get('fire_mode'),
        # 同一次发现的第二个。`sight` 是归一化的名字，`sight_asset` 是枪上那个
        # 件的原始资产名 —— 前者对不上时，后者是唯一能说出「它到底戴的什么」的
        # 东西。
        sight_asset=meta.get('sight_asset') or '',
        # 和台上同一个常数、同一个理由：它是「一个 count 今天在这台机器上花多久
        # 到屏幕」，盖在开火时刻上，不是读回来的时候查表。
        comp_lag_s=RECOIL_COMP_LAG_MS / 1000.0,
        # ⚠ 台上盖的是 fire_delay_ms，这里**故意留 None**。那个数说的是「本仓库
        # 发的 click 和固件起播之间差多少」，而这一梭的 click 是人按的，走的是
        # 另一条路（鼠标 → Pico → Windows 输入队列 → KeyPoller 的 5 ms 轮询）。
        # 台上那个值在这条路上没有测过，盖上去就是拿一条路的数描述另一条路。
        fire_delay_ms=None,
        fps=(len(t) - 1) / span if span > 0 else float('nan'),
        # ⚠ 实战里没有东西读 ADS。台上是扳机松开时 `rig.gun.in_ads()` 读一次，
        # 而 robot 没注册 AdsDetector。None 是「没人问过」，不是「没开镜」——
        # 后者会是 False，而那是个断言。
        ads_end=None,
        ts=datetime.now().strftime('%m%d_%H%M%S'),
        note=note,
    )


def store(result, meta, root=None):
    """转换 + 落盘。-> 写进了哪个文件，或者 None（枪名都没有就不写）。

    ⚠ **没有枪名就不写。** 一个 `weapon=''` 的梭会落进 `__bare.jsonl`，和别的
    无名梭混在一个文件里，然后看起来像一格数据。这个仓库为「记录描述的对象不是
    被测量的那个对象」付过五次账，每一次都不抛异常、印出来的数也全都正常。
    """
    mag = to_magazine(result, meta)
    if not mag.weapon:
        return None
    return S.append(mag, root=root or S.PLAY_DIR)
