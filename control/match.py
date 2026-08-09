"""Dispatcher — reads config tables, matches key events to frames, runs detectors.

Two responsibilities:
1. Key actions:  KEY_ACTION_TABLE → state changes + hardware
2. Detections:   DETECT_TABLE → find frame → run detector → write result

MISMATCH_TABLE is still scheduled here, but the comparing and the saving is
calibration/mismatch.py's job — this loop has a firing window to hit and has
no business writing PNGs.

The Tab screen is NOT one of the tables. It is control/tab_watch.py, because
its regions are not in the per-frame capture and its state has to come from
looking rather than from having seen the key.
"""
import time

from daemon_loop import DaemonLoop
from collections import deque

from config import KEY_ACTION_TABLE, DETECT_TABLE, MISMATCH_TABLE
from calibration.mismatch import MismatchCollector
from control.tab_watch import TabWatch
from detector.weapon import Weapon
from press.pico_mouse import get_mouse


class Dispatcher(DaemonLoop):
    """Main logic loop. Consumes key events, drives detections."""


    def __init__(self, state, capture, poller):
        self.state = state
        self.capture = capture
        self.poller = poller
        self._detectors = {}  # name -> detector instance, set by register()
        self._pending = deque()  # (target_ts, detect_entry)
        self._running = False
        self._thread = None
        self._shut_done = False
        # Both share the detector registry by reference: register() mutates
        # the dict in place, so detectors added later are visible to them too.
        self.mismatch = MismatchCollector(state, capture, self._detectors)
        # The Tab screen is not in the per-frame capture, so it reads its own
        # regions on demand. It owns state.tab_open and the guns' loadout.
        self.tab = TabWatch(state, self._detectors)

    def register(self, name, detector):
        """Register a detector instance by name."""
        self._detectors[name] = detector

    # ── Thread lifecycle ──


    # ── Main loop ──

    def _loop(self):
        while self._running:
            try:
                # 1. Process key events
                for ev in self.poller.pop_events():
                    self._handle_key(ev)

                # 2. Process pending detections (target_ts reached)
                self._process_pending()

                # 3. Tab screen: one anchor check at most, and only when there
                #    is a reason. Keeps state.tab_open measured rather than
                #    guessed. The guns are read once, on the close.
                self.tab.tick()
                self._follow_tab()

                # 4. Periodic mismatch collection — DISABLED for debugging
                # self.mismatch.poll(time.perf_counter())

            except Exception as e:
                print(f"[dispatch] {e}", flush=True)

            time.sleep(0.01)

    # ════════════════════════════════════════════════════════════
    # Key event handling
    # ════════════════════════════════════════════════════════════

    def _handle_key(self, ev):
        """Process a single KeyEvent: actions + schedule detections."""
        # Name the cause BEFORE anything acts on it, so every [armed] line
        # printed downstream of this key carries the key. See _said_pattern.
        self._cause = f'key {ev.key!r} {ev.event}'
        # Shutdown
        if ev.key == 'f13' and ev.event == 'press':
            self.shutdown()
            self._running = False
            return

        # Tab: take the final reading NOW, while the panel is still drawn, and
        # start watching for the screen to actually change. This is what the
        # 'delay': -50 entries used to do by reaching back into the ring
        # buffer -- which is what forced the Tab regions into every captured
        # frame. Deliberately before the tables: it does not touch tab_open,
        # so every cond below still sees the screen as it is right now.
        if ev.key == 'tab' and ev.event == 'press':
            self.tab.on_key(ev.ts)

        # DETECT_TABLE: schedule detections using CURRENT state
        for entry in DETECT_TABLE:
            if not self._key_matches(entry, ev):
                continue
            if not self._cond_met(entry.get('cond')):
                continue
            target_ts = ev.ts + entry['delay'] / 1000.0
            self._pending.append((target_ts, entry))

        # MISMATCH_TABLE: schedule mismatch collection (snapshot GT now)
        for entry in MISMATCH_TABLE:
            if not self._key_matches(entry, ev):
                continue
            if not self._cond_met(entry.get('cond')):
                continue
            # Use explicit gt_value if provided, otherwise snapshot from state
            if 'gt_value' in entry:
                gt_snapshot = entry['gt_value']
            else:
                gt_field = entry.get('gt_field', '')
                gt_snapshot = getattr(self.state, gt_field, None)
            target_ts = ev.ts + entry['delay'] / 1000.0
            self._pending.append((target_ts, {
                '_mismatch': True, '_gt_snapshot': gt_snapshot, **entry
            }))

        # KEY_ACTION_TABLE: state changes + hardware (AFTER scheduling above)
        #
        # The ordering is still load-bearing, but no longer for tab_open. The
        # conds above read `tab_open` and `stop_recoil`; tab_open is now owned
        # by TabWatch and never written here, but `stop_recoil` IS written
        # below and two DETECT entries cond on it — so scheduling first is
        # what makes them see the state as it was when the key was pressed.
        for entry in KEY_ACTION_TABLE:
            if not self._key_matches(entry, ev):
                continue
            if not self._cond_met(entry.get('cond')):
                continue
            self._apply_state(entry.get('state', []))
            self._apply_hw(entry.get('hw', []))

    def _key_matches(self, entry, ev):
        """Check if a table entry matches this key event."""
        entry_key = entry['key']
        entry_event = entry.get('event', 'press')
        if entry_event != ev.event:
            return False
        # Combo key: ('alt', 'tab')
        if isinstance(entry_key, tuple):
            trigger = entry_key[-1]
            mods = set(entry_key[:-1])
            return ev.key == trigger and mods <= ev.held_keys
        return ev.key == entry_key

    def _cond_met(self, cond):
        """Evaluate condition string against state."""
        if not cond:
            return True
        s = self.state
        for part in cond.split('&&'):
            part = part.strip()
            negate = part.startswith('!')
            attr = part.lstrip('!')
            val = getattr(s, attr, False)
            if negate:
                if val:
                    return False
            else:
                if not val:
                    return False
        return True

    # ════════════════════════════════════════════════════════════
    # State + Hardware
    # ════════════════════════════════════════════════════════════

    def _apply_state(self, state_list):
        """Apply state changes from KEY_ACTION_TABLE entry.

        ⚠ `stop_recoil` IS ANNOUNCED, because it is the one flag that turns
        the whole tool off and nothing said when it moved. Reading the play
        log meant reconstructing it from KEY_ACTION_TABLE by hand -- and what
        that reconstruction found was that `tab` sets it True with no `cond`,
        so the same key disarms on the way IN and again on the way OUT.
        """
        s = self.state
        for item in state_list:
            if isinstance(item, tuple) and len(item) == 2:
                attr, val = item
                if attr == 'stop_recoil' and bool(val) != bool(s.stop_recoil):
                    print(f'[state] stop_recoil {bool(s.stop_recoil)} -> '
                          f'{bool(val)}   (after {self._cause})', flush=True)
                # Method call: ('set_active_by_key', 1)
                method = getattr(s, attr, None)
                if callable(method):
                    method(val)
                else:
                    setattr(s, attr, val)
            elif isinstance(item, tuple) and len(item) == 1:
                # Toggle: ('toggle_aim',)
                method = getattr(s, item[0], None)
                if callable(method):
                    method()

    _pattern_said = None

    # ── 固件里此刻装的那条曲线，读回来的 ──
    #
    # ⚠ 读回，不是上传值。`collect_timed.py:850` 那行原来是 `curve = rig.arm(w)`
    # 而现在是 `rig.mouse.read_pattern()`，因为两者不是同一个东西：
    # `upload_pattern` 的折叠把每一个负偏移都塌成 `curve[0]['t_ms'] == 0`。
    # 没有它，`y_true = y_obs + C(t)` 里的 C 就是一个从没被证实过的假设。
    #
    # ⚠ 惰性，而且**故意不在 upload 时读**。upload 每秒会触发多次（Tab 读稳定
    # 期间每一个配件都触发一次），而 read_pattern 是 40 行的串口往返。这里只翻
    # 一个脏标志；真正的读发生在观测器空闲的时候，也就是没人在开火的时候。
    _curve_dirty = True
    _armed_curve = ()

    def armed_curve(self, refresh=True):
        """固件里那条曲线。`refresh=False` 只取缓存，不碰串口。

        ⚠ **开火期间必须传 `refresh=False`。** 一次串口往返在观测循环里就是几帧,
        而那几帧正好是第一发的踢腿 —— 曲线曾因此给自己的第一发写了 -0.6 counts。
        """
        if refresh and self._curve_dirty:
            try:
                self._armed_curve = tuple(get_mouse().read_pattern() or ())
                self._curve_dirty = False
            except Exception as e:
                # 读不到就说读不到。空的 curve 会让这一梭以 comp_enabled=False
                # 入库，也就是「没压枪」—— 而它其实压了。那是个安静的错记录。
                print(f'[armed] could not read the pattern back: {e!r}',
                      flush=True)
                self._armed_curve = ()
        return list(self._armed_curve)

    # What most recently drove the loop, so a change can name its own cause.
    _cause = 'startup'

    _tab_was = False

    def _follow_tab(self):
        """Re-arm when the panel is SEEN to close, not when a key was pressed.

        ⚠ `stop_recoil` IS SET BY THE TAB KEY AND CLEARED BY NOTHING THAT
        FOLLOWS IT. The table entry has no `cond`, so the same key sets it True
        on the way in AND on the way out; the only things that clear it are
        `1`, `2`, and the RELEASE of shift or right-click. So after closing the
        inventory the tool is disarmed until the player happens to switch
        weapons or scope in -- and hip-firing straight out of the panel gets no
        compensation at all, with nothing anywhere saying so.

        That is the same shape as the F key wiping the kit: a KEYPRESS standing
        in for a state nobody looked at. TabWatch now measures the panel, so
        the close is an observation and this hangs off it.

        ⚠ IT ONLY EVER CLEARS ON A CLOSE, never sets on an open. The key entry
        already disarms on the way in, and re-deriving that here would be a
        second author of it -- while missing the clear costs a whole fight.
        """
        now = bool(self.state.tab_open)
        if self._tab_was and not now and self.state.stop_recoil:
            self.state.stop_recoil = False
            self._cause = 'tab seen to close'
            self._apply_hw(['recoil_on', 'upload_pattern'])
        self._tab_was = now

    def _said_pattern(self, msg):
        """Print what is armed, once per distinct answer, WITH ITS CAUSE.

        upload_pattern fires on every weapon, attachment, posture and fire-mode
        change, which is many times a second while a Tab read settles. Printing
        each one would bury the log it exists to make readable; printing only
        changes makes the log a list of what actually changed.

        ⚠ THE CAUSE IS THE HALF THAT WAS MISSING, and its absence cost a whole
        play session. The 2026-08-09 log recorded `[armed] CLEARED — no curve
        for m416` and nothing anywhere said WHY the key had changed. Finding
        out meant reading config.KEY_ACTION_TABLE by hand and discovering that
        the F key wiped every attachment. A log that states an effect and not
        its cause sends the reader to the source, which is exactly the trip the
        log exists to save.
        """
        if msg != self._pattern_said:
            self._pattern_said = msg
            print(f'[armed] {msg}   (after {self._cause})', flush=True)

    def _apply_hw(self, hw_list):
        """Apply hardware actions."""
        for action in hw_list:
            try:
                m = get_mouse()
                if action == 'recoil_off':
                    m.set_recoil_enabled(False)
                elif action == 'recoil_on':
                    m.set_recoil_enabled(True)
                elif action == 'upload_pattern':
                    w = self.state.active
                    # 装进去的东西变了，缓存里那条就描述另一个对象了。翻标志，
                    # 不读 —— 读发生在观测器空闲时。
                    self._curve_dirty = True
                    if len(w.dy_s) == 0 or self.state.stop_recoil:
                        m.clear_pattern()
                        # 清空是确定的，不用再去读一次问它清没清。
                        self._armed_curve = ()
                        self._curve_dirty = False
                        self._said_pattern('CLEARED — ' + (
                            'stop_recoil is set (Tab, spawner panel, a menu)'
                            if self.state.stop_recoil else
                            f'no curve for {w.name or "(empty hands)"}'))
                    else:
                        m.upload_pattern(w.dx_s, w.dy_s, w.t_s)
                        # ⚠ THE SUCCESS LINE IS THE POINT, NOT THE FAILURE
                        # ONE. set_seq already says when it cannot find a
                        # curve; nothing said when it COULD, so a log showing
                        # no complaint was indistinguishable from a log of a
                        # session that never reached here -- and "it is not
                        # holding the gun down" is asked against exactly that
                        # ambiguity. Deduped on the content, so it costs one
                        # line per distinct thing actually armed.
                        self._said_pattern(
                            f'{w.name} {w.posture} '
                            f'muzzle={w.muzzle or "-"} grip={w.grip or "-"} '
                            f'stock={w.butt or "-"} scope={w.scope or "-"} '
                            f'fire={w.fire_mode or "-"} -> {len(w.dy_s)} knots'
                            f', {sum(w.dy_s):.0f} counts over '
                            f'{(w.t_s[-1] if w.t_s else 0):.2f}s')
                elif action == 'shutdown':
                    self.shutdown()
                    self._running = False
            except Exception as e:
                print(f"[hw] {action}: {e}", flush=True)

    # ════════════════════════════════════════════════════════════
    # Pending detections
    # ════════════════════════════════════════════════════════════

    def _process_pending(self):
        """Check pending detections, run those whose target_ts has arrived."""
        now = time.perf_counter()
        remaining = deque()
        ran = False
        for target_ts, entry in self._pending:
            if now < target_ts:
                # Not ready yet, keep
                if now < target_ts + 1.0:  # drop if >1s stale
                    remaining.append((target_ts, entry))
                continue
            # Ready — find frame and run
            if entry.get('_mismatch'):
                self.mismatch.run_scheduled(target_ts, entry)
            else:
                again = self._run_detect(target_ts, entry)
                # Into `remaining`, NOT self._pending: this loop rebuilds the
                # queue and replaces it below, so anything appended to the old
                # one is dropped on the floor.
                if again is not None:
                    remaining.append(again)
                ran = True
        self._pending = remaining
        if ran:
            self.state.print_status()

    def _retry(self, target_ts, entry):
        """Re-queue an entry whose detector could not read. -> pending|None

        WHY AN UNREADABLE RESULT IS NOT THE SAME AS A FAILURE. Some HUD
        elements are only DRAWN in some states, so `None` frequently means
        "ask again in a moment", not "this cannot be read". The posture icon
        renders only while the sight is up (docs/game_quirks.md), and all three
        of its triggers fire when it usually is not: `c` and `z` are pressed
        before aiming, and 350 ms after a right-button release the sight is
        often already down. A one-shot read there returns None, `set_posture`
        discards it, THE STALE POSTURE SURVIVES, and compensation runs on a
        factor wrong by up to 2x — standing 1.0 against prone 0.50.

        The delays were never the problem, which is worth stating because they
        were the obvious suspect. Measured 2026-08-05 over six backdrops
        (tools/probe_posture_trace.py, calibration/artifacts/posture/traces/20260805_094215):
        with the sight ALREADY UP, the icon follows a posture key in 34..68 ms
        and was readable in 3786 of 3787 samples. With the sight down it was
        readable in none, across a full 2000 ms window, in every round. So the
        200 ms delay clears the real latency three times over; what it cannot
        clear is a frame where the icon is not painted at all.

        Bounded on purpose. If the sight has not come up within `retry_ms *
        retries` of a stance change, the stance does not matter yet — nothing
        is being aimed — and the next right-button event schedules its own
        read. An unbounded retry would instead keep a stale entry alive
        forever and re-read it into state long after the key that caused it.
        """
        every = entry.get('retry_ms')
        left = entry.get('_retries_left', entry.get('retries', 0))
        if not every or left <= 0:
            return None
        return (target_ts + every / 1000.0,
                {**entry, '_retries_left': left - 1})

    def _run_detect(self, target_ts, entry):
        """Find frame at target_ts, run detector, write result to state.

        Returns a (ts, entry) to re-queue when the read came back empty and
        the entry asks for retries, else None. See `_retry`.
        """
        # The condition is checked twice: once when this was scheduled, and
        # again now, because state may have moved in between.
        #
        # There used to be a `delay > 0` guard here, so that entries reading a
        # PAST frame (delay < 0) skipped the second check — their frame
        # predated the state change, so re-checking against current state would
        # have been asking the wrong question. There are no negative delays
        # left; control/tab_watch.py replaced the two that existed.
        if not self._cond_met(entry.get('cond')):
            return None
        regions = entry['regions']
        crops = self.capture.get_crops(target_ts, regions)
        if crops is None:
            return self._retry(target_ts, entry)

        detect_name = entry['detect']
        detector = self._detectors.get(detect_name)
        if detector is None:
            return None
        # A detection is the OTHER thing that moves the curve key, and without
        # this every [armed] line during a Tab settle would be blamed on
        # whatever key was pressed minutes ago.
        self._cause = f'{detect_name} on {entry["key"]!r}'

        result = detector.classify(crops)
        if result is None:
            return self._retry(target_ts, entry)

        # Write result to state — direct attribute assignment
        result_field = entry.get('result')
        if result_field and result is not None:
            setattr(self.state, result_field, result)

            # Side effects after specific result writes
            if result_field in ('weapon_gt', 'weapon_pred'):
                self.state.sync_weapons()
                self._apply_hw(['upload_pattern'])
            elif result_field == 'highlight_pred':
                self.state.set_active_by_detect(result)
                self._apply_hw(['upload_pattern'])
            elif result_field == 'posture':
                self.state.set_posture(result)
                self._apply_hw(['upload_pattern'])
            elif result_field == 'fire_mode':
                self.state.set_fire_mode(result)
                self._apply_hw(['upload_pattern'])
            elif result_field == 'attachments':
                for gun_id, att in result.items():
                    self.mismatch.check_attachment(gun_id, att, crops)
                    self.state.set_attachments(gun_id, att)
                self._apply_hw(['upload_pattern'])
        return None

    # ════════════════════════════════════════════════════════════
    # Shutdown
    # ════════════════════════════════════════════════════════════

    def shutdown(self):
        """Disarm the firmware and persist. Call this, not just stop().

        stop() only clears the flag; the loop keeps running until it notices.
        Reset the hardware before that and the next pass re-arms it, which is
        why robot.py joins the thread first.

        Idempotent because two paths land here: the f13 key and process exit.
        Whichever runs second must not repeat the save or re-report it.

        The disarm is not allowed to fail quietly. One Pico that stays armed
        after this process is gone keeps compensating for a gun nobody is
        holding, and three agents share this one serial port -- the next run
        would measure that instead of the recoil it fired.
        """
        if self._shut_done:
            return
        self._shut_done = True
        try:
            m = get_mouse()
            m.set_recoil_enabled(False)
            m.clear_pattern()
            m.set_aim_mode(False)
            m.set_delta(0, 0)
        except Exception as e:
            print(f"[shutdown] !! FIRMWARE STILL ARMED: {e}", flush=True)
        self.tab.close()          # releases the two on-demand GDI grabbers
        Weapon.save_scales()
        print("[shutdown] scales saved", flush=True)
