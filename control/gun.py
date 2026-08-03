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

`frames` is anything with `grab()` and `flush(n)` — see detector/cropper.py.

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

from press.pico_mouse import HID_KEY_TAB, HID_KEY_C, HID_KEY_Z, HID_KEY_B

import cv2

from detector.tab_detector import TabTypeDetector

ADS_SETTLE_S = 0.5
ADS_WATCH_S = 2.5         # how long to watch for the icon after a right-click;
                          # measured ~0.85 s idle and slower right after firing
POSTURE_WATCH_S = 1.5     # same, for the C/Z animation
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
    FIRE_MODE_FOR = {'mg3': 'high'}

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
        the detector saw instead of guessing from a one-line error."""
        if not self.dump_dir:
            return
        try:
            os.makedirs(self.dump_dir, exist_ok=True)
            frame = self.grab()
            for k in ('posture', 'type', 'ammo'):
                if k in frame:
                    cv2.imwrite(os.path.join(self.dump_dir, f'fail_{tag}_{k}.png'),
                                frame[k])
            print(f"      [dbg] wrote fail_{tag}_*.png")
        except Exception as e:
            print(f"      [dbg] dump failed: {e}")

    # ── aiming ──

    def ads_signals(self, frame=None):
        """(posture icon says aiming, crosshair says aiming), either may be None.

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
        """Is the player looking through a sight? The crosshair decides.

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

    def _ads_now(self, timeout_s):
        """Poll in_ads() until it says yes, or the time runs out."""
        t0 = time.perf_counter()
        while True:
            self.flush(1)
            if self.in_ads() is True:
                return True
            if time.perf_counter() - t0 >= timeout_s:
                return False
            time.sleep(0.05)

    # enter_ads() -- blind right-click plus a fixed ADS_SETTLE_S, no read-back
    # -- was removed on 2026-08-03. Zero callers: the only reference was
    # sweep.Rig's forward, which nothing called either. It was ensure_ads with
    # the verification taken out, and right click is a TOGGLE, so a blind press
    # lands out of ADS exactly as often as into it. That is the failure the
    # docstring below spends a paragraph on. If a caller ever genuinely needs
    # an unverified tap, ViewDriver.ads_tap() is the one with a guard on it.

    def ensure_ads(self, tries=3):
        """Get into ADS, and be sure of it before firing anything.

        Right click is a TOGGLE, so clicking it without knowing the current
        state lands in the wrong one half the time; each click is therefore
        WATCHED to completion rather than sampled once. Clicking again while
        the ADS animation is still playing just toggles back out, which is how
        an impatient version of this oscillated forever.

        The state test is in_ads(), where the crosshair decides. It used to be
        the icon alone, on the belief that the icon only renders while aiming.
        That belief let a whole burst go out in third person with no sight up,
        pass the 80% ADS gate, and report residuals of +588 counts — the
        analysis applies the scoped K=1.55 to motion that happened at the
        hip's 0.50."""
        if self._ads_now(ADS_SETTLE_S):
            return True
        for _ in range(tries):
            self.mouse.click(buttons=0x02, duration_ms=60)
            t0 = time.perf_counter()
            if self._ads_now(ADS_WATCH_S):
                dt = time.perf_counter() - t0
                if dt > ADS_SETTLE_S:
                    print(f"      [ads] confirmed {dt:.2f}s after click")
                return True
        return False

    # ── stance ──

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
            print("      [!] not in ADS (posture icon and crosshair must "
                  "agree) — cannot verify posture")
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

    # ── fire mode ──

    def read_fire_mode(self):
        """'single' | 'full' | 'high' | ... off the HUD, or None.

        grab() hands back a dict of region crops, not a screenshot — the whole
        point of the banded grabber is that no full frame is ever copied.
        """
        if self.fire_det is None:
            return None
        crop = self.grab().get('fire_mode')
        return None if crop is None else \
            self.fire_det.classify({'fire_mode': crop})

    def ensure_fire_mode(self, weapon, tries=6):
        """Cycle B until the gun is in the mode its curve was timed for.

        Guns do not all spawn in full auto -- the Mk14 and the DMRs come out
        single -- and a magazine fired in single mode is one round per trigger
        pull. The old code held the button, got one shot, and measured the
        recoil of a burst that never happened.

        B is a cycle, not a switch, so this presses and WATCHES rather than
        pressing once and hoping. Returns the mode it ended in, or None if the
        detector cannot see one at all (which is not the same as being wrong).
        """
        want = self.FIRE_MODE_FOR.get(weapon, 'full')
        seen = self.read_fire_mode()
        if seen is None:
            return None
        for _ in range(tries):
            if seen == want:
                return seen
            self.mouse.key(HID_KEY_B, 60)
            time.sleep(0.35)
            self.flush(3)
            seen = self.read_fire_mode()
        return seen

    # ── inventory ──
    #
    # CONVERGED. There used to be a second implementation of "is the Tab screen
    # up" right here — cv2 luma instead of the channel maximum, a closed band
    # instead of an open one — and stocktake.open_tab called the disagreement a
    # feature. It was a fork, and it carried the sky false-positive that
    # detector/tab_detector.py now rejects with a dark-floor test.
    #
    # What is still legitimately different is the CAPTURE path, not the
    # judgement: this gets 'type' out of the banded grabber's dict, already in
    # hand from the frame the loop is on, while InventoryControl.tab_open()
    # win32_cap's the region on demand. Passing the crop straight through is
    # what keeps that true — building a detector that grabs its own frame would
    # cost a second capture per call inside the fire loop.

    def tab_open(self, frame):
        # Indexed, not .get(): classify() reads a missing crop as "shut", and
        # a grabber that is not carrying 'type' would then report the
        # inventory closed forever -- ensure_inventory_closed() would return
        # True without ever pressing Tab. The old frame['type'] raised here,
        # and that is worth keeping.
        return bool(self.tab_det.classify(frame['type']))

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
        """One Tab cycle returns both the weapon name and its attachments.

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
        self.mouse.key(HID_KEY_TAB, 60)
        time.sleep(TAB_OPEN_S)
        # full(), not grab(). Both classify() calls below cut by SCREEN
        # coordinate, and grab() returns {region_name: crop} -- indexing that
        # with a pair of slices raises KeyError, which is what killed the first
        # unattended night run three minutes after this method was last
        # "fixed". tab_open() wants the named dict, so it keeps getting one.
        cropped = self.grab()
        frame = self.full(cropped)
        ok = self.tab_open(cropped)
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
