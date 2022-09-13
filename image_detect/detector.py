import os

import cv2
import numpy as np
import torch

from image_detect.net import VGG
from image_detect.cropper import Cropper

name_size_dict = {
    'gun_scope': [64, 64],
    'gun_muzzle': [64, 64],
    'gun_grip': [64, 64],
    'gun_butt': [64, 64],
    'gun_name': [64, 128],

    'fire_mode': [32, 32],
    'in_tab': [32, 64],
}

pos_name_dict = {
    'gun_scope': ['x15', 'x1h', 'x1r', 'x2', 'x3', 'x4', 'x6', 'x8', ],
    'gun_muzzle': ['com_ar', 'com_sm', 'com_sr', 'fla_ar', 'fla_sm', 'fla_sr', 'sup_ar'],
    'gun_grip': ['ang', 'hal', 'las', 'lig', 'thu', 'ver'],
    'gun_butt': ['cheek', 'heavy', 'sto'],

    'gun_name': ['98k', 'ace32', 'akm', 'aug', 'awm', 'dbs', 'dp28', 'g36c', 'groza', 'k2', 'lynx', 'm16', 'm24',
                 'm249', 'm416', 'm762', 'mg3', 'mini14', 'mk12', 'mk14', 'mk47', 'mosin', 'mp5k', 'mp9', 'o12', 'p90',
                 'pp19', 'qbu', 'qbz', 's12k', 's1897', 's686', 'scar', 'sks', 'slr', 'tommy', 'ump45', 'uzi', 'vector',
                 'vss', 'win94'],

    'fire_mode': ["burst2", "burst3", "full", "high", "single", ],
    'in_tab': ["in_tab"],
}


class Detector:
    def __init__(self, pos_name):
        self.crop = Cropper(pos_name)

        self.cls_name = pos_name.replace("1", "").replace("2", "")
        self.im_hw = name_size_dict[self.cls_name]
        out_size = len(pos_name_dict[self.cls_name]) + 1

        self.model = VGG(self.im_hw, out_size)
        try:
            self.model.load_state_dict(torch.load(self.cls_name + ".pth.tar"))
        except:
            self.model.load_state_dict(torch.load('image_detect/' + self.cls_name + ".pth.tar"))
        self.model.eval()

    def __call__(self):
        im = self.crop()
        im_cv2 = im.copy()

        im = im.astype(np.float32) / 255.0
        im = cv2.resize(im, self.im_hw)
        im = np.transpose(im, (2, 0, 1))
        im = torch.from_numpy(im).float()
        input_batch = im.unsqueeze(0)

        with torch.no_grad():
            output = self.model(input_batch)
        idx = int(np.argmax(output[0]))

        name = pos_name_dict[self.cls_name][idx - 1] if idx > 0 else ""

        save_dir = os.path.join("for_data_check", name or "background")
        os.makedirs(save_dir, exist_ok=True)
        howMany = len(os.listdir(save_dir))
        if howMany < 500:
            save_path = os.path.join(save_dir, str(howMany) + ".png")
            cv2.imwrite(save_path, im_cv2)

        return name


if __name__ == '__main__':
    de = Detector('gun_scope')
