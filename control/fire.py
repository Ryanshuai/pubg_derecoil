"""Firing one magazine, and everything the game says about it while it goes.

    from control.fire import FireDriver
    fire = FireDriver(frames, mouse, tracker, ammo_det=..., gun=gun_driver)
    n = fire.magazine_size()                  # read it, do not assume it
    rec, fire_s, steps, fire_end, first_shot, ads_frac = fire.fire_magazine()
    fire.wait_reload()                        # PUBG reloads itself

`gun` is a control.gun.GunDriver, and it is NOT optional bookkeeping: a
magazine's numbers are meaningless without knowing whether the shots were
aimed. ADS is watched THROUGH the burst, not just before it — PUBG drops it on
reload, on a posture change, on being shot at, and hip fire analysed with the
scoped K reads about three times high.

WHAT THIS RETURNS AND WHY EACH PIECE IS THERE:

  rec          the recording, for control/aim and calibration/analysis
  fire_s       wall clock
  steps        how many times the ammo region changed
  fire_end     the LAST round leaving. Everything after it is the camera
               drifting back, which is real but happens once the bullets are
               already gone -- folding it into the residual flatters the total
               while saying nothing about where the rounds went.
  first_shot   the FIRST round leaving. The bins start here, not at the first
               frame captured: between the click going out over USB and that
               round's recoil appearing on screen there is input sampling, the
               shot, a render and a present -- 20 to 50 ms against an 88 ms
               bullet interval.
  ads_frac     the fraction of polls that were aimed, for the gate

TIMING IS WALL CLOCK, NOT FRAME COUNTS. DXGI re-serves the previous frame
while the screen is idle, so counting frames counts nothing.
"""
import os
import time

import cv2
import numpy as np

from detector.view_tracker import MagazineRecorder
from press.pico_mouse import HID_KEY_R

AMMO_THRESH = 200
AMMO_CHANGED = 0.02
EMPTY_STATIC_S = 0.55     # ammo frozen this long while firing => magazine out
AMMO_OCR_EVERY = 3        # frames between digit reads; see fire_magazine
PREFIRE_FRAMES = 3        # baseline grabs before the trigger
TAIL_RECORD_S = 0.25      # keep recording past the counter reaching zero, so the
                          # last round's recoil is inside the recording
MIN_FIRE_S = 0.8
MAX_FIRE_S = 9.0
RELOAD_TIMEOUT_S = 9.0
RELOAD_STATIC_S = 0.35    # counter must hold this long before the mag is ready
RELOAD_MIN_S = 2.0        # ...and if it never visibly moved, wait at least this
SETTLE_AFTER_RELOAD_S = 1.8   # counter refills mid-animation; gun is not ready


class FireDriver:
    """Holds the trigger, watches the counter, and waits out the reload."""

    def __init__(self, frames, mouse, tracker, ammo_det=None, gun=None):
        self.frames = frames
        self.mouse = mouse
        self.tracker = tracker
        self.ammo_det = ammo_det
        self.gun = gun
        # Set to a directory to keep the ammo crops the OCR could not read
        # during a burst. The counter reads 40/40 sitting still and about five
        # times in a 42-round magazine while firing, and every interval
        # measurement in the project is built on it, so the crops are the only
        # way to find out what is different about a frame taken mid-burst.
        self.ammo_debug_dir = None

    def grab(self):
        return self.frames.grab()

    # ── the ammo counter ──

    def ammo_sig(self, frame):
        g = cv2.cvtColor(frame['ammo'], cv2.COLOR_BGR2GRAY)
        return cv2.threshold(g, AMMO_THRESH, 255, cv2.THRESH_BINARY)[1] > 0

    def read_ammo(self, frame=None):
        """Rounds left in the magazine, or None if the number is not drawn.

        None is NOT zero. An empty magazine still draws `0`; None means the
        counter could not be read at all — mid-reload, inventory open, weapon
        holstered — and treating it as zero reads as "just fired everything".
        """
        if self.ammo_det is None:
            return None
        try:
            return self.ammo_det.classify(frame if frame is not None
                                          else self.grab())
        except Exception:
            return None

    def magazine_size(self, timeout_s=2.0):
        """How many rounds are actually loaded, read off the HUD.

        Worth reading rather than assuming: fitting an extended magazine
        changes the capacity, the base magazine and the extended one differ by
        ten rounds, and a cell that fires a different number of rounds than its
        siblings is not a noisy repeat of them but a different measurement.
        Before this the count came from watching the ammo region flicker, which
        over-counted by about 2.4x and could not be compared against anything.
        """
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout_s:
            n = self.read_ammo()
            if n:
                return n
            time.sleep(0.05)
        return None

    def top_up(self, settle_s=0.4):
        """Reload by hand, wait it out, and read back what is in there.

        -> (rounds, reload_s). `reload_s` is None when the reload started and
        never finished; `rounds` is None when the counter could not be read.

        WHY THIS IS NOT JUST A KEYPRESS. Fitting an extended magazine does not
        fill it: the capacity grows and the rounds in it do not, so the first
        burst of a cell runs short. One short magazine in the bare m416 cell
        pulled the mean 85 counts off and took the cell's spread from ~2% to
        10%, which then propagated into every ratio measured against it.

        WHY IT HAS TO HAPPEN BEFORE THE AIM IS SET. Reloading DROPS OUT OF ADS
        (docs/game_quirks.md). Topping up after ensure_posture put ADS back
        exactly where it could not survive: the first magazine fired from the
        hip and was analysed with the scoped K of 1.55 against the hip's 0.50.
        It reported +498 counts of residual on a gun that had measured -31 an
        hour earlier with the same compensation. It only bites when a reload
        actually happens, which is why it hid for so long -- a freshly spawned
        gun is already full and R does nothing at all.

        THAT LAST CASE IS WHY THIS RETURNS A PAIR. "Already full" and "the
        reload never finished" both leave the counter sitting still, and only
        one of them is fine. wait_reload() separates them (it settles out via
        RELOAD_MIN_S when nothing ever moved, and returns None only on the
        timeout), and the caller has to be able to see which it got: harvest
        threw this return value away, so a reload that stalled was measured as
        a full magazine and reported nothing wrong.
        """
        self.mouse.key(HID_KEY_R, 60)
        time.sleep(settle_s)
        reload_s = self.wait_reload()
        # Read AFTER the wait, not before: the counter refills partway through
        # the animation, so a count taken early is whatever the reload had got
        # to. This is the cell's expected round count and every magazine in it
        # is checked against this number.
        return self.magazine_size(), reload_s

    # ── one magazine ──

    def fire_magazine(self):
        rec = MagazineRecorder(
            self.tracker,
            human_fn=getattr(self.mouse, 'human_totals', None))
        # Baseline frames BEFORE the trigger. The first bullet's kick has to
        # have somewhere to be measured from, and recording that only starts
        # after the click leaves the opening round sharing its window with
        # whatever the view was doing beforehand. That is how the curve came
        # to carry -0.6 counts for its own first shot: bin 0 was mostly
        # pre-fire stillness, the fit read that as "this round barely kicks",
        # and wrote it back -- which then made the next measurement agree.
        for _ in range(PREFIRE_FRAMES):
            rec.push(time.perf_counter(), self.grab())
        self.mouse.click(buttons=0x01, duration_ms=int(MAX_FIRE_S * 1000))
        t0 = time.perf_counter()
        prev, last_change, steps = None, t0, 0
        empty_at = None
        first_shot = None
        # The ammo OCR is the reason this loop runs at 93 fps when the grabber
        # alone does 161. Every frame it read digits it did not need: emptiness
        # is confirmed by EMPTY_STATIC_S = 0.55 s of stillness, which a third
        # of the frames establishes just as well. Halving the frame interval
        # halves the bin-edge error in analyse(), so this is not a micro
        # optimisation, it is measurement precision.
        n = None
        poll = 0
        # ADS is watched THROUGH the burst, not just before it. The posture
        # icon only renders while aiming, so its absence means the shot went
        # from the hip -- and hip fire is analysed with K=1.55 when the truth
        # is 0.50, a factor of three. That is not noise, it is a wrong number
        # that looks entirely plausible: one such magazine once reported +498
        # counts of residual on a gun that had measured -31 an hour before.
        # PUBG drops ADS on reload, on a posture change, on being shot at.
        # Counted apart, because "not in ADS" is a verdict and the two signals
        # are what produced it. detector/CLAUDE.md flags that ads_detector was
        # fitted on 492 frames of a Kar98k with NOTHING BEING FIRED, and that
        # recoil shake blurring the crosshair is the most likely way for it to
        # fail -- which is the whole of this loop. When a magazine is thrown
        # away for being out of ADS, the run has to be able to say WHICH signal
        # said so, or a flaky detector is indistinguishable from a real
        # third-person burst and every fix is a guess.
        ads_seen = ads_polls = icon_seen = cross_seen = 0
        # (t, rounds_left) at every OCR poll. The bullet interval is the slope
        # of this, and it is the only place the game states its own fire rate.
        # detector/weapon.WEAPON_RPM is a hand-typed wiki table and it is wrong
        # on a third of the roster -- and a wrong interval is not a small
        # error, it COMPOUNDS: the firmware lays the compensation out on the
        # nominal grid, so a 5% gap puts bullet 40's pulse two whole rounds
        # away from the round it was meant to cancel.
        ammo_trace = []
        ammo_misses = []
        ammo_polls = 0
        # The trigger is released in a `finally`, and that is not tidiness.
        # grab() raises FocusLost the moment the game leaves the
        # foreground, and this loop is holding the fire button down for
        # up to MAX_FIRE_S. An exception escaping here leaves it held —
        # the character keeps firing into a window nobody is watching,
        # through the reload, into the next cell.
        try:
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
                    # First change = the first round has left. This is the game's
                    # own statement of when firing began, and analyse() bins from
                    # it rather than from whichever frame happened to be first.
                    if first_shot is None:
                        first_shot = now
                prev = sig
                # The counter reaching zero is the magazine ending, stated by the
                # game rather than inferred from pixels that stopped moving. The
                # flicker heuristic below still runs as the fallback -- it is what
                # covers a weapon whose counter cannot be read -- but on its own it
                # cannot say how many rounds went out, only that something stopped
                # changing, and it over-counted them by about 2.4x.
                poll += 1
                if poll % AMMO_OCR_EVERY == 0:
                    n = self.read_ammo(frame)
                    ammo_polls += 1
                    if n is not None:
                        ammo_trace.append((now, int(n)))
                    elif self.ammo_debug_dir and len(ammo_misses) < 12:
                        # Kept because the counter reads 40/40 when idle and about
                        # 5 times in a 42-round magazine while firing, and nothing
                        # in the pipeline says why. Without the actual crop this is
                        # unfixable from a log.
                        ammo_misses.append(frame['ammo'].copy())
                    ads_polls += 1
                    icon, cross = self.gun.ads_signals(frame)
                    icon_seen += bool(icon)
                    cross_seen += bool(cross)
                    # The crosshair decides — see GunDriver.in_ads. The icon is
                    # counted so a disagreement is visible, not so it can veto.
                    if (cross if cross is not None else icon):
                        ads_seen += 1
                if n == 0:
                    # The counter reads zero the instant the last round leaves,
                    # and that round's recoil has not played out yet. Breaking
                    # here throws away the biggest kick in the magazine — the tail
                    # rounds are the steepest part of every curve. Keep recording
                    # for a couple of bullet intervals so the last shot lands
                    # inside the recording it belongs to.
                    if empty_at is None:
                        empty_at = now
                        last_change = now
                    elif now - empty_at >= TAIL_RECORD_S:
                        break
                if (now - t0) > MIN_FIRE_S and (now - last_change) > EMPTY_STATIC_S:
                    break
        finally:
            self.mouse.click(buttons=0x00, duration_ms=0)
        # last_change is the last round leaving the magazine. Everything after
        # it is the camera drifting back toward the pre-fire aim, which is real
        # but happens after the bullets are already gone -- folding it into the
        # residual flatters the total while telling you nothing about where the
        # rounds went.
        ads_frac = (ads_seen / ads_polls) if ads_polls else float('nan')
        rec.ammo_trace = ammo_trace
        rec.ammo_polls = ammo_polls
        if ammo_misses and self.ammo_debug_dir:
            os.makedirs(self.ammo_debug_dir, exist_ok=True)
            for k, crop in enumerate(ammo_misses):
                cv2.imwrite(os.path.join(self.ammo_debug_dir,
                                         f'miss_{int(t0*1000) % 100000}_{k}.png'),
                            crop)
            print(f"      [ammo] {len(ammo_trace)}/{ammo_polls} polls read; "
                  f"{len(ammo_misses)} failing crops -> {self.ammo_debug_dir}")
        rec.ads_icon_frac = (icon_seen / ads_polls) if ads_polls else float('nan')
        rec.ads_cross_frac = (cross_seen / ads_polls) if ads_polls else float('nan')
        return (rec, time.perf_counter() - t0, steps, last_change,
                first_shot, ads_frac)

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
        if self.gun is not None:
            self.gun.dump('reload')
        return None
