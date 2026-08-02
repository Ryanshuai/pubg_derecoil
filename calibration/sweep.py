"""Unattended recoil calibration sweep for the training range.

Works through {weapons} x {postures}, firing magazines and measuring the
residual left over by the current curve. Everything the game needs — ADS,
posture, reload recovery — is driven from the Pico; the only human step is
swapping weapons, and even that goes away once SpawnerSwitcher lands.

    python calibration/sweep.py --weapons ar --mags 3
    python calibration/sweep.py --weapons aug,m416 --postures standing
    python calibration/sweep.py --weapons smg --resume

SHADOW MODE: results are written to JSONL and printed as suggested factors.
Nothing is written back to weapon_scales.json / posture_scales.json — closed
loop learning reinforces its own errors, so a human approves first.

Why each piece exists (all learned the hard way, see
docs/recoil_observer_design.md):

  * compensation stays ON while measuring — AUG's pattern is ~1358 counts over
    40 rounds = 2100 px at K=1.55, so uncompensated the view ends up in the
    sky where there is no texture to correlate. Compensated, what remains IS
    the residual.
  * attachments are read via Tab before every weapon — weapon_scales.json is
    calibrated WITH compensator+grip, so a bare-gun pattern over-compensates
    by 30%.
  * PUBG auto-reloads and drops out of ADS doing so; measuring the next
    magazine from the hip would apply the scoped K (1.55) to hip-fire motion
    (0.50).
  * posture is verified by the icon detector rather than assumed from
    keypresses, because a missed toggle silently mislabels a whole run.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np
import win32gui
import win32process

from config import (HUD_REGIONS, RECOIL_SIGHT_PROFILES,
                    RECOIL_K_DEFAULT_SCOPED, TAB_PIXEL_THRESH,
                    TAB_COUNT_MIN, TAB_COUNT_MAX)
from detector.attachment_detector import AttachmentDetector
from detector.cropper import make_grabber
from detector.posture_detector import PostureDetector
from detector.view_tracker import ViewTracker, MagazineRecorder
from detector.weapon import Weapon, WEAPON_RPM, ar, smg, mg
from detector.weapon_template_detector import TabWeaponDetector
from press.pico_mouse import (get_mouse, HID_KEY_TAB, HID_KEY_C, HID_KEY_Z)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weapon_switcher import get_switcher

cv2.setNumThreads(1)
HERE = os.path.dirname(os.path.abspath(__file__))

# ── timing (wall clock; frame counts are unreliable because DXGI re-serves
#    the previous frame while the screen is idle) ──
AMMO_THRESH = 200
AMMO_CHANGED = 0.02
EMPTY_STATIC_S = 0.55     # ammo frozen this long while firing => magazine out
MIN_FIRE_S = 0.8
MAX_FIRE_S = 9.0
RELOAD_TIMEOUT_S = 9.0
RELOAD_STATIC_S = 0.35    # counter must hold this long before the mag is ready
RELOAD_MIN_S = 2.0        # ...and if it never visibly moved, wait at least this
SETTLE_AFTER_RELOAD_S = 1.8   # counter refills mid-animation; gun is not ready
ADS_SETTLE_S = 0.5
ADS_WATCH_S = 2.5         # how long to watch for the icon after a right-click;
                          # measured ~0.85 s idle and slower right after firing
POSTURE_WATCH_S = 1.5     # same, for the C/Z animation
TAB_OPEN_S = 0.55
TAB_CLOSE_S = 0.35
POSTURE_SETTLE_S = 0.6
RECENTER_SETTLE_S = 0.25   # let the view stop before the next burst

# Closed-loop recentring. See Rig.recenter.
RECENTER_TOL = 8        # counts; below this the next burst does not care
RECENTER_TRIES = 4
RECENTER_STEP = 60      # counts per move, so no single frame pair wraps
TRACK_TIMEOUT_S = 2.0   # post-fire recovery has always finished well inside
TRACK_STILL_S = 0.20    # this long under tol_px counts as stopped
TRACK_STILL_PX = 1.0
TRACK_MIN_S = 0.12      # USB command -> rendered frame; before this, "not
                        # started" and "finished" look identical
# Fraction of the correlator's half-patch range an absolute reading may use
# before it is refused as possibly wrapped. See Rig.absolute_offset.
ABS_TRUST_FRAC = 0.6
# How far the absolute reading may disagree with the running total before the
# reading is treated as wrapped rather than the total as drifted.
ABS_AGREE_COUNTS = 45
PROBE_COUNTS = 30       # for tracking_confirmed()

# Homing against the pitch clamp. The clamp is the only absolute position the
# game offers: a running total drifts and a correlation wraps, but "the game
# refuses to rotate further" is the same place every time.
CLAMP_PUSH = 4000       # one open-loop shove, comfortably past the travel
CLAMP_SETTLE_S = 0.35
BAND_STEP = 100         # rise per probe while mapping the measurable band
BAND_MAX = 3000         # stop rising; the travel is well inside this
BAND_TRACK_FRAC = 0.5   # observed/commanded above this counts as measurable
POSTURES = ('standing', 'crouching', 'prone')

# One implementation, in press/pointer.py, because it guards the mouse calls
# that live there too. Re-exported under the old names so importers of this
# module keep working.
from press.pointer import (game_focused, raise_game, ensure_focus,  # noqa: E402
                           FocusKeeper, focus_keeper, GAME_EXES)


class Rig:
    """Owns the capture, the Pico and the detectors for one sweep."""

    def __init__(self, sight):
        prof = RECOIL_SIGHT_PROFILES.get(sight, {})
        self.sight = sight
        self.K = prof.get('K', RECOIL_K_DEFAULT_SCOPED)
        self.tracker = ViewTracker(patch_xs=prof.get('patch_xs'))
        self.mouse = get_mouse()
        self.att_det = AttachmentDetector()
        self.gun_det = TabWeaponDetector()
        self.posture_det = PostureDetector()

        regions = dict(self.tracker.regions())
        for k in ('ammo', 'type', 'posture', 'gun_name_1', 'gun_name_2'):
            regions[k] = HUD_REGIONS[k]
        for k, v in HUD_REGIONS.items():
            if k.startswith('att_'):
                regions[k] = v
        self.grabber, self.paced = make_grabber(regions)
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

    def close(self):
        try:
            self.mouse.click(buttons=0x00, duration_ms=0)
            self.mouse.set_recoil_enabled(False)
        except Exception:
            pass
        self.grabber.close()

    # ── screen reads ──

    def grab(self):
        return self.grabber.grab()

    def flush(self, n=8):
        for _ in range(n):
            self.grabber.grab()

    def ammo_sig(self, frame):
        g = cv2.cvtColor(frame['ammo'], cv2.COLOR_BGR2GRAY)
        return cv2.threshold(g, AMMO_THRESH, 255, cv2.THRESH_BINARY)[1] > 0

    def tab_open(self, frame):
        g = cv2.cvtColor(frame['type'], cv2.COLOR_BGR2GRAY)
        n = int((g > TAB_PIXEL_THRESH).sum())
        return TAB_COUNT_MIN <= n <= TAB_COUNT_MAX

    def read_posture(self, timeout_s=0.8, gap_s=0.08):
        """Watch the posture icon until it reads, or time out.

        Deadline rather than a sample count: the icon is absent during the ADS
        animation, the inventory fade and mid-posture-change, and how long that
        lasts is not a constant — measured ~0.85 s to appear after a click, and
        slower right after firing. A fixed handful of quick samples reads None
        while the animation is still running, and the caller then toggles ADS
        back off, which never converges.

        A None must never be treated as a posture: toggling on an unknown state
        is how an unattended run walks itself into a posture nobody asked for.
        """
        t0 = time.perf_counter()
        while True:
            self.flush(2)
            p = self.posture_det.classify({'posture': self.grab()['posture']})
            if p:
                return p
            if time.perf_counter() - t0 >= timeout_s:
                return None
            time.sleep(gap_s)

    def dump(self, tag):
        """Save the crops behind a failed decision, so a human can see what
        the detector saw instead of guessing from a one-line error."""
        try:
            frame = self.grab()
            for k in ('posture', 'type', 'ammo'):
                if k in frame:
                    cv2.imwrite(os.path.join(HERE, f'fail_{tag}_{k}.png'),
                                frame[k])
            print(f"      [dbg] wrote fail_{tag}_*.png")
        except Exception as e:
            print(f"      [dbg] dump failed: {e}")

    def ensure_inventory_closed(self, tries=3):
        """An inventory left open hides the posture icon AND swallows C/Z,
        which looks exactly like a broken detector."""
        for _ in range(tries):
            self.flush(2)
            if not self.tab_open(self.grab()):
                return True
            self.mouse.key(HID_KEY_TAB, 60)
            time.sleep(TAB_CLOSE_S)
        self.flush(2)
        return not self.tab_open(self.grab())

    def ensure_inventory_open(self, tries=3):
        """Tab is a toggle, so pressing it blind lands in the wrong state half
        the time. Watch instead."""
        for _ in range(tries):
            self.flush(2)
            if self.tab_open(self.grab()):
                return True
            self.mouse.key(HID_KEY_TAB, 60)
            time.sleep(TAB_OPEN_S)
        self.flush(2)
        return self.tab_open(self.grab())

    def read_loadout(self, slot=1):
        """One Tab cycle returns both the weapon name and its attachments."""
        self.ensure_inventory_closed()
        self.mouse.key(HID_KEY_TAB, 60)
        time.sleep(TAB_OPEN_S)
        frame = self.grab()
        ok = self.tab_open(frame)
        gun = att = None
        if ok:
            names = self.gun_det.classify(frame)
            gun = names[slot - 1] or ''
            att = self.att_det.classify(frame).get(slot)
        if not self.ensure_inventory_closed():
            print("      [!] inventory would not close")
        return (gun, att) if ok else (None, None)

    # ── state control ──

    def ensure_ads(self, tries=3):
        """Get into ADS, using the posture icon as the ADS indicator.

        The icon only renders while aiming, so "icon visible" == "in ADS".
        That matters because right-click is a TOGGLE here: clicking it without
        knowing the current state lands in the wrong one half the time, and
        there is no other reliable read of ADS from the HUD.

        Each click is then WATCHED to completion rather than sampled once.
        Clicking again while the previous ADS animation is still playing just
        toggles back out, so an impatient version of this oscillates forever —
        which is exactly what it did before."""
        if self.read_posture(timeout_s=ADS_SETTLE_S) is not None:
            return True
        for _ in range(tries):
            self.mouse.click(buttons=0x02, duration_ms=60)
            t0 = time.perf_counter()
            if self.read_posture(timeout_s=ADS_WATCH_S) is not None:
                dt = time.perf_counter() - t0
                if dt > ADS_SETTLE_S:
                    print(f"      [ads] icon appeared {dt:.2f}s after click")
                return True
        return False

    def ensure_posture(self, target, tries=4):
        """Toggle until the icon detector agrees. Keypresses alone are not
        trusted: one dropped toggle would mislabel an entire run.

        Requires ADS (the icon does not render from the hip) and a closed
        inventory (which hides the icon and swallows C/Z)."""
        if not self.ensure_inventory_closed():
            print("      [!] inventory stuck open — C/Z would be swallowed")
            self.dump('inventory')
            return False
        if not self.ensure_ads():
            print("      [!] no posture icon — not in ADS? cannot verify")
            self.dump('ads')
            return False
        for _ in range(tries):
            cur = self.read_posture(timeout_s=POSTURE_WATCH_S)
            if cur is None and self.ensure_ads(tries=2):
                # Going prone can drop ADS, and the icon goes with it — that
                # reads identically to "detector broken", so re-aim and re-read
                # before believing it.
                cur = self.read_posture(timeout_s=POSTURE_WATCH_S)
            if cur == target:
                return True
            if cur is None:
                # Never toggle on an unknown state — a blind C/Z here is how an
                # unattended run ends up measuring a posture nobody asked for.
                print(f"      [!] posture unreadable (want {target})")
                self.dump('posture')
                return False
            print(f"      posture {cur} -> {target}")
            if target == 'prone':
                self.mouse.key(HID_KEY_Z, 60)
            elif target == 'crouching':
                # from prone, Z stands up first; C then crouches
                self.mouse.key(HID_KEY_Z if cur == 'prone' else HID_KEY_C, 60)
            else:  # standing
                self.mouse.key(HID_KEY_Z if cur == 'prone' else HID_KEY_C, 60)
            time.sleep(POSTURE_SETTLE_S)
        cur = self.read_posture(timeout_s=POSTURE_WATCH_S)
        if cur != target:
            print(f"      [!] gave up at {cur!r}, wanted {target}")
            self.dump('posture')
        return cur == target

    def set_reference(self):
        """Remember where the cell is aiming, for absolute_offset()."""
        self.ref_patches = self.tracker.slice_frame(self.grab())
        self.pending_pitch = 0.0
        self.tracking_lost = False

    def absolute_offset(self):
        """Counts between the view now and the cell's reference, or None.

        The incremental integral cannot catch an error in its own starting
        belief — drive pending_pitch to zero and the view stays wherever that
        belief was wrong by. This can, because it compares against the
        reference itself rather than against a running total.

        It only works CLOSE to the reference: past half a patch the
        correlation wraps and answers confidently in the wrong direction. That
        is exactly why it runs after the incremental loop and not instead of
        it — by then the view is supposed to be within a few counts, so if the
        comparison is out of range that is itself the finding.
        """
        if getattr(self, 'ref_patches', None) is None:
            return None
        cur = self.tracker.slice_frame(self.grab())
        m = self.tracker.measure_pair(self.ref_patches, cur)
        if m is None or m.out_of_range:
            return None
        off = m.dy / self.K
        # A wrapped reading is not noisy, it is confident and wrong — the peak
        # comes back a whole patch out, and every patch wraps together so the
        # cross-patch agreement still looks healthy. There is no way to tell
        # from the reading itself, so anything not comfortably inside the
        # range is refused rather than believed. Measured: half a patch is
        # 128 px, which at K=1.55 is 82 counts.
        if abs(m.dy) > ABS_TRUST_FRAC * self.tracker.patch_h / 2:
            return None
        return off

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
        self.mouse.move(0, direction * CLAMP_PUSH)
        time.sleep(CLAMP_SETTLE_S)

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
        self.pitch_centre = (lo + hi) // 2
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
        if not getattr(self, 'pitch_centre', 0):
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
            if not m.out_of_range:
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
                      f"reference — more than "
                      f"{ABS_TRUST_FRAC * self.tracker.patch_h / 2 / self.K:.0f}"
                      f" counts away, or the scene changed. Running total says "
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
            self._move_tracked(off)
            total += int(round(off))
        else:
            print(f"        [!] view will not come back ({off:+.0f} counts "
                  f"off) — at the pitch clamp? the next magazine measures "
                  f"short and looks fine doing it")
        return total

    def enter_ads(self):
        self.mouse.click(buttons=0x02, duration_ms=60)
        time.sleep(ADS_SETTLE_S)

    # ── one magazine ──

    def fire_magazine(self):
        rec = MagazineRecorder(
            self.tracker,
            human_fn=getattr(self.mouse, 'human_totals', None))
        self.mouse.click(buttons=0x01, duration_ms=int(MAX_FIRE_S * 1000))
        t0 = time.perf_counter()
        prev, last_change, steps = None, t0, 0
        while True:
            now = time.perf_counter()
            if now - t0 > MAX_FIRE_S:
                break
            frame = self.grab()
            rec.push(now, frame)
            sig = self.ammo_sig(frame)
            if prev is not None and float(np.mean(sig != prev)) > AMMO_CHANGED:
                last_change = now
                steps += 1
            prev = sig
            if (now - t0) > MIN_FIRE_S and (now - last_change) > EMPTY_STATIC_S:
                break
        self.mouse.click(buttons=0x00, duration_ms=0)
        # last_change is the last round leaving the magazine. Everything after
        # it is the camera drifting back toward the pre-fire aim, which is real
        # but happens after the bullets are already gone -- folding it into the
        # residual flatters the total while telling you nothing about where the
        # rounds went.
        return rec, time.perf_counter() - t0, steps, last_change

    def wait_reload(self):
        """PUBG reloads by itself; we only wait it out (and it exits ADS).

        Watches for the counter to leave its empty-magazine reading and then
        hold still, rather than waiting for it to match a snapshot taken back
        at the start of the cell. The snapshot goes stale — the standing cell
        lost four of its five magazines twice running to a reference that
        never came back — and "moved, then settled" needs no reference at all.
        """
        empty = self.ammo_sig(self.grab())
        t0 = time.perf_counter()
        prev, stable_since = None, None
        while True:
            now = time.perf_counter()
            if now - t0 >= RELOAD_TIMEOUT_S:
                break
            sig = self.ammo_sig(self.grab())
            if prev is not None and float(np.mean(sig != prev)) < AMMO_CHANGED:
                if stable_since is None:
                    stable_since = now
            else:
                stable_since = None
            prev = sig
            if stable_since is None or now - stable_since <= RELOAD_STATIC_S:
                continue
            # Normally the counter visibly climbs back and holds. But the
            # magazine can refill inside the 0.55 s the fire loop spends
            # confirming the gun is empty — a quickdraw magazine is that fast —
            # and then `empty` is already the full reading and no change ever
            # comes. Settled plus long enough is the same evidence.
            if float(np.mean(sig != empty)) > AMMO_CHANGED or \
                    now - t0 > RELOAD_MIN_S:
                time.sleep(SETTLE_AFTER_RELOAD_S)
                return now - t0
        self.dump('reload')
        return None


def analyse(res, K, bullet_interval_s, fire_end_ts=None):
    dy = np.asarray(res.dy, dtype=float)
    ts = np.asarray(res.ts, dtype=float)
    if len(ts) < 2:
        return None

    # Screen motion is recoil - compensation - hand, all in mouse counts. The
    # compensation is what we set out to grade, so only the hand has to come
    # back out; without the Pico reporting it this is a zero vector and the
    # measurement silently assumes a still hand.
    human = (np.asarray(res.human_dy, dtype=float) if res.human_dy
             else np.zeros_like(dy))

    oor = np.asarray(res.out_of_range, dtype=bool)
    if len(oor) != len(dy):                     # replayed or truncated result
        oor = np.zeros(len(dy), dtype=bool)

    # Where the view actually ended up, over the WHOLE recording rather than
    # the burst — post-fire recovery moves it too, and recentring has to undo
    # the real position, not the analytically interesting part of it.
    drift = float(np.nansum(np.where(oor, np.nan, dy)) / K)

    # Cut at the last round fired, plus the one bullet interval its own recoil
    # needs to play out. Every per-frame array is sliced together — slicing
    # some and not others misaligns the out-of-range mask, and a misaligned
    # mask fails silently.
    if fire_end_ts is not None:
        keep = ts <= fire_end_ts + bullet_interval_s
        if keep.sum() >= 2:
            dy, human, ts, oor = dy[keep], human[keep], ts[keep], oor[keep]

    ts = ts - ts[0]
    counts = dy / K + human

    # Past half a patch the correlation peak wraps, so a frame flagged
    # out-of-range is not merely imprecise, it is wrong by a whole patch —
    # 83 counts at K=1.55. Dropping the frame costs only the ~1 count of
    # residual it carried; keeping it cost 266 counts on a magazine where the
    # hand moved fast enough to hit the limit three times.
    counts = np.where(oor, np.nan, counts)
    nb = int(ts[-1] / bullet_interval_s) + 1
    per_bullet = []
    for b in range(nb):
        m = (ts >= b * bullet_interval_s) & (ts < (b + 1) * bullet_interval_s)
        per_bullet.append(float(np.nansum(counts[m])) if m.any() else 0.0)
    return {
        'n_frames': len(dy), 'span_s': float(ts[-1]),
        'cum_px': float(np.nansum(dy)),
        'cum_counts': float(np.nansum(counts)),
        'per_bullet_counts': [round(v, 3) for v in per_bullet],
        'max_abs_frame_px': float(np.nanmax(np.abs(dy))),
        'n_rejected': int(np.sum(res.n_rejected)),
        'n_out_of_range': int(np.sum(res.out_of_range)),
        'n_dropped_oor': int(np.sum(oor)),
        'view_drift_counts': drift,
        'n_low_gate': int(np.sum(res.gates)),
        # Net is what was removed from the residual; abs is how much hand
        # motion happened at all. A small net with a large abs means the hand
        # wandered and came back — the correction still holds, but the run is
        # noisier than a still one.
        'human_counts': float(np.nansum(human)),
        'human_abs_counts': float(np.nansum(np.abs(human))),
        'mean_mad': float(np.mean(res.mad)) if res.mad else float('nan'),
    }


def calibrate_combo(rig, weapon, posture, mags, log):
    """Measure one (weapon, posture) cell. Returns a summary dict or None."""
    # Loadout first: opening Tab drops ADS, and posture can only be verified
    # from ADS (the icon does not render from the hip).
    gun_seen, att = rig.read_loadout()
    if gun_seen is None:
        print("    [!] inventory would not open — cannot read attachments")
        return None
    if gun_seen and gun_seen != weapon:
        print(f"    [!] expected {weapon}, inventory says {gun_seen!r}")
        return None

    w = Weapon()
    w.set('name', weapon)
    w.set('posture', posture)
    w.set('muzzle', (att or {}).get('muzzle', ''))
    w.set('grip', (att or {}).get('grip', ''))
    w.set_seq()
    if not len(w.t_s):
        print(f"    [!] no pattern for {weapon}")
        return None
    pattern_counts = float(np.sum(w.dy_s))

    rig.mouse.upload_pattern(w.dx_s, w.dy_s, w.t_s, w.bullet_interval_s)
    rig.mouse.set_recoil_enabled(True)
    time.sleep(0.3)

    # Enters ADS as a side effect — the posture icon is the ADS indicator.
    if not rig.ensure_posture(posture):
        print(f"    [!] could not reach posture {posture}")
        return None
    # In ADS and before the first round: home to the measurable middle of the
    # pitch travel, then take the reference there.
    rig.flush(6)
    rig.goto_pitch_centre()
    rig.set_reference()

    rows = []
    for i in range(mags):
        if not focus_keeper().ok(f'{weapon}/{posture} mag {i}'):
            break
        if i > 0:                    # auto-reload drops us to the hip
            if not rig.ensure_ads():
                print("      [!] could not re-enter ADS after reload")
                break
            rig.goto_pitch_centre()      # same absolute aim every magazine
            rig.set_reference()

        rec, fire_s, steps, fire_end = rig.fire_magazine()
        if steps == 0:
            print(f"      mag {i}: no rounds fired (reload still running?) "
                  f"— skipped")
            time.sleep(1.5)
            continue
        a = analyse(rec.finish(), rig.K, w.bullet_interval_s, fire_end)
        if a is None:
            continue
        a.update(mag=i, fire_s=round(fire_s, 2), ammo_steps=steps,
                 fps=round(rec.effective_fps(), 1))
        rows.append(a)
        rig.pending_pitch += a['view_drift_counts']
        print(f"      mag {i}: {fire_s:.2f}s {steps:3d} steps  "
              f"residual {a['cum_counts']:+8.1f} counts "
              f"({100*a['cum_counts']/pattern_counts:+6.1f}% of pattern)  "
              f"rej={a['n_rejected']} oor={a['n_out_of_range']} "
              f"hand={a['human_counts']:+.0f}/{a['human_abs_counts']:.0f}")
        if rig.wait_reload() is None:
            print("      [!] auto-reload did not finish — stopping combo")
            break

    if not rows:
        return None

    cc = np.array([r['cum_counts'] for r in rows])
    ratio = 1.0 + cc.mean() / pattern_counts
    summary = {
        'type': 'combo', 'weapon': weapon, 'posture': posture,
        'sight': rig.sight, 'K': rig.K, 'n_mags': len(rows),
        'attachments': att, 'scale': w.scale,
        'posture_factor': w.get_posture_factor(),
        'pattern_counts': pattern_counts,
        'residual_counts_mean': float(cc.mean()),
        'residual_counts_std': float(cc.std()),
        'residual_pct': float(100 * cc.mean() / pattern_counts),
        'implied_ratio': float(ratio),
        'mags': rows,
        'ts': datetime.now().isoformat(timespec='seconds'),
    }
    log.write(json.dumps(summary) + '\n')
    log.flush()
    print(f"    => residual {cc.mean():+.1f} +- {cc.std():.1f} counts "
          f"({summary['residual_pct']:+.1f}%)   implied factor {ratio:.3f}")
    return summary


def load_done(path):
    """(weapon, posture) pairs already measured, for --resume."""
    done = set()
    if os.path.exists(path):
        for line in open(path):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get('type') == 'combo':
                done.add((r['weapon'], r['posture']))
    return done


def expand_weapons(spec):
    groups = {'ar': sorted(ar), 'smg': sorted(smg), 'mg': sorted(mg),
              'all': sorted(ar | smg | mg)}
    out = []
    for tok in spec.split(','):
        tok = tok.strip()
        if not tok:
            continue
        out.extend(groups.get(tok, [tok]))
    seen, uniq = set(), []
    for x in out:
        if x in WEAPON_RPM and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def main():
    ap = argparse.ArgumentParser(
        description='Unattended recoil calibration sweep (shadow mode).')
    ap.add_argument('--weapons', default='ar',
                    help="'ar', 'smg', 'mg', 'all', or names: aug,m416")
    ap.add_argument('--postures', default=','.join(POSTURES))
    ap.add_argument('--sight', default='red_dot',
                    choices=sorted(RECOIL_SIGHT_PROFILES.keys()))
    ap.add_argument('--mags', type=int, default=3,
                    help='magazines per (weapon, posture) cell')
    ap.add_argument('--switcher', default='manual',
                    choices=('manual', 'spawner'))
    ap.add_argument('--out', default='')
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--countdown', type=int, default=6)
    args = ap.parse_args()

    weapons = expand_weapons(args.weapons)
    postures = [p.strip() for p in args.postures.split(',') if p.strip()]
    bad = [p for p in postures if p not in POSTURES]
    if bad:
        print(f"[!] unknown posture(s): {bad}")
        return 1
    if not weapons:
        print("[!] no weapons selected")
        return 1

    out = args.out or os.path.join(
        HERE, f"sweep_{args.sight}_{datetime.now().strftime('%m%d_%H%M')}.jsonl")
    done = load_done(out) if args.resume else set()

    total = len(weapons) * len(postures)
    print(f"sweep     : {len(weapons)} weapons x {len(postures)} postures "
          f"= {total} cells, {args.mags} mags each")
    print(f"weapons   : {', '.join(weapons)}")
    print(f"postures  : {', '.join(postures)}")
    print(f"sight     : {args.sight}")
    print(f"out       : {out}")
    if done:
        print(f"resume    : {len(done)} cells already done, skipping")
    print(f"est. time : ~{total * args.mags * 9 / 60:.0f} min of firing "
          f"plus weapon swaps")
    print("\n[SHADOW MODE] nothing is written back to the scale files.\n")

    rig = Rig(args.sight)
    print(f"grabber   : {type(rig.grabber).__name__} (paced={rig.paced})  "
          f"K={rig.K:.4f}  {len(rig.tracker.xs)} patches")

    switcher = get_switcher(
        args.switcher, verify_fn=lambda: (rig.read_loadout()[0] or ''))

    print("\n>>> Taking the foreground. Stand still and aim at something with "
          "texture — the recoil is measured off it.")
    if not ensure_focus(countdown_s=args.countdown, label='the sweep'):
        print("[!] ABORT: game not focused, and could not take the "
              "foreground. Is PUBG running?")
        rig.close()
        return 1
    time.sleep(0.6)              # the game ignores the first frames after a
    keeper = focus_keeper()      # foreground change

    log = open(out, 'a')
    log.write(json.dumps({
        'type': 'header', 'sight': args.sight, 'K': rig.K,
        'patch_xs': list(rig.tracker.xs), 'patch': rig.tracker.patch,
        'band_y': rig.tracker.band_y, 'mags': args.mags,
        'ts': datetime.now().isoformat(timespec='seconds'),
    }) + '\n')

    results = []
    try:
        for wi, weapon in enumerate(weapons):
            todo = [p for p in postures if (weapon, p) not in done]
            if not todo:
                print(f"[{wi+1}/{len(weapons)}] {weapon}: all done, skipping")
                continue

            print(f"\n[{wi+1}/{len(weapons)}] {weapon}")
            if not switcher.switch_to(weapon):
                print(f"    skipped — could not equip {weapon}")
                continue

            for posture in todo:
                if not keeper.ok(f'{weapon}/{posture}'):
                    raise KeyboardInterrupt
                print(f"    posture: {posture}")
                s = calibrate_combo(rig, weapon, posture, args.mags, log)
                if s:
                    results.append(s)
            rig.ensure_posture('standing')
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        rig.close()
        switcher.close()
        log.close()

    report(results, out)
    return 0


def report(results, out):
    if not results:
        print("\nno cells completed")
        return
    print("\n" + "=" * 88)
    print("SUGGESTED FACTORS (shadow mode — not written to disk)")
    print("=" * 88)
    print(f"{'weapon':<10}{'posture':<11}{'mags':>5}{'residual %':>12}"
          f"{'implied':>9}{'now':>8}{'suggest':>9}  note")
    print("-" * 88)
    for r in sorted(results, key=lambda x: (x['weapon'], x['posture'])):
        ratio = r['implied_ratio']
        if r['posture'] == 'standing':
            now, sug, what = r['scale'], r['scale'] * ratio, 'scale'
        else:
            now = r['posture_factor']
            sug = now * ratio
            what = f"posture[{r['posture']}]"
        flag = ''
        if abs(r['residual_pct']) > 40:
            flag = '  <-- large, check the run'
        if r['residual_counts_std'] > abs(r['residual_counts_mean']) * 0.5:
            flag += '  <-- noisy'
        print(f"{r['weapon']:<10}{r['posture']:<11}{r['n_mags']:>5}"
              f"{r['residual_pct']:>+11.1f}%{ratio:>9.3f}{now:>8.3f}"
              f"{sug:>9.3f}  {what}{flag}")
    print(f"\n  raw -> {out}")
    print("  review, then apply with a separate step — nothing was written.")


if __name__ == '__main__':
    sys.exit(main())
