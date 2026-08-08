"""Raw per-frame samples, kept forever. The store MODEL.md is built on.

    from calibration.samples import Magazine, append, load

    append(Magazine(weapon='m416', config={...}, sight='red_dot', K=1.5474,
                    t=[...], dy_px=[...], curve=[{'t_ms':13,'dy':40.0}, ...]))

    mags = load('m416', config={...})
    for m in mags:
        t, y = m.y_true_counts()      # one estimate of the SAME function

WHY A NEW STORE
---------------
The per-frame measurements already exist -- MagazineResult carries `ts` and
`dy` -- and analyse() throws them into 42 bullet bins and discards them. Three
thousand magazines have been fired and not one raw sample survives; the records
hold `per_bullet_counts` and aggregates like `mean_mad`, and nothing else.

That is why questions as basic as "does the per-pair correlation noise scale
with the displacement" cannot be answered from four months of data.

WHAT MAKES POOLING LEGAL
------------------------
Every magazine was fired under a DIFFERENT compensation curve -- the curve is
being rewritten as the run goes. So `y_obs` alone is not comparable between
magazines. But

    y_true(t) = y_obs(t) + y_comp(t)

and if each magazine records THE CURVE THAT WAS PLAYING, every magazine's
y_true is an estimate of the same function, whatever curve it was fired under.

⚠ SO THE CURVE IS STORED BY VALUE, NOT BY NAME. `data/curves/m416_att.json`
is overwritten every time the fit runs; a magazine that recorded the name would,
a week later, be reconstructed against a curve that did not exist when it was
fired. Every sample in this store would then be quietly wrong, and the store's
whole reason for existing is that it does not need re-collecting.

⚠ NOTHING IS EVER DELETED FROM HERE. A magazine that looks bad is a magazine
that the clustering will put outside the main cluster, which is a decision made
at fit time with all the other magazines visible -- not a decision made at
collection time by whichever gate happened to be in fashion. The gates that
used to drop magazines at collection time are the reason there is no data on
what they dropped (root CLAUDE.md: "闸门会审查掉你用来重调它的数据").
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIR = os.path.join(ROOT, 'calibration', 'artifacts', 'recoil', 'samples')

# Schema version. Bumped when a field changes meaning rather than when one is
# added -- readers skip unknown fields, so additions are free, but a field whose
# UNITS change silently reinterprets every stored magazine.
VERSION = 1


def config_key(config):
    """A stable filename fragment for an attachment set.

    Sorted, so {'muzzle':'comp_ar','grip':'vert'} and the same dict built in
    the other order land in the same file rather than two.
    """
    if not config:
        return 'bare'
    items = sorted((str(k), str(v)) for k, v in config.items() if v)
    return '_'.join(f'{k}-{v}' for k, v in items) or 'bare'


def comp_counts_at(curve, t_s):
    """Compensation DELIVERED by time t, in mouse counts. Vectorised over t.

    This reproduces the firmware, not an idealisation of it
    (pico_firmware/src/main.c, get_recoil_delta + bullet_duration):

      - each knot i starts at `t_ms` and its delta is spread EVENLY over
        `dur_i` ms, so the cumulative curve is piecewise LINEAR between knots,
        not a staircase of pulses
      - `dur_i = t_ms[i+1] - t_ms[i]`, and the LAST knot reuses the gap before
        it -- it has no next one, and the hardcoded 100 ms this replaced smeared
        the Vector's final round over nearly two rounds' worth of time
      - the knot times already include RECOIL_FIRE_DELAY_MS; upload_pattern
        applies it before sending, so t is measured from the CLICK

    ⚠ Only one spread is active at a time. The firmware's while-loop overwrites
    `spread_dy_per_ms` when it reaches a new knot, so a knot whose whole window
    is skipped delivers NOTHING rather than catching up. With 17 ms knots and a
    1 ms tick that cannot happen; it is modelled as the exact piecewise-linear
    sum below, which is only correct while that holds.

    ⚠ `dur_ms` IS USED WHEN THE KNOT CARRIES IT, and PicoMouse.read_pattern()
    puts it there because the FIRMWARE computed it. Re-deriving it from the
    knot spacing here would compare this file against itself -- and the last
    knot's duration is precisely the one that has been wrong before (it used
    to be a hardcoded 100 ms, which on a Vector smeared the final round over
    nearly two rounds' worth of time).
    """
    t = np.atleast_1d(np.asarray(t_s, dtype=float)) * 1000.0
    if not curve:
        return np.zeros_like(t)
    tk = np.array([float(k['t_ms']) for k in curve])
    dy = np.array([float(k.get('dy', 0.0)) for k in curve])
    if all('dur_ms' in k for k in curve):
        dur = np.array([float(k['dur_ms']) for k in curve])
    elif len(tk) == 1:
        dur = np.array([100.0])
    else:
        gaps = np.diff(tk)
        dur = np.concatenate([gaps, gaps[-1:]])
    dur = np.where(dur < 1.0, 1.0, dur)
    # frac[i, j] = how much of knot i has been delivered by time t[j]
    frac = np.clip((t[None, :] - tk[:, None]) / dur[:, None], 0.0, 1.0)
    return (dy[:, None] * frac).sum(axis=0)


@dataclass
class Magazine:
    """One burst, as measured. Times are seconds SINCE THE CLICK."""

    weapon: str
    sight: str
    K: float                                  # screen px per mouse count
    config: dict = field(default_factory=dict)
    posture: str = 'standing'

    # The curve that was playing, BY VALUE: [{'t_ms': int, 'dx':, 'dy':}, ...]
    # Empty list means the compensation was off, which is a legitimate and very
    # useful magazine -- it measures y_true directly.
    curve: list = field(default_factory=list)
    comp_enabled: bool = True

    # ── the samples ──
    # t[i] is frame i's PRESENT time (capture/dxgi_time) minus the click.
    t: list = field(default_factory=list)
    # Frame-to-frame view shift in screen pixels, len(t) - 1 of them, aligned so
    # dy_px[i] is the shift between frame i and frame i+1.
    dy_px: list = field(default_factory=list)
    # What the HAND contributed over the same pair, in counts, off the Pico's
    # passthrough. Screen motion is hand + compensation + recoil.
    human_dy: list = field(default_factory=list)
    # Pairs the correlator could not place (peak wrapped). NOT dropped here --
    # dropping is a fit-time decision and this store does not make those.
    oor: list = field(default_factory=list)

    # When the trigger was released, seconds after the click. THE FIRMWARE
    # STOPS COMPENSATING THERE -- get_recoil_delta returns early when `firing`
    # is false -- so the curve does not play to completion unless the trigger
    # outlasts it. Modelling it as if it did over-states y_comp on every frame
    # after the release, and since y_true = y_obs + y_comp that lands directly
    # on the answer. 0 means "not recorded"; the curve is then integrated in
    # full, which is what the pre-2026-08-08 magazines assumed.
    hold_s: float = 0.0

    # ── context, for clustering and for asking questions later ──
    magazine_size: int = 0
    ads_frac: float = float('nan')
    fps: float = float('nan')
    ts: str = ''
    note: str = ''
    version: int = VERSION

    # ── derived ──

    def y_obs_counts(self):
        """Cumulative screen motion since the first frame, in counts.

        ⚠ A frame-to-frame shift describes an INTERVAL, so the cumulative sum
        after k intervals is the position at frame k -- shift[i] belongs to
        t[i+1], never to t[i]. Returning it against t[:-1] would put every
        sample one frame early, the same class of error as the bins this store
        replaces.

        ⚠ AND FRAME 0 IS A SAMPLE, worth 0 by definition. It is the anchor, so
        its position is exactly known -- that is a statement about the origin,
        not a shift attributed to it. Leaving it out cost the fitter its first
        grid column: every magazine started one frame late, the column was
        all-NaN, and nanmedian returned NaN for the one point on the curve
        whose value is not in doubt.
        """
        dy = np.asarray(self.dy_px, dtype=float)
        human = (np.asarray(self.human_dy, dtype=float) if self.human_dy
                 else np.zeros_like(dy))
        oor = (np.asarray(self.oor, dtype=bool) if self.oor
               else np.zeros(len(dy), dtype=bool))
        counts = dy / self.K + human
        counts = np.where(oor, np.nan, counts)
        return (np.asarray(self.t, dtype=float),
                np.concatenate([[0.0], np.nancumsum(counts)]))

    def y_true_counts(self):
        """(t, y_true) -- this magazine's estimate of the weapon's own recoil.

        The whole point of the store: whatever curve was playing gets added
        back, so magazines fired under different curves are comparable.

        ⚠ The compensation is frozen at `hold_s`. The firmware only plays it
        while the trigger is down, so a curve longer than the burst is not
        delivered in full -- and the difference goes straight into y_true,
        because nothing else in this expression can absorb it.
        """
        t, y_obs = self.y_obs_counts()
        if not self.comp_enabled:
            return t, y_obs
        tc = np.minimum(t, self.hold_s) if self.hold_s > 0 else t
        return t, y_obs + comp_counts_at(self.curve, tc)

    def n_frames(self):
        return len(self.t)


# ── disk ──

def path_for(weapon, config=None):
    return os.path.join(SAMPLE_DIR, f'{weapon}__{config_key(config)}.jsonl')


def append(mag: Magazine):
    """One line per magazine, append-only."""
    p = path_for(mag.weapon, mag.config)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    d = asdict(mag)
    # Round the arrays: 3 decimals on a pixel shift is a thousandth of a pixel,
    # far under the correlator's resolution, and it halves the file.
    d['t'] = [round(float(v), 6) for v in mag.t]
    d['dy_px'] = [round(float(v), 3) for v in mag.dy_px]
    d['human_dy'] = [round(float(v), 3) for v in mag.human_dy]
    d['oor'] = [bool(v) for v in mag.oor]
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps(d, ensure_ascii=False) + '\n')
    return p


def load(weapon, config=None, path=None):
    """Every magazine ever stored for this weapon+config. [] when none."""
    p = path or path_for(weapon, config)
    if not os.path.exists(p):
        return []
    out = []
    for ln in open(p, encoding='utf-8'):
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        known = {f for f in Magazine.__dataclass_fields__}
        out.append(Magazine(**{k: v for k, v in d.items() if k in known}))
    return out


def configs_for(weapon):
    """Which configs have samples on disk."""
    if not os.path.isdir(SAMPLE_DIR):
        return []
    pre = f'{weapon}__'
    return sorted(f[len(pre):-6] for f in os.listdir(SAMPLE_DIR)
                  if f.startswith(pre) and f.endswith('.jsonl'))
