import numpy as np
import json
from scipy.interpolate import interp1d

# from calibrate_distance.gun_distance_constant import dist_lists
from state.time_periods_constant import time_periods

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
ar = {'akm', 'aug', 'groza', 'm416', 'qbz', 'scar', 'm762', 'g36c', 'm16', 'mk47', }
smg = {'tommy', 'uzi', 'ump45', 'vector', 'pp19', 'mp5k'}
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


with open(r"calibrate_distance\distance_dict.json", "r") as f:
    dist_lists = json.load(f)


def calculate_press_seq(name, factor, is_calibrating=False):
    if is_calibrating:
        print("loading")
        with open(r"calibrate_distance\distance_dict.json", "r") as f:
            dist_lists = json.load(f)
    if name not in dist_lists:
        return [0], [0], [0.1]

    y_s = np.array(dist_lists.get(name, [0])) * factor
    x_s = np.ones_like(y_s) * is_calibrating * factor * 30

    t_s = time_periods.get(name, 0.1) * np.ones_like(y_s)
    t_s[0] = 0
    t_s = np.cumsum(t_s)
    x_s = np.cumsum(x_s)
    y_s = np.cumsum(y_s)

    print(len(x_s), len(y_s), len(t_s))
    y_fun = interp1d(t_s, y_s, kind=2)
    x_fun = interp1d(t_s, x_s, kind=2)

    t_s = np.linspace(0, t_s[-1], num=int(t_s[-1] / 0.01))
    y_s = y_fun(t_s)
    y_s = np.diff(y_s)
    x_s = x_fun(t_s)
    x_s = np.diff(x_s)
    return x_s, y_s, t_s


class Ground():
    pass


class Back():
    pass


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
            self.is_press = False
            # if self.fire_mode == "full" and self.type in ['ar', 'smg', 'mg']:
            if self.type in ['ar', 'smg', 'mg']:
                self.fire_mode = "full"
                self.is_press = True
            # if self.fire_mode == "single" and self.type in ['dmr', 'shotgun']:
            if self.type in ['dmr', 'shotgun']:
                self.fire_mode = "single"
                self.is_press = True
        elif pos == 'scope':
            self.scope = state
            if state == "":
                self.scope_factor = 1
            else:
                self.scope_factor = int(self.scope.replace('r', '').replace('h', '').replace('x', ''))
                self.scope_factor = factor_scope(self.scope_factor)
            if self.name == 'vss':
                self.scope_factor = 4
        elif pos == 'muzzle':
            self.muzzle = state
            self.muzzle_factor = 1.0
            if self.muzzle.startswith('fla'):
                self.muzzle_factor = 0.9
            elif self.muzzle.startswith('com'):
                if self.type == 'ar':
                    self.muzzle_factor = 0.85
                elif self.type == 'smg':
                    self.muzzle_factor = 0.75
                elif self.type in ['dmr', "sp"]:
                    self.muzzle_factor = 0.8
        elif pos == 'grip':
            self.grip = state
            if self.grip == 'thu':
                self.grip_factor = 0.85
            elif self.grip == 'lig':
                self.grip_factor = 1.1
            elif self.grip == 'hal':
                self.grip_factor = 0.8
            elif self.grip == 'ang':
                self.grip_factor = 1.0
            elif self.grip == 'ver':
                self.grip_factor = 0.8
            elif self.grip == '':
                self.grip_factor = 1.0
        elif pos == 'butt':
            self.butt = state
            if self.butt == 'sto':
                self.butt_factor = 0.85

    def set_seq(self):
        if self.type in ['ar', 'smg', 'mg']:
            self.all_factor = self.scope_factor * self.muzzle_factor * self.grip_factor * self.butt_factor
            factor = factor_scope(self.all_factor)
            self.dx_s, self.dy_s, self.t_s = calculate_press_seq(self.name, factor, is_calibrating=self.is_calibrating)
        elif self.type in ['dmr', 'shotgun']:
            self.all_factor = self.scope_factor * self.muzzle_factor * self.grip_factor
            factor = factor_scope(self.all_factor)
            self.dist_seq = [i * factor for i in dist_lists.get(self.name, [0])]
            self.time_seq = [x * 0.01 for x in range(len(self.dist_seq))]


class All_States():
    def __init__(self, is_calibrating=False):
        self.to_press = True
        self.screen_state = '3p'  # 1p, 3p, tab, map s1, s2, ... s15

        self.weapon_n = 0
        self.weapon = [Weapon(is_calibrating), Weapon(is_calibrating)]

        self.hm = None
        self.bp = None
        self.vt = None

    def set_weapon_n(self, weapon_n):
        self.weapon_n = weapon_n


if __name__ == '__main__':
    states = All_States()
    states.weapon[0].set('name', 'm416')
    states.weapon[0].set_seq()
