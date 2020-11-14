import os
import numpy as np

from state.all_states import dmr
from calibrate_distance.image_match import detect_dir
from calibrate_distance.write_dict import write_to_file_abs

print('start detecting y_move_list')

root_dir = 'image_match_dir'
# for gun_name in os.listdir(root_dir):
for gun_name in ['m762']:
    gun_name_dir = os.path.join(root_dir, gun_name)
    y_move_array_acc = np.zeros((50))
    for time in os.listdir(gun_name_dir):
        seq_dir = os.path.join(gun_name_dir, time)
        if gun_name in dmr:
            y_move_array_acc += detect_dir(seq_dir, type='avr')
        else:
            y_move_array_acc += detect_dir(seq_dir)
    y_move_array_acc = y_move_array_acc / len(os.listdir(gun_name_dir))
    y_move_array_acc = list(filter(lambda x: x != 0, y_move_array_acc))
    write_to_file_abs(gun_name, y_move_array_acc)
