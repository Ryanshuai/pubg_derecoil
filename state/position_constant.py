from screeninfo import get_monitors

monitors = get_monitors()
main_monitor = [monitor for monitor in monitors if monitor.is_primary][0]

crop_position_3840_2160 = {
    'fire_mode': [1330, 1648, 34, 28],
    'in_tab': [198, 747, 33, 68],
    'posture': [1005, 705, 43, 38],
    # 'in_scope': [?, ?, ?, ?],

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

print(main_monitor.width, main_monitor.height)

if main_monitor.width == 3840 and main_monitor.height == 2160:
    crop_position = crop_position_3840_2160

if main_monitor.width == 1920 and main_monitor.height == 1080:
    crop_position = crop_position_3840_2160
    for k, v in crop_position.items():
        crop_position[k] = [int(x * 0.5) for x in v]


def get_pos(name):
    yxhw = crop_position[name]
    return yxhw


pos_names = ['gun1_name', 'gun1_scope', 'gun1_muzzle', 'gun1_grip', 'gun1_butt',
             'gun2_name', 'gun2_scope', 'gun2_muzzle', 'gun2_grip', 'gun2_butt']
