"""What can I call to do X, and which level of it do I want.

    pixi run surface              every intent, one line each
    pixi run surface aim          the levels for one intent
    pixi run surface --audit      public callables no intent claims yet
    pixi run surface --families   near-duplicate name families, auto-found
    pixi run surface-check        R must not drive. Its own task: an offline
                                  gate with no task is one nobody runs.

WHY THIS EXISTS, AND WHY IT IS A COMMAND RATHER THAN A PARAGRAPH:

tools/CLAUDE.md has said "这一层的唯一纪律：先找，再写" since it was written,
and the 25-row table under that sentence is a record of the discipline failing
25 times. The instruction is not wrong; it is unenforceable. "Read first" asks
for diligence, and the caller who most needs it is the one who is not blocked
and therefore never opens the file.

So the fix is not a stronger sentence. It is making the reading cheap: 20 lines
of output instead of control/inventory.py's 3337. Measured 2026-08-07, the
surface a caller is choosing from:

    713 public callables in control/calibration/press/detector
    197 of them with no docstring at all
    29 entry points match "put an attachment on the gun" -- 12 in one file
    4350 lines of docstring total, and the discriminating sentence is
    typically in paragraph four

THE LEVELS, AND WHY LEVELS RATHER THAN DESCRIPTIONS:

A caller always knows their own intent. A caller never knows the callee's
local quirks. So the selection criterion has to live on the side the caller
can answer from.

⚠ THE LEVEL IS THE STRENGTH OF THE GUARANTEE, NOT THE SIZE OF THE CALL. The
first draft of this file said "L2 = I want the result, L1 = I want to control
the pacing", which conflates two axes — how much one call accomplishes, and
what a True return actually promises. control/spawner.py hides the difference
because there the two coincide (give_many is big AND verified, click_category
is small AND blind). control/aim.py tears it open on the first entry:
home_to_clamp is one large movement with ZERO verification, and tagging it by
size put it a level above where it belongs. Four of the first fifteen tags
were wrong the same way, within an hour of the definition being written.

    L2  A True return means THE GOAL IS TRUE, confirmed against the screen.
        Retries internally. Start here; you should need nothing else.
    L1  A True return means THIS STEP LANDED — the domain step you asked for.
        The goal is the caller's to compose and to retry.
    L0  MECHANISM. The guards live one level up, so calling it directly walks
        around them. It may verify nothing, or — worse — verify the wrong
        thing.
    R   Drives nothing in the game. Reads an answer, or records one.

⚠ L0 IS NOT "UNVERIFIED", AND GETTING THAT WRONG COST THE THIRD PASS AT THIS
DEFINITION. drag, right_click_equip and right_click_unequip were all filed L0
because they are the primitive gestures — position in the stack, which is the
size axis again, wearing a third disguise. All three read the screen back,
retry in place and fail loudly, so "unverified" was simply false about them.

What is true about them is worse than being unverified, and it is what the tag
now says: right_click_unequip verifies THE SLOT IS EMPTY and returns ok=True
when the empty slot is because THE WHOLE GUN FELL ON THE FLOOR. It checked,
and it checked the wrong object. unequip() is the entry point because its tile
and AMBIGUOUS guards are what keep the gesture off an empty slot in the first
place — 74 lost parts across 11 collector runs say so (inventory.py:2046).

So the question L0 answers is "is anything above me holding a guard I am about
to skip", not "did I look". Under it, drag's `_reject` still runs but
unequip's guards do not; right_click_equip's retry silently never fires unless
the caller passes `att=`, which equip() does and a direct caller will not.

R is not a fourth level, it is the other axis showing itself: `travel()` and
`absolute_offset()` answer questions, and a question has no guarantee to rank.
Without R they get filed as weak drivers, which is how `set_reference` — which
moves nothing — ended up beside `to_stop`, which walks the view into a clamp.

⚠ A FLAG THAT CHANGES THE LEVEL IS THE FINDING. `travel(measure=True)` turns
an R into an L1 that ratchets the view for a minute. It is filed under R with
the default it actually ships with, and the docstring carries the warning —
but a parameter that moves a callable between levels is one callable doing two
jobs, and the next person to touch travel() should split it.

Descriptions need every pair of siblings distinguished (12 entries = 66 pairs).
Levels need each entry tagged once.

The shape is not invented here: control/spawner.py has had exactly these three
(give_many / sync+read+goto / click_category) since 2026-08-04, and it is the
one family whose complaints stopped. What it never had is the tag anywhere a
caller would see it -- 33 public entries, 0 docstrings mentioning a level. The
tier lived only in control/CLAUDE.md, which is read once at the start of a
session and then competes with everything else for attention.

⚠ AN INTENT THAT CANNOT BE TIERED IS THE FINDING, NOT A GAP IN THIS TABLE. A
function that fits no level is one of two things: dead (control/aim.py's
goto_level, whose only caller is calibration/sweep.py's forwarder) or
un-tiered because nobody ever decided what it was for. Both are worth knowing;
neither is fixed by adding a fourth level.
"""
import argparse
import ast
import collections
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# `audit` is aliased: this file already has an audit(info) for --audit,
# and the import silently lost to it -- the ledger call reached the wrong
# function and died on an unhashable dict. A collision that raises is the
# lucky version; the same shadowing with compatible signatures would have
# printed a plausible answer.
from _ledger import (Reason, CODE, INFERRED,          # noqa: E402
                     audit as ledger_audit, callers_of, calls)

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The layers a caller picks from. tools/ is excluded on purpose: nothing there
# is meant to be called by anything else (tools/CLAUDE.md: "这里没有别人 import
# 的东西"), so it is not part of anyone's choice.
DIRS = ('control', 'detector', 'press', 'calibration', 'harness')

L2, L1, L0, R = 'L2', 'L1', 'L0', 'R'

# The order they print in, and what each one PROMISES. Kept as data so the
# rendering and the audit cannot drift from the definition in the docstring.
LEVELS = (
    (L2, '返回真 = 目标已达成，屏幕确认过，内部会重试'),
    (L1, '返回真 = 这一步落地了。目标由调用方编排'),
    (L0, '机制层：守卫在上一层，直接调会绕过去'),
    # ⚠ "不驱动游戏", NOT "随时可调". set_reference() drives nothing and is
    # still order-sensitive -- it takes the reference wherever the view is,
    # including against a clamp. The promise R makes is about the GAME, and
    # bookkeeping that only the driver sees is still bookkeeping.
    (R,  '不驱动游戏：读一个答案，或只记一笔'),
)

# ════════════════════════════════════════════════════════════════════
# The intent table
# ════════════════════════════════════════════════════════════════════
#
# Hand-written, and it has to be: the auto-detected families (--families)
# group by shared words, which finds `equip`/`right_click_equip` but never
# finds that `ensure_kit` is the same intent. Naming is exactly what has gone
# wrong, so a table keyed on names cannot be the whole answer.
#
# ADDING AN INTENT: run --audit, take the biggest cluster of unclaimed
# callables, and decide which level each is. If a callable will not take a
# level, say so in UNTIERED with the reason rather than forcing one.

INTENTS = {
    'aim': {
        'what': '把视角放到某个俯仰',
        # Re-filed 2026-08-07 after the guarantee/size confusion: home_to_clamp
        # dropped L1 -> L0 (it verifies nothing and says so), and four
        # question-answering entries moved out of L1 into R.
        L2: ['control/aim.py:ViewDriver.goto_midline',
             'control/aim.py:ViewDriver.recenter',
             'control/aim.py:ViewDriver.reaim'],
        L1: ['control/aim.py:ViewDriver.to_stop',
             'control/aim.py:ViewDriver.step_once',
             'control/aim.py:ViewDriver.tracking_confirmed',
             'control/aim.py:ViewDriver.measure_travel',
             'control/aim.py:ViewDriver.calibrate_pitch'],
        L0: ['control/aim.py:ViewDriver.turn',
             'control/aim.py:ViewDriver.ads_tap',
             'control/aim.py:ViewDriver.home_to_clamp'],
        R: ['control/aim.py:ViewDriver.travel',
            'control/aim.py:ViewDriver.absolute_offset',
            'control/aim.py:ViewDriver.track_still',
            'control/aim.py:ViewDriver.set_reference'],
    },
    'spawn': {
        'what': '从刷新器刷东西出来',
        # give -> L1 and give_gear -> L2 on 2026-08-07: `give` is a dispatch
        # whose strength is its WEAKEST branch (_spawn refuses unless the
        # panel is already up), while give_gear is the only give_* that
        # routes through give_many and so opens, collapses, proves and closes.
        L2: ['control/spawner.py:SpawnerControl.give_many',
             'control/spawner.py:SpawnerControl.rack_pair',
             'control/spawner.py:SpawnerControl.give_gear'],
        # expand / collapse deleted 2026-08-07: zero callers, and both
        # signatures named parameters no body read. goto()/collapse_all() are
        # what every live caller was already using.
        L1: ['control/spawner.py:SpawnerControl.give',
             'control/spawner.py:SpawnerControl.ensure_panel',
             'control/spawner.py:SpawnerControl.goto',
             'control/spawner.py:SpawnerControl.collapse_all',
             'control/spawner.py:SpawnerControl.give_weapon',
             'control/spawner.py:SpawnerControl.give_attachment'],
        L0: [],
        R: ['control/spawner.py:SpawnerControl.sync',
            'control/spawner.py:SpawnerControl.panel_open',
            'control/spawner.py:SpawnerControl.plan',
            'control/spawner.py:SpawnerControl.script'],
    },
    'kit': {
        'what': '把配件装到枪上 / 卸下来',
        # ⚠ build() WAS TAGGED L2 AND IS NOT, and the argument was already in
        # the file: batch()'s own docstring says a batch whose steps all
        # reported ok "is exactly the case where nobody would look again", and
        # names ensure_kit's readback as the one legitimate override. build
        # never reads the gun back, only adds (an occupied slot is skipped),
        # and a part dropped at plan time produces no step at all — so ok=True
        # survives "nothing was fitted". Same promise as equip, one batch up.
        L2: ['control/inventory.py:InventoryControl.ensure_kit'],
        L1: ['control/inventory.py:InventoryControl.build',
             'control/inventory.py:InventoryControl.equip',
             'control/inventory.py:InventoryControl.unequip',
             'control/inventory.py:InventoryControl.strip',
             'control/inventory.py:InventoryControl.transfer',
             'control/inventory.py:InventoryControl.run_plan',
             # auto_equip_key was filed BECAUSE it is auto_equip's guard and a
             # guard has to outrank what it guards. It had gone unfiled for
             # the ordinary reason -- it is nobody's first choice, so nobody
             # reached for it -- and that is exactly how the guard chain ends
             # up broken at an invisible node: the guard exists and no
             # declaration says it is one.
             'control/inventory.py:InventoryControl.auto_equip_key'],
        # ⚠ hold FELL L1 -> L0 and the table was six hours behind its own
        # docstring, which already read "Nothing reads the HUD back: True
        # means the key went out". It also short-circuits on a CACHED
        # `self.held`, so anything that can move a gun -- the spawner, an
        # eviction -- has to clear it first. That is not a weak L1, it is a
        # mechanism whose guard is somebody else's.
        L0: ['control/inventory.py:InventoryControl.hold',
             'control/inventory.py:InventoryControl.drag',
             'control/inventory.py:InventoryControl.right_click_equip',
             'control/inventory.py:InventoryControl.right_click_unequip',
             'control/inventory.py:InventoryControl.auto_equip'],
    },
    'tab': {
        'what': '把 Tab 界面弄开 / 关',
        # tab_open and await_tab moved L1 -> R on 2026-08-07: neither sends a
        # key. await_tab is a blocking read with a deadline, and calling that
        # a driver is the size axis again -- it is "the back half of a press",
        # but the press is ensure_tab's, and the level tags THIS callable.
        L2: ['control/inventory.py:InventoryControl.tab_up'],
        L1: ['control/inventory.py:InventoryControl.ensure_tab',
             'control/stock.py:open_tab'],
        L0: [],
        # TabWatch is the READ-ONLY half of the Tab screen and the split is
        # the design, not an accident of who wrote what: it belongs to the
        # live loop, ticks passively, and NEVER SENDS A KEY. Filing all five R
        # is what makes that legible from a declaration -- InventoryControl
        # and TabWatch look like two ways to do the same thing until you can
        # see that one of them cannot drive at all.
        #
        # on_key() is the one that reads like a driver and is not: it is told
        # a key was SEEN, and arms a watch. The keypress is somebody else's.
        R: ['control/inventory.py:InventoryControl.tab_open',
            'control/inventory.py:InventoryControl.await_tab',
            'control/tab_watch.py:TabWatch.measure_open',
            'control/tab_watch.py:TabWatch.read_loadout',
            'control/tab_watch.py:TabWatch.tick',
            'control/tab_watch.py:TabWatch.on_key',
            'control/tab_watch.py:TabWatch.close'],
    },
    'stock': {
        'what': '背包盘点 / 补货 / 清库存',
        # ⚠ restock FELL AND ROSE AGAIN, both times on evidence rather than
        # taste, and the round trip is the argument for the ratchet printing
        # movement at all. It lost its L2 on 2026-08-07: the first line
        # claimed "True when the pack holds one of everything wanted" and
        # nothing re-read the pack. It got the post-read the same day and
        # STILL did not get the level back, because the unreadable path
        # returned spawn_missing's ok -- give_many's ok -- which proves clicks
        # landed on planned NODES and nothing about what arrived. Both paths
        # look in the pack now, and a path that cannot see it returns False.
        L2: ['control/stock.py:restock',
             # ⚠ WHICH gun, not just A gun. The ammo counter proves a weapon
             # is out and says nothing about whose -- a rack left loaded by
             # the previous run satisfied that and handed the caller the wrong
             # weapon for an hour, with every bearing coming back "nothing
             # tracks" because the patches sat on the VSS's integral scope.
             # It reads the rack, holds, and reads the counter back.
             'control/stock.py:ensure_weapon_in_hand',
             'control/inventory.py:InventoryControl.drop_weapon'],
        L1: ['control/stock.py:read_stock',
             'control/stock.py:tidy',
             'control/stock.py:spawn_missing',
             'control/inventory.py:InventoryControl.clear_rack',
             'control/inventory.py:InventoryControl.clear_ground',
             'control/inventory.py:InventoryControl.clear_inventory',
             'control/inventory.py:InventoryControl.discard'],
        L0: ['control/inventory.py:InventoryControl.stow'],
        # Stock is a VALUE OBJECT over one backpack read — every method here
        # is a question about that snapshot and none of them looks at the
        # screen again. Filed R together because that is the useful fact: a
        # caller holding a Stock can ask it anything for free, and the cost
        # was paid once by read_stock.
        R: ['control/stock.py:weapon_in_hand',
            'control/stock.py:Stock.have',
            'control/stock.py:Stock.missing',
            'control/stock.py:Stock.in_pack',
            'control/stock.py:Stock.on_guns',
            'control/stock.py:Stock.duplicates',
            'control/stock.py:Stock.unwanted',
            'control/stock.py:Stock.summary',
            'control/stock.py:backpack_worn'],
    },
    'ready': {
        'what': '让游戏进入可以被驱动的状态',
        # enter_match / exit_to_lobby left L0: they verify, through the same
        # _pump and the same LobbyState the L1 uses. What is weaker is which
        # screens they can start FROM, and coverage is not a guarantee —
        # filing it as one turns the level into "is this function any good".
        L2: ['control/session.py:ensure_ready'],
        L1: ['control/lobby.py:LobbyControl.ensure_in_match',
             'control/lobby.py:LobbyControl.enter_match',
             'control/lobby.py:LobbyControl.exit_to_lobby',
             'control/map.py:MapControl.goto_range',
             'control/map.py:MapControl.ensure_map',
             'control/lobby.py:LobbyControl.ensure_mode',
             'control/focus.py:ensure_focus'],
        # THE BLIND CLICKS AND KEYS THE FOUR L1s ARE MADE OF. Every one is a
        # single gesture with no readback of its own; the polling that turns
        # them into a state machine lives in enter_match / exit_to_lobby /
        # ensure_in_match, which is precisely what L0 means. press_esc is the
        # sharpest of them -- ESC is a TOGGLE, so a caller who sends it at a
        # screen with no menu OPENS one, and the pumps are what read first.
        #
        # ⚠ click_leave is the one to look at before reaching past any of
        # these: it refuses unless the entry's glyphs match, because EXIT TO
        # DESKTOP sits one pitch below LEAVE TRAINING. The refusal is the
        # guard working, and it is inside the L0 rather than above it.
        # stir() is here rather than under 'frame', where it was filed for one
        # run before --check bit: it spells the same as the frame-source
        # methods around it in gun.py and does something else entirely -- it
        # presses W then S. Its purpose is staying ready (an eviction every
        # ~20.5 minutes that may or may not be an idle timer), not reading a
        # screen. L0 because it is a mechanism with NO guard of its own and no
        # verification: it returns True for "a Pico exists", and whether the
        # character actually moved is not read by anything.
        L0: ['control/gun.py:GunDriver.stir',
             'control/lobby.py:LobbyControl.press_play',
             'control/lobby.py:LobbyControl.press_esc',
             'control/lobby.py:LobbyControl.dismiss_error',
             'control/lobby.py:LobbyControl.click_reconnect',
             'control/lobby.py:LobbyControl.click_leave',
             'control/lobby.py:LobbyControl.click_leave_confirm',
             'control/lobby.py:LobbyControl.click_exit',
             'control/map.py:MapControl.press_map',
             # raise_game IS the mechanism ensure_focus is built on: it
             # borrows the foreground thread's input queue, and its return is
             # whether it got focus THAT TIME. ensure_focus is what retries it
             # three times and waits out FOCUS_SETTLE_S; reaching past it gets
             # a single unsettled attempt, and the first frames after a
             # foreground change are dropped by the game.
             'control/focus.py:raise_game'],
        # state() is R and it MATTERS that it is: a read function that also
        # un-iconified the window would be moving the thing it was asked to
        # describe. Restoring is restore_window's job, filed L0 under `launch`.
        R: ['control/lobby.py:LobbyControl.state',
            'control/lobby.py:LobbyControl.mode',
            'control/focus.py:game_focused',
            'control/focus.py:foreground',
            # The keeper is a BUDGET, not an action: ok() asks whether the
            # foreground is still ours and spends one of five regains if it is
            # not. Filed R because the regain it performs is raise_game's, and
            # that is the L0 next to it.
            'control/focus.py:focus_keeper',
            'control/focus.py:FocusKeeper.ok'],
    },
    'fire': {
        'what': '打一个弹匣 / 换弹 / 读弹药',
        # fire_magazine is NOT L2: no bool, no retry, and MAX_FIRE_S timeout
        # shares an exit with "the magazine emptied".
        #
        # ⚠ disarm ROSE L0 -> L1 on 2026-08-07, and the table was the last
        # thing to hear about it. The firmware got a `[pat] end <0|1>`
        # readback, _write stopped swallowing the CDC timeout on the critical
        # path, and the docstring was rewritten to "CONFIRMED BY READING THE
        # FIRMWARE BACK" — while this list still said L0 with a comment
        # explaining that the readback does not exist. Caught by the tag-match
        # check below, not by anyone re-reading either side.
        #
        # Rig.arm is filed even though it lives in calibration/, because it is
        # arm()'s guard and a guard that cannot be named cannot be checked.
        # This is the cross-layer coupling made visible rather than removed:
        # --no-comp is genuinely the EXPERIMENT's decision, so the guard
        # belongs where it is; what was wrong was that only a docstring said so.
        L2: ['control/fire.py:FireDriver.top_up'],
        L1: ['control/fire.py:FireDriver.fire_magazine',
             'control/fire.py:FireDriver.fire_magazine_timed',
             'control/fire.py:FireDriver.disarm',
             'calibration/sweep.py:Rig.arm'],
        L0: ['control/fire.py:FireDriver.arm'],
        R: ['control/fire.py:FireDriver.wait_reload',
            'control/fire.py:FireDriver.magazine_size',
            'control/fire.py:FireDriver.read_ammo',
            'control/fire.py:FireDriver.ammo_sig'],
    },
    'posture': {
        'what': '姿势 / 开镜 / 火力模式',
        # ensure_hip -> L1: its guarantee comes from an unread button
        # RELEASE, and in_ads cannot tell hip from shoulder. ensure_fire_mode
        # -> L1: it returns a mode string, and 'single' is truthy.
        # ensure_inventory_closed -> L0 and DELIBERATELY not level with its
        # `open` twin: the same missing guard makes `open` fail honestly and
        # makes `closed` return a wrong True under the spawner panel.
        L2: ['control/gun.py:GunDriver.ensure_posture',
             'control/gun.py:GunDriver.ensure_ads'],
        L1: ['control/gun.py:GunDriver.ensure_hip',
             'control/gun.py:GunDriver.ensure_fire_mode',
             'control/gun.py:GunDriver.read_loadout',
             'control/gun.py:GunDriver.ensure_inventory_open'],
        L0: ['control/gun.py:GunDriver.ensure_inventory_closed'],
        # blocking_screen filed the minute it was written — by the ratchet,
        # which failed the run on the commit that added it. That is the whole
        # point of the thing: it is the author who knows the level, and the
        # only moment they reliably know it is now.
        R: ['control/gun.py:GunDriver.blocking_screen',
            # dump() writes the crops behind a failed decision and drives
            # nothing; tab_open() reads the anchor. Both are what you reach
            # for AFTER something refused, which is when a caller is least
            # able to afford a surprise.
            'control/gun.py:GunDriver.dump',
            'control/gun.py:GunDriver.tab_open',
            'control/gun.py:GunDriver.read_posture',
            'control/gun.py:GunDriver.in_ads',
            'control/gun.py:GunDriver.ads_signals',
            'control/gun.py:GunDriver.read_fire_mode'],
    },
    'inventory': {
        'what': 'Tab 屏幕的地址、读数与记录（不动手）',
        # ⚠ THE FILING IS WHAT SAID WHERE TO CUT. 37 unfiled entries in one
        # file and almost all of them R -- a level histogram that says the
        # unclaimed surface of the repo's most coupled module was a VOCABULARY
        # and its readers, not driving. On 2026-08-08 the two pure halves left
        # (control/locations.py, control/kit_plan.py) and inventory.py went
        # 3776 -> 3277. The refs below moved with them; the names are still
        # importable from control.inventory, which re-exports.
        #
        # ⚠ 37 unfiled entries in one file, and almost all of them are R. That
        # is the finding rather than the backlog: control/inventory.py reads as
        # the most coupled module in the repo -- 3337 lines, entries at every
        # level -- and its unclaimed surface turns out to be an ADDRESS
        # VOCABULARY and a set of readers, with the driving concentrated in the
        # dozen entries already filed under kit/tab/stock.
        #
        # Which is why this is a separate intent instead of more rows under
        # `kit`: a caller asking "where is 库存 row 3" and a caller asking "put
        # a compensator on gun 1" are not the same caller, and merging them is
        # what made 29 entry points match "put an attachment on the gun".
        #
        # It is also what said WHERE to cut: the two pure halves left on
        # 2026-08-08 (control/locations.py, control/kit_plan.py) and
        # inventory.py went 3776 -> 3277. What stayed is the driver.
        L2: [],
        # ⚠ THE TWO THAT ARE NOT READS. Everything else in this intent answers
        # a question for free; these two OPEN THE TAB SCREEN to do it --
        # survey calls tab_up(), and loadout is a thin wrapper over survey.
        # They sat under R until 2026-08-08 on the assumption that an
        # InventoryControl read only ever parks the cursor.
        #
        # L1 rather than R is not bookkeeping. A caller who believes these are
        # free will call one in the middle of a gesture, and a Tab toggle
        # there is how a drag ends up aimed at a screen that just closed.
        L1: ['control/inventory.py:InventoryControl.survey',
             'control/inventory.py:InventoryControl.loadout'],
        L0: [],
        R: ['control/locations.py:at_gun',
            'control/locations.py:at_slot',
            'control/locations.py:at_inv',
            'control/locations.py:at_ground',
            'control/locations.py:as_loc',
            'control/locations.py:parse_loc',
            'control/locations.py:loc_str',
            'control/locations.py:is_gun',
            'control/locations.py:is_slot',
            'control/locations.py:kind_of',
            'control/locations.py:move_info',
            'control/locations.py:panel_counts',
            'control/kit_plan.py:loose_items',
            'control/kit_plan.py:slot_matches',
            'control/kit_plan.py:kit_faults',
            'control/kit_plan.py:plan_kit',
            'control/kit_plan.py:plan_equip',
            'control/inventory.py:step',
            'control/inventory.py:batch',
            'control/inventory.py:journal',
            'control/inventory.py:dump',
            'control/inventory.py:InventoryControl.look',
            'control/inventory.py:InventoryControl.frame',
            'control/inventory.py:InventoryControl.read_slots',
            'control/inventory.py:InventoryControl.read_weapons',
            'control/inventory.py:InventoryControl.slot_state',
            'control/inventory.py:InventoryControl.slot_states',
            'control/inventory.py:InventoryControl.gun_slot',
            'control/inventory.py:InventoryControl.plate_ink',
            'control/inventory.py:InventoryControl.point_of',
            'control/inventory.py:InventoryControl.sync',
            'control/inventory.py:InventoryControl.set_rows',
            'control/inventory.py:InventoryControl.close',
            'control/inventory.py:InventoryControl.park'],
    },
    'kitting': {
        'what': '把一整套配件弄到枪上，跨格子地维持它',
        # ⚠ IT MOVED HERE FROM calibration/ ON 2026-08-08, and the move is the
        # filing's whole reason. calibration/CLAUDE.md's first rule is "一个
        # ensure_* 都不该有 —— 想让游戏做点什么，去 control/", and Kitter.apply
        # is an ensure_* by any reading: it puts a kit on a gun and proves it
        # went on. It spent its whole life inside a 3365-line module whose
        # other half was the recoil sweep, which is how a rule gets broken
        # without anybody deciding to break it — nobody ever looked at that
        # class on its own.
        #
        # ⚠ IT IS NOT A SECOND ensure_kit, and the line count says otherwise
        # (Kitter ~440, ensure_kit 184) which is exactly how I mis-read it an
        # hour before writing this. Kitter._apply's body is ONE CALL to
        # InventoryControl.ensure_kit. What is around it is the state one gun
        # cannot hold: which rack slot the spawner chose (it does not promise
        # one), putting evicted parts back on the gun before throwing it, and
        # holding a single Tab session across a whole weapon.
        L2: [],
        # apply() returns the readback or None, and None means A SLOT
        # DISAGREED — the drags happened, the screen was read, and what came
        # back is not what was asked for. That is a step landing or not
        # landing, which is L1; the GOAL (the gun wears this config) belongs to
        # ensure_kit underneath it, and it is the one that retries.
        L1: ['control/kitting.py:Kitter.apply',
             'control/kitting.py:Kitter.find_gun',
             'control/kitting.py:Kitter.strip',
             'control/kitting.py:Kitter.clear_rack',
             'control/kitting.py:stock_parts'],
        # session() hands out a held Tab screen and drives nothing itself; the
        # cost it saves is real and measured — without it apply() closed the
        # screen and the next helper reopened it, 0.79 s apart, named in the
        # churn log as one function closing and reopening its own screen.
        L0: ['control/kitting.py:Kitter.session'],
        # The config vocabulary: pure, offline, no game. `pixi run
        # setup-verdict` scores setup_verdict on six stored cases.
        R: ['control/kitting.py:parse_config',
            'control/kitting.py:config_name',
            'control/kitting.py:effective_config',
            'control/kitting.py:supported_configs',
            'control/kitting.py:fixed_kit',
            'control/kitting.py:want_for',
            'control/kitting.py:expand',
            'control/kitting.py:setup_verdict',
            'control/kitting.py:note_fits',
            'control/kitting.py:Kitter.close'],
    },
    'lifecycle': {
        'what': '建一个驱动 / 关掉它 / 问它能不能按键',
        # ⚠ FOUR CLASSES, FOUR ANSWERS TO THE SAME THREE QUESTIONS, until
        # 2026-08-07. Lobby and Map built the Pointer lazily, closed cleanly
        # and released on `with`; SpawnerControl did none of the three (its
        # __exit__ was `return False`, under a comment claiming "the Pointer
        # is lazily built" twenty-three lines below the line that built it
        # eagerly); InventoryControl had no `with` at all, so its 41
        # construction sites each hand-rolled a try/finally.
        #
        # The differences were invisible from a call site, which is exactly
        # what this whole table exists to fix -- so the fix was a shared base
        # (control/driver.py) rather than four corrected copies.
        L2: [],
        L1: [],
        L0: [],
        # ALL R, and `pointer` is the one worth arguing about: it OPENS COM10
        # and commands nothing. That is the R definition working rather than
        # being bent -- it hands back a handle, and every actual command goes
        # through a method on it, each of which is filed under its own intent.
        # The cost is real and named in its docstring, which is why the
        # property exists at all: `sc.plan()` is 纯离线 and used to take the
        # shared serial port to answer.
        R: ['control/driver.py:Driver.pointer',
            'control/driver.py:Driver.can_press',
            'control/driver.py:Driver.close',
            'control/spawner.py:SpawnerControl.close',
            'control/lobby.py:LobbyControl.close',
            'control/map.py:MapControl.close',
            'control/inventory.py:InventoryControl.close',
            'control/tab_watch.py:TabWatch.close'],
    },
    'panel': {
        'what': '刷新器面板的坐标表、读数与救场点击',
        # ⚠ THE SIX RESCUE ENTRIES ARE CLASS-LEVEL ALIASES, and until
        # 2026-08-08 that made them UNJUDGEABLE. control/spawner.py publishes
        # them as `click_category = _click_category`; _bodies() only collected
        # FunctionDefs, so _driving() had no body to look at and answered "does
        # not drive" about six blind-click entry points on the busiest driving
        # module in the repo. "Nothing to judge" and "judged, and inert" are
        # not the same answer, and collapsing them lands on the side that
        # cannot fail.
        #
        # The shape this intent describes is the one control/CLAUDE.md has
        # called the model family since 2026-08-04 -- give_many / sync+read+goto
        # / click_category -- and it is the family whose complaints stopped.
        # What it never had is the tag anywhere a caller would see it.
        L2: [],
        L1: [],
        # Every one of these TOGGLES and says nothing about the result.
        # click_category's own docstring says so; goto and collapse_all drive
        # a sequence of them. sync() + read() is what turns any of it into a
        # statement about the screen, and that is the guard.
        L0: ['control/spawner.py:SpawnerControl.click_category',
             'control/spawner.py:SpawnerControl.click_entry',
             'control/spawner.py:SpawnerControl.goto',
             'control/spawner.py:SpawnerControl.collapse_all',
             'control/spawner.py:SpawnerControl.spawn',
             'control/spawner.py:SpawnerControl.switch_to_slot2'],
        # The measured coordinate tables and the panel readers. All offline:
        # `pixi run spawner-plan` scores plan() with no game attached, and
        # read() answers off ONE screenshot with no baseline to compare
        # against -- which is what lets a caller read mid-sequence and believe
        # the answer.
        R: ['control/spawner.py:SpawnerControl.read',
            'control/spawner.py:SpawnerControl.ready',
            'control/spawner.py:plan',
            'control/spawner.py:click_plan',
            'control/spawner.py:position_of',
            'control/spawner.py:weapon_position',
            'control/spawner.py:attachment_position',
            'control/spawner.py:builtin_layout',
            'control/spawner.py:load_layout',
            'control/spawner.py:check_against_run',
            'control/spawner.py:record_goto',
            'control/spawner.py:shoot_parked',
            'control/spawner.py:Category.key',
            'control/spawner.py:PanelState.at',
            'control/spawner.py:PanelState.collapsed',
            'control/spawner.py:PanelState.entries_for'],
    },
    'evidence': {
        'what': '出事之后留下的东西：现场、信念、回路收尾',
        # ⚠ ALL R AND THAT IS LOAD-BEARING. Every entry here runs when
        # something has ALREADY gone wrong, and a diagnostic that drives is a
        # diagnostic that changes the scene it was called to photograph.
        # GunDriver.blocking_screen was written under the same rule on
        # 2026-08-07: it probes for the spawner panel only on a failure path,
        # and returns None when the probe itself raises, because a probe that
        # cannot run must not accuse.
        L2: [],
        # ⚠ dump_state DRIVES, AND THAT IS THE HAZARD WORTH THE TAG. It opens
        # Tab to photograph the loadout, so THE ROUTINE THAT RECORDS A FAILURE
        # CHANGES THE SCENE IT WAS CALLED TO RECORD -- and it is called at the
        # exact moment nobody wants another keypress in flight. It was filed R
        # here for one run on the assumption that a dump only reads; --check
        # said otherwise, and --why named ensure_tab().
        #
        # Not an argument for removing the Tab shot: the loadout is usually
        # the thing that explains the failure. It is an argument for the
        # caller knowing, which is what the level is for.
        L0: [],
        # shutdown() disarms the firmware and persists — a teardown that
        # DRIVES, and the docstring already says "call this, not just stop the
        # loop". Left as the last unfiled entry for a day; filing it is what
        # takes the ceiling to zero.
        L1: ['control/evidence.py:dump_state',
             'control/match.py:Dispatcher.shutdown'],
        R: ['control/evidence.py:full_frame',
            # ⚠ TWO ENTRIES STOOD HERE AND ARE GONE (2026-08-08):
            # control/lobby.py's placed_at / forget_placement, the two halves
            # of "which range has THIS PROCESS put the character on".
            #
            # They were filed here after being moved out of control/session.py
            # (as at_range / forget_range), and the filing recorded that the
            # belief kept shrinking: first every in-repo caller had to clear
            # it, then only one did. It reached zero when the teleport was
            # bound to the entry event alone — nothing decides anything from a
            # stored position any more, so there is no belief to report or to
            # forget. The rule now lives in one line of ensure_in_match and is
            # pinned by `pixi run placement`.
            'control/match.py:Dispatcher.register'],
    },
    'launch': {
        'what': '游戏进程本身：起、关、重启、还原窗口',
        # ⚠ A WHOLE AXIS THAT EXISTED FOR A DAY WITH NO LEVELS. control/lobby.py
        # grew the process states (NOT_RUNNING / NO_WINDOW / MINIMIZED) and
        # five entry points on 2026-08-07, every one of them on the unattended
        # recovery path, and the drift ratchet is what noticed -- the unfiled
        # count went 105 -> 112 and named them.
        #
        # It is a separate intent from `ready` rather than more rows under it,
        # and the reason is the one the module docstring gives: a dead game
        # renders a BRIGHT DESKTOP, which every pixel probe reads as
        # FULLBLEED, which all three lobby policies treat as a loading screen
        # -- so they wait out 300 s and report the game as slow. "Is there a
        # process" has to be asked BEFORE "what is on screen", and two
        # questions asked in a fixed order are two intents.
        L2: ['control/lobby.py:LobbyControl.restart_game'],
        L1: ['control/lobby.py:LobbyControl.ensure_running',
             'control/lobby.py:LobbyControl.quit_game'],
        # launch() is fire-and-verify: Steam forks, so there is no pid and no
        # exit code that means "the game came up". ensure_running is the proof
        # and therefore the guard. restore_window drives a real window change
        # and reads nothing back.
        L0: ['control/lobby.py:LobbyControl.launch',
             'control/lobby.py:LobbyControl.restore_window'],
        # The three-way answers the states are built on. game_minimized
        # returns True/False/None and the None is load-bearing: "no window to
        # ask about" is NOT "it is fine", and IsWindowVisible answering True
        # for an iconified window is what burned 420 s of LAUNCH_TIMEOUT
        # against a game that was driveable the whole time.
        R: ['control/focus.py:game_pids',
            'control/focus.py:game_minimized',
            'control/focus.py:game_hwnd',
            'control/focus.py:window_info'],
    },
    'frame': {
        'what': '拿一帧屏幕',
        # ALL R, AND THE INTENT EXISTS BECAUSE OF THE NAMES, NOT THE BODIES.
        # Every one of these is `return self.frames.<same thing>()` -- one
        # line, no decision, nothing to get wrong. What is worth a table entry
        # is that FOUR OBJECTS SPELL IT THE SAME WAY. An agent reading a
        # declaration sees `grab()` and cannot tell whose it is, and this
        # project's whole discipline is that the declaration is enough.
        #
        # They are not forwarders to be deleted, and the 2026-08-07 Rig unwrap
        # deliberately kept them when it deleted 33 of their neighbours: a Rig
        # IS the frame source it hands to ViewDriver, GunDriver and FireDriver,
        # so the three drivers hold the same object and honestly answer the
        # same question. Collapsing them would mean making callers reach past
        # the driver that owns the loop, which is the coupling the unwrap was
        # undoing, not a step further along it.
        #
        # So the fix for the homonym is the filing, not a rewrite: here is the
        # list, they all mean the same thing, and `full` is the only one with
        # anything to say (it blits the bands back to screen coordinates -- see
        # its docstring for the two detectors that need that and why).
        L2: [],
        L1: [],
        L0: [],
        R: ['control/aim.py:ViewDriver.grab',
            'control/aim.py:ViewDriver.flush',
            'control/gun.py:GunDriver.grab',
            'control/gun.py:GunDriver.full',
            'control/gun.py:GunDriver.flush',
            'control/fire.py:FireDriver.grab'],
    },
}

# ════════════════════════════════════════════════════════════════════
# Who holds the guard — the composition axis
# ════════════════════════════════════════════════════════════════════
#
# ⚠ THE LEVELS GRADE STRENGTH AND SAY NOTHING ABOUT COMPOSITION, and until
# 2026-08-07 that hole swallowed the whole point of L0. Its definition is "the
# guards live one level up, so calling it directly walks around them" -- a
# claim about ANOTHER function, made in prose, checked by nothing. Measured
# when it finally was: five of twelve filed L0s had no L1/L2 in control/ that
# called them at all.
#
# That is the level-shaped coupling defect. The driving layer publishes a
# mechanism and the EXPERIMENT layer carries the discipline not to misuse it,
# which inverts the dependency: control/ ends up relying on its callers'
# care. FireDriver.disarm's own docstring wrote it down as a design note --
# "--no-comp lives one layer up, in sweep.Rig.arm()" -- rather than as debt.
#
# So every L0 names its guard, and the machine checks the claim: the guard
# exists, it outranks the L0, and its body ACTUALLY CALLS IT. A guard that
# stopped calling its L0 is the interesting failure and the one prose cannot
# catch.

class UNGUARDED(str):
    """No guard anywhere, with a reason. -> an L0 that is a BARE MECHANISM.

    Not an escape hatch for "I could not find the guard". An entry here is a
    statement that nothing in the repo stands between a caller and this
    function, which is worth saying out loud precisely because the L0 tag
    otherwise implies somebody does.
    """


GUARDS = {
    'control/aim.py:ViewDriver.ads_tap':
        UNGUARDED('nothing in control/ calls it. GunDriver.ensure_ads is the '
                  'verified way into ADS and does NOT go through this — the '
                  'tap exists for calibration/capture_ads.py, which needs the '
                  'toggle without the settle and without the readback, '
                  'because the settle is what it is measuring.'),
    # ⚠ THE FIRST TWO GUARD NAMES HERE WERE BOTH GUESSES AND BOTH WRONG, and
    # the machine said so on its first run — `recenter` and `measure_travel`
    # are the functions these READ LIKE they belong under, and neither calls
    # them. That is the check earning its keep before the table was finished:
    # a guard nobody verified is exactly as good as no guard, and prose would
    # have carried the wrong pair indefinitely.
    'control/aim.py:ViewDriver.turn':
        'control/aim.py:ViewDriver.step_once',
    'control/aim.py:ViewDriver.home_to_clamp':
        'control/aim.py:ViewDriver.calibrate_pitch',
    'control/fire.py:FireDriver.arm': 'calibration/sweep.py:Rig.arm',
    # ⚠ BOTH GUARDS ARE THE SAME FUNCTION, IN calibration/. That is the
    # finding, not the filing: Rig.arm arms and then disarms so the pattern is
    # uploaded but OFF, which is what --no-comp means. Reach past it and
    # compensation goes ON, and the run meant to measure raw recoil measures
    # it compensated under a filename that says otherwise. A cross-layer guard
    # is legitimate here -- the decision IS the experiment's -- but it has to
    # be visible, and before this table it was one sentence in a docstring.
    # ── the spawner panel's rescue surface ──
    #
    # Three of the six have a guard that calls them and three DO NOT, and the
    # split is the finding rather than an omission. give_weapon and give_many
    # drive the panel through _spawn / _collapse_all, so those two are guarded
    # in the ordinary way. click_category, click_entry and goto are published
    # for probes that have to drive the panel BY HAND -- control/CLAUDE.md
    # calls them exactly that ("要手动救场，用 L0"), and a rescue surface with
    # something standing in front of it would not be one.
    #
    # Filing them UNGUARDED is what makes that a number: five published entry
    # points in this repo have nothing between a caller and the hardware, and
    # before this table it was zero sentences.
    'control/spawner.py:SpawnerControl.spawn':
        'control/spawner.py:SpawnerControl.give_weapon',
    'control/spawner.py:SpawnerControl.collapse_all':
        'control/spawner.py:SpawnerControl.give_many',
    'control/spawner.py:SpawnerControl.switch_to_slot2':
        'control/spawner.py:SpawnerControl.give_weapon',
    'control/spawner.py:SpawnerControl.click_category':
        UNGUARDED('the rescue surface. Published for probes that must drive '
                  'the panel by hand when the declarative entries cannot get '
                  'there; nothing in control/ calls it, and the discipline '
                  'that replaces a guard is sync() + read() -- which is a '
                  'habit, not a caller, and therefore not checkable here.'),
    'control/spawner.py:SpawnerControl.click_entry':
        UNGUARDED('same rescue surface as click_category. It clicks an entry '
                  'handed back by read() or goto(), and whether that entry is '
                  'still on screen is the caller\'s to know.'),
    'control/spawner.py:SpawnerControl.goto':
        UNGUARDED('same rescue surface. It walks to a node and reports the '
                  'path it took -- the `path` field is still collecting '
                  'evidence for whether this menu is an accordion, so its '
                  'callers are probes by design.'),

    # ensure_focus is the only thing that should ever call raise_game: it
    # retries three times, falls back to a countdown, and waits FOCUS_SETTLE_S
    # afterwards — the first frames after a foreground change are dropped by
    # the game, and raise_game alone returns before any of that.
    'control/focus.py:raise_game': 'control/focus.py:ensure_focus',

    # apply() is the only thing that holds a session: it opens the screen
    # once for a whole weapon's kitting instead of letting each helper open
    # and close its own. Reach past it and you get the alternation the churn
    # log measured at 80% of every Tab press in the corpus.
    'control/kitting.py:Kitter.session':
        'control/kitting.py:Kitter.apply',

    'control/gun.py:GunDriver.stir':
        UNGUARDED('a bare mechanism, and deliberately: it presses W then S to '
                  'test whether the ~20.5-minute evictions are an idle timer '
                  'or a clock. Nothing verifies the character moved, there is '
                  'no level above it, and its own docstring says it comes '
                  'straight back out if the interval does not stretch.'),
    'control/gun.py:GunDriver.ensure_inventory_closed':
        'control/gun.py:GunDriver.read_loadout',
    'control/lobby.py:LobbyControl.press_play':
        'control/lobby.py:LobbyControl.ensure_in_match',
    'control/lobby.py:LobbyControl.press_esc':
        'control/lobby.py:LobbyControl.ensure_in_match',
    'control/lobby.py:LobbyControl.dismiss_error':
        'control/lobby.py:LobbyControl.ensure_in_match',
    'control/lobby.py:LobbyControl.click_reconnect':
        'control/lobby.py:LobbyControl.ensure_in_match',
    'control/lobby.py:LobbyControl.click_exit':
        'control/lobby.py:LobbyControl.ensure_in_match',
    'control/lobby.py:LobbyControl.click_leave':
        'control/lobby.py:LobbyControl.exit_to_lobby',
    'control/lobby.py:LobbyControl.click_leave_confirm':
        'control/lobby.py:LobbyControl.exit_to_lobby',
    # ⚠ press_map's guard is ensure_map and NOT goto_range, even though
    # goto_range is the L1 everyone actually calls: goto_range goes through
    # ensure_map, and naming the nearest guard is what makes "has it stopped
    # calling me" a real question. M is a toggle, so ensure_map READS BEFORE
    # IT PRESSES -- the guard is the read, not the retry count.
    'control/map.py:MapControl.press_map':
        'control/map.py:MapControl.ensure_map',
    'control/lobby.py:LobbyControl.launch':
        'control/lobby.py:LobbyControl.ensure_running',
    'control/lobby.py:LobbyControl.restore_window':
        'control/lobby.py:LobbyControl.ensure_running',
    'control/inventory.py:InventoryControl.auto_equip':
        'control/inventory.py:InventoryControl.auto_equip_key',
    'control/inventory.py:InventoryControl.drag':
        'control/inventory.py:InventoryControl.unequip',
    'control/inventory.py:InventoryControl.right_click_equip':
        'control/inventory.py:InventoryControl.equip',
    'control/inventory.py:InventoryControl.right_click_unequip':
        'control/inventory.py:InventoryControl.unequip',
    # ensure_kit rather than equip: both call hold(), and ensure_kit is the
    # L2 that also owns clearing the stale `self.held` cache the docstring
    # warns about. Naming the strongest guard is the point -- a caller who
    # skips the L0 wants to know what the strongest thing they are skipping is.
    'control/inventory.py:InventoryControl.hold':
        'control/inventory.py:InventoryControl.ensure_kit',
    'control/inventory.py:InventoryControl.stow':
        'control/inventory.py:InventoryControl.clear_ground',
}


# Public callables that deliberately take no level, and why. This is the
# useful half of the table: an entry here is a decision, and --audit counts
# anything that is neither tiered nor listed here.
def _is_inert(name):
    """This callable commands no hardware. -> bool

    The predicate behind every "it is a fact, not an action" reason below, and
    it is the SAME classifier the R tag is checked with — so a function that
    quietly grows a mouse call is caught here exactly as it would be there.
    Reusing it is the point: a second implementation of "does this drive"
    would be free to disagree with the first.
    """
    ref = f'control/aim.py:ViewDriver.{name}'
    bodies = _bodies()
    if ref not in bodies:
        return False
    driving, _ = _driving(bodies)
    return ref not in driving


UNTIERED = {
    'control/aim.py:ViewDriver.goto_level': Reason(
        'DISABLED, NOT DELETED, and the distinction is the decision. The Rig '
        'forwarder that was its only caller went on 2026-08-07, so it now has '
        'zero callers — and a level would say it is a way to do something.',
        CODE,
        # "Zero callers" is the fastest-rotting claim in the repo: somebody
        # adds one and the reason becomes false in the direction that matters,
        # silently. This is the entry that most needed a predicate.
        lambda: not callers_of('goto_level')),

    'control/aim.py:ViewDriver.goto_pitch_centre': Reason(
        'disproven as an AIM (it targets the middle of the trackable band, '
        'which moves with the character\'s heading) but still load-bearing '
        'inside reaim(). Not a choice a caller should be offered, and not '
        'dead either.',
        CODE,
        # Both halves are claims. The one that can rot is the second: if reaim
        # stops calling it, it is dead and should be in the queue rather than
        # excused as "load-bearing".
        lambda: calls('control/aim.py', 'goto_pitch_centre', inside='reaim')),

    'control/aim.py:ViewDriver.pitch_scale': Reason(
        'a NUMBER, not an action. Kept public because capture records store '
        'it.',
        CODE, lambda: _is_inert('pitch_scale')),

    'control/aim.py:ViewDriver.open_loop': Reason(
        'a constructor for callers with nothing to close the loop with.',
        CODE, lambda: _is_inert('open_loop')),

    'control/aim.py:ViewDriver.retune': Reason(
        'reconfiguration, not aiming.',
        CODE, lambda: _is_inert('retune')),

    # ViewDriver.backend's entry stood here and went with the symbol
    # (2026-08-08). Its reason -- "a fact about the hardware, recorded next to
    # a capture" -- was true and is exactly why the property could not survive
    # the SendInput backend: with one backend left it recorded the constant
    # 'pico' into every capture's metadata. This ledger is a ratchet, so the
    # entry outliving the symbol turned surface-check RED rather than granting
    # a silent amnesty. That is the ratchet working.
}


def _load():
    """{'<dir>/<file>.py:<qual>': (first_doc_line, doc_lines, args)}"""
    out = {}
    for d in DIRS:
        for p in sorted((ROOT / d).glob('*.py')):
            try:
                tree = ast.parse(p.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            mod = f'{d}/{p.name}'

            defs = {}          # bare name -> (first, nlines, args), private too

            def walk(node, prefix=''):
                for n in node.body:
                    if isinstance(n, ast.ClassDef):
                        walk(n, n.name + '.')
                    elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        doc = ast.get_docstring(n) or ''
                        lines = [x for x in doc.split('\n') if x.strip()]
                        args = [a.arg for a in n.args.args if a.arg != 'self']
                        rec = (lines[0] if lines else '', len(lines), args)
                        defs[prefix + n.name] = rec
                        if n.name.startswith('_') or n.name == 'main':
                            continue
                        out[f'{mod}:{prefix}{n.name}'] = rec
                    # ⚠ `public = _private` IS PUBLIC API AND WAS INVISIBLE.
                    # control/spawner.py exposes six methods this way --
                    # goto, collapse_all, read, spawn, click_category,
                    # click_entry -- and every one of them is in
                    # control/CLAUDE.md's usage examples. A surface map that
                    # cannot see the documented entry points reports them as
                    # "GONE" the moment anyone files them, which is what it
                    # did on 2026-08-07. Resolved against defs, which holds
                    # the private ones too.
                    elif isinstance(n, ast.Assign) \
                            and len(n.targets) == 1 \
                            and isinstance(n.targets[0], ast.Name) \
                            and isinstance(n.value, ast.Name) \
                            and not n.targets[0].id.startswith('_'):
                        src = defs.get(prefix + n.value.id)
                        if src is not None:
                            out[f'{mod}:{prefix}{n.targets[0].id}'] = src
            walk(tree)
    return out


def _fmt(ref, info, width=76):
    first, _, args = info.get(ref, ('', 0, []))
    name = ref.split(':')[-1]
    sig = f'{name}({", ".join(args)})'
    if len(sig) > 40:
        sig = sig[:37] + '...)'
    return f'      {sig:<42s} {first[:width]}'


def show(names, info):
    for key in names:
        spec = INTENTS[key]
        all_refs = [r for lvl, _ in LEVELS for r in (spec.get(lvl) or [])]
        mods = {r.split(':')[0] for r in all_refs}
        # The module only earns a column when the intent actually spans more
        # than one -- an intent served by a single file would otherwise print
        # the same path on every row, which is noise wearing the shape of data.
        print(f'\n══ {key}  —  {spec["what"]}'
              + ('' if len(mods) > 1 else f'   [{mods.pop()}]'))
        for lvl, blurb in LEVELS:
            refs = spec.get(lvl) or []
            if not refs:
                continue
            print(f'\n  {lvl:<3s} {blurb}')
            for ref in refs:
                if ref not in info:
                    print(f'      {ref}   ⚠ GONE — the table names a callable '
                          f'that no longer exists')
                    continue
                line = _fmt(ref, info, width=64)
                if len(mods) > 1:
                    line += f'   [{ref.split(":")[0]}]'
                print(line)


def audit(info):
    """Public callables no intent claims and UNTIERED does not excuse."""
    claimed = {r for s in INTENTS.values()
               for lvl, _ in LEVELS for r in (s.get(lvl) or [])}
    claimed |= set(UNTIERED)
    # Only the driving layers: detector/ is frame -> semantics and picked by
    # what it reads, not by what a caller wants done.
    rest = [r for r in sorted(info)
            if r not in claimed and r.split('/')[0] == 'control']
    print(f'\n══ control/ 里没有归入任何意图的 public 入口: {len(rest)}\n')
    by_mod = collections.defaultdict(list)
    for r in rest:
        by_mod[r.split(':')[0]].append(r)
    for mod in sorted(by_mod, key=lambda m: -len(by_mod[m])):
        print(f'  {mod}  ({len(by_mod[mod])})')
        for r in by_mod[mod]:
            print(_fmt(r, info, width=56))
    print(f'\n  ({len(UNTIERED)} more are listed in UNTIERED with a reason)')
    return len(rest)


# Attribute reads that mean "this body commands the hardware". Names, not
# imports: every driver reaches its device through a held object
# (`self.mouse.move`, `self.pointer.drag`), which is exactly the shape
# tools/check_layering.py cannot see -- it parses imports only, and this repo
# has paid for two reach-throughs it missed.
DRIVES = ('mouse', 'pointer', 'pico', 'kb')
# Cross-module names that command without going through a `.mouse` attribute
# in the caller's own body. Seeds only — everything else is derived below.
DRIVING_SEEDS = ('click_at', 'press_map', 'give_many', 'ensure_panel')

# ⚠ `x.get(k)` CANNOT BE PROVEN TO BE THE REPO'S `get`. Some class in the
# scanned tree defines a driving `get`, so linking every `.get(` to it wired
# ViewDriver.travel's `self._travel.get(key)` -- a dict lookup -- into the
# inventory driver, and the honest R tag went red. Derived from dir() rather
# than hand-listed: a hand-list is a thing that rots, and the question being
# asked is exactly "could this receiver be a builtin container".
AMBIGUOUS = (set(dir(dict)) | set(dir(list)) | set(dir(str)) | set(dir(set))
             | set(dir(tuple)))


# The one callable whose hardware contact is PARKING THE CURSOR, and the reads
# that reach the hardware only through it. Every InventoryControl read grabs a
# frame, and a frame grab moves the cursor to PARK_XY first — a point chosen to
# be off every interactive element, because a hovered slot tile draws a TOOLTIP
# OVER ITSELF and slot reads are template matches. The move exists so the
# detection is not fooled; the game does not react to it.
#
# ⚠ THIS IS AN ESCAPE HATCH AND THE THIRD ONE THIS FILE HAS HAD. The other two
# were prose predicates ("mentions ⚠", "mentions <param>=True") and BOTH waved
# through the exact case they existed to catch. So this one asserts nothing on
# its own: _cursor_only() re-runs the real classifier with park() stubbed out,
# and the tag is allowed only if the ref stops driving — i.e. park really was
# the only path. Add a click to park and all twelve go red in the same run.
CURSOR_ONLY_VIA = 'control/inventory.py:InventoryControl.park'
CURSOR_ONLY_CALLS = frozenset({'move_to', 'cursor_pos'})
#
# ⚠ loadout AND survey WERE IN THIS SET AND DID NOT BELONG. They were filed
# here on the assumption that every InventoryControl read only parks -- and
# survey calls tab_up(), which PRESSES TAB. The hatch caught it the moment the
# classifier could see far enough to notice, which is the whole reason it
# re-runs the real classifier instead of asserting something of its own.
CURSOR_ONLY = frozenset({
    'control/inventory.py:InventoryControl.park',
    'control/inventory.py:InventoryControl.frame',
    'control/inventory.py:InventoryControl.look',
    'control/inventory.py:InventoryControl.read_slots',
    'control/inventory.py:InventoryControl.read_weapons',
    'control/inventory.py:InventoryControl.slot_state',
    'control/inventory.py:InventoryControl.slot_states',
    'control/inventory.py:InventoryControl.gun_slot',
    'control/inventory.py:InventoryControl.plate_ink',
    'control/inventory.py:InventoryControl.sync',
})


def _cursor_only(bodies):
    """Which CURSOR_ONLY claims hold. -> (ok_refs, [(ref, why)])

    Two conditions, and the first is the one that rots:

      park() itself must still touch nothing but the cursor. Its whole
      standing as a non-event depends on going to a point the game ignores.
      A click added here silently converts twelve R tags into lies.

      the ref must stop driving once park is stubbed. That is the real
      classifier answering the real question, not a second implementation of
      it agreeing with itself.
    """
    park = bodies.get(CURSOR_ONLY_VIA)
    if park is None:
        return set(), [(CURSOR_ONLY_VIA, 'park() is gone; CURSOR_ONLY has no '
                                         'subject and every entry under it is '
                                         'now an unchecked claim')]
    # ⚠ THE LEAF, NOT THE HANDLE. `self.pointer.move_to(...)` reaches the
    # hardware THROUGH .pointer and what it does there is move_to — so the
    # question is what is called at the end of the chain, not that .pointer is
    # on it. Collecting the whole chain declared park() in breach of its own
    # rule on the first run, which at least proved the rule was being applied.
    touched = set()
    for n in ast.walk(park):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        chain, v = set(), n.func
        while isinstance(v, ast.Attribute):
            chain.add(v.attr)
            v = v.value
        if chain & set(DRIVES):
            touched.add(n.func.attr)
    stray = sorted(touched - CURSOR_ONLY_CALLS)
    if stray:
        return set(), [(CURSOR_ONLY_VIA,
                        f'park() now reaches {", ".join(stray)} — it is no '
                        f'longer "move to a point the game ignores", so the '
                        f'{len(CURSOR_ONLY)} R tags resting on it are void')]
    # Re-run the production classifier with park emptied out.
    stub = ast.parse('def _stub():\n    return None').body[0]
    without = dict(bodies, **{CURSOR_ONLY_VIA: stub})
    still, _ = _driving(without)
    bad = [(r, 'claims CURSOR_ONLY but still drives with park() stubbed out — '
               'it has another path to the hardware')
           for r in sorted(CURSOR_ONLY) if r in still]
    return set(CURSOR_ONLY) - {r for r, _ in bad}, bad


def _driving(bodies):
    """Every callable that ends up commanding the hardware. -> {ref}

    ⚠ TRANSITIVE, AND THAT IS THE WHOLE POINT. Checking only for a `.mouse`
    in the body and a list of known driving names let ViewDriver.recenter pass
    as R: it drives through `self._move_tracked`, one private hop away. One
    hop is all it takes, and a check that a single indirection defeats is a
    check that reports the shape of the code rather than what it does.

    ⚠ THE CLOSURE IS GLOBAL BY BARE METHOD NAME, and the per-module version
    it replaces was WRONG IN THE DANGEROUS DIRECTION. control/stock.py's
    restock() drives through `ac.drag(...)`, where `ac` is a PARAMETER holding
    an InventoryControl — so a per-module closure resolves nothing and reports
    the repo's busiest driver as inert. That is a false green, which is the
    one outcome this check exists to prevent. (The comment here used to claim
    the approximation erred toward over-reporting. It did not. Written down
    because the claim was plausible and stood for about an hour.)

    Global matching over-reports instead: two unrelated classes sharing a
    method name are conflated, so an R tag can be refused for the wrong
    reason. That failure is loud — somebody argues with the tag — where the
    other one is silent.
    """
    # ⚠ AN ATTRIBUTE CALL AND A BARE-NAME CALL RESOLVE TO DIFFERENT THINGS,
    # and collapsing them produced a chain worth printing in full:
    #
    #   ViewDriver.track_still --measure_pair()--> ViewTracker.measure_pair
    #     --round()--> Collector.round --bare_host()--> ... --> drag()
    #
    # `round()` there is the BUILTIN. Matching it against a method named
    # `round` linked the view tracker to the inventory driver through eight
    # hops of nothing, and flagged two honest R tags. A method can only ever
    # be reached as `x.name()`; a bare `name()` is a module-level function or
    # a builtin. Keeping the two kinds apart costs one tuple and removes the
    # entire class of collision.
    direct, calls = set(), {}
    for ref, fn in bodies.items():
        names = set()
        # ⚠ A DRIVING HANDLE IS ONLY DRIVING WHEN SOMETHING IS CALLED THROUGH
        # IT. `self.pointer.pico is not None` -- InventoryControl.can_press,
        # verbatim -- touches .pointer and .pico and COMMANDS NOTHING: it asks
        # whether a Pico exists so a four-minute run can refuse in the first
        # second instead of the fourth minute. Counting the bare read made the
        # checker refuse an honest R tag on a capability question.
        #
        # `bases` is every attribute on the left of a call's dot, walking the
        # whole chain: `self.pointer.move_to(...)` contributes .pointer AND
        # .move_to, `self.pointer.pico.key(...)` contributes .pointer and
        # .pico. A read that never becomes a call contributes nothing.
        bases = set()
        # ⚠ AND THE HANDLE CAN BE PARKED IN A LOCAL FIRST. Tightening this to
        # "only counts as a call base" fixed can_press (which reads
        # `self.pointer.pico is not None` and commands nothing) and broke
        # SpawnerControl.switch_to_slot2 in the same edit:
        #
        #     mouse = self.pointer.pico      # a load, no call
        #     mouse.key(HID_KEY_2, 60)       # a call, on a bare local
        #
        # That is a KEYPRESS, and it read as inert. The two shapes are one
        # line apart and the difference between them is whether the handle is
        # ever called through -- not whether the call happens to be spelled
        # with the attribute still attached.
        bound = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Attribute):
                v, chain = n.value, set()
                while isinstance(v, ast.Attribute):
                    chain.add(v.attr)
                    v = v.value
                if chain & set(DRIVES):
                    bound |= {x.id for x in n.targets if isinstance(x, ast.Name)}
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                v = n.func
                if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) \
                        and v.value.id in bound:
                    direct.add(ref)
                if isinstance(v, ast.Name) and v.id in bound:
                    direct.add(ref)
                while isinstance(v, ast.Attribute):
                    bases.add(v.attr)
                    v = v.value
        if bases & set(DRIVES):
            direct.add(ref)
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Attribute):
                    if f.attr not in AMBIGUOUS:
                        names.add(('attr', f.attr))
                    if f.attr in DRIVING_SEEDS:
                        direct.add(ref)
                elif isinstance(f, ast.Name):
                    names.add(('name', f.id))
                    if f.id in DRIVING_SEEDS:
                        direct.add(ref)
        calls[ref] = names

    driving = set(direct)
    by_name = collections.defaultdict(set)
    for ref in bodies:
        qual = ref.split(':')[1]
        by_name[('attr' if '.' in qual else 'name', qual.split('.')[-1])].add(ref)

    # ⚠ A NAME MANY CLASSES DEFINE RESOLVES TO NONE OF THEM. `classify` has
    # eleven owners and `read` has several, so `self.tab.classify(...)` linked
    # InventoryControl.tab_open to AmmoDetector.classify, then to
    # ScopeVerifier.read, then to ensure_tab -- four hops of pure homonym, and
    # a 41x18 screenshot was reported as driving the mouse.
    #
    # So a call only resolves when every definition of that name agrees. All
    # of them drive -> the call drives. None -> it does not. MIXED -> unknown,
    # and unknown is not an accusation. The chains that matter survive it:
    # `drag` has two owners and both drive, `ensure_tab` and `_move_tracked`
    # have one each.
    changed = True
    while changed:
        changed = False
        for ref, names in calls.items():
            if ref in driving:
                continue
            for nm in names:
                owners = by_name.get(nm, set())
                if owners and owners <= driving:
                    driving.add(ref)
                    changed = True
                    break
    return driving, direct


def _gated(fn, direct, driving_names=frozenset()):
    """Is ALL of this body's driving behind a declared, default-off switch?

    ⚠ THIS HATCH WAS TOO WIDE TWICE, and each version was caught only by
    feeding the check a tag that SHOULD fail:

      v1  excused any docstring containing '⚠'. This repo puts ⚠ on nearly
          everything worth reading, so it waved through ViewDriver.recenter,
          which drives unconditionally.
      v2  excused any function with a default-False parameter named in the
          docstring as `<param>=True`. goto_midline mentions `measure=True`
          while driving on every path, so it went green too.

    Both times the green answer was the interesting one, and both times a
    single deliberately-wrong tag would have missed it — v1 was found by
    testing two, v2 by testing five. A gate is only tested from the side it
    is supposed to refuse.

    v3 is syntactic instead of textual: the switch must default to False, be
    named in the docstring, AND every driving call in the body must sit inside
    an `if` that tests it. A body that touches the hardware itself (`direct`)
    cannot be gated at all — there is no branch to hide behind.

    ⚠ "EVERY DRIVING CALL", NOT "EVERY CALL". The first cut of v3 required all
    of them and so refused travel(), whose one gated call sits among half a
    dozen dict lookups that are obviously not driving. Over-strict fails the
    same way over-loose does — the tag stops meaning anything, it just gets
    argued with instead of trusted.
    """
    if fn in direct:
        return False
    args = fn.args
    if not args.defaults:
        return False
    doc = ast.get_docstring(fn) or ''
    flags = {a.arg for a, d in zip(args.args[-len(args.defaults):],
                                   args.defaults)
             if isinstance(d, ast.Constant) and d.value is False
             and f'{a.arg}=True' in doc}
    if not flags:
        return False

    # Every Call in the body, with the flags its enclosing `if`s test on.
    guarded, seen = [], set()

    def walk(node, tests):
        for n in ast.iter_child_nodes(node):
            if isinstance(n, ast.If):
                names = {x.id for x in ast.walk(n.test)
                         if isinstance(x, ast.Name)}
                for b in n.body:
                    walk(b, tests | names)
                for b in n.orelse:
                    walk(b, tests)         # the else branch is NOT gated
                continue
            if isinstance(n, ast.IfExp):
                names = {x.id for x in ast.walk(n.test)
                         if isinstance(x, ast.Name)}
                walk_expr(n.body, tests | names)
                walk_expr(n.orelse, tests)
                continue
            if isinstance(n, ast.Call):
                seen.add(id(n))
                guarded.append((n, tests))
            walk(n, tests)

    def walk_expr(node, tests):
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                seen.add(id(n))
                guarded.append((n, tests))

    walk(fn, frozenset())
    # A call the walker never reached is a call nobody proved is gated.
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and id(n) not in seen:
            guarded.append((n, frozenset()))

    def name_of(call):
        f = call.func
        return f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', '')

    drivers = [(c, t) for c, t in guarded if name_of(c) in driving_names]
    return bool(drivers) and all(t & flags for _, t in drivers)


# The classifier's own ground truth, decided by reading the bodies. It has
# been wrong three times in one afternoon -- per-module closure missed
# `ac.drag` through a parameter, the builtin `round()` linked the view tracker
# to the inventory driver, and `dict.get` did it again -- and every one of
# those was found by hand. A classifier nothing tests is one that reports its
# last bug as a fact.
#
# Two-sided on purpose: three of these MUST come back R, and a check that only
# lists drivers passes trivially by calling everything a driver.
SELFTEST = (
    ('control/stock.py:restock', True,
     'drives through `ac.drag`, where ac is a PARAMETER'),
    ('control/stock.py:tidy', True, 'same, one level down'),
    ('control/inventory.py:InventoryControl.look', True,
     'parks the cursor before the grab, so reading the Tab screen MOVES it'),
    ('control/inventory.py:InventoryControl.drag', True, 'touches .pointer'),
    ('control/aim.py:ViewDriver.recenter', True,
     'drives through the private _move_tracked -- one hop is all it takes'),
    ('control/aim.py:ViewDriver.track_still', False,
     'watches; commands nothing'),
    ('control/aim.py:ViewDriver.absolute_offset', False, 'a question'),
    ('control/inventory.py:InventoryControl.plan_kit', False,
     'pure -- no game, no screen'),
    ('control/spawner.py:SpawnerControl.plan', False, 'clicks nothing'),
    # ⚠ THESE THREE PIN THE `AMBIGUOUS` FIX, and they are here because the
    # first nine did not. Deleting AMBIGUOUS turned the check red — but via
    # travel's TAG, not via the self-test, so the message blamed a tag for a
    # classifier bug. Empirically these are the control/ callables whose
    # verdict flips when a container method is allowed to resolve to a repo
    # method of the same name; each one is a plain JSON/dict reader.
    ('control/spawner.py:load_layout', False,
     'reads a scrape file. Its `.get(` is a dict, not the repo method'),
    ('control/aim.py:_load_travel', False, 'reads calibration/artifacts/pitch JSON'),
    ('control/match.py:Dispatcher._cond_met', False, 'evaluates a condition'),
    # ⚠ THESE TWO PIN THE HOMONYM RULE, and they are a matched pair on
    # purpose: one must come back R and the other must NOT, and the naive
    # classifier got them the same way round for the same wrong reason.
    ('control/inventory.py:InventoryControl.tab_open', False,
     'is one 41x18 crop and a classify(). `classify` has eleven owners, so '
     'linking it to any single one of them is a homonym, not a call'),
    ('control/stock.py:read_stock', True,
     'PRESSES TAB. It is named read_ and it opens the inventory screen to do '
     'it -- the one real driver the homonym rule must not lose'),
)


def _selftest(driving):
    """Does the driving classifier still agree with what the bodies say?"""
    bad = []
    for ref, want_drives, why in SELFTEST:
        got = ref in driving
        if got != want_drives:
            bad.append(f'  ✗ {ref}\n      classifier says '
                       f'{"drives" if got else "R"}, but it {why}')
    if bad:
        print('\n'.join(bad))
        print(f'\n{len(bad)}/{len(SELFTEST)} classifier self-test failure(s). '
              f'The R tags below cannot be trusted until this is green.')
    return not bad


def check(info):
    """R must not drive. -> exit code.

    THE ONE PART OF A LEVEL A MACHINE CAN HOLD. L2 vs L1 is about the strength
    of a promise, and no parser reads promises. But "does this body command the
    hardware" is a syntactic question, and it is the question the R tag answers
    — so R is the tag that cannot rot.

    It earned its keep before it was finished: writing the aim table by hand,
    `calibrate_pitch` was filed R because it reads like a question ("find the
    band"). Its body opens with home_to_clamp(+1) and a mouse.move loop. The
    name is the part that lied, which is the whole reason the tag exists, and
    the reason the tag needs a check under it.
    """
    bodies = _bodies()

    driving, direct = _driving(bodies)
    # The classifier first. A wrong classifier makes every verdict below
    # meaningless in whichever direction it is wrong, so it does not get to
    # report on the tags until it agrees with the bodies.
    if not _selftest(driving):
        return 1
    direct_fns = {bodies[r] for r in direct if r in bodies}
    # Names that resolve to something driving — what `_gated` needs to tell a
    # gated hardware call from a gated dict lookup. Global, like the closure:
    # a per-module version cannot see `ac.drag` where `ac` is a parameter.
    driving_names = {r.split(':')[1].split('.')[-1] for r in driving}
    cursor_ok, cursor_bad = _cursor_only(bodies)
    bad = list(cursor_bad)
    for spec in INTENTS.values():
        for ref in (spec.get(R) or []):
            fn = bodies.get(ref)
            if fn is None:
                bad.append((ref, 'tagged R but no such callable'))
                continue
            if ref not in driving:
                continue
            if ref in cursor_ok:
                continue                    # verified cursor-park-only
            if _gated(fn, direct_fns, driving_names):
                continue                    # declared, default-off escape
            how = ('its own body touches ' + ', '.join(
                sorted({f'.{n.attr}' for n in ast.walk(fn)
                        if isinstance(n, ast.Attribute) and n.attr in DRIVES}))
                   ) if ref in direct else 'it reaches the hardware through a '\
                                           'call chain'
            bad.append((ref, f'tagged R but it drives — {how}'))
    for ref, why in bad:
        print(f'  ✗ {ref}\n      {why}')
    if bad:
        print(f'\n{len(bad)} R-tagged callable(s) drive something. Either the '
              f'tag is wrong or the body is.')
        return 1
    n = sum(len(s.get(R) or []) for s in INTENTS.values())
    print(f'  ✓ classifier self-test {len(SELFTEST)}/{len(SELFTEST)} '
          f'({sum(1 for _, d, _ in SELFTEST if not d)} of them must come back '
          f'R, so "call everything a driver" does not pass)')
    print(f'  ✓ {n} R-tagged callables, none of them commands the hardware')
    return guards(bodies)


def _reaches(node, leaf):
    """Does this body put `leaf` in front of a caller? Two shapes, both real.

        self.press_esc()                              a call
        self._await_frame(pred, t, self.press_map)    HANDED TO A PUMP

    ⚠ THE SECOND ONE IS NOT A CONCESSION, it is how half of control/ is
    written: MapControl.ensure_map is `_await_frame(pred, timeout,
    self.press_map)` — press, look, press again until the screen agrees. A
    check that only counted calls declared that guard broken on its first run,
    and the honest reading is that ensure_map guards press_map exactly as much
    as if it had called it inline.

    ⚠ AND IT IS DELIBERATELY NARROWER THAN "the name appears somewhere". The
    reference has to be an ARGUMENT to a call — something is receiving it and
    will run it. `self._retry = self.press_map` stored on the object does not
    count, because nothing here can say whether that field is ever used, and
    the two previous times a hatch in this file was widened to "mentions it"
    (⚠ in the docstring, then `<param>=True` anywhere) it waved through the
    exact case it existed to catch.
    """
    # ⚠ BARE NAMES TOO, and leaving them out made this blind to an entire
    # module. control/focus.py is module-level functions all the way down, so
    # `ensure_focus` reaches `raise_game` as `_take_focus(...)` -> `raise_game()`
    # -- two bare Name calls, no attribute anywhere. The check declared a real
    # guard broken and the only symptom was a confident error message.
    #
    # A bare name is riskier than an attribute in general (builtins, locals),
    # but not here: this asks whether ONE named target appears inside ONE
    # named body, which is a far narrower question than the call-graph
    # closure's.
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr == leaf:
            return True
        if isinstance(f, ast.Name) and f.id == leaf:
            return True
        for a in list(n.args) + [k.value for k in n.keywords]:
            if isinstance(a, ast.Attribute) and a.attr == leaf:
                return True
            if isinstance(a, ast.Name) and a.id == leaf:
                return True
    return False


def guards(bodies=None):
    """Every L0 names its guard, and the guard really calls it. -> exit code.

    THE SECOND MECHANISABLE PART OF A LEVEL, and it is a different kind of
    claim from R's. R is about one body. This is about a RELATION between two,
    which is the only part of "high cohesion, low coupling" a parser can hold:
    the level says how strong a promise is, and this says who is standing in
    front of the weak ones.

    Three ways to fail, and the third is the one prose could never catch:

      unfiled   an L0 with no GUARDS entry. The tag asserts a guard exists;
                somebody has to name it.
      missing   the named guard is not a callable, or does not outrank the L0
      SILENT    the guard exists, outranks it, and NO LONGER CALLS IT. Nothing
                breaks, nothing warns, and the L0 quietly becomes reachable
                only by walking around a guard that has stopped guarding.
    """
    bodies = _bodies() if bodies is None else bodies
    lv = _levels()
    l0s = [r for r, l in lv.items() if l == L0]
    rank = {L0: 0, L1: 1, L2: 2}
    bad = []

    for ref in sorted(l0s):
        g = GUARDS.get(ref)
        if g is None:
            bad.append((ref, 'filed L0 but names no guard. "The guards live '
                             'one level up" is a claim about another '
                             'function — say which.'))
            continue
        if isinstance(g, UNGUARDED):
            if not g.strip():
                bad.append((ref, 'UNGUARDED with no reason'))
            continue
        node = bodies.get(g)
        if node is None:
            bad.append((ref, f'guard {g} is not a callable'))
            continue
        if rank.get(lv.get(g), -1) <= rank[L0]:
            bad.append((ref, f'guard {g} is {lv.get(g) or "unfiled"} — a guard '
                             f'has to outrank what it guards'))
            continue
        # ⚠ AN ALIAS AND ITS TARGET ARE ONE FUNCTION, so a guard that calls
        # `self._click_category(...)` is guarding `click_category` -- and
        # nothing in the repo ever spells the public name, because the public
        # name exists FOR CALLERS, which is the whole point of publishing it.
        # Checking only the ref's own spelling declared six guards broken on
        # the first run after control/spawner.py's rescue surface was filed.
        # Same AST node means same function; that is exact, not a heuristic.
        # ⚠ THE ALIASES OF THE GUARDED THING, NOT OF THE GUARD. The first
        # version read `bodies.items() if n is node` where `node` is the
        # GUARD's body, so it asked whether give_many calls give_many. Two
        # honest guard claims went red, and the message printed the giveaway
        # ("neither calls collapse_all/give_many()") one line before I read it.
        target = bodies.get(ref)
        same = {r.split(':')[1].split('.')[-1]
                for r, nd in bodies.items() if nd is target}
        leaf = ref.split(':')[1].split('.')[-1]
        # ⚠ ONE PRIVATE HOP, IN THE GUARD'S OWN FILE, AND NO MORE. ensure_focus
        # does not call raise_game itself: it calls _take_focus, which retries
        # it three times. The guard is still guarding — the retry loop and the
        # FOCUS_SETTLE_S wait are exactly what a direct caller would skip — and
        # a rule that could not see one private hop would have to call this
        # UNGUARDED, which is a worse lie than the one it prevents.
        #
        # Bounded three ways, because widening a hatch in this file has twice
        # waved through the case it existed to catch: the hop must be PRIVATE
        # (`_name`), it must be defined in the SAME FILE as the guard, and
        # there is exactly one of them. A cross-module chain is not a guard
        # relationship, it is a coincidence with a call in it.
        spellings = {leaf} | same
        reached = any(_reaches(node, nm) for nm in spellings)
        if not reached:
            gmod = g.split(':')[0]
            for r2, n2 in bodies.items():
                if not r2.startswith(gmod + ':'):
                    continue
                hop = r2.split(':')[1].split('.')[-1]
                if not hop.startswith('_') or hop.startswith('__'):
                    continue
                if _reaches(node, hop) and any(_reaches(n2, nm)
                                               for nm in spellings):
                    reached = True
                    break
        if not reached:
            spell = '/'.join(sorted({leaf} | same))
            bad.append((ref, f'guard {g} neither calls {spell}() nor hands it '
                             f'to a pump any more. It is named as the thing '
                             f'standing in front of this and it has stopped.'))

    for ref in sorted(GUARDS):
        if lv.get(ref) != L0:
            bad.append((ref, f'has a GUARDS entry but is '
                             f'{lv.get(ref) or "unfiled"}, not L0 — delete '
                             f'the line or the table drifts into fiction'))

    for ref, why in bad:
        print(f'  ✗ {ref}\n      {why}')
    if bad:
        print(f'\n{len(bad)} guard claim(s) do not hold.')
        return 1
    # The UNTIERED table is a ledger like any other: every entry says why
    # a public callable takes no level, and until 2026-08-08 nothing
    # checked those reasons. `goto_level` is the one that made the case --
    # it is excused as having ZERO CALLERS, which is the claim in this
    # repo that rots fastest and most silently: somebody adds one and the
    # reason becomes false in the direction that matters.
    led_lines, led_bad = ledger_audit([('UNTIERED', UNTIERED)])
    for line in led_lines:
        print(line)
    for key, why in led_bad:
        print('  \u2717 %s' % key)
        print('      ledger reason: %s' % why)
    if led_bad:
        print('\n%d UNTIERED reason(s) no longer hold.' % len(led_bad))
        return 1

    loose = sum(1 for r in l0s if isinstance(GUARDS.get(r), UNGUARDED))
    print(f'  ✓ {len(l0s)} L0s, every one names a guard that outranks it and '
          f'calls it ({loose} declared bare mechanisms)')
    return tags(bodies)


def tags(bodies=None):
    """The docstring's own tag must be the table's. -> exit code.

    TWO PLACES HOLD THE LEVEL and they are edited by different motives. The
    docstring changes when somebody fixes the function; the table changes when
    somebody audits the surface. Nothing made them meet, so they drifted, and
    the drift is invisible from either side alone.

    Found the moment it ran: FireDriver.disarm got its firmware readback on
    2026-08-07 and its first line became "L1 — Compensation off, CONFIRMED BY
    READING THE FIRMWARE BACK", while this table still filed it L0 under a
    comment explaining that the readback does not exist. A caller reading the
    function saw L1; `pixi run surface fire` said L0. Both were confident.

    ⚠ ONLY WHEN THE FIRST LINE CARRIES A TAG. Most callables do not, and
    demanding one everywhere would turn this into a docstring-format rule --
    the drift-catching part is the DISAGREEMENT, not the absence.
    """
    bodies = _bodies() if bodies is None else bodies
    bad = []
    for ref, want in sorted(_levels().items()):
        node = bodies.get(ref)
        doc = ast.get_docstring(node) if node is not None else None
        if not doc:
            continue
        m = re.match(r'\s*(L2|L1|L0|R)\b', doc.splitlines()[0])
        if m and m.group(1) != want:
            bad.append((ref, f'its first line says {m.group(1)}, this table '
                             f'says {want}. One of them was updated when the '
                             f'function changed and the other was not.'))
    for ref, why in bad:
        print(f'  ✗ {ref}\n      {why}')
    if bad:
        print(f'\n{len(bad)} level tag(s) disagree with the table.')
        return 1
    n = sum(1 for ref in _levels()
            if (d := ast.get_docstring(bodies[ref]) if ref in bodies else None)
            and re.match(r'\s*(L2|L1|L0|R)\b', d.splitlines()[0]))
    print(f'  ✓ {n} first-line tags agree with the table')
    return 0


def _bodies():
    """{ref: FunctionDef} for every def in the scanned layers, private too.

    Private ones are included on purpose: ViewDriver.recenter drives through
    `_move_tracked`, and a closure that only saw public names would call it
    inert.
    """
    out = {}
    for d in DIRS:
        for p in sorted((ROOT / d).glob('*.py')):
            try:
                tree = ast.parse(p.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            mod = f'{d}/{p.name}'

            def walk(node, prefix=''):
                for n in node.body:
                    if isinstance(n, ast.ClassDef):
                        walk(n, n.name + '.')
                    elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out[f'{mod}:{prefix}{n.name}'] = n
                # ⚠ CLASS-LEVEL ALIASES, AND THEY WERE A FALSE GREEN.
                # control/spawner.py publishes its rescue surface as
                # `click_category = _click_category` -- six of them. _load()
                # was taught about aliases when they showed up as "GONE" in
                # the audit; THIS function never was, so every aliased entry
                # had no body here, and _driving() answered "does not drive"
                # about six blind-click entry points on the busiest driving
                # module in the repo.
                #
                # "No body to judge" and "judged, and it is inert" are not the
                # same answer, and collapsing them lands on the side that
                # cannot fail. The alias points at a def in the same class, so
                # resolving it is exact rather than a guess.
                for n in node.body:
                    if not (isinstance(n, ast.Assign)
                            and isinstance(n.value, ast.Name)):
                        continue
                    src = out.get(f'{mod}:{prefix}{n.value.id}')
                    if src is None:
                        continue
                    for tgt in n.targets:
                        if isinstance(tgt, ast.Name):
                            out[f'{mod}:{prefix}{tgt.id}'] = src
            walk(tree)
    return out


def why(ref):
    """Print the call chain that makes `ref` count as driving.

    A verdict nobody can audit gets switched off the first time it is
    inconvenient. Every wrong answer this checker gave today was diagnosed by
    printing the chain by hand, twice through a name collision eight hops
    long; the third time it became this.
    """
    bodies = _bodies()
    driving, direct = _driving(bodies)
    if ref not in bodies:
        near = [r for r in bodies if r.endswith(ref) or ref in r][:8]
        print(f'no such callable: {ref}'
              + ('\n  did you mean:\n    ' + '\n    '.join(near) if near else ''))
        return 1
    if ref not in driving:
        print(f'  {ref}\n  does NOT drive — nothing in it reaches the hardware')
        return 0

    calls = {}
    for r, fn in bodies.items():
        s = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Attribute) and f.attr not in AMBIGUOUS:
                    s.add(('attr', f.attr))
                elif isinstance(f, ast.Name):
                    s.add(('name', f.id))
        calls[r] = s
    by = collections.defaultdict(set)
    for r in bodies:
        q = r.split(':')[1]
        by[('attr' if '.' in q else 'name', q.split('.')[-1])].add(r)

    def path(r, seen=()):
        if r in direct:
            touched = sorted({f'.{n.attr}' for n in ast.walk(bodies[r])
                              if isinstance(n, ast.Attribute)
                              and n.attr in DRIVES})
            return [f'{r}   ← touches {", ".join(touched) or "a seed call"}']
        for kind, nm in sorted(calls.get(r, ())):
            owners = by.get((kind, nm), set())
            # ⚠ THE SAME RULE THE VERDICT USED, and it was not. This read
            # `if tgt in driving`, i.e. follow the link when ANY owner of that
            # name drives -- while _driving() only follows it when EVERY owner
            # does. So the explainer walked links the judge had rejected.
            #
            # Measured 2026-08-08: control/evidence.py:dump_state was flagged,
            # and --why blamed `close()` -> calibration/sweep.py:Rig.close.
            # `close` has 26 owners and exactly one of them drives, so that
            # link is not why it was flagged; the real path was ensure_tab().
            # An explanation that names the wrong cause is worse than none --
            # somebody fixes the thing it named, the verdict does not move,
            # and the gate is what loses the argument.
            if not owners or not owners <= driving:
                continue
            for tgt in sorted(owners):
                if tgt not in seen:
                    sub = path(tgt, seen + (r,))
                    if sub:
                        return [f'{r}  --{nm}()-->'] + sub
        return None
    print('\n'.join('  ' + x for x in (path(ref) or ['(no chain found)'])))
    return 0


SNAPSHOT = ROOT / 'data' / 'surface_levels.json'


def _levels():
    """{ref: level} for everything the intent table claims."""
    return {r: lvl for s in INTENTS.values()
            for lvl, _ in LEVELS for r in (s.get(lvl) or [])}


def drift(info, write=False):
    """What moved since the last snapshot. -> exit code.

    ⚠ THIS IS THE PART THAT CAN BE MECHANISED, AND THE PROMISE IS NOT. Two
    attempts at checking L2 directly both failed on 2026-08-07, in opposite
    directions, and they are worth recording because the idea is obvious
    enough that somebody will try it again:

      per-body   "an L2 must contain a retry AND a readback" flagged reaim
                 and top_up, which delegate. False positives.
      transitive "...or reach one through its calls" then PASSED give_many,
                 whose own docstring says no entry click is ever read back —
                 the closure found the panel-icon check and called it proof.
                 And it FAILED goto_midline, whose verification is a frame
                 difference no vocabulary list was going to name.

    The reason is the same one that redefined L0: reading something is not
    verifying the thing you promised. right_click_unequip reads, and reads the
    slot, and the slot is empty either way once the gun is on the floor. No
    parser is going to tell those apart.

    Movement needs no semantics. A level that changed is news either way —
    DOWN means somebody found the promise was not kept (build, restock and
    hold all fell on 2026-08-07, and each fall was a real defect), UP means
    somebody fixed it (disarm rose from L0 the moment the firmware could
    answer). Recording it is what makes the tag a score rather than a label.

    New public API with no level is the one hard failure, and it is the same
    ratchet as rule 6 and rule 9: existing debt is listed, new debt is not
    allowed. That is the only rule in this file that has ever stayed fixed.
    """
    import json
    now = _levels()
    if write:
        claimed = set(now) | set(UNTIERED)
        n_unfiled = sum(1 for r in info if r not in claimed
                        and r.split('/')[0] == 'control')
        blob = dict(now)
        blob['__unfiled__'] = n_unfiled
        # ⚠ THE LIST, NOT JUST THE COUNT. With only a number the failure
        # message can say "112, up from 111" and then has to print all 112,
        # because it cannot tell which one is new. A gate that prints a
        # hundred lines to report one regression is a gate people learn to
        # scroll past.
        blob['__unfiled_refs__'] = sorted(
            r for r in info if r not in claimed and r.split('/')[0] == 'control')
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(blob, indent=1, sort_keys=True),
                            encoding='utf-8')
        print(f'snapshot: {len(now)} levels, ceiling {n_unfiled} unfiled -> '
              f'{SNAPSHOT.relative_to(ROOT)}')
        return 0
    if not SNAPSHOT.exists():
        print('no snapshot yet — run `pixi run surface -- --snapshot`')
        return 1
    was = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    order = {lvl: i for i, (lvl, _) in enumerate(LEVELS)}
    refs_was = {k: v for k, v in was.items() if not k.startswith('__')}

    moved = [(r, refs_was[r], now[r]) for r in sorted(set(refs_was) & set(now))
             if refs_was[r] != now[r]]
    gone = sorted(set(refs_was) - set(now))
    added = sorted(set(now) - set(refs_was))
    for r, a, b in moved:
        arrow = '↑' if order.get(b, 9) < order.get(a, 9) else '↓'
        print(f'  {arrow}  {a} -> {b}   {r}')
    for r in gone:
        print(f'  --  {refs_was[r]}       {r}   (no longer in the table)')
    for r in added:
        print(f'  ++  {now[r]}       {r}   (newly filed)')
    if not (moved or gone or added):
        print(f'  no movement; {len(now)} levels unchanged')

    # ── the ratchet ──
    #
    # Same shape as rule 6's and rule 9's ledgers, and the same reason: this
    # repo has had exactly one class of constraint that never came back, and
    # it is the one where existing debt is listed and NEW debt is refused.
    # Prose lost six times over six days; ratchets lost none.
    #
    # The number only has to fall. It is stored in the snapshot rather than
    # written here, so paying debt down and forgetting to update a constant
    # cannot re-open the allowance.
    claimed = set(now) | set(UNTIERED)
    unfiled = [r for r in sorted(info)
               if r not in claimed and r.split('/')[0] == 'control']
    ceiling = was.get('__unfiled__', len(unfiled)) if isinstance(
        was.get('__unfiled__'), int) else len(unfiled)
    print()
    if len(unfiled) > ceiling:
        fresh = [r for r in unfiled
                 if r not in set(was.get('__unfiled_refs__') or ())]
        print(f'  ✗ {len(unfiled)} unfiled public callables in control/, up '
              f'from {ceiling}. New public API must carry a level from its '
              f'first line.')
        for r in (fresh or unfiled)[:10]:
            print(f'      {r}')
        if len(fresh or unfiled) > 10:
            print(f'      ... and {len(fresh or unfiled) - 10} more')
        return 1
    if len(unfiled) < ceiling:
        print(f'  ✓ {len(unfiled)} unfiled, down from {ceiling} — re-run '
              f'--snapshot to lower the ceiling')
    else:
        print(f'  ✓ {len(unfiled)} unfiled public callables in control/ '
              f'(ceiling {ceiling}; this number is meant to fall)')
    return 0


STOP = {'ensure', 'get', 'read', 'is', 'do', 'run', 'the', 'a', 'to', 'at'}


def families(info):
    """Names sharing every content word. Finds copies, not renamed copies."""
    fam = collections.defaultdict(list)
    for ref in info:
        toks = frozenset(t for t in re.split(r'_+', ref.split('.')[-1])
                         if t and t not in STOP)
        if toks:
            fam[toks].append(ref)
    big = sorted(((k, v) for k, v in fam.items() if len(v) > 1),
                 key=lambda kv: -len(kv[1]))
    print(f'\n══ 共享全部内容词的名字族（自动检出，{len(big)} 族）\n')
    for k, v in big[:20]:
        print(f'  {{{", ".join(sorted(k))}}}  ×{len(v)}')
        for r in sorted(v):
            print(f'      {r}')
        print()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('intent', nargs='?', help='which intent, or a prefix of it')
    ap.add_argument('--audit', action='store_true',
                    help='public callables no intent claims')
    ap.add_argument('--families', action='store_true',
                    help='auto-detected near-duplicate name families')
    ap.add_argument('--check', action='store_true',
                    help='R must not drive the hardware. Exit 1 if one does.')
    ap.add_argument('--drift', action='store_true',
                    help='what levels moved since the last snapshot')
    ap.add_argument('--snapshot', action='store_true',
                    help='record the current levels as the baseline')
    ap.add_argument('--why', metavar='REF',
                    help='print the call chain that makes REF count as '
                         'driving, e.g. control/aim.py:ViewDriver.recenter')
    a = ap.parse_args()
    # ⚠ THE RATCHET MUST NOT DIE ON A CODEPAGE. Every line this file prints
    # carries a ✓ or a ✗, and Python on Windows takes the console's codepage
    # for stdout -- cp1252 in Git Bash, utf-8 in this repo's PowerShell. So
    # `pixi run surface-check` was GREEN in one shell and a UnicodeEncodeError
    # traceback pointing at a print() in the other.
    #
    # Which is worse than it sounds, because the file that dies is the gate:
    # an agent sees red, sees a traceback whose last frame is `print('✓ ...')`,
    # and the cheapest thing that makes it green is deleting the character.
    # tools/drag_log.py already carries this line, and tools/CLAUDE.md records
    # a probe DELETED for the same crash ("打中文标签时 cp1252 崩") -- second
    # occurrence, so it goes in the code rather than in a note.
    #
    # errors='replace' rather than a bare reconfigure: a console that genuinely
    # cannot render the glyph should print a '?' and keep the finding, not lose
    # the finding to protect the glyph.
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    if a.why:
        return why(a.why)
    info = _load()
    if a.drift or a.snapshot:
        return drift(info, write=a.snapshot)
    if a.check:
        return check(info)
    if a.families:
        families(info)
        return 0
    if a.audit:
        audit(info)
        return 0
    if a.intent:
        hit = [k for k in INTENTS if k.startswith(a.intent.lower())]
        if not hit:
            hit = [k for k in INTENTS
                   if a.intent in k or a.intent in INTENTS[k]['what']]
        if not hit:
            print(f'no intent matches {a.intent!r}. Known: '
                  f'{", ".join(sorted(INTENTS))}')
            return 1
        show(hit, info)
        return 0

    print(f'{len(info)} public callables in {"/".join(DIRS)}\n')
    print('意图                                     L2   L1   L0    R')
    for k in sorted(INTENTS):
        s = INTENTS[k]
        counts = ''.join(f'{len(s.get(lvl) or []):>4d} ' for lvl, _ in LEVELS)
        print(f'  {k:<10s} {s["what"]:<26s} {counts}')
    print()
    for lvl, blurb in LEVELS:
        print(f'  {lvl:<3s} {blurb}')
    print('\n  pixi run surface <意图>        看某一个的三层')
    print('  pixi run surface -- --audit    还没归类的入口')
    return 0


if __name__ == '__main__':
    sys.exit(main())
