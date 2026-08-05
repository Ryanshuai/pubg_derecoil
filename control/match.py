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
import threading
from collections import deque

from config import KEY_ACTION_TABLE, DETECT_TABLE, MISMATCH_TABLE
from calibration.mismatch import MismatchCollector
from control.tab_watch import TabWatch
from detector.weapon import Weapon
from press.pico_mouse import get_mouse


class Dispatcher:
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

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def join(self):
        if self._thread:
            self._thread.join()

    # ── Main loop ──

    def _loop(self):
        while self._running:
            try:
                # 1. Process key events
                for ev in self.poller.pop_events():
                    self._handle_key(ev)

                # 2. Process pending detections (target_ts reached)
                self._process_pending()

                # 3. Tab screen: one grab at most, and only when there is a
                #    reason. Keeps state.tab_open measured rather than guessed,
                #    and keeps the loadout fresh while the panel is up so that
                #    closing it needs no race and no buffered past frame.
                self.tab.tick()

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
        """Apply state changes from KEY_ACTION_TABLE entry."""
        s = self.state
        for item in state_list:
            if isinstance(item, tuple) and len(item) == 2:
                attr, val = item
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
                    if len(w.dy_s) == 0 or self.state.stop_recoil:
                        m.clear_pattern()
                    else:
                        m.upload_pattern(w.dx_s, w.dy_s, w.t_s, w.bullet_interval_s)
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
        (tools/probe_posture_trace.py, docs/posture/traces/20260805_094215):
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
