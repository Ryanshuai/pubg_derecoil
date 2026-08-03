import os
import numpy as np
import json

CURVE_DIR = os.path.join(os.path.dirname(__file__), '..', 'docs', 'recoil', 'curves')


# Weapon RPM (rounds per minute) from PUBG Wiki -- a STARTING GUESS, not a
# measurement, and wrong on a third of the roster. docs/recoil/weapon_rpm.json
# holds what the HUD ammo counter actually said and is merged over this table
# below; see MEASURED_RPM_PATH.
WEAPON_RPM = {
    'akm': 600, 'aug': 680, 'groza': 750, 'm416': 680, 'qbz': 680,
    'scar': 600, 'm762': 620, 'g36c': 680, 'm16': 750, 'mk47': 600,
    'k2': 680, 'ace32': 680, 'famas': 900,
    'tommy': 700, 'uzi': 1050, 'ump45': 650, 'vector': 1100,
    'pp19': 700, 'mp5k': 900, 'p90': 900, 'mp9': 1100, 'js9': 900,
    'dp28': 550, 'm249': 750, 'mg3': 990,
    'vss': 700, 'mk14': 600, 'mini14': 600, 'qbu': 600,
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
                                 'docs', 'recoil', 'weapon_rpm.json')


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


sp = {'98k', 'm24', 'awm', 'mosin', 'win94', 'lynx'}
dmr = {'mini14', 'mk14', 'qbu', 'sks', 'slr', 'vss', 'dragunov', 'mk12'}
ar = {'akm', 'aug', 'groza', 'm416', 'qbz', 'scar', 'm762', 'g36c', 'm16', 'mk47', 'k2', 'ace32', 'famas'}
smg = {'tommy', 'uzi', 'ump45', 'vector', 'pp19', 'mp5k', 'p90', 'mp9', 'js9'}
mg = {'m249', 'dp28', 'mg3'}
shotgun = {'s12k', 's1897', 's686'}

# Weapons that can fire in full-auto or burst (used for fire_mode logic)
can_full_guns = {
    'akm', 'aug', 'groza', 'm416', 'qbz', 'scar', 'mk14', 'tommy', 'uzi', 'vss',
    'm762', 'ump45', 'vector', 'dp28', 'm249', 'pp19', 'g36c',
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
    for fname in os.listdir(CURVE_DIR):
        if not fname.endswith('.json'):
            continue
        with open(os.path.join(CURVE_DIR, fname), 'r') as f:
            data = json.load(f)
        weapon = data['weapon']          # e.g. 'akm' or 'akm_att'
        out.setdefault(weapon, {})[data.get('stance', 'standing')] = \
            data['shots']
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

    def reload(self):
        """Re-read the curve files.

        Needed by anything that measures a residual and writes the corrected
        curve back in the same process: without it the next magazine would be
        fired with the pattern this one just replaced, and the loop would
        never close.
        """
        self.recoil_data = load_curves()

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

SCALES_PATH = os.path.join(os.path.dirname(__file__), '..', 'press', 'weapon_scales.json')

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
POSTURE_SCALES_PATH = os.path.join(os.path.dirname(__file__), '..', 'press', 'posture_scales.json')

# Default posture factors by weapon type
_POSTURE_DEFAULTS = {
    'ar':  {'crouching': 0.80, 'prone': 0.50},
    'smg': {'crouching': 0.80, 'prone': 0.50},
    'dmr': {'crouching': 0.80, 'prone': 0.50},
    'mg':  {'crouching': 0.50, 'prone': 0.30},
    'shotgun': {'crouching': 0.80, 'prone': 0.50},
}

def _load_posture_scales():
    if os.path.exists(POSTURE_SCALES_PATH):
        with open(POSTURE_SCALES_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def _save_posture_scales(scales):
    with open(POSTURE_SCALES_PATH, 'w', encoding='utf-8') as f:
        json.dump(scales, f, indent=2, ensure_ascii=False)

_posture_scales = _load_posture_scales()

def _get_posture_factor(weapon_name, weapon_type, posture):
    """Return posture factor for a weapon. standing=1.0 always."""
    if posture == 'standing':
        return 1.0
    per_weapon = _posture_scales.get(weapon_name, {})
    if posture in per_weapon:
        return per_weapon[posture]
    defaults = _POSTURE_DEFAULTS.get(weapon_type, {'crouching': 0.80, 'prone': 0.50})
    return defaults.get(posture, 1.0)


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
                self.fire_mode = 'full'
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
                self.fire_mode = state if state else 'full'
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
            cur = _get_posture_factor(self.name, self.type, self.posture)
            new_f = max(0.01, round(cur + delta, 3))
            if self.name not in _posture_scales:
                _posture_scales[self.name] = {}
            _posture_scales[self.name][self.posture] = new_f
        self.set_seq()

    def get_posture_factor(self):
        return _get_posture_factor(self.name, self.type, self.posture)

    @staticmethod
    def save_scales():
        _save_scales(_weapon_scales)
        _save_posture_scales(_posture_scales)

    def _hot_reload(self):
        """Reload curves from disk so edits take effect immediately."""
        self.bullet_calculator.reload()

    def set_seq(self):
        import config as _cfg
        if getattr(_cfg, 'DEBUG_HOT_RELOAD', False):
            self._hot_reload()

        posture_f = _get_posture_factor(self.name, self.type, self.posture)

        if self.type in ['ar', 'smg', 'mg', 'dmr', 'shotgun']:
            from detector.weapon_attachments import calibration_factor, attachment_factor
            # Reverse calibration to get naked scale, then apply current attachments
            cal_f = calibration_factor(self.name)
            att_f = attachment_factor(self.name, self.muzzle, self.grip)
            naked_scale = self.scale / cal_f
            factor = self.scope_factor * naked_scale * att_f * posture_f

            self.dx_s, self.dy_s, self.t_s = self.bullet_calculator.calculate_press_seq(
                self.name, factor, 'standing', has_att=True)
        else:
            # sp (bolt-action snipers) etc. — no recoil control
            self.dx_s, self.dy_s, self.t_s = [], [], []

    # ── the curve, indexed by bullet ──
    #
    # A curve entry carries a TIME, not a bullet number, and the two use
    # different units for the same thing. Delays are stored as whole
    # milliseconds (88 for the AUG) while the firing interval is 60/RPM
    # (88.235 ms), so entry i sits at i x 88 ms and asking which bullet that
    # is by flooring i x 88 / 88.235 = 0.9973 i gives i-1 for every i >= 1.
    #
    # Flooring cost a whole calibration round. comp[] came out shifted one
    # bullet early with the first two entries merged, so fit_curve corrected
    # bullet k using bullet k's residual against entry k+1's compensation, and
    # the rebuilt curve slid one bullet earlier on every pass. The AUG's
    # applied curve then over-compensated by -40 counts a magazine, repeatably.
    #
    # Rounding is right for any curve whose entries are one per bullet at the
    # nominal cadence: the error is 0.0027 per bullet, so it would take ~185
    # rounds to reach half a bin.

    def bullet_of(self, t):
        """Which bullet a curve entry at time `t` belongs to."""
        return int(round(t / self.bullet_interval_s))

    def curve_bullets(self):
        """How many bullets the curve covers. 0 when there is no curve."""
        return self.bullet_of(self.t_s[-1]) + 1 if len(self.t_s) else 0

    def comp_bins(self, nb=None):
        """Compensation summed per bullet -> (dy, dx), each of length nb.

        nb defaults to the curve's own length. Pass a longer one to line the
        bins up with a magazine that outlasts the curve; those extra bullets
        come back zero, which is exactly right — they fire uncompensated.
        """
        import numpy as np
        nb = int(nb or self.curve_bullets())
        dy, dx = np.zeros(nb), np.zeros(nb)
        if nb:
            for a, b, t in zip(self.dy_s, self.dx_s, self.t_s):
                i = min(nb - 1, self.bullet_of(t))
                dy[i] += a
                dx[i] += b
        return dy, dx
