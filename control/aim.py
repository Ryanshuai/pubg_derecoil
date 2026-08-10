"""Where the view is pointing, driven closed-loop against the screen.

    IMPORTANT: everything here is `move, measure, repeat until the screen
    agrees`. Nothing believes a mouse command landed because it was sent.

This is the half of recoil calibration that has nothing to do with recoil: put
the view somewhere known, prove it got there, and notice when it did not. Aim
assist wants exactly the same thing, which is why it lives here rather than
inside the calibration rig it was written for.

    from control.aim import ViewDriver
    view = ViewDriver(tracker, mouse, frames, sight='red_dot',
                      K=RECOIL_SIGHT_PROFILES['red_dot']['K'])
    view.set_reference()          # remember where this cell aims
    ...                           # fire a magazine
    view.recenter()               # and put it back, provably

`frames` is anything with `grab()` and `flush(n)` — see capture/cropper.py.
`tracker` is a detector/view_tracker.ViewTracker; this module never builds one,
because which patches are usable depends on what the optic hides and only the
caller knows the optic.

THREE WAYS THE VIEW'S POSITION CAN BE KNOWN, and they fail differently:

  * a RUNNING TOTAL (pending_pitch), integrated frame to frame. Cannot wrap —
    at ~150 fps nothing covers half a patch between two frames — but it cannot
    catch an error in its own starting belief.
  * an ABSOLUTE MATCH against a reference picture (absolute_offset). Catches a
    wrong starting belief, and wraps past half a patch, where it answers
    confidently in the wrong direction.
  * a HARD STOP (home_to_clamp). The game refuses to rotate past straight up
    or straight down, and that is the same place every time. Immune to both,
    but it costs a visible sweep of the screen.

They are used in that order and cross-checked against each other, because each
one's failure is the next one's blind spot. The one that has actually bitten:
a wrapped absolute reading that lands near ZERO looks exactly like success —
knocked 300 counts off centre, absolute_offset() came back -0.3. That is what
tracking_confirmed() exists for.

TWO NAMED EXCEPTIONS, AND WHAT EARNS THE NAME. `turn()` and `ads_tap()` are
open loop. They are named here so they stay two rather than becoming a habit,
and so the argument for them lives in ONE place instead of being restated in
every docstring that needs it:

  turn()      has no closed-loop equivalent to defer to. It exists to CHANGE
              WHAT IS BEHIND THE PANEL, and "different" is the whole
              requirement — where it lands is not unchecked, it is irrelevant.
  ads_tap()   does have one (control/gun.py's ensure_ads, which watches the
              crosshair), and therefore REFUSES to run for callers who can
              reach it. It is open loop only for the one caller that cannot.

That caller is calibration/capture_ads.py, and its reason is CIRCULAR RATHER
THAN LAZY: every loop that could be closed around a sight coming up needs
detector/ads_detector.py, and capture_ads exists to photograph the transition
that detector is FITTED TO. Confirming the sight with the detector under
construction is confirming it against itself. The same sentence governs
`open_loop()`, which is the constructor that hands out a ViewDriver with no
tracker for exactly this reason.

So the criterion is **"refuse when a verified route exists FOR YOU"**, not
"refuse because it is open loop". turn() takes no guard; ads_tap() raises.

WHY IT MATTERS THAT THIS IS CLOSED LOOP: PUBG clamps pitch, and a magazine
fired against the clamp measures near-zero recoil while reporting nothing
wrong. A silently corrupted measurement is worse than a failed one. The open
loop version of recenter() moved by an offset computed from the burst and
checked nothing; magazine after magazine the log read "residual +197,
recentred +66" and the leftover accumulated in one direction until the cell
was firing into the stop.
"""
import json
import os
import time

import cv2
import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from config import RECOIL_SIGHT_PROFILES, SCREEN_W, SCREEN_H
# Not through self.frames: ScreenBuffer serves the BANDED crops a detector
# asked for, and the pitch walk needs a slab of open world in the middle of the
# screen, which is not one of them.
from capture.cropper import win32_cap, FocusLost
# Same layer, and the reason this one is allowed to reach for focus: the
# stop walk holds the screen for minutes at a time, which is long enough
# that a lost foreground is the LIKELY outcome rather than an edge case --
# two of the first four hip-fire runs died that way on 2026-08-06.
from control.focus import ensure_focus
# NoPico is re-exported: calibration/ must not import press/ (rule 6 in
# tools/check_layering.py), and a caller that asks open_loop() for a device has
# to be able to catch the refusal it can raise.
from press.pointer import Pointer, NoPico     # noqa: F401 (re-export)

# ── closed-loop recentring ──
RECENTER_TOL = 8        # counts; below this the next burst does not care
RECENTER_TRIES = 4
RECENTER_STEP = 60      # counts per move, so no single frame pair wraps
RECENTER_SETTLE_S = 0.25   # let the view stop before the next burst
TRACK_TIMEOUT_S = 2.0   # post-fire recovery has always finished well inside
TRACK_STILL_S = 0.20    # this long under tol_px counts as stopped
TRACK_STILL_PX = 1.0
TRACK_MIN_S = 0.12      # USB command -> rendered frame; before this, "not
                        # started" and "finished" look identical
# Fraction of the correlator's half-patch range an absolute reading may use
# before it is refused as possibly wrapped. See absolute_offset.
ABS_TRUST_FRAC = 0.6
# How far the absolute reading may disagree with the running total before the
# reading is treated as wrapped rather than the total as drifted.
ABS_AGREE_COUNTS = 45
# How far the reference may be pre-shifted before the match is refused, as a
# fraction of the patch height. np.roll WRAPS, so a pre-shift of a whole patch
# has rotated the reference all the way round and matches nothing; even before
# that, the further it rolls the less real content overlaps.
#
# Measured, tools/test_abs_offset.py, 40 stored game frames with exact ground
# truth (the shifted patch is sliced from the same screenshot, not synthesised):
#
#     pre-shift px     0    77   128   200   300
#     placed to 4 px  100%  100%  100%   80%   10%
#
# 0.5 * 256 = 128 px, the last column that is perfect. Raising it trades a
# refusal for a confident wrong answer, which is the wrong direction here.
ROLL_MAX_FRAC = 0.5
PROBE_COUNTS = 30       # for tracking_confirmed()

# Homing against the pitch clamp. The clamp is the only absolute position the
# game offers: a running total drifts and a correlation wraps, but "the game
# refuses to rotate further" is the same place every time.
CLAMP_PUSH = 4000       # one open-loop shove, comfortably past the travel
CLAMP_SETTLE_S = 0.35
# The ceiling _send() raises PicoMouse's net-travel guard to. Comfortably past
# the largest single command this class issues -- goto_midline's shove is
# CLAMP_OVERSHOOT * 8034 = 12051 hip-fire -- because inside _send the guard is
# not the safety net, KNOWING WHERE THE VIEW IS is. It is not infinity so that
# a genuine runaway in this file still stops somewhere.
RANGING_LIMIT = 20000
BAND_STEP = 100         # rise per probe while mapping the measurable band
BAND_MAX = 3000         # stop rising; the travel is well inside this
BAND_TRACK_FRAC = 0.5   # observed/commanded above this counts as measurable
# Where in the measurable band to aim. 0.5 is level: the band ends at the top
# where the view has tilted up far enough for the tracker patches to fill with
# sky, and at the bottom where it is staring at close ground, so its middle is
# the horizon. Measured standing: bands of 800..2200 and 700..2200, centres
# 1500 and 1450, which is where the view looks flat.
#
# Aiming lower would buy headroom -- recoil only ever pushes up -- and 0.30 was
# tried for exactly that. It works and it is wrong: the view spends the burst
# pointed at the ground, which is neither how the gun is used nor a fair sample
# of the texture it is measured against. Level wins.
AIM_FRAC = 0.50

# ── the whole travel, stop to stop ──
#
# PUBG clamps pitch at straight down and straight up, so the MIDPOINT between
# the two stops is level -- by construction, on any terrain, through any sight,
# in any posture. That is a stronger statement than anything else in this file
# can make about where the view is pointing, and it is the one an operator
# asked for on 2026-08-06 after watching run upon run fire at the ground:
# "每次都低头到底，然后抬头到中线".
#
# ⚠ THE VIEW TRACKER CANNOT DO THIS, and the first version of this block tried
# to anyway. Phase correlation goes blind at both clamps -- bare close ground
# below, empty sky above -- and blind reads as STILL, which is how an earlier
# attempt reported the game's whole pitch travel as 13 counts while the
# character stared at the floor. So the signal is a raw frame difference over
# a central crop, which needs no texture at all, plus two controls per step.
# All of it, including every constant below, is tools/probe_pitch_range.py's,
# measured live on 2026-08-04; that file's docstring is the long version and
# it now calls these methods rather than keeping its own copy.
#
#     change    did the picture change over this step
#     control   how much it changes on its own over the same time AT THIS SAME
#               PITCH. Idle sway is not a constant -- close ground swings far
#               more pixels per breath than distant sky -- so a literal
#               threshold is too tight at one end and too loose at the other.
#     predict   what it WOULD have changed by had it moved, taken by sliding
#               the frame itself. This is the guard against the failure the
#               whole approach exists for: "the picture did not change" means
#               nothing when there was nothing in the picture to change.
#
# ⚠ EVERYTHING IS IN RAW COUNTS OF THE CURRENT SIGHT, and that is the point.
# pitch_scale() is a MODEL of how magnification converts counts, it has never
# been validated against a measurement, and every attempt to land level through
# a 4x on 2026-08-05 failed on a different unit error while the model looked
# right each time. Walking into a stop and counting commands needs no model;
# the answer simply comes out bigger through a scope.
# Where in the travel to aim, as a fraction of it. 0.5 is what the symmetry
# argument gives -- the two clamps are straight down and straight up, so their
# midpoint is level -- and the rest came from an operator watching the view on
# 2026-08-06: 0.5 read as "抬枪有一点高", 0.45 overshot the other way, 0.47 is
# where it settled. Two corrections in one sitting, both by eye, so treat the
# third decimal as unresolved rather than as measured.
#
# THE EYE WINS BECAUSE IT IS THE ONLY DIRECT OBSERVATION OF LEVEL. Everything
# else here measures the STOPS; nothing in this file can see the horizon. And
# 0.45 is not purely a correction to the game's symmetry, because two of this
# module's own choices bias the same way:
#
#   * the up-pass bracket came back (7725, 8034] -- 309 counts wide -- and the
#     point estimate takes its TOP end. If the travel is really 7725, half of
#     it is 3862 rather than 4017, i.e. 155 counts of the correction is mine.
#   * the same run measured tracked/commanded 0.636 +- 0.095 and flagged it as
#     NOT FLAT, so the count ruler is not perfectly linear across the travel
#     and a fraction of it is not exactly a fraction of the ANGLE.
#
# So this is an empirical aim fraction, not a claim about where the clamps are.
# Re-check it by eye after any change to the bracket rule or the step size:
# calibration/artifacts/pitch/standing_level.png is written by tools/probe_pitch_range.py and is
# the only thing that can say.
# ⚠ 0.47 -> 0.45 on 2026-08-06, by eye, at the operator's call while watching
# the campaign. It is an AIM fraction and always has been -- the two bullets
# above already say the bracket's top end contributes ~155 counts of it and
# that the count ruler is not flat -- so there is no derivation that says 0.47
# was right and 0.45 is wrong, or the reverse. What decides it is
# calibration/artifacts/pitch/standing_level.png, and that file is written by
# tools/probe_pitch_range.py, not by this run. RE-SHOOT IT before treating
# either number as level.
MIDLINE_FRAC = 0.45

CLAMP_OVERSHOOT = 1.5    # goto_midline's shove, as a multiple of the KNOWN
                         # travel. Any number above 1 reaches the stop from
                         # anywhere; 1.5 leaves margin for a travel measured
                         # one step short without costing anything, since the
                         # stop absorbs whatever is over.
FOCUS_TRIES = 3          # restarts of the stop walk after a lost foreground
TRAVEL_STEP = 100        # counts per step, and the bracket on each stop is
                         # this wide. Scaled up for a magnified sight, or a 4x
                         # would take 165 steps to cross its travel.
TRAVEL_MAX = 9000        # give up. The travel is ~5000 COMMANDED counts on a
                         # red dot, not ~3000 -- see MOVED_FRAC. Also scaled.
TRAVEL_CONFIRM = 2       # consecutive still steps before believing a stop
TRAVEL_SETTLE = 0.20     # after the view stops, before the after-frame
MIN_TRAVEL = 500         # below this nothing was taking input; refuse it
# ── deciding "did it move", and the run that set these numbers ──
#
# The first live run (2026-08-04, m416 + red dot, training range) decided it by
# change/control and got it wrong in both directions, because CONTROL IS THE
# NOISY ONE. Over a single ~0.6 s step the idle sway of an ADS'd rifle put it
# anywhere from 0.42 to 3.79 grey levels, while `change` on a genuinely moving
# step sat at a steady ~9. So the ratio flickered between 3.0 and 21 on
# identical steps, and with a threshold at 4.0:
#
#   * the UP pass returned a stop at 400 counts. Its last two steps read
#     change 8.60 and 9.37 — the same as every moving step before them — and
#     were called still only because control happened to spike to 2.86 and
#     2.77. Standing came out with a travel of 550.
#   * crouching refused outright as "blind" on predict 15.04 vs control 3.79,
#     a ratio of 3.97 against the same 4.0. Nothing was wrong with that view.
#
# `predict` is the stable comparison and it is what a step SHOULD produce, so
# the primary test is now change/predict. Measured over that whole run:
#
#     moving steps    change/predict  0.57 .. 1.00
#     at a stop       change/predict  0.03, 0.06, 0.16, 0.18
#
# The floor at 0.30 sits in a 3x gap. Control survives only as a secondary
# floor (a stop still shows the sway, so change lands near control there) and
# in the blind test, where the question is whether a full step could out-signal
# the noise AT ALL rather than whether this one did.
#
# THE 0.57-1.00 IS NOT SLOP, IT IS THE COUNT EFFICIENCY. The view rotates
# ~60% of what is commanded: the tracker read +60 counts per 100 commanded on
# every early step, and prone's tracked/commanded came back 0.603 ± 0.014. A
# CONSTANT factor, which is why the midpoint survives it untouched — half of a
# travel measured in commanded counts is still half of the travel. It is also
# why TRAVEL_MAX has to be 9000 for a red dot: the real travel costs ~1/0.6 as
# many commands as it does degrees.
MOVED_FRAC = 0.30   # change must be this much of what a full step would give
CTRL_MULT = 1.5     # ...and this much above the idle change at the same pitch
BLIND_MULT = 2.0    # predict below this x control: nothing here can answer
# The crop the difference is taken over: the middle of the screen, which is all
# game world. Everything outside it is HUD, and HUD does not move with pitch.
WORLD_BOX = (int(SCREEN_H * 0.15), int(SCREEN_W * 0.25),
             int(SCREEN_H * 0.65), int(SCREEN_W * 0.50))


def _load_pitch_range():
    """Level, per posture, as counts above the bottom pitch clamp.

    Measured once by tools/probe_pitch_range.py. Empty falls back to the
    ground-to-sky band scan in calibrate_pitch().

    It lives under calibration/artifacts/pitch/ next to the per-step screenshots it was read
    off, because it is measured FACT, not calibration policy -- same reason
    weapon_rpm.json sits in calibration/artifacts/recoil/ and kit_facts.json in calibration/artifacts/compat/.
    """
    path = os.path.join(_ROOT, 'calibration', 'artifacts', 'pitch', 'pitch_range.json')
    try:
        return {k: v for k, v in json.load(open(path, encoding='utf-8')).items()
                if not k.startswith('_')}
    except Exception:
        return {}


PITCH_RANGE = _load_pitch_range()

_TRAVEL_PATH = os.path.join(_ROOT, 'calibration', 'artifacts', 'pitch', 'pitch_travel.json')


def _load_travel():
    """{sight: raw counts from the bottom pitch stop to the top one}.

    A SEPARATE FILE from pitch_range.json on purpose. That one is keyed by
    POSTURE and this one by SIGHT, and they are not the same quantity: the
    travel is 180 degrees of rotation whatever the character is doing, while
    `level_up` is an offset that was measured through one optic. Merged into
    one file the two key spaces would sit side by side with nothing but a
    reader's memory keeping `standing` and `red_dot` apart, and _load_pitch_
    range() would hand a sight entry to goto_level() as if it were a posture.
    """
    try:
        return {k: v for k, v in json.load(open(_TRAVEL_PATH,
                                                encoding='utf-8')).items()
                if not k.startswith('_')}
    except Exception:
        return {}


PITCH_TRAVEL = _load_travel()


class ViewDriver:
    """Rotates the view, and knows where it left it."""

    def __init__(self, tracker, mouse, frames, K, sight='red_dot'):
        self.tracker = tracker
        self.mouse = mouse
        self.frames = frames
        self.K = K
        self.sight = sight
        # Pitch owed back to the view, in ADS mouse counts. See recenter().
        self.pending_pitch = 0.0
        # Where this cell is aiming, for the absolute check. See set_reference.
        self.ref_patches = None
        # Set when the view's position stops being knowable. A cell measured
        # after this is not wrong-looking, it is wrong — see recenter().
        self.tracking_lost = False
        # Counts above the bottom clamp where the view can be measured,
        # found once. See calibrate_pitch.
        self.pitch_centre = 0
        self.pitch_band = None
        # {posture: raw counts between the two pitch stops, for THIS sight}.
        # Measured once per posture per run and then free; see measure_travel.
        self._travel = {}
        # Every per-step reading the last travel measurement took, answer or
        # not. tools/probe_pitch_range.py builds its whole report out of this.
        self.travel_detail = {}
        # Postures already told about "the midline here is not shown to be
        # level", so the caveat lands once instead of before every magazine.
        self._warned_midline = set()
        # Test seam: what to grab the central world crop with. None means the
        # screen. tools/probe_pitch_range.py's --selftest drives the entire
        # stop-walk offline against a synthetic scene through this, which is
        # the only way any of it gets exercised without occupying the game.
        self.world_fn = None
        # Off: the band scan sweeps the view ground-to-sky at the start of
        # every posture, which is slow and unpleasant to watch. See reaim().
        self.use_homing = False

    @classmethod
    def open_loop(cls, frames=None):
        """A ViewDriver for a caller with nothing to close the loop WITH.

        Only turn(), ads_tap() and the frame passthroughs work on it: there is
        no tracker and no meaningful K, so every measuring method raises. That
        is the intent — a number they produced here would be arithmetic on a
        tracker that does not exist, and this class's whole discipline is that
        an unverified position is not a position.

        The one caller is calibration/capture_ads.py; the circularity that
        makes it legitimate is in this module's header, stated once for both
        this and ads_tap().

        The Pointer comes from press/pointer.py's strict constructor, so no
        Pico means no run — the reason is written there, and it applies to
        turning the view exactly as it does to the button.
        """
        return cls(None, Pointer.opened(), frames, K=1.0)

    # ⚠ A `backend` PROPERTY STOOD HERE and is gone (2026-08-08). Its
    # docstring was right about why it existed -- "a run made on SendInput is
    # one the game very likely ignored outright, and the file has to be able
    # to say which it was" -- and that is exactly why it went with the
    # SendInput backend rather than outliving it. A metadata field that can
    # only take one value does not record which run this was.

    def retune(self, tracker, K, sight):
        """The optic changed, so the patches and the scale did too.

        The reference and the pitch centre do NOT survive it: both are stated
        in the old optic's counts, and a 4x scope buys 3.3x the rotation per
        count (see pitch_scale).
        """
        self.tracker, self.K, self.sight = tracker, K, sight
        self.ref_patches = None
        self.pitch_centre = 0
        self._travel = {}

    # ── the frame source, so callers do not have to reach past this object ──

    def grab(self):
        return self.frames.grab()

    def flush(self, n=8):
        self.frames.flush(n)

    def turn(self, yaw, pitch=0, settle_s=0.0):
        """L0 — Swing the view, OPEN LOOP, and do not claim to know where it
        ended. Nothing here arrives anywhere; goto_midline() is the L2.

        The first of the module's two named exceptions (see the header for
        what earns the name). Everything else here moves in order to ARRIVE
        somewhere and proves it against the screen. This one moves in order to
        CHANGE WHAT IS BEHIND THE PANEL: calibration/legacy_collect_templates.py
        photographs the translucent Tab screen against a dozen different
        backdrops, and where the view lands is not merely unchecked, it is
        irrelevant — different is the whole requirement.

        Callers used to get this by reaching through to `rig.mouse.move`,
        which is worse than it looks: a HAL member pulled out of a high-level
        object is invisible to tools/check_layering.py, because that only
        reads imports. The reach-through kept `calibration/` importing press
        with nothing to grep for.

        Do NOT build a positioning routine on this. It does not update
        pending_pitch, so a recenter() afterwards is measuring from a belief
        this call already invalidated. Use goto_level / recenter / reaim.
        """
        self._send(int(yaw), int(pitch))
        if settle_s:
            time.sleep(settle_s)

    def _send(self, dx, dy):
        """L0 — Every pitch/yaw command this class issues goes through here.

        ⚠ IT EXISTS TO BE EXEMPT FROM A GUARD, and the asymmetry is the point.
        PicoMouse.move() refuses once net vertical travel passes
        NET_DY_LIMIT, because a caller that keeps pushing one way arrives at a
        pitch stop where the game DISCARDS further counts — the commands
        succeed, the view does not move, and every reading after that is of a
        stationary screen reported as a small effect. A probe walked ~4800
        counts that way on 2026-08-08 with its +1/-1 sign in the outer loop.
        THAT guard is for callers who do not know where the view is.

        This class does know. home_to_clamp pushes past any possible travel
        ON PURPOSE, and goto_midline then walks back a measured half — 4017
        counts hip-fire, past the limit by itself. A threshold cannot separate
        the two cases: 4017 is legitimate and 4800 is the bug. WHO IS PUSHING
        separates them, and this method is that answer expressed in code.

        `reset_travel()` afterwards is the other half: every route through
        here ends with the view at a place this class can name, so the next
        raw caller's budget starts from a known origin instead of inheriting
        ours.
        """
        with self.mouse.travel_budget(RANGING_LIMIT):
            self.mouse.move(int(dx), int(dy))
        self.mouse.reset_travel()

    def ads_tap(self, hold_ms=60, settle_s=0.0):
        """L0 — Toggle the sight in or out. OPEN LOOP, and the module's second
        named exception — read turn() above for why they are named. The L2 is
        control/gun.py's ensure_ads(); a measurement must use that one.

        THE CLOSED-LOOP VERSION IS control/gun.py's ensure_ads(), and it is
        what a measurement must use. It watches the crosshair until the sight
        is really up, because a whole burst once went out from the hip, passed
        the gate, and reported +588 counts of residual: the analysis applies
        the scoped K of 1.55 to motion that happened at the hip's 0.50.

        This cannot be that, for the circularity in the module header. What
        that leaves is the caller's problem, and capture_ads can carry it: a
        human looks at probe.jpg before a run, and the report is built on
        medians across views.

        ADS IS A TAP, NOT A HOLD. Holding the right button down is
        shoulder aim -- a third state, see detector/ads_detector.py -- and the
        sight picture never appears; it is the
        RELEASE that switches into it. Run calibration/artifacts/ads/runs/20260801_222936 is 64
        frames of that mistake — iron sights and a red dot came out with
        hip-to-"ADS" differences of 31.45 and 31.48, which was the gun coming
        up rather than a scope. Toggling back out is a SECOND call, and
        leaving it out is worse than it looks: the next thing captured starts
        already scoped in and nothing downstream can tell.

        A docstring saying "prefer ensure_ads()" would be a plea; the guard
        below is a refusal, and it says why in its own message rather than
        having this paragraph say it twice.
        """
        if self.tracker is not None:
            raise RuntimeError(
                'ads_tap() is for ViewDriver.open_loop() only. This driver has '
                'a tracker, so it came from a rig that also has a GunDriver — '
                'use gun.ensure_ads(), which watches the crosshair until the '
                'sight is really up. An unverified ADS toggle is how a burst '
                'goes out from the hip and gets analysed at the scoped K.')
        # Positional, not keyword: `mouse` here is a Pointer under open_loop()
        # but a raw PicoMouse under sweep.Rig, and the two spell the duration
        # differently (hold_ms vs duration_ms).
        self.mouse.click(0x02, hold_ms)
        if settle_s:
            time.sleep(settle_s)

    # ── where the view is ──

    def set_reference(self):
        """R — Remember where the cell is aiming, for absolute_offset(). It
        drives nothing, which is NOT the same as being safe to call anywhere.

        ⚠ Takes the reference WHEREVER THE VIEW IS, including against a clamp,
        and the patches are per sight — so raise the sight first, then call
        this, and never the other way round (goto_midline's `set_ref=False`).
        """
        self.ref_patches = self.tracker.slice_frame(self.grab())
        self.pending_pitch = 0.0
        self.tracking_lost = False

    def absolute_offset(self, predicted=None):
        """R — Counts between the view now and the cell's reference, or None.
        Drives nothing. Absolute, so it can catch a wrong starting belief;
        track_still() only integrates and cannot.

        The incremental integral cannot catch an error in its own starting
        belief — drive pending_pitch to zero and the view stays wherever that
        belief was wrong by. This can, because it compares against the
        reference itself rather than against a running total.

        `predicted` is where the integral thinks the view is, in counts from
        the CURRENT reference link. The reference is pre-shifted by that much
        before correlating, so what has to fit inside the correlator's range
        is the integral's ERROR rather than the whole distance travelled.
        measure_pair's docstring already assigned this job here — "patch-window
        pre-shifting (which extends usable range) belongs to the caller, which
        knows the pattern being injected" — and until 2026-08-03 the caller
        did not do it, which is why the usable range was ±50 counts from the
        reference instead of ±50 counts from the prediction.

        Measured on 40 stored game frames, tools/test_abs_offset.py:

            raw          100% out to 100 px, 28% at 128, 0% and confidently
                         wrong by -150 px at 200
            pre-shifted  100% out to 128 px, 80% at 200, 10% at 300

        The 300 px collapse is not a tuning problem. np.roll wraps, so past
        one patch height the "pre-shift" has rotated the reference all the way
        around and there is nothing left to match. Hence ROLL_MAX_PX, and
        hence the refusal above ROLL_MAX_FRAC rather than a bigger roll.
        """
        # Why the last call gave up, for the caller to print. A message that
        # names a cause nobody checked is worse than no message: this one used
        # to say "more than N counts away, or the scene changed" for all four
        # reasons including "there is no reference", and a probe that had
        # simply forgotten to call set_reference() reported nine magazines of
        # a range problem that did not exist. Cost two live runs and a wrong
        # fix before anyone read the first line of this function.
        self.place_fail = None
        if self.ref_patches is None:
            self.place_fail = ('no reference — set_reference() was never '
                               'called for this cell')
            return None
        pred = self.pending_pitch if predicted is None else predicted
        # Recoil rotates the view up, which slides content DOWN the screen, so
        # a view `pred` counts up from the reference reads +pred*K px.
        roll_px = int(round(pred * self.K))
        if abs(roll_px) > ROLL_MAX_FRAC * self.tracker.patch_h:
            # Beyond this the reference holds none of the content being
            # looked for. Refusing is not a limitation to work around;
            # believing a correlation against wrapped-around pixels is how
            # a confident wrong answer gets made.
            self.place_fail = (
                f'the running total puts the view {pred:+.0f} counts from the '
                f'reference, past the '
                f'{ROLL_MAX_FRAC * self.tracker.patch_h / self.K:.0f} the '
                f'reference patch can still hold')
            return None
        ref = self.ref_patches
        if roll_px:
            ref = [np.ascontiguousarray(np.roll(p, roll_px, axis=0))
                   for p in ref]
        cur = self.tracker.slice_frame(self.grab())
        m = self.tracker.measure_pair(ref, cur)
        if m is None or m.out_of_range:
            self.place_fail = (
                'the match came back out of range — the scene changed, or the '
                'running total is wrong by more than half a patch')
            return None
        # A wrapped reading is not noisy, it is confident and wrong — the peak
        # comes back a whole patch out, and every patch wraps together so the
        # cross-patch agreement still looks healthy. There is no way to tell
        # from the reading itself, so anything not comfortably inside the
        # range is refused rather than believed. Applied to the RESIDUAL: the
        # pre-shift is supposed to have taken the bulk of the distance out.
        if abs(m.dy) > ABS_TRUST_FRAC * self.tracker.patch_h / 2:
            self.place_fail = (
                f'{m.dy / self.K:+.0f} counts left over after pre-shifting by '
                f'{roll_px / self.K:+.0f} — the running total is wrong by more '
                f'than the correlator can resolve, so the match may have '
                f'wrapped')
            return None
        return (roll_px + m.dy) / self.K

    def pitch_scale(self):
        """Counts this sight needs per unit of view rotation, vs the red dot.

        K is pixels per count; pixels per degree scales with magnification, so
        DEGREES per count goes as K / mag and the counts needed for a given
        rotation go as mag / K. Between the red dot (mag 1) and the VSS's fixed
        PSO-1 (mag 4) that is a factor of about 3.3 -- computed from
        RECOIL_SIGHT_PROFILES rather than quoted, because the red dot's K has
        moved three times and every prose copy of it went stale silently.

        Everything that drives the view by an absolute count — homing into the
        pitch stop, rising to level — has to multiply by this or it moves the
        wrong distance. Unscaled, CLAMP_PUSH's 4000 counts is worth 1212 red
        dot counts through a 4x and does not reach the stop at all, and the
        1770 that means level leaves the view buried in the ground.
        """
        prof = RECOIL_SIGHT_PROFILES.get(self.sight, {})
        ref = RECOIL_SIGHT_PROFILES['red_dot']
        here = prof.get('mag', 1.0) / (prof.get('K') or self.K)
        there = ref.get('mag', 1.0) / ref['K']
        return here / there if there else 1.0

    # ── moving it ──

    def home_to_clamp(self, direction=+1):
        """L0 — Push the view into a pitch stop. Open loop, deliberately: it
        measures nothing, so nothing it returns is worth reading. to_stop() is
        the L1 that walks the same distance and reports it.

        +1 is down, -1 is up: a positive mouse dy pulls the view down.

        Nothing is measured here, and nothing can be. The clamps are where the
        view stares at bare ground or empty sky, which is exactly where phase
        correlation has nothing to lock onto — the first version of this tried
        to detect the stop by watching the view halt and reported the game's
        entire pitch travel as 13 counts while the character was looking
        straight down.

        It does not need measuring. A stop is a stop: push further than the
        travel could possibly be and the view is against it, wherever it
        started. That is the one absolutely repeatable position this game
        offers.
        """
        # max(1.0, ...) so the scale can only ever ADD push. pitch_scale() is
        # an unvalidated model (see its docstring); a value below 1 out of a
        # mis-entered profile would turn the one thing in this file that cannot
        # fail into a shove that stops short of the stop, and every caller
        # would go on treating where it landed as absolute.
        push = CLAMP_PUSH * max(1.0, self.pitch_scale())
        self._send(0, int(direction * push))
        time.sleep(CLAMP_SETTLE_S)

    def track_still(self, timeout_s=TRACK_TIMEOUT_S, still_s=TRACK_STILL_S,
                    tol_px=TRACK_STILL_PX, prev=None, min_s=TRACK_MIN_S):
        """R — Integrate view motion until it stops. Returns counts moved.
        Watches; it commands nothing. Relative and never wraps, where
        absolute_offset() is absolute and can.

        Frame to frame, so it never wraps: the correlator's range is half a
        patch per PAIR of frames, not per interval, and at ~150 fps nothing
        the game or the mouse does covers that between two frames. An absolute
        match against a reference taken a magazine ago would wrap long before
        the drift got interesting.

        Waiting for "still" matters as much as the integral. PUBG pulls the
        view back for a while after the trigger releases, and a reading taken
        before that finishes describes a position the view is already leaving.
        """
        # `prev` must be captured BEFORE the move that is being measured. A
        # mouse command lands in the frame or two right after it is issued, so
        # a tracker that grabs its own first frame afterwards starts counting
        # from a view that has already arrived — it sees nothing move and
        # says so. That mistake measured the game's entire pitch travel as 14
        # counts.
        if prev is None:
            prev = self.tracker.slice_frame(self.grab())
        t0 = time.perf_counter()
        total_px, quiet_since = 0.0, None
        while time.perf_counter() - t0 < timeout_s:
            cur = self.tracker.slice_frame(self.grab())
            m = self.tracker.measure_pair(prev, cur)
            prev = cur
            if m is None:
                continue
            # `isfinite` as well as `out_of_range`: they are different
            # refusals. out_of_range means the correlator measured something
            # too large to trust; NaN means it could not measure at all —
            # phaseCorrelate on a patch with no texture, which in this game is
            # the sky. A NaN added here is not a wrong number, it is a
            # CONTAGIOUS one: it lands in pending_pitch, survives every later
            # addition, and finally surfaces hundreds of lines away as
            # `int(round(nan))` inside recenter(), with a traceback that names
            # neither the sky nor the patch. Measured 2026-08-05 on a vss cell
            # that had drifted into the pitch clamp with the view near
            # vertical.
            if not m.out_of_range and np.isfinite(m.dy):
                total_px += m.dy
            now = time.perf_counter()
            # min_s covers the gap between issuing a move over USB and the
            # game rendering it; before then "not moving yet" and "finished
            # moving" look the same.
            if abs(m.dy) <= tol_px and now - t0 >= min_s:
                if quiet_since is None:
                    quiet_since = now
                elif now - quiet_since >= still_s:
                    break
            elif abs(m.dy) > tol_px:
                quiet_since = None
        return total_px / self.K

    def _move_tracked(self, d_counts):
        """Move the view and measure what actually happened.

        Split into steps: a single jump of a few hundred counts lands entirely
        between two frames, which is exactly the case the correlator cannot
        measure — it wraps and reports a small move in the wrong direction.
        Stepping keeps every frame-to-frame displacement inside the range that
        makes the measurement meaningful.
        """
        moved = 0.0
        left = int(round(d_counts))
        while left:
            step = max(-RECENTER_STEP, min(RECENTER_STEP, left))
            prev = self.tracker.slice_frame(self.grab())
            self._send(0, step)
            left -= step
            got = self.track_still(timeout_s=0.8, still_s=0.10, prev=prev)
            moved += got
            # Commanded a move, the view did not move: that IS the pitch
            # clamp. PUBG stops rotating at straight up and straight down, and
            # a magazine fired there measures near-zero recoil while reporting
            # nothing wrong. Shoving harder cannot help — the old open-loop
            # code had no way to notice and would keep pushing into it.
            if abs(step) >= RECENTER_STEP and abs(got) < abs(step) * 0.2:
                self.tracking_lost = True
                print(f"        [!] moved {step:+d} counts and the view did "
                      f"not follow ({got:+.0f}) — at the pitch clamp, or the "
                      f"game is not taking input")
                break
        return moved

    def tracking_confirmed(self, probe=PROBE_COUNTS):
        """L1 — Push the view a known amount and check the reading follows.
        ⚠ It MOVES the view, so never between set_reference() and the burst.

        Guards the one failure the range check cannot: a WRAPPED correlation
        that lands near zero. Knocked 300 counts off centre, absolute_offset()
        came back -0.3 — not noisy, not flagged, just confidently claiming the
        view was exactly where it started. A reading that is large and
        suspicious can be refused on its magnitude; a reading that is small and
        wrong looks identical to success.

        So instead of asking whether the number is plausible, this makes the
        view move by a known amount and asks whether the number moved with it.
        Nothing else in the loop can tell "centred" from "lost".
        """
        before = self.absolute_offset()
        if before is None:
            return False
        self._move_tracked(probe)
        after = self.absolute_offset()
        self._move_tracked(-probe)
        if after is None:
            return False
        # A positive mouse dy pulls the view DOWN, so the offset moves by
        # -probe. Generous tolerance: this is separating "tracking" from
        # "not tracking at all", not calibrating anything.
        return abs((after - before) + probe) < probe * 0.4

    def recenter(self):
        """L2 — Put the view back where the cell started, and prove it did.

        A magazine never ends where it started: the compensation is wrong by
        exactly the residual being measured, so every burst walks the view a
        few hundred counts. PUBG clamps pitch — at straight up or straight
        down the view stops moving, and a magazine fired there measures
        near-zero recoil while reporting nothing wrong. A silently corrupted
        cell is worse than a failed one.

        This used to compute an offset from the burst recording and move by
        it, open loop, with nothing checking the result. It did not come back:
        magazine after magazine the log read "residual +197, recentred +66",
        and the leftover accumulated in one direction until the cell was
        firing into the clamp. Two reasons, both invisible without a check —
        the recording's drift figure does not subtract the player's own mouse
        movement, and the recording stops while PUBG is still pulling the view
        back, so whatever recovery happens afterwards is never seen.

        So: move, measure, repeat until the screen agrees. self.pending_pitch
        is the integrated offset from the cell's reference, updated by every
        measurement including this function's own moves, which is what makes
        the loop close.

        Must run in ADS. The offset is in ADS counts and a mouse count buys a
        third as much rotation from the hip (K 0.50 against 1.55), so
        recentring from the hip under-corrects threefold — and the auto-reload
        drops out of ADS, which is exactly when it is tempting to do this.
        """
        # Let post-fire recovery finish before believing any number.
        self.pending_pitch += self.track_still()
        # Stated, not assumed. track_still filters the NaN at its source, so
        # this can only fire if some other path put one in — and then the
        # honest answer is that the view's position is unknown, which is a
        # thing this class already knows how to report. Crashing here instead
        # loses the cell AND the run.
        if not np.isfinite(self.pending_pitch):
            print('        [!] the integrated view offset is not a number — '
                  'the correlator returned NaN somewhere. Position unknown.')
            # tracking_lost, not just a return: the caller's next act is to
            # fire, and a magazine fired from an unknown position is not noisy
            # data, it is wrong data that looks fine. This is the flag that
            # already means exactly that, and harvest already checks it.
            self.tracking_lost = True
            self.pending_pitch = 0.0
            return None
        # Sign: dy > 0 means the view rotated UP (the recoil direction), and a
        # positive mouse dy pulls it back DOWN — so the correction has the same
        # sign as the drift, and what the screen then does comes back negative,
        # which is what walks pending_pitch to zero.
        total = 0
        for _ in range(RECENTER_TRIES):
            d = int(round(self.pending_pitch))
            if abs(d) < RECENTER_TOL:
                break
            self.pending_pitch += self._move_tracked(d)
            total += d

        # Now that the view is supposed to be back, ask the reference rather
        # than the running total. This is the only step that can catch the
        # integral having started from a wrong belief.
        for _ in range(RECENTER_TRIES):
            off = self.absolute_offset()
            if off is None:
                print(f"        [!] cannot place the view against the cell's "
                      f"reference: {self.place_fail}.\n"
                      f"            Running total says "
                      f"{self.pending_pitch:+.0f}; going with that")
                break
            # The reference outranks the running total only when the two
            # roughly agree. They are independent — one integrates frame to
            # frame and cannot wrap, the other compares against a picture from
            # a magazine ago and can — so a disagreement means the wrapping
            # one is lying, and it is not the integral.
            if abs(off - self.pending_pitch) > ABS_AGREE_COUNTS:
                print(f"        [!] reference says {off:+.0f} counts, running "
                      f"total says {self.pending_pitch:+.0f} — the reference "
                      f"reading has wrapped; going with the total")
                break
            self.pending_pitch = off
            if abs(off) < RECENTER_TOL:
                # "Already centred" is exactly what a wrapped reading looks
                # like, so it is the one answer worth confirming.
                if not self.tracking_confirmed():
                    print("        [!] the view reads centred but does not "
                          "respond to a test move — the reference match has "
                          "wrapped and this cell's position is unknown")
                    self.tracking_lost = True
                self.pending_pitch = 0.0
                break
            # Take the move back out of the running total, exactly as the
            # incremental loop above does. Discarding it was harmless while
            # absolute_offset() ignored pending_pitch; once it started
            # PRE-SHIFTING by it (2026-08-03) the stale value made every
            # following pass predict a view that had already moved, so the
            # residual came back the size of the correction and the loop sat
            # there until it ran out of tries. Measured: four magazines
            # reporting "view will not come back" 19 to 63 counts off, which
            # is the correction it had just made.
            self.pending_pitch += self._move_tracked(off)
            total += int(round(off))
        else:
            print(f"        [!] view will not come back ({off:+.0f} counts "
                  f"off) — at the pitch clamp? the next magazine measures "
                  f"short and looks fine doing it")
        return total

    # ── choosing where to aim ──

    def _world(self):
        """The central crop as grayscale. One GDI grab, ~4 ms."""
        if self.world_fn is not None:
            return self.world_fn()
        return cv2.cvtColor(win32_cap(WORLD_BOX), cv2.COLOR_BGR2GRAY)

    @staticmethod
    def _change(a, b):
        """Mean absolute difference in grey levels. Deliberately the dumbest
        possible measure of "is this a different picture": it needs no texture,
        no features and no threshold of its own, which is the entire reason it
        works where the correlator does not."""
        return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean())

    @classmethod
    def _shift_change(cls, img, px):
        """What this picture would look like if it slid `px` rows.

        Overlapping slices rather than np.roll: rolling wraps the bottom of the
        frame onto the top and the seam contributes a difference no real motion
        would produce.
        """
        px = max(1, min(abs(int(px)), img.shape[0] - 1))
        return cls._change(img[:-px], img[px:])

    def step_once(self, counts):
        """L1 — Command `counts` of pitch (positive = down) and report what
        happened. One step; to_stop() is the loop built out of it.

        -> {'change', 'control', 'predict', 'tracked', 'moved', 'blind'}. See
        the TRAVEL block at the top of this file for what each one answers and
        which live run set the thresholds.

        `tracked` is the view tracker's opinion in counts, and it DECIDES
        NOTHING -- near the stops it is blind and blind reads as zero. It is
        recorded because where it does work it says whether the count ruler is
        linear, and got/counts constant across the travel is what makes the
        midpoint in counts the midpoint in angle.
        """
        t0 = time.perf_counter()
        before = self._world()
        prev = (self.tracker.slice_frame(self.grab())
                if self.tracker is not None else None)
        # turn() is the named open-loop entry point and the honest one here:
        # this does not want a closed loop, it wants to find out what the game
        # does with counts it was told to consume.
        self.turn(0, int(counts))
        got = (self.track_still(timeout_s=0.7, still_s=0.10, prev=prev)
               if prev is not None else 0.0)
        time.sleep(TRAVEL_SETTLE)
        after = self._world()
        dt = time.perf_counter() - t0

        # The control is taken AT THIS PITCH and over the same wall time, not
        # once per run: idle sway against close ground is several times what it
        # is against sky, and a fixed floor would be wrong at one end whichever
        # end it was set at.
        c0 = self._world()
        time.sleep(dt)
        c1 = self._world()

        d_still = max(self._change(c0, c1), 0.05)
        d_move = self._change(before, after)
        d_pred = self._shift_change(after, abs(counts) * self.K)
        return {'change': d_move, 'control': d_still, 'predict': d_pred,
                'tracked': got,
                'moved': (d_move > MOVED_FRAC * d_pred
                          and d_move > CTRL_MULT * d_still),
                'blind': d_pred < BLIND_MULT * d_still}

    def to_stop(self, direction, step, label='', max_counts=None, verbose=True):
        """L1 — Walk into a pitch stop, one verified step at a time, and
        report the distance. home_to_clamp() is the free shove that does not.
        -> (last_moving_total, rows).

        `direction` is +1 down, -1 up. ON RETURN THE VIEW IS AGAINST THE STOP —
        the confirming steps were absorbed by it — so the caller can treat this
        position as absolute without knowing how much was swallowed.

        last_moving_total is the cumulative commanded counts of the last step
        that actually moved, so the stop lies in (last_moving_total, +step].
        None means no stop was established, and the caller MUST NOT turn that
        into a number.
        """
        max_counts = TRAVEL_MAX if max_counts is None else max_counts
        total, last_moving, still_run, rows = 0, 0, 0, []
        while total < max_counts:
            r = self.step_once(direction * step)
            total += step
            rows.append(dict(r, at=total, change=round(r['change'], 2),
                             control=round(r['control'], 2),
                             predict=round(r['predict'], 2),
                             tracked=round(r['tracked'], 1)))
            if verbose:
                print(f"    {label} {total:5d}  change {r['change']:6.2f}  "
                      f"predict {r['predict']:6.2f}  = "
                      f"{r['change'] / max(r['predict'], .01):4.2f}"
                      f"  control {r['control']:5.2f}  "
                      f"{'moved' if r['moved'] else 'STILL'}"
                      f"{' BLIND' if r['blind'] else '     '}"
                      f"   tracked {r['tracked']:+7.1f}")
            if r['moved']:
                last_moving, still_run = total, 0
                continue
            if r['blind']:
                # ⚠ A STEP THAT CANNOT ANSWER DOES NOT GET TO VETO ONE THAT
                # DID. If a NON-blind step has already read STILL, the stop is
                # established and this one is simply silent about it -- and it
                # reads "still" too, so it is not even evidence against.
                # Measured 2026-08-06, hip fire, fifth attempt: 8034 read STILL
                # with predict 33.59 against control 14.61 (ratio 2.30, well
                # clear of the blind floor), then 8343's control jumped to
                # 19.01 and the whole pass was thrown away one step short of
                # its answer. The pass before it had already reached the same
                # stop at 7416.
                if still_run >= 1:
                    print(f"    [!] {label}: stop confirmed by ONE clean still "
                          f"step and one blind one (predict {r['predict']:.2f} "
                          f"vs control {r['control']:.2f}) — weaker than "
                          f"{TRAVEL_CONFIRM} clean stills, recorded as such")
                    return last_moving, rows
                # Nothing here could have shown motion whether or not there was
                # any, and nothing before it said the view had stopped.
                # Declaring the stop would be a number that looks like a
                # measurement and is not one.
                #
                # ⚠ "Turn and run it again" is NOT the advice at the TOP stop:
                # straight up is sky on every bearing, so no facing fixes it.
                # There the lever is the STEP -- `predict` is proportional to
                # it, so a bigger step lifts the signal above the idle sway.
                print(f"    [!] {label}: the view is somewhere with nothing in "
                      f"it — a {step}-count step would only change this "
                      f"picture by {r['predict']:.2f} against a control of "
                      f"{r['control']:.2f}, so 'it did not move' means nothing "
                      f"here. Going up, raise --step; going down, turn to face "
                      f"something with texture.")
                return None, rows
            still_run += 1
            if still_run >= TRAVEL_CONFIRM:
                return last_moving, rows
        print(f"    [!] {label}: no stop within {max_counts} counts")
        return None, rows

    def measure_travel(self, posture='standing', step=None, store=True,
                       verbose=True, on_stop=None, tries=FOCUS_TRIES):
        """L1 — The stop walk, with the foreground taken back if it is lost.
        A MINUTE of visible ratcheting; travel() reads the stored answer.

        ⚠ A LOST FOREGROUND VOIDS THE PASS, IT DOES NOT PAUSE IT. The counts
        this walk adds up are only a distance because the game consumed them,
        and an unfocused game consumes nothing -- so the run restarts from the
        stop rather than picking up where the exception landed. Resuming would
        produce a travel short by however long the foreground was gone, and it
        would look like a perfectly ordinary measurement.

        Worth doing at all because the walk holds the screen for minutes:
        two of the first four hip-fire runs on 2026-08-06 died to a foreground
        this could have taken back, one of them AFTER the up pass had already
        succeeded.
        """
        for attempt in range(max(1, tries)):
            try:
                return self._travel_passes(posture, step, store, verbose,
                                           on_stop)
            except FocusLost:
                print(f"  [!] lost the foreground during the pitch walk "
                      f"(attempt {attempt + 1}/{tries}) — every count since "
                      f"the last stop went nowhere, so this restarts rather "
                      f"than resumes")
                if not ensure_focus(countdown_s=0):
                    print("  [!] could not take the foreground back")
                    raise
                self.flush(6)
        return 0

    def _travel_passes(self, posture='standing', step=None, store=True,
                       verbose=True, on_stop=None):
        """Raw counts between the two pitch stops, for this sight. 0 if unknown.

        Read the TRAVEL block at the top of this file for the signal and the
        constants. Three passes, and the third is nearly free because the view
        has to come back down anyway: up brackets the travel in (up, up+step],
        down brackets it independently from the other end, and the intersection
        is narrower than either. It also says whether the measurement repeats.

        ⚠ MEASURES WHATEVER POSTURE THE CHARACTER IS ALREADY IN, and `posture`
        only says which one that is, for the cache and the file. It does not
        change stance — the caller has done that, and doing it here would move
        the view out from under the pass in progress.

        ⚠ AND IT IS PER POSTURE, WHICH IS NOT OBVIOUS. Standing and crouching
        both travel 3450 counts, to the count; PRONE TRAVELS 1450, because the
        game clips how far a prone character can look. So the travel is not a
        property of the sight alone, and half of prone's travel is only level
        if the clipping took the same amount off each end — which nothing in
        prone's own counts can say. See goto_midline, which says so out loud
        rather than quietly aiming somewhere.

        Once per posture per run; goto_midline() afterwards is two mouse moves.
        """
        if self.mouse is None:
            return 0
        # A magnified sight buys less rotation per count, so both the step and
        # the give-up bound have to grow with it or a 4x needs 165 steps to
        # cross its travel and gives up before it does. max(1.0, ...) because
        # pitch_scale is a model and this is the one place it is allowed to
        # help: it only ever sets the RESOLUTION of a measurement whose answer
        # comes from the stops.
        scale = max(1.0, self.pitch_scale())
        step = int(step or round(TRAVEL_STEP * scale))
        cap = int(TRAVEL_MAX * scale)

        # Every pass is kept, whether or not it produced an answer, because a
        # failure here is diagnosed from the per-step readings and nothing
        # else: "no part of the range tracks" is four words for thirty
        # measurements, and on 2026-08-05 that sent an hour into theorising
        # about the scene while the actual numbers were sitting unprinted.
        self.travel_detail = {'step': step, 'cap': cap, 'sight': self.sight,
                              'posture': posture}
        d = self.travel_detail

        # `on_stop(label)` fires the moment the view is AGAINST a stop, which
        # is the only moment a photograph of it can be taken -- and the
        # photographs are the only check a human has on whether the midpoint
        # really is level. tools/probe_pitch_range.py passes one; harvest does
        # not.
        def reached(label):
            if on_stop:
                on_stop(label)

        # ONE SHOVE, NOT A WALK. Everything after this counts FROM the bottom
        # stop, and the walk that used to get there contributed nothing but
        # time: its count was read only for `is None`, because it starts from
        # wherever the view happens to be and so measures nothing. That was a
        # third of the stepping, and stepping is what an operator watching this
        # on 2026-08-06 objected to -- "低头抬头，不要一开一卡，直接到位不行吗".
        # home_to_clamp is the module's most-trusted primitive for exactly this
        # reason: push further than the travel could be and the view is against
        # the stop, wherever it started, with nothing to measure.
        # `cap` rather than home_to_clamp's CLAMP_PUSH: the push has to clear a
        # travel this method does not know yet, and CLAMP_PUSH * pitch_scale is
        # 12380 counts against a hip travel of roughly 10500 -- a margin of
        # 1.18x on a model that is the reason for this whole measurement. cap
        # is the give-up bound, so if the travel is beyond it nothing here was
        # going to work anyway.
        self._send(0, cap)
        time.sleep(CLAMP_SETTLE_S)
        reached('bottom')
        up_ok, d['up'] = self.to_stop(-1, step, 'up  ', cap, verbose)
        reached('top')
        if up_ok is None:
            return 0
        # ⚠ A FAILED DOWN PASS DOES NOT DISCARD THE MEASUREMENT, and it used to.
        # The answer is the up pass -- the direction goto_midline travels -- and
        # the down pass is its CONTROL, so losing the control costs the check,
        # not the number. Measured 2026-08-06 on the first hip-fire run: the up
        # pass reached the top stop cleanly at 7416 and the down pass then went
        # blind at 5562 in the bare ground near the bottom, which is the failure
        # this whole signal is documented to have there. Returning 0 threw away
        # a good measurement because a check could not be run.
        down_ok, d['down_out'] = self.to_stop(+1, step, 'down', cap, verbose)
        d['up_last_moving'], d['down_last_moving'] = up_ok, down_ok
        if down_ok is None:
            print("  [!] the down pass could not reach the bottom stop, so the "
                  "up/down asymmetry is UNKNOWN for this sight — the travel "
                  "below is the up pass alone, unchecked")

        # ⚠ THE ANSWER IS THE UP PASS. The down pass is its control, not half
        # of an average, and that distinction is not pedantry: goto_midline
        # only ever rises, from the bottom stop, so the quantity it needs is
        # "commanded counts to go UP one whole travel" -- and the game does not
        # consume counts identically in the two directions. The up/down
        # asymmetry in K re-measured 5.37% on 2026-08-05 (the 53% before it was
        # an artefact of a UI being on screen). Over a 3450-count travel that
        # is ~185 counts, nearly two steps, so the two brackets come apart and
        # averaging them puts the midline ~86 counts low -- a real aim error
        # that nothing downstream would report.
        #
        # So the down pass answers a different and better question: does the
        # count ruler read the same in both directions? A disagreement wider
        # than one step IS that asymmetry, measured, per sight, for free.
        # ⚠ THE BRACKET RUNS DOWN FROM up_ok, NOT UP FROM IT, and this file and
        # tools/probe_pitch_range.py both had it the other way round. Write out
        # what the two observations actually say:
        #
        #   the step ENDING at up_ok moved  =>  before it the view was short of
        #                                       the stop  =>  travel > up_ok-step
        #   the next step did not move      =>  the view was already at the
        #                                       stop      =>  travel <= up_ok
        #
        # So travel is in (up_ok - step, up_ok]. The old (up_ok, up_ok + step]
        # is a whole step high and cannot be right: it puts the stop beyond a
        # position the view is already known to have reached.
        #
        # The point estimate is the TOP of that interval rather than its
        # middle, and that rests on a measurement rather than on taste: the
        # game consumes ~60% of commanded counts, while `predict` is computed
        # from the full commanded step, so a final partial step registers as
        # moved once it delivers about half of what it could. The residual past
        # up_ok-step is therefore ~half a step on average, which lands the
        # estimate back at up_ok.
        #
        # It checks out against the one number measured a completely different
        # way. The live standing run reported 3450 under the old bracket, i.e.
        # up_ok = 3400; half of 3400 is 1700, and tools/fit_pitch_level.py
        # --from-spawn measured level at 1700 counts above the bottom stop by
        # descending from the view the GAME sets at spawn. Under the old
        # bracket that agreement was 25 counts off.
        lo, hi = up_ok - step, up_ok
        gap = None if down_ok is None else abs(up_ok - down_ok)
        # None, not False: "the two passes disagree" and "there was only one
        # pass" are different states and a boolean cannot hold both. Stored as
        # null so a later reader has to notice.
        agreed = None if gap is None else gap <= step
        if agreed is False:
            print(f"  [!] the passes disagree by {gap} counts: up says the "
                  f"travel is in ({up_ok - step}, {up_ok}], down says "
                  f"({down_ok - step}, {down_ok}] — "
                  f"{100.0 * gap / max(up_ok, 1):.1f}% apart. Aiming on the up "
                  f"pass, which is the direction goto_midline travels.")
        travel = int(hi)
        d.update(travel=travel, bracket=[lo, hi], agreed=agreed, gap=gap,
                 down_bracket=(None if down_ok is None
                               else [down_ok - step, down_ok]))
        if travel < MIN_TRAVEL:
            print(f"  [!] travel came out {travel} counts — the game was not "
                  f"taking input, or the screen was frozen")
            return 0
        self._travel[posture] = travel
        print(f"  travel[{self.sight}/{posture}]: {travel} counts stop to stop "
              f"(bracket {lo}..{hi}, step {step}), so the midline is "
              f"{travel // 2} above the bottom stop")
        if store:
            self._store_travel(posture, travel, lo, hi, step, agreed)
        return travel

    def _store_travel(self, posture, travel, lo, hi, step, agreed):
        """Write it next to the pitch offsets it is measured in the same units
        as. Starts from what is on disk, and merges at BOTH levels: this
        process knows about one sight and one posture, and a write that rebuilt
        the file from that would delete every other -- the same mistake _store
        in tools/fit_pitch_level.py made, where a standing+crouching run
        silently dropped the stored prone."""
        try:
            os.makedirs(os.path.dirname(_TRAVEL_PATH), exist_ok=True)
            try:
                keep = json.load(open(_TRAVEL_PATH, encoding='utf-8'))
            except Exception:
                keep = {}
            per_sight = dict(keep.get(self.sight) or {})
            per_sight[posture] = {
                'counts': travel, 'K': self.K, 'step': step,
                'bracket': [lo, hi], 'agreed': agreed,
                'down_bracket': self.travel_detail.get('down_bracket'),
                'gap': self.travel_detail.get('gap')}
            keep[self.sight] = per_sight
            keep['_measured'] = (
                'Commanded mouse counts to travel from the bottom pitch stop '
                'to the top one, keyed <sight>.<posture>, by control/aim.py '
                'measure_travel(). The midpoint is level BECAUSE PUBG clamps '
                'pitch symmetrically at straight down and straight up -- which '
                'holds standing and crouching (both 3450 through a red dot, to '
                'the count) and is NOT established for prone, whose travel is '
                'clipped to 1450. `counts` is the UP pass, because that is the '
                'direction goto_midline travels; `down_bracket` is the same '
                'travel measured coming back down, and `gap` between them is '
                'the up/down asymmetry in the count ruler -- `agreed` false '
                'means it exceeded one step.')
            json.dump(keep, open(_TRAVEL_PATH, 'w', encoding='utf-8'),
                      indent=2)
        except Exception as e:
            print(f'  [!] could not store the pitch travel: {e}')

    def travel(self, posture='standing', measure=False, sight=None):
        """R — Stop-to-stop travel for this sight and posture: cached or
        stored. Drives nothing at the default; measure_travel() is the L1
        that actually walks it. ⚠ `measure=True` turns this into that L1.

        ⚠ `measure` DEFAULTS TO FALSE, so a caller that is doing something else
        never stops to build a ruler. Measuring is a stepped walk into both
        stops -- a minute of the view visibly ratcheting -- and it does not
        belong in the middle of a calibration run. The operator watching one on
        2026-08-06 put it plainly: "这个不需要啊，已经有了。不用测量了。测量单独
        做。" Run tools/probe_pitch_range.py --sight <sight> once, and every run
        after that reads the number.
        """
        sight = sight or self.sight
        key = (sight, posture)
        if self._travel.get(key):
            return self._travel[key]
        got = (PITCH_TRAVEL.get(sight) or {}).get(posture)
        if isinstance(got, dict):
            got = got.get('counts')
        if got:
            self._travel[key] = int(got)
            return self._travel[key]
        return self.measure_travel(posture) if measure else 0

    def goto_midline(self, posture='standing', measure=False, sight=None,
                     set_ref=True):
        """L2 — THE aim. Bottom stop, then halfway up. Counts risen, or 0.

        ⚠ goto_level() and goto_pitch_centre() answer this same question and
        are BOTH disproven (2026-08-05). They have the more obvious names, so
        read their first lines before reaching for either.

        TWO MOVES. One shove into the bottom stop, one rise to the midline,
        nothing measured in between -- the travel is a per (sight, posture)
        constant that was measured once and is read from disk. `measure=True`
        would build it here and is off by default; see travel().

        ⚠ `sight` IS THE STATE THE CHARACTER IS IN RIGHT NOW, not the optic the
        magazine will be fired through, and the caller is expected to pass
        'hipfire' and to have put the character there (control.gun.ensure_hip).
        Pitch is a property of the character and the two clamps are the same
        two clamps through every optic; only counts-per-degree changes, and
        that conversion is the unvalidated model that put the view in the
        ground. Positioning always from the hip deletes it: one measured hip
        travel serves every scope, instead of one measurement per scope.

        ⚠ AND THEN `set_ref` MUST BE FALSE, because the tracker patches are
        per sight -- a reference grabbed from the hip describes a picture this
        magazine will not be fired through. The caller raises the sight first
        and calls set_reference() itself.

        This is the aim every magazine should start from, and it replaces both
        older answers to that question:

          goto_pitch_centre  aimed at the middle of the TRACKABLE band, which
                             follows whatever the character happens to be
                             facing -- 800..2200 one run and 100..2100 the
                             next -- so two cells aimed at different pitches
                             and neither recorded which.
          goto_level         rose by a STORED offset measured through one
                             optic in one posture and converted with
                             pitch_scale(). Standing red dot was fine; every
                             magnified attempt on 2026-08-05 landed at the
                             character's feet, each time on a different unit
                             error.

        Half the travel needs no band, no stored posture offset and no
        magnification model. It needs one thing instead: that the two stops sit
        symmetrically about level.

        ⚠ THAT IS MEASURED FOR STANDING AND CROUCHING AND NOT FOR PRONE. Those
        two travel the same 3450 counts, which is what a symmetric ±90 looks
        like; prone travels 1450, so the game clips it, and nothing in prone's
        own counts says whether the clip took the same amount off each end. The
        midline is still the most repeatable place to start a prone magazine --
        it is an offset from a hard stop, so every prone cell aims at the same
        pitch -- but it is not established to be level, and this says so once
        per posture rather than letting a reader assume it.
        """
        t = self.travel(posture, measure=measure, sight=sight)
        if not t:
            return 0
        if posture not in ('standing', 'crouching') and \
                posture not in self._warned_midline:
            self._warned_midline.add(posture)
            print(f"  [.] {posture}: the midline is repeatable but not shown "
                  f"to be level — the game clips this posture's pitch and the "
                  f"clip is not known to be symmetric")
        half = int(round(t * MIDLINE_FRAC))
        # NOT home_to_clamp(), and not pitch_scale either. That one shoves a
        # fixed CLAMP_PUSH multiplied by the magnification model; here the
        # travel is KNOWN, in the same counts this is about to command, so the
        # guaranteed overshoot is just a multiple of it. One less place for the
        # model to be wrong, and a real margin instead of CLAMP_PUSH's 4000
        # against a red-dot travel of 3400.
        self._send(0, int(t * CLAMP_OVERSHOOT))
        time.sleep(CLAMP_SETTLE_S)
        # Two moves, and nothing measured between them: the stop says where the
        # view started and half the travel says how far to go. Stepping would
        # only buy the correlator's opinion of a distance already known -- an
        # opinion it cannot give anyway over the bare ground this passes over.
        self._send(0, -half)
        time.sleep(CLAMP_SETTLE_S)
        self.pitch_centre = half
        self.pitch_band = None
        self.pending_pitch = 0.0
        self.flush(4)
        if set_ref:
            self.set_reference()
        return half

    def goto_level(self, posture):
        """DEAD — use goto_midline(). Rises by a STORED offset, and every
        magnified attempt on 2026-08-05 landed at the character's own feet.

        Kept only because deleting it belongs with the calibration/sweep.py
        facade that is its sole caller. It takes no level in `pixi run surface`
        for that reason: an entry that fits no level is either dead or never
        decided, and this one is dead.

        Replaces the ground-to-sky scan for every cell that has a stored
        offset. The scan cost ~20 seconds of very visible sweeping, and worse,
        it was not repeatable: it kept whatever pitch happened to have texture,
        which depends on what the character is facing, so it came back
        100..1900 one run and 800..2200 the next. Two cells aimed at different
        pitches measure different recoil, and nothing recorded which was which.

        The bottom clamp is absolute, so an offset from it is absolute. See
        calibration/artifacts/pitch/pitch_range.json for where the numbers come from.

        ⚠ IT DOES NOT PUT THE VIEW LEVEL WHEN THE STORED NUMBER CAME FROM THE
        TRACKABLE BAND. That method centred on wherever the texture was, i.e.
        the ground, and a human watching a VSS run on 2026-08-05 said so:
        "看的是地面". tools/fit_pitch_level.py --from-spawn replaces it by
        measuring down from the view the GAME sets at spawn, which is level by
        construction. Entries carrying `from: spawn` are the good ones.

        ⚠ PREFER NOT USING THIS AT ALL. Entering the match gives a level view
        for free -- the game sets it -- so a cell that spawns, leaves the pitch
        alone, puts the sight up and fires needs no offset, no clamp and no
        magnification conversion. This path exists for runs that cannot
        respawn between cells; every attempt to make it land level on a
        magnified sight on 2026-08-05 failed on a different unit error, while
        "just respawn" was correct the first time and was what the operator
        had asked for.
        """
        up = (PITCH_RANGE.get(posture) or {}).get('level_up')
        if not up:
            return 0
        self.home_to_clamp(+1)
        self._send(0, -int(up * self.pitch_scale()))
        time.sleep(CLAMP_SETTLE_S)
        self.pitch_centre = int(up)
        self.pitch_band = None
        self.flush(4)
        self.set_reference()
        return int(up)

    def calibrate_pitch(self, step=BAND_STEP):
        """L1 — Find the band of pitch where the view can actually be measured.
        It DRIVES: home_to_clamp then a stepped rise. (Tagged R for an hour on
        2026-08-07 because it reads like a question; `pixi run surface --check`
        exists because that mistake is not detectable by reading the name.)

        ⚠ A DIAGNOSTIC, NOT AN AIM. The band follows the character's heading,
        so its middle is not a repeatable place to start a magazine — that is
        goto_midline(). This answers "can the correlator see anything here".

        From the bottom clamp, rise in steps and compare what the view did
        against what it was told to do. Near either clamp the answer is
        nothing — bare ground below, blank sky above, no texture for the
        correlator either way. In between the two agree.

        The middle of THAT band is where every magazine should start. Not the
        geometric middle of the travel: the useful quantity is not "far from
        both stops", it is "far from both stops AND measurable", and only one
        of those can be observed.
        """
        self.home_to_clamp(+1)                       # bottom stop
        rises, usable = 0, []
        while rises < BAND_MAX:
            prev = self.tracker.slice_frame(self.grab())
            self._send(0, -step)
            got = self.track_still(timeout_s=0.7, still_s=0.10, prev=prev)
            rises += step
            if abs(got) > step * BAND_TRACK_FRAC:
                usable.append(rises)
        if not usable:
            print("  [!] no part of the pitch range tracks — is the view "
                  "somewhere featureless, or the game not taking input?")
            self.tracking_lost = True
            return 0
        lo, hi = usable[0], usable[-1]
        # Low in the band, not the middle. Recoil only ever pushes the view UP,
        # so half the band was being spent on headroom the measurement can
        # never use. Prone is where that bites: the trackable band is ~600
        # counts there against 1500-1800 standing, and a bare UMP45 magazine
        # peaked 236 counts above the aim with 300 to spare -- sitting the
        # last third of the burst within 64 counts of the edge, where the
        # band test itself only demands that tracking recover half the motion.
        # A cell measured there under-reads its recoil and nothing flags it.
        self.pitch_centre = int(lo + (hi - lo) * AIM_FRAC)
        # Kept so a cell can record where it was aiming. The band depends on
        # what the character happens to be facing -- 800..2200 in one run and
        # 100..2100 in the next -- and the measured recoil moves with it, so a
        # cell that cannot say where it aimed cannot be compared with another.
        self.pitch_band = (lo, hi)
        print(f"  pitch: measurable from {lo} to {hi} counts above the bottom "
              f"clamp; aiming at {self.pitch_centre}")
        return self.pitch_centre

    def goto_pitch_centre(self):
        """NOT AN AIM — reaim()'s internals wearing a public name. It targets
        the middle of the TRACKABLE band, and that band follows the character's
        heading (800..2200 one run, 100..2100 the next). Use goto_midline().

        ⚠ The paragraph below used to open "Every magazine starts here". That
        stopped being true when goto_midline replaced it, and the sentence
        stayed — which is the whole reason first lines now carry a level.

        A burst walks the view a few hundred
        counts and the walk accumulates, so starting from wherever the last
        one finished eventually means firing into the clamp — where the view
        stops moving, the weapon measures unusually mild, and nothing reports
        a problem.

        Homing is what makes this immune to the drift it is correcting. Going
        back to a remembered picture depends on the running total that got you
        there and on a correlation that wraps past half a patch; going back to
        a hard stop depends on neither.
        """
        if not self.pitch_centre:
            self.calibrate_pitch()
        self.home_to_clamp(+1)
        # One move, open loop. There is nothing to measure: the clamp says
        # where the view is and pitch_centre says how far to go, so stepping
        # and re-measuring the way home only buys the correlator's opinion of
        # a distance already known — twenty-odd tracked steps per magazine for
        # an answer that was in hand before the first one.
        self._send(0, -int(self.pitch_centre))
        time.sleep(RECENTER_SETTLE_S)
        self.pending_pitch = 0.0
        return int(self.pitch_centre)

    def reaim(self):
        """L2 — Dispatch between the two ways back to the magazine's start.
        Not a third strategy: use_homing off calls recenter(), on calls
        goto_pitch_centre(). Call those directly if you know which you want.

        ⚠ AND THE `on` BRANCH GOES TO A DISPROVEN AIM. goto_pitch_centre
        targets the middle of the trackable band, which follows the
        character's heading -- goto_midline() replaced it for exactly that
        reason, and this dispatcher was never re-pointed. harvest no longer
        comes through here; calibration/sweep.py's calibrate_combo still does,
        with harness/adapter.py defaulting use_homing to True.

        Two ways, and the switch is use_homing (off by default).

        OFF — measure the way back to the cell's own reference. Cheap and
        invisible: the view barely moves and nothing sweeps the screen.

        ON — home against the pitch clamp and rise to the middle of the
        measurable band. Immune to drift in a way the other cannot be, since
        it returns to a hard stop rather than to a running total. It is also
        obtrusive: mapping the band whips the view from the ground to the sky
        and back at the start of every posture, which is unpleasant to sit
        behind and slow. Worth it for a long unattended sweep; not worth it
        for a few magazines with someone watching.
        """
        if self.use_homing:
            back = self.goto_pitch_centre()
            self.set_reference()
            return back
        return self.recenter()
