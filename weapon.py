import os
import numpy as np
import json

CURVE_DIR = os.path.join(os.path.dirname(__file__), 'calibrate_distance', 'weapon_curve_kava4')

time_periods = {
    'akm': 0.1,
    'aug': 0.08571,
    "ace32": 0.088,
    'pp19': 0.086,
    'dp28': 0.109,
    'g36c': 0.086,
    'groza': 0.08,
    'm416': 0.086,
    'm16': 0.075,
    'm249': 0.075,
    'mk14': 0.09,
    'm762': 0.086,
    "mg3": 0.06,
    'qbz': 0.092,
    'scar': 0.096,
    'tommy': 0.08,
    'ump45': 0.09,
    'uzi': 0.048,
    'mp5k': 0.0666,
    "p90": 0.06,
    'vector': 0.055,
    'slr': 0.01,
    'mini14': 0.01,
    'qbu': 0.01,
    'sks': 0.01,
    's12k': 0.01,
    's686': 0.01,
}

all_guns = ['98k', 'm24', 'awm', 'mini14', 'mk14', 'qbu', 'sks', 'slr', 'vss', 'akm', 'aug', 'groza', 'm416', 'qbz',
            'scar', 'm762', 'g36c', 'm16', 'mk47', 'tommy', 'uzi', 'ump45', 'vector', 'pp19', 'm249', 'dp28', 's12k',
            's1987', 's686', 'win94', ]

single_guns = ['98k', 'awm', 'm16', 'm24', 'mini14', 's12k', 's1987', 's686', 'sks', 'slr', 'win94', ]
full_guns = ['dp28', 'm249', ]
single_burst_guns = ['m16', 'mk47', ]
single_full_guns = ['akm', 'aug', 'groza', 'm416', 'qbz', 'scar', 'mk14', 'tommy', 'uzi', 'vss', ]
single_burst_full_guns = ['m762', 'ump45', 'vector', ]
can_full_guns = ['akm', 'aug', 'groza', 'm416', 'qbz', 'scar', 'mk14', 'tommy', 'uzi', 'vss', 'm762', 'ump45', 'vector',
                 'dp28', 'm249', 'pp19', 'g36c', ]

sp = {'98k', 'm24', 'awm', }
dmr = {'mini14', 'mk14', 'qbu', 'sks', 'slr', 'vss', }
ar = {'akm', 'aug', 'groza', 'm416', 'qbz', 'scar', 'm762', 'g36c', 'm16', 'mk47', "k2", "ace32"}
smg = {'tommy', 'uzi', 'ump45', 'vector', 'pp19', 'mp5k', "p90"}
mg = {'m249', 'dp28', "mg3"}
shotgun = {'s12k', 's1987', 's686', }

bullet_762_guns = ['98k', 'm24', 'mk14', 'sks', 'slr', 'akm', 'groza', 'm762', 'mk47', 'dp28', ]
bullet_556_guns = ['mini14', 'qbu', 'aug', 'm416', 'qbz', 'scar', 'g36c', 'm16', ]
bullet_9_guns = ['vss', 'uzi', 'vector', 'pp19', ]
bullet_45_guns = ['tommy', 'ump45', 'win94', ]
bullet_12_guns = ['s12k', 's1987', 's686', ]
bullet_300_guns = ['awm', ]


def factor_scope(scope):
    factor = 1
    if scope == 1:
        factor = 1.
    if scope == 2:
        factor = 0.85
    if scope == 3:
        factor = 0.85
    if scope == 4:
        factor = 0.85
    if scope == 6:
        factor = 0.85
    screen_factor = 1  # screen_h_factor
    return scope * factor * screen_factor


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


class Weapon():
    def __init__(self, is_calibrating=False):
        self.fire_mode = 'full'
        self.name = ''
        self.posture = 'standing'
        self.scope = '1'
        self.muzzle = ''
        self.grip = ''
        self.butt = ''

        self.type = 'ar'

        self.all_factor = 1
        self.scope_factor = 1

        self.time_interval = 0.1
        self.t_s = []
        self.dx_s = []
        self.dy_s = []
        self.is_press = False
        self.is_calibrating = is_calibrating
        self.bullet_calculator = BulletCalculator()

    def __str__(self):
        return "-".join((self.name, self.fire_mode, self.scope, self.muzzle[:3], self.grip, self.butt))

    def set(self, pos, state):
        if pos == 'name':
            self.name = state
            self.time_interval = time_periods.get(self.name, 0.1)
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
            # if self.fire_mode == "full" and self.type in ['ar', 'smg', 'mg']:
            if self.type in ['ar', 'smg', 'mg']:
                self.fire_mode = state if state else 'full'
            # if self.fire_mode == "single" and self.type in ['dmr', 'shotgun']:
            if self.type in ['dmr', 'shotgun']:
                self.fire_mode = "single"
        elif pos == 'scope':
            self.scope = state
            # Map attachment_detector names to magnification
            scope_mag = {
                '': 1, 'upper_dotsight_01': 1, 'upper_holosight': 1,
                'upper_aimpoint2x_01': 2, 'upper_canted_sight': 1,
                'upper_3x': 3, 'upper_acog_01': 4,
                'upper_6x': 6, 'upper_cqbss': 8, 'upper_pm2_01': 15,
                # Old format compat
                'x1h': 1, 'x1r': 1, 'x15': 15,
                'x2': 2, 'x3': 3, 'x4': 4, 'x6': 6, 'x8': 8,
            }
            mag = scope_mag.get(state, 1)
            self.scope_factor = factor_scope(mag)
            if self.name == 'vss':
                self.scope_factor = factor_scope(4)
        elif pos == 'muzzle':
            self.muzzle = state
        elif pos == 'grip':
            self.grip = state
        elif pos == 'butt':
            self.butt = state

    def set_seq(self):
        # Map posture to Kava4 stance (prone uses crouching data)
        stance = 'crouching' if self.posture in ('crouching', 'prone') else 'standing'

        # Has any recoil-affecting attachment → use _att curve from Kava4
        has_att = bool(self.muzzle or self.grip or self.butt)

        if self.type in ['ar', 'smg', 'mg']:
            self.all_factor = self.scope_factor
            self.dx_s, self.dy_s, self.t_s = self.bullet_calculator.calculate_press_seq(
                self.name, self.all_factor, stance, has_att)

        elif self.type in ['dmr', 'shotgun']:
            self.all_factor = self.scope_factor
            self.dx_s, self.dy_s, self.t_s = self.bullet_calculator.calculate_press_seq(
                self.name, self.all_factor, stance, has_att)

# if __name__ == '__main__':
#     states = All_States()
#     states.weapon[0].set('name', 'm416')
#     states.weapon[0].set_seq()
