"""Capture ADS / hip-fire frames for every scope a bolt-action can mount.

The point of the dataset is one downstream question: *is the player scoped in
right now?* Nothing in this file answers it — it only produces the frames that
let the answer be designed from evidence instead of guessed. Which is why the
capture is deliberately broader than any single method would need:

  every scope        iron sights, red dot, holo, 2x, 3x, 4x, 6x, 8x. The 3x+
                     scopes paint an unmistakable black ring over the whole
                     screen; a red dot at 1x barely moves a pixel outside the
                     centre. A detector that only ever saw 4x would look
                     perfect and be useless.

  ten backgrounds    between takes the view turns right by a random step and
                     tilts somewhere above or below the horizon, so the
                     *scene* varies while the *scope* does not. Anything that
                     survives a median across views is caused by the scope,
                     not by what is behind it — see --report, which is built
                     on exactly that. Ten steady in-scope shots per scope.

  the transition     ADS is not a step function. Frames are taken at 40, 150
                     and 400 ms after the right button goes down, so the
                     scope-in animation is in the data and the eventual
                     detector's latency can be measured rather than assumed.

  both edges         one hip frame before the press and one after the release.
                     The release side is the negative that matters: a detector
                     that latches on and never lets go passes every test built
                     only from the press side.

Frames are full-screen — whatever crop the detector ends up wanting can be cut
later, and a crop chosen now would silently decide the method. They are JPEG
q95 by default (~700 KB vs ~4 MB for PNG at 3440x1440, and a full run is ~220
frames); pass --png when a lossless copy is wanted.

Equipping the scope is the other agents' job, and --equip auto hands it to
them: control/inventory.py opens Tab, finds the scope in 库存 or on the
ground, drags it onto the gun and reads the slot back. Put all eight in the
backpack first — a preflight pass says which are missing before any capturing
starts, rather than four scopes into a run. --equip manual instead stops and
lets the operator fit each one by hand.

Either way the result is checked, not assumed: the scope slot is classified
with AttachmentDetector and the asset it reads is written into every frame's
record. A run cannot silently label 20 frames "scope_6x" when the drag failed
and the gun still wears a 4x.

Per scope the loop is: Tab -> confirm the Tab screen is really up -> drag the
scope onto the gun -> read the slot back -> Tab -> for each of ten views, tap
the right button, frames, tap it again -> Tab again for the next scope.

ADS is a toggle, and it is the *release* that switches into the sight —
holding the right button down is hip aim and no scope ever appears. Run
20260801_222936 is 64 frames of that mistake. Hence --probe: three frames and
a side-by-side picture, before committing to four hundred.

Usage (from the repo root)
    # look before leaping: is the sight actually in front of the camera?
    python calibration/capture_ads.py --weapon kar98k --probe

    # the sniper is on the player somewhere and every scope is in the
    # backpack: which slot holds it is read off the Tab name plate, and the
    # gun is switched into the hands before anything is captured
    python calibration/capture_ads.py --weapon kar98k

    # how long the ADS toggle takes, in and out — run this before trusting
    # any sampling time, and again whenever the sight changes class
    python calibration/capture_ads.py --weapon kar98k --timing

    # fit each scope by hand instead, pressing F8 to continue
    python calibration/capture_ads.py --weapon kar98k --equip manual

    # only the ones still missing
    python calibration/capture_ads.py --scopes scope_6x,scope_8x

    # offline: what separates ADS from hip, per scope and across all of them
    python calibration/capture_ads.py --report docs/ads/runs/<stamp>

Runs land in docs/ads/runs/<timestamp>/: one directory per scope, plus a
manifest.json in the shared CaptureRun format (calibration/capture_run.py).
Runs captured before 2026-08-03 carry index.jsonl + meta.json instead and are
read through the same API — see load_dir there. They are not converted: those
frames cost tens of minutes of game time each and cannot be re-made, so a
converter would only produce a lossy copy of an irreplaceable original.

STILL IN docs/ads/runs/, NOT docs/runs/ads/, and that is not inertia. The
directory a frame lives in is ground truth for a different regression:
tools/test_tab_open.py reads docs/ads/runs/** as "gameplay, Tab shut" and
docs/runs/** as "a capture OF the Tab screen". Moving these would silently
relabel 400 frames in that corpus. The format is what unifies; the path is
already load-bearing.

WHAT THIS FILE CAN AND CANNOT LABEL. `state: ads` is written into every record
and is NOT a label — it is a fact about the procedure ("the right button was
tapped and this frame is 700 ms later"), which is true, rather than a claim
that a sight picture appeared, which nothing here verifies. Run 20260801_222936
is 64 frames where the procedure ran exactly as written and produced no sight
picture at all. The scope on the gun IS labelled, because the equip reads the
slot back. See scope_label() for the case analysis.

What came of it: detector/ads_detector.py, fitted and scored by
calibration/fit_ads_detector.py over these runs. Its NOT_SCOPED / SCOPED tables
are the only ground truth about the sight picture that exists — a human read
the runs and adjudicated them. No capture program can produce that, which is
why it lives in the consumer rather than in the runs.
"""
import argparse
import ctypes
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from capture_run import CaptureRun, LABEL_DETECTED, LABEL_REQUESTED
from config import HUD_REGIONS, SCREEN_W, SCREEN_H
from detector.attachment_catalog import ATTACHMENTS
from detector.cropper import FocusLost, ScreenBuffer
from control.aim import ViewDriver, NoPico
from control.focus import game_focused

try:
    # The operator prompts and the scope labels are Chinese; a console left on
    # cp1252 would otherwise raise UnicodeEncodeError out of a print() and take
    # a capture run down with it, mid-scope.
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# Under docs/, not a scratch dir: these frames are the record the ADS detector
# gets designed against and every later revision gets re-checked against.
OUT_ROOT = os.path.join(ROOT, 'docs', 'ads', 'runs')

# ════════════════════════════════════════════════════════════
# What gets captured
# ════════════════════════════════════════════════════════════

# 'iron' is not an attachment — it is the empty scope slot, and it is the
# hardest negative in the set: the sight picture changes a lot while nothing
# is drawn over the screen. Everything else keys into ATTACHMENTS so the asset
# names used for verification have exactly one definition.
IRON = 'iron'

DEFAULT_SCOPES = [IRON, 'red_dot', 'holo', 'scope_2x', 'scope_3x', 'scope_4x',
                  'scope_6x', 'scope_8x']
# Fit a bolt-action too, but rarer than a sixth of a run's time is worth by
# default; 'variable' is the mixed-magnification sight, which behaves like two
# scopes in one and deserves its own pass.
EXTRA_SCOPES = ['scope_15x', 'variable']

MAGNIFICATION = {IRON: 1.0, 'red_dot': 1.0, 'holo': 1.0, 'scope_2x': 2.0,
                 'scope_3x': 3.0, 'scope_4x': 4.0, 'scope_6x': 6.0,
                 'scope_8x': 8.0, 'scope_15x': 15.0, 'variable': 0.0}

# ms after the right button goes down. 40 catches the very start of the
# animation, 400 is past every scope's settle, and everything at or beyond
# STEADY_MS is treated as steady-state by --report — so one view yields
# exactly one steady ADS frame, and --views is the count of "scoped in, this
# background" pictures the run ends up with per scope.
ADS_SAMPLE_MS = (40, 150, 400, 700)
STEADY_MS = 700
HIP_AFTER_MS = 1000      # after the second tap, for the un-scope transition

# Measured with --timing, not guessed, and measured twice because the first
# answer was only true of one sight:
#
#   iron (timing_20260802_012548)  in by 250 ms, out by 651 ms, plateau 42.5
#   8x   (timing_20260802_022122)  in by 545 ms, out by 815 ms, plateau 67.0
#
# The 8x does not just settle later, it *overshoots*: 90.7 at 383 ms, falling
# back to 66.6 by 630 ms and flat from there to 2.4 s. So the old 400 ms
# "steady" sample sat on the peak of a transient for every magnified scope —
# run 20260802_021631 reports 93.3 for the 8x, which is that overshoot rather
# than the 67 it settles to. 400 ms is kept as a transition sample precisely
# because that overshoot is distinctive; 700 ms is the one that is steady.

VIEWS = 10               # backgrounds per scope
VIEW_SEED = 20260801     # random, but the same random every time: meta.json
                         # records the offsets, so a frame can be put back
MAX_PITCH = 220          # counts. Small enough never to reach the ±90° clamp,
                         # which is what keeps the offsets reversible and lets
                         # the view be handed back where the operator left it.
YAW_STEP = (700, 2000)   # counts to keep turning right by, per view

VIEW_SETTLE = 0.35       # after a view change, before anything is captured
HIP_SETTLE = 0.20        # before the pre-press hip frame

TAP_MS = 70              # right button down-up. ADS is a toggle: it is the
                         # release that switches into the sight, and holding
                         # the button is hip aim, so this must be a real tap.
TAP_SETTLE = 0.04        # let the report land before timing anything from it

# One view's hip frame against its own hip_after frame, downsampled. Held
# deliberately below what --timing suggests: that arithmetic is midway between
# the floor and the plateau *of the sight it measured*, and the two sights
# disagree — 25 for iron (8.0 / 42.5), 37 for the 8x (6.1 / 67.0). Taking 37
# would leave a genuinely stuck iron sight only 5 above the line. 25 clears
# every measured noise floor (8.0, 6.1, and 9.3 worst case across the 80 views
# of run 20260802_021631) and sits under every measured plateau.
#
# 12.0 was the old value and it was inside the un-scope animation, not above
# it: at 350 ms the gun is still coming down and reads 15.6, so every view
# tripped the check, and the "correction" was another tap, which toggled the
# sight straight back up. A too-eager stuck check causes the exact corruption
# it exists to catch, so this threshold wants the noise floor well below it and
# the animation finished before it is ever evaluated.
SCOPE_STUCK_DIFF = 25.0

VK_ESCAPE = 0x1B
VK_F8 = 0x77             # not F9: config's dispatcher already owns that one


class StuckInScope(RuntimeError):
    """The ADS toggle would not come back off, so 'hip' can no longer be
    trusted to mean hip."""


class Aborted(RuntimeError):
    """The operator pressed Esc."""


def scope_info(key):
    """(zh label, expected AttachmentDetector asset, magnification)."""
    if key == IRON:
        return '机械瞄准', '', 1.0
    entry = ATTACHMENTS.get(key)
    if entry is None or entry['slot'] != 'scope':
        raise KeyError(f'{key!r} is not a scope in the attachment catalogue')
    return entry['zh'], entry['asset'] or '', MAGNIFICATION.get(key, 0.0)


def make_views(n, seed=VIEW_SEED):
    """n (yaw, pitch) offsets in mouse counts, from where the view started.

    Keep turning right by a random amount and look somewhere random-ish above
    or below the horizon: the scope is the same in every one of them and the
    scene is not, which is the whole basis of --report's median. A fixed table
    would work too, but eight hand-picked angles get memorised by whatever is
    fitted to them; a seed gives variety and reproducibility at once.

    View 0 is always (0, 0) — wherever the operator aimed before starting.
    """
    rng = np.random.default_rng(seed)
    out, yaw = [(0, 0)], 0
    while len(out) < n:
        yaw += int(rng.integers(*YAW_STEP))
        out.append((yaw, int(rng.integers(-MAX_PITCH, MAX_PITCH + 1))))
    return out


def parse_scopes(spec):
    if not spec or spec == 'default':
        return list(DEFAULT_SCOPES)
    if spec == 'all':
        return DEFAULT_SCOPES + EXTRA_SCOPES
    keys = [s.strip() for s in spec.split(',') if s.strip()]
    for k in keys:
        scope_info(k)     # raises on anything that is not a scope
    return keys


# ════════════════════════════════════════════════════════════
# Screen / operator
# ════════════════════════════════════════════════════════════

# One whole-screen source for the run, opened once. Whole-screen is a NAMED
# branch of ScreenBuffer, not an incidental optimisation: with no region set
# full() hands back a FRESH array every call, and this file depends on that.
# capture_scope keeps its `hip` frame to diff later frames against, and _watch
# keeps decimated copies; a shared buffer would quietly turn all of them into
# the same picture. Adding a second region here would take the fast path away
# and alias them.
_FRAMES = ScreenBuffer(None, focus_fn=game_focused)


def grab():
    """Full screen, refusing to hand back a frame the game did not draw.

    Every frame in the set is worthless the moment the game loses focus — the
    scene freezes, ADS never engages, and the run would otherwise complete and
    label a folder of identical desktop screenshots 'ads'. ScreenBuffer's
    focus_fn raises FocusLost rather than handing the frozen picture back.
    """
    return _FRAMES.full()


def looks_black(img, thresh=6):
    """GDI hands back a black frame under exclusive fullscreen on some setups."""
    return float(img[::8, ::8].mean()) < thresh


def key_down(vk):
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def wait_for_key(vk=VK_F8, label='F8', prompt=''):
    """Block until the operator presses `vk`, or Esc to abort the run.

    Polled rather than read from stdin so the operator never has to alt-tab:
    leaving the game would drop focus, and the first thing capture does after
    this returns is take a frame.
    """
    if prompt:
        print(prompt, flush=True)
    print(f'    [{label}] 继续，[Esc] 中止 ...', flush=True)
    while key_down(vk):       # ignore a key still held from the last prompt
        time.sleep(0.02)
    while True:
        if key_down(VK_ESCAPE):
            raise Aborted('aborted by operator')
        if key_down(vk):
            while key_down(vk):
                time.sleep(0.02)
            return
        time.sleep(0.02)


# ════════════════════════════════════════════════════════════
# Which slot the gun is in
# ════════════════════════════════════════════════════════════

# --weapon names that are not the HUD icon's own key.
WEAPON_ICON_ALIAS = {'kar98k': '98k', 'kar98': '98k', 'k98': '98k'}

# Which slot is lit is the reliable reading, and it is also the one that
# matters: the frames photograph whatever is in the hands. Measured p95 of
# dewhite over the HUD slot, two runs: 151 vs 72, and 119 vs 52.
HUD_HL_MARGIN = 15.0

# The silhouette match is the weaker signal and is only allowed to override
# the held slot when it is emphatic. It correlates the icon's alpha against
# the dewhited HUD, which works (a Kar98k scored 0.90 against its own
# template) until the HUD sits on a bright background, where the scene's
# texture leaks through and the same gun drops to 0.48. Worse, matchTemplate
# takes a max over positions, so a short template finds some sub-region to fit
# and wins: on that frame an s686 shotgun scored 0.82 against a Kar98k. Making
# this trustworthy needs length-aware scoring and is a separate job.
HUD_MATCH_STRONG = 0.75  # correlation with the weapon's own silhouette
HUD_MATCH_MARGIN = 0.12  # and how far it has to beat the other slot
SLOT_SWITCH_WAIT = 0.9   # the swap animation, before the HUD is read back

_icon_cache = {}


class SlotNotFound(RuntimeError):
    """Neither HUD weapon slot looks like the weapon that was asked for."""


def _hud_reader():
    """The shared WeaponHudDetector. Built once; the bank is 2.9 MB."""
    if 'det' not in _icon_cache:
        from detector.weapon_hud_detector import WeaponHudDetector
        _icon_cache['det'] = WeaponHudDetector()
    return _icon_cache['det']


def _known(icon_key):
    """Can the HUD reader answer about this weapon at all?

    It only knows weapons it has reference captures of, so this is a real
    question and not a formality -- dbs, o12 and win94 are on the roster with
    no frames at all.
    """
    det = _hud_reader()
    return det.ready and icon_key in det.codes


def _hud_slot(frame, slot):
    y, x, h, w = HUD_REGIONS[f'weapon_{slot}']
    return frame[y:y + h, x:x + w]



def hud_match(frame, icon_key):
    """How well each HUD weapon slot matches one weapon. -> {1: s, 2: s}

    Cosine to that weapon's nearest reference capture, via the shared
    WeaponHudDetector. It used to correlate against the game's extracted art
    instead; that art is the INPUT to the game's compositing and this code
    only ever sees the output, which measured 0.489 against 0.975 for real
    captures. Higher is better either way, so callers did not change.
    """
    det = _hud_reader()
    if not _known(icon_key):
        raise SlotNotFound(f'no HUD reference captures for {icon_key!r}')
    return {slot: det.scores(_hud_slot(frame, slot)).get(icon_key, 0.0)
            for slot in (1, 2)}


def hud_highlight(frame):
    """How lit each HUD weapon slot is. -> {1: s, 2: s}; higher is in hand."""
    from dl_models.icon_merging import dewhite
    return {slot: float(np.percentile(dewhite(_hud_slot(frame, slot)), 95))
            for slot in (1, 2)}


def tab_weapon_names(frame):
    """{1: key, 2: key} off the Tab screen's two name plates, None if unread."""
    from detector.weapon_template_detector import TabWeaponDetector
    crops = {}
    for g in (1, 2):
        y, x, h, w = HUD_REGIONS[f'gun_name_{g}']
        crops[f'gun_name_{g}'] = frame[y:y + h, x:x + w]
    names = TabWeaponDetector().classify(crops)
    return {g: (n or None) for g, n in zip((1, 2), names)}


def dump_slot_debug(frame, out_dir, top=4):
    """Write what the HUD weapon slots look like, and what they matched.

    A slot read that fails is the one failure with nothing to look at
    afterwards — the run stops before a single frame is saved, and the numbers
    alone ("slot1 0.47, slot2 0.31") do not say whether the gun was stowed,
    swapped, or simply drawn differently than the template expects.
    """
    os.makedirs(out_dir, exist_ok=True)
    save(frame, os.path.join(out_dir, 'slot_debug_full.jpg'))
    tiles = []
    for slot in (1, 2):
        crop = _hud_slot(frame, slot)
        tile = cv2.resize(crop, (crop.shape[1] * 3, crop.shape[0] * 3),
                          interpolation=cv2.INTER_NEAREST)
        cv2.putText(tile, f'weapon_{slot}', (8, 24), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (0, 255, 255), 2)
        tiles.append(tile)
    path = os.path.join(out_dir, 'slot_debug.jpg')
    cv2.imwrite(path, np.vstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 95])

    keys = sorted(_hud_reader().codes) if _hud_reader().ready else []
    for slot in (1, 2):
        ranked = sorted(((hud_match(frame, k)[slot], k) for k in keys),
                        reverse=True)
        print(f'[slot] weapon_{slot} looks most like: '
              + ', '.join(f'{k} {s:.2f}' for s, k in ranked[:top]))
    print(f'[slot] wrote {path}')
    return path


def find_weapon_slot(frame, weapon):
    """Which weapon slot holds `weapon`. -> (slot, scores)

    The operator used to be told to put the gun in slot 1, and a run where
    they did not silently worked on the other gun: 20260802_015545 stripped an
    AUG's scope and then captured 40 frames of the Kar98k still wearing its
    own sight, every one of them labelled 'iron'. Nothing in the run could
    notice, because the slot was an assumption rather than a reading.
    """
    hl = hud_highlight(frame)
    held = max(hl, key=hl.get)
    lit = ', '.join(f'slot{s} {hl[s]:.0f}' for s in (1, 2))

    key = WEAPON_ICON_ALIAS.get(weapon, weapon)
    try:
        scores = hud_match(frame, key)
    except SlotNotFound:
        scores = None

    # A stowed gun can only be found by its silhouette, so that reading is
    # still worth having — but it takes an emphatic one to send the run at a
    # weapon the player is not holding.
    if scores is not None:
        best = max(scores, key=scores.get)
        seen = ', '.join(f'slot{s} {scores[s]:.2f}' for s in (1, 2))
        if (scores[best] >= HUD_MATCH_STRONG
                and scores[best] - scores[3 - best] >= HUD_MATCH_MARGIN):
            return best, f'silhouette ({seen})'
    else:
        seen = f'no {key} template'

    if hl[held] - hl[3 - held] > HUD_HL_MARGIN:
        return held, f'in hand ({lit}); silhouette inconclusive ({seen})'

    raise SlotNotFound(
        f'cannot tell which slot to work on. Lit: {lit}. Looks like {weapon}: '
        f'{seen}. Hold the gun you want captured, or pass --slot 1 / --slot 2.')


def hold_weapon(inv, slot):
    """Bring weapon `slot` into the hands, and read the HUD back to check.

    Every ADS frame is a picture of whatever is in the hands, so this is not a
    convenience — capturing while the other gun is held produces a directory
    named after a scope that was fitted to a weapon nobody photographed.

    THE KEYPRESS IS InventoryControl's; THE VERDICT IS THIS FILE'S, and that
    split is deliberate because the two tests are NOT the same test.
    InventoryControl.hold() presses 1/2, sleeps the swap animation and returns
    True having read nothing back — and it short-circuits on a CACHED
    `self.held`. That is defensible where it lives: a wrong gun there means the
    next drag lands on the wrong weapon and the slot readback catches it one
    step later. Here nothing downstream would ever notice, so the HUD
    highlight stays the verdict, before and after.

    What was worth taking from InventoryControl is the part this file had no
    answer for: 1 and 2 are SWALLOWED while the inventory is up, so the press
    has to be bracketed by a Tab close and reopen, and ensure_tab() re-presses
    a Tab the game ate (docs/game_quirks.md). The old code just pressed the key
    and hoped the screen happened to be down.
    """
    hl = hud_highlight(grab())
    if hl[slot] - hl[3 - slot] > HUD_HL_MARGIN:
        return True                       # already in hand
    if inv is None or not inv.can_press():
        print(f'[slot] no Pico to press {slot} — switch to it by hand')
        return False
    # settle=SLOT_SWITCH_WAIT rather than its 0.6 default: that constant is
    # this file's measurement of the swap animation, and the HUD is read the
    # instant this returns.
    inv.hold(slot, settle=SLOT_SWITCH_WAIT)
    hl = hud_highlight(grab())
    ok = hl[slot] - hl[3 - slot] > HUD_HL_MARGIN
    if not ok:
        print(f'[slot] pressed {slot} but the HUD still lights slot '
              f'{max(hl, key=hl.get)} '
              f'({", ".join(f"{s}:{hl[s]:.0f}" for s in (1, 2))})')
    return ok


# ════════════════════════════════════════════════════════════
# View rotation and the ADS tap
# ════════════════════════════════════════════════════════════

class Aimer:
    """Swings the view between takes, and taps the ADS toggle. Nothing else.

    Both jobs are OPEN LOOP and both are meant to be. The view is moved to put
    a DIFFERENT SCENE behind the sight, not to arrive anywhere provable, so
    ViewDriver's closed-loop methods would fight it rather than help; and the
    ADS tap cannot be verified by the thing that verifies it everywhere else
    (control/gun.py's ensure_ads) because this run is what that detector gets
    fitted to. control/aim.py names both exceptions and carries the reasons.

    It used to also switch weapons and press Tab, with its own Pico handle and
    its own SendInput fallback wired straight to ctypes. Those are
    InventoryControl's — see hold_weapon() for the one part of the weapon
    switch that stayed here, and why.
    """

    def __init__(self, backend='auto'):
        # This is where the Pico gets opened for the whole run, retries and
        # all (press/pointer.py's Pointer.opened). Everything built after it —
        # InventoryControl included — goes through get_mouse()'s singleton and
        # finds the port already up, so the retry only ever has to happen once
        # and it happens before the operator is asked to switch to the game.
        self.view = ViewDriver.open_loop(backend, _FRAMES)
        self.backend = self.view.backend
        if self.backend != 'pico':
            print('[aim] WARNING: no Pico, and --backend sendinput says to go '
                  'anyway. PUBG reads raw input and will very likely ignore '
                  'this — check the first ADS frame before trusting a run.')
        self.yaw = 0
        self.pitch = 0

    # ── View ──

    def look_at(self, yaw, pitch, settle=VIEW_SETTLE):
        """Go to a view offset (in counts) relative to where the run started.

        The running total is this file's, not ViewDriver's: turn() deliberately
        does not track where it left the view, and what is wanted here is only
        "somewhere else, reproducibly, and hand it back at the end" — see
        make_views(). Nothing measures against these numbers.
        """
        dyaw, dpitch = yaw - self.yaw, pitch - self.pitch
        if dyaw or dpitch:
            self.view.turn(dyaw, dpitch)
        self.yaw, self.pitch = yaw, pitch
        time.sleep(settle)

    def recentre(self):
        """Hand the view back roughly where the operator left it."""
        self.look_at(0, 0, settle=0.1)

    # ── ADS ──

    def tap(self):
        """Press and release the right button — the gesture that toggles ADS."""
        self.view.ads_tap(TAP_MS, TAP_SETTLE)

    def ads_burst(self, sample_ms):
        """Tap into the sight, sample, tap back out. -> [(want_ms, got_ms, img)]

        ADS here is a toggle, not a hold: holding the right button down is
        hip/shoulder aim and the sight picture never appears. Run
        20260801_222936 is 64 frames of exactly that mistake — iron sights and
        a red dot came out with hip-to-"ADS" differences of 31.45 and 31.48,
        because what was being measured was the gun coming up, not a scope.
        Releasing is what switches into the sight, so the gesture is a tap.

        Which makes the *second* tap the thing that matters. Leave it out and
        the next view starts already scoped in, its 'hip' frame is a lie, and
        nothing downstream can tell. capture_scope() checks it landed.

        got_ms is stamped before the grab, not after: the frame is the screen
        at the moment BitBlt starts, and the copy itself costs ~70 ms at
        3440x1440 — enough to move a 40 ms sample to 110 ms if measured after.
        """
        out = []
        t0 = time.perf_counter()
        self.tap()
        try:
            for ms in sample_ms:
                target = t0 + ms / 1000.0
                while True:
                    now = time.perf_counter()
                    if now >= target - 0.003:
                        break
                    time.sleep(min(0.003, target - now))
                shot = time.perf_counter()
                out.append((ms, (shot - t0) * 1000.0, grab()))
        finally:
            self.tap()
        return out


# ════════════════════════════════════════════════════════════
# Equipping the scope — the other agents' half
#
# An equipper is called once per scope and answers two things:
#
#     ok        True   the gun is wearing what was asked for
#               False  the drag happened but could not be confirmed
#               None   nothing was even attempted — the scope is not in the
#                      bag, or the Tab screen never came up
#     verified  the asset name the scope slot actually read, or None when
#               nobody looked. run() compares it against the catalogue, so
#               both paths get the same mismatch handling.
#
# False and None are kept apart because they deserve opposite treatment: a
# drag that went through but timed out on verification still leaves the right
# scope on the gun most of the time, and those frames are worth having with
# the slot reading recorded next to them. A scope that is not in the backpack
# leaves the *previous* one fitted, and capturing that would mislabel it.
#
# Whoever opens the Tab screen must close it again: capture starts the moment
# this returns.
# ════════════════════════════════════════════════════════════

class ManualEquip:
    """Stop and let the operator fit the scope, in the game, by hand."""

    name = 'manual'
    self_verifies = False       # run() falls back to ScopeVerifier

    # DECLARED ABSENT, not simply missing. run() used to ask `hasattr(equip,
    # 'locate')` and friends, so what this strategy cannot do was expressed by
    # three attributes failing to exist — invisible from here, which is the
    # only place a reader looks to find out what a strategy is. The file
    # already had the better idiom one line up: `self_verifies` states a
    # capability on the class. These do the same.
    #
    # None rather than a no-op method, because run() must be able to tell
    # "there is no Tab-reading step for this strategy" from "the step ran and
    # answered nothing" — preflight() returning None already means the second
    # thing, and printing "preflight could not read the Tab screen" for a
    # manual run would be a false report of a failure.
    locate = None               # cannot read the Tab name plate
    preflight = None            # cannot inventory the bag
    gun = None                  # nothing to aim at a rack slot

    def __call__(self, key):
        zh, _, _ = scope_info(key)
        what = ('把瞄准镜卸下来（机械瞄准）' if key == IRON
                else f'装上 {zh} ({key})')
        wait_for_key(prompt=f'\n>>> {what}，关掉 Tab，回到游戏画面。')
        return True, None

    def close(self):
        pass


class AttachEquip:
    """Fit the scope with control/inventory.py, on the Tab screen.

    One call is: open Tab -> find the scope in 库存/地面 -> drag it onto the
    gun -> read the slot back -> close Tab. Dropping a scope onto an occupied
    slot is a swap, and the displaced one lands back in 库存 as a new row, so
    the eight scopes cycle through the same slot without any cleanup between
    them — the only reason this re-detects (`look()`) on every call.

    InventoryControl verifies its own drags against the exact template, which is
    the same read ScopeVerifier would make afterwards, so it hands the answer
    straight back rather than paying for a second Tab cycle.
    """

    name = 'inventory'
    self_verifies = True

    def __init__(self, inv, gun=2):
        if inv is None:
            raise RuntimeError('control/inventory.py did not load')
        if not inv.can_press():
            raise RuntimeError('no Pico: nothing can press Tab')
        self.ac = inv
        self.gun = gun

    def close(self):
        # The InventoryControl is the run's, not this object's — run() closes
        # it. ScopeVerifier and hold_weapon() share the same one.
        pass

    # ── Tab ──

    def _on_tab(self, fn, *a):
        """Run fn with the Tab screen up, and put it away again afterwards.

        ensure_tab rather than three blind presses with a sleep each: a Tab
        sent right after the previous toggle is sometimes SWALLOWED
        (docs/game_quirks.md), and the old loop here read that as "the screen
        would not open" and gave up on the scope. It re-presses instead, and
        it decides with tab_open() — a 41x18 pixel check with an offline
        regression behind it (pixi run tab-open) — rather than by sleeping a
        fixed 0.55 s and hoping.

        Not InventoryControl.tab_up(): that restores the screen to how it was
        FOUND, and the equipper contract above says the Tab screen must be
        down when this returns, whatever state it was in. Capture starts
        immediately, and a run photographed through an open inventory is a run
        of the inventory.
        """
        if not self.ac.ensure_tab(True):
            print('[equip] Tab would not open')
            return None
        try:
            return fn(*a)
        finally:
            if not self.ac.ensure_tab(False):
                print('[equip] Tab would not close')

    # ── The two operations ──

    def preflight(self, keys):
        """Which of `keys` are nowhere to be found. [] when all are present.

        Worth one Tab cycle before the run: a scope that is not in the bag
        fails at its turn, four scopes and several minutes in.
        """
        def look():
            if not self.ac.sync():
                return None
            view = self.ac.look()
            worn = self._worn(view)
            return [k for k in keys if k != IRON
                    and view.find(k) is None
                    and scope_info(k)[1] != worn]
        return self._on_tab(look)

    def locate(self, weapon):
        """Which slot holds `weapon`, read off the Tab screen's name plates.

        The name plate is the real answer to this question: it is rendered
        text with a template per weapon, against the Tab screen's flat panel
        rather than the game world. InventoryControl already reads it, but
        _read_guns() drops anything outside ROSTER — and ROSTER came from the
        spawner, which only ever listed AR/DMR/SMG/LMG, so every bolt-action
        comes back None ("gun1 is unnamed" in run 20260801_222936). The
        classifier itself has a 98k template and always did, so this asks it
        directly and skips the roster gate.
        """
        want = WEAPON_ICON_ALIAS.get(weapon, weapon)

        def read():
            names = tab_weapon_names(self.ac._frame())
            hits = [g for g in (1, 2) if names[g] == want]
            print(f'[slot] Tab name plates: '
                  + ', '.join(f'gun{g} {names[g] or "?"}' for g in (1, 2)))
            return hits[0] if len(hits) == 1 else None
        return self._on_tab(read)

    def _worn(self, view):
        """The asset in the gun's scope slot, '' when it is empty."""
        item = view.equipped(self.gun, 'scope')
        return item.asset if item is not None else ''

    def __call__(self, key):
        return self._on_tab(self._fit, key) or (None, None)

    def _fit(self, key):
        """-> (ok, verified). See the equipper contract above for the tri-state.

        The diff, the drags and the readback are InventoryControl's; the tri-
        state is this file's, and it is the part worth preserving. `None` means
        NOTHING WAS ATTEMPTED, which leaves the PREVIOUS scope on the gun, and
        capturing that would file a folder of frames under a scope the weapon
        was not wearing. `False` means a drag went out and could not be
        confirmed, and those frames are worth keeping with the readback
        recorded next to them.
        """
        ac = self.ac
        if not ac.sync():
            return None, None
        _, want, _ = scope_info(key)
        # look() also names the guns, which narrows the template bank the slot
        # is read with; ensure_kit reuses the pass rather than taking its own.
        view = ac.look()
        worn = self._worn(view)

        rec = ac.ensure_kit(self.gun, {'scope': None if key == IRON else key},
                            look=lambda: view)
        got = (rec['worn'] or {}).get('scope', '')
        if rec['ok']:
            # No icon template for this one (混合瞄具, iron sights): the slot
            # can only be checked for having CHANGED, so reporting `got` would
            # invite a comparison against '' that means nothing.
            return True, (got if (want or key == IRON) else None)
        if rec['missing'] or not any(s.get('attempts') for s in rec['steps']):
            # Nothing was even tried: the scope is not in the bag, or the plan
            # refused it. The gun still wears whatever it wore.
            print(f'[equip] {key} is not in 库存 or on the ground')
            return None, worn
        return False, (got if want else None)


def open_inventory(backend='auto'):
    """One InventoryControl for the whole run, or None if it will not build.

    Shared rather than one per user, and there are three: AttachEquip drives
    the drags, ScopeVerifier borrows it to open Tab, and hold_weapon() borrows
    it to press 1/2. Three of them would mean three TabGrabbers and three
    template banks for one screen — and, worse, three independent beliefs
    about whether the inventory is currently up.

    Built AFTER Aimer on purpose: Aimer is what pays the Pico retry, and
    get_mouse() is a singleton, so by the time this runs the port is either
    open or the run has already stopped.
    """
    try:
        from control.inventory import InventoryControl
        return InventoryControl(backend=backend)
    except Exception as e:
        print(f'[equip] control/inventory.py unavailable ({e})')
        return None


def make_equipper(spec, inv, gun):
    if spec == 'manual':
        return ManualEquip()
    try:
        eq = AttachEquip(inv, gun)
        print('[equip] using control/inventory.py')
        return eq
    except Exception as e:
        if spec == 'attach':
            raise
        print(f'[equip] control/inventory.py unavailable ({e}); falling back to '
              f'manual')
        return ManualEquip()


# ════════════════════════════════════════════════════════════
# Verifying the scope actually went on
# ════════════════════════════════════════════════════════════

class ScopeVerifier:
    """Reads the weapon's scope slot off the Tab screen.

    Cheap insurance against the whole failure mode this dataset cannot survive:
    a mislabelled scope. Without it a failed drag produces twenty frames of a
    4x sitting in a directory called scope_6x, and every later evaluation is
    quietly wrong.

    Only reached with --equip manual: the inventory equipper reads the slot
    back itself, from the same detector, on the Tab cycle it already paid for.
    """

    def __init__(self, inv, weapon_slot=2):
        from detector.attachment_detector import AttachmentDetector
        self.det = AttachmentDetector()
        self.region = HUD_REGIONS[f'att_{weapon_slot}_scope']
        self.slot = weapon_slot
        self.inv = inv
        can_press = inv is not None and inv.can_press()
        self.ready = bool(self.det._templates) and can_press
        if not self.det._templates:
            print('[verify] no attachment templates on disk — disabled')
        elif not can_press:
            print('[verify] no Pico, cannot press Tab — disabled')

    def read(self):
        """Open Tab, classify the scope slot, close Tab. -> asset name or None.

        None means the Tab screen never came up, which must not be reported as
        an empty slot: '' is a legitimate answer (iron sights) and the two
        would be indistinguishable.

        "Did it come up" used to be a whole-screen absdiff against a before
        frame, thresholded at 12.0 — which answers "did the picture change",
        not "is the inventory up". The training range is not a still life, and
        a Tab the game swallowed left the run reading a slot off the world.
        ensure_tab() re-presses a swallowed key and decides on tab_open()'s
        pixel check instead.
        """
        if not self.inv.ensure_tab(True):
            print('[verify] Tab would not open')
            return None
        try:
            y, x, h, w = self.region
            frame = grab()
            # classify_crop, not the old private _classify_slot: same metric,
            # plus the is-anything-drawn gate. An empty scope slot used to be
            # answered with whatever template came closest, and this verifier
            # exists precisely to catch a scope that never went on.
            return self.det.classify_crop(frame[y:y + h, x:x + w], 'scope')
        finally:
            if not self.inv.ensure_tab(False):
                print('[verify] Tab would not close')

    def check(self, key, retries=1):
        """What the scope slot reads as, or None when nobody could look.

        None and '' are deliberately different answers: '' is iron sights,
        None is "the Tab screen never came up".
        """
        if not self.ready:
            return None
        _, want, _ = scope_info(key)
        if want == '' and key != IRON:
            # No template for this one in the catalogue. Expecting '' would
            # make an empty slot — i.e. the drag never happened — read as a
            # pass, which is worse than not checking at all.
            print(f'[verify] {key} has no template asset, cannot check')
            return None
        for _ in range(retries + 1):
            got = self.read()
            if got is not None:
                return got
        return None


def scope_label(key, verified, operator):
    """What is KNOWN about the scope on the gun. -> ([label], why)

    Three outcomes, and the third one is the point. capture_run.py's rule is
    that a label exists only when someone looked, and `source` says who; an
    unverified intention gets no label at all, because a label that records an
    intention is wrong exactly when the action silently failed. That failure is
    not hypothetical here — run 20260802_015545 stripped an AUG's scope and
    then filed 40 frames of a Kar98k still wearing its own sight under `iron`.

      readback agrees with what was asked for   REQUESTED. The scope key came
                                                from --scopes, the drag was
                                                aimed at that named part, and
                                                the slot was read back. Asked
                                                and confirmed.

      readback disagrees                        DETECTED, carrying what was
                                                READ, not what was wanted. The
                                                run captures anyway (--strict
                                                stops instead), so the frames
                                                have to say what is on the gun
                                                rather than what was ordered.

      nobody read the slot back                 no label. Covers --equip manual
                                                with the verifier off, and the
                                                sights with no catalogue asset
                                                (变倍镜) where '' would be
                                                indistinguishable from an empty
                                                slot.

    `operator` is the one exception, and it is a deliberate one: with --equip
    manual a human fitted the scope, looked at the screen and pressed F8. That
    is a reading by the one detector in the building that cannot be circular
    with any template, so it is REQUESTED — but tagged `by: operator`, because
    its failure mode (a person continuing without doing it) is real and differs
    from the machine's.

    `verified` is the asset the scope slot read back, '' for a confirmed empty
    slot (iron sights), None when nobody looked. The three are all different
    answers and collapsing '' into None is what would make a failed drag read
    as a pass.
    """
    _, want, _ = scope_info(key)
    if verified is None:
        if operator:
            return ([{'slot': 'scope', 'asset': want, 'source': LABEL_REQUESTED,
                      'by': 'operator'}], 'operator fitted it by hand')
        return [], 'nobody read the slot back'
    if verified == want:
        return ([{'slot': 'scope', 'asset': want, 'source': LABEL_REQUESTED,
                  'by': 'slot readback'}], 'asked for it and read it back')
    return ([{'slot': 'scope', 'asset': verified, 'source': LABEL_DETECTED,
              'by': 'slot readback'}],
            f'wanted {want or "<empty>"}, the slot reads '
            f'{verified or "<empty>"}')


# ════════════════════════════════════════════════════════════
# Capture
# ════════════════════════════════════════════════════════════

def save(img, path, png=False, quality=95):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if png:
        cv2.imwrite(path, img)
    else:
        cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, quality])


def probe_sheet(run):
    """hip beside the steady ADS frame, quarter size, for a human to check.

    The one thing no assertion in this file can make: that the right button
    actually put the sight in front of the camera. Which is also why `state`
    is a fact and not a label — this picture, read by a person, is the only
    thing that settles it.
    """
    files = {e['state']: e['capture'] for e in run.entries if 'state' in e}
    hip, ads = files.get('hip'), files.get('ads')
    if not (hip and ads):
        return None
    a = cv2.imread(os.path.join(run.path, hip))
    b = cv2.imread(os.path.join(run.path, ads))
    if a is None or b is None:
        return None
    diff = float(cv2.absdiff(a, b).mean())
    pair = np.hstack([a, b])
    pair = cv2.resize(pair, (pair.shape[1] // 4, pair.shape[0] // 4))
    cv2.putText(pair, f'left: hip   right: ADS   diff {diff:.1f}', (14, 34),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)
    path = os.path.join(run.path, 'probe.jpg')
    cv2.imwrite(path, pair, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return path


def _diff(img, ref):
    """Mean absolute difference at 1/8 res — cheap and plenty sensitive."""
    return float(cv2.absdiff(img[::8, ::8], ref[::8, ::8]).mean())


def _watch(aimer, ref, ms):
    """Tap, then sample |frame - ref| as fast as the grabber allows.

    -> [(t_ms since the tap, diff, quarter-res frame)]
    """
    out = []
    t0 = time.perf_counter()
    aimer.tap()
    while True:
        t = (time.perf_counter() - t0) * 1000.0
        if t > ms:
            return out
        img = grab()
        out.append((t, _diff(img, ref), img[::4, ::4].copy()))


def _settle(curve, tol=0.08, floor_tol=1.5):
    """When the curve stops moving. -> (t_ms, plateau value)

    The plateau is the median of the last three samples; settle is the first
    sample that is within tolerance of it and stays there.
    """
    if len(curve) < 4:
        return None, None
    plateau = float(np.median([d for _, d, _ in curve[-3:]]))
    band = max(floor_tol, tol * plateau)
    for i, (t, d, _) in enumerate(curve):
        if all(abs(dd - plateau) <= band for _, dd, _ in curve[i:]):
            return t, plateau
    return curve[-1][0], plateau


def _strip(curve, path, n=12):
    """A row of evenly spaced frames, each labelled with its time."""
    if not curve:
        return None
    idx = np.unique(np.linspace(0, len(curve) - 1, min(n, len(curve))).astype(int))
    tiles = []
    for i in idx:
        t, d, img = curve[i]
        tile = cv2.resize(img, (img.shape[1] // 2, img.shape[0] // 2))
        cv2.putText(tile, f'{t:.0f}ms d={d:.1f}', (8, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
        tiles.append(tile)
    cv2.imwrite(path, np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 88])
    return path


def timing_run(args):
    """Measure how long the ADS toggle really takes, in and out.

    STEADY_MS and HIP_AFTER_MS were guesses, and for a bolt-action both were
    too short. 400 ms catches the gun still swinging up. Worse, a hip_after
    grabbed at 350 ms catches it still coming down, which reads as "still
    scoped" — so the stuck check taps again and puts the sight back up. That is
    precisely the failure the check exists to prevent, caused by the check.

    Tap in, grab flat out until the picture stops changing; tap out, grab
    again. Both curves are scored against the hip frame the sequence started
    from, so one rises to a plateau and the other falls back to the scene's own
    noise floor. Where each flattens is the number to use; the gap between
    plateau and floor is what SCOPE_STUCK_DIFF has to sit inside.
    """
    aimer = Aimer(args.backend)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(OUT_ROOT, f'timing_{stamp}')
    os.makedirs(out_dir, exist_ok=True)

    print(f'weapon   {args.weapon}, whatever sight is on it now')
    print(f'watching {args.timing_ms} ms either side of the tap')
    print(f'out      {out_dir}\n')
    wait_for_key(prompt=f'>>> 切到游戏，{args.weapon} 拿在手上，对着有内容的'
                        f'方向站定别动。\n    程序会点一次右键开镜、再点一次'
                        f'退出，全程连拍。')

    base = grab()
    if looks_black(base):
        print('[timing] the grab is black — switch the game to borderless '
              'windowed and try again.')
        return 1

    try:
        up = _watch(aimer, base, args.timing_ms)
        down = _watch(aimer, base, args.timing_ms)
    except (FocusLost, Aborted) as e:
        print(f'\n!! {e}')
        return 1

    t_up, plateau = _settle(up)
    t_down, floor = _settle(down)

    print(f'\n{"":>8}  {"进镜 (vs hip)":<16}  {"退镜 (vs hip)":<16}')
    for i in range(max(len(up), len(down))):
        a = f'{up[i][0]:6.0f} {up[i][1]:6.1f}' if i < len(up) else ''
        b = f'{down[i][0]:6.0f} {down[i][1]:6.1f}' if i < len(down) else ''
        print(f'{i:>8}  {a:<16}  {b:<16}')

    print(f'\n进镜 settles at {t_up:.0f} ms, plateau {plateau:.1f}')
    print(f'退镜 settles at {t_down:.0f} ms, floor {floor:.1f}  '
          f'(scene noise: grass, clouds, the HUD clock)')
    if plateau and floor is not None and plateau > floor:
        print(f'\nsuggested:  STEADY_MS {_round50(t_up + 150)}   '
              f'HIP_AFTER_MS {_round50(t_down + 150)}   '
              f'SCOPE_STUCK_DIFF {(plateau + floor) / 2:.0f} '
              f'(midway between {floor:.1f} and {plateau:.1f})')
    else:
        print('\n!! the ADS curve never rose above the noise — the tap did not '
              'scope in. Check timing_in.jpg before changing any constant.')

    for name, curve in (('timing_in.jpg', up), ('timing_out.jpg', down)):
        _strip(curve, os.path.join(out_dir, name))
    with open(os.path.join(out_dir, 'timing.json'), 'w', encoding='utf-8') as f:
        json.dump(dict(stamp=stamp, weapon=args.weapon, watch_ms=args.timing_ms,
                       backend=aimer.backend,
                       ads_settle_ms=t_up, ads_plateau=plateau,
                       hip_settle_ms=t_down, hip_floor=floor,
                       ads=[[round(t, 1), round(d, 2)] for t, d, _ in up],
                       hip=[[round(t, 1), round(d, 2)] for t, d, _ in down]),
                  f, ensure_ascii=False, indent=2)

    # Two taps is an even number, so the sight should be down again. Say so
    # either way — the operator is about to take the mouse back.
    rest = _diff(grab(), base)
    print(f'\nback at hip: diff {rest:.1f}' if rest < max(6.0, floor * 2) else
          f'\n!! still {rest:.1f} away from hip — the gun may be left scoped in')
    print(f'frames -> {out_dir}')
    return 0


def _round50(ms):
    return int(round(ms / 50.0) * 50)


def capture_scope(key, aimer, run, views, sample_ms, png,
                  weapon, slot, verified, labels=()):
    """One scope, every view: hip -> ADS burst -> hip after the release.

    `labels` is scope_label()'s verdict, repeated on every frame of the scope
    because that is what it describes — the gun wore the same sight for all of
    them. `state` / `t_ms` go in as FACTS beside it: what the procedure did,
    which is knowable, rather than what the sight showed, which is not.
    """
    zh, asset, mag = scope_info(key)
    ext = 'png' if png else 'jpg'
    n = 0

    for vi, (yaw, pitch) in enumerate(views):
        aimer.look_at(yaw, pitch)

        common = dict(scope=key, zh=zh, mag=mag, view=vi, yaw=yaw, pitch=pitch,
                      weapon=weapon, slot=slot, verified=verified,
                      labels=list(labels))

        time.sleep(HIP_SETTLE)
        hip = grab()
        run.add(hip, f'{key}/hip_v{vi}_t0000.{ext}',
                state='hip', t_ms=0, t_actual_ms=0.0, **common)
        n += 1

        for want_ms, got_ms, img in aimer.ads_burst(sample_ms):
            run.add(img, f'{key}/ads_v{vi}_t{want_ms:04d}.{ext}',
                    state='ads', t_ms=want_ms, t_actual_ms=round(got_ms, 1),
                    steady=want_ms >= STEADY_MS, **common)
            n += 1

        time.sleep(HIP_AFTER_MS / 1000.0)
        after = grab()
        # Did the second tap come out of the sight? The view has not moved, so
        # this frame and the hip frame should be the same scene. If they are
        # not, the toggle is stuck on, and every frame from here would be
        # scoped in and labelled hip. Untangle it before the next view.
        stuck = _diff(after, hip)
        run.add(after, f'{key}/hipafter_v{vi}_t{HIP_AFTER_MS:04d}.{ext}',
                state='hip_after', t_ms=HIP_AFTER_MS,
                t_actual_ms=float(HIP_AFTER_MS), stuck_diff=round(stuck, 2),
                **common)
        n += 1

        # A corrective tap is itself a toggle, so it cannot be fired and
        # forgotten: if it was the wrong call the sight goes back up and every
        # frame after this is scoped in and labelled hip. Re-read after each
        # one, and if two do not bring the view back, stop the run — a short
        # set is recoverable, a set with mislabelled frames in it is not.
        for attempt in range(2):
            if stuck <= SCOPE_STUCK_DIFF:
                break
            print(f'[capture] {key} v{vi}: still scoped in after the second '
                  f'tap (diff {stuck:.1f}) — tapping again')
            aimer.tap()
            time.sleep(HIP_AFTER_MS / 1000.0)
            stuck = _diff(grab(), hip)
        else:
            raise StuckInScope(
                f'{key} v{vi}: {stuck:.1f} away from hip after two corrective '
                f'taps (threshold {SCOPE_STUCK_DIFF})')

    return n


def run(args):
    scopes = parse_scopes(args.scopes)
    sample_ms = tuple(int(s) for s in args.ads_ms.split(','))
    views = make_views(max(1, args.views))
    if args.probe:
        # Three frames and out. Everything that has gone wrong so far was
        # visible in the first ADS frame of the run and cost 400 frames to
        # find out — this is the cheap look before committing to the rest.
        scopes, sample_ms, views = scopes[:1], (max(sample_ms),), views[:1]
    steady = sum(1 for m in sample_ms if m >= STEADY_MS)

    stamp = time.strftime('%Y%m%d_%H%M%S')
    # path=, so the run keeps this root rather than CaptureRun's default — see
    # the module docstring: docs/ads/runs/** is what tools/test_tab_open.py
    # calls "Tab shut". quality=None with --png, which is how CaptureRun is
    # told to write losslessly.
    run = CaptureRun.create('ads', stamp=stamp,
                            path=os.path.join(OUT_ROOT, stamp),
                            quality=None if args.png else 95)
    out_dir = run.path

    slot = None if args.slot == 'auto' else int(args.slot)
    print(f'weapon   {args.weapon} '
          f'({"slot " + str(slot) if slot else "slot: read off the Tab screen"})')
    print(f'scopes   {", ".join(scopes)}')
    print(f'views    {len(views)}   ads samples {sample_ms} ms')
    print(f'out      {out_dir}')
    print(f'frames   {len(scopes) * len(views) * (len(sample_ms) + 2)} '
          f'expected, {len(views) * steady} steady ADS shots per scope\n')

    # Everything that talks to the console happens before the operator is
    # asked to switch away — once the game is in front they cannot read it.
    aimer = Aimer(args.backend)
    inv = open_inventory(args.backend)
    equip = make_equipper(args.equip, inv, slot or 1)
    verifier = None

    done = []
    try:
        prompt = (f'>>> 切到游戏，{args.weapon}（栓狙）带在身上（1 号或 2 号位'
                  f'都行），站在空旷处，视角摆正。')
        if equip.self_verifies:
            prompt += '\n    要采的镜子全部放进背包，Tab 关掉。'
        prompt += '\n    开镜是点一下右键（不是按住），程序按这个来。'
        wait_for_key(prompt=prompt)
        frame = grab()
        if looks_black(frame):
            print('[capture] the screen grab is black — GDI cannot see the '
                  'game. Switch it to borderless windowed and try again.')
            return 1

        if slot is None:
            # The Tab name plate first: it reads the weapon's name rather than
            # guessing at its outline, and Tab has to be opened for the scopes
            # anyway. The HUD silhouette is the fallback for --equip manual,
            # where nothing can press Tab.
            if equip.locate:
                slot = equip.locate(args.weapon)
                if slot:
                    print(f'[slot] working on slot {slot} — Tab name plate')
            if slot is None:
                try:
                    slot, why = find_weapon_slot(frame, args.weapon)
                except SlotNotFound:
                    dump_slot_debug(frame, out_dir)
                    raise
                print(f'[slot] working on slot {slot} — {why}')
        equip.gun = slot          # ManualEquip declares it None and ignores it
        if not hold_weapon(inv, slot):
            print('[slot] the gun is not in hand — stopping rather than '
                  'photographing the other one')
            return 1
        if args.verify and not equip.self_verifies:
            try:
                verifier = ScopeVerifier(inv, slot)
            except Exception as e:
                print(f'[verify] disabled: {e}')

        if equip.preflight:
            missing = equip.preflight(scopes)
            if missing is None:
                print('[equip] preflight could not read the Tab screen')
            elif missing:
                print(f'[equip] not in the bag: {", ".join(missing)} — those '
                      f'will be skipped')

        for key in scopes:
            zh, want, _ = scope_info(key)
            print(f'\n── {key} ({zh}) ' + '─' * 30)
            ok, verified = equip(key)
            if ok is None:
                print(f'[equip] {key} was not fitted, skipping — the gun is '
                      f'still wearing {verified or "the previous scope"}')
                continue

            if verified is None and verifier is not None:
                verified = verifier.check(key)
            # The slot reading is the authority, not the drag's own verdict:
            # InventoryControl gives up after 0.8 s, and a slow swap that landed
            # a moment later reads correct here while its record says False.
            if verified is not None and verified != want:
                print(f'[verify] gun{slot} scope slot reads '
                      f'{verified or "<empty>"}, expected {want or "<empty>"}')
                if args.strict:
                    print('[verify] --strict: stopping')
                    break
                print('[verify] capturing anyway; the manifest labels these '
                      'frames with what was READ, not what was asked for')
            elif ok is False and verified is None:
                print(f'[verify] {key} could not be confirmed either way')

            # operator = "the only account of what went on the gun is a
            # human's". That is exactly what self_verifies=False means: the
            # equipper read nothing back, a person fitted it and pressed F8. A
            # machine readback still outranks them when there is one — the
            # branch above already put it in `verified`.
            labels, why = scope_label(key, verified,
                                      operator=not equip.self_verifies)
            print(f'[label] {key}: '
                  + (f'{labels[0]["source"]} — {why}' if labels
                     else f'no ground truth — {why}'))

            aimer.look_at(0, 0, settle=0.1)
            n = capture_scope(key, aimer, run, views, sample_ms,
                              args.png, args.weapon, slot, verified, labels)
            done.append(key)
            print(f'[capture] {key}: {n} frames')
    except (FocusLost, Aborted, StuckInScope, SlotNotFound) as e:
        print(f'\n!! {e} — stopping. {len(run.entries)} frames already on disk '
              f'are still usable.')
    except KeyboardInterrupt:
        print('\n!! interrupted')
    finally:
        # The InventoryControl is closed last and by run(), because run() is
        # what opened it: AttachEquip, ScopeVerifier and hold_weapon() all
        # borrow the same one, so none of them may close it.
        undos = [aimer.recentre, equip.close]
        if inv is not None:
            undos.append(inv.close)
        for undo in undos:
            try:
                undo()
            except Exception:
                pass
        # Everything meta.json used to hold, now the run's facts. Written in
        # the `finally` for the same reason it always was: a run that stops
        # early still has to say what it was trying to do. The per-frame
        # records are already on disk — CaptureRun.add() saves the manifest
        # every time, so a hard crash no longer loses the whole index.
        run.facts.update(
            stamp=stamp, weapon=args.weapon, slot=slot,
            screen=[SCREEN_W, SCREEN_H], views=len(views),
            view_seed=VIEW_SEED, view_offsets=views,
            ads_sample_ms=list(sample_ms), steady_ms=STEADY_MS,
            hip_after_ms=HIP_AFTER_MS,
            format='png' if args.png else 'jpg', backend=aimer.backend,
            equipper=equip.name, scopes_requested=scopes, scopes_done=done,
            frames=len(run.entries))
        run.save()

    print(f'\n{len(run.entries)} frames -> {out_dir}')
    if args.probe:
        sheet = probe_sheet(run)
        print(f'check the sight picture: {sheet}' if sheet else
              'probe captured nothing to compare')
        return 0 if sheet else 1
    print(f'next: python calibration/capture_ads.py --report {out_dir}')
    return 0 if done else 1


# ════════════════════════════════════════════════════════════
# Offline report — where ADS and hip actually differ
#
# Per scope, the median over views of the steady ADS frames minus the median
# over views of the hip frames. The median is the whole trick: the scene is
# different in every view and the scope is not, so whatever survives is the
# scope. `min_over_scopes` then keeps only what survives for *every* scope,
# which is the region a single scope-agnostic detector could live in.
# ════════════════════════════════════════════════════════════

DOWN = 4                 # analysis runs at 1/4 resolution; 3440x1440 medians
                         # over ~30 frames otherwise cost GBs for no detail
                         # that a region-level answer would use.
COMMON_MIN_DIFF = 4.0    # below this a "region that always changes" is noise
                         # in the median, not a usable cue


def _median(paths, run_dir):
    imgs = []
    for p in paths:
        img = cv2.imread(os.path.join(run_dir, p))
        if img is None:
            continue
        imgs.append(cv2.resize(img, (SCREEN_W // DOWN, SCREEN_H // DOWN),
                               interpolation=cv2.INTER_AREA))
    if not imgs:
        return None
    return np.median(np.stack(imgs).astype(np.float32), axis=0)


def _cells(diff_small, cell):
    """Mean |diff| per cell x cell block of the FULL-res frame."""
    c = max(1, cell // DOWN)
    h, w = diff_small.shape
    gh, gw = h // c, w // c
    return diff_small[:gh * c, :gw * c].reshape(gh, c, gw, c).mean(axis=(1, 3))


def _heat(base_small, grid, cell, path):
    c = max(1, cell // DOWN)
    up = cv2.resize(grid, (grid.shape[1] * c, grid.shape[0] * c),
                    interpolation=cv2.INTER_NEAREST)
    norm = np.clip(up / max(1e-6, up.max()) * 255, 0, 255).astype(np.uint8)
    heat = cv2.applyColorMap(norm, cv2.COLORMAP_INFERNO)
    base = base_small[:heat.shape[0], :heat.shape[1]].astype(np.uint8)
    cv2.imwrite(path, cv2.addWeighted(base, 0.45, heat, 0.55, 0))


def report(run_dir, cell=80, top=8):
    """Offline, and it must keep reading the runs captured before CaptureRun.

    Hence load_dir rather than a hand-rolled index.jsonl parse: the eleven runs
    under docs/ads/runs are the only ADS data that exists and re-capturing one
    costs tens of minutes of game time, so --report has to work on both shapes
    from the same code path — not from a branch that only one of them exercises.
    """
    try:
        run = CaptureRun.load_dir(run_dir)
    except FileNotFoundError as e:
        print(e)
        return 1
    recs = run.entries
    if not recs:
        print(f'{run_dir} has no captures')
        return 1

    groups = {}
    for r in recs:
        if r.get('state') == 'ads' and r['t_ms'] >= STEADY_MS:
            tag = 'ads'
        elif r.get('state') in ('hip', 'hip_after'):
            tag = 'hip'
        else:
            continue
        groups.setdefault((r['scope'], tag), []).append(r['capture'])

    run_dir = run.path
    scopes = sorted({s for s, _ in groups})
    n_views = len({r['view'] for r in recs if 'view' in r})
    if n_views < 3:
        print(f'note: only {n_views} view(s) — the median cannot separate the '
              f'scope from the scene yet, treat the numbers as indicative')

    out = os.path.join(run_dir, 'report')
    os.makedirs(out, exist_ok=True)

    grids, summary = {}, {}
    for s in scopes:
        ads = _median(groups.get((s, 'ads'), []), run_dir)
        hip = _median(groups.get((s, 'hip'), []), run_dir)
        if ads is None or hip is None:
            print(f'{s}: missing frames, skipped')
            continue
        diff = np.abs(ads - hip).mean(axis=2)
        grid = _cells(diff, cell)
        grids[s] = grid
        _heat(hip, grid, cell, os.path.join(out, f'diff_{s}.jpg'))
        flat = np.dstack(np.unravel_index(np.argsort(-grid, axis=None),
                                          grid.shape))[0][:top]
        summary[s] = dict(
            mean_diff=round(float(grid.mean()), 2),
            max_diff=round(float(grid.max()), 2),
            top_cells=[dict(box=[int(x * cell), int(y * cell),
                                 int((x + 1) * cell), int((y + 1) * cell)],
                            diff=round(float(grid[y, x]), 2))
                       for y, x in flat])

    if grids:
        common = np.minimum.reduce([grids[s] for s in sorted(grids)])
        any_hip = _median(sum((groups.get((s, 'hip'), [])
                               for s in sorted(grids)), [])[:12], run_dir)
        if any_hip is not None:
            _heat(any_hip, common, cell, os.path.join(out, 'diff_min.jpg'))
        flat = np.dstack(np.unravel_index(np.argsort(-common, axis=None),
                                          common.shape))[0][:top]
        summary['_min_over_scopes'] = [
            dict(box=[int(x * cell), int(y * cell),
                      int((x + 1) * cell), int((y + 1) * cell)],
                 worst_scope_diff=round(float(common[y, x]), 2))
            for y, x in flat]

    with open(os.path.join(out, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f'\n{"scope":<12} {"mean":>7} {"max":>8}   best cell (x0,y0,x1,y1)')
    for s in scopes:
        if s not in summary:
            continue
        d = summary[s]
        b = d['top_cells'][0]
        print(f'{s:<12} {d["mean_diff"]:>7.2f} {d["max_diff"]:>8.2f}   '
              f'{tuple(b["box"])} diff {b["diff"]:.1f}')
    if '_min_over_scopes' in summary:
        best = summary['_min_over_scopes'][0]['worst_scope_diff']
        print('\nregions that separate ADS from hip for EVERY scope '
              '(min over scopes):')
        if best < COMMON_MIN_DIFF:
            print(f'  none — the best region only moves {best:.1f} on its '
                  f'worst scope. No single fixed ROI covers 1x and 8x at '
                  f'once; expect a per-magnification rule, or a cue other '
                  f'than "this box changed".')
        for c in summary['_min_over_scopes']:
            print(f'  {tuple(c["box"])}  worst-case diff '
                  f'{c["worst_scope_diff"]:.1f}')
    print(f'\nheatmaps + summary.json -> {out}')
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--report', metavar='RUN_DIR',
                    help='offline: analyse a finished run instead of capturing')
    ap.add_argument('--cell', type=int, default=80,
                    help='--report grid cell, in full-res px (default 80)')
    ap.add_argument('--weapon', default='kar98k',
                    help='what is in hand; recorded in every frame record')
    ap.add_argument('--slot', default='auto', choices=('auto', '1', '2'),
                    help='weapon slot the gun sits in — where scopes get '
                         'dragged, and which slot gets read back. auto '
                         '(default) reads it off the HUD and switches to it')
    ap.add_argument('--scopes', default='default',
                    help=f'comma list, or "all". default = '
                         f'{",".join(DEFAULT_SCOPES)}')
    ap.add_argument('--views', type=int, default=VIEWS,
                    help=f'backgrounds per scope (default {VIEWS}); the view '
                         f'is turned right by a random step between each')
    ap.add_argument('--ads-ms', default=','.join(str(m) for m in ADS_SAMPLE_MS),
                    help='ms after the right button goes down to sample at')
    ap.add_argument('--equip', default='auto',
                    choices=('manual', 'auto', 'attach'),
                    help='auto (default): control/inventory.py drags each '
                         'scope on, falling back to manual if it cannot load. '
                         'attach: the same, but fail loudly instead. manual: '
                         'the operator fits each scope and presses F8')
    ap.add_argument('--no-verify', dest='verify', action='store_false',
                    help='skip the Tab-screen check of what is on the gun '
                         '(--equip auto reads the slot back regardless)')
    ap.add_argument('--strict', action='store_true',
                    help='stop the run when a scope fails verification')
    ap.add_argument('--probe', action='store_true',
                    help='one scope, one view, one steady frame, then stop — '
                         'writes probe.jpg (hip beside ADS) so the sight '
                         'picture can be checked before committing to a run')
    ap.add_argument('--timing', action='store_true',
                    help='measure how long the ADS toggle takes in and out, '
                         'and print what STEADY_MS / HIP_AFTER_MS / '
                         'SCOPE_STUCK_DIFF should be. Equips nothing')
    ap.add_argument('--timing-ms', type=int, default=2200,
                    help='--timing: ms to keep grabbing after each tap '
                         '(default 2200)')
    ap.add_argument('--png', action='store_true',
                    help='lossless frames (~6x the disk of the JPEG default)')
    ap.add_argument('--backend', default='auto',
                    choices=('auto', 'pico', 'sendinput'))
    args = ap.parse_args()

    if args.report:
        return report(args.report, cell=args.cell)
    try:
        if args.timing:
            return timing_run(args)
        return run(args)
    except NoPico as e:
        print(f'\n!! {e}')
        return 1


if __name__ == '__main__':
    sys.exit(main())

