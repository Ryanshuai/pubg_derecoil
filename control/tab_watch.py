"""TabWatch — is the inventory up, and what are the guns wearing.

Everything about the Tab screen that the live loop needs, kept OUT of the
per-frame capture. Its regions sit at the top of the screen and the gameplay
HUD at the bottom, so having both in one DXGI bounding box cost 5.46 ms of
every frame — 87% of the capture budget — for a panel that is usually not on
screen. See config.FRAME_REGIONS.

⚠ ONE READ PER TAB SESSION, TAKEN THE MOMENT THE ANCHOR SAYS CLOSED. NOTHING
IS READ WHILE THE PANEL IS UP, AND NOTHING IS REMEMBERED BETWEEN TICKS.

    panel opens        nothing
    panel is up        nothing
    anchor reads shut  ONE grab, ONE classify, publish

The whole file is that sentence. Two things make it work and both are
measurements rather than hopes:

THE ANCHOR GOES FIRST. `open` is decided by the 41x18 「类型」 header, and that
text stops being legible EARLY in the close — while the weapon panel, its name
plates and its slot tiles are all still drawn. So the instant this says shut is
an instant at which the panel can still be read. There is nothing to race and
nothing to remember.

THE WATCH IS ALREADY PER-TICK WHERE IT MATTERS. A Tab keypress arms
TAB_SETTLE_S of tick-rate anchor checks, and the game honours a close in
77-128 ms — comfortably inside it. So the transition is caught within one
10 ms tick of it happening, for free, on the path every real close takes.

⚠ AND THE COST IS WHY IT CANNOT SIMPLY POLL. A GDI grab is ~5 ms almost
regardless of size (41x18 measures 5.2 ms), so at the 10 ms dispatcher tick a
single unconditional anchor check would be 52% of a core. That is the number
that shapes this file: checks are event-driven, with a slow drift check to
catch what no key announced.

⚠ WHAT IS GIVEN UP, PLAINLY: a close that NO KEY announced — alt-tab, a
disconnect dialog, another agent — is found by the drift check up to
TAB_DRIFT_S later, and by then the panel really is gone. The grab comes back
with no tiles painted, `any_drawn` catches it, and the kit is not published.
That is a missed reading, not a wrong one.

⚠ NOTHING MAY BE ADDED THAT RUNS WHILE THE PANEL IS UP. Not a periodic
re-read, not a cached last-good reading, not a buffer of past frames, not a
score over kept frames. Every such scheme has to answer "which moment does
this describe", and while the panel is up the answer is "one the player has
already changed" — a gun caught mid-swap, with its muzzle in your hand,
becomes the gun the compensation is built for. This has been built and removed
more than once, so the versions are deliberately not described here or
anywhere else: written down they read as prior art, and prior art comes back.
`pixi run tab-watch` fails if anything grabs the panel before it closes.

`toggle_tab_open` — the one thing this replaces that is worth remembering —
flipped a cached bool on every Tab keypress and let a detection 300 ms later
correct it. For those 300 ms the flag was a guess, and a guess gates a dozen
`cond: '!tab_open'` entries, including whether recoil compensation runs. A
swallowed keypress (docs/game_quirks.md: one issued right after a previous
toggle simply does not arrive) left it inverted with nothing to notice. Here
the flag only ever changes because the screen was looked at.

Nothing here blocks: tick() does at most one 5 ms anchor check, except on the
tick where the panel closes, which also grabs and classifies.
"""
import time

from config import HUD_REGIONS, TAB_DRIFT_S, TAB_SETTLE_S

ATT_REGIONS = [k for k in HUD_REGIONS if k.startswith('att_')]
NAME_REGIONS = ['gun_name_1', 'gun_name_2']


class TabWatch:
    """Measured Tab state plus the loadout read as the panel closed.

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
        self._next_drift = 0.0

    # ── Capture, built lazily so a state-only caller costs nothing ──

    def _type_crop(self):
        if self._type_grab is None:
            from capture.cropper import RegionGrabber
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
        """Grab the weapon panel and read both guns off it. -> dict or None.

        ⚠ CALLED ONCE PER TAB SESSION, ON THE CLOSE, AND THE TIMING IS THE
        DESIGN. See the module docstring: the anchor stops being legible before
        the panel stops being drawn, so this runs at a moment when there is
        still a panel to read.

        ⚠ NAMES AND ATTACHMENTS COME OFF ONE FRAME, which is the root
        CLAUDE.md's second law rather than a convenience: the names are what
        narrow each slot's template bank, so reading them from a different
        moment than the tiles means the record describes a gun that was never
        measured. It is how a UZI came to be wearing a sniper cheek pad.
        """
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
                # ⚠ AN UNPAINTED PANEL IS NOT A BARE GUN, AND IT IS ALSO THE
                # EVIDENCE FOR THE PREMISE ABOVE. classify() reports an
                # unpainted tile as '', the same '' an empty slot gives, and
                # _publish writes those onto both weapons: a fully kitted gun
                # becomes `bare` and the compensation is cleared.
                #
                # Both outcomes print, because a log where this always says
                # "still painted" is what says the anchor really does go first,
                # and a log where it always says "already gone" says it does
                # not and this file is built on a false premise. A guard that
                # only ever refuses silently cannot tell those apart.
                #
                # A tile that is merely EMPTY is still DRAWN (detector/
                # CLAUDE.md: border-ring Sobel p90 46-173 empty against 5-26 for
                # no tile at all), so this does not refuse a bare gun. It
                # refuses a panel with no tiles anywhere.
                if not att.any_drawn(frame):
                    self._log('the panel was already gone when the anchor '
                              'said so — no tiles painted, so the kit is NOT '
                              'published (an unpainted tile is not an empty '
                              'slot). Expected after an alt-tab or a dialog, '
                              'where no key announced the close.')
                else:
                    named = {i + 1: nm
                             for i, nm in enumerate(out['weapons'] or ()) if nm}
                    out['attachments'] = att.classify(frame, named)
                    self._log('read at the close, panel still painted')
        except Exception as e:
            self._log(f'panel read failed: {e}')
            return None
        return out

    # ── Driving ──

    def on_key(self, now=None):
        """A Tab keypress was seen. Watch for the screen to actually change.

        `now` should be the KeyEvent's own timestamp: the event may have sat in
        the poller's queue for a tick or two, and the settle window should be
        measured from when the key was pressed, not from when this got around
        to hearing about it. It also has to be the same clock tick() is driven
        on, or the window is nonsense.

        The keypress is NOT the state change. It is a request that the game may
        honour in 28-38 ms (opening), 77-128 ms (closing), or not at all if it
        was swallowed. So this only arms a watch; `open` moves when the screen
        does.

        ⚠ IT READS NOTHING, IN EITHER DIRECTION, and must not be made to. A
        grab here is timed off the KEY rather than off the SCREEN, and the key
        can arrive a tick or two late — by which point it is a picture of a
        fading panel. The watch it arms is what catches the close, within one
        tick of the screen actually changing.
        """
        now = time.perf_counter() if now is None else now
        self._watch_until = now + TAB_SETTLE_S
        self._want = not self.open

    def tick(self, now=None):
        """One unit of work, at most one anchor check. From the dispatch loop."""
        now = time.perf_counter() if now is None else now

        if self._want is not None and now >= self._watch_until:
            # The window closed. Either the screen moved (and `open` already
            # followed it) or the keypress never arrived -- leave `open` alone
            # and say so. Guessing here is exactly what toggle_tab_open did.
            if self.open != self._want:
                self._log(f'Tab key did not take effect (still '
                          f'{"open" if self.open else "closed"})')
            self._want = None

        watching = now < self._watch_until
        drifting = now >= self._next_drift
        if not watching and not drifting:
            return
        if drifting:
            self._next_drift = now + TAB_DRIFT_S
        got = self.measure_open()
        if got is not None and got != self.open:
            if not watching:
                self._log(f'drifted: screen says '
                          f'{"open" if got else "closed"}')
            self._set_open(got)
            self._watch_until = 0.0

    # ── Internals ──

    def _set_open(self, value):
        # ⚠ THE FLAG MOVES FIRST, THEN THE PANEL IS READ. The screen has said
        # what it says; that is a measurement and it gets recorded even if the
        # classify below throws or comes back empty. Reading first and flipping
        # after would let one bad frame hold `tab_open` True, and `tab_open`
        # gates whether compensation runs at all.
        self.open = value
        self.state.tab_open = value
        self._log('open' if value else 'closed')
        if value:
            return
        # The one place the panel is read. Publishing before returning matters:
        # control/match.py's _follow_tab re-arms the firmware on this same tick,
        # right after this, and it must upload the curve for what the gun is
        # wearing now rather than what it wore before you opened the panel.
        got = self.read_loadout()
        if got is not None:
            self._publish(got)

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
