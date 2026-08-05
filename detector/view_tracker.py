"""ViewTracker — measures view rotation from centre-band screen patches.

The capture thread only slices and stores patches (~0.02 ms/frame); all the
FFT work happens afterwards on a worker, where the budget is one magazine
rather than one frame. See docs/recoil_observer_design.md.

Geometry: patches sit on the screen's vertical centre line, where a pitch
rotation produces a pure translation (gain = 1, pitch->yaw crosstalk < 1%,
both independent of focal length). Anywhere else the patch undergoes an
affine warp and phaseCorrelate — which assumes pure translation — is biased.

Usage:
    tracker = ViewTracker()
    grabber, paced = make_grabber(tracker.regions())

    rec = MagazineRecorder(tracker)
    while firing:
        rec.push(time.perf_counter(), grabber.grab())   # cheap
    result = rec.finish()                                # batch FFT
"""
from dataclasses import dataclass, field

import cv2
import numpy as np

from config import (SCREEN_H, RECOIL_BAND_Y, RECOIL_PATCH, RECOIL_PATCH_H,
                    RECOIL_PATCH_XS, RECOIL_GATE_MIN, RECOIL_MAD_FLOOR,
                    RECOIL_MAD_K, RECOIL_CHANNEL)


@dataclass
class FrameMeasurement:
    """One frame pair's view shift, in screen pixels.

    Sign convention (screen coordinates, NOT rotation angles):
      dy > 0  content slid DOWN  = view rotated UP    = recoil direction
      dy < 0  content slid UP    = view rotated DOWN  = compensation direction
      dx > 0  content slid RIGHT = view rotated LEFT
    """
    dx: float
    dy: float
    per_patch_dy: list          # raw per-patch dy, before rejection
    per_patch_dx: list
    mad: float                  # median abs deviation across patches
    rejected: list              # bool per patch
    n_valid: int
    out_of_range: bool          # reading is further from the prediction than
                                # the correlator can legitimately travel
    low_gate: int               # patches below the texture floor


@dataclass
class MagazineResult:
    """Everything a human or a learner needs from one burst."""
    ts: list = field(default_factory=list)
    dy: list = field(default_factory=list)          # per-frame, robust
    dx: list = field(default_factory=list)
    per_patch_dy: list = field(default_factory=list)
    mad: list = field(default_factory=list)
    n_rejected: list = field(default_factory=list)
    out_of_range: list = field(default_factory=list)
    gates: list = field(default_factory=list)
    # Mouse counts the HAND contributed over the same frame pair, straight off
    # the Pico's passthrough. Screen motion is hand + compensation + recoil, so
    # without this term any nudge during a burst is booked as recoil.
    human_dy: list = field(default_factory=list)
    human_dx: list = field(default_factory=list)

    def cumulative_dy(self):
        return np.cumsum(np.nan_to_num(self.dy))

    def n_frames(self):
        return len(self.ts)


class ViewTracker:
    """Slices centre-band patches and turns frame pairs into a view shift."""

    def __init__(self, patch_xs=None, band_y=None, patch=None,
                 gate_min=None, channel=None, patch_h=None):
        self.xs = tuple(patch_xs if patch_xs is not None else RECOIL_PATCH_XS)
        self.patch = patch if patch is not None else RECOIL_PATCH        # width
        self.patch_h = patch_h if patch_h is not None else RECOIL_PATCH_H
        # Height is what has to cover the motion, so the band recentres with it
        # rather than staying wherever a square patch happened to put it.
        self.band_y = (band_y if band_y is not None else
                       RECOIL_BAND_Y if patch_h is None else
                       (SCREEN_H // 2 - self.patch_h // 2))
        self.gate_min = gate_min if gate_min is not None else RECOIL_GATE_MIN
        self.channel = channel if channel is not None else RECOIL_CHANNEL
        # Pre-built so it is never rebuilt inside the measurement loop.
        self._win = cv2.createHanningWindow((self.patch, self.patch_h),
                                            cv2.CV_32F)
        # Over-range shows up as an FFT wraparound of exactly one patch height:
        # the measurement is vertical, so width has nothing to do with it.
        self._wrap = float(self.patch_h)

    # ── capture-side ──

    def regions(self):
        """{name: (y, x, h, w)} for make_grabber / HUD_REGIONS."""
        return {f'recoil_{i}': (self.band_y, x, self.patch_h, self.patch)
                for i, x in enumerate(self.xs)}

    def names(self):
        return [f'recoil_{i}' for i in range(len(self.xs))]

    def slice_frame(self, frame):
        """Pull the patches out of a grabber frame dict as uint8, one channel.

        This is the only thing the capture thread runs — measured at
        ~0.02 ms for 7 patches. Returns None if the frame lacks the regions.
        """
        out = []
        for name in self.names():
            crop = frame.get(name)
            if crop is None:
                return None
            # Single channel avoids cvtColor and cuts the copy 3x. Which
            # channel barely matters; green carries the most luma weight.
            out.append(np.ascontiguousarray(crop[:, :, self.channel]))
        return out

    # ── measurement side (worker thread) ──

    def gate_score(self, patch_u8):
        """Gradient energy. Guards against degenerate frames (loading screen,
        flashbang whiteout, full-screen UI) — NOT a texture-quality filter:
        measured accuracy is uncorrelated with this score down to ~0.5."""
        p = patch_u8.astype(np.float32)
        gx = cv2.Sobel(p, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(p, cv2.CV_32F, 0, 1, ksize=3)
        return float(np.mean(gx * gx + gy * gy))

    def measure_pair(self, prev, cur, predicted_dy=0.0):
        """Measure the shift between two patch lists.

        `predicted_dy` is only used to recognise the over-range signature;
        patch-window pre-shifting (which extends usable range) belongs to the
        caller, which knows the pattern being injected.
        """
        dys, dxs, gates = [], [], []
        for a_u8, b_u8 in zip(prev, cur):
            a = a_u8.astype(np.float32)
            b = b_u8.astype(np.float32)
            (sx, sy), _ = cv2.phaseCorrelate(a, b, self._win)
            # phaseCorrelate returns b's translation relative to a, already in
            # screen coordinates. Recoil rotates the view up, which slides
            # content DOWN the screen, so recoil reads dy > 0 with no flip.
            dys.append(sy)
            dxs.append(sx)
            gates.append(self.gate_score(a_u8))

        dys_a = np.asarray(dys)
        dxs_a = np.asarray(dxs)
        gates_a = np.asarray(gates)

        med = float(np.median(dys_a))
        mad = float(np.median(np.abs(dys_a - med)))
        # MAD collapses to 0 when patches agree, so a floor is required or
        # every patch gets rejected on a clean frame.
        thresh = max(RECOIL_MAD_K * mad, RECOIL_MAD_FLOOR)
        rejected = (np.abs(dys_a - med) > thresh) | (gates_a < self.gate_min)

        keep = ~rejected
        if keep.any():
            dy = float(np.median(dys_a[keep]))
            dx = float(np.median(dxs_a[keep]))
        else:
            dy, dx = float('nan'), float('nan')

        # Beyond P/2 the FFT wraps around: a true +70 px shift reads as
        # 70-128 = -58. Matching the wrap distance exactly only works when the
        # prediction is good, which it is not at the start of a burst. Instead
        # flag any reading further from the prediction than half a patch —
        # the correlator cannot legitimately travel that far (usable range is
        # 3P/8), so such a value is untrustworthy whether it wrapped or not.
        out_of_range = bool(
            np.isfinite(dy) and abs(dy - predicted_dy) > self._wrap * 0.5
        )

        return FrameMeasurement(
            dx=dx, dy=dy,
            per_patch_dy=[round(v, 4) for v in dys],
            per_patch_dx=[round(v, 4) for v in dxs],
            mad=mad, rejected=rejected.tolist(),
            n_valid=int(keep.sum()), out_of_range=out_of_range,
            low_gate=int((gates_a < self.gate_min).sum()),
        )


class MagazineRecorder:
    """Buffers patches during fire, runs the FFTs once fire stops."""

    def __init__(self, tracker, max_frames=1500, drop_duplicates=True,
                 human_fn=None):
        """human_fn() -> (dx, dy) cumulative human mouse counts, or None to
        assume a still hand. Sampled per frame and differenced later, so its
        absolute value never matters and a dropped report costs nothing."""
        self.tracker = tracker
        self.max_frames = max_frames
        self.drop_duplicates = drop_duplicates
        self.human_fn = human_fn
        self._ts = []
        self._patches = []
        self._human = []
        self.n_duplicates = 0

    def _is_duplicate(self, p):
        """DXGI in video_mode re-serves the last frame when the screen is
        idle, so grab() can return the same pixels many times in a row. Those
        carry no motion information and would dilute any rate estimate, so
        they are dropped. Short-circuits on the first differing patch."""
        prev = self._patches[-1]
        for a, b in zip(p, prev):
            if not np.array_equal(a, b):
                return False
        return True

    def push(self, ts, frame):
        """Capture-thread call. Slices and stores; no FFT."""
        if len(self._patches) >= self.max_frames:
            return False
        p = self.tracker.slice_frame(frame)
        if p is None:
            return False
        if self.drop_duplicates and self._patches and self._is_duplicate(p):
            self.n_duplicates += 1
            return False
        self._ts.append(ts)
        self._patches.append(p)
        self._human.append(self.human_fn() if self.human_fn else (0, 0))
        return True

    def push_patches(self, ts, patches):
        """Same, when the caller already sliced (e.g. replay from disk)."""
        if len(self._patches) >= self.max_frames:
            return False
        self._ts.append(ts)
        self._patches.append(patches)
        self._human.append(self.human_fn() if self.human_fn else (0, 0))
        return True

    def clear(self):
        self._ts.clear()
        self._patches.clear()
        self._human.clear()
        self.n_duplicates = 0

    def span_s(self):
        """Wall-clock time actually covered by the stored frames."""
        return (self._ts[-1] - self._ts[0]) if len(self._ts) > 1 else 0.0

    def effective_fps(self):
        s = self.span_s()
        return (len(self._ts) - 1) / s if s > 0 else float('nan')

    def n_frames(self):
        return len(self._patches)

    def finish(self, predicted_dy=None):
        """Worker-thread call. Batch-measures every consecutive frame pair.

        Without an explicit prediction the previous frame's reading is used,
        which is what makes the out-of-range test meaningful: view rotation is
        continuous, so a jump of more than half a patch between consecutive
        frames is not physical.
        """
        # NOTHING BUFFERED AT ALL is a different fault from a thin buffer, and
        # it is the one that hides. push() drops a frame silently three ways --
        # buffer full, duplicate, and slice_frame() returning None -- and only
        # the third can account for ALL of them. slice_frame returns None when
        # the frame lacks a region the tracker asks for, i.e. this tracker and
        # the grabber that produced the frame disagree about how many patches
        # exist.
        #
        # That is exactly what went wrong with the VSS: calibration/sweep.py's
        # FireDriver held a 7-patch tracker by value while set_sight() had
        # rebuilt the grabber for the 3-patch vss_pso1 profile, so every push
        # asked for recoil_3..6 and got nothing. Four magazines, zero samples,
        # and not one line of output from here to analyse() -- the weapon had
        # simply never been measurable, for as long as anyone had tried.
        #
        # Said once, at the source, because the caller sees only an empty
        # result and cannot tell an empty magazine from a mis-wired one.
        if self._patches == [] and self._ts == []:
            print(f'[tracker] recorded 0 frames for {len(self.tracker.xs)} '
                  f'patches ({", ".join(self.tracker.names())}) — every push '
                  f'was dropped, which for a whole magazine means the grabber '
                  f'is not producing these regions')
        res = MagazineResult()
        prev_dy = 0.0
        for i in range(1, len(self._patches)):
            pred = predicted_dy[i - 1] if predicted_dy is not None else prev_dy
            m = self.tracker.measure_pair(self._patches[i - 1],
                                          self._patches[i], pred)
            res.ts.append(self._ts[i])
            res.dy.append(m.dy)
            res.dx.append(m.dx)
            res.per_patch_dy.append(m.per_patch_dy)
            res.mad.append(m.mad)
            res.n_rejected.append(len(m.rejected) - m.n_valid)
            res.out_of_range.append(m.out_of_range)
            res.gates.append(m.low_gate)
            res.human_dx.append(self._human[i][0] - self._human[i - 1][0])
            res.human_dy.append(self._human[i][1] - self._human[i - 1][1])
            if np.isfinite(m.dy) and not m.out_of_range:
                prev_dy = m.dy
        return res
