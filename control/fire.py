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
RELOAD_HOLD_S = 0.30      # the READ NUMBER has to stop changing for this long
RELOAD_CONFIRM = 2        # ...over at least this many reads of the same value
# After the counter reaches the magazine size, before anyone clicks. The
# counter refills PARTWAY THROUGH the reload animation -- that is why the old
# code slept a flat 1.8 s on top of a pixel settle -- so "the number is back"
# is the start of the tail, not the end of it. 0.5 s is what the operator asked
# for on 2026-08-06 ("弹夹换好，数字变成最大以后，等 0.5s，再开镜") and it is
# anchored to a real event rather than to the end of firing, which is what the
# 1.8 s was. The measured hazard it covers: PUBG ignores right click while the
# animation plays -- m416, four magazines, a click 2000 ms after the counter
# stopped falling took 0/4, at 2300 ms 3/4, at 2400 ms 4/4.
RELOAD_READY_SETTLE_S = 0.5


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

    # ── MODEL.md's collection path ──

    # How much longer than "the last round has left" the trigger is held, in
    # bullet intervals. The interval is an ESTIMATE here and nothing watches
    # the counter during the burst, so this covers the estimate being low: at
    # 1.5 intervals a 42-round magazine tolerates the rate being off by 3.5%
    # and still fires every round. Overshooting costs nothing -- the magazine
    # empties, PUBG starts its own reload, and the recording is already over.
    FIRE_MARGIN_INTERVALS = 1.5
    # Recording continues this long after the trigger is released, so the last
    # round's kick lands inside the samples rather than after them. The
    # compensation has already stopped (get_recoil_delta returns early when
    # `firing` is false), so these frames are pure recoil settling.
    TAIL_S = 0.35
    # Baseline before the click. The first frame is the ANCHOR every later
    # sample is measured against, and it has to describe a still view --
    # y_obs(t) is displacement from it, so an anchor taken mid-motion tilts
    # every sample in the magazine by the same amount.
    PREFIRE_S = 0.12

    def fire_magazine_timed(self, grabber, mag_size, interval_s):
        """L1 — hold the trigger for one magazine, sampling on the
        compositor's clock. -> dict(t_click, t, patches, n_missed, n_frames).

        `t` is seconds since the click, NEGATIVE for the prefire baseline.
        That sign is deliberate: MODEL.md puts the origin at the click, and a
        prefire sample is genuinely before it. Their displacement should be
        ~0, which makes them a free check on whether the view was still.

        WHAT THIS DOES NOT DO, AND WHY
        ------------------------------
        It does not read the ammo counter, not once, not every third frame.
        Polling it forced the per-frame grab to cover a box from the patches
        at y=592 down to the HUD at y=1366 -- and DXGI has ONE bounding box,
        so that is 1.37 Mpx and 3.90 ms against 0.45 Mpx and 1.72 ms for the
        patches alone. At a 6.06 ms frame budget that is the difference
        between sampling at the refresh rate and sampling at half of it.

        So the magazine's end is ESTIMATED from its size and rate, and the
        trigger is released on a timer. Nothing downstream depends on that
        estimate being right: the samples are placed on the clock, not in
        bullet bins, and how many rounds actually went out is read from the
        counter afterwards.

        ⚠ THE FIRMWARE RELEASES THE TRIGGER, not this loop. CMD_CLICK carries
        its own duration, so a crash in the sampling loop cannot leave the
        button held -- which the older path could only prevent with a finally
        that had to run.
        """
        ts, patches = [], []
        t_pre = time.perf_counter()
        # Sample the baseline first so the anchor exists before the click. A
        # frame is only appended when the compositor actually produced one, so
        # this is a duration and not a count.
        while time.perf_counter() - t_pre < self.PREFIRE_S:
            t, f = grabber.grab_timed()
            if f is not None:
                ts.append(t)
                patches.append(f)
        hold_s = (max(0, mag_size - 1) + self.FIRE_MARGIN_INTERVALS) * interval_s
        t_click = self.mouse.click(buttons=0x01,
                                   duration_ms=int(hold_s * 1000))
        stop = t_click + hold_s + self.TAIL_S
        while time.perf_counter() < stop:
            t, f = grabber.grab_timed()
            if f is not None:
                ts.append(t)
                patches.append(f)
        return {
            't_click': t_click,
            't': [x - t_click for x in ts],
            'patches': patches,
            'hold_s': hold_s,
            'n_frames': len(ts),
            'n_missed': getattr(grabber, 'n_missed', 0),
            'n_prefire': sum(1 for x in ts if x < t_click),
        }

    # ── the firmware's compensation ──

    def arm(self, weapon):
        """L0 — Load this weapon's curve into the Pico and switch compensation on.
        Nothing confirms it: the return is len(dy_s), the array you passed
        in, not anything the firmware said back.

        ⚠ --no-comp LIVES ONE LAYER UP, in sweep.Rig.arm(), which arms then
        disarms so the pattern is uploaded but OFF. Reach past it and
        compensation goes ON — the run meant to measure raw recoil measures
        it compensated, under a filename that says otherwise.

        -> how many rounds the pattern covers, which is what a caller logs.

        Nine files did these two lines by hand through rig.mouse, and the
        count is the point: the repo has 13 sites that switch compensation ON
        and 4 that switch it OFF. The asymmetry is not an accident of style,
        it is the shape of the bug 5k found -- a run that dies between them
        leaves the firmware compensating for a gun nobody is holding, and
        three agents share this one serial port, so the next run measures that
        instead of what it fired.

        Pair it with disarm() in a finally, or let Rig.close() do it.
        """
        self.mouse.upload_pattern(weapon.dx_s, weapon.dy_s, weapon.t_s)
        self.mouse.set_recoil_enabled(True)
        return len(weapon.dy_s)

    def disarm(self, clear=False):
        """L1 — Compensation off, CONFIRMED BY READING THE FIRMWARE BACK.

        ⚠ False now has three causes and they are not the same: the write
        failed, the firmware still says enabled, or the firmware is too old
        to answer. The third is reported as a refusal on purpose — "cannot
        tell" rounded to "disarmed" is exactly the hole this closed.

        WHAT IT USED TO BE, because the shape recurs: `set_recoil_enabled`
        is a one-way write with no readback, and press/pico_mouse._write
        swallowed SerialTimeoutException — the CDC backpressure it documents
        as normal — with a bare `pass`. So a dropped disarm left the pattern
        running and this returned True. calibration/sweep.py's `--no-comp`
        guard is `if not self.fire.disarm(): raise`, which means the run that
        exists to measure UNCOMPENSATED recoil had a safety net that could
        not fire. Same shape as control/inventory.py's right_click_unequip:
        it checked, and it checked something that is true either way.

        Idempotent. Does NOT swallow the failure: a disarm that throws used
        to be caught and passed over in four separate teardown paths.
        """
        try:
            self.mouse.set_recoil_enabled(False)
            if clear:
                self.mouse.clear_pattern()
        except Exception as e:
            print(f'  [!] FIRMWARE STILL ARMED: {e}', flush=True)
            return False

        # The readback, and the reason it is not wrapped in a "if available"
        # is that an unanswerable question is a failed disarm here. The one
        # caller that must not proceed on a guess is the --no-comp arm.
        try:
            state = self.mouse.recoil_enabled()
        except Exception as e:
            print(f'  [!] FIRMWARE STILL ARMED: could not read it back: {e}',
                  flush=True)
            return False
        if state is None:
            print('  [!] FIRMWARE STILL ARMED (unconfirmed): this build has '
                  'no enable readback. Reflash before trusting --no-comp.',
                  flush=True)
            return False
        if state:
            print('  [!] FIRMWARE STILL ARMED: it reports compensation ON '
                  'after being told to stop.', flush=True)
            return False
        return True

    # ── the ammo counter ──

    def ammo_sig(self, frame):
        """R — The ammo region binarised: a mask for "did these pixels
        change". Not a count — read_ammo() says how many rounds are left,
        and this one structurally cannot.

        ⚠ COUNTING ITS CHANGES OVER-COUNTS ROUNDS BY ~2.4x. It is the
        fallback for a weapon whose digits will not read, and wait_reload
        prints a line when it has to settle on it, because "the pixels
        stopped" is far weaker evidence than "it reads 30".
        """
        g = cv2.cvtColor(frame['ammo'], cv2.COLOR_BGR2GRAY)
        return cv2.threshold(g, AMMO_THRESH, 255, cv2.THRESH_BINARY)[1] > 0

    def read_ammo(self, frame=None):
        """R — Rounds left in the magazine, or None if the number is not drawn.
        One classify() on the ammo crop; ammo_sig() is the pixel signature
        beside it, which cannot say how many.

        ⚠ None is not zero — and it is not "no counter" either: the bare
        `except Exception` swallows FocusLost, so a game that left the
        foreground reads as an unreadable counter and every caller keeps
        polling the desktop.

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
        """R — How many rounds are actually loaded, read off the HUD. Polls
        read_ammo(); it neither fires nor reloads, so top_up() is what makes
        the magazine full before you ask.

        ⚠ IT ANSWERS WITH WHATEVER THE COUNTER SAYS NOW, and the counter
        refills PARTWAY THROUGH the reload animation — asked early it hands
        a half-filled magazine back as a capacity. An ext_ar AUG (42) came
        back 40 and comparable() refused the cell outright.

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
            # `is not None and n > 0`, not `if n`. Zero is a READING —
            # read_ammo spends a paragraph on "None is NOT zero. An empty
            # magazine still draws `0`" — and folding it in here made an
            # emptied gun indistinguishable from a counter that would not
            # read: both polled to the timeout and returned None.
            if n is not None and n > 0:
                return n
            time.sleep(0.05)
        return None

    def top_up(self, settle_s=0.4, tries=3):
        """L2 — Reload by hand, wait it out, and read back what is in there,
        retrying until two reads agree. The only entry here that presses R;
        wait_reload() only watches for one to finish.

        ⚠ RELOADING DROPS OUT OF ADS, so this runs BEFORE the aim is set.
        Topping up after ensure_posture fired the first magazine from the
        hip and reported +498 counts of residual on a gun that had measured
        -31 an hour earlier with the same compensation.

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

        ⚠ IT RELOADS UNTIL TWO READS AGREE, and that is not belt-and-braces.
        This is the ONE caller that cannot pass `expect` to wait_reload -- it
        is the thing that measures the capacity -- so it falls back to "above
        zero and no longer changing", and a refill that pauses on its way up
        satisfies that. Measured 2026-08-06: an AUG wearing an ext_ar (42) came
        out of top_up reading 40, its cell fired 40 rounds, and the sibling
        `grip` cell fired 42. analyse_factors.comparable() then REFUSED the
        pair outright -- "recoil accumulates over the magazine, so a cell cut
        short measures less of the curve, not less recoil per bullet" -- so the
        whole run produced two good cells and no factor.
        A full magazine ignores R entirely, so the second press costs nothing
        when it was already full and fixes it when it was not.
        """
        # ⚠ THE PAIR MUST COME FROM THE SAME RELOAD, and it did not. `rounds`
        # was carried forward across attempts (a later read of None keeps the
        # earlier count, deliberately -- see the fall-through below) while
        # `reload_s` was overwritten every pass, so the tuple could describe
        # attempt 1's magazine and attempt 3's duration. Nothing downstream
        # can see that: both are plain floats and the cell records them side
        # by side as if they were one measurement.
        rounds = reload_s = None
        rounds_from = None          # the reload_s that produced `rounds`
        for attempt in range(max(1, tries)):
            self.mouse.key(HID_KEY_R, 60)
            time.sleep(settle_s)
            this_reload_s = self.wait_reload()
            reload_s = this_reload_s
            # Read AFTER the wait, not before: the counter refills partway
            # through the animation, so a count taken early is whatever the
            # reload had got to. This is the cell's expected round count and
            # every magazine in it is checked against this number.
            got = self.magazine_size()
            if got is not None and got == rounds:
                return rounds, rounds_from
            if got is not None and rounds is not None and got < rounds:
                # Going DOWN is not a top-up finishing, it is a misread or a
                # round leaving. Keep the larger and say so.
                print(f"      [!] the counter fell {rounds} -> {got} across "
                      f"top-up attempts; keeping {rounds}")
                return rounds, rounds_from
            if rounds is not None:
                print(f"      [!] the magazine was not full: {rounds} -> "
                      f"{got} after another reload")
            if got is not None:
                rounds, rounds_from = got, this_reload_s
        # rounds_from, not reload_s: if the last attempt read no counter at
        # all, `rounds` still belongs to an earlier pass and so must its
        # duration. None here is honest -- it says the surviving count came
        # from a reload whose time was never measured.
        return rounds, rounds_from

    # ── one magazine ──

    def fire_magazine(self):
        """L1 — Hold the trigger for one magazine and bring back what the
        game said while it went. One burst, no retry: top_up() is the L2
        beside it, the one that loops until the screen agrees.

        ⚠ ADS IS MEASURED, NOT ENFORCED. ads_frac is a number for the
        caller's gate; a magazine fired from the hip comes back looking
        exactly like a good one and is then analysed at the scoped K of 1.55
        against the hip's 0.50 — one such burst reported +498.
        """
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

    def wait_reload(self, expect=None):
        """R — PUBG reloads by itself; this only waits it out (and it exits ADS).
        IT PRESSES NOTHING — top_up() is what sends R.

        ⚠ WHEN IT RETURNS THE ANIMATION MAY STILL BE PLAYING, and PUBG eats
        right click while it does: 2000 ms after the counter stopped falling
        took 0/4, 2300 ms 3/4. Only closed-loop ADS is safe next.

        READS THE NUMBER. `expect` is the magazine size, and when the counter
        gets back to it the reload is over — stated by the game, not inferred
        from a timer. Give it and this returns the moment the digits say the
        rounds are in; leave it None (top_up, which is what MEASURES the size)
        and the rule relaxes to "the number is above zero and has stopped
        changing for RELOAD_HOLD_S".

        WHY THAT REPLACED A FIXED WAIT. The old path watched the ammo region's
        BINARISED PIXELS settle and then slept SETTLE_AFTER_RELOAD_S = 1.8 s on
        top, because the counter refills partway through the animation and the
        pixels cannot tell how far in they are. The digits can: 30 of 30 is not
        a guess about the animation, it is the animation's result. An operator
        watching a VSS run on 2026-08-06 asked for exactly this — "不要等，而是
        看子弹数字".

        ⚠ THE 1.8 s WAS PAYING FOR SOMETHING REAL, and this does not repay it
        with another constant. PUBG ignores right click while the reload plays:
        measured on an m416 over four magazines, a click 2000 ms after the
        counter stopped falling took 0/4, at 2300 ms 3/4, at 2400 ms 4/4. What
        makes it safe to drop is that the thing that needed the wait —
        GunDriver.ensure_ads — is CLOSED LOOP and retries: a swallowed click
        costs one more click, not a hip-fired magazine. If that turns out to be
        wrong the symptom is a low ads_frac on the first magazine after each
        reload, which the gate already reports per magazine.

        The pixel path stays as the fallback for a weapon whose counter cannot
        be read, and it says so when it is used, because "settled" is much
        weaker evidence than "reads 30" and a run should not be able to take
        the weak one silently.
        """
        empty = self.ammo_sig(self.grab())
        t0 = time.perf_counter()
        prev, stable_since = None, None
        last_n, changed_at, same_reads = None, t0, 0
        digits_seen = False
        while True:
            now = time.perf_counter()
            if now - t0 >= RELOAD_TIMEOUT_S:
                break
            frame = self.grab()

            # ── what the game says ──
            n = self.read_ammo(frame)
            if n is not None:
                digits_seen = True
                if n != last_n:
                    last_n, changed_at, same_reads = n, now, 1
                else:
                    same_reads += 1
                # A single read is not a reading. The counter is mid-animation
                # for most of a reload and a lone frame that happens to parse
                # is the one most likely to have caught it half drawn --
                # ammo_detector refuses those on IoU, but two agreeing reads
                # cost ~20 ms and remove the question.
                if n > 0 and same_reads >= RELOAD_CONFIRM and (
                        (expect and n >= expect) or
                        (not expect and now - changed_at >= RELOAD_HOLD_S)):
                    # The counter is back; the ANIMATION is not necessarily
                    # over. See RELOAD_READY_SETTLE_S.
                    time.sleep(RELOAD_READY_SETTLE_S)
                    return now - t0

            # ── fallback: this weapon's counter does not read ──
            sig = self.ammo_sig(frame)
            if prev is not None and float(np.mean(sig != prev)) < AMMO_CHANGED:
                if stable_since is None:
                    stable_since = now
            else:
                stable_since = None
            prev = sig
            if digits_seen:
                continue        # the digits are answering; do not guess over them
            if stable_since is None or now - stable_since <= RELOAD_STATIC_S:
                continue
            # Normally the counter visibly climbs back and holds. But the
            # magazine can refill inside the 0.55 s the fire loop spends
            # confirming the gun is empty — a quickdraw magazine is that fast —
            # and then `empty` is already the full reading and no change ever
            # comes. Settled plus long enough is the same evidence.
            if float(np.mean(sig != empty)) > AMMO_CHANGED or \
                    now - t0 > RELOAD_MIN_S:
                print("      [reload] the counter never read — settled on "
                      "pixels instead, which cannot say how many rounds are in")
                time.sleep(SETTLE_AFTER_RELOAD_S)
                return now - t0
        if self.gun is not None:
            self.gun.dump('reload')
        # Which half failed is the whole diagnosis: digits that read and never
        # reached `expect` is a reload that did not happen, digits that never
        # read at all is the counter being unreadable on this weapon.
        print(f"      [reload] timed out after {RELOAD_TIMEOUT_S:.0f}s "
              f"(last counter reading {last_n!r}"
              f"{'' if expect is None else f', wanted >= {expect}'})")
        return None
