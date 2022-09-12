import os

import cv2
import numpy as np
import torch

try:
    from .net import VGG
except ImportError:
    from image_detect.net import VGG

name_size_dict = {
    'gun_scope': ['gun_scope', 64, 9],
    'gun_muzzle': [64, 64, 10],
    'gun_grip': [64, 64, 7],
    'gun_butt': [64, 64, 3],
    'gun_name': [64, 64, 35],

    'fire_mode': ['fire_mode', 32, 6],
    'in_tab': [32, 32, 2],
}

pos_name_dict = {
    'gun_scope': ['x15', 'x1h', 'x1r', 'x2', 'x3', 'x4', 'x6', 'x8', ],
    'gun_muzzle': ['com_ar', 'com_sm', 'com_sr', 'fla_ar', 'fla_sm', 'fla_sr', 'sup_ar', 'sup_sm', 'sup_sr'],
    'gun_grip': ['ang', 'hal', 'las', 'lig', 'thu', 'ver'],
    'gun_butt': ["cheek", 'sto', ],

    'gun_name': ['98k', 'ace32', 'akm', 'aug', 'awm', 'dbs', 'dp28', 'g36c', 'groza', 'k2', 'lynx', 'm16', 'm24',
                 'm249', 'm416', 'm762', 'mg3', 'mini14', 'mk12', 'mk14', 'mk47', 'mosin', 'mp5k', 'o12', 'p90', 'pp19',
                 'qbu', 'qbz', 's12k', 's1897', 's686', 'scar', 'sks', 'slr', 'tommy', 'ump45', 'uzi', 'vector', 'vss',
                 'win94'],

    'fire_mode': ["burst2", "burst3", "full", "high", "single", ],
    'in_tab': ["in_tab"],
}


class Detector:
    def __init__(self, class_name):
        self.class_name = class_name
        net_cfg, im_size, out_size = name_size_dict[class_name]
        self.im_size = im_size
        self.model = VGG(net_cfg, out_size)
        try:
            self.model.load_state_dict(torch.load(class_name + ".pth.tar"))
        except:
            self.model.load_state_dict(torch.load('image_detect/' + class_name + ".pth.tar"))
        self.model.eval()

    def im2name(self, im):
        im_cv2 = im

        im = im.astype(np.float32) / 255.0
        im = cv2.resize(im, (self.im_size, self.im_size))
        im = np.transpose(im, (2, 0, 1))
        im = torch.from_numpy(im).float()
        input_batch = im.unsqueeze(0)

        with torch.no_grad():
            output = self.model(input_batch)
        idx = int(np.argmax(output[0]))
        name = self.idx2name(idx)

        save_dir = os.path.join("for_data_check", name or "background")
        os.makedirs(save_dir, exist_ok=True)
        howMany = len(os.listdir(save_dir))
        if howMany < 500:
            save_path = os.path.join(save_dir, str(howMany) + ".png")
            cv2.imwrite(save_path, im_cv2)

        return name

    def idx2name(self, idx):
        if idx == 0:
            return ""

        name = pos_name_dict[self.class_name][idx - 1]
        return name


if __name__ == '__main__':
    de = Detector('gun_scope')

    # test_dir = "test"
    # for image_name in os.listdir(test_dir):
    #     image_path = os.path.join(test_dir, image_name)
    #     im = Image.open(image_path).convert('RGB')
    #
    #     name = ""
    #
    #     print(image_name + "----->" + name)
