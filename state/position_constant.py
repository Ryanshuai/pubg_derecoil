# [y1, x1, y2, x2]

def get_pos(name):
    yxhw = crop_position[name]
    return yxhw


pos_names = ['gun1_name', 'gun1_scope', 'gun1_muzzle', 'gun1_grip', 'gun1_butt',
             'gun2_name', 'gun2_scope', 'gun2_muzzle', 'gun2_grip', 'gun2_butt']

crop_position = {
    'fire_mode': [1330, 1648, 1364, 1676],
    'in_tab': [198, 747, 33, 68],
    'posture': [1005, 705, 1048, 743],
    # 'in_scope': [1669, 1179, 1766, 1208],

    'gun1_name': [190, 2740, 60, 110],
    'gun1_scope': [229, 3204, 100, 100],
    'gun1_muzzle': [494, 2657, 100, 100],
    'gun1_grip': [494, 2863, 100, 100],
    # 'gun1_magazine': [330, 2474, 100, 100],
    'gun1_butt': [494, 3508, 100, 100],

    'gun2_name': [652, 2740, 60, 110],
    'gun2_scope': [690, 3203, 100, 100],
    'gun2_muzzle': [952, 2658, 100, 100],
    'gun2_grip': [952, 2864, 100, 100],
    # 'gun2_magazine': [636, 2474, 100, 100],
    'gun2_butt': [955, 3508, 100, 100],
}

screen_position_states = \
    {'fire_mode': ['single', 'burst2', 'burst3', 'full'],
     'in_tab': ['in'],
     'in_scope': ['in'],
     'weapon1name': ['98k', 'm24', 'awm', 'mini14', 'mk14', 'qbu', 'sks', 'slr', 'vss', 'akm', 'aug', 'groza', 'm416',
                     'qbz', 'scar', 'm762', 'g36c', 'm16', 'mk47', 'tommy', 'uzi', 'ump45', 'vector', 'pp19', 'm249',
                     'dp28', 's12k', 's1987', 's686', 'win94', ],
     'weapon2name': ['98k', 'm24', 'awm', 'mini14', 'mk14', 'qbu', 'sks', 'slr', 'vss', 'akm', 'aug', 'groza', 'm416',
                     'qbz', 'scar', 'm762', 'g36c', 'm16', 'mk47', 'tommy', 'uzi', 'ump45', 'vector', 'pp19', 'm249',
                     'dp28', 's12k', 's1987', 's686', 'win94', ],
     'weapon1scope': ['1r', '1h', '2', '3', '4', '6', '8', '15'],
     'weapon2scope': ['1r', '1h', '2', '3', '4', '6', '8', '15'],
     'weapon1muzzle': ['ar_flash', 'ar_suppressor', 'ar_compensator', 'smg_flash', 'smg_suppressor', 'smg_compensator',
                       'sp_flash', 'sp_suppressor', 'sp_compensator'],
     'weapon2muzzle': ['ar_flash', 'ar_suppressor', 'ar_compensator', 'smg_flash', 'smg_suppressor', 'smg_compensator',
                       'sp_flash', 'sp_suppressor', 'sp_compensator'],
     'weapon1grip': ['thumb', 'lightweight', 'half', 'angled', 'vertical'],
     'weapon2grip': ['thumb', 'lightweight', 'half', 'angled', 'vertical'],
     'weapon1butt': ['m416_butt'],
     'weapon2butt': ['m416_butt'],
     'weapon1magazine': [],
     'weapon2magazine': [],
     'helmet': [],
     'armor': [],
     'backpack': [],
     'ground0': [],
     'ground1': [],
     'ground2': [],
     'ground3': [],
     'ground4': [],
     'ground5': [],
     'ground6': [],
     'ground7': [],
     'ground8': [],
     'ground9': [],
     'ground10': [],
     'ground11': [],
     'ground12': []
     }
