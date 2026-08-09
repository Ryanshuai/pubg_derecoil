"""Raw per-frame samples, kept forever. The store MODEL.md is built on.

    from calibration.samples import Magazine, append, load

    append(Magazine(weapon='m416', config={...}, sight='red_dot',
                    K=RECOIL_SIGHT_PROFILES['red_dot']['K'],
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

⚠ NOTHING IS DROPPED AT COLLECTION TIME. A magazine that looks bad is a
magazine the clustering will put outside the main cluster, which is a decision
made at fit time with all the other magazines visible -- not a decision made at
collection time by whichever gate happened to be in fashion. The gates that
used to drop magazines at collection time are the reason there is no data on
what they dropped (root CLAUDE.md: "闸门会审查掉你用来重调它的数据").

⚠ AND THAT RULE IS ABOUT THE MEASUREMENT, NOT ABOUT THE LABEL. This paragraph
used to open "NOTHING IS EVER DELETED FROM HERE", and on 2026-08-09 that became
false: 44 magazines in five quarantined files were deleted, having first been
offered every chance to be named. The distinction the old wording lost:

    a magazine with a bad READING        keep it. The fit can see it, weigh it
                                         against its siblings, and cut it with
                                         everything visible.
    a magazine with an unknown LABEL     nothing can ever pool it. Pooling is
                                         the only thing this store does, and a
                                         key nobody can write is not a cell.

The rescue is attempted first and it can succeed -- twelve mg3 magazines were
taken back the same day because their fire mode is measurable from the trace
rate, with a positive control on magazines the HUD had labelled. What cannot be
taken back is a file whose own name records that its label was INFERRED FROM
ITS OWN NUMBERS: renaming that makes it evidence for the number it was inferred
from. docs/game_quirks.md carries both halves of that audit.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ⚠ THE ONLY THING THIS STORE READS FROM config, and it reads it because
# fire_tag decides which FILE a magazine lands in. A second copy of that table
# here would be a second answer to "where does this data go".
import config as cfg                                            # noqa: E402

SAMPLE_DIR = os.path.join(ROOT, 'calibration', 'artifacts', 'recoil', 'samples')

# Schema version. Bumped when a field changes meaning rather than when one is
# added -- readers skip unknown fields, so additions are free, but a field whose
# UNITS change silently reinterprets every stored magazine.
VERSION = 1


# ⚠ RE-EXPORTED FROM config, NOT DEFINED HERE (2026-08-09). detector/weapon.py
# held a byte-identical second copy; the format now has one author and both
# layers import it. Re-exported rather than requiring callers to change import
# site: `samples.config_key` is what the store's own path_for uses and what a
# dozen files already say.
from config import config_key, parse_config_key            # noqa: E402,F401


def comp_counts_at(curve, t_s):
    """Compensation DELIVERED by time t, in mouse counts. Vectorised over t.

    ⚠ IT DOES REPRODUCE THE FIRMWARE, TO WITHIN ONE CARRIED COUNT, AND THAT IS
    NOW MEASURED -- `pixi run comp-counts` runs this against a tick-exact
    transcription of main.c's get_recoil_delta across a hold sweep. They differ
    by at most one count and the difference does NOT grow with hold duration.

    ⚠ THIS DOCSTRING SAID THE OPPOSITE FOR A DAY, and the retraction is worth
    more than the correction. It read "IT DOES NOT REPRODUCE THE FIRMWARE, AND
    THAT IS MEASURED", blaming the trend that tools/probe_delivery_path.py
    --hold-sweep found -- curve/move rising with how long the button was held,
    +0.0148/s and +0.0242/s -- on "the LATE, SMALL knots, where the firmware's
    int(accum)-with-carry lives".

    The TREND IS REAL AND STILL UNEXPLAINED. What was wrong is the attribution,
    and it was reached by elimination: pixels-per-count cannot know the hold
    duration, therefore the integration. That eliminates correctly and then
    names a mechanism the elimination never pointed at. Truncate-and-carry is
    EXACT in total -- whatever is truncated stays in the accumulator and comes
    out on a later tick -- so it could not have been the cause, and reading
    main.c would have said so before any measurement. MODEL.md 6.1 item 1 now
    carries it as an unlocated measurement difference instead of a broken
    function.

    ⚠ SO DO NOT "FIX" THIS FUNCTION AGAINST THE FIRMWARE. That work was
    proposed, scoped, and refuted by the gate before a line of it was written.

    What it MODELS (pico_firmware/src/main.c, get_recoil_delta +
    bullet_duration):

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
    #
    # ⚠ THE SIGN IS `+ human` AND THAT IS NOW MEASURED, not argued. It had
    # never been exercised: 2 nonzero values out of 131146 intervals across the
    # whole store, because every magazine is fired with nobody touching the
    # mouse. And the two sources DISAGREED on paper --
    # press/firmware/src/main.c:604 says "so the PC can SUBTRACT the hand",
    # this file adds it.
    #
    # Both are right, because the conventions are opposite: the firmware
    # accumulates raw_dy (mouse down positive) while the correlator reports
    # view rotation (up positive), so the hand arrives already negated.
    # tools/probe_human_sign.py, hand moved by a human for 12 s, per-FRAME
    # regression (a cumulative sum cannot do this -- it is destroyed by the
    # pitch clamp and by hip fire's K):
    #
    #     screen_px = -0.0464 * hand_counts,  r = -0.825, 162 moving frames
    #
    # Slope NEGATIVE -> `+ human` removes the hand. Settled.
    #
    # ⚠ BUT THE MAGNITUDE SAYS THE TERM DOES NOT YET WORK. |slope| is 0.0464
    # px/count against the red dot's K -- 33x short. If the screen and the hand
    # measured the same rotation over the same interval it would BE K. They do
    # not: `human_totals` is a snapshot of a cumulative counter while the
    # correlator's dy covers a whole frame interval, so the two are correlated
    # (r = -0.83) and not ALIGNED. Subtracting per frame removes about 3% of
    # the hand, not the hand.
    #
    # And the frames where a hand really moves are the ones nearest the
    # correlator's 128 px wrap ceiling, so they are the least trustworthy ones.
    # This term is no longer "unverified"; it is verified and known not to work
    # yet, which is a different and more useful state.
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
    # ⚠ `magazine_size` IS THE MAGAZINE'S IDENTITY, and the icon is not.
    # Measured 2026-08-08 on a freshly spawned mp5k, ONE frame for the picture
    # and the reading:
    #
    #     scope     red_dot         mse   45.9    runner-up  918.3
    #     muzzle    comp_smg        mse   32.0    runner-up  137.8
    #     grip      vert_grip       mse   34.5    runner-up 1198.8
    #     stock     tactical_stock  mse   50.8    runner-up 1844.4
    #     magazine  quickext_smg    mse  591.9    runner-up  874.0   <-- 1.48x
    #
    # Every other slot wins by 3-35x with an MSE under 51. The magazine's best
    # is 12x worse than any of them and ABOVE MSE_EMPTY_TH (450), so the reader
    # calls a plainly-occupied tile empty; across runs the same slot answered
    # '?', quick_smg and quickext_smg. That is why `magazine` is out of
    # RECOIL_SLOTS and why it must not be read back INTO the key.
    #
    # The count settles what the icon cannot, and it is the operator's rule:
    # read the number after the reload. A quickdraw-only magazine does not
    # change capacity, so `quick_smg` on a gun that fires 40 is impossible.
    # Confirmed from the chair: this gun wears a 快速扩容弹匣 and reads 40.
    #
    # ⚠ It cannot separate extended from extended-quickdraw -- same capacity.
    # That is fine and worth saying: quickdraw changes RELOAD SPEED, which is
    # invisible to y_true. What it does catch is a base magazine, which fires
    # 10 rounds fewer and shortens the burst, and nothing else in the record
    # would say so.
    magazine_size: int = 0
    # ⚠ THE SECOND WITNESS TO THE BURST LENGTH, and without it the first one
    # was never checked. `magazine_size` says how many rounds were in there;
    # this says how many were still in there when the trigger came up. Anything
    # above 0 means the hold was too SHORT -- the burst measured part of a
    # magazine while every number in the record described a whole one.
    #
    # Measured 2026-08-09 on the mg3, which is why it exists. The store's rate
    # for that gun is 59.97 ms/round, the trace's own autocorrelation says
    # 88-92 ms, so a hold sized for 75 rounds delivered about 50 and left ~25 in
    # the belt. Then the reload started from a part-full belt and the next
    # magazine read 58, then 36, then 50, then 41... a different burst length
    # every time, and the cell failed its arms check at 12.6% with nothing in
    # the output naming the cause. PUBG's mg3 is the one gun with TWO cyclic
    # rates (the fire-mode key switches it), so a single stored constant cannot
    # describe it -- but nothing here needs to know that: the counter says.
    #
    # None means not recorded (every magazine before this existed). 0 is a
    # different and much better answer: the magazine emptied.
    rounds_left: object = None
    # ⚠ IT IS PART OF THE GUN'S IDENTITY, NOT A SETTING, and it decides which
    # FILE this magazine lands in (path_for). GunDriver.FIRE_MODE_FOR has
    # carried "the MG3 has TWO automatic modes -- a slow one and a fast one"
    # since before any of this, and `ensure_fire_mode` presses B and watches
    # until the HUD agrees -- and NOTHING IN THE COLLECTION PATH EVER CALLED IT.
    # So both of this gun's stored rates were measured by whichever mode it
    # happened to spawn in, and neither magazine says which.
    #
    # 'full' / 'high' / 'single' / ... is the READBACK off the HUD, never the
    # request. 'unreadable' means the read ran and failed -- which is different
    # from None, and None here means only "fired before this field existed".
    fire_mode: object = None
    ads_frac: float = float('nan')
    # ⚠ TWO POINTS, NOT A RATE, and it exists because ads_frac is nan on
    # every magazine ever stored -- the timed firing path never wired that
    # up. True means the trigger released with the gun still scoped;
    # aim_and_scope's ensure_ads() is the other end of the bracket. None
    # means the read itself failed. It cannot see a dropout that recovers
    # mid-burst; it catches dropping out and STAYING out, which is worth
    # ~3x in K and is what actually happens.
    ads_end: object = None
    # ⚠ THE WITNESS FOR `sight`, WHICH IS OTHERWISE A FLAG. `sight` above is
    # what the run ASKED for; this is the raw optic asset `read_sight` took off
    # the gun in the same breath. collect_timed already refuses when the two
    # disagree -- but that refusal happened in another process on another day,
    # and a magazine that cannot be re-checked is a magazine whose sight is an
    # assertion. Root CLAUDE.md's second law is a program can only check a
    # thing that exists in TWO places; before this it existed in one.
    #
    # Deliberately NOT in RECOIL_SLOTS and NOT in config_key: the key has to be
    # stable and the magazine slot destabilised it once already. `sight` is
    # already how pools are separated (calibration/calibrate_scope.py splits on
    # it); this is the evidence for that split, not a second axis.
    #
    # '' means "not recorded", which is every magazine before 2026-08-09 and is
    # why calibrate_scope.audit() reports the count rather than dropping them.
    sight_asset: str = ''
    # ⚠ THE OTHER HALF OF RECOIL_FIRE_DELAY_MS, and the two must agree or the
    # loop diverges -- see y_true_counts for the L*omega argument.
    #
    # The offset shifts WHEN the firmware emits; this is how long an emitted
    # count takes to become a photon. Both are needed and they are different
    # numbers: the offset is chosen (it minimises drift, and with an amplitude
    # term in the residual that optimum is NOT -L), while this is measured
    # (tools/probe_input_latency.py, 40 trials: 21.7 ms by mean-minus-half-a-
    # frame, 18.3 by the minimum).
    #
    # Stored PER MAGAZINE rather than read from config at analysis time, for
    # the reason this file keeps paying for: a constant read later describes
    # the machine as it is NOW, and a magazine fired last week was fired
    # through a different display chain. None means "not recorded" -- the
    # magazines from before this existed -- and is treated as 0.
    comp_lag_s: object = None
    # ⚠ IT DOES HAVE TO BE RECORDED, AND THE ARGUMENT THAT IT DID NOT WAS
    # VERIFIED ON THE ONE CASE WHERE IT HOLDS. `--fire-delay-ms` carried
    # "Nothing needs recording: read_pattern returns the shifted times, so
    # curve[0]['t_ms'] IS the offset that played", checked against the batch
    # that reads +13.
    #
    # A POSITIVE offset is the only kind that survives the round trip.
    # upload_pattern FOLDS everything before t=0 into a step at t=0, because a
    # knot at a negative time is an instruction the firmware cannot obey -- so
    # every negative offset comes back reading exactly 0:
    #
    #     curve[0]['t_ms'] over the mp5k store:  {13: 10, 0: 50, 80: 5}
    #
    # Those 50 were fired at -90, -70, -50, -36, -30 and -10 and the record
    # cannot tell them apart. The offset sweep had to be re-armed from the KNOT
    # COUNT (the fold eats one knot per ~17 ms of lead), which is a signature,
    # not a reading -- and -36 and -50 produce the same 174.
    #
    # None means "not recorded", which is the honest state of those 50.
    fire_delay_ms: object = None
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
        counts = dy / analysis_k(self) + human
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

        ⚠ AND THE TWO TERMS ARE NOT ON THE SAME SIDE OF THE SCREEN unless
        `comp_lag_s` is set. `y_obs` is what the screen DID; the curve is what
        the firmware EMITTED, and emitted counts take L to become photons. So
        the honest expression is

            y_true(t) = y_obs(t) + C(t - L)

        ⚠ AND IT NEEDS NO eta -- eta IS NOISE, and that is the settled
        reading, not a preference between runs. It has been measured three
        times, as the disagreement between two interleaved arms:

            0.971   1.004   0.980      mean 0.9850 +- 0.0098
                                       1.52 sigma from 1.00

        and an arm difference carries sem 1.50% BY CONSTRUCTION (one arm n=8,
        per-magazine CV 3%) against an observed spread of 1.73%. Not a penny
        more. The 4.7 and 6.2 sigma that kept resurrecting it were computed
        against the WITHIN-RUN sem, which does not predict reproducibility.

        All four candidate mechanisms are separately refuted: SIZE (240
        one-count moves deliver full K), PATH (0.90% short, not 4%), recoil
        recovery (no trend over an 85x excursion), and the accounting error
        (comp_counts_at UNDER-states -- the wrong direction). A term that
        cannot say why it should affect y_true does not go in the model.

        The middle reading, THREE arms with the view's excursion spanning
        85x:

            curve 0    n=5   y_true(2.40s) 853.8 +-5.7   excursion 853.8
            curve 473  n=5                 853.5 +-3.9              428.8
            curve 945  n=5                 850.4 +-6.2               10.1

        0.4% across the lot. There is no delivery deficit, and the earlier
        0.9711 was one batch fitted by one coefficient -- the definition of
        overfitting, called "the strongest thing this model has ever said" at
        the time.

        ⚠ WHAT KILLED IT IS WORTH MORE THAN THE COEFFICIENT: between the two
        runs the two arms drifted about 2% in OPPOSITE directions (+1.64% and
        -1.98%). Interleaving guarantees one session; it does not guarantee
        the session has no structure inside it, and ~2% of opposed drift is
        enough to manufacture a 4.7 sigma arm difference. The gate after
        interleaving is REPLICATION, not a cleverer single run.

        Raised from the chair before it had bitten: "两边都知道这个负三十六的
        存在...不自洽的话会震荡，或者越来越错".

        ⚠ `comp_lag_s` IS None ON EVERY MAGAZINE FIRED BEFORE 2026-08-08, and
        None means "not recorded", which is treated as 0 -- exactly what those
        magazines' y_true already assumed. It is NOT rounded to the current
        constant: L is a property of the machine at the time of firing, and
        stamping today's value onto a magazine fired under an unknown one is
        the record describing a different object than the one measured.
        """
        t, y_obs = self.y_obs_counts()
        if not self.comp_enabled:
            return t, y_obs
        tc = np.minimum(t, self.hold_s) if self.hold_s > 0 else t
        lag = self.comp_lag_s or 0.0
        return t, y_obs + comp_counts_at(self.curve, np.maximum(tc - lag, 0.0))

    def n_frames(self):
        return len(self.t)


# ── disk ──

# ⚠ RE-EXPORTED FROM config (2026-08-09), same reason as config_key:
# detector/weapon.py's curve lookup needs this word and detector must not
# import calibration. Until it could say it, the lookup keyed (weapon,
# config, posture, sight) with no fire mode, so the mg3 played ONE curve for
# two automatic modes 1.5x apart in cyclic rate.
from config import fire_tag                                # noqa: E402,F401


def path_for(weapon, config=None, fire_mode=None):
    return os.path.join(
        SAMPLE_DIR,
        f'{weapon}__{config_key(config)}{fire_tag(weapon, fire_mode)}.jsonl')


def append(mag: Magazine):
    """One line per magazine, append-only."""
    p = path_for(mag.weapon, mag.config, mag.fire_mode)
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


def analysis_k(mag):
    """The K to ANALYSE this magazine at — the LIVE estimate, not the stored one.

    ⚠ `mag.K` IS A RECORD OF THE RUN, NOT A PROPERTY OF THE BURST. What the
    burst actually produced is `dy_px`, raw screen pixels; K is this
    repository's estimate of a GAME CONSTANT at the moment of collection, and
    the game constant did not change when the estimate did.

    Baking the collection-time K into the answer breaks MODEL.md's pooling
    licence, which is that every magazine under one (weapon, config) estimates
    the same y_true(t). Counted 2026-08-09, and it is not a corner case:

        red_dot   (live)   576 magazines
                  1.5474   285          <- superseded estimate
                  1.5128    31          <- superseded estimate
        vss_pso1  1.875     15          <- superseded the same day

    (The live value is spelled `(live)` rather than written out: `pixi run
    params` counts a prose copy of a current config constant as a second author
    of it, and it is right to -- the superseded ones below are RECORDS of
    numbers that no longer exist anywhere and so are exempt by the same rule.)

    316 of 907 red_dot magazines — 35% — were being pooled with the other 576
    as estimates of the same quantity while being scaled by a different
    constant. On a weak arm y_obs IS the whole answer, so that difference goes
    straight into y_true and nothing downstream can see it.

    config.py's red_dot block already argued this and stopped one step short:
    "K is a property of the game that did not change — only the estimate did —
    so re-analysing the store at [the new value] is legitimate, and it is not
    done silently here." Right about legitimate; the silence was being bought
    by never doing it at all. It is done here and announced by load().

    ⚠ FALLS BACK TO THE STORED K when the sight has no live profile. That
    covers `iron`, any optic measured before its profile exists, and — on
    purpose — the synthetic magazines in fit_time_curve's gate, whose whole
    point is that the fit recovers `total` at WHATEVER K generated them.
    """
    from config import RECOIL_SIGHT_PROFILES
    live = (RECOIL_SIGHT_PROFILES.get(mag.sight) or {}).get('K')
    return live or mag.K


def load(weapon, config=None, path=None, fire_mode=None, sight=None):
    """Every magazine ever stored for this weapon+config. [] when none.

    ⚠ `sight` SELECTS, IT DOES NOT OVERRIDE. One file holds every optic the
    cell was ever fired through, because path_for keys on (weapon, config,
    fire_mode) and NOT on the sight -- while the curve store keys on
    (weapon, config, posture, SIGHT), which is a 22% difference between a red
    dot and a 4x. So the two disagree about what one cell is, and the file is
    the looser of the two.

    Measured 2026-08-09: `mp5k__bare.jsonl` held 151 red_dot magazines and 8
    scoped ones after a single scope run, and fit_time_curve then refused to
    fit ANYTHING from it -- correctly, since the pool was not one cell, but
    that also meant the red dot's own curve could no longer be rebuilt. The
    refusal was right and it had no exit; this is the exit.

    Passing a sight that no magazine carries returns [], which is the honest
    answer and not an error: it is what "nothing has been fired through this
    optic yet" looks like.
    """
    p = path or path_for(weapon, config, fire_mode)
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
        m = Magazine(**{k: v for k, v in d.items() if k in known})
        why = _unusable(m)
        if why:
            _SKIPPED_SAID.setdefault(p, set())
            if why not in _SKIPPED_SAID[p]:
                _SKIPPED_SAID[p].add(why)
                print(f'[samples] {os.path.basename(p)}: skipping magazine(s) '
                      f'— {why}. NOT deleted; they are on disk and unpoolable.',
                      flush=True)
            continue
        if sight is not None and m.sight != sight:
            continue
        out.append(m)
    # ⚠ RESTATED, NOT REWRITTEN, AND NEVER SILENTLY. dy_px on disk is
    # untouched; only the counts derived from it move. One line per (file,
    # stored K) because a pool is loaded on every fit.
    restated = {}
    for m in out:
        k_live = analysis_k(m)
        if m.K and abs(k_live - m.K) > 1e-9:
            restated[(m.sight, m.K, k_live)] = restated.get(
                (m.sight, m.K, k_live), 0) + 1
    for (sight, k_was, k_now), n in sorted(restated.items()):
        said = _K_SAID.setdefault(p, set())
        if (sight, k_was, k_now) in said:
            continue
        said.add((sight, k_was, k_now))
        print(f'[samples] {os.path.basename(p)}: {n} magazine(s) collected at '
              f'{sight} K={k_was} are analysed at {k_now} '
              f'({100 * (k_now / k_was - 1):+.2f}%) — K estimates the game, '
              f'and the game did not change when the estimate did. dy_px on '
              f'disk is untouched.', flush=True)
    return out


# Same idea as _SKIPPED_SAID below, for the K restatement.
_K_SAID = {}

# One line per (file, reason), because a pool is loaded on every fit and the
# same skip would otherwise print on every one of them.
_SKIPPED_SAID = {}


def _unusable(m):
    """Why this magazine cannot be POOLED, or None. Never a reason to delete.

    ⚠ THE STORE NEVER DELETES, AND THAT IS NOT THE SAME AS "EVERY RECORD IS
    USABLE". The rule exists so an inconvenient MEASUREMENT cannot be quietly
    dropped. A record whose `y_comp` cannot be reconstructed is not an
    inconvenient measurement -- MODEL.md's whole licence to pool is
    y_true = y_obs + y_comp, so a magazine that cannot supply the second term
    is not an estimate of y_true at all.

    ⚠ AND IT MUST BE A SKIP, NOT A CRASH. Seven vector magazines were written
    with `curve` set to 124 -- the KNOT COUNT, because collect_into_store said
    `curve = rig.arm(w)` and Rig.arm returns an int. Every fit of those cells
    then died inside comp_counts_at with "'int' object is not iterable", four
    layers down, naming neither the file nor the magazine. One malformed record
    took out the whole cell including the six good magazines beside it.

    Reconstructing the curve is NOT on the table: the seed file that played is
    still on disk, but int16 quantisation with a carry sits between the request
    and the wire, so rebuilding it would be MODEL.md 2.3's forbidden move --
    inventing a number to fill a gap that a measurement was supposed to fill.
    """
    c = m.curve
    if c is None or (isinstance(c, list) and not c):
        return None            # an empty list IS a reading: nothing played
    if not isinstance(c, list):
        return (f'`curve` is {type(c).__name__} {c!r}, not a list of knots, so '
                f'y_comp is unknown and y_true = y_obs + y_comp cannot be '
                f'formed')
    if not all(isinstance(k, dict) and 't_ms' in k and 'dy' in k for k in c):
        return '`curve` holds entries that are not knots'
    return None


def configs_for(weapon):
    """Which configs have LIVE samples on disk. Quarantined cells excluded.

    ⚠ A QUARANTINED CELL IS A FILENAME, NOT A DELETION -- magazines are never
    deleted here, they are renamed to `<weapon>__<config>.MISLABELLED_<why>.jsonl`
    with the reason in the name. So the directory holds files this must not
    report: `bare.MISLABELLED_kitted_gun_read_as_bare` is not a config, and
    handing it back as one would load five magazines fired out of a different
    gun into whatever asked.

    `config_key` never emits a dot -- it joins sorted `slot-part` pairs with
    underscores -- so a dot in the fragment is exactly the quarantine mark.
    """
    if not os.path.isdir(SAMPLE_DIR):
        return []
    pre = f'{weapon}__'
    return sorted(f[len(pre):-6] for f in os.listdir(SAMPLE_DIR)
                  if f.startswith(pre) and f.endswith('.jsonl')
                  and '.' not in f[len(pre):-6])


def all_magazines(weapon):
    """Every LIVE magazine stored for this weapon, across every config.

    For the questions that are about the WEAPON rather than about one cell --
    "has anything ever fired a different magazine capacity here" is the first,
    and it is the one thing that makes two cells incomparable no matter how
    clean each is on its own.
    """
    out = []
    for cfg in configs_for(weapon):
        out.extend(load(weapon, None,
                        path=os.path.join(SAMPLE_DIR, f'{weapon}__{cfg}.jsonl')))
    return out
