"""边玩边观测那条路的离线自检。不碰游戏、不碰 Pico、不写主库。

    pixi run play-selftest

造一组合成帧（一条已知的视角位移）+ 一个合成的手部计数器，喂给 `PlayObserver`，
检查它切出来的那一梭：起止对不对、预抓帧在不在、human 有没有被采到、失焦有没有
被丢掉、以及 `play_store` 转出来的 `Magazine` 是不是那一梭。

⚠ **它验的是管道，不是模型。** 相关器读得准不准、`human` 那一项减得对不对，都
不在这里 —— 那些要在游戏里、拿真的枪量。这个文件回答的是一个更基本的问题：
「一梭从人按下扳机到落进 jsonl，中间有没有哪一段是断的」。

⚠ 而它存在的直接理由是：这条路**没有别的办法验**。台上那条路可以拿一梭真数据
反复回放，实战这条路的输入是「人什么时候按的鼠标」，那个东西不可重放。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np

from capture.cropper import StillGrabber
from detector.view_tracker import ViewTracker
from control.play_observer import PlayObserver, PREFIRE_FRAMES, MIN_FRAMES
from calibration import play_store
from config import SCREEN_H, SCREEN_W


def _frames(n, step_px):
    """n 张全屏图，每张比上一张整体下移 step_px。

    随机纹理，因为相位相关要的是内容；`np.roll` 是纯平移，所以真值精确已知 ——
    这是这个文件唯一敢断言的数值。
    """
    rng = np.random.default_rng(7)
    base = rng.integers(0, 255, (SCREEN_H, SCREEN_W, 3), dtype=np.uint8)
    return [np.roll(base, int(round(i * step_px)), axis=0) for i in range(n)]


class _Poller:
    def __init__(self):
        self.left_held = False


def _run(hold_frames, step_px=3.0, human_per_frame=0, focus=True,
         pre=PREFIRE_FRAMES + 2, tail=6):
    """跑一梭，-> (magazines, observer)。同步驱动，不起线程。"""
    tracker = ViewTracker()
    total = pre + hold_frames + tail
    grab = StillGrabber(tracker.regions(), _frames(total, step_px))
    poller = _Poller()
    out = []
    human = {'y': 0}

    obs = PlayObserver(
        poller,
        on_magazine=lambda r, m: out.append((r, m)),
        meta_fn=lambda: {'weapon': 'mp5k', 'sight': 'red_dot',
                         'config': {'grip': 'vert_grip'}, 'posture': 'prone',
                         'fire_mode': 'auto', 'sight_asset': 'RedDot_01_C'},
        curve_fn=lambda refresh: [{'t_ms': 0, 'dx': 0, 'dy': 40.0},
                                  {'t_ms': 80, 'dx': 0, 'dy': 38.0}],
        mouse=_Mouse(human),
        tracker=tracker,
        grabber=grab,
        focus_fn=lambda: focus,
    )
    obs._ensure_mouse()
    obs._ensure_grabber()

    for i in range(total):
        poller.left_held = pre <= i < pre + hold_frames
        if poller.left_held:
            human['y'] += human_per_frame
        obs._tick()
        time.sleep(0.001)          # 让 perf_counter 真的往前走
    # 松开之后 _tick 靠 RELEASE_GAP_S 收尾；上面多跑的 6 帧覆盖不到那个墙钟间隔，
    # 所以显式收一次。真实循环里这一步由时间自己完成。
    if obs._rec is not None:
        obs._finish(time.perf_counter())
    return out, obs


class _Mouse:
    def __init__(self, d):
        self._d = d

    def human_totals(self):
        return (0, self._d['y'])

    def human_available(self):
        return True


def main():
    # 同 robot.py 开头：Windows 默认 cp1252 编码不了中文，而这里每一行输出都是
    # 中文。不设它的话第一个 print 就把整个自检炸掉。
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    fails = []

    def check(name, cond, detail=''):
        print(f'  {"OK  " if cond else "FAIL"} {name}' +
              (f'   {detail}' if detail else ''))
        if not cond:
            fails.append(name)

    print('一梭走完整条路')
    out, obs = _run(hold_frames=20, human_per_frame=0)
    check('交出了正好一梭', len(out) == 1, f'got {len(out)}')
    if out:
        res, meta = out[0]
        n = len(res.ts)
        check('预抓帧在里面', meta['n_prefire'] == PREFIRE_FRAMES,
              f"n_prefire={meta['n_prefire']}")
        check('帧数 = 预抓 + 按住 + 松开后那几帧', n >= 20 + PREFIRE_FRAMES,
              f'n={n}')
        check('hold_s 是正的', meta['hold_s'] > 0, f"hold_s={meta['hold_s']}")
        check('human_ok 被记下来了', meta['human_ok'] is True)
        check('曲线是读回的那条', len(meta['curve']) == 2)
        check('source=detected', meta['source'] == 'detected')

        mag = play_store.to_magazine(res, meta)
        check('枪名传下来了', mag.weapon == 'mp5k')
        check('config 只有后坐力槽', set(mag.config) == {'grip'}, str(mag.config))
        check('posture 传下来了', mag.posture == 'prone')
        check('K 取的是 live 表', mag.K > 0, f'K={mag.K}')
        check('comp_enabled 跟着曲线走', mag.comp_enabled is True)
        # ⚠ 这一条是整个文件里最要紧的：t 的原点必须是**点击**，所以预抓的那几帧
        # 必须是负的。原点错了，y_true = y_obs + C(t) 里的 C 就整体偏了，而残差
        # 看起来会完全正常 —— 那正是这个仓库付过最贵一次账的形状。
        check('预抓帧的 t 是负的', min(mag.t) < 0, f'min t={min(mag.t):.4f}')
        check('按下那一刻 t≈0',
              any(abs(x) < 0.02 for x in mag.t), f'closest={min(abs(x) for x in mag.t):.4f}')
        check('dy_px 比 t 少一个', len(mag.dy_px) == len(mag.t) - 1,
              f'{len(mag.dy_px)} vs {len(mag.t)}')
        check('human_dy 对齐 dy_px', len(mag.human_dy) == len(mag.dy_px))
        check('fire_delay_ms 留空（这条路没测过）', mag.fire_delay_ms is None)
        check('ads_end 留空（没人问过）', mag.ads_end is None)
        # ⚠ 这两条挡的是同一个失败：`_play_meta` 返回了字段而 `to_magazine` 没接。
        # 火力模式那一个会让 mg3 的两个自动档（循环射速差 1.50 倍）落进同一个
        # 文件，而文件名是靠 fire_tag 分开的。
        check('fire_mode 传到了 Magazine', mag.fire_mode == 'auto',
              f'fire_mode={mag.fire_mode!r}')
        check('sight_asset 传到了 Magazine', mag.sight_asset == 'RedDot_01_C',
              f'sight_asset={mag.sight_asset!r}')
        import calibration.samples as _S
        check('文件名带上了火力模式',
              _S.path_for(mag.weapon, mag.config, mag.fire_mode,
                          root=_S.PLAY_DIR)
              != _S.path_for(mag.weapon, mag.config, None, root=_S.PLAY_DIR)
              or not _S.fire_tag(mag.weapon, 'auto'),
              'fire_tag 对这把枪是空的，那就该一样')

    print('\n手动了 —— human 必须被采到，而且不是零')
    out, _ = _run(hold_frames=20, human_per_frame=5)
    check('交出了一梭', len(out) == 1)
    if out:
        mag = play_store.to_magazine(*out[0])
        nz = sum(1 for v in mag.human_dy if v)
        check('human_dy 有非零值', nz > 0, f'{nz}/{len(mag.human_dy)} 非零')
        check('手部计数总量对得上', abs(sum(mag.human_dy)) > 0,
              f'sum={sum(mag.human_dy):.1f}')

    print('\n失焦 —— 一梭都不许出来')
    out, obs = _run(hold_frames=20, focus=False)
    check('没有任何一梭落盘', len(out) == 0, f'got {len(out)}')

    # ⚠ 第一版这里断言「单发点射被挡下」，而它**不该**被挡下 —— 断言错了，不是
    # 代码错了。松开扳机之后观测器还在收帧（曲线停了但视角还在往回落），所以一次
    # 单发也能攒够 MIN_FRAMES。而那正是想要的：第一发是最有用的那个点之一，它
    # 「没有邻居可以借」，前面没有东西能把误差推给它。
    print('\n单发点射要留下来（第一发没有邻居可借，是最有用的点之一）')
    out, _ = _run(hold_frames=1, pre=PREFIRE_FRAMES)
    check('单发的一梭被记下来了', len(out) == 1, f'got {len(out)}')
    if out:
        check('它知道自己很短', out[0][1]['hold_s'] < 0.05,
              f"hold_s={out[0][1]['hold_s']}")

    print(f'\n而真的凑不够 {MIN_FRAMES} 帧的，一个 dy_px 都算不出来，不交出去')
    out, _ = _run(hold_frames=1, pre=0, tail=1)
    check(f'短于 {MIN_FRAMES} 帧的被挡下', len(out) == 0, f'got {len(out)}')

    print('\n没有枪名就不写盘')
    class _R:
        ts = [0.0, 0.01]; dy = [1.0]; human_dy = [0.0]; out_of_range = [False]
        # 每帧一个，所以比 dy 多一个 —— 这个替身要撑住的正是这条不变量。
        frame_ts = [0.0, 0.01]; reticle_y = [693.7, 694.1]; reticle_x = [1718.0, 1718.0]
        # 黑框是 per pair，所以和 dy 一样长 —— 替身要撑住的正是这条不变量。
        weapon_dy = [0.4]; weapon_dx = [0.0]
    check('store() 返回 None',
          play_store.store(_R(), {'t0': 0.0, 'weapon': ''}) is None)

    print()
    if fails:
        print(f'{len(fails)} FAILED: ' + ', '.join(fails))
        return 1
    print('all good')
    return 0


if __name__ == '__main__':
    sys.exit(main())
