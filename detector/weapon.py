import os
import numpy as np
import json

import config

# ⚠ UNDER calibration/, NOT under docs/, AND THAT IS THE POINT.
#
# calibration/CLAUDE.md says every product goes to docs/ and never next to the
# source. That rule is right for MEASUREMENTS -- frames and runs that nothing
# should version. Curves are not measurements. They are the ARTIFACT the
# runtime loads: control/match.py calls upload_pattern() on every weapon,
# attachment and posture change, and with no curve the tool simply does not
# compensate. config.CURVES_DIR carries the full account of what that cost
# once.
CURVE_DIR = config.CURVES_DIR


# Weapon RPM (rounds per minute) from PUBG Wiki -- a STARTING GUESS, not a
# measurement, and wrong on a third of the roster. calibration/artifacts/recoil/weapon_rpm.json
# holds what the HUD ammo counter actually said and is merged over this table
# below; see MEASURED_RPM_PATH.
WEAPON_RPM = {
    'akm': 600, 'aug': 680, 'groza': 750, 'm416': 680, 'qbz': 680,
    'scar': 600, 'm762': 620, 'g36c': 680, 'm16': 750, 'mk47': 600,
    'k2': 680, 'ace32': 680, 'famas': 900,
    'tommy': 700, 'uzi': 1050, 'ump45': 650, 'vector': 1100,
    'mp5k': 900, 'p90': 900, 'mp9': 1100, 'js9': 900,
    'm249': 750, 'mg3': 990,
    'vss': 700, 'mk14': 600, 'mini14': 600,
    'sks': 600, 'slr': 600, 'dragunov': 600, 'mk12': 600,
    's12k': 250,
}


# Measured fire rates, fitted to the HUD ammo counter over a whole magazine by
# calibration/sweep.fit_interval. These override the wiki table above.
#
# Why this file exists at all: a wrong bullet interval is not a small error, it
# COMPOUNDS. The firmware lays each round's compensation on the nominal grid,
# so an interval 5% long puts bullet n's pulse 0.05*n bullets late -- invisible
# at bullet 2, two whole rounds out by bullet 40. It corrupts the measurement
# in the same direction, because analyse() bins on the same grid, and the error
# lands in the tail where the curve is steepest. The AUG's curve had grown a
# 164-count final bullet against a 93-count plateau: that spike was four rounds
# of accumulated phase, not recoil.
MEASURED_RPM_PATH = os.path.join(os.path.dirname(__file__), '..',
                                 'calibration', 'artifacts', 'recoil', 'weapon_rpm.json')


def load_measured_rpm(path=MEASURED_RPM_PATH):
    """{weapon: rpm} measured in game, or {} if none has been."""
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: float(v['rpm']) if isinstance(v, dict) else float(v)
            for k, v in data.items() if not k.startswith('_')}


MEASURED_RPM = load_measured_rpm()
WEAPON_RPM.update(MEASURED_RPM)


# DP-28, PP-19 Bizon, QBU, Mosin, R45 and P1911 are NOT missing from these
# sets by oversight — the June 2026 update (42.1) removed them from the game,
# and every trace of them was deleted on 2026-08-04 rather than kept behind a
# flag. See detector/attachment_catalog.py for why half-present is worse than
# absent. Do not re-add one from a wiki page without checking the spawner.
sp = {'98k', 'm24', 'awm', 'win94', 'lynx'}
dmr = {'mini14', 'mk14', 'sks', 'slr', 'vss', 'dragunov', 'mk12'}
ar = {'akm', 'aug', 'groza', 'm416', 'qbz', 'scar', 'm762', 'g36c', 'm16', 'mk47', 'k2', 'ace32', 'famas'}
smg = {'tommy', 'uzi', 'ump45', 'vector', 'mp5k', 'p90', 'mp9', 'js9'}
mg = {'m249', 'mg3'}
shotgun = {'s12k', 's1897', 's686'}

# Weapons that can fire in full-auto or burst (used for fire_mode logic)
can_full_guns = {
    'akm', 'aug', 'groza', 'm416', 'qbz', 'scar', 'mk14', 'tommy', 'uzi', 'vss',
    'm762', 'ump45', 'vector', 'm249', 'g36c',
    'k2', 'ace32', 'famas', 'mg3', 'mp5k', 'p90', 'mp9', 'js9',
}


# Actual magnification from PUBG Wiki FOV data (base FOV=80°)
# https://pubg.wiki.gg/wiki/
SCOPE_MAGNIFICATION = {
    1:  1.0,    # red dot / holo
    2:  2.0,    # 2x aimpoint
    3:  3.0,    # 3x backlit
    4:  4.0,    # 4x ACOG
    6:  6.0,    # 6x
    8:  8.0,    # 8x CQBSS
    15: 12.0,   # 15x PM II (actually 12x)
}

_SCOPE_TO_MAG = {
    '': 1,
    'Upper_DotSight_01_C': 1, 'Upper_Holosight_C': 1,
    'Upper_Aimpoint_C': 2, 'SideRail_DotSight_RMR_C': 1,
    'Upper_Scope3x_C': 3, 'Upper_ACOG_01_C': 4,
    'Upper_Scope6x_C': 6, 'Upper_CQBSS_C': 8, 'Upper_PM2_01_C': 15,
}


def load_curves():
    """{weapon: {stance: shots}} off every JSON in CURVE_DIR.

    One definition. It used to be written out twice — once in
    BulletCalculator.__init__ and once in Weapon._hot_reload — which is two
    places to forget when the format moves.
    """
    out = {}
    # ⚠ A MISSING CURVE_DIR USED TO TAKE THE WHOLE PROCESS DOWN, from inside
    # Weapon.__init__ -- so nothing that builds a Weapon could even start, and
    # the traceback pointed at os.listdir rather than at the fact that
    # calibration/artifacts/recoil/curves/ is gitignored and therefore one `rm` from gone.
    # Happened 2026-08-08. Returning {} is not a silent fallback: every caller
    # then gets an empty pattern, and arm() has always refused that.
    if not os.path.isdir(CURVE_DIR):
        print(f'[curves] {CURVE_DIR} does not exist — no compensation curve '
              f'for any weapon. docs/ is gitignored, so this directory has no '
              f'history to restore from; the imported curves come from the '
              f'upstream pattern repo named in each file\'s `source` field, '
              f'and anything fitted here is gone.', flush=True)
        return out
    for fname in os.listdir(CURVE_DIR):
        if not fname.endswith('.json'):
            continue
        # ⚠ BACKUPS ARE NOT CURVES, AND THEY CLAIM THE SAME NAME. write_curve
        # keeps a timestamped copy per EMA step -- `m416_att.0807_030635.bak
        # .json` -- and the copy's own `weapon` field still says `m416_att`.
        # This function keys on that field, not on the filename, so every
        # backup overwrites the live entry and the winner is whichever one
        # os.listdir happens to yield last.
        #
        # Measured 2026-08-07: 1080 files in the directory, 991 of them
        # backups, and `m416_att.json` came out at position 253 of the 255
        # that claim `m416_att`. It wins only because '0' sorts before 'j' --
        # a naming coincidence, not a design. One backup named differently, or
        # one filesystem that enumerates in another order, and the live
        # compensation fires a curve from hours ago while every log looks
        # normal and the only symptom is "it does not hold the gun down".
        #
        # It is also 92% of the read: skipping them takes load_curves from
        # 163 ms to ~13 ms, which is what makes reloading per burst affordable
        # at all (see Weapon._hot_reload).
        if fname.endswith('.bak.json'):
            continue
        # ⚠ encoding IS NOT OPTIONAL, and its absence here was a live trap
        # until 2026-08-09. load_final_curves twenty lines down always passed
        # encoding='utf-8'; this one took the platform default, which on
        # Windows is cp1252 -- so ONE curve file containing a non-ASCII note
        # raised UnicodeDecodeError out of BulletCalculator.__init__ and took
        # down every build_weapon call for EVERY gun, with a traceback
        # pointing at a codec rather than at a file. Found by writing a seed
        # whose provenance line had a Chinese word in it.
        with open(os.path.join(CURVE_DIR, fname), 'r', encoding='utf-8') as f:
            data = json.load(f)
        weapon = data['weapon']          # e.g. 'akm' or 'akm_att'
        out.setdefault(weapon, {})[data.get('stance', 'standing')] = \
            data['shots']
    return out


# ⚠ RE-EXPORTED, NOT REDEFINED (2026-08-09). This file used to carry its own
# byte-identical copy, with the reason written in its docstring: "expressed
# here so detector/ does not import calibration/". config is imported by both
# layers already, so the copy bought nothing and risked the failure that
# docstring described -- two authors drifting, and a lookup that just misses.
from config import config_key, fire_tag                    # noqa: E402,F401
# ⚠ THE ONE PLACE THAT KNOWS 'Muzzle_Compensator_Large_C' IS 'comp_ar'.
# set_seq needs it because the detector speaks assets and the curve store
# speaks catalogue keys; weapon_attachments imports only config and the
# catalogue, so this is not a cycle.
from detector.weapon_attachments import worn_keys          # noqa: E402


# Guns whose optic is part of the WEAPON, not of the scope slot. An empty
# readback means something different on these: not "no optic", but "the only
# optic this gun will ever have".
#
# ⚠ ONE NAME PER GUN, NEVER A SHARED 'integral'. Two guns' built-in optics are
# two different optics with two different Ks, and the day one of them is
# measured a shared name would hand that K to the other -- which is the
# borrow-across-guns error that `laser` and `comp_smg` already cost this repo
# (detector/CLAUDE.md's factor table: 0.5907 on the mp5k, 0.7197 on the vector,
# one wiki number for both).
#
# What the missing entry cost, found 2026-08-09: the p90's curve was filed
# under `integral` and the vss's under `vss_pso1`, while the lookup below
# answered `iron` for both -- so NEITHER GUN HAD EVER PLAYED ITS CURVE, and the
# only symptom was `no fitted curve ... NOT compensating`, which reads exactly
# like a cell nobody has measured yet. The m416 prints the same line for the
# same slot and there it is TRUE.
INTEGRAL_SIGHT = {'vss': 'vss_pso1', 'p90': 'p90_integral'}


def sight_tag(weapon, sight):
    """The curve FILE fragment for an optic. '' for this weapon's ORDINARY one.

    ⚠ THE CURVE KEY HAS ALWAYS INCLUDED THE SIGHT AND THE FILENAME NEVER DID,
    so every optic for one (weapon, config) wrote to `weapon__config.json` and
    the last fit won. Measured 2026-08-09 by doing it: fitting an MP5K's
    red_dot, 2x and 3x in that order left ONE file on disk holding the 3x, and
    its own log said so in passing -- "replacing a fit of 828 counts from 4
    magazines" -- while looking like three successful writes.
    Two of the three curves never existed.

    Modelled on config.fire_tag, which is the same problem solved for fire
    modes and sits ten lines from the code that builds this path. Same rule:
    the baseline is PER WEAPON, not a literal. A VSS's ordinary optic is its
    fixed PSO-1, so `vss__bare.json` keeps its name and a hypothetical second
    optic would be the tagged one -- keying on the literal 'red_dot' would have
    renamed all three integral-optic curves and orphaned them, since
    load_final_curves reads the sight out of the CONTENT and would then see two
    files claiming one key.

    `''` and None both give '' for the same reason fire_tag does: that is where
    the existing files already are.
    """
    if not sight or sight == INTEGRAL_SIGHT.get(weapon, 'red_dot'):
        return ''
    return f'__{sight}'


# One line per unmeasured configuration, not one per keypress: set_seq runs
# on every weapon, attachment and posture change.
def _sight_of(scope_asset, weapon=None):
    """Which RECOIL_SIGHT_PROFILES entry an equipped optic corresponds to.

    The curve store keys on the profile name because that is what carries K,
    and K is what a count is worth. `''` is not "no sight, so red dot" -- an
    empty scope slot means the player is looking down iron sights or hip
    firing, where a count rotates the view about a third as far.

    ⚠ `weapon` IS NOT OPTIONAL IN MEANING, only in signature: an empty readback
    is ambiguous without it (iron sights, or an integral optic?) and the two
    answers are about 3x apart. It defaults to None so a caller that genuinely
    has no gun in hand can still ask, and every caller that has one passes it.
    """
    if not scope_asset:
        # ⚠ AN EMPTY SCOPE SLOT IS NOT A RED DOT. _SCOPE_TO_MAG maps '' to
        # magnification 1, which is true and misleading: iron sights are 1x
        # but they are not the red dot's sensitivity, and this function keys
        # the CURVE, not the zoom. Returning 'red_dot' here handed the red
        # dot's 895-count curve to a gun with no optic at all.
        #
        # Nor is it iron sights on a gun that has no scope slot to be empty.
        return INTEGRAL_SIGHT.get(weapon, 'iron')
    # ⚠ AN UNREADABLE OPTIC IS NOT A RED DOT, AND THE DEFAULT USED TO SAY IT
    # WAS. `_SCOPE_TO_MAG.get(asset, 1)` turned every asset this file does not
    # know -- including AttachmentDetector's `'?'`, which means "something is
    # in the tile and the templates cannot separate it" -- into magnification
    # 1, and the table below then turned 1 into `red_dot`.
    #
    # Measured 2026-08-09 on an MP5K wearing a 4x: the tile reads `'?'` at
    # margin 1.14, and this function answered `red_dot`. A count is worth about
    # three times as much through a 4x, so the whole run would have been scaled
    # by the wrong constant with every printed number looking normal -- root
    # CLAUDE.md's second law, reached through a default argument.
    #
    # It only became reachable when MSE_EMPTY_TH was corrected (450 -> 1000):
    # before that the same tile read `''` and fell into the branch above, which
    # answers `iron` and is refused downstream. So fixing the detector's false
    # NEGATIVE opened a silent false POSITIVE one line later.
    #
    # `unknown` is deliberately not a RECOIL_SIGHT_PROFILES key: collect_timed
    # and calibrate_scope both refuse a sight with no profile, Rig has no K for
    # it, and the curve lookup misses rather than firing another optic's curve.
    # Every one of those is the honest outcome for "we cannot tell".
    mag = _SCOPE_TO_MAG.get(scope_asset)
    if mag is None:
        return 'unknown'
    return {1: 'red_dot', 2: '2x', 3: '3x', 4: '4x',
            6: '6x', 8: '8x', 15: '15x'}.get(mag, 'unknown')


_MISSING_SAID = {}
# Same shape, opposite event: MISSING says "no curve at all", SEED says "a
# curve, but somebody else's guess". Both are once-per-key, because load runs
# per burst and a per-magazine line would train the operator to skim past it.
_SEED_SAID = {}


def load_final_curves():
    """{(weapon, config_key, posture): shots} for MODEL.md's fitted curves.

    A "final" curve is one whose dy values are the mouse counts to send, full
    stop. It carries no scale, no attachment factor and no posture factor,
    because it was fitted from magazines fired on exactly that gun in exactly
    that configuration -- those factors are already IN it.

    ⚠ THAT IS WHY THEY CANNOT SHARE A LOOKUP WITH THE OLD CURVES. The old ones
    are raw patterns that set_seq multiplies by scope x naked_scale x
    attachment x posture on the way out. Running a fitted curve through the
    same path multiplies the answer by the factors a second time; on the m416
    measured 2026-08-08 that is 895 counts of truth turned into 1521 of
    compensation, which is the 71% over-compensation the fit was measured
    against in the first place.

    ⚠ AND A MISS RETURNS NOTHING RATHER THAN A NEIGHBOUR. Under plan A there is
    one curve per attachment combination and no interpolation between them, so
    an m416 with a compensator has nothing to say about an m416 without one.
    Falling back to "some other m416 curve" is exactly the error that made the
    shipped curve fire a bare-gun pattern at an attachment-laden gun.
    """
    out = {}
    if not os.path.isdir(CURVE_DIR):
        return out
    for fname in os.listdir(CURVE_DIR):
        if not fname.endswith('.json') or fname.endswith('.bak.json'):
            continue
        with open(os.path.join(CURVE_DIR, fname), encoding='utf-8') as f:
            data = json.load(f)
        if not data.get('shots'):
            continue
        # ⚠ THE SIGHT IS PART OF THE KEY, and leaving it out is a bug this
        # repository has already paid for once. PUBG scales ADS sensitivity
        # with magnification, so the counts needed to cancel the same angular
        # recoil scale with it too: K is 0.5 hip-firing, roughly 1.5 on a red
        # dot and 1.9 on a 4x -- config.RECOIL_SIGHT_PROFILES is the source and
        # the exact values are deliberately not repeated here, because this
        # line carried a stale one for a day (1.5474 after config had moved).
        # A curve fitted at the red dot, played while hip firing, is out by a
        # factor of three, and that ratio is what this comment is about.
        #
        # `build_weapon` never setting `scope` cost every magnification above
        # 1x its compensation until 2026-08-05 (aug at 4x: +265% residual,
        # presenting as "the correlator lost the view"). The first version of
        # THIS lookup, written 2026-08-08, keyed on (weapon, config, posture)
        # and reintroduced exactly that -- with the sight sitting right there
        # in the file it was reading.
        # ⚠ AND SO IS THE FIRE MODE (2026-08-09), for a smaller version of the
        # same reason. The mg3 has two automatic modes 1.5x apart in cyclic
        # rate; the sample store separates them (`__fire-full`) and this lookup
        # did not, so whichever mode the gun was in, it played the curve fitted
        # for the other. It is `fire_tag` and not the raw mode so that the
        # change is ADDITIVE: a weapon's ordinary mode tags '', an absent
        # fire_mode field tags '', and every curve already on disk keeps the
        # key it had.
        key = (data['weapon'], config_key(data.get('config')),
               data.get('posture', data.get('stance', 'standing')),
               data.get('sight', 'red_dot'),
               fire_tag(data['weapon'], data.get('fire_mode')))
        # ⚠ A SEED IS NOT A FIT AND MUST NOT LOOK LIKE ONE. tools/import_kava4
        # writes community patterns here so a gun with no measurement of its
        # own still has SOMETHING to fire -- without one the view climbs into
        # open sky, where phase correlation returns 0 confidently and the
        # magazine is lost with every gate green (docs/timing.md). But the file
        # lands under the same name a real fit does, so from the outside the
        # two are indistinguishable, which is the root CLAUDE.md's second law
        # exactly. Say it once per curve, not per magazine.
        if data.get('seed') and not _SEED_SAID.get(key):
            _SEED_SAID[key] = True
            print(f'[curves] {key[0]} {key[1]} {key[2]} {key[3]} is a SEED, '
                  f'not a measurement -- {data.get("total_counts", 0):.0f} '
                  f'counts imported from a community script to keep the view '
                  f'on screen. Fire it, fit it, and the fit replaces it.',
                  flush=True)
        out[key] = data['shots']
    return out


class BulletCalculator:
    """Load per-shot recoil data from Kava4 JSON files.

    Data format: list of {delay_ms, dx, dy} per shot.
    dx/dy are raw mouse move counts (at SensSetting=1, VerticalSensitivity=1).
    4 variants per weapon: standing, crouching, standing+att, crouching+att.
    """

    def __init__(self):
        from config import COUNTS_PER_RECOIL_UNIT
        self.counts_per_unit = COUNTS_PER_RECOIL_UNIT
        # recoil_data[gun_name][stance] = shots list
        # stance: 'standing', 'crouching'
        # gun_name may end with '_att' for attachment variant
        self.recoil_data = load_curves()
        self._final = load_final_curves()

    def reload(self):
        """Re-read the curve files.

        Needed by anything that measures a residual and writes the corrected
        curve back in the same process: without it the next magazine would be
        fired with the pattern this one just replaced, and the loop would
        never close.
        """
        self.recoil_data = load_curves()
        self._final = load_final_curves()

    def calculate_press_seq(self, gun_name, factor, stance='standing', has_att=False):
        """Return (dx_s, dy_s, t_s) arrays for press.py.

        stance: 'standing' or 'crouching' — selects the matching recoil pattern.
        has_att: True → use '{gun_name}_att' variant if available.
        """
        # Try _att variant first when has attachments
        key = f'{gun_name}_att' if has_att else gun_name
        data = self.recoil_data.get(key, {})
        shots = data.get(stance, data.get('standing', []))

        # Fallback to base if _att not found
        if not shots and has_att:
            data = self.recoil_data.get(gun_name, {})
            shots = data.get(stance, data.get('standing', []))

        if not shots:
            return [0], [0], [0.1]

        t = 0.0
        t_s = []
        dx_s = []
        dy_s = []
        for i, shot in enumerate(shots):
            if i > 0:
                t += shot['delay_ms'] / 1000.0
            t_s.append(t)
            dx_s.append(shot['dx'] * self.counts_per_unit * factor)
            dy_s.append(shot['dy'] * self.counts_per_unit * factor)

        dx_s = np.array(dx_s)
        dy_s = np.array(dy_s)
        t_s = np.array(t_s)
        return dx_s, dy_s, t_s

# config, not a '..' into press/ -- see config.WEAPON_SCALES_PATH for what
# that path was claiming and why it was wrong.
SCALES_PATH = config.WEAPON_SCALES_PATH

def _load_scales():
    if os.path.exists(SCALES_PATH):
        with open(SCALES_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def _save_scales(scales):
    existing = _load_scales()
    existing.update(scales)
    with open(SCALES_PATH, 'w', encoding='utf-8') as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

# Per-weapon scale overrides (loaded once, saved on change)
_weapon_scales = _load_scales()

# ── Per-weapon posture factors ───────────────────────────
# Structure: {"akm": {"crouching": 0.8, "prone": 0.5}, ...}
# standing is always 1.0 (not stored).
POSTURE_SCALES_PATH = config.POSTURE_SCALES_PATH

# ⚠ THE PER-TYPE DEFAULT TABLE THAT STOOD HERE IS GONE (2026-08-09), replaced
# by config.POSTURE_FACTOR -- one pair for every weapon, and config owns it.
# It was a second author of the same quantity with nothing behind it: its
# ar/smg/dmr crouching agreed with the one real measurement (0.80) and its
# prone did not (0.50 against 0.561), and no reader could tell which half was
# which. `mg` at 0.50/0.30 was a third opinion again, for a class with one
# fired weapon and no posture data at all.


def _load_posture_scales():
    if os.path.exists(POSTURE_SCALES_PATH):
        with open(POSTURE_SCALES_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        # ⚠ `_`-PREFIXED KEYS ARE NOT WEAPONS. `_retired` holds the values the
        # operator tuned before 2026-08-09 by ↑↓, and they are kept rather
        # than deleted while being kept OUT of the lookup: they were tuned to
        # scale curves that no longer exist (the whole bullet-bucket set was
        # deleted with that coordinate), so they are corrections to a baseline
        # this repository cannot produce any more. A number whose subject is
        # gone is not a weaker number, it is a number about something else.
        return {k: v for k, v in d.items() if not k.startswith('_')}
    return {}


def _save_posture_scales(scales):
    # Merge rather than overwrite: the caller holds only the live half, and
    # writing that back alone would delete `_retired` on the first ↑ press.
    on_disk = {}
    if os.path.exists(POSTURE_SCALES_PATH):
        with open(POSTURE_SCALES_PATH, 'r', encoding='utf-8') as f:
            on_disk = json.load(f)
    keep = {k: v for k, v in on_disk.items() if k.startswith('_')}
    with open(POSTURE_SCALES_PATH, 'w', encoding='utf-8') as f:
        json.dump({**keep, **scales}, f, indent=2, ensure_ascii=False)


_posture_scales = _load_posture_scales()


def _get_posture_factor(weapon_name, posture):
    """How much of the standing curve this posture needs. standing = 1.0.

    config.POSTURE_FACTOR is the value and carries the provenance; the
    per-weapon file is the operator's live ↑↓ override on top of it.
    """
    if posture == 'standing':
        return 1.0
    per_weapon = _posture_scales.get(weapon_name) or {}
    if posture in per_weapon:
        return per_weapon[posture]
    return config.POSTURE_FACTOR.get(posture, 1.0)


class Weapon():
    def __init__(self):
        self.fire_mode = ''
        self.name = ''
        self.posture = 'standing'
        self.scope = ''
        self.muzzle = ''
        self.grip = ''
        self.butt = ''

        self.type = 'ar'

        self.scope_factor = 1
        self.scale = 1.0  # per-weapon scale, adjusted by ↑↓

        self.t_s = []
        self.dx_s = []
        self.dy_s = []
        self.bullet_interval_s = 0.1  # default 600 RPM
        self.bullet_calculator = BulletCalculator()
        # Plan A's lookup: one fitted curve per exact attachment
        # combination. Read once per Weapon; _hot_reload refreshes it
        # alongside the raw curves so a refit lands without a restart.
        self._final = load_final_curves()

    def set(self, pos, state):
        if pos == 'name':
            self.name = state
            if not state:
                self.fire_mode = ''
                self.type = 'ar'
                self.scope = ''
                self.muzzle = ''
                self.grip = ''
                self.butt = ''
                self.scope_factor = 1
                self.scale = 1.0
                return
            self.scale = _weapon_scales.get(state, 1.0)
            rpm = WEAPON_RPM.get(state, 600)
            self.bullet_interval_s = 60.0 / rpm
            if state in can_full_guns:
                # ⚠ NOT THE LITERAL 'full'. Picking up a gun sets the mode
                # this weapon ORDINARILY has, and for the mg3 that is 'high'.
                # With 'full' here, a freshly held mg3 tagged `__fire-full`
                # and looked up a curve fitted for the other rate — or, once
                # the fire mode entered the key, none at all.
                self.fire_mode = config.fire_mode_for(state)
            if self.name in sp:
                self.type = 'sp'
            elif self.name in dmr:
                self.type = 'dmr'
            elif self.name in ar:
                self.type = 'ar'
            elif self.name in smg:
                self.type = 'smg'
            elif self.name in mg:
                self.type = 'mg'
            elif self.name in shotgun:
                self.type = 'shotgun'
        elif pos == 'posture':
            self.posture = state if state in ('standing', 'crouching', 'prone') else 'standing'
        elif pos == 'fire_mode':
            if self.name in can_full_guns:
                # Same as above: an unreadable HUD falls back to this weapon's
                # ordinary mode, which is what fire_tag treats as untagged, so
                # "could not read it" and "it is the usual one" reach the same
                # curve rather than reaching none.
                self.fire_mode = state or config.fire_mode_for(self.name)
            else:
                self.fire_mode = 'single'
        elif pos == 'scope':
            self.scope = state
            nominal = _SCOPE_TO_MAG.get(state, 1)
            if self.name == 'vss':
                nominal = 4
            self.scope_factor = SCOPE_MAGNIFICATION.get(nominal, nominal)
        elif pos == 'muzzle':
            self.muzzle = state
        elif pos == 'grip':
            self.grip = state
        elif pos == 'butt':
            self.butt = state

    def adjust_scale(self, delta):
        """Adjust per-weapon scale or posture factor depending on posture.

        standing: adjusts the base scale (as before).
        crouching/prone: adjusts the posture factor for current weapon+posture.
        """
        if not self.name:
            return
        if self.posture == 'standing':
            self.scale = max(0.01, round(self.scale + delta, 3))
            _weapon_scales[self.name] = self.scale
        else:
            cur = _get_posture_factor(self.name, self.posture)
            new_f = max(0.01, round(cur + delta, 3))
            if self.name not in _posture_scales:
                _posture_scales[self.name] = {}
            _posture_scales[self.name][self.posture] = new_f
        self.set_seq()

    def get_posture_factor(self):
        return _get_posture_factor(self.name, self.posture)

    @staticmethod
    def save_scales():
        _save_scales(_weapon_scales)
        _save_posture_scales(_posture_scales)

    _curves_stamp = None

    def _hot_reload(self):
        """Re-read the curves IF any of them changed. Cheap when they did not.

        ⚠ THE POINT IS THAT A MEASURED CURVE REACHES THE GAME WITHOUT A
        RESTART. A night of --apply passes writes better curves every
        magazine, and a live process that loaded them at startup keeps firing
        the ones it read hours ago -- which is indistinguishable, from the
        player's side, from the calibration not having worked. Asked for on
        2026-08-07: "每次开枪都是最新的曲线".
        A DIRECTORY STAT, NOT A RE-READ. set_seq() runs on every weapon,
        attachment and posture change, so an unconditional reload put 163 ms
        of file I/O on that path and is why this sat behind a debug flag.
        The newest mtime in the directory answers "did anything change" in one
        syscall; only when it moves is anything parsed.
        """
        try:
            stamp = max(os.path.getmtime(os.path.join(CURVE_DIR, f))
                        for f in os.listdir(CURVE_DIR)
                        if f.endswith('.json') and not f.endswith('.bak.json'))
        except (OSError, ValueError):
            stamp = None
        if stamp is not None and stamp == Weapon._curves_stamp:
            return
        Weapon._curves_stamp = stamp
        self.bullet_calculator.reload()
        # The fitted curves live in the same directory and are
        # the ones a refit actually rewrites, so reloading only
        # the raw ones would hot-reload everything except the
        # thing that changes.
        self._final = load_final_curves()

    def set_seq(self):
        import config as _cfg
        if getattr(_cfg, 'DEBUG_HOT_RELOAD', False):
            self._hot_reload()

        if self.type in ['ar', 'smg', 'mg', 'dmr', 'shotgun']:
            # PLAN A: one fitted curve per exact attachment combination, no
            # interpolation between them. When this gun's combination has been
            # measured, that curve IS the answer -- emitted with NO factors,
            # because scope, scale, attachments and posture are all already
            # baked into the counts it was fitted from.
            # ⚠ CATALOGUE KEYS, NOT ASSET NAMES, AND THIS WAS THE WHOLE GAME.
            # `self.muzzle` is what the DETECTOR read off the tile --
            # 'Muzzle_Compensator_Large_C'. Every curve on disk is named with
            # what the EXPERIMENT asked for -- 'comp_ar'. Those are two names
            # for one part, and this built the key straight out of the first:
            #
            #   looked up   m416 grip-Lower_ForeGrip_C_muzzle-Muzzle_Compen...
            #   on disk     m416 grip-vert_grip_muzzle-comp_ar_stock-tactic...
            #
            # so EVERY KITTED GUN MISSED, on every gun, since plan A shipped.
            # It could not be seen from either end: the store is full of
            # kitted cells, the runtime prints one honest "NOT compensating"
            # line per configuration, and a player putting a compensator on
            # simply stops being helped. `scope` escaped only because
            # `_sight_of` has always had a translation table.
            #
            # ⚠ AND AN UNRECOGNISED PART POISONS THE WHOLE KEY, on purpose --
            # worn_keys returns None rather than dropping it. Dropping one
            # would look up the kit MINUS that part and fire a curve fitted
            # without it, which is the 1521-against-895 failure below wearing
            # a different hat.
            cfg = worn_keys(self.muzzle, self.grip, self.butt)
            sight = _sight_of(self.scope, self.name)
            if cfg is None:
                said = ('unnamed', self.name, self.muzzle, self.grip,
                        self.butt)
                if not _MISSING_SAID.get(said):
                    _MISSING_SAID[said] = True
                    print(f'[curves] {self.name}: a fitted part has no '
                          f'catalogue name (muzzle={self.muzzle!r} '
                          f'grip={self.grip!r} stock={self.butt!r}) -- NOT '
                          f'compensating, because the kit it belongs to '
                          f'cannot be named either.', flush=True)
                self.dx_s, self.dy_s, self.t_s = [], [], []
                return
            fmode = fire_tag(self.name, self.fire_mode)
            key = (self.name, config_key(cfg), self.posture, sight, fmode)
            shots = self._final.get(key)
            scale = 1.0
            # ⚠ ONE FALLBACK, AND ONLY ALONG POSTURE. Nothing in the store is
            # anything but `standing`, so crouching and prone missed on every
            # gun and the honest output -- no compensation -- was also the
            # output for the two postures a player spends most of a fight in.
            #
            # WHAT MAKES THIS DIFFERENT FROM THE FALLBACK BELOW, and the
            # difference is the whole licence: this substitutes THE SAME CELL'S
            # OWN CURVE and multiplies it by a measured scalar. It does not
            # substitute another gun, another kit, another optic or another
            # fire mode -- every one of those changes the shape, and a shape
            # cannot be recovered by scaling. Posture is the one axis this
            # repository has an actual number for (config.POSTURE_FACTOR).
            #
            # ⚠ AND IT IS A SCALAR ON A TRAJECTORY, WHICH IS AN ASSUMPTION
            # THIS REPOSITORY HAS ALREADY SEEN FAIL. The scope ratio was
            # believed constant on the same reasoning and measures 5.7% of
            # spread between t=1.2 s and t=2.4 s (calibrate_scope.py). The
            # posture ratio comes from two burst TOTALS in a coordinate that
            # no longer exists, so nobody has been able to ask it the same
            # question. It is stated, printed, and stamped `derived` on the
            # way out so that it cannot be mistaken for a fitted curve.
            if not shots and self.posture != 'standing':
                shots = self._final.get(
                    (self.name, config_key(cfg), 'standing', sight, fmode))
                if shots:
                    scale = _get_posture_factor(self.name, self.posture)
                    said = ('derived', *key)
                    if not _MISSING_SAID.get(said):
                        _MISSING_SAID[said] = True
                        print(f'[curves] {self.name} {config_key(cfg)} '
                              f'{sight}: no {self.posture} curve, DERIVING it '
                              f'from the standing one x{scale:.2f} '
                              f'(config.POSTURE_FACTOR -- one m762 run in the '
                              f'retired coordinate, not measured here). Fire '
                              f'{self.posture} to replace it.', flush=True)
            if shots:
                t = 0.0
                self.t_s, self.dx_s, self.dy_s = [], [], []
                for i, s in enumerate(shots):
                    if i:
                        t += s['delay_ms'] / 1000.0
                    self.t_s.append(t)
                    self.dx_s.append(float(s['dx']) * scale)
                    self.dy_s.append(float(s['dy']) * scale)
                return
            # ⚠ NO FALLBACK TO ANOTHER COMBINATION. Under plan A a miss means
            # this configuration has not been measured, and the honest output
            # is no compensation rather than another gun's answer. The old
            # path's fallback is what fired a bare-gun curve at a gun wearing a
            # compensator, a foregrip and a stock -- 1521 counts against 895 of
            # real recoil, and nothing anywhere said so.
            said = (self.name, config_key(cfg), self.posture, sight)
            if not _MISSING_SAID.get(said):
                _MISSING_SAID[said] = True
                print(f'[curves] no fitted curve for {self.name} '
                      f'{config_key(cfg)} {self.posture} {sight} -- NOT '
                      f'compensating. Measure it with `pixi run collect-timed '
                      f'--weapon {self.name} --sight {sight}`.', flush=True)
            self.dx_s, self.dy_s, self.t_s = [], [], []
            return
        # ⚠ TEN LINES OF THE PRE-PLAN-A FACTOR PATH STOOD HERE, AFTER THAT
        # `return`, AND WERE UNREACHABLE (deleted 2026-08-09). They rebuilt the
        # curve as scope_factor * (scale / calibration_factor) *
        # attachment_factor * posture_factor and called calculate_press_seq.
        #
        # Deleted rather than left, because unreachable code that reads like
        # policy is worse than none: it named `attachment_factor` as the way a
        # kitted gun gets compensated, and under plan A there ARE no factors --
        # the curve is looked up by the exact configuration and emitted with
        # none applied ('scaled_by: NOTHING'). Anyone reading this file for how
        # attachments reach the firmware would have found the wrong answer in
        # live-looking code.
        #
        # It also passed CATALOGUE KEYS to attachment_factor, which wants ASSET
        # names -- the same vocabulary error that made every kitted seed print
        # `kit x1.0000` until it was found in tools/import_kava4.py the same
        # day. Had this path ever run, it would have applied a factor of 1.0 to
        # every gun and looked entirely reasonable doing it.
        else:
            # sp (bolt-action snipers) etc. — no recoil control
            self.dx_s, self.dy_s, self.t_s = [], [], []

    # ⚠ THE CURVE WAS ALSO INDEXABLE BY BULLET, AND IS NOT ANY MORE
    # (2026-08-08). bullet_of / curve_bullets / comp_bins converted a curve
    # time into a round number via 60/RPM, and comp_bins -- the only one of
    # the three that did anything with the answer -- had NO CALLERS. They were
    # the last of the bullet-bucket coordinate: what is fitted and what the
    # firmware plays are both functions of TIME, so a round number is not a
    # position in this model, it is a lossy re-derivation of one.
    #
    # What went with them is worth keeping, because the shape recurs. The
    # conversion had to ROUND, never floor: delays are whole milliseconds (88
    # for the AUG) while the interval is 60/RPM (88.235 ms), so entry i sits
    # at i x 88 ms and flooring i x 88 / 88.235 = 0.9973 i gives i-1 for every
    # i >= 1. That off-by-one cost a whole calibration round -- comp[] came
    # out shifted one bullet early with the first two entries merged, the
    # fitter corrected bullet k against entry k+1's compensation, and the
    # rebuilt curve slid one bullet earlier on every pass. The AUG's curve
    # over-compensated by -40 counts a magazine, repeatably.
    #
    # `bullet_interval_s` survives, and only for one thing: estimating how
    # long to hold the trigger (control/fire.fire_magazine_timed). That is
    # fire control, not compensation, and its own docstring says nothing
    # downstream depends on the estimate being right.

