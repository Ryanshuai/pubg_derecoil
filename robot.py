"""Robot — assembly entry point.

Creates components, registers detectors, starts threads. No business logic.
All behavior is driven by config tables.

⚠ IT ALSO KEEPS A LOG, AND UNTIL 2026-08-09 IT DID NOT. Everything this
process knows about why it is not compensating -- which curve it looked up,
which one it could not find, which part it could not name, whether the upload
to the Pico raised -- was printed to a terminal and then scrolled away. The
first time somebody said "I just tried it and it does not hold the gun down",
the answer was already gone.

That is the same shape as the rule in the root CLAUDE.md about not grepping a
live run: a session you cannot replay is the one session whose evidence you
must not throw away. A play session is exactly that.

⚠ AND THE LOG IS NOT THE TERMINAL, since 2026-08-09. `logbook.note` writes to
the file only, and the routine per-event chatter goes through it: measured on
two real play logs, `[tab]` and `[state]` were 79% and 82% of what reached the
screen, which scrolled the status table -- the only thing a player reads while
playing -- off the top between every pair of Tab presses. The lines are all
still in the file. See logbook.py.
"""
import sys

# Force UTF-8 stdout so CN attachment names don't crash print_status
# (Windows defaults to cp936/cp1252 which can't encode them). Must run
# before any module-level print.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass


import os

from logbook import start_log
from detector.game_state import GameState
from detector.weapon_hud_detector import WeaponHudDetector
from detector.fire_mode_detector import FireModeDetector
from detector.gun_tag_detector import GunTagDetector
from detector.posture_detector import PostureDetector
from detector.highlight_detector import HighlightDetector
from detector.tab_detector import TabTypeDetector
from detector.weapon_template_detector import TabWeaponDetector
from detector.attachment_detector import AttachmentDetector
from capture.screen_capture import ScreenCapture
from capture.key_poller import KeyPoller
from control.match import Dispatcher
from control.play_observer import PlayObserver
from calibration import play_store
from config import PLAY_OBSERVE


def _play_meta(state):
    """扳机按下那一刻，这把枪是什么 —— **按检测器的说法**。

    ⚠ 每一个字段都只有一个来源。台上每一格都读回配件、读回镜位、读回架子，因为
    闸门是「同一个东西两个独立说法，对不上就拒绝」；打架中途开不了 Tab，所以这里
    第二个说法根本不存在。`source='detected'` 由观测器盖上，这个函数不假装它有。

    ⚠ `magazine_size` 留 0 而不是猜一个。它是弹匣的身份（`samples.Magazine` 那段
    实测：magazine 槽的图标 MSE 591.9 对亚军 874.0，1.48x，读不出来），而实战里
    没有「换完弹读一次计数器」这个动作。0 的意思是没人问过。
    """
    w = state.active
    from detector.weapon import _sight_of
    try:
        sight = _sight_of(w.scope, w.name) or ''
    except Exception:
        sight = ''
    return {
        'weapon': w.name or '',
        # RECOIL_SLOTS 的三个槽，且只有这三个。`scope` 不在里面 —— 光学件走
        # `sight` 改 K，不改曲线（collect_timed.py 的 RECOIL_SLOTS 那段）。
        # ⚠ Weapon 管枪托叫 `butt`，池化键叫 `stock`。翻译在这一行，只此一处。
        'config': {k: v for k, v in (('muzzle', w.muzzle), ('grip', w.grip),
                                     ('stock', w.butt)) if v},
        'posture': w.posture or state.posture or 'standing',
        'sight': sight,
        'sight_asset': w.scope or '',
        'fire_mode': w.fire_mode or state.fire_mode or None,
        'magazine_size': 0,
    }


class Robot:
    def __init__(self):
        self.state = GameState()
        # ⚠ There was a `device = torch.device(...)` here until 2026-08-08.
        # FireModeDetector was the last torch user in the whole detector graph,
        # and it is a RandomForest now. Nothing on the frame path imports torch.

        # Components
        self.capture = ScreenCapture()
        self.poller = KeyPoller()
        self.dispatcher = Dispatcher(self.state, self.capture, self.poller)

        # Register detectors
        self.dispatcher.register('weapon_hud', WeaponHudDetector())
        self.dispatcher.register('fire_mode', FireModeDetector())
        self.dispatcher.register('posture', PostureDetector())
        self.dispatcher.register('highlight', HighlightDetector())
        self.dispatcher.register('tab_type', TabTypeDetector())
        self.dispatcher.register('tab_weapon', TabWeaponDetector())
        self.dispatcher.register('tab_attachment', AttachmentDetector())
        # ⚠ THIS IS WHAT DECIDES `tab_open` NOW, not `tab_type`. The boxed slot
        # number lives inside the weapon block TabWatch already grabs, so the
        # openness judgement and the loadout come off one frame at one instant;
        # `tab_type` is 1282 px away and was a second grab 4 ms later. It also
        # answers the sharper question -- panel up AND a gun in that slot --
        # which is the actual precondition for reading a loadout.
        self.dispatcher.register('gun_tag', GunTagDetector())

        # 边玩边观测。⚠ 它只**记**，一行压枪的行为都不改：没有它 robot 播的是
        # 同一条曲线，有它也一样。这一点是刻意的 —— 一个既观测又影响被观测对象
        # 的回路，出问题时分不出是哪一半。
        #
        # ⚠ 默认关。理由整段在 config.PLAY_OBSERVE，一句话是：实战梭的身份只有
        # 检测器一个来源，而检测层还读不准 —— 于是每一梭都会诚实地记下自己是别的
        # 东西。**说出来**，因为一个没在采的观测器和一个采不到东西的观测器，日志
        # 里长得一模一样。
        self.play = None
        if PLAY_OBSERVE:
            self.play = PlayObserver(
                self.poller,
                on_magazine=self._on_play_magazine,
                meta_fn=lambda: _play_meta(self.state),
                curve_fn=self.dispatcher.armed_curve)
        else:
            print('[play] observing OFF (config.PLAY_OBSERVE) — 实战梭不入库',
                  flush=True)

        # Start threads
        self.capture.start()
        self.poller.start()
        self.dispatcher.start()
        if self.play is not None:
            self.play.start()
        print("init done", flush=True)

    def _on_play_magazine(self, result, meta):
        """一梭进影子库。**不进主库** —— 理由整段在 calibration/play_store.py。"""
        try:
            p = play_store.store(result, meta)
        except Exception as e:
            print(f'[play] store failed: {e!r}', flush=True)
            return
        if p is None:
            # 没枪名就不写。说出来，否则「采到零梭」和「一梭都没打」在日志里
            # 长得一模一样 —— 这个仓库为那种沉默付过账。
            print(f'[play] {meta.get("n_frames")} frames with no weapon name '
                  f'— not stored', flush=True)
            return
        print(f'[play] {meta.get("weapon")} hold={meta.get("hold_s")}s '
              f'{meta.get("n_frames")} frames -> {os.path.basename(p)}',
              flush=True)

    # How long the dispatcher gets to notice stop() before we disarm anyway.
    # A tick is 10 ms plus whatever one pass of detectors costs, so this is
    # two orders of margin -- it is a deadline, not an expected duration.
    JOIN_TIMEOUT_S = 2.0

    def shutdown(self):
        """Stop the threads, THEN disarm.

        This used to be stop() plus save_scales(), and stop() only clears a
        flag -- so every exit that was not the f13 key (Ctrl-C, an exception,
        join() returning) left the firmware compensating and the pattern
        loaded. Dispatcher.shutdown() is where the hardware reset lives; the
        join() in between is what stops the loop from re-arming it.

        It also saves the scales, so this no longer does.

        The join is BOUNDED, and the disarm runs whether or not it came back
        in time. Ordering the two is a preference; disarming is not. A loop
        wedged inside a detector would otherwise take the Pico with it, and
        three agents share that port -- the next run would measure a gun
        nobody is holding.
        """
        # 观测器先停：它持有第二个 grabber，而它读 poller 的左键状态。停在
        # poller 之后就是在读一个已经不更新的标志。
        if self.play is not None:
            self.play.stop()
            self.play.join(self.JOIN_TIMEOUT_S)
            self.play.close()
            print(f'[play] {self.play.n_bursts} bursts recorded, '
                  f'{self.play.n_short} too short, '
                  f'{self.play.n_unfocused} dropped to lost focus', flush=True)
        self.poller.stop()
        self.capture.stop()
        self.dispatcher.stop()
        self.dispatcher.join(self.JOIN_TIMEOUT_S)
        if self.dispatcher.is_alive():
            print(f'[shutdown] dispatcher still running after '
                  f'{self.JOIN_TIMEOUT_S}s -- disarming anyway', flush=True)
        self.dispatcher.shutdown()


if __name__ == '__main__':
    # ⚠ BEFORE Robot(), because the lines worth having start at import: which
    # curves loaded, which ones are seeds, and the first `[curves] no fitted
    # curve for ...` a gun produces the moment it is picked up.
    start_log()
    robot = Robot()
    try:
        # NOT dispatcher.join(). See DaemonLoop.wait -- a no-timeout join on
        # Windows holds a pending SIGINT until it returns, and this loop only
        # returns on f13, so Ctrl-C was never delivered at all.
        robot.dispatcher.wait()
    except KeyboardInterrupt:
        print('\n[robot] Ctrl-C', flush=True)
    finally:
        robot.shutdown()
