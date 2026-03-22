import os
import numpy as np
import json

CURVE_DIR = os.path.join(os.path.dirname(__file__), 'calibrate_distance', 'weapon_curve_kava4')


sp = {'98k', 'm24', 'awm'}
dmr = {'mini14', 'mk14', 'qbu', 'sks', 'slr', 'vss'}
ar = {'akm', 'aug', 'groza', 'm416', 'qbz', 'scar', 'm762', 'g36c', 'm16', 'mk47', 'k2', 'ace32', 'famas'}
smg = {'tommy', 'uzi', 'ump45', 'vector', 'pp19', 'mp5k', 'p90', 'mp9', 'js9'}
mg = {'m249', 'dp28', 'mg3'}
shotgun = {'s12k', 's1987', 's686'}

# Weapons that can fire in full-auto or burst (used for fire_mode logic)
can_full_guns = {
    'akm', 'aug', 'groza', 'm416', 'qbz', 'scar', 'mk14', 'tommy', 'uzi', 'vss',
    'm762', 'ump45', 'vector', 'dp28', 'm249', 'pp19', 'g36c',
    'k2', 'ace32', 'famas', 'mg3', 'mp5k', 'p90', 'mp9', 'js9',
}


# Actual magnification from PUBG Wiki FOV data (base FOV=80°)
# https://pubg.wiki.gg/wiki/
SCOPE_MAGNIFICATION = {
    1:  1.0,    # red dot / holo: 80°
    2:  2.0,    # 2x aimpoint: 40°
    3:  3.0,    # 3x backlit: 26.66°
    4:  4.21,   # 4x ACOG: 19° (not 20°)
    6:  6.0,    # 6x: 13.33°
    8:  8.0,    # 8x CQBSS: 10°
    15: 12.0,   # 15x PM II: 6.67° (actually 12x)
}


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
        self.recoil_data = {}
        for fname in os.listdir(CURVE_DIR):
            if not fname.endswith('.json'):
                continue
            with open(os.path.join(CURVE_DIR, fname), 'r') as f:
                data = json.load(f)
            weapon = data['weapon']  # e.g. 'akm' or 'akm_att'
            stance = data.get('stance', 'standing')
            if weapon not in self.recoil_data:
                self.recoil_data[weapon] = {}
            self.recoil_data[weapon][stance] = data['shots']

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
        for shot in shots:
            t += shot['delay_ms'] / 1000.0
            t_s.append(t)
            dx_s.append(shot['dx'] * self.counts_per_unit * factor)
            dy_s.append(shot['dy'] * self.counts_per_unit * factor)

        return np.array(dx_s), np.array(dy_s), np.array(t_s)


SCALES_PATH = os.path.join(os.path.dirname(__file__), 'weapon_scales.json')

def _load_scales():
    if os.path.exists(SCALES_PATH):
        with open(SCALES_PATH, 'r') as f:
            return json.load(f)
    return {}

def _save_scales(scales):
    with open(SCALES_PATH, 'w') as f:
        json.dump(scales, f, indent=2)

# Per-weapon scale overrides (loaded once, saved on change)
_weapon_scales = _load_scales()


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
        self.bullet_calculator = BulletCalculator()

    def set(self, pos, state):
        if pos == 'name':
            self.name = state
            self.scale = _weapon_scales.get(state, 1.0)
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
            # Map attachment_detector class name → nominal magnification
            _SCOPE_TO_MAG = {
                '': 1, 'upper_dotsight_01': 1, 'upper_holosight': 1,
                'upper_aimpoint2x_01': 2, 'upper_canted_sight': 1,
                'upper_3x': 3, 'upper_acog_01': 4,
                'upper_6x': 6, 'upper_cqbss': 8, 'upper_pm2_01': 15,
            }
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
        """Adjust per-weapon scale and save to file."""
        if not self.name:
            return
        self.scale = max(0.01, self.scale + delta)
        _weapon_scales[self.name] = round(self.scale, 3)
        _save_scales(_weapon_scales)
        self.set_seq()

    def set_seq(self):
        # Map posture to Kava4 stance (prone uses crouching data)
        stance = 'crouching' if self.posture in ('crouching', 'prone') else 'standing'

        # Has any recoil-affecting attachment → use _att curve from Kava4
        has_att = bool(self.muzzle or self.grip or self.butt)

        if self.type in ['ar', 'smg', 'mg', 'dmr', 'shotgun']:
            self.dx_s, self.dy_s, self.t_s = self.bullet_calculator.calculate_press_seq(
                self.name, self.scope_factor * self.scale, stance, has_att)
        else:
            # sp (bolt-action snipers) etc. — no recoil control
            self.dx_s, self.dy_s, self.t_s = [], [], []
