"""The rig: capture, Pico and detectors for one calibration run.

    from calibration.sweep import Rig
    rig = Rig('red_dot')

Rig is an ASSEMBLY SHELL. It builds the frame source, the mouse and the
detectors once, and hands them to the three closed loops in control/ —
ViewDriver, GunDriver and FireDriver. It decides nothing about what to measure;
that belongs to whoever is running the experiment.

⚠ THIS FILE USED TO BE THE SWEEP. `calibrate_combo` fired a cell, binned the
view motion by round against the ammo counter, and wrote an EMA-blended curve
back to disk — 350 lines of the coordinate MODEL.md retired on 2026-08-08. The
replacement is calibration/collect_timed.py (fire into the sample store) and
calibration/fit_time_curve.py (one full refit over everything ever stored).

What survived is what the new path imports: collect_timed.py line 43 is
`from calibration.sweep import Rig`.
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

from config import (HUD_REGIONS, RECOIL_SIGHT_PROFILES,
                    RECOIL_K_DEFAULT_SCOPED, SCREEN_W, SCREEN_H)
from detector.attachment_detector import AttachmentDetector
from capture.cropper import FocusLost, ScreenBuffer
from detector.posture_detector import PostureDetector
from detector.view_tracker import ViewTracker
from detector.weapon import Weapon, WEAPON_RPM, ar, smg, mg
from detector.weapon_template_detector import TabWeaponDetector
from press.pico_mouse import get_mouse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

cv2.setNumThreads(1)
HERE = os.path.dirname(os.path.abspath(__file__))
# Runs are measurements, not source: they land under docs/ with the rest of
# what this repo has measured, never next to the script that wrote them.
RUNS = os.path.join(os.path.dirname(HERE), 'calibration', 'artifacts', 'recoil', 'runs')
# Where GunDriver.dump() puts the crops behind a failed decision.
FAIL_DIR = os.path.join(os.path.dirname(HERE), 'calibration', 'artifacts', 'fail')

# Warn when a magazine's view excursion eats this much of the headroom above
# the aim. Past it the burst is finishing where tracking is already only
# required to recover half the motion, so the recoil reads low.
HEADROOM_WARN_FRAC = 0.6
# Re-exported, not redefined -- tools/probe_pitch_range.py imports it from
# here. The author is config.POSTURES.
from config import POSTURES                                 # noqa: E402,F401

# Re-exported for the tools that reach them through this module. Re-checked
# 2026-08-06, after the openers moved to control.session.ensure_ready:
# `focus_keeper` still has one importer (harvest.py), `game_focused` is used
# below as ScreenBuffer's focus_fn, and `ensure_focus` went to zero on both
# counts -- so it is gone from here rather than forwarded for nobody. Anything
# that wants it goes to control.focus, and anything opening a run wants
# ensure_ready instead.
from control.focus import game_focused, focus_keeper  # noqa: E402
# The three closed loops this rig is made of. None of them is about recoil —
# they are "point the view", "get the character into a known state" and "empty
# a magazine and report what the game said", which is why they are in control/
# and this file only decides WHICH cells to measure.
from control.aim import ViewDriver
from control.gun import GunDriver
from control.fire import FireDriver, MAX_FIRE_S  # noqa: F401  (tools import it)
# Module level is safe: control/session.py imports only control.focus up here
# and pulls the four control objects in inside the function, precisely so the
# things it gates can import it back.
from control.session import ensure_ready


class Rig:
    """Owns the capture, the Pico and the detectors for one sweep."""

    def __init__(self, sight, prefer_dxgi=True):
        prof = RECOIL_SIGHT_PROFILES.get(sight, {})
        self.sight = sight
        # See _build_grabber: MODEL.md's collection path owns DXGI for the
        # burst and leaves this one on GDI, because there is only one
        # duplication interface per output per process.
        self.prefer_dxgi = prefer_dxgi
        self.K = prof.get('K', RECOIL_K_DEFAULT_SCOPED)
        # `patch` as well as the columns: a profile squeezed for space needs
        # narrower patches to fit non-overlapping ones, and overlapping patches
        # vote together in the median that measure_pair rejects outliers
        # against. ViewTracker has taken the width since it was written; only
        # this line and its twin in set_sight never passed it on.
        self.tracker = ViewTracker(patch_xs=prof.get('patch_xs'),
                                   patch=prof.get('patch'),
                                   patch_h=prof.get('patch_h'))
        self.mouse = get_mouse()
        self.att_det = AttachmentDetector()
        self.gun_det = TabWeaponDetector()
        self.posture_det = PostureDetector()
        # Not wrapped in a try. The crosshair is half of the ADS gate, and the
        # gate is what decides whether a whole run's numbers mean anything --
        # the posture icon alone once passed a burst fired from the hip, in
        # third person, and the run read as clean. A fallback here is a silent
        # downgrade of the only thing standing between a bad burst and the
        # curve, so a detector that will not build stops the run instead.
        from detector.ads_detector import AdsDetector, CROP_R
        self.ads_det = AdsDetector()
        self.ads_region = (SCREEN_H // 2 - CROP_R, SCREEN_W // 2 - CROP_R,
                           2 * CROP_R, 2 * CROP_R)
        try:
            from detector.fire_mode_detector import FireModeDetector
            # No device: the torch head went on 2026-08-08 and this is a
            # RandomForest now. The `import torch` that used to sit here was
            # the last one on any measurement path.
            self.fire_det = FireModeDetector()
        except Exception as e:
            print(f"  [!] no fire-mode detector ({e}) — cannot tell a gun that "
                  f"spawned in single fire from one in full auto")
            self.fire_det = None
        try:
            from detector.ammo_detector import AmmoDetector
            self.ammo_det = AmmoDetector()
        except Exception as e:
            print(f"  [!] no ammo counter ({e}); falling back to watching the "
                  f"ammo region flicker, which cannot count rounds")
            self.ammo_det = None

        self._build_grabber()
        # The Rig is the frame source all three read through, which is why it
        # can hand them `self`. It owns the detectors and the Pico; they own
        # the loops.
        self.view = ViewDriver(self.tracker, self.mouse, self, self.K, sight)
        self.gun = GunDriver(self, self.mouse, self.posture_det, self.ads_det,
                             fire_det=self.fire_det, gun_det=self.gun_det,
                             att_det=self.att_det, dump_dir=FAIL_DIR)
        # `gun` is not decoration here: a magazine's numbers are meaningless
        # without knowing whether the shots were aimed, so the fire loop polls
        # ADS all the way through the burst.
        self.fire = FireDriver(self, self.mouse, self.tracker,
                               ammo_det=self.ammo_det, gun=self.gun)
        # Which posture the pitch band was mapped for. Set by callers that
        # re-map per posture (harvest); pure bookkeeping, nobody here reads it.
        self.band_posture = None


    def close(self):
        # The trigger first: an exception escaping mid-burst leaves it held,
        # and a disarmed firmware does not stop the character shooting.
        try:
            self.mouse.click(buttons=0x00, duration_ms=0)
        except Exception as e:
            print(f'  [!] could not release the trigger: {e}', flush=True)
        self.fire.disarm()
        self.frames.close()

    # ── what USED to be here ──
    #
    # 26 forwarders and 7 property aliases, deleted 2026-08-07. They existed
    # so an earlier split "cost no call sites", and the price turned out to be
    # the declaration: control/ carries a level and a warning on the first
    # line of every public method, and a forward with no docstring of its own
    # showed the caller none of it. `Rig.goto_level` read exactly as
    # legitimate as `Rig.goto_midline` while being dead and disproven.
    #
    # Reach through the driver that owns the loop: rig.view / rig.gun /
    # rig.fire. `grab`, `full` and `flush` stay because they are not
    # forwarders -- Rig IS the frame source those three drivers hold.
    # ── screen reads ──

    def grab(self):
        return self.frames.grab()

    def full(self, frame=None, only=None, copy=False):
        """The banded crops blitted back to screen coordinates.

        The Rig is the frame source it hands to ViewDriver, GunDriver and
        FireDriver, so anything they can ask a ScreenBuffer for has to be
        forwarded here or it is simply missing. This one was: GunDriver.
        read_loadout cuts the gun name plates by pixel coordinate, got the
        {name: crop} dict instead, and died with a KeyError on a pair of
        slices -- three minutes into the first unattended run.
        """
        return self.frames.full(frame, only=only, copy=copy)

    def flush(self, n=8):
        # 8, not ScreenBuffer's FLUSH_FRAMES of 3. Every call site here is
        # explicit (flush(6) / flush(4) / flush(2)), so the default is only
        # ever a fallback -- but it is the fallback this rig was tuned with.
        self.frames.flush(n)

    # ── the view: forwarded to control/aim.py ──
    #
    # Kept as forwards rather than made callers say `rig.view.recenter()`,
    # because the state is SHARED and mutated from both sides: harvest does
    # `rig.view.pending_pitch += a['view_drift_counts']` after every magazine, and
    # a copy of that number on the Rig would drift away from the one the
    # recentring loop is closing on. One owner, one value, aliases forward.
    #
    # New code should reach for `rig.view` directly; these exist so the split
    # cost no call sites.


    # ── the character: forwarded to control/gun.py ──


    # How far to tilt when the posture icon cannot be read. Big enough to put
    # different scenery behind a 66 px HUD crop, small enough to stay well
    # inside the measurable band -- goto_pitch_centre lands in the middle of
    # it, and calibrate_pitch measures the band in the thousands of counts.
    POSTURE_NUDGE_COUNTS = 300

    def nudge_view(self):
        """Move what is BEHIND the HUD, for a detector that cannot read it.

        ViewDriver.turn() is the named entry point for exactly this -- "moves
        in order to CHANGE WHAT IS BEHIND THE PANEL", where the landing place
        is not merely unchecked but irrelevant. Its own warning ("does not
        update pending_pitch, so a recenter() afterwards is measuring from a
        belief this call already invalidated") is the same constraint as the
        one below, reached from the other side.

        Only ever passed to ensure_posture when homing is on, and that is the
        whole safety argument: this destroys the running total the view driver
        keeps, and homing does not use one -- goto_pitch_centre returns to the
        pitch clamp, a hard stop, immediately afterwards. With homing off the
        cell measures against a reference this would silently invalidate, so
        no nudge is offered and the cell fails honestly instead.
        """
        self.view.turn(0, -self.POSTURE_NUDGE_COUNTS, settle_s=0.25)

    def stir(self, ms=120):
        """One step forward and back — the only thing in a run that MOVES.

        A named forward for the same reason as read_posture below: the
        layering lint parses imports, so `rig.mouse.key(HID_KEY_W, ...)`
        from calibration would be exactly the reach it cannot see, and this
        repo has paid for two of those already.

        See GunDriver.stir for what it is testing and what would retire it.
        """
        return self.gun.stir(ms)


    def ensure_posture(self, target, tries=4):
        return self.gun.ensure_posture(
            target, tries,
            nudge=self.nudge_view if self.view.use_homing else None)


    # ── the magazine: forwarded to control/fire.py ──
    #
    # ammo_debug_dir is a property rather than a copy, so setting it here
    # actually reaches the fire loop that reads it. The comment used to say
    # harvest sets it; nothing in the repo ever did, so the branch behind it
    # was unreachable from the day it was written. --ammo-debug sets it now.


    def arm(self, weapon):
        # ⚠ `no_comp` SPLITS THE TWO TERMS OF true = comp + residual, which is
        # the only way to ask which of them a bimodal cell lives in. Every cell
        # so far has been measured with compensation ON, so a residual that
        # comes back in two modes could be two recoils (impossible -- the gun
        # is a constant) or two deliveries of the compensation, and no run that
        # has both switched on at once can tell those apart. With this set the
        # residual IS the recoil.
        #
        # The pattern is still UPLOADED, only not enabled: an armed and a
        # disarmed run then differ in exactly one thing, which is what makes
        # them comparable. (This used to say "so curve_bullets() and the bin
        # edges agree" -- there are no bins and no bullet index any more; the
        # samples sit on a clock.) arm() then disarm() rather than
        # reaching for mouse.upload_pattern here -- Rig owns the Pointer and
        # `pixi run layering` cannot see a HAL member touched through a
        # high-level object, which is how this file would grow the next
        # parallel driver. Nothing is fired between the two calls.
        if getattr(self, 'no_comp', False):
            n = self.fire.arm(weapon)
            if not self.fire.disarm():
                raise RuntimeError('--no-comp asked for compensation OFF and '
                                   'the firmware would not confirm it; every '
                                   'magazine after this would be measured '
                                   'compensated and labelled otherwise')
            return n
        return self.fire.arm(weapon)


    def _regions(self):
        """Every window this rig reads, for the current tracker patches."""
        regions = dict(self.tracker.regions())
        for k in ('ammo', 'type', 'posture', 'fire_mode',
                  'gun_name_1', 'gun_name_2'):
            regions[k] = HUD_REGIONS[k]
        if self.ads_region:
            regions['crosshair'] = self.ads_region
        for k, v in HUD_REGIONS.items():
            if k.startswith('att_'):
                regions[k] = v
        return regions

    def _build_grabber(self):
        """(Re)open the banded grabber for the current tracker patches.

        ⚠ `prefer_dxgi=False` IS NOT A DOWNGRADE, it is how the two capture
        paths coexist. DXGI allows one duplication interface per output per
        process, so MODEL.md's collection path -- which owns a
        DXGISyncGrabber over the patches for the whole run -- cannot also have
        this one on DXGI. GDI can, and everything read through here (ammo,
        fire mode, posture, attachments, gun names) is event-triggered rather
        than per-frame: 6 ms once or twice a magazine against 1.72 ms on every
        frame of the burst. Putting them in one DXGI box costs the opposite
        trade -- the bounding box stretches from the patches at y=592 to the
        HUD at y=1366 and the per-frame grab goes 1.72 -> 3.90 ms, which at a
        6.06 ms frame budget is the difference between sampling at the refresh
        rate and sampling at half of it.

        focus_fn is passed, and it is the one behaviour change worth stating:
        grab() now RAISES when the game is not in the foreground, instead of
        handing back the frozen picture PUBG leaves on screen. A run that keeps
        grabbing through a lost foreground does not fail -- it measures a still
        image and reports a suspiciously clean residual. Callers that fire have
        to catch FocusLost; see calibrate_combo.
        """
        self.frames = ScreenBuffer(self._regions(),
                                   prefer_dxgi=self.prefer_dxgi,
                                   focus_fn=game_focused)

    def set_sight(self, sight):
        """Switch which optic the measurement assumes. True if it changed.

        Not just K. Each profile carries its own patch columns, because what
        the scope body hides differs: the red dot leaves seven usable columns
        across the screen, the VSS's integral PSO-1 leaves three, and putting
        a patch on the scope tube measures the tube rather than the world. So
        the tracker and the grabber are rebuilt, not just the constant.

        This exists because the VSS cannot be measured any other way — it
        carries a fixed 4x and takes no sight, so a run pinned to the red dot
        analyses a PSO-1 view with the red dot's K — the two differ by about
        20% and the sign of the answer does not survive it: MINUS 482 counts.
        (Both values live in RECOIL_SIGHT_PROFILES and are not repeated here.)
        """
        if sight == self.sight:
            return False
        prof = RECOIL_SIGHT_PROFILES.get(sight)
        if not prof:
            print(f"  [!] no sight profile {sight!r} — staying on {self.sight}")
            return False
        self.sight = sight
        self.K = prof.get('K', RECOIL_K_DEFAULT_SCOPED)
        self.tracker = ViewTracker(patch_xs=prof.get('patch_xs'),
                                   patch=prof.get('patch'),
                                   patch_h=prof.get('patch_h'))
        # The regions move with the patches; set_regions swaps them without
        # dropping the buffer or reopening a capture backend.
        self.frames.set_regions(self._regions())
        # The view driver holds the old optic's tracker, K and reference, and
        # all three are wrong now: a 4x buys 3.3x the rotation per count.
        self.view.retune(self.tracker, self.K, sight)
        # AND SO DOES THE FIRE DRIVER, which took its tracker by value in
        # __init__ and had nothing to update it. That omission is why the VSS
        # has never produced a single cell.
        #
        # The names are positional -- ViewTracker.names() is
        # [f'recoil_{i}' for i in range(len(self.xs))] -- so a stale 7-patch
        # tracker reading a freshly-rebuilt 3-region frame asks for recoil_3
        # through recoil_6, slice_frame() gets None from frame.get(), and
        # MagazineRecorder.push() drops EVERY frame. Measured 2026-08-04:
        # four VSS magazines, `0 tracked samples` on all four, analyse()
        # refusing at its first gate.
        #
        # It is invisible on every other weapon because vss_pso1 is the only
        # profile with a different patch COUNT: switching back to red_dot
        # restores seven and the stale reference happens to match again. So
        # the bug presents as "the VSS cannot be measured" rather than as
        # anything about sights.
        self.fire.tracker = self.tracker
        print(f"  sight -> {sight}  K={self.K:.4f}  "
              f"{len(self.tracker.xs)} patches")
        return True

    # The table itself lives on GunDriver, which is what reads it. Aliased
    # rather than copied: two dicts named the same thing would drift, and the
    # symptom of drifting would be the MG3 measured against the wrong one of
    # its two automatic fire rates — a wrong number, not an error.
    FIRE_MODE_FOR = GunDriver.FIRE_MODE_FOR

# ⚠ EVERYTHING BELOW THIS POINT WENT ON 2026-08-08. calibrate_combo, the
# resume bookkeeping, the CLI and the report were the bullet-bucket sweep:
# fire a cell, bin the view motion by round, compare against the curve, write
# an EMA-blended curve back. MODEL.md retired the coordinate they were written
# in, and calibration/collect_timed.py + fit_time_curve.py are what replaced
# them — samples in, one full refit out, no rounds and no bins.
#
# WHAT STAYED IS THE ASSEMBLY SHELL, and it stayed because it is the thing the
# new path builds on: `from calibration.sweep import Rig` is line 43 of
# collect_timed.py. Rig owns the one Pointer and the detectors and hands them
# to the three control/ drivers — the same job robot.py does for the live
# loop. tools/check_layering.py's rule 6 ledger says so with a predicate.
#
# So this file is now one class and nothing else, and `pixi run sweep` is
# gone with the CLI it ran.
