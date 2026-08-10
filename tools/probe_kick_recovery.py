"""Split each measured recoil curve into a per-shot kick and the between-shot
shape, and ask whether attachments act on those two separately.

The question this answers: measured whole-kit factors are not the product of
the single-slot factors (mp5k 3-part kit is +17% off, vector +9.7%), and the
coupling sits on ONE EDGE -- mp5k's grip x muzzle is +4.4 sigma while its
grip x stock and muzzle x stock are -2.0 and -0.7.  One candidate explanation
is that the engine IS multiplicative but on components we never measured,
because we collapse a whole y_true(t) into one scalar.  kick-vs-recovery is
the component split that the existing curves can resolve on their own: 17 ms
grid against a 53-100 ms shot interval is 3-6 knots per shot.

⚠ A ONE-PARAMETER LAW ON THAT EDGE DOES FIT, AND IT SHIPS: an absolute floor
under grip+muzzle, chi2 133.5 against 581.8 for the plain product, on
explain_factor's 'parts' tier (`pixi run kit-floor`).  That is a CORRECTION
that works on the two measured SMGs, not a mechanism -- the two guns disagree
on whether the stock belongs in it and chi2/dof is still ~19.  So what the
coupling physically IS remains open, and this probe is the attempt that did
not answer it.

ANSWER (2026-08-09, one run, NOT yet reproduced): the split is not supported,
and on the two guns that need it most it is not measurable at all.

  `period`  Per-shot structure is real and large.  Every curve names its own
            period by spectrum, and it lands on the ammo counter's interval to
            0.0-0.5 ms on 11 of 12 guns (snr 27-778, modulation 34-190%).  Two
            independent witnesses, and the scan never sees the counter.
            But the folded phase profile is POSITIVE IN EVERY BIN on all four
            guns checked -- there is no reverse-travel segment inside a burst.
            Recovery, as a thing that pulls the view back between shots, is not
            in this data at all.  mk14 is the one disagreement (+13.8 ms, snr
            6.0, 5.7% modulation) and it is semi-auto, so its bursts are not
            what the other rows are.

  `ratio`   Fails its own null: 22% of null curves fire at p<0.01 (nominal 1%),
            so the p-values in that table are NOT usable -- see NULL CHECK at
            the bottom of its output.  The one gun that is readable at all is
            aug (4.89 knots/shot, not grid-locked, 24-35 magazines, null passes
            at p=0.62), and there brake/vert_grip/comp_ar all read PURE SCALE.

  Why it cannot be pushed further on this data: shape needs >=4-5 knots/shot.
  mp5k is 3.83 and vector is 3.12 -- and those are exactly the two guns whose
  kits are furthest from multiplicative (+17.0%, +9.7%).  m416/m762/vss clear
  the knot count but their shot period is an integer multiple of the 17 ms grid
  (LOCKED), so a folded shape cannot be told from a sampling artefact.

  Still open, and now the better candidate: every curve carries dx == 0.000
  exactly.  The horizontal component was never measured, so if the engine is
  multiplicative per-axis, that is where it would still be hiding.

Stage 1 (`period`): does per-shot structure exist at all, and does the curve
name its own period?  The period is recovered from the curve by spectrum, NOT
assumed from weapon_rpm.json -- the ammo counter is then the second,
independent witness.  A curve whose own peak disagrees with the counter is
reported, never quietly aligned.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

CURVES = Path("data/curves")
RPM = Path("calibration/artifacts/recoil/weapon_rpm.json")


def load_curve(path: Path) -> dict:
    d = json.loads(path.read_text())
    dy = np.array([s["dy"] for s in d["shots"]], dtype=float)
    dt = np.array([s["delay_ms"] for s in d["shots"]], dtype=float)
    t = np.cumsum(dt) - dt[0]
    return {
        "weapon": d["weapon"],
        "config": d["config"],
        "name": path.stem,
        "grid_ms": float(d["grid_ms"]),
        "dy": dy,
        "t": t,
        "n_mag": d.get("n_magazines"),
        "sight": d.get("sight"),
        "posture": d.get("posture"),
        "total": float(d["total_counts"]),
    }


def comparable(a: dict, b: dict) -> str | None:
    """Why these two curves may NOT be divided, or None if they may.

    A ratio is only about the attachment if everything else about the two
    magazines is the same.  Sight and posture are recorded per curve, so both
    are checked rather than assumed -- CLAUDE.md's second cross-layer law: the
    thing the number describes must be the thing that was measured.
    """
    if a["sight"] != b["sight"]:
        return f"sight {b['sight']} vs {a['sight']}"
    if a["posture"] != b["posture"]:
        return f"posture {b['posture']} vs {a['posture']}"
    return None


def own_period(dy: np.ndarray, grid_ms: float, lo_ms: float = 40.0,
               hi_ms: float = 200.0) -> tuple[float, float, np.ndarray, np.ndarray]:
    """Period the curve names for itself, by least-squares single sinusoid.

    Detrended first: the curve's own decay is a huge low-frequency term that
    would otherwise swamp every candidate.  Returns (best_ms, snr, periods,
    power) where snr is the peak against the median of the scanned band.
    """
    n = len(dy)
    x = np.arange(n, dtype=float)
    # remove a smooth trend (the recoil envelope) so the scan sees modulation
    win = max(3, int(round(2.5 * hi_ms / grid_ms)) | 1)
    trend = np.convolve(np.pad(dy, win // 2, mode="edge"), np.ones(win) / win,
                        mode="valid")[:n]
    r = dy - trend
    r = r - r.mean()

    periods = np.arange(lo_ms, hi_ms, 0.25)
    power = np.empty_like(periods)
    for i, p_ms in enumerate(periods):
        w = 2 * math.pi * grid_ms / p_ms
        c, s = np.cos(w * x), np.sin(w * x)
        # projection onto the 2-d sinusoid basis
        power[i] = (r @ c) ** 2 + (r @ s) ** 2
    power /= max(len(r) / 2.0, 1.0)
    k = int(np.argmax(power))
    snr = power[k] / max(np.median(power), 1e-12)
    return float(periods[k]), float(snr), periods, power


def fold(dy: np.ndarray, grid_ms: float, period_ms: float, n_phase: int = 12,
         skip_ms: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Fold the curve at `period_ms` into n_phase bins. Returns (mean, sem)."""
    n = len(dy)
    t = np.arange(n) * grid_ms
    m = t >= skip_ms
    ph = ((t[m] % period_ms) / period_ms * n_phase).astype(int) % n_phase
    val = dy[m]
    mean = np.array([val[ph == i].mean() if (ph == i).any() else np.nan
                     for i in range(n_phase)])
    sem = np.array([val[ph == i].std(ddof=1) / math.sqrt(max((ph == i).sum(), 1))
                    if (ph == i).sum() > 1 else np.nan for i in range(n_phase)])
    return mean, sem


def cmd_period(args) -> None:
    rpm = json.loads(RPM.read_text())
    paths = sorted(CURVES.glob(f"{args.weapon}__*.json")) if args.weapon \
        else sorted(CURVES.glob("*__bare.json"))
    if not paths:
        raise SystemExit(f"no curves matched (weapon={args.weapon!r})")

    print(f"{'curve':46s}{'knots':>6s}{'own_ms':>8s}{'snr':>7s}"
          f"{'counter_ms':>11s}{'diff':>8s}{'mod_%':>7s}")
    for p in paths:
        c = load_curve(p)
        if len(c["dy"]) < 30:
            print(f"{c['name']:46s}{len(c['dy']):6d}   too short")
            continue
        best, snr, _, _ = own_period(c["dy"], c["grid_ms"])
        ref = rpm.get(c["weapon"], {}).get("interval_ms")
        mean, _ = fold(c["dy"], c["grid_ms"], ref if ref else best,
                       n_phase=args.phase, skip_ms=args.skip)
        mod = (np.nanmax(mean) - np.nanmin(mean)) / max(abs(np.nanmean(mean)), 1e-9)
        d = f"{best - ref:+7.1f}" if ref else "      -"
        r = f"{ref:11.2f}" if ref else "          -"
        print(f"{c['name']:46s}{len(c['dy']):6d}{best:8.2f}{snr:7.1f}{r}{d}"
              f"{mod * 100:7.1f}")


def band_power(r: np.ndarray, grid_ms: float, period_ms: float) -> float:
    """Sinusoid power at exactly `period_ms`, per sample."""
    x = np.arange(len(r), dtype=float)
    w = 2 * math.pi * grid_ms / period_ms
    r = r - r.mean()
    return ((r @ np.cos(w * x)) ** 2 + (r @ np.sin(w * x)) ** 2) / max(len(r) / 2.0, 1.0)


def cmd_ratio(args) -> None:
    """Stage 2: does an attachment change the WITHIN-SHOT shape, or only scale it?

    a(t) = y_att(t)/y_bare(t).  If the attachment is a pure scale, a(t) carries
    no shot-rate component.  Working on the ratio is what makes this legible on
    a LOCKED gun: any 17 ms-grid sampling artefact is common to both curves and
    divides out, so what is left at the shot period is the attachment's own
    doing.  The null is built from the same curve, not assumed: `bg` is the
    median power over off-shot periods, and `p_boot` is how often a random
    period in that band beats the shot period.
    """
    rpm = json.loads(RPM.read_text())
    weapons = [args.weapon] if args.weapon else sorted(
        {p.stem.split("__")[0] for p in CURVES.glob("*__bare.json")})

    print(f"{'curve':44s}{'mag':>4s}{'T_ms':>7s}{'f':>7s}{'shot_pw':>9s}{'bg':>8s}"
          f"{'ratio':>7s}{'p_boot':>8s}  verdict")
    skipped, rows = [], []
    for w in weapons:
        base = CURVES / f"{w}__bare.json"
        if not base.exists() or w not in rpm:
            continue
        b = load_curve(base)
        T = rpm[w]["interval_ms"]
        for p in sorted(CURVES.glob(f"{w}__*.json")):
            if p.stem.endswith("__bare"):
                continue
            c = load_curve(p)
            why = comparable(b, c)
            if why:
                skipped.append((c["name"], why))
                continue
            if (c["n_mag"] or 0) < args.min_mag:
                skipped.append((c["name"], f"only {c['n_mag']} magazines"))
                continue
            n = min(len(b["dy"]), len(c["dy"]))
            y0, y1 = b["dy"][:n], c["dy"][:n]
            keep = y0 > args.floor * np.abs(y0).mean()
            if keep.sum() < 24:
                continue
            a = y1[keep] / y0[keep]
            # detrend: the factor is a slow function of t (comp_ar spans 17%)
            win = max(3, int(round(3 * T / b["grid_ms"])) | 1)
            trend = np.convolve(np.pad(a, win // 2, mode="edge"),
                                np.ones(win) / win, mode="valid")[:len(a)]
            r = a - trend
            # the kept samples are irregular; score against the same index axis
            pw = band_power(r, b["grid_ms"], T)
            cand = np.arange(40.0, 200.0, 0.5)
            cand = cand[np.abs(cand - T) > 4.0]
            null = np.array([band_power(r, b["grid_ms"], q) for q in cand])
            bg = float(np.median(null))
            p_boot = float((null >= pw).mean())
            f = c["total"] / b["total"]
            is_null = abs(f - 1.0) < args.null_band
            v = ("SHAPE CHANGED" if p_boot < 0.01 else
                 "shape changed?" if p_boot < 0.05 else "pure scale")
            rows.append((c["name"], is_null, p_boot))
            print(f"{c['name']:44s}{c['n_mag'] or 0:4d}{T:7.1f}{f:7.4f}"
                  f"{pw:9.4f}{bg:8.4f}{pw / max(bg, 1e-12):7.2f}{p_boot:8.3f}  {v}"
                  f"{'   <- NULL' if is_null else ''}")
    # never filter an unrepeatable run silently -- CLAUDE.md
    print(f"\nskipped {len(skipped)}:")
    for name, why in skipped:
        print(f"  {name:56s} {why}")

    # The test's own false-positive rate, measured, not assumed.  A curve with
    # f ~ 1 barely moves the recoil, so a(t) is flat by construction and ANY
    # shot-rate power it reports is manufactured.  Printing this next to the
    # verdicts is the point: a self-consistent, arithmetically correct, blind
    # criterion is more dangerous than no criterion -- CLAUDE.md.
    nulls = [(n, p) for n, is_null, p in rows if is_null]
    if nulls:
        fp01 = sum(p < 0.01 for _, p in nulls) / len(nulls)
        fp05 = sum(p < 0.05 for _, p in nulls) / len(nulls)
        print(f"\nNULL CHECK  ({len(nulls)} curves with |f-1| < {args.null_band}, "
              f"where a(t) is flat by construction)")
        print(f"  p<0.01 fired on {fp01 * 100:5.1f}% of nulls   (nominal  1.0%)")
        print(f"  p<0.05 fired on {fp05 * 100:5.1f}% of nulls   (nominal  5.0%)")
        for n, p in sorted(nulls, key=lambda x: x[1]):
            print(f"    {n:54s} p={p:.3f}{'   FALSE POSITIVE' if p < 0.05 else ''}")
        if fp05 > 0.10:
            print("  => p-values above are NOT usable. The detrend leaves the")
            print("     residual autocorrelated, so the off-shot periods are not")
            print("     a valid null. Read only rows whose n_mag is large.")


def main() -> None:
    # No required subcommand: `pixi run kick-recovery` with no argument used to
    # exit 2 on argparse, so the task was red every time anyone ran it the
    # obvious way -- a task that always fails is worth less than no task.
    # Bare invocation runs both stages, which is what reading this probe means.
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("period", help="stage 1: does per-shot structure exist")
    p.add_argument("--weapon", default=None, help="all bare curves if omitted")
    p.add_argument("--phase", type=int, default=12)
    p.add_argument("--skip", type=float, default=0.0,
                   help="ignore the first N ms (first-shot transient)")
    p.set_defaults(func=cmd_period)
    q = sub.add_parser("ratio", help="stage 2: scale-only, or shape change?")
    q.add_argument("--weapon", default=None)
    q.add_argument("--floor", type=float, default=0.15,
                   help="drop knots where bare is below this fraction of mean")
    q.add_argument("--min-mag", type=int, default=4, dest="min_mag",
                   help="skip curves built from fewer magazines than this")
    q.add_argument("--null-band", type=float, default=0.035, dest="null_band",
                   help="|f-1| below this makes a curve a null for the self-check")
    q.set_defaults(func=cmd_ratio)
    a = ap.parse_args()
    if a.cmd is None:
        print("== stage 1: does per-shot structure exist ==")
        cmd_period(argparse.Namespace(weapon=None, phase=12, skip=0.0))
        print("\n== stage 2: scale-only, or shape change? ==")
        cmd_ratio(argparse.Namespace(weapon=None, floor=0.15, min_mag=4,
                                     null_band=0.035))
        return
    a.func(a)


if __name__ == "__main__":
    main()
