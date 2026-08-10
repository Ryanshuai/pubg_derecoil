"""Getting the character into the state a measurement assumes, and proving it.

    IMPORTANT: every toggle here is WATCHED to completion, never pressed and
    hoped for. Right click, C, Z, B and Tab are all toggles or cycles, so
    pressing one without knowing the current state lands in the wrong one half
    the time.

    from control.gun import GunDriver
    gun = GunDriver(frames, mouse, posture_det, ads_det, fire_det=..., ...)
    gun.ensure_ads()                  # and be sure of it before firing
    gun.ensure_posture('prone')
    gun.ensure_fire_mode('mg3')

`frames` is anything with `grab()` and `flush(n)` — see capture/cropper.py.

WHY THIS IS NOT "just press the key": every one of these was once written as
a keypress plus a sleep, and every one of them produced a run full of
confident wrong numbers rather than an obvious failure.

  * ADS — a whole burst went out in third person with no sight up, passed the
    gate, and reported +588 counts of residual. The analysis applies the
    scoped K of 1.55 to motion that happened at the hip's 0.50.
  * posture — one dropped C/Z mislabels an entire run, and every cell in it
    is filed under a posture the character was never in.
  * fire mode — guns do not all spawn in full auto. The Mk14 and the DMRs come
    out single, so holding the button gets ONE shot, and the old code measured
    the recoil of a burst that never happened.
  * inventory — an open inventory hides the posture icon AND swallows C/Z,
    which looks exactly like a broken detector.

ON READING ADS: two signals exist and they are NOT equals — see in_ads(). The
crosshair decides; the posture icon is corroboration and a canary. ANDing them
was worse than using the strong one alone, and the reason is worth keeping in
mind whenever a second signal is tempting: ANDing a strong signal with a weak
one does not give you two signals, it gives you the weak one.
"""
import os
import time

from press.pico_mouse import (HID_KEY_TAB, HID_KEY_C, HID_KEY_Z, HID_KEY_B,
                              HID_KEY_W, HID_KEY_S)

import cv2

from config import FIRE_MODE_FOR as _CONFIG_FIRE_MODE_FOR
from detector.tab_detector import TabTypeDetector

ADS_SETTLE_S = 0.5
# ⚠ A PROBE IS NOT A SETTLE, and using one constant for both cost a full
# second per magazine. ensure_ads/ensure_hip open by asking "am I already
# there?", and that ask was given ADS_SETTLE_S as its timeout -- so on the
# path where the answer is NO, which is the only path that goes on to click,
# it polls the full 0.5 s before doing anything. The pre-fire sequence is
# ensure_hip -> goto_midline -> ensure_ads, so BOTH probes are guaranteed to
# fail and both burn it: 1.0 s a magazine, on top of the two 0.5 s settles
# that were actually asked for ("开镜等 0.5s").
#
# Nothing is in flight when the probe runs -- nobody has clicked -- so there
# is no animation to wait out. The only reason to poll at all is that in_ads()
# returns None on a frame it cannot read, and _ads_wait samples every 0.05 s,
# so this is three chances at a clean read. If it guesses wrong the cost is
# one extra toggle, which the watched-to-completion retry already handles.
ADS_PROBE_S = 0.15
ADS_WATCH_S = 2.5         # how long to watch for the icon after a right-click;
                          # measured ~0.85 s idle and slower right after firing
POSTURE_WATCH_S = 1.5     # same, for the C/Z animation
# How long to keep re-READING the fire mode before calling it unreadable. It is
# a re-read, never a re-press: B is a cycle. 0.5 s is ~4 reads at the HUD's
# refresh and the icon is either drawn or it is not -- there is no animation to
# wait out, unlike the posture icon's 34-68 ms after a C/Z.
FIRE_MODE_READ_S = 0.5
POSTURE_SETTLE_S = 0.6
TAB_OPEN_S = 0.55
TAB_CLOSE_S = 0.35


class GunDriver:
    """The character's state: aiming, stance, fire mode, inventory."""

    # Which fire mode a curve assumes. Everything full-auto means 'full'; the
    # MG3 is the exception, because it has TWO automatic modes -- a slow one
    # and a fast one -- and WEAPON_RPM['mg3'] is 990, the fast one. Firing the
    # slow mode against a curve timed for the fast one spaces the compensation
    # wrong on every single round.
    #
    # ⚠ THE TABLE MOVED TO config (2026-08-09) AND THIS IS AN ALIAS. It is read
    # by calibration/samples.py as well, to decide which file a magazine lands
    # in, and two copies of a table that decides where DATA GOES is this
    # repository's most expensive recurring shape.
    FIRE_MODE_FOR = _CONFIG_FIRE_MODE_FOR

    def __init__(self, frames, mouse, posture_det, ads_det,
                 fire_det=None, gun_det=None, att_det=None, dump_dir=None):
        self.frames = frames
        self.mouse = mouse
        self.posture_det = posture_det
        # Not optional, by design. The crosshair is half of the ADS gate, and
        # the gate is what decides whether a whole run's numbers mean anything
        # -- the posture icon alone once passed a burst fired from the hip, in
        # third person, and the run read as clean. Callers build this WITHOUT
        # a try, so a detector that will not build stops the run.
        self.ads_det = ads_det
        self.fire_det = fire_det
        self.gun_det = gun_det
        self.att_det = att_det
        self.dump_dir = dump_dir
        # Built on first use, and only ever on a failure path. See
        # blocking_screen(): the fire loop must not pay for it.
        self._panel_det = None
        # Stateless and pixel-only (device=None loads no model), so it is built
        # here rather than injected: every caller wants the same one.
        self.tab_det = TabTypeDetector()

    def grab(self):
        return self.frames.grab()

    def full(self, frame=None):
        """The frame as SCREEN pixels, for a detector that cuts its own crops.

        grab() hands back {region_name: crop} — a banded capture, because
        grabbing 3440x1440 per poll is what the banding exists to avoid. Most
        detectors take that dict and index it by name. Two do not: the gun
        name plates and the attachment slots are cut by pixel coordinate, and
        given the dict they raise KeyError on a tuple of slices.

        ScreenBuffer.full() blits the crops back where they came from, so the
        coordinates line up without a full-screen grab. Every region those two
        detectors read is in the rig's set already (gun_name_1/2 and att_*),
        which is what makes the blit sufficient rather than merely convenient.

        Pass a frame already grabbed to avoid a second capture; the crops and
        the composite then describe the same instant, which matters when the
        thing being read is a screen that was just toggled open.
        """
        return self.frames.full(frame)

    def flush(self, n=8):
        self.frames.flush(n)

    def dump(self, tag):
        """Save the crops behind a failed decision, so a human can see what
        the detector saw instead of guessing from a one-line error.

        NUMBERED, because the failures worth diagnosing are the ones that
        REPEAT and a fixed filename keeps only the last of them. The 2026-08-04
        posture axis failed 'posture unreadable' on six cells across four
        weapons and left one frame: the icon is white and low-saturation, the
        training range's pale wood passes the same gate, and the two merge into
        one blob under the dilations (46% of the crop, IoU 0.268 against prone
        where the threshold is 0.32). One frame cannot say whether the fix
        generalises -- and the labelled set that says this detector is 99% has
        1714 samples with not one of this background in it.
        """
        if not self.dump_dir:
            return
        try:
            os.makedirs(self.dump_dir, exist_ok=True)
            self._dumps = getattr(self, '_dumps', 0) + 1
            frame = self.grab()
            for k in ('posture', 'type', 'ammo'):
                if k in frame:
                    cv2.imwrite(os.path.join(self.dump_dir,
                                             f'fail_{tag}_{self._dumps:03d}_{k}.png'),
                                frame[k])
            print(f"      [dbg] wrote fail_{tag}_{self._dumps:03d}_*.png")
        except Exception as e:
            print(f"      [dbg] dump failed: {e}")

    # ── aiming ──

    def ads_signals(self, frame=None):
        """R — The two ADS signals unmerged, for a loop that must name which one
        objected. in_ads() is the merge; the crosshair wins.

        ⚠ THE ICON SLOT IS True-OR-None AND NEVER False — it cannot say "the
        icon says not aiming", only "no icon at all". And `cross` is None
        whenever the grabber carries no 'crosshair' region, which is a
        different fact from "no crosshair on screen". bool() collapses both.

        Split out so a fire loop can count the two separately. A discarded
        magazine has to name which signal objected: they fail for opposite
        reasons -- the icon stops rendering out of ADS, the crosshair detector
        was never validated with a gun going off -- and the fixes have nothing
        in common.
        """
        frame = self.grab() if frame is None else frame
        icon = self.posture_det.classify({'posture': frame['posture']})
        cross = None
        if self.ads_det is not None and 'crosshair' in frame:
            cross = self.ads_det.scoped_crop(frame['crosshair'])
        return (None if icon is None else True), cross

    def in_ads(self, frame=None):
        """R — Scoped or not, off one frame. Reads only; ensure_ads() is the L2
        that acts.

        ⚠ TWO ANSWERS FOR A THREE-STATE GAME: False covers hip fire AND
        shoulder aim alike. And EMPTY HANDS HAVE NO CROSSHAIR, so this reads
        True for a character who never scoped — prove the gun is out with
        ammo, not with this.

        Two signals exist and they are not equals.

        The CROSSHAIR is positive evidence: PUBG draws a centre crosshair
        exactly when the player is NOT looking through a sight, so its absence
        means a sight is up. detector/ads_detector.py separates 492 labelled
        frames with a 14x margin and zero errors.

        The POSTURE ICON is weak in both directions. It passed a burst fired
        from the hip in third person -- the run reported +588 counts of
        residual and read as clean -- and it also FAILS THE OTHER WAY during
        fire: on a scoped AUG magazine it was present in 48% of polls while the
        crosshair said scoped in 96%. docs/game_quirks.md has the reason, that
        the icon only renders about 0.85 s after firing starts, and it flickers
        after that.

        So this used to require both to agree, and that was wrong in a way
        worth spelling out: ANDing a strong signal with a weak one does not
        give you two signals, it gives you the weak one. Half of every cell was
        being thrown away by the detector that had already been shown to be
        unreliable. detector/CLAUDE.md said to prefer the crosshair; this now
        does.

        The icon is still read, still logged, and a systematic disagreement is
        still printed -- it is corroboration and a canary, not a veto.

        Returns None only when neither signal can be read at all.
        """
        icon, cross = self.ads_signals(frame)
        if cross is not None:
            return bool(cross)
        if icon is None:
            return None
        return icon is not None

    def _ads_wait(self, want, timeout_s):
        """Poll in_ads() until it reads `want`, or the time runs out.

        `want=False` is not "not True": in_ads returns None when NEITHER signal
        can be read, and None must not be counted as out of ADS -- an unreadable
        screen would then look exactly like a successfully lowered sight.
        """
        t0 = time.perf_counter()
        while True:
            self.flush(1)
            if self.in_ads() is want:
                return True
            if time.perf_counter() - t0 >= timeout_s:
                return False
            time.sleep(0.05)

    def _ads_now(self, timeout_s):
        """Poll in_ads() until it says yes, or the time runs out."""
        return self._ads_wait(True, timeout_s)

    # enter_ads() -- blind right-click plus a fixed ADS_SETTLE_S, no read-back
    # -- was removed on 2026-08-03. Zero callers: the only reference was
    # sweep.Rig's forward, which nothing called either. It was ensure_ads with
    # the verification taken out, and right click is a TOGGLE, so a blind press
    # lands out of ADS exactly as often as into it. That is the failure the
    # docstring below spends a paragraph on. If a caller ever genuinely needs
    # an unverified tap, ViewDriver.ads_tap() is the one with a guard on it.

    def ensure_ads(self, tries=3, settle_s=ADS_SETTLE_S):
        """L2 — Sight up, crosshair-confirmed, retried. THE ADS a measurement must
        use; ViewDriver.ads_tap() is the L0 beside it.

        ⚠ CALL FireDriver.wait_reload() FIRST. PUBG eats the right button
        for the whole reload animation (0/4 landing at 2000 ms, 4/4 at 2400)
        and right click is a toggle, so eaten clicks cancel in pairs; each
        recovery costs 2.5 s of watch.

        Right click is a TOGGLE, so clicking it without knowing the current
        state lands in the wrong one half the time; each click is therefore
        WATCHED to completion rather than sampled once. Clicking again while
        the ADS animation is still playing just toggles back out, which is how
        an impatient version of this oscillated forever.

        The state test is in_ads(), where the crosshair decides. It used to be
        the icon alone, on the belief that the icon only renders while aiming.
        That belief let a whole burst go out in third person with no sight up,
        pass the 80% ADS gate, and report residuals of +588 counts — the
        analysis applies the SCOPED K to motion that happened at the hip's
        0.50, and the two differ by about 3x (RECOIL_SIGHT_PROFILES; the
        scoped value is not quoted here because it has moved three times).

        `settle_s` IS AFTER THE CONFIRMATION, NOT INSTEAD OF IT, and only on
        the path that actually clicked. The crosshair is the first thing to go
        when the sight comes up, so in_ads() turns true partway through the
        animation and a caller that fires on that edge fires while the picture
        is still moving. Asked for by the operator on 2026-08-06 -- "开镜等
        0.5s" -- and it is 0.5 because that is what ADS_SETTLE_S already
        was; nobody has measured how much of the animation is left at the
        moment the crosshair vanishes.

        Nothing is slept when the sight was ALREADY up: there was no
        transition, so there is nothing to settle, and this is called several
        times per cell."""
        if self._ads_now(ADS_PROBE_S):
            return True
        for _ in range(tries):
            self.mouse.click(buttons=0x02, duration_ms=60)
            t0 = time.perf_counter()
            if self._ads_now(ADS_WATCH_S):
                dt = time.perf_counter() - t0
                if dt > ADS_SETTLE_S:
                    print(f"      [ads] confirmed {dt:.2f}s after click")
                if settle_s:
                    time.sleep(settle_s)
                return True
        return False

    def ensure_hip(self, tries=3, settle_s=ADS_SETTLE_S):
        """L1 — Release the button and drop the sight, so the pitch is positioned
        in ONE fixed aim state. NOT ensure_ads()'s inverse: that one's
        readback proves its target, this one's cannot.

        ⚠ THE HIP COMES FROM THE RELEASE, NOT FROM THE READ. in_ads()
        answers "scoped or not" and shoulder aim also answers "not", so True
        here is compatible with the button being held. That asymmetry put
        four K runs in shoulder aim with filenames saying ADS.

        THE GAME HAS THREE AIM STATES, not two, and detector/ads_detector.py
        is this repo's definition of them:

            hip fire      腰射.  Button NOT touched. Centre-dot crosshair.
            shoulder aim  肩射 / tactical aim.  Button HELD. Third person,
                          pulled in over the shoulder, no sight picture. A
                          THIRD STATE, not a kind of hip fire.
            ADS           开镜.  Button TAPPED (it is a toggle).

        ⚠ THE READ CANNOT TELL THE FIRST TWO APART, so this does not rely on
        it. in_ads() answers "scoped or not", and both hip fire and shoulder
        aim answer "not" -- so the button is explicitly RELEASED here and the
        hip guarantee comes from that action, while the read only confirms the
        sight is down. Getting this backwards is what put four whole K
        calibration runs in shoulder aim while their filenames said ADS.

        WHY POSITION THE VIEW FROM THE HIP. Pitch is a property of the
        character, not of the optic -- the two clamps are the same two clamps
        through every sight. What DOES change with the optic is how many mouse
        counts a degree costs, and that conversion (aim.pitch_scale) is an
        unvalidated model which put the view in the ground on every magnified
        attempt of 2026-08-05. Doing the move in ONE fixed state deletes the
        conversion from the problem: the hip travel is measured once and every
        scope inherits it. Asked for on 2026-08-06 -- "所有的倍数，都应该换成
        不开镜。然后抬头。"

        The costs are one extra verified toggle per magazine, and that the
        reference frame must be taken AFTER the sight comes back up -- the
        tracker patches are per sight, so a reference grabbed from the hip
        describes a picture the magazine will not be fired through.
        """
        # Nothing in this project holds the right button any more, but the
        # release is what MAKES this hip fire rather than shoulder aim, so it
        # is sent rather than assumed. Idempotent and ~free.
        self.mouse.click(buttons=0x00, duration_ms=0)
        if self._ads_wait(False, ADS_PROBE_S):
            return True
        for _ in range(tries):
            self.mouse.click(buttons=0x02, duration_ms=60)
            if self._ads_wait(False, ADS_WATCH_S):
                if settle_s:
                    time.sleep(settle_s)
                return True
        return False

    def stir(self, ms=120):
        """Take one step and take it back. Nothing else in a run MOVES.

        ⚠ AN EXPERIMENT WITH A NAMED PREDICTION, not a fix. Four evictions on
        2026-08-07 landed 18.1 / 22.3 / 21.2 minutes apart -- mean 20.5, sd
        2.2, 11% of the mean -- across three different weapons doing different
        amounts of firing and inventory work. That is a clock, and there are
        two candidates it cannot tell apart:

          the match (or the server) ends on a schedule; or
          PUBG's idle timer counts MOVEMENT, and this project never moves.
            Shooting, Tab, C/Z and the spawner are all input, and none of them
            is a step. docs/game_quirks.md says the collector has never been
            kicked -- measured on shorter runs that teleported between cells,
            and a teleport MOVES the character.

        Calling this between configs separates them: if the interval stretches
        past 20.5 minutes it is the idle timer, and if it does not, it is a
        clock and this can come straight back out.

        Forward then back, so the net displacement is about zero. It runs
        between CONFIGS and never between magazines, and goto_midline re-homes
        the pitch afterwards regardless -- the character's position does not
        enter the measurement, which tracks the view relatively.
        """
        if self.mouse is None:
            return False
        self.mouse.key(HID_KEY_W, ms)
        self.mouse.key(HID_KEY_S, ms)
        return True

    # ── stance ──

    def read_posture(self, timeout_s=0.8, gap_s=0.08):
        """R — Poll the posture icon until it reads, or time out. Commands nothing;
        ensure_posture() is the L2 that acts on the answer.

        ⚠ FROM THE HIP THIS IS None NO MATTER HOW LONG YOU WAIT — 0 of 3787
        un-scoped frames had the icon drawn. It does NOT raise the sight, so
        None usually means "not scoped", not "the detector failed". Never
        toggle C/Z on a None.

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

    def ensure_posture(self, target, tries=4, nudge=None):
        """L2 — Get the character into `target` stance, screen-confirmed. It
        RAISES THE SIGHT to do it, so it is not read_posture()'s peer.

        ⚠ IT LEAVES YOU IN ADS. ensure_inventory_closed() then ensure_ads()
        run first — the icon is drawn in 0 of 3787 un-scoped frames — and
        nothing puts the sight back down. ensure_hip() after this, or the
        pitch gets positioned scoped.

        Toggle until the icon detector agrees. Keypresses alone are not
        trusted: one dropped toggle would mislabel an entire run.

        Requires ADS (the icon does not render from the hip) and a closed
        inventory (which hides the icon and swallows C/Z).

        `nudge` IS A LAST RESORT AGAINST THE BACKGROUND, not against timing.
        The icon is white and low-saturation and the detector's gate is
        absolute (V>180, S<80), so pale scenery behind it — the training
        range's wood — passes the same gate, merges with the glyph under the
        dilations and takes the largest-component pick with it. Measured
        2026-08-04: 46% of the crop passing the gate, one 1403 px blob, best
        IoU 0.268 against prone where the threshold is 0.32. The character WAS
        prone; only the reading failed, and the cell was discarded.

        The icon sits at a fixed place on the HUD, so the only thing that
        changes what is behind it is where the view points. A caller that can
        safely re-point hands one in; this calls it once and re-reads.

        SAFELY IS THE WHOLE CONDITION, and it is the caller's to judge. Moving
        the view invalidates any running total the caller holds, so this is
        only sound when what follows re-establishes the view from a hard stop
        — which is exactly the homing path (`--home`), where goto_pitch_centre
        runs after this and homes to the pitch clamp. With homing off there is
        no nudge to pass, and none is.
        """
        if not self.ensure_inventory_closed():
            print("      [!] inventory stuck open — C/Z would be swallowed")
            self.dump('inventory')
            return False
        if not self.ensure_ads():
            print("      [!] not in ADS (posture icon and crosshair must "
                  "agree) — cannot verify posture")
            self.dump('ads')
            return False
        # Every posture the icon showed while this ran. It separates two
        # failures that print the same line otherwise, and they have opposite
        # fixes: a state that NEVER MOVED means the key never reached the game
        # or the game refused it (prone is blocked against some geometry), so
        # pressing harder is pointless; a state that moved and came back is a
        # toggle read mid-animation and re-pressed, which more settle time
        # does fix. 2026-08-04's posture axis lost the m416's prone cell with
        # "gave up at 'crouching'" and there was no way to tell which.
        seen = []
        nudged = False
        for _ in range(tries):
            cur = self.read_posture(timeout_s=POSTURE_WATCH_S)
            if cur is None and self.ensure_ads(tries=2):
                # Going prone can drop ADS, and the icon goes with it — that
                # reads identically to "detector broken", so re-aim and re-read
                # before believing it.
                cur = self.read_posture(timeout_s=POSTURE_WATCH_S)
            if cur == target:
                return True
            if cur is None and nudge and not nudged:
                # Not a retry — the same read at the same place gives the same
                # answer. This moves what is BEHIND the icon and then asks
                # again. Once: if a second background cannot be read either,
                # the fault is not the scenery.
                nudged = True
                nudge()
                cur = self.read_posture(timeout_s=POSTURE_WATCH_S)
                if cur is not None:
                    # It used to say the brightness gate had let scenery in.
                    # That explanation was checked on 2026-08-05 and is wrong:
                    # the one stored failure has a CLEAN 424 px mask and reads
                    # crouching at IoU 0.756 — the sample is mislabelled, not
                    # misread. Three alternative masks/templates cannot beat
                    # the current reader either; the accuracy figure itself
                    # lives in detector/posture_detector.py's docstring and is
                    # not restated here. So say what was observed and nothing
                    # more; the icon
                    # is simply not drawn at every moment (it needs ADS — see
                    # docs/game_quirks.md), and moving the view costs time
                    # during which it can appear.
                    print(f"      posture readable after moving the view "
                          f"(read {cur!r}) — it was unreadable a moment "
                          f"earlier; cause not established")
            if cur is None:
                # Never toggle on an unknown state — a blind C/Z here is how an
                # unattended run ends up measuring a posture nobody asked for.
                # ⚠ ASK WHO ELSE IS ON SCREEN BEFORE ACCUSING THE DETECTOR.
                # ensure_inventory_closed() reads "the inventory is not
                # DRAWN", which is also true with the spawner panel over it —
                # and the panel eats C and Z. The old message named the
                # posture detector and dumped its crops, so the evidence on
                # disk was of the one component that was working.
                why = self.blocking_screen()
                print(f"      [!] posture unreadable (want {target})"
                      + (f" — {why}" if why else '')
                      + ('' if nudge or why else ' — no nudge available '
                         '(homing is off, so moving the view would '
                         'invalidate the reference this cell measures '
                         'against)'))
                self.dump('posture')
                return False
            seen.append(cur)
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
            seen.append(cur)
            moved = len({s for s in seen if s is not None}) > 1
            print(f"      [!] gave up at {cur!r}, wanted {target} — saw "
                  f"{' -> '.join(str(s) for s in seen)}; "
                  + ('the state moved, so the keys ARE arriving and a read '
                     'landed mid-animation: raise POSTURE_SETTLE_S'
                     if moved else
                     f'the state never moved across {len(seen) - 1} press(es), '
                     f'so the key is being swallowed or the game refuses '
                     f'{target} here — more tries will not help'))
            self.dump('posture')
        return cur == target

    # ── fire mode ──

    def read_fire_mode(self):
        """R — The fire mode off the HUD, or None. Reads; the L1 that presses B is
        ensure_fire_mode().

        ⚠ None MEANS EITHER "NO DETECTOR" OR "CANNOT READ" — `fire_det` is
        optional in __init__, so a rig built without one reports unreadable
        forever and nothing prints. Unreadable is not full auto: the Mk14
        and the DMRs spawn single.

        grab() hands back a dict of region crops, not a screenshot — the whole
        point of the banded grabber is that no full frame is ever copied.
        """
        if self.fire_det is None:
            return None
        crop = self.grab().get('fire_mode')
        return None if crop is None else \
            self.fire_det.classify({'fire_mode': crop})

    def ensure_fire_mode(self, weapon, tries=6, want=None):
        """L1 — Press B and watch until the HUD reads a mode. RETURNS THE MODE
        STRING, not a bool — alone among the ensure_* here.

        `want` overrides FIRE_MODE_FOR for callers that are deliberately
        measuring the other one. ⚠ IT IS A PARAMETER HERE RATHER THAN A SECOND
        LOOP IN THE CALLER: the mg3's slow mode has to be measurable on purpose,
        and calibration/CLAUDE.md opens with what a parallel driver costs --
        every copy in this repository drifted from what it copied, and the
        symptom was a batch of numbers that looked completely normal.

        ⚠ 'single' IS TRUTHY AND None IS NOT SAFE. It gives up after `tries`
        and hands back whatever it ended on, so the caller compares against
        FIRE_MODE_FOR itself. On a DMR or the Mk14 an unreadable mode must
        REFUSE: they spawn single, and single fires one round into a
        magazine's worth of analysis.

        Guns do not all spawn in full auto -- the Mk14 and the DMRs come out
        single -- and a magazine fired in single mode is one round per trigger
        pull. The old code held the button, got one shot, and measured the
        recoil of a burst that never happened.

        B is a cycle, not a switch, so this presses and WATCHES rather than
        pressing once and hoping. Returns the mode it ended in, or None if the
        detector cannot see one at all (which is not the same as being wrong).

        ⚠ AN UNREADABLE FRAME MUST NOT BE PRESSED THROUGH, and it used to be.
        A single None from read_fire_mode() left the loop comparing None to
        `want`, which is never equal, so it pressed B AGAIN -- on a CYCLE, with
        no idea where it was. That is the exact thing this class exists to stop,
        and it was in the one method that had never been called.

        Measured on the night runs' own evidence frames: 14 of 51 in-game
        frames read None (the HUD is not drawn with empty hands, mid-swap, or
        with a screen up), against 37 that read a mode -- so this is the common
        case, not an edge one. Both reads are therefore polled to a DEADLINE,
        the same shape as the posture detector's retry: read again, do not
        press again.
        """
        want = want or self.FIRE_MODE_FOR.get(weapon, 'full')
        seen = self._fire_mode_settled()
        if seen is None:
            return None
        for _ in range(tries):
            if seen == want:
                return seen
            self.mouse.key(HID_KEY_B, 60)
            time.sleep(0.35)
            self.flush(3)
            got = self._fire_mode_settled()
            if got is None:
                # The press happened; what it landed on is unknown. Saying so
                # is the only honest answer -- another blind B would be a
                # second unobserved step around the cycle.
                return None
            seen = got
        return seen

    def _fire_mode_settled(self, timeout_s=FIRE_MODE_READ_S):
        """The mode off the HUD, re-reading until one appears or time is up."""
        end = time.perf_counter() + timeout_s
        while True:
            got = self.read_fire_mode()
            if got is not None or time.perf_counter() >= end:
                return got
            self.flush(2)

    # ── inventory ──
    #
    # CONVERGED. There used to be a second implementation of "is the Tab screen
    # up" right here — cv2 luma instead of the channel maximum, a closed band
    # instead of an open one — and what is now control/stock.open_tab called
    # the disagreement a feature (its own docstring still records that, under
    # "this docstring has been wrong twice"). It was a fork, and it carried the
    # sky false-positive that
    # detector/tab_detector.py now rejects with a dark-floor test.
    #
    # What is still legitimately different is the CAPTURE path, not the
    # judgement: this gets 'type' out of the banded grabber's dict, already in
    # hand from the frame the loop is on, while InventoryControl.is_tab_open()
    # win32_cap's the region on demand. Passing the crop straight through is
    # what keeps that true — building a detector that grabs its own frame would
    # cost a second capture per call inside the fire loop.

    def is_tab_open(self, frame):
        # Indexed, not .get(): classify() reads a missing crop as "shut", and
        # a grabber that is not carrying 'type' would then report the
        # inventory closed forever -- ensure_inventory_closed() would return
        # True without ever pressing Tab. The old frame['type'] raised here,
        # and that is worth keeping.
        return bool(self.tab_det.classify(frame['type']))

    def blocking_screen(self):
        """What is eating the Tab key, or None. One capture, FAILURE PATH ONLY.

        ⚠ IT ANSWERS THE QUESTION ensure_inventory_closed CANNOT. That method
        reads "the inventory is not DRAWN", and the spawner panel covers the
        inventory while swallowing Tab, C and Z — so it returns True and the
        next thing to fail is ensure_posture, which reports "posture
        unreadable" and dumps the posture detector. The detector is the wrong
        suspect, and a dump of it is the wrong evidence.

        Not on the happy path: this is a win32_cap of the spawner icon box,
        and the fire loop calls ensure_inventory_closed once per cell. The
        answer is only interesting once something has already failed, which is
        also when a caller can afford a capture.

        InventoryControl._blocking_screen asks the same question with the same
        detector. It is not shared because the two live on opposite sides of a
        frame source: this class is handed banded crops by the loop it runs
        in, and reaching for an InventoryControl here would open a Pointer —
        a second claim on the one serial port — inside the per-magazine path.
        """
        try:
            if self._panel_det is None:
                from detector.spawner_detector import SpawnerDetector
                self._panel_det = SpawnerDetector()
            if not self._panel_det.ready:
                return None
            from capture.cropper import win32_cap
            from detector.spawner_detector import ICON_BOX
            if self._panel_det.classify(win32_cap(ICON_BOX)):
                return ('the item-spawner panel is up: it covers the '
                        'inventory and the game ignores Tab, C and Z beneath '
                        'it. SpawnerControl.ensure_panel(False) first.')
        except Exception:
            return None          # a probe that cannot run must not accuse
        return None

    def ensure_inventory_closed(self, tries=3):
        """L0 — Press Tab until the inventory is not DRAWN. It verifies the wrong
        thing: callers want "C/Z will reach the game", and the spawner panel
        makes this True while swallowing both. The guard is upstream —
        ensure_tab(False) asks _blocking_screen() first.

        ⚠ TRUE UNDER THE COMMA MENU. ensure_posture() then reports "posture
        unreadable" and dumps the detector, which is the wrong suspect. Shut
        the panel first (harness/adapter.py does).

        An inventory left open hides the posture icon AND swallows C/Z,
        which looks exactly like a broken detector."""
        for _ in range(tries):
            self.flush(2)
            if not self.is_tab_open(self.grab()):
                return True
            self.mouse.key(HID_KEY_TAB, 60)
            time.sleep(TAB_CLOSE_S)
        self.flush(2)
        if not self.is_tab_open(self.grab()):
            return True
        # Every press was sent and the screen never changed. Name the eater
        # rather than leaving the next gate to blame its own detector.
        why = self.blocking_screen()
        if why:
            print(f'      [!] Tab had no effect — {why}', flush=True)
        return False

    def ensure_inventory_open(self, tries=3):
        """L1 — Press Tab until the banded 'type' crop says the inventory is up.
        THE SECOND ENTRY TO InventoryControl.ensure_tab(True) — prefer that
        one, or ac.tab_up(); this is for a caller with no ac.

        ⚠ NO SPAWNER-PANEL PROBE AND NO JOURNAL. Under the comma menu Tab is
        ignored, so this burns all three presses and reports what looks like
        a timing failure, where ensure_tab asks _blocking_screen() and
        refuses with the reason. Its presses are invisible to drag-log.

        Tab is a toggle, so pressing it blind lands in the wrong state half
        the time. Watch instead."""
        for _ in range(tries):
            self.flush(2)
            if self.is_tab_open(self.grab()):
                return True
            self.mouse.key(HID_KEY_TAB, 60)
            time.sleep(TAB_OPEN_S)
        self.flush(2)
        return self.is_tab_open(self.grab())

    def read_loadout(self, slot=1):
        """L1 — One forced Tab cycle -> (gun name, attachments) for `slot`. DESPITE
        THE NAME IT DRIVES: shuts Tab, opens it BLIND, shuts it.

        ⚠ IT CLOSES A SCREEN SOMEBODY UPSTREAM IS HOLDING, and the open is
        an unwatched keypress: a swallowed Tab returns (None, None), which
        is NOT the ('', None) of an empty rack. InventoryControl's tab_up()
        + loadout() answers the same question inside a held screen in 53 ms.

        Both reads used to hand the raw frame to a classify() that wanted a
        dict of pre-cut crops, so this raised AttributeError every time it got
        as far as reading anything. AttachmentDetector takes a frame now; the
        name detector still takes crops, so they are cut here.

        The name is fed to the attachment read rather than just returned:
        naming the weapon narrows each slot's template bank to what that gun
        can hold, which on the reference captures is the difference between
        Suppressor (SMG) and Suppressor (AR).
        """
        from config import HUD_REGIONS
        self.ensure_inventory_closed()
        # ⚠ WATCHED, NOT BLIND. This was one keypress plus a fixed
        # TAB_OPEN_S, and a swallowed Tab then returned (None, None) -- which
        # the caller in control/kitting.py prints as "inventory would not
        # open — cannot read attachments", naming the inventory, which is the
        # one thing that was not wrong.
        #
        # Measured 2026-08-07: over 90 minutes the WATCHED path
        # (InventoryControl.ensure_tab) toggled Tab 312 times with 0 failures
        # and one press every time, while this blind press failed on the first
        # attempt of EVERY cell of an unattended night -- four in a row, which
        # tripped the halt. The difference is not the keyboard, it is whether
        # anybody looked.
        #
        # ensure_inventory_open() is in this same class, directly above, and
        # does exactly this. It was written as "the second entry to
        # ensure_tab(True), for a caller with no ac" and this is that caller.
        # Same shape as _nudge_backdrop and tracking_confirmed: the fix
        # already existed and was simply never called from the place that
        # needed it.
        if not self.ensure_inventory_open():
            return None, None
        # full(), not grab(). Both classify() calls below cut by SCREEN
        # coordinate, and grab() returns {region_name: crop} -- indexing that
        # with a pair of slices raises KeyError, which is what killed the first
        # unattended night run three minutes after this method was last
        # "fixed". is_tab_open() wants the named dict, so it keeps getting one.
        cropped = self.grab()
        frame = self.full(cropped)
        ok = self.is_tab_open(cropped)
        gun = att = None
        if ok:
            crops = {}
            for k in ('gun_name_1', 'gun_name_2'):
                y, x, h, w = HUD_REGIONS[k]
                crops[k] = frame[y:y + h, x:x + w]
            names = self.gun_det.classify(crops)
            gun = names[slot - 1] or ''
            named = {i + 1: n for i, n in enumerate(names) if n}
            att = self.att_det.classify(frame, named).get(slot)
        if not self.ensure_inventory_closed():
            print("      [!] inventory would not close")
        return (gun, att) if ok else (None, None)
