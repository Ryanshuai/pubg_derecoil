"""TabWatch — is the inventory up, and what are the guns wearing.

Everything about the Tab screen that the live loop needs, kept OUT of the
per-frame capture. Its regions sit at the top of the screen and the gameplay
HUD at the bottom, so having both in one DXGI bounding box cost 5.46 ms of
every frame — 87% of the capture budget — for a panel that is usually not on
screen. See config.FRAME_REGIONS.

Two things it replaces, and why each was wrong:

`toggle_tab_open` flipped a cached bool on every Tab keypress and let a
detection 300 ms later correct it. For those 300 ms the flag was a guess, and
a guess gates a dozen `cond: '!tab_open'` entries — including whether recoil
compensation runs. A swallowed keypress (docs/game_quirks.md: one issued right
after a previous toggle simply does not arrive) left it inverted with nothing
to notice. Here the flag only ever changes because the screen was looked at.

`delay: -50` reached backwards into the ring buffer to catch the panel as it
was 50 ms before the close keypress. That works, and is more robust than
racing forwards, but it is what forced the Tab regions into every frame in the
first place. Instead the reading is kept fresh WHILE the panel is up, so when
it closes the last one taken is already the final state — no race, no buffer.

Nothing here blocks: tick() does at most one grab (~5-10 ms) and returns.
"""
import time

from config import (HUD_REGIONS, TAB_DRIFT_S, TAB_REFRESH_S, TAB_SETTLE_S)

ATT_REGIONS = [k for k in HUD_REGIONS if k.startswith('att_')]
NAME_REGIONS = ['gun_name_1', 'gun_name_2']


class TabWatch:
    """Measured Tab state plus the last loadout seen while it was up.

    detectors is the Dispatcher's registry, shared by reference so anything
    registered later is visible here too. Needs 'tab_type', 'tab_weapon' and
    'tab_attachment'; missing ones just disable their part.
    """

    def __init__(self, state, detectors, verbose=True):
        self.state = state
        self._detectors = detectors
        self.verbose = verbose
        self.open = False
        self.loadout = None          # {'weapons':..., 'attachments':..., 'ts':}
        self._type_grab = None
        self._panel_grab = None
        self._watch_until = 0.0      # a key was seen; watch for the change
        self._want = None            # what we expect it to become
        self._next_refresh = 0.0
        self._next_drift = 0.0

    # ── Capture, built lazily so a state-only caller costs nothing ──

    def _type_crop(self):
        if self._type_grab is None:
            from detector.cropper import RegionGrabber
            self._type_grab = RegionGrabber({'type': HUD_REGIONS['type']})
        return self._type_grab.grab()

    def _panel_frame(self):
        if self._panel_grab is None:
            from detector.tab_items import TabGrabber
            # Only the two weapon panels: both name plates and all ten slots,
            # 9.6 ms, against 18.7 for the whole Tab screen.
            self._panel_grab = TabGrabber(only=('right',))
        return self._panel_grab.grab()

    def close(self):
        for g in (self._type_grab, self._panel_grab):
            if g is not None:
                try:
                    g.close()
                except Exception:
                    pass
        self._type_grab = self._panel_grab = None

    def _log(self, msg):
        if self.verbose:
            print(f'[tab] {msg}', flush=True)

    # ── Reads ──

    def measure_open(self):
        """Look at the anchor. -> bool, or None if it could not be read."""
        det = self._detectors.get('tab_type')
        if det is None:
            return None
        try:
            return bool(det.classify(self._type_crop()))
        except Exception as e:
            self._log(f'open-check failed: {e}')
            return None

    def read_loadout(self):
        """Grab the weapon panel and read both guns off it. -> dict or None."""
        weap = self._detectors.get('tab_weapon')
        att = self._detectors.get('tab_attachment')
        if weap is None and att is None:
            return None
        try:
            frame = self._panel_frame()
        except Exception as e:
            self._log(f'panel grab failed: {e}')
            return None
        out = {'ts': time.perf_counter(), 'weapons': None, 'attachments': None}
        try:
            if weap is not None:
                out['weapons'] = weap.classify(
                    {k: _crop(frame, k) for k in NAME_REGIONS})
            if att is not None:
                out['attachments'] = att.classify(
                    {k: _crop(frame, k) for k in ATT_REGIONS})
        except Exception as e:
            self._log(f'panel read failed: {e}')
            return None
        return out

    # ── Driving ──

    def on_key(self, now=None):
        """A Tab keypress was seen. Watch for the screen to actually change.

        `now` should be the KeyEvent's own timestamp: the event may have sat
        in the poller's queue for a tick or two, and the settle window should
        be measured from when the key was pressed, not from when this got
        around to hearing about it. It also has to be the same clock tick()
        is driven on, or the window is nonsense.

        The keypress is NOT the state change. It is a request that the game
        may honour in 28-38 ms (opening), 77-128 ms (closing), or not at all
        if it was swallowed. So this only arms a watch; `open` moves when the
        screen does.

        If the panel is up, this also takes one last reading straight away --
        the panel stays fully drawn for another 13-28 ms, so this usually
        lands, and when it does not the refresh loop's reading from <=100 ms
        ago is still there.
        """
        if self.open:
            got = self.read_loadout()
            if got is not None:
                self._publish(got)
        now = time.perf_counter() if now is None else now
        self._watch_until = now + TAB_SETTLE_S
        self._want = not self.open

    def tick(self, now=None):
        """One unit of work, at most one grab. Call from the dispatcher loop."""
        now = time.perf_counter() if now is None else now

        if now < self._watch_until:
            got = self.measure_open()
            if got is not None and got != self.open:
                self._set_open(got, now)
                self._watch_until = 0.0
            elif now + 0.011 >= self._watch_until and self._want is not None:
                # Last look before giving up. The screen never changed, so the
                # keypress did not arrive -- leave `open` alone. Guessing here
                # is exactly what toggle_tab_open did.
                self._log(f'Tab key did not take effect (still '
                          f'{"open" if self.open else "closed"})')
                self._want = None
            return

        if self.open and now >= self._next_refresh:
            self._next_refresh = now + TAB_REFRESH_S
            got = self.read_loadout()
            if got is not None:
                self._publish(got)
            return

        if now >= self._next_drift:
            self._next_drift = now + TAB_DRIFT_S
            got = self.measure_open()
            if got is not None and got != self.open:
                self._log(f'drifted: screen says {"open" if got else "closed"}')
                self._set_open(got, now)

    # ── Internals ──

    def _set_open(self, value, now):
        self.open = value
        self.state.tab_open = value
        if value:
            self._next_refresh = 0.0     # read it immediately
        self._log(f'{"open" if value else "closed"}')

    def _publish(self, got):
        """Write a reading through to GameState, as the detections used to."""
        self.loadout = got
        if got['weapons'] is not None:
            self.state.weapon_gt = got['weapons']
            self.state.sync_weapons()
        if got['attachments'] is not None:
            for gun_id, a in got['attachments'].items():
                self.state.set_attachments(gun_id, a)


def _crop(frame, key):
    y, x, h, w = HUD_REGIONS[key]
    return frame[y:y + h, x:x + w]
