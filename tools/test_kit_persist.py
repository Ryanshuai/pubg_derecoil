"""Nothing may leave the tool silently disarmed. Offline: no game, no Pico.

    pixi run kit-persist

Two instances of one shape, a year apart in the table and identical in kind:
a KEYPRESS turns compensation off and nothing observable turns it back on.

    the F key    wiped both guns' kit, so the curve key fell to `bare`
    the win key  set stop_recoil, and nothing reliably cleared it

⚠ THIS IS THE GATE FOR THE BIGGEST SINGLE REASON THE TOOL STOPPED
COMPENSATING MID-FIGHT. `('clear_attachments',)` hung off the F key, so every
pickup wiped both guns' scope, muzzle, grip and stock. Nothing re-read them --
attachments are only visible on the Tab panel and F does not open it -- so ONE
pickup dropped the curve key to `bare` and the compensation stayed off until
the player opened Tab by hand.

F is the most-pressed key in a real match (ammo, meds, armour, attachments)
and almost none of those presses change your gun.

Measured, play log 2026-08-09 (calibration/artifacts/robot/0809_141201.log):
30 bursts, `[armed]` printed ONCE, and four m416 bursts went down recorded as
`bare`.

The clear now hangs off an OBSERVED weapon-name change in
GameState.sync_weapons. Clearing on a keypress is a guess about what the world
did; clearing on a name change is a measurement of it, and the name is already
read 500 ms after every F.

⚠ BOTH DIRECTIONS ARE CHECKED, and the second is the one that keeps this
honest. A gate that only proves "the kit survives" is passed by deleting the
clear altogether -- and then a real weapon swap fires the old gun's curve,
which is the 1521-counts-against-895 failure this repository already paid for.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from config import KEY_ACTION_TABLE                        # noqa: E402
from detector.game_state import GameState                  # noqa: E402

FAILS = []
KIT = {'scope': 'Upper_DotSight_01_C',
       'muzzle': 'Muzzle_Compensator_Large_C',
       'grip': 'Lower_Foregrip_C',
       'stock': 'Stock_AR_Composite_C'}


def check(what, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {what:<52} {got!r}'
          + ('' if ok else f'  != {want!r}'))
    if not ok:
        FAILS.append(what)


def armed(state):
    """counts the firmware would be handed for gun 1 right now."""
    return round(sum(state.weapon_1.dy_s), 1)


def fitted():
    s = GameState()
    s.weapon_gt = ('m416', '')
    s.sync_weapons()
    s.set_attachments(1, KIT)
    return s


print('=== the F key no longer carries a wipe ===')
f_entry = next(e for e in KEY_ACTION_TABLE
               if e['key'] == 'f' and e.get('event', 'press') == 'press')
check('F does not clear attachments',
      any(i == ('clear_attachments',) for i in f_entry.get('state', [])), False)
check('F still drops the weapon GT so the read can win',
      ('weapon_gt', ('', '')) in f_entry.get('state', []), True)

print('\n=== a pickup that does not change the gun keeps the curve ===')
s = fitted()
before = armed(s)
check('the full kit is armed', before > 0, True)
# What an F press does now: GT cleared, then the +500 ms read comes back with
# the SAME name (you picked up ammo, not a gun).
s.weapon_gt = ('', '')
s.sync_weapons()
check('still armed after the pickup', armed(s), before)
s.weapon_pred = ('m416', '')
s.sync_weapons()
check('still armed after the name read confirms m416', armed(s), before)

print('\n=== but a real weapon swap still clears it ===')
# ⚠ THE ORDER IS THE REAL ONE AND IT IS LOAD-BEARING. `weapon_name` prefers
# weapon_gt over weapon_pred, so a pred that says `scar` while a stale gt still
# says `m416` changes nothing. Picking a gun up is an F: the key drops the GT
# first, and the +500 ms weapon_hud read then wins. Writing the pred alone
# passed the "kit survives" half and failed all five of these -- which is the
# gate doing its job on the test rather than on the code.
s2 = fitted()
s2.weapon_gt = ('', '')          # the F press
s2.sync_weapons()
s2.weapon_pred = ('scar', '')    # the read, 500 ms later
s2.sync_weapons()
check('muzzle forgotten', s2.weapon_1.muzzle, '')
check('scope forgotten', s2.weapon_1.scope, '')
check('grip forgotten', s2.weapon_1.grip, '')
check('stock forgotten', s2.weapon_1.butt, '')
check('and it is not still firing the m416 curve', armed(s2), 0)

print('\n=== 架子说这一格空了：枪、配件、曲线一起走 ===')
# ⚠ 这是本文件那句话的第三个实例，方向反过来：不是「悄悄失去武装」，是**悄悄
# 武装着一把不在手上的枪**。`''` 以前同时表示「没读出来」和「这里没有东西」，
# 而 `weapon_name` 的 or 链把两者都解析成系统里最老的那个猜测。
# play log 2026-08-15 13:06:50，相邻两行：
#     [tab] read ... | gun2 (no gun in the rack slot)
#         m416 | full ... | 无曲线
# 架子答对了，答案被扔掉了。
sE = fitted()
check('先确认它在压枪', armed(sE) > 0, True)
sE.set_rack({1: False, 2: True})     # 1 号槽的枪被扔了，2 号还在
sE.weapon_gt = ('', 'vector')
sE.sync_weapons()
check('名字清了', sE.weapon_1.name, '')
check('配件清了', (sE.weapon_1.muzzle, sE.weapon_1.scope), ('', ''))
check('曲线清了', armed(sE), 0)
check('表里印 (empty)', '(empty)' in sE._fmt(sE.weapon_1, True), True)
check('而 2 号槽没被连坐', sE.weapon_2.name, 'vector')
# 反方向：present=True 不许清。一道只验「该清的清了」的闸，被「无条件清空」
# 通过——而那正好是 2026-08-09 因为 F 键退场的那个 bug。
sK = fitted()
sK.set_rack({1: True, 2: False})
sK.sync_weapons()
check('架子说有枪就不许清', armed(sK) > 0, True)

print('\n=== HUD 只在落地那一段作数，Tab 读回过一次就不作数了 ===')
# 操作员的规矩，而它的方向是对的：落地时还没有任何架子读回，一个凑合的名字
# 好过没有；一旦 Tab 答过一次，HUD 就只剩下坏处——它看不见配件，所以它说对了
# 也加不出曲线，说错了却会改名 + 清 kit。
# 空的 HUD 槽画的不是暗底板，是**穿透过来的游戏世界**：867 帧实测（slot 1 是
# kar98k、slot 2 全程空，calibration/artifacts/ads/runs），空槽 Laplacian 中位
# 317 对闸门 12，`drawn()` 放行 75.5%，其中 47% 拿到一个确信的枪名。
sH = GameState()
sH.weapon_pred = ('m416', '')
sH.sync_weapons()
check('落地：HUD 能给名字', sH.weapon_1.name, 'm416')
sH.set_rack({1: True, 2: True})      # 开了一次 Tab，架子答了
sH.weapon_gt = ('m416', 'vector')
sH.sync_weapons()
# ⚠ 然后按 F，而这一步是这一节的**全部**。F 把 `weapon_gt` 清成 ('', '')（上面
# 那节验的就是它），于是解析落到 pred 上——**那是 pred 唯一说了算的路**。不带
# 这一步的话 gt 一直压着 pred，「HUD 还作不作数」怎么写都是绿的：实测把
# rack_seen 那道闸整个删掉，不带 F 的版本 all ok。
sH.weapon_gt = ('', '')
sH.sync_weapons()
sH.weapon_pred = ('scar', 'awm')     # 同一个检测器，Tab 之后再说话
sH.sync_weapons()
check('Tab 之后：HUD 改不动 1 号槽', sH.weapon_1.name, 'm416')
check('也改不动 2 号槽', sH.weapon_2.name, 'vector')
# 空槽上的幻影是同一条路的另一头：架子说没枪，HUD 说有，架子赢。
sP = GameState()
sP.set_rack({1: True, 2: False})
sP.weapon_gt = ('m416', '')
sP.sync_weapons()
sP.weapon_pred = ('m416', 'awm')
sP.sync_weapons()
check('造不出 2 号槽那把幻影', sP.weapon_2.name, '')
# 而 Tab 自己仍然改得动——否则这道闸被「把两个来源都关掉」通过。
sH.weapon_gt = ('scar', 'vector')
sH.sync_weapons()
check('Tab 仍然改得动', sH.weapon_1.name, 'scar')
# 采集侧一个字都没关：`weapon_pred` 照写、mismatch 照采，只是不再驱动。
# 跟 HighlightDetector 是同一个形状（上面那节）。
check('weapon_pred 仍然被记录着', sH.weapon_pred, ('scar', 'awm'))
check('DETECT_TABLE 里 weapon_hud 还在跑',
      sorted({e['key'] for e in __import__('config').DETECT_TABLE
              if e.get('detect') == 'weapon_hud'}), ['1', '2', 'f'])

print('\n=== 空的槽切不过去，因为游戏本来就不让切 ===')
# 操作员实测：2 号槽空的时候按 2，PUBG **不切**——键被吞掉，手上还是那把枪。
# 而 `active` 照样挪过去的话，这一按就等于「停止压枪」：那把空枪没有名字、
# 没有曲线，于是固件对着一把**仍然在手上、仍然在开火**的枪解除了武装。
# 这是本文件题目那句话的又一个实例，而且是玩家自己按出来的。
sA = GameState()
sA.set_rack({1: True, 2: False})
sA.weapon_gt = ('m416', '')
sA.sync_weapons()
sA.set_active_by_key(1)
sA.set_active_by_key(2)
check('按 2 焦点不动', sA.active is sA.weapon_1, True)
check('highlight_gt 也不动（游戏没切，就不是真值）', sA.highlight_gt, 1)
# 反方向：架子说有枪就必须切得过去，否则这道闸被「1/2 键整个失效」通过。
sB = GameState()
sB.set_rack({1: True, 2: True})
sB.weapon_gt = ('m416', 'vector')
sB.sync_weapons()
sB.set_active_by_key(2)
check('架子说有枪就切得过去', sB.active is sB.weapon_2, True)
# 而没人看过架子（落地那一段）不许拒绝——在无知上拒绝会把人卡在第一次 Tab 之前。
sC = GameState()
sC.set_active_by_key(2)
check('没人看过架子时照切', sC.active is sC.weapon_2, True)

print('\n=== the right button re-arms, on the PRESS ===')
# ⚠ SAME FAMILY AS THE F KEY ABOVE: a key silently leaves the tool disarmed
# and nothing brings it back. `win` sets stop_recoil and clears nothing. Play
# log 2026-08-09 17:09:58 -- `win` press, then THIRTY-SIX SECONDS disarmed,
# until a Tab happened to be opened and closed.
#
# Every other re-arm in the table hangs off a key the player may simply not
# press (1, 2, a shift release). The right button is the one they cannot
# avoid: it is the key that means "I am about to shoot".
rights = [e for e in KEY_ACTION_TABLE if e['key'] == 'right']
check('exactly one right-button entry', len(rights), 1)
check('and it is on the PRESS', rights[0].get('event'), 'press')
check('it clears stop_recoil', ('stop_recoil', False) in rights[0]['state'],
      True)
check('and re-arms the firmware', rights[0].get('hw'),
      ['recoil_on', 'upload_pattern'])
# ⚠ THE PRESS EDGE IS THE POINT, NOT A DETAIL. Under release-only the whole
# HOLD ran disarmed, and a held right button is shoulder aim -- one of the
# three aiming states, not an edge case. With toggle ADS the two edges are
# ~50 ms apart; with a held one it is the entire engagement.
check('nothing re-arms on the right RELEASE (that was the bug)',
      any(e['key'] == 'right' and e.get('event') == 'release'
          for e in KEY_ACTION_TABLE), False)

print('\n=== win disarms, and the next right-click undoes it ===')
# End to end through the real dispatcher, because the assertions above are
# about a table and the failure was about a SEQUENCE. Dispatcher.__new__ skips
# the poller, the threads and the hardware.
from collections import deque, namedtuple                      # noqa: E402
from control.match import Dispatcher                           # noqa: E402

KeyEvent = namedtuple('KeyEvent', ['key', 'event', 'ts', 'held_keys'])


class _Tab:
    def on_key(self, ts):
        pass


d = Dispatcher.__new__(Dispatcher)
d.state = GameState()
d._detectors = {}
d._pending = deque()
d._hw = []
d._apply_hw = d._hw.append
d.tab = _Tab()
d._handle_key(KeyEvent('win', 'press', 0.0, frozenset()))
check('win disarmed it', d.state.stop_recoil, True)
d._handle_key(KeyEvent('right', 'press', 0.1, frozenset()))
check('the right-click press re-armed it', d.state.stop_recoil, False)
check('and pushed the curve with it', d._hw[-1], ['recoil_on',
                                                  'upload_pattern'])

print('\n=== 而那个「空」的读数，F 一按就过期 ===')
# ⚠ 拒绝是靠一个**测量**成立的，而测量会过期：捡一把枪进空的 2 号槽再按 2，游戏
# 是切的，而一条陈旧的 present=False 会拒掉它，把 1 号枪的曲线留在固件上——
# **一把在手上的枪配着另一把枪的曲线**，比拒绝本身要防的「没有曲线」更糟。
#
# ⚠ 走真 Dispatcher，不是直接调 `forget_rack()`。直接调的那一版只钉得住表里那
# 一行，把 `('forget_rack',)` 从 KEY_ACTION_TABLE 里删掉之后**行为那一半照样绿**
# ——实测，变异 M7 只咬中 1 条。键和状态之间那一跳正是要验的东西。
d3 = Dispatcher.__new__(Dispatcher)
d3.state = GameState()
d3._detectors, d3._pending, d3._hw = {}, deque(), []
d3._apply_hw = d3._hw.append
d3.tab = _Tab()
d3.state.set_rack({1: True, 2: False})
d3.state.weapon_gt = ('m416', '')
d3.state.sync_weapons()
d3._handle_key(KeyEvent('2', 'press', 0.0, frozenset()))
check('按 2 被拒（架子说空）', d3.state.active is d3.state.weapon_1, True)
d3._handle_key(KeyEvent('f', 'press', 0.1, frozenset()))
check('F 之后占用读数过期', d3.state.weapon_present, {1: None, 2: None})
check('但 HUD 没有因此复活', d3.state.rack_seen, True)
d3._handle_key(KeyEvent('2', 'press', 0.2, frozenset()))
check('于是 2 又切得过去', d3.state.active is d3.state.weapon_2, True)

print('\n=== clearing gun 1 does not touch gun 2 ===')
s3 = GameState()
s3.weapon_gt = ('m416', 'vector')
s3.sync_weapons()
s3.set_attachments(2, KIT)
kept = s3.weapon_2.muzzle
s3.weapon_gt = ('', '')
s3.sync_weapons()
s3.weapon_pred = ('scar', 'vector')
s3.sync_weapons()
check('gun 1 swapped, gun 2 keeps its muzzle', s3.weapon_2.muzzle, kept)

# ⚠ 下面三节钉的是同一天(2026-08-10)的三件事，而它们放在**这个**闸里是因为
# 这个文件的题目就是「没有东西可以让这个工具悄悄失去武装」：一条不解除武装的
# 退出路径、一块看不出自己没在压枪的屏幕、以及**一把压着别人曲线的枪**，都是
# 那句话的实例——最后那个更糟，因为屏幕上它看起来完全正常。
print('\n=== 焦点只有 1 和 2 两个键能动 ===')
# HUD 高亮曾经是第二个作者：`highlight_gt` 一为 0（f / g / x / **tab** 都会清
# 它），HighlightDetector 的读数就直接改写 `active`。而 tab 在标定路径上一直
# 在开合，所以那个窗口几乎常开。2026-08-10 的 play log 里 akm+scar 和
# p90+mp5k 两对枪的 `*` 在没人碰键的情况下来回跳，而下一行就是肇因：
# `gt_int=1 pred_int=2`——操作员按的是 1，检测器读的是 2。
#
# ⚠ 判据是**源码里谁写 `self.active`**，不是「跳没跳」。行为判据在这里必然
# 瞎：现在没有任何东西调度那个检测器，所以任何序列都不会跳，而重新加回一个
# 调度就又跳了。**能否定它的只有「有几个作者」这个问题。**
import ast                                                    # noqa: E402

gs_src = open(os.path.join(ROOT, 'detector', 'game_state.py'),
              encoding='utf-8').read()
authors = set()
for fn in ast.walk(ast.parse(gs_src)):
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    for n in ast.walk(fn):
        tgts = ([n.target] if isinstance(n, ast.AnnAssign) else
                list(n.targets) if isinstance(n, ast.Assign) else [])
        for t in tgts:
            if (isinstance(t, ast.Attribute) and t.attr == 'active'
                    and isinstance(t.value, ast.Name) and t.value.id == 'self'):
                authors.add(fn.name)
check('写 self.active 的函数', sorted(authors), ['__init__', 'set_active_by_key'])
# 而调度侧对称的一半：DETECT_TABLE 里再出现 highlight，就是在给那个作者重新
# 接线。MISMATCH_TABLE 里的两条留着——那两条有真 GT（1/2 键本身）可对，是采
# 集训练数据，不驱动任何东西。
from config import DETECT_TABLE, MISMATCH_TABLE                # noqa: E402
check('DETECT_TABLE 里没有 highlight',
      [e['key'] for e in DETECT_TABLE if e.get('detect') == 'highlight'], [])
check('MISMATCH_TABLE 里还在采它',
      sorted(e['key'] for e in MISMATCH_TABLE
             if e.get('detect') == 'highlight'), ['1', '2'])

# 端到端两个方向：1/2 说了算，而 tab —— 那个把窗口打开的键 —— 一个人都不动。
d2 = Dispatcher.__new__(Dispatcher)
d2.state = GameState()
d2._detectors, d2._pending, d2._hw = {}, deque(), []
d2._apply_hw = d2._hw.append
d2.tab = _Tab()
d2._handle_key(KeyEvent('2', 'press', 0.0, frozenset()))
check('按 2 拿的是 2 号枪', d2.state.active is d2.state.weapon_2, True)
d2._handle_key(KeyEvent('tab', 'press', 0.1, frozenset()))
check('按 tab 焦点不动', d2.state.active is d2.state.weapon_2, True)
d2._handle_key(KeyEvent('1', 'press', 0.2, frozenset()))
check('按 1 回到 1 号枪', d2.state.active is d2.state.weapon_1, True)

print('\n=== 退出只有 Ctrl-C 一条路 ===')
shutdown_keys = [e['key'] for e in KEY_ACTION_TABLE
                 if 'shutdown' in (e.get('hw') or [])]
# 两个作者中的一个是死的：`f13` 分支写在 match.py 里，而 POLL_VK_MAP 从不轮询
# 它 —— 所以「关闭」这件事印在两处文档里，其中一处一次都没执行过。
check('KEY_ACTION_TABLE 里没有关闭键', shutdown_keys, [])
src = open(os.path.join(ROOT, 'control', 'match.py'), encoding='utf-8').read()
check("match.py 里没有 'f13' 分支", "'f13'" in src, False)
check("_apply_hw 不再认 'shutdown'", "== 'shutdown'" in src, False)

print('\n=== 屏幕上那张表必须说得出「有没有在压枪」 ===')
# 终端现在只剩这张表（`[armed]` 整个进了日志文件），所以「这把枪没曲线」这个
# 事实必须由表自己印。否则一把没在压枪的枪和一把在压枪的枪，屏幕上一模一样 ——
# 而这正是本文件开头那 30 梭付过的账。
s4 = fitted()
armed_row = s4._fmt(s4.weapon_1, True)
check('有曲线时表里带弹数', '发' in armed_row and '无曲线' not in armed_row, True)
s4.weapon_1.dx_s, s4.weapon_1.dy_s, s4.weapon_1.t_s = [], [], []
check('没曲线时表里说无曲线', '无曲线' in s4._fmt(s4.weapon_1, True), True)
m_src = src[src.index('def _said_pattern'):]
check('[armed] 走 note 不走 print',
      'note(f\'[armed]' in m_src and "print(f'[armed] {msg}" not in m_src, True)

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
    sys.exit(1)
print('all ok')
