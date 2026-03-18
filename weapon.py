import os
import numpy as np
import json
from scipy.interpolate import interp1d

CURVE_DIR = os.path.join(os.path.dirname(__file__), 'calibrate_distance', 'weapon_curve')

# Map code weapon name → recoil JSON filename (without C_Recoil_ prefix and .json suffix)
RECOIL_FILE_MAP = {
    'akm': 'AKM', 'aug': 'AUG', 'ace32': 'ACE32', 'm416': 'HK416',
    'm762': 'BerylM762', 'groza': 'Groza', 'scar': 'SCAR', 'm16': 'M16A4',
    'g36c': 'G36C', 'qbz': 'QBZ', 'mk47': 'Mutant', 'k2': 'K2',
    'dp28': 'DP28', 'm249': 'M249', 'mg3': 'MG3',
    'mk14': 'M14', 'mini14': 'Mini14', 'sks': 'SKS', 'qbu': 'QBU', 'vss': 'VSS',
    'mk12': 'Mk12',
    'tommy': 'SMG_Thompson', 'uzi': 'SMG_Uzi', 'ump45': 'SMG_UMP',
    'vector': 'SMG_Vector', 'pp19': 'SMG_Bizon', 'mp5k': 'SMG_MP5K', 'p90': 'SMG_P90',
    's12k': 'Saiga', 's686': '686', 'win94': 'W94',
    '98k': 'K98',
}

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


def _parse_recoil_curve(filepath):
    """Parse a C_Recoil_*.json file, return (vertical_curve, horizontal_curve).
    Each curve is a list of (time, value) tuples sorted by time.
    """
    with open(filepath, 'r') as f:
        data = json.load(f)
    root = list(data.values())[0]

    curves = []
    for key in ['__FloatCurves_1', '__FloatCurves_2']:
        if key not in root:
            curves.append([(0, 0)])
            continue
        points = []
        for kf in root[key]['Keys']:
            t = kf['value']['Time']
            v = kf['value']['Value']
            points.append((t, v))
        points.sort()
        curves.append(points)

    return curves[0], curves[1]  # vertical, horizontal


class BulletCalculator:
    def __init__(self):
        from config import COUNTS_PER_RECOIL_UNIT
        self.counts_per_unit = COUNTS_PER_RECOIL_UNIT

        # Load official recoil curves
        self.recoil_curves = {}
        for gun_name, file_key in RECOIL_FILE_MAP.items():
            filepath = os.path.join(CURVE_DIR, f'C_Recoil_{file_key}.json')
            if not os.path.exists(filepath):
                continue
            vert, horiz = _parse_recoil_curve(filepath)
            vt, vv = zip(*vert)
            ht, hv = zip(*horiz)
            self.recoil_curves[gun_name] = {
                'vert': interp1d(vt, vv, kind='linear', fill_value=vv[-1], bounds_error=False),
                'horiz': interp1d(ht, hv, kind='linear', fill_value=hv[-1], bounds_error=False),
            }

    def calculate_press_seq(self, gun_name, factor):
        if gun_name not in self.recoil_curves:
            return [0], [0], [0.1]

        curves = self.recoil_curves[gun_name]
        dt = time_periods.get(gun_name, 0.1)

        # 55 bullets or fill to max curve time
        num_bullets = max(55, int(3.5 / dt))
        t_s = dt * np.arange(num_bullets)

        # Sample curve at each bullet time → recoil value per bullet
        y_s = curves['vert'](t_s) * self.counts_per_unit * factor
        x_s = np.zeros_like(y_s)  # horizontal disabled for now

        y_s = np.cumsum(y_s)
        x_s = np.cumsum(x_s)

        y_fun = interp1d(t_s, y_s, kind=2)
        x_fun = interp1d(t_s, x_s, kind=2)

        t_out = np.linspace(0, t_s[-1], num=int(t_s[-1] / 0.01))
        y_out = np.diff(y_fun(t_out))
        x_out = np.diff(x_fun(t_out))
        return x_out, y_out, t_out


class Weapon():
    def __init__(self, is_calibrating=False):
        self.fire_mode = 'full'
        self.name = ''
        self.scope = '1'
        self.muzzle = ''
        self.grip = ''
        self.butt = ''

        self.type = 'ar'

        self.all_factor = 1
        self.scope_factor = 1
        self.muzzle_factor = 1
        self.grip_factor = 1
        self.butt_factor = 1

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
            self.muzzle_factor = 1.0
            # flash hider
            if 'flash' in state or state.startswith('fla'):
                self.muzzle_factor = 0.9
            # compensator
            elif 'compensator' in state or state.startswith('com'):
                if self.type == 'ar':
                    self.muzzle_factor = 0.85
                elif self.type == 'smg':
                    self.muzzle_factor = 0.75
                elif self.type in ['dmr', 'sp']:
                    self.muzzle_factor = 0.8
            # suppressor: no recoil change
        elif pos == 'grip':
            self.grip = state
            grip_factors = {
                '': 1.0,
                'thumb_grip': 0.85, 'thu': 0.85,
                'light_grip': 1.1, 'lig': 1.1,
                'half_grip': 0.8, 'hal': 0.8,
                'lower_angledforegrip': 1.0, 'ang': 1.0,
                'lower_foregrip': 0.8, 'ver': 0.8,
                'lower_laserpointer': 1.0, 'las': 1.0,
            }
            self.grip_factor = grip_factors.get(state, 1.0)
        elif pos == 'butt':
            self.butt = state
            butt_factors = {
                '': 1.0,
                'stock_ar_composite': 0.85, 'sto': 0.85,
                'stock_sniperrifle_cheekpad': 0.85, 'cheek': 0.85,
                'stock_sniperrifle_bulletloops': 1.0,
                'stock_shotgun_bulletloops': 1.0,
                'stock_uzi': 0.85, 'uzi': 0.85,
                'lower_sniper_cheekpad_vss': 0.85,
                'heavy': 0.85,
            }
            self.butt_factor = butt_factors.get(state, 1.0)

    def set_seq(self):
        if self.type in ['ar', 'smg', 'mg']:
            self.all_factor = self.scope_factor * self.muzzle_factor * self.grip_factor * self.butt_factor
            self.dx_s, self.dy_s, self.t_s = self.bullet_calculator.calculate_press_seq(self.name, self.all_factor)

        elif self.type in ['dmr', 'shotgun']:
            self.all_factor = self.scope_factor * self.muzzle_factor * self.grip_factor
            self.dx_s, self.dy_s, self.t_s = self.bullet_calculator.calculate_press_seq(self.name, self.all_factor)

# if __name__ == '__main__':
#     states = All_States()
#     states.weapon[0].set('name', 'm416')
#     states.weapon[0].set_seq()
