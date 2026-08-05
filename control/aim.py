"""Where the view is pointing, driven closed-loop against the screen.

    IMPORTANT: everything here is `move, measure, repeat until the screen
    agrees`. Nothing believes a mouse command landed because it was sent.

This is the half of recoil calibration that has nothing to do with recoil: put
the view somewhere known, prove it got there, and notice when it did not. Aim
assist wants exactly the same thing, which is why it lives here rather than
inside the calibration rig it was written for.

    from control.aim import ViewDriver
    view = ViewDriver(tracker, mouse, frames, K=1.5474, sight='red_dot')
    view.set_reference()          # remember where this cell aims
    ...                           # fire a magazine
    view.recenter()               # and put it back, provably

`frames` is anything with `grab()` and `flush(n)` — see detector/cropper.py.
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

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from config import RECOIL_SIGHT_PROFILES
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


def _load_pitch_range():
    """Level, per posture, as counts above the bottom pitch clamp.

    Measured once by tools/probe_pitch_range.py. Empty falls back to the
    ground-to-sky band scan in calibrate_pitch().

    It lives under docs/pitch/ next to the per-step screenshots it was read
    off, because it is measured FACT, not calibration policy -- same reason
    weapon_rpm.json sits in docs/recoil/ and kit_facts.json in docs/compat/.
    """
    path = os.path.join(_ROOT, 'docs', 'pitch', 'pitch_range.json')
    try:
        return {k: v for k, v in json.load(open(path, encoding='utf-8')).items()
                if not k.startswith('_')}
    except Exception:
        return {}


PITCH_RANGE = _load_pitch_range()


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
        # Off: the band scan sweeps the view ground-to-sky at the start of
        # every posture, which is slow and unpleasant to watch. See reaim().
        self.use_homing = False

    @classmethod
    def open_loop(cls, backend='auto', frames=None):
        """A ViewDriver for a caller with nothing to close the loop WITH.

        Only turn(), ads_tap() and the frame passthroughs work on it: there is
        no tracker and no meaningful K, so every measuring method raises. That
        is the intent — a number they produced here would be arithmetic on a
        tracker that does not exist, and this class's whole discipline is that
        an unverified position is not a position.

        The one caller is calibration/capture_ads.py, and its reason is
        circular rather than lazy: it PHOTOGRAPHS the ADS transition so that
        detector/ads_detector.py can be fitted to it. Every loop that could be
        closed around a sight going up needs that detector.

        The Pointer comes from press/pointer.py's strict constructor, so no
        Pico means no run — the reason is written there, and it applies to
        turning the view exactly as it does to the button.
        """
        return cls(None, Pointer.opened(backend), frames, K=1.0)

    @property
    def backend(self):
        """What the moves are actually going out on — 'pico' or 'sendinput'.

        Worth recording next to a capture: a run made on SendInput is not a
        slightly worse run, it is one the game very likely ignored outright,
        and the file has to be able to say which it was.
        """
        return getattr(self.mouse, 'backend', type(self.mouse).__name__)

    def retune(self, tracker, K, sight):
        """The optic changed, so the patches and the scale did too.

        The reference and the pitch centre do NOT survive it: both are stated
        in the old optic's counts, and a 4x scope buys 3.3x the rotation per
        count (see pitch_scale).
        """
        self.tracker, self.K, self.sight = tracker, K, sight
        self.ref_patches = None
        self.pitch_centre = 0

    # ── the frame source, so callers do not have to reach past this object ──

    def grab(self):
        return self.frames.grab()

    def flush(self, n=8):
        self.frames.flush(n)

    def turn(self, yaw, pitch=0, settle_s=0.0):
        """Swing the view, OPEN LOOP, and do not claim to know where it ended.

        The exception to this module's rule, and it is named so it stays an
        exception. Everything else here moves in order to ARRIVE somewhere and
        proves it against the screen. This one moves in order to CHANGE WHAT
        IS BEHIND THE PANEL: calibration/collect_templates.py photographs the
        translucent Tab screen against a dozen different backdrops, and where
        the view lands is not merely unchecked, it is irrelevant — different
        is the whole requirement.

        Callers used to get this by reaching through to `rig.mouse.move`,
        which is worse than it looks: a HAL member pulled out of a high-level
        object is invisible to tools/check_layering.py, because that only
        reads imports. The reach-through kept `calibration/` importing press
        with nothing to grep for.

        Do NOT build a positioning routine on this. It does not update
        pending_pitch, so a recenter() afterwards is measuring from a belief
        this call already invalidated. Use goto_level / recenter / reaim.
        """
        self.mouse.move(int(yaw), int(pitch))
        if settle_s:
            time.sleep(settle_s)

    def ads_tap(self, hold_ms=60, settle_s=0.0):
        """Toggle the sight in or out. OPEN LOOP, and the module's second
        named exception — read turn() above for why they are named.

        THE CLOSED-LOOP VERSION IS control/gun.py's ensure_ads(), and it is
        what a measurement must use. It watches the crosshair until the sight
        is really up, because a whole burst once went out from the hip, passed
        the gate, and reported +588 counts of residual: the analysis applies
        the scoped K of 1.55 to motion that happened at the hip's 0.50.

        This cannot be that, and the reason is circular rather than lazy.
        ensure_ads() reads detector/ads_detector.py, and the only caller here
        is calibration/capture_ads.py, which exists to photograph the
        transition that detector is FITTED TO. Confirming the sight is up with
        the detector under construction is confirming it against itself. So
        nothing is checked, and the caller is the one that must be able to
        live with that — capture_ads does, because a human looks at
        probe.jpg before a run and the report is built on medians across
        views.

        ADS IS A TAP, NOT A HOLD. Holding the right button down is
        hip/shoulder aim and the sight picture never appears; it is the
        RELEASE that switches into it. Run docs/ads/runs/20260801_222936 is 64
        frames of that mistake — iron sights and a red dot came out with
        hip-to-"ADS" differences of 31.45 and 31.48, which was the gun coming
        up rather than a scope. Toggling back out is a SECOND call, and
        leaving it out is worse than it looks: the next thing captured starts
        already scoped in and nothing downstream can tell.

        REFUSED ON A CLOSED-LOOP DRIVER, and that guard is the point of this
        paragraph. A docstring saying "prefer ensure_ads()" is a plea; this is
        a refusal. A ViewDriver holding a tracker was built by sweep.Rig, and
        Rig also builds the GunDriver whose ensure_ads() watches the crosshair
        — so for that caller the closed-loop version is not merely preferable,
        it is already in the room. Without this, the method sits on rig.view
        as a second, silent, unverified ADS path beside the verified one, and
        the two disagree exactly when it matters (the sight did not come up).

        turn() deliberately has NO such guard, and the difference is the whole
        criterion: turn() has no closed-loop equivalent to defer to — changing
        the scenery behind a panel is not a position anyone can verify — so
        every caller may legitimately need it. This one does have an
        equivalent. The rule is "refuse when a verified route exists for you",
        not "refuse because it is open loop".
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
        """Remember where the cell is aiming, for absolute_offset()."""
        self.ref_patches = self.tracker.slice_frame(self.grab())
        self.pending_pitch = 0.0
        self.tracking_lost = False

    def absolute_offset(self, predicted=None):
        """Counts between the view now and the cell's reference, or None.

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
        rotation go as mag / K. Between the red dot (1/1.5474) and the VSS's
        fixed PSO-1 (4/1.875) that is a factor of 3.30.

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
        """Push the view into a pitch stop. Open loop, deliberately.

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
        self.mouse.move(0, int(direction * CLAMP_PUSH * self.pitch_scale()))
        time.sleep(CLAMP_SETTLE_S)

    def track_still(self, timeout_s=TRACK_TIMEOUT_S, still_s=TRACK_STILL_S,
                    tol_px=TRACK_STILL_PX, prev=None, min_s=TRACK_MIN_S):
        """Integrate view motion until it stops. Returns counts moved.

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
            self.mouse.move(0, step)
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
        """Push the view a known amount and check the reading follows.

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
        """Put the view back where the cell started, and prove it did.

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

    def goto_level(self, posture):
        """Home to the bottom stop, then rise to level. Counts, or 0 if unknown.

        Replaces the ground-to-sky scan for every cell that has a stored
        offset. The scan cost ~20 seconds of very visible sweeping, and worse,
        it was not repeatable: it kept whatever pitch happened to have texture,
        which depends on what the character is facing, so it came back
        100..1900 one run and 800..2200 the next. Two cells aimed at different
        pitches measure different recoil, and nothing recorded which was which.

        The bottom clamp is absolute, so an offset from it is absolute. See
        docs/pitch/pitch_range.json for where the numbers come from.

        Texture is now the shooter's problem rather than the aim's: this puts
        the view level and says so if nothing there tracks, instead of quietly
        aiming somewhere else.
        """
        up = (PITCH_RANGE.get(posture) or {}).get('level_up')
        if not up:
            return 0
        self.home_to_clamp(+1)
        self.mouse.move(0, -int(up * self.pitch_scale()))
        time.sleep(CLAMP_SETTLE_S)
        self.pitch_centre = int(up)
        self.pitch_band = None
        self.flush(4)
        self.set_reference()
        return int(up)

    def calibrate_pitch(self, step=BAND_STEP):
        """Find the band of pitch where the view can actually be measured.

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
            self.mouse.move(0, -step)
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
        """Home against the bottom stop, then rise to the measurable middle.

        Every magazine starts here. A burst walks the view a few hundred
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
        self.mouse.move(0, -int(self.pitch_centre))
        time.sleep(RECENTER_SETTLE_S)
        self.pending_pitch = 0.0
        return int(self.pitch_centre)

    def reaim(self):
        """Put the view back where the magazine should start.

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
