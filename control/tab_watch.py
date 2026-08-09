"""TabWatch — is the inventory up, and what are the guns wearing.

Everything about the Tab screen that the live loop needs, kept OUT of the
per-frame capture. Its regions sit at the top of the screen and the gameplay
HUD at the bottom, so having both in one DXGI bounding box cost 5.46 ms of
every frame — 87% of the capture budget — for a panel that is usually not on
screen. See config.FRAME_REGIONS.

⚠ THE TAB KEY GRABS AND SAVES, IMMEDIATELY, BEFORE ANYTHING IS DECIDED. Then,
and only if the panel was up, that same frame is classified.

    Tab press, panel shut   ONE grab, saved `<stamp>_press.png`
    panel is up             nothing
    Tab press, panel up     ONE grab, saved, classify, publish
    anchor reads shut       nothing — the reading was taken ~100 ms ago

⚠ THE PRESS EDGE, AND THE FILTER IS IN control/match.py, NOT HERE. Nothing in
this file knows or asks which edge it is being handed; `on_key` decides from
what the SCREEN says at that instant. That is not tidiness — this paragraph
has been WRONG TWICE about the keybind and cost nothing either time, because
no behaviour was ever derived from it.

    said, 2026-08-09 am    "Tab toggles, so press-only sees the close"
    said, 2026-08-09 pm    "Tab is HELD, so the RELEASE is what closes it"

Both were inferred from intervals between log lines that LAG the key. The key
itself was finally measured off the saved frames' own stamps -- 66 press/
release pairs, panel judged by the same GunTagDetector the live path uses:

    KEY DOWN, press -> its own release      43..225 ms   median   97
    tap -> next tap                        279..954 s    median 2071

97 ms is a tap; the 2 seconds is the panel being looked at. So TAB IS A
TOGGLE, one session is TWO taps, and the panel falls on a different edge of
each -- which is exactly what the latencies already in docs/game_quirks.md
predict (28-38 ms to appear, 77-128 ms to go) once the key is known to be
down for 97:

     30 / 66   OPENING tap    press = world    release = PANEL
     28 / 66   CLOSING tap    press = PANEL    release = world
      7 / 66   nothing at all — the key was swallowed
      1 / 66   panel across both edges

THE PRESS IS THE ONE THAT CARRIES THE FINAL STATE. Every closing tap's press
catches the panel, and the final state is what a loadout has to be. The
release would add the ENTRY state, a second sample of the same session; that
was tried and dropped. By session, on those same taps:

     30   sessions with a panel on some edge
     29   ... a PRESS caught it too
      1   ... release-only, i.e. what dropping it costs      3.3%
                                     grabs 4.4/session -> 2.2

Nothing is checked, asked or measured before the grab. Measured, warm, four
runs:

    panel grab              8-12 ms   <- the only part that must beat the fade
    anchor grab + compose   3-9 ms    | after the panel pixels are already
    png write               ~4 ms     | captured, so they cost nothing that matters

Add the input path — poller 5 ms, dispatcher tick 10 ms — and the panel is on
disk-bound pixels within ~27 ms of the physical edge, against 77-128 ms before
the game takes the panel down. 50-100 ms of margin, and it is margin only
because the grab is FIRST. Deciding first is what put a permission in front of
it that by measurement only arrives once the panel is already gone.

Both edges are saved even though only one has a panel to read. The opening
frame costs one grab and answers a question the closing one cannot: an opening
frame with a panel still in it means the previous close never registered.

⚠ THE OBVIOUS ALTERNATIVE — READ WHEN THE ANCHOR SAYS SHUT — WAS BUILT, RUN
AND REFUTED BY ITS OWN SAVED FRAMES, 2026-08-09. The idea was that the 41x18
「类型」 header stops being legible early in the close, leaving the panel
readable for a moment afterwards. It does not:

    six closes, six saved frames, every one pure game world — no panel at all
    ink 0 / 9 / 6 / 0 / 0        (a real name plate reads in the hundreds)

By the time the anchor reads shut the panel is GONE, not fading. Every read
came back with two blank plates.

⚠ AND THE GUARD THAT SHOULD HAVE CAUGHT IT SAID `tiles painted` ON ALL SIX.
`any_drawn` asks whether there is DETAIL in the tile rings, and its separation
(absent 5-26, empty 46-173) was measured with a panel on screen. Bare grass and
timber score far above 46. It answers "is something drawn here", which is only
the question you meant while a panel is up — it cannot tell you that one is.
The thing that CAN is the anchor, which is what `open` already is.

⚠ WHAT IS GIVEN UP, PLAINLY: a close that no key announced — alt-tab, a
disconnect dialog, another agent — reads nothing at all. The drift check still
notices the state change, so `tab_open` stays honest; the loadout simply keeps
whatever it last knew. A missed reading, not a wrong one.

⚠ AND THE COST IS WHY IT CANNOT SIMPLY POLL. A GDI grab is ~5 ms almost
regardless of size (41x18 measures 5.2 ms), so at the 10 ms dispatcher tick a
single unconditional anchor check would be 52% of a core. Checks are
event-driven, with a slow drift check to catch what no key announced.

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

Nothing here blocks: tick() does at most one 5 ms anchor check. The ~25 ms of
grab-and-save happens on a Tab edge, where nothing is being fired.
"""
import datetime
import os
import time

import numpy as np

from config import HUD_REGIONS, TAB_DRIFT_S, TAB_SETTLE_S
from logbook import note

ATT_REGIONS = [k for k in HUD_REGIONS if k.startswith('att_')]
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
    registered later is visible here too. Needs 'tab_type', 'tab_weapon' and
    'tab_attachment'; missing ones just disable their part.
    """

    def __init__(self, state, detectors, verbose=True, shot_dir=SHOT_DIR):
        """shot_dir: where the close's frame is kept. None to keep none.

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

        `tab_type` is kept and still registered: it is the second, independent
        source, and _compose logs the two whenever they disagree.
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

    def _anchor_says(self):
        """What the retired 「类型」 anchor thinks. -> bool or None. Diagnostic."""
        det = self._detectors.get('tab_type')
        if det is None:
            return None
        try:
            return bool(det.classify(self._type_crop()))
        except Exception:
            return None

    def _compose(self, frame):
        """The panel block with the ANCHOR STRIP laid above it. -> array.

        ⚠ THE ANCHOR IS IN THE PICTURE BECAUSE IT IS THE OTHER HALF OF THE
        EVIDENCE. `open` is decided by that 41x18 「类型」 header and by nothing
        else, so a frame showing only the weapon panel can say "there was no
        panel" but never "and here is what the thing that decides was showing
        at the same moment". They sit 1282 px apart on screen and would never
        appear in one crop by accident.

        It is a SECOND grab, 3-9 ms, taken AFTER the panel one -- so the strip
        is a few milliseconds newer than the block below it. That is worth
        knowing when reading a frame caught mid-transition, and it is the right
        way round: the panel is the thing that has to be caught in time.

        ⚠ TWO GRABS, AND IT IS NOT MONOTONE IN EITHER DIRECTION. Interleaved,
        n=200 per arm:

            A  panel only                  8.71 ms  sd 1.86
            B  panel + type, TWO grabs    12.95 ms  sd 2.58   <- what it does
            C  panel + type, ONE grab     16.16 ms  sd 1.85

            adding type   B - A = +4.24 ms   18.9 sigma
            one vs two    C - B = +3.21 ms   14.3 sigma

        ONE grab loses because the rectangles overlap in y (panel 123..680,
        type 129..147) and RegionGrabber bands by y -- asking one grabber for
        both merges them into a single 1911x557 box, three times the pixels.

        MANY grabs lose far worse. Cutting the panel into the twelve regions
        actually read is 78.68 ms (n=60) against 13.46 for the block, while
        moving a sixth of the data: a GDI grab costs ~6 ms before it copies
        anything, and 78.68/13 = 6.05 is that constant measured directly.

            cost ~= 6 ms x grabs + ~4-8 ms x megapixels

        So: as few grabs as possible, but never at the price of a bounding box
        that balloons. Two.

        ⚠ THE 4.24 ms IS AFFORDABLE ONLY BECAUSE IT IS SPENT SECOND. The panel
        pixels are already captured when this runs, so the anchor comes out of
        the margin against the fade rather than out of the race for it.

        ⚠ AND THE FIRST VERSION OF THIS COMPARISON WAS NOT A MEASUREMENT: 17.47
        against 18.60, one 40-sample run each, block after block, no variance
        reported -- 1.13 ms sitting inside a noise band that was never
        computed. Redone interleaved the gap is real, which is luck, not
        method. The separate arm's sd alone is twice the difference it was
        being asked to establish.

        ⚠ THE ANCHOR SHOULD EVENTUALLY MOVE INSIDE THE BLOCK, AND THE REASON IS
        NOT THE 4.24 ms. tab_layout.gun_tag_point puts the boxed slot numbers
        「1」「2」 at (2237,145) and (2237,447), both INSIDE the rectangle this
        already grabs. Reading presence from them would cost no extra grab at
        all -- and, which matters more, the openness judgement and the loadout
        would come off THE SAME PIXELS AT THE SAME INSTANT. As it stands the
        anchor is grabbed 4 ms after the panel, so two rectangles describe two
        moments, and this file's whole history is that distinction.

        It is also the RIGHT precondition rather than a proxy: those numbers are
        drawn only when the panel is up AND a gun occupies that slot, and a
        panel with no gun in it has nothing worth reading.

        ⚠ BUT NOT AS A PIXEL COUNT, AND NOT ON BRIGHTNESS EITHER. Both are
        measured, and the anchor this would replace is the proof.

        `type`, 41x18, over the same 12 paired backgrounds:

            max        open 238..238    shut 178..236    gap TWO counts
            mean       open 142..156    shut 167..215    gap 11
            white px   open 220..249    shut   0..738    OVERLAPS
            sat_min    open 0.000       shut 0.024..0.125   <- the real one

        Brightness is worth nothing here: the header text clips at exactly 238
        and a bright background reaches 236. What carries the anchor is that a
        pure white glyph puts a FULLY DESATURATED pixel on screen and the world
        never does -- 0.000 against a world minimum of 0.024. And note the
        `white px` row: counting how much white is present OVERLAPS, on the
        very region the criterion works on. Amount fails, kind succeeds.

        The play log says the same thing about the plates from the other side:
        15:24:10 recorded `ink 11248 / 6424` on a frame whose saved picture is
        bare sky and a shed -- no panel at all -- while a real plate reads in
        the hundreds. The false positive is BIGGER than the true one.

        ⚠ AND OVEREXPOSED SKY IS ALSO SATURATION ZERO, which is the hole in the
        12-background corpus: it has no sky in it. So even `sat_min` is not
        enough on its own. It has to be the GLYPH -- the shape of a boxed digit
        -- and the shape is what neither amount nor kind can stand in for.

        Which needs one frame of a panel that is actually drawn, and there is
        not one on disk: the only paired open/shut corpus
        (calibration/artifacts/tab_type/bg*) was captured with an EMPTY RACK, so
        its `open` frames show the dimmed blurred world and no weapon panel at
        all. `snap` now saves both edges of every Tab, which is exactly the
        corpus that is missing.
        """
        y, x, h, w = _BLOCK()
        block = frame[y:y + h, x:x + w]
        try:
            crop = self._type_crop()
            anchor = crop['type'] if isinstance(crop, dict) else crop
            ah, aw = anchor.shape[:2]
        except Exception:
            return block
        pad = 4
        out = np.zeros((h + ah + 3 * pad, max(w, aw + 2 * pad), 3), np.uint8)
        out[pad:pad + ah, pad:pad + aw] = anchor
        out[ah + 3 * pad:ah + 3 * pad + h, :w] = block
        return out

    def snap(self, tag):
        """Grab the weapon panel and put it on disk. -> the frame, or None.

        ⚠ THE GRAB IS THE FIRST THING THAT HAPPENS ON A TAB KEY, AND THE SAVE
        IS THE SECOND. Nothing is decided, checked or classified before them.
        A grab is ~10 ms; deciding first is what put a permission in front of
        it that only arrives once the panel is down.

        It fires on BOTH presses, the one that opens and the one that closes,
        and the file says which. Only one of the two has a panel to read, but
        both have a picture worth having: an OPENING frame with a panel in it
        means the previous close never registered, and an opening frame of bare
        world is what a normal open looks like. Two labelled pictures per Tab
        say more than one unlabelled one.
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

        Failure is one line and never fatal: a log that cannot save a picture
        is still a log, and this runs on the tick that re-arms the firmware.
        """
        if not self._shot_dir:
            return ''
        try:
            import cv2
            os.makedirs(self._shot_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime('%m%d_%H%M%S_%f')[:-3]
            path = os.path.join(self._shot_dir, f'{stamp}_{tag}.png')
            if not cv2.imwrite(path, self._compose(frame)):
                return '   (frame not saved: imwrite refused)'
            return f'   shot {path}'
        except Exception as e:
            return f'   (frame not saved: {e!r})'

    # ── Driving ──

    def on_key(self, now=None, event='press'):
        """A Tab key EDGE was seen. Watch for the screen to actually change.

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
        for the six saved frames that settled it. The edge leads the close by
        77-128 ms and the poller holds an event for at most a tick or two, so
        the grab lands with 57 ms or more of panel left.

        ⚠ THIS FUNCTION DOES NOT KNOW WHICH EDGE IT IS BEING HANDED, AND MUST
        NOT LEARN. `event` names the file and nothing else. Live it is fed the
        PRESS only (the filter is in control/match.py, with the measurement);
        the offline gate feeds it both, which is the point — what decides here
        is `self.open`, the SCREEN's answer, so an edge seen while the panel is
        up reads it and an edge seen while it is down does not.

        That is why this function needed no correction either time the keybind
        claim was wrong, and the claim was wrong twice in one day. See the
        module docstring for both retractions. A version that had encoded "the
        release is the one that closes it" would have been wrong in BEHAVIOUR.

        The edge is a TRIGGER, never an answer: `open` is still whatever the
        screen last said, and if the game eats this key the panel stays up and
        the reading simply describes it correctly.
        """
        now = time.perf_counter() if now is None else now
        # ⚠ LOGGED IN BOTH DIRECTIONS, because the close press was invisible.
        # `stop_recoil` was already True while the panel was up, so the state
        # line printed nothing and the log held no timestamp for the press that
        # closed it -- which is exactly the number needed to work out how much
        # panel was left by the time anything looked. It had to be inferred
        # from the game's 77-128 ms instead of read off.
        # ⚠ GRAB AND SAVE FIRST, DECIDE AFTER. Every edge, both directions.
        # ⚠ AND THE TAG IN THE FILENAME IS THE EVENT, NEVER A VERDICT. It used
        # to be `opening`/`closing`, i.e. what this object BELIEVED -- and on 8
        # of 38 saved frames that belief was wrong, bare grass filed as a
        # close. A name that carries a belief turns every later reader into a
        # victim of it; `press`/`release` is a fact about the keyboard.
        frame = self.snap(event)
        up = None if frame is None else self.measure_open(frame)
        self._log(f'Tab {event}: panel {"UP" if up else "not up"}'
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

        ⚠ AND IT PRINTS THE STATUS TABLE AT THE END, which it did not until
        2026-08-09. `print_status` hung off `_process_pending` alone, so a Tab
        read reached the screen only when some UNRELATED detection happened to
        run next -- 24 Tab reads produced 8 tables in one play log, and each of
        those 8 described the state at whatever moment the next posture or
        highlight read landed. The panel is where a loadout actually changes;
        it is the one place the table must not lag. It is deduped on content,
        so a Tab that changed nothing costs nothing.
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
