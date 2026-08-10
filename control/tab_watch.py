"""TabWatch — is the inventory up, and what are the guns wearing.

Everything about the Tab screen that the live loop needs, kept OUT of the
per-frame capture. Its regions sit at the top of the screen and the gameplay
HUD at the bottom, so having both in one DXGI bounding box cost 5.46 ms of
every frame — 87% of the capture budget — for a panel that is usually not on
screen. See config.FRAME_REGIONS.

⚠ THE TAB KEY GRABS AND SAVES, IMMEDIATELY, BEFORE ANYTHING IS DECIDED. Then,
and only if the panel was up, that same frame is classified.

    Tab press, panel shut   ONE grab, saved `<stamp>_press.png`
    Tab press, panel up     ONE grab, saved, classify, publish
    panel is up             nothing
    panel seen to close     nothing — the reading was taken ~100 ms ago

    panel grab      8-12 ms   <- the only part that must beat the fade
    png write        ~4 ms    <- after the pixels are already captured

Add the input path — poller 5 ms, dispatcher tick 10 ms — and the panel is on
disk-bound pixels within ~25 ms of the physical key, against 77-128 ms before
the game takes it down. 50-100 ms of margin, and it is margin ONLY because the
grab is FIRST. Deciding first is what put a permission in front of it that by
measurement only arrives once the panel is already gone.

⚠ THE PRESS, AND ONLY THE PRESS. `on_key` takes no edge; the filter is one
line in control/match.py. Tab is a TOGGLE, so a session is TWO taps and the
panel falls on the opening tap's release and the CLOSING TAP'S PRESS — which
is the one carrying the final state. Measured off 66 saved tap pairs; the
numbers and what press-only costs (3.3% of sessions, and half the grabs) are
in docs/game_quirks.md.

⚠ NOTHING HERE MAY LEARN WHICH EDGE IT GOT. What decides is `up`: whether the
panel is on THIS frame. That is why this keybind was claimed wrong twice in
one day at zero cost — no behaviour was ever derived from the claim. A
function that can be TOLD an edge can BRANCH on one.

Both presses of a session are saved even though only the closing one has a
panel to read. The opening frame costs one grab and answers a question the
closing one cannot: an opening frame WITH a panel in it means the previous
close never registered.

⚠ THE OBVIOUS ALTERNATIVE — READ WHEN THE PANEL IS SEEN TO CLOSE — WAS BUILT,
RUN AND REFUTED BY ITS OWN SAVED FRAMES, 2026-08-09. The idea was that the
「类型」 header stops being legible early in the close, leaving the panel
readable for a moment afterwards. It does not:

    six closes, six saved frames, every one pure game world — no panel at all
    ink 0 / 9 / 6 / 0 / 0        (a real name plate reads in the hundreds)

By the time anything says shut, the panel is GONE, not fading.

⚠ AND THE GUARD THAT SHOULD HAVE CAUGHT IT SAID `tiles painted` ON ALL SIX.
`any_drawn` asks whether there is DETAIL in the tile rings, and its separation
(absent 5-26, empty 46-173) was measured with a panel on screen. Bare grass and
timber score far above 46. It answers "is something drawn here", which is only
the question you meant while a panel is up — it cannot tell you that one is.

⚠ WHAT IS GIVEN UP, PLAINLY: a close that no key announced — alt-tab, a
disconnect dialog, another agent — reads nothing at all. The drift check still
notices the state change, so `tab_open` stays honest; the loadout simply keeps
whatever it last knew. A missed reading, not a wrong one.

⚠ AND THE COST IS WHY IT CANNOT SIMPLY POLL. A GDI grab is ~6 ms before it
copies a pixel, so at the 10 ms dispatcher tick an unconditional check would
be most of a core. Checks are event-driven, with a slow drift check to catch
what no key announced.

⚠ NOTHING MAY BE ADDED THAT RUNS WHILE THE PANEL IS UP. Not a periodic
re-read, not a cached last-good reading, not a buffer of past frames, not a
score over kept frames. Every such scheme has to answer "which moment does
this describe", and while the panel is up the answer is "one the player has
already changed" — a gun caught mid-swap, with its muzzle in your hand,
becomes the gun the compensation is built for. This has been built and removed
more than once, so the versions are deliberately not described here or
anywhere else: written down they read as prior art, and prior art comes back.
`pixi run tab-watch` fails if anything CLASSIFIES before the panel closes.

`toggle_tab_open` — the one thing this replaces that is worth remembering —
flipped a cached bool on every Tab keypress and let a detection 300 ms later
correct it. For those 300 ms the flag was a guess, and a guess gates a dozen
`cond: '!tab_open'` entries, including whether recoil compensation runs. A
swallowed keypress (docs/game_quirks.md: one issued right after a previous
toggle simply does not arrive) left it inverted with nothing to notice. Here
the flag only ever changes because the screen was looked at.

Nothing here blocks: tick() does at most one grab. The ~15 ms of grab-and-save
happens on a Tab press, where nothing is being fired.
"""
import datetime
import os
import time

from config import HUD_REGIONS, TAB_DRIFT_S, TAB_SETTLE_S
from logbook import note

NAME_REGIONS = ['gun_name_1', 'gun_name_2']

# ⚠ EVERY CLOSE LEAVES ITS FRAME ON DISK, and it is on by default because the
# question it answers cannot be answered any other way. This file rests on a
# claim about what the screen looks like at one instant -- the anchor text has
# gone, the panel has not -- and no amount of reasoning settles that. The
# picture does. It sits beside the run's log, named with the same clock, so a
# line in the log and the frame it was read from are one lookup apart.
#
# It is the right of the operator to look at what the machine looked at. When a
# reading is wrong, "what did it see" has been, every single time in this
# repository, the question that ended the argument -- and until now the only
# way to ask it was to reproduce the moment.
SHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))), 'calibration', 'artifacts', 'robot', 'tab')

_BLOCK_RECT = None


def _BLOCK():
    """The rectangle TabGrabber fills: both name plates and all ten tiles.

    Asked of detector/tab_items rather than spelled here, and lazily, so that
    importing this module still costs nothing on a state-only path.
    """
    global _BLOCK_RECT
    if _BLOCK_RECT is None:
        from detector.tab_items import tab_blocks
        _BLOCK_RECT = tab_blocks()['right']
    return _BLOCK_RECT


class TabWatch:
    """Measured Tab state plus the loadout read as the panel closed.

    detectors is the Dispatcher's registry, shared by reference so anything
    registered later is visible here too. Needs 'gun_tag', 'tab_weapon' and
    'tab_attachment'; missing ones just disable their part.
    """

    def __init__(self, state, detectors, verbose=True, shot_dir=SHOT_DIR,
                 on_change=None):
        """shot_dir: where the close's frame is kept. None to keep none.

        `on_change` is called AFTER a reading has been written through to
        GameState, with no arguments. It exists because writing the record and
        telling the DEVICE are two different things, and until 2026-08-09 only
        the first happened here: the upload hung off the dispatcher noticing
        the panel disappear, which is a second event that may not arrive.

        Measured over every play log on disk: 166 Tab reads, and 69 of them --
        42% -- had no observed close within three seconds of the read. In each
        of those the Weapon object held the new curve and the Pico held the old
        one, with nothing anywhere saying so.

        ⚠ IT IS A PARAMETER BECAUSE THE OFFLINE GATE WROTE INTO THE EVIDENCE
        DIRECTORY, and that is a worse failure than it sounds. `pixi run
        tab-watch` drives this class with a synthetic screen -- a zeroed buffer
        with one stamped pixel -- so every run of the suite dropped black PNGs
        into the folder that is supposed to hold what the GAME looked like.
        Seventeen of them landed there within minutes of the feature existing,
        and from the outside they are indistinguishable from a real capture:
        same name, same shape, same directory. The operator opened the folder,
        found it full of black frames, and reasonably concluded the capture was
        broken.

        A directory whose whole purpose is to answer "what did it actually
        see" cannot hold anything that was never seen.
        """
        self.state = state
        self._detectors = detectors
        self.verbose = verbose
        self._shot_dir = shot_dir
        self._on_change = on_change
        self.open = False
        self.loadout = None          # {'weapons':..., 'attachments':..., 'ts':}
        self._panel_grab = None
        self._watch_until = 0.0      # a key was seen; watch for the change
        self._want = None            # what we expect it to become
        self._next_drift = 0.0

    # ── Capture, built lazily so a state-only caller costs nothing ──

    def _panel_frame(self):
        """The one rectangle this file grabs: both plates, all ten tiles.

        ⚠ ONE GRAB, AND THE COUNT IS THE COST, NOT THE AREA. A GDI grab costs
        ~6 ms before it copies a pixel -- measured directly, 78.68 ms for the
        twelve regions actually read against 13.46 for the whole block, and
        78.68/13 = 6.05. So cutting this into the pieces it uses is 6x slower
        while moving a sixth of the data:

            cost ~= 6 ms x grabs + ~4-8 ms x megapixels

        Which is also why the block is `only=('right',)` and not the whole Tab
        screen: 9.6 ms against 18.7, one grab either way, all of the difference
        in pixels.
        """
        if self._panel_grab is None:
            from detector.tab_items import TabGrabber
            self._panel_grab = TabGrabber(only=('right',))
        return self._panel_grab.grab()

    def close(self):
        if self._panel_grab is not None:
            try:
                self._panel_grab.close()
            except Exception:
                pass
        self._panel_grab = None

    def _log(self, msg):
        """A line for the LOG FILE. Not the terminal. See logbook.py.

        ⚠ THIS CHANNEL IS 66% OF EVERY PLAY LOG, and that is what moved it off
        the screen. Measured on two real sessions 2026-08-09: 154 of 234 lines
        and 116 of 172 were `[tab]`. Every Tab press writes six of them -- the
        snap, the panel verdict, the two slot reads, the loadout, the flag --
        and the status table, which is the only thing anybody reads while
        playing, is three. So the reading scrolled itself away.

        NOTHING IS LOST: the file still gets all of it, timestamped, which is
        the form the question takes afterwards ("what did it see at 15:24").
        `_warn` is the other half -- see there for what still reaches the
        screen and why the split is where it is.
        """
        if self.verbose:
            note(f'[tab] {msg}')

    def _warn(self, msg):
        """A line for the TERMINAL. For when the reading did not happen.

        ⚠ THE SPLIT IS NOT "IMPORTANT vs UNIMPORTANT", IT IS "DID THE THING
        HAPPEN". `_log` narrates a Tab that worked; this one says a Tab that
        did not -- the grab raised, the classify raised, a kit was refused
        because nothing can name the gun. Those change what the operator
        should do NEXT, and they are rare by construction: a session with one
        of these per Tab is a session that is broken, and then the flood is
        the message.

        A quiet failure here is the exact shape this repository keeps paying
        for -- `read_loadout` returning None looks identical to a Tab nobody
        pressed, and the loadout simply keeps whatever it last knew.
        """
        if self.verbose:
            print(f'[tab] {msg}', flush=True)

    # ── Reads ──

    def measure_open(self, frame=None):
        """Is there a panel with a gun in it? -> bool, or None if unreadable.

        ⚠ THE SIGNAL MOVED OFF THE 「类型」 ANCHOR ON 2026-08-09 and onto the
        boxed slot numbers inside the weapon panel itself. Three reasons, in
        the order they matter:

        SAME PIXELS, SAME INSTANT. The tag box is inside the rectangle this
        file already grabs, so the openness judgement and the loadout now come
        off ONE frame. The anchor is 1282 px away and was a separate grab taken
        ~4 ms later -- two rectangles describing two moments, which is the
        distinction this whole file exists around.

        IT IS THE ACTUAL PRECONDITION, not a proxy. The tag is drawn only when
        the panel is up AND a gun occupies that slot, and an open panel over an
        empty rack has nothing worth reading.

        SEPARATION, on 38 real frames labelled by eye: white 110-112 against
        0-3, box darkness 29-51 against 112-205. The anchor's own margin, on
        brightness, is TWO counts.

        ⚠ AND `open` NOW MEANS SOMETHING SLIGHTLY NARROWER, which is a real
        change and not a refinement: an inventory opened over an EMPTY RACK
        reads False. A dozen `cond: '!tab_open'` entries hang off this,
        including whether compensation runs -- so with no gun in the rack the
        tool now stays armed while the panel is up. It has no curve to play
        there (no gun, no cell), so the cost is a state that cannot act rather
        than a wrong action. Stated because nothing downstream would say it.

        """
        det = self._detectors.get('gun_tag')
        if det is None:
            return None
        try:
            if frame is None:
                frame = self._panel_frame()
            return bool(det.any_drawn(frame))
        except Exception as e:
            self._warn(f'open-check failed: {e}')
            return None

    def snap(self, tag):
        """Grab the weapon panel and put it on disk. -> the frame, or None.

        ⚠ THE GRAB IS THE FIRST THING THAT HAPPENS ON A TAB KEY, AND THE SAVE
        IS THE SECOND. Nothing is decided, checked or classified before them.
        A grab is ~10 ms; deciding first is what put a permission in front of
        it that only arrives once the panel is down.

        It fires on BOTH presses of a Tab session, the one that opens and the
        one that closes. Only the closing one has a panel to read, but both are
        pictures worth having: an OPENING frame with a panel in it means the
        previous close never registered, and an opening frame of bare world is
        what a normal open looks like.

        `tag` says which grab this was, and the two callers pass a literal --
        'press' from on_key, 'read' from a read_loadout given no frame. It is
        never a verdict. The names used to be `opening`/`closing`, i.e. what
        this object BELIEVED, and on 8 of 38 real saves that belief was wrong:
        bare grass filed as a close, off a stale `open` flag. A filename that
        carries a belief makes a victim of every later reader.
        """
        try:
            frame = self._panel_frame()
        except Exception as e:
            self._warn(f'panel grab failed: {e}')
            return None
        self._log(f'snap ({tag}){self._save(frame, tag)}')
        return frame

    def read_loadout(self, frame=None):
        """Read both guns off a panel frame. -> dict or None.

        ⚠ CALLED ONCE PER TAB SESSION, ON THE KEYPRESS THAT CLOSES IT, AND ON
        THE FRAME `snap` ALREADY TOOK. Grabbing again here would be a second
        picture some tens of milliseconds later than the one saved beside the
        log — so the evidence and the reading would be of two different
        moments, which is the whole failure this file keeps having.

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
        if frame is None:
            frame = self.snap('read')
            if frame is None:
                return None
        out = {'ts': time.perf_counter(), 'weapons': None,
               'attachments': None, 'present': {}}
        try:
            # ⚠ SLOT PRESENCE IS READ FIRST AND EVERYTHING ELSE HANGS OFF IT.
            # The boxed number is drawn only when that rack slot holds a gun,
            # so it answers "is there anything here to read" before a single
            # template is scored -- and answers it PER SLOT, which is the part
            # that was missing. In the 2026-08-09 corpus slot 2 was empty in
            # every frame, and reading it anyway produced a kit for a gun that
            # was not there.
            tag = self._detectors.get('gun_tag')
            for slot in (1, 2):
                out['present'][slot] = (bool(tag.drawn(frame, slot))
                                        if tag is not None else True)
            if not any(out['present'].values()):
                self._log('neither rack slot has a gun in it — nothing to read')
                return out

            if weap is not None:
                got = weap.classify({k: _crop(frame, k) for k in NAME_REGIONS})
                # A slot with no gun cannot have a name, whatever the plate
                # region happens to score. Dropping it here means the name
                # never reaches the template narrowing or GameState.
                out['weapons'] = tuple(n if out['present'][i + 1] else ''
                                       for i, n in enumerate(got))
                # ⚠ INK IS THE SECOND, INDEPENDENT SOURCE. A blank name has two
                # causes that print identically: the plate was not there, or it
                # was and the OCR could not read it. `ink` counts white-text
                # pixels THROUGH THE SAME MASK classify matches with, so the two
                # are claims about one set of pixels rather than two opinions.
                out['ink'] = [_ink(weap, frame, k) for k in NAME_REGIONS]
            if att is not None:
                # ⚠ AN UNPAINTED PANEL IS NOT A BARE GUN. classify() reports an
                # unpainted tile as '', the same '' an empty slot gives, and
                # _publish writes those onto the weapon: a fully kitted gun
                # becomes `bare` and the compensation is cleared. A tile that is
                # merely EMPTY is still DRAWN (detector/CLAUDE.md: border-ring
                # Sobel p90 46-173 empty against 5-26 for no tile at all), so
                # this does not refuse a bare gun.
                out['painted'] = bool(att.any_drawn(frame))
                if out['painted']:
                    # ⚠ THE EFFECTIVE NAME, NOT THIS FRAME'S READ. The names
                    # narrow each slot's template bank to what the gun can
                    # physically hold, and building them from the plate read
                    # alone means a blank plate narrows to NOTHING -- every tile
                    # then scored against all 55 templates, which does not fail,
                    # it answers.
                    #
                    # Measured, play log 2026-08-09 15:10:20: GameState knew the
                    # gun was an m416 (the HUD detector had named it seven
                    # seconds earlier, off completely different pixels), the Tab
                    # plate came back blank, and the stock slot was published as
                    # `Stock_SniperRifle_CheekPad_C` -- a part the m416 cannot
                    # mount. `compatible('m416')` would have refused it
                    # outright; it was never asked.
                    named = {i + 1: nm
                             for i, nm in enumerate(self._names(out)) if nm}
                    kit = att.classify(frame, named)
                    out['attachments'] = {g: v for g, v in kit.items()
                                          if out['present'].get(g)}
        except Exception as e:
            self._warn(f'panel read failed: {e}')
            return None
        self._log(_describe(out))
        return out

    def _names(self, out):
        """This frame's plate read, falling back to what GameState knows.

        Two independent sources for one object, which is the shape every guard
        in this repository ends up taking. The plate read is this frame's own
        evidence; weapon_hud's reading comes off the bottom-right HUD, a
        different set of pixels through a different bank, and it survives a
        plate that has faded. Either alone answers less than both.
        """
        mine = out.get('weapons') or ('', '')
        try:
            known = self.state.weapon_name
        except AttributeError:
            known = ('', '')
        pres = out.get('present') or {}
        return tuple('' if pres.get(i + 1) is False else (m or k)
                     for i, (m, k) in enumerate(zip(mine, known)))

    def _save(self, frame, tag):
        """Write a frame beside the run's log. -> '   shot <path>' or ''.

        ⚠ IT SAVES THE WHOLE BLOCK, not the crops it read. The crops are what
        the detector looked at; the block is what the SCREEN looked like, and
        when a reading is wrong the two questions have different answers --
        a crop can be perfectly readable while the panel behind it is the
        wrong panel, half drawn, or somebody else's.

        ⚠ AND THE BLOCK ALONE. A 「类型」 anchor strip was composited above it
        until 2026-08-09, on the reasoning that a picture must also show what
        the thing DECIDING was looking at. That reasoning died with the anchor:
        the decision is `gun_tag`, which is INSIDE this block, so the frame and
        the verdict already come off the same pixels. The strip cost a second
        grab of +4.24 ms (n=200 interleaved, 18.9 sigma) on every press to show
        a signal nothing reads.

        ⚠ SO THE DIRECTORY HOLDS TWO SHAPES. Frames saved on 2026-08-09 between
        the strip landing and this are TALLER, with an 18 px strip and padding
        above the block. Reading either kind is the same operation -- take the
        LAST tab_blocks()['right'] rows -- which is why they were not rewritten.

        Failure is one line and never fatal: a log that cannot save a picture
        is still a log, and this runs on the tick that re-arms the firmware.
        """
        if not self._shot_dir:
            return ''
        try:
            import cv2
            y, x, h, w = _BLOCK()
            os.makedirs(self._shot_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime('%m%d_%H%M%S_%f')[:-3]
            path = os.path.join(self._shot_dir, f'{stamp}_{tag}.png')
            if not cv2.imwrite(path, frame[y:y + h, x:x + w]):
                return '   (frame not saved: imwrite refused)'
            return f'   shot {path}'
        except Exception as e:
            return f'   (frame not saved: {e!r})'

    # ── Driving ──

    def on_key(self, now=None):
        """The Tab key was PRESSED. Watch for the screen to actually change.

        `now` should be the KeyEvent's own timestamp: the event may have sat in
        the poller's queue for a tick or two, and the settle window should be
        measured from when the key was pressed, not from when this got around
        to hearing about it. It also has to be the same clock tick() is driven
        on, or the window is nonsense.

        The keypress is NOT the state change. It is a request that the game may
        honour in 28-38 ms (opening), 77-128 ms (closing), or not at all if it
        was swallowed. So this only arms a watch; `open` moves when the screen
        does.

        ⚠ AND IF THE PANEL IS UP, THIS IS WHERE IT IS READ. Not on the close
        the watch below detects — by then it is gone. See the module docstring
        for the six saved frames that settled it. The press leads the close by
        77-128 ms and the poller holds an event for at most a tick or two, so
        the grab lands with 57 ms or more of panel left.

        ⚠ IT TAKES NO EDGE, AND MUST NOT GROW ONE BACK. The parameter existed
        for one day, during which the keybind claim behind it was stated wrong
        TWICE — "Tab toggles" then "Tab is HELD" — and cost nothing but prose
        both times, because what decides here has never been the edge. It is
        `up`: the SCREEN's answer on THIS frame. A press seen while the panel
        is up reads it, a press seen while it is down does not, and neither
        branch would change if the keybind did. The module docstring holds the
        measurement and both retractions; control/match.py holds the filter.

        The press is a TRIGGER, never an answer: `open` is still whatever the
        screen last said, and if the game eats this key the panel stays up and
        the reading simply describes it correctly.
        """
        now = time.perf_counter() if now is None else now
        # ⚠ LOGGED ON EVERY PRESS, BOTH THE OPENING ONE AND THE CLOSING ONE,
        # because the closing press was invisible. `stop_recoil` was already
        # True while the panel was up, so the state line printed nothing and
        # the log held no timestamp for the press that closed it -- which is
        # exactly the number needed to work out how much panel was left by the
        # time anything looked. It had to be inferred from the game's 77-128 ms
        # instead of read off.
        # ⚠ GRAB AND SAVE FIRST, DECIDE AFTER. Every press.
        frame = self.snap('press')
        up = None if frame is None else self.measure_open(frame)
        self._log(f'Tab press: panel {"UP" if up else "not up"}'
                  + ('' if up is not None else ' (unreadable)'))
        if up:
            got = self.read_loadout(frame)
            if got is not None:
                self._publish(got)
        # ⚠ THIS FRAME IS A MEASUREMENT, so it settles `open` rather than being
        # filtered through it. The old shape read `self.open`, which could be
        # stale -- and was, on those same 8 frames.
        if up is not None and up != self.open:
            self._set_open(up)
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
        # ⚠ NOTHING IS READ HERE, AND THAT IS THE CORRECTION OF 2026-08-09.
        # This is where the read used to be, on the reasoning that the anchor
        # goes dark before the panel does. Six saved frames say otherwise: by
        # the time this runs there is no panel, only the game world. The
        # reading was taken on the keypress, ~100 ms ago, and control/match.py's
        # _follow_tab re-arms the firmware right after this — with the loadout
        # that reading published.

    def _publish(self, got):
        """Write a reading through to GameState, as the detections used to.

        ⚠ A KIT IS NOT PUBLISHED FOR A GUN NOTHING CAN NAME, and that is the
        root CLAUDE.md's second law rather than caution. The slot templates are
        narrowed BY the weapon name; with no name every tile is matched against
        all 55, and a blind match does not fail, it answers. Seen in a play log
        2026-08-09, both name plates blank: `muzzle-choke` (a shotgun part) on
        one gun and `stock-cheek_pad` (a sniper part) on the other, published
        and keyed into the curve store as if measured.

        The name checked is the EFFECTIVE one -- weapon_hud's reading counts,
        so a gun already identified from the HUD keeps its kit even when the
        Tab plate is unreadable.

        ⚠ AND IT TELLS BOTH CONSUMERS AT THE END: the SCREEN (print_status)
        and the DEVICE (on_change). Neither did until 2026-08-09, and both had
        the same shape of bug -- the reading was written into GameState and
        then everything downstream waited for some OTHER event to notice.

        the screen   `print_status` hung off the dispatcher's detection queue,
                     so a Tab read reached the operator only when an unrelated
                     posture or highlight read happened to run next. 24 Tab
                     reads produced 8 tables in one play log, each describing
                     the state at whatever moment that other read landed.

        the device   the upload hung off the dispatcher seeing the panel
                     DISAPPEAR. Over every play log on disk: 166 Tab reads,
                     69 with no observed close within 3 s. In each of those
                     the Weapon object held the new curve and the Pico held
                     the old one -- 「压枪还是老的」 -- with nothing saying so.

        Both are deduped downstream (print_status on content, _said_pattern on
        the armed line), so a Tab that changed nothing costs nothing.

        ⚠ on_change IS CALLED EVEN WHEN NOTHING IN THE READING CHANGED, and
        that is deliberate. "Did it change" is a comparison this class would
        have to author a second time -- against what the Weapon already held,
        for four slots plus the name plus the optic -- and the two authors
        would drift. The one place that comparison exists is the dedup on the
        printed line, where being wrong costs a duplicate line rather than a
        missed upload.
        """
        self.loadout = got
        if got['weapons'] is not None:
            self.state.weapon_gt = got['weapons']
            self.state.sync_weapons()
        if got['attachments'] is not None:
            eff = self.state.weapon_name
            for gun_id, a in got['attachments'].items():
                if not eff[gun_id - 1]:
                    self._warn(f'gun {gun_id}: slots read but the name plate '
                               f'was not, so the kit describes a gun nothing '
                               f'can name — NOT published (an unnamed gun '
                               f'matches every template blind)')
                    continue
                self.state.set_attachments(gun_id, a)
        # getattr, not try/except AttributeError: the latter would also
        # swallow one raised INSIDE print_status, and detector/game_state.py's
        # _fmt says in as many words that it reads the Weapon fields directly
        # SO THAT a class that moved them raises here instead of quietly
        # printing three blanks.
        show = getattr(self.state, 'print_status', None)
        if callable(show):
            show()
        # ⚠ THE DEVICE LAST, AFTER GameState IS WHOLE. Every set_ above runs
        # Weapon.set_seq, so the curve the upload reads is only correct once
        # all of them have run. Calling this per-slot would upload three
        # intermediate curves, and the last one would still be right -- but
        # the FIRST two would be a gun wearing half a kit, which is precisely
        # the "the record describes an object nobody measured" failure.
        if self._on_change is not None:
            self._on_change()


def _crop(frame, key):
    y, x, h, w = HUD_REGIONS[key]
    return frame[y:y + h, x:x + w]


def _ink(weap, frame, key):
    """White-text pixel count on a name plate, or None if it cannot be had."""
    fn = getattr(weap, 'ink', None)
    if fn is None:
        return None
    try:
        return int(fn(_crop(frame, key)))
    except Exception:
        return None


def _short(asset):
    """`Muzzle_Compensator_Large_C` -> `Compensator_Large`, for one log line."""
    if not asset:
        return '-'
    s = asset
    for pre in ('Upper_', 'Lower_', 'Muzzle_', 'Stock_', 'Magazine_',
                'SideRail_'):
        if s.startswith(pre):
            s = s[len(pre):]
            break
    return s[:-2] if s.endswith('_C') else s


def _describe(out):
    """The whole reading on one line: what was read, and what it was read from.

    ⚠ THE POINT IS THAT IT NAMES THE READING, NOT THE EVENT. `read at the
    close, panel still painted` was the previous version and it said only that
    something had happened -- so a session where every plate came back blank
    and every slot was matched blind logged eleven identical successful-looking
    lines. What a reading SAYS is the only thing that makes a log diagnosable
    without reproducing the moment.
    """
    names = out.get('weapons') or ('', '')
    ink = out.get('ink') or [None, None]
    kit = out.get('attachments') or {}
    pres = out.get('present') or {}
    bits = []
    for i in (0, 1):
        if pres.get(i + 1) is False:
            bits.append(f'gun{i + 1} (no gun in the rack slot)')
            continue
        got = names[i] or '?'
        pen = '' if ink[i] is None else f' ink {ink[i]}'
        slots = kit.get(i + 1)
        worn = ('  ' + ' '.join(f'{k}={_short(v)}'
                                for k, v in sorted(slots.items()))
                if slots else '')
        bits.append(f'gun{i + 1} {got}{pen}{worn}')
    paint = ('tiles painted' if out.get('painted') else
             'NO tiles painted — the panel had already gone, kit not read')
    return 'read at the close: ' + ' | '.join(bits) + f' | {paint}'
