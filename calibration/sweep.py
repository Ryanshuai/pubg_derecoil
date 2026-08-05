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

from config import (HUD_REGIONS, RECOIL_SIGHT_PROFILES,
                    RECOIL_K_DEFAULT_SCOPED, SCREEN_W, SCREEN_H)
from detector.attachment_detector import AttachmentDetector
from detector.cropper import FocusLost, ScreenBuffer
from detector.posture_detector import PostureDetector
from detector.view_tracker import ViewTracker
from detector.weapon import Weapon, WEAPON_RPM, ar, smg, mg
from detector.weapon_template_detector import TabWeaponDetector
from press.pico_mouse import get_mouse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weapon_switcher import get_switcher
# The measurement maths, which needs no game and no hardware — so it lives
# apart from the rig that does, and `pixi run analysis` can check it offline.
# Import it from there, not from here: this module is not a re-export point.
from analysis import analyse

cv2.setNumThreads(1)
HERE = os.path.dirname(os.path.abspath(__file__))
# Runs are measurements, not source: they land under docs/ with the rest of
# what this repo has measured, never next to the script that wrote them.
RUNS = os.path.join(os.path.dirname(HERE), 'docs', 'recoil', 'runs')
# Where GunDriver.dump() puts the crops behind a failed decision.
FAIL_DIR = os.path.join(os.path.dirname(HERE), 'docs', 'fail')

# Warn when a magazine's view excursion eats this much of the headroom above
# the aim. Past it the burst is finishing where tracking is already only
# required to recover half the motion, so the recoil reads low.
HEADROOM_WARN_FRAC = 0.6
POSTURES = ('standing', 'crouching', 'prone')

# Re-exported for the tools that reach them through this module. Checked
# 2026-08-03: every `from sweep import` in the repo takes ensure_focus or
# focus_keeper and nothing else, so raise_game / FocusKeeper / GAME_EXES were
# dropped -- they were forwarding for importers that do not exist, while the
# real users go to control.focus directly.
from control.focus import (game_focused, ensure_focus,  # noqa: E402
                           focus_keeper)
# The three closed loops this rig is made of. None of them is about recoil —
# they are "point the view", "get the character into a known state" and "empty
# a magazine and report what the game said", which is why they are in control/
# and this file only decides WHICH cells to measure.
from control.aim import ViewDriver, PROBE_COUNTS, BAND_STEP
from control.gun import GunDriver
from control.fire import FireDriver, MAX_FIRE_S  # noqa: F401  (tools import it)


class Rig:
    """Owns the capture, the Pico and the detectors for one sweep."""

    def __init__(self, sight):
        prof = RECOIL_SIGHT_PROFILES.get(sight, {})
        self.sight = sight
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
            import torch
            self.fire_det = FireModeDetector(
                'cuda' if torch.cuda.is_available() else 'cpu')
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

    # Two diagnostics print these. Aliases rather than copies: set_sight
    # rebuilds the region set underneath and a stale copy would name the wrong
    # backend in the log of the run that changed it.
    grabber = property(lambda s: s.frames.grabber)
    paced = property(lambda s: s.frames.paced)

    def close(self):
        # The trigger first: an exception escaping mid-burst leaves it held,
        # and a disarmed firmware does not stop the character shooting.
        try:
            self.mouse.click(buttons=0x00, duration_ms=0)
        except Exception as e:
            print(f'  [!] could not release the trigger: {e}', flush=True)
        self.fire.disarm()
        self.frames.close()

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
    # `rig.pending_pitch += a['view_drift_counts']` after every magazine, and
    # a copy of that number on the Rig would drift away from the one the
    # recentring loop is closing on. One owner, one value, aliases forward.
    #
    # New code should reach for `rig.view` directly; these exist so the split
    # cost no call sites.

    pending_pitch = property(lambda s: s.view.pending_pitch,
                             lambda s, v: setattr(s.view, 'pending_pitch', v))
    tracking_lost = property(lambda s: s.view.tracking_lost,
                             lambda s, v: setattr(s.view, 'tracking_lost', v))
    pitch_centre = property(lambda s: s.view.pitch_centre,
                            lambda s, v: setattr(s.view, 'pitch_centre', v))
    use_homing = property(lambda s: s.view.use_homing,
                          lambda s, v: setattr(s.view, 'use_homing', v))

    def set_reference(self):
        return self.view.set_reference()

    def absolute_offset(self):
        return self.view.absolute_offset()

    def home_to_clamp(self, direction=+1):
        return self.view.home_to_clamp(direction)

    def track_still(self, **kw):
        return self.view.track_still(**kw)

    def tracking_confirmed(self, probe=PROBE_COUNTS):
        return self.view.tracking_confirmed(probe)

    def recenter(self):
        return self.view.recenter()

    def goto_level(self, posture):
        return self.view.goto_level(posture)

    def calibrate_pitch(self, step=BAND_STEP):
        return self.view.calibrate_pitch(step)

    def goto_pitch_centre(self):
        return self.view.goto_pitch_centre()

    def reaim(self):
        return self.view.reaim()

    # ── the character: forwarded to control/gun.py ──

    def ensure_ads(self, tries=3):
        return self.gun.ensure_ads(tries)

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

    def ensure_posture(self, target, tries=4):
        return self.gun.ensure_posture(
            target, tries,
            nudge=self.nudge_view if self.use_homing else None)

    def ensure_fire_mode(self, weapon, tries=6):
        return self.gun.ensure_fire_mode(weapon, tries)

    def ensure_inventory_closed(self, tries=3):
        return self.gun.ensure_inventory_closed(tries)

    def ensure_inventory_open(self, tries=3):
        return self.gun.ensure_inventory_open(tries)

    def read_loadout(self, slot=1):
        return self.gun.read_loadout(slot)

    # ── the magazine: forwarded to control/fire.py ──
    #
    # ammo_debug_dir is a property rather than a copy, so setting it here
    # actually reaches the fire loop that reads it. The comment used to say
    # harvest sets it; nothing in the repo ever did, so the branch behind it
    # was unreachable from the day it was written. --ammo-debug sets it now.

    ammo_debug_dir = property(lambda s: s.fire.ammo_debug_dir,
                              lambda s, v: setattr(s.fire, 'ammo_debug_dir', v))

    def ammo_sig(self, frame):
        return self.fire.ammo_sig(frame)

    def read_ammo(self, frame=None):
        return self.fire.read_ammo(frame)

    def magazine_size(self, timeout_s=2.0):
        return self.fire.magazine_size(timeout_s)

    def fire_magazine(self):
        return self.fire.fire_magazine()

    def wait_reload(self):
        return self.fire.wait_reload()

    def top_up(self, settle_s=0.4):
        return self.fire.top_up(settle_s)

    def arm(self, weapon):
        return self.fire.arm(weapon)

    def disarm(self, clear=False):
        return self.fire.disarm(clear)

















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

        focus_fn is passed, and it is the one behaviour change worth stating:
        grab() now RAISES when the game is not in the foreground, instead of
        handing back the frozen picture PUBG leaves on screen. A run that keeps
        grabbing through a lost foreground does not fail -- it measures a still
        image and reports a suspiciously clean residual. Callers that fire have
        to catch FocusLost; see calibrate_combo.
        """
        self.frames = ScreenBuffer(self._regions(), prefer_dxgi=True,
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
        analyses its 1.875 counts-per-pixel view with 1.5474 and reports a
        recoil of MINUS 482 counts.
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

    rig.arm(w)
    time.sleep(0.3)

    # Enters ADS as a side effect — the posture icon is the ADS indicator.
    if not rig.ensure_posture(posture):
        print(f"    [!] could not reach posture {posture}")
        return None
    # In ADS and before the first round. With homing on this lands at the
    # middle of the measurable pitch band; with it off it stays where it is.
    rig.flush(6)
    if rig.use_homing:
        rig.goto_pitch_centre()
    rig.set_reference()
    # Magazine 0 has no `tracking_lost` to protect it, and set_reference()
    # takes its reference wherever the view happens to be — including against
    # the pitch clamp, where the view does not move and the recoil reads near
    # zero. See calibration/harvest.py's copy of this check for what that cost
    # on the vss.
    if not rig.tracking_confirmed():
        print("    [!] the view does not respond to a test move — at the "
              "pitch clamp, or the correlator has lost it. Refusing the cell.")
        return None

    rows = []
    for i in range(mags):
        if not focus_keeper().ok(f'{weapon}/{posture} mag {i}'):
            break
        if i > 0:                    # auto-reload drops us to the hip
            # WAIT THE RELOAD OUT BEFORE CLICKING. The game does not act on
            # the right button while it is reloading, and ensure_ads used to
            # spend all three of its clicks inside that window. Measured
            # 2026-08-05 on an m416, four magazines, clicking at fixed offsets
            # after the counter stopped falling (tools/probe_ads_after_reload):
            #
            #   clicked  300 ms   0/4 took       clicked 2000 ms   0/4
            #   clicked 1400 ms   0/4            clicked 2300 ms   3/4
            #   clicked 1700 ms   0/4            clicked 2400 ms   4/4
            #
            # and when a click DOES take, the sight is up 102..104 ms later —
            # against an ADS_WATCH_S of 2.5 s. So the watch was never short;
            # it was waiting for a state change that had not been requested.
            # Worse, right click is a toggle, so the clicks that DID land
            # after the window paired up and cancelled.
            #
            # Not a constant, because reload length is per weapon: wait_reload
            # polls the ammo counter until it climbs back and settles. It was
            # already used at three other points in this file and in
            # harvest.py — just not in the loop that needed it.
            if rig.wait_reload() is None:
                print("      [!] the reload never finished — not clicking "
                      "into it")
                break
            if not rig.ensure_ads():
                print("      [!] could not re-enter ADS after reload")
                break
            # POSTURE IS A PER-MAGAZINE PRECONDITION TOO, and until 2026-08-05
            # it was checked once for the whole cell while ADS was checked
            # every magazine. Both are assumptions the compensation multiplies
            # by; only one was being maintained.
            #
            # What that cost: an m762 bare/prone cell measured 2026 counts —
            # the STANDING figure, 2058/2103 in the same run — while the
            # firmware pushed the prone factor of 0.50. Residual +1105, which
            # is 117% of the compensation applied, and it passed every gate in
            # analysis.magazine_fault. The cell then reported a prone factor
            # of 0.9633 where the same weapon's kitted cell said 0.5502; one
            # weapon cannot have both.
            #
            # Cheap enough to do every time: the icon is readable in 3786 of
            # 3787 samples while the sight is up, and it follows a stance
            # change in 34..68 ms (tools/probe_posture_trace.py). ADS is
            # already up by the line above, which is exactly the condition the
            # icon needs.
            seen = rig.gun.read_posture()
            if seen != posture:
                print(f"      [!] posture is {seen!r}, not {posture!r} — the "
                      f"stance changed mid-cell, so every magazine after this "
                      f"would carry the wrong factor")
                break
            rig.reaim()

        try:
            rec, fire_s, steps, fire_end, first_shot, _ = rig.fire_magazine()
        except FocusLost:
            # The game left the foreground mid-burst. The frames after that
            # are the frozen picture PUBG leaves behind, so this magazine is
            # gone -- but the CELL is not: take the foreground back and fire
            # another one. Before ScreenBuffer's focus_fn there was nothing to
            # catch: the run kept grabbing the same still image and reported a
            # suspiciously clean residual.
            print("      [!] lost the foreground mid-magazine — discarded")
            if not focus_keeper().ok(f'{weapon}/{posture} mag {i}'):
                break
            rig.flush(6)
            continue
        if steps == 0:
            print(f"      mag {i}: no rounds fired (reload still running?) "
                  f"— skipped")
            time.sleep(1.5)
            continue
        a = analyse(rec.finish(), rig.K, w.bullet_interval_s, fire_end,
                    first_shot_ts=first_shot)
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
    # Keeps the ammo crops the OCR could not read mid-burst. FireDriver has
    # had the switch and the writing code since it was split out, and its
    # comment said harvest sets it -- nothing ever did, in any file, so the
    # branch was unreachable and the only way to see why the counter misses
    # about five times in a 42-round magazine stayed shut off.
    ap.add_argument('--ammo-debug', default='', metavar='DIR',
                    help='write unreadable ammo crops here (default: off)')
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
        RUNS, f"sweep_{args.sight}_{datetime.now().strftime('%m%d_%H%M')}.jsonl")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
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
    if args.ammo_debug:
        rig.ammo_debug_dir = args.ammo_debug
        print(f"ammo dbg  : unreadable crops -> {args.ammo_debug}")
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
    keeper = focus_keeper()

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
