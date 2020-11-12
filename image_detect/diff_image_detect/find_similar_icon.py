import numpy as np
import cv2
import os

from screen_parameter import max_icon_diff
from image_detect.pytorch_image_detect.predict import image2name


class DiffDetector:
    def __init__(self, png_dir):  # white or icon
        self.png_dir = png_dir
        os.makedirs(png_dir, exist_ok=True)
        self.png_dict = dict()
        for png_name in os.listdir(png_dir):
            png = cv2.imread(os.path.join(png_dir, png_name), cv2.IMREAD_UNCHANGED)
            self.png_dict[png_name[:-4]] = png

    def detect(self, crop_im, avr_thr=max_icon_diff):
        for item_name, png in self.png_dict.items():
            avr = detect_3d_diff_average(crop_im, png)
            # print('test', item_name, avr)
            if avr < avr_thr:
                # print(item_name, avr)
                return item_name
        im_name = image2name(crop_im)
        self.save_image(im_name, crop_im)
        return im_name

    def save_image(self, im_name, crop_im):
        original_im_path = os.path.join(self.png_dir + "_original", im_name + ".png")
        os.makedirs(self.png_dir + "_original", exist_ok=True)
        if os.path.exists(original_im_path):
            original_im = cv2.imread(original_im_path)
            if np.sum(abs(original_im - crop_im)) > 0:
                rgba = get_same_image4c(original_im, crop_im)
                rgba_path = os.path.join(self.png_dir, im_name + ".png")
                cv2.imwrite(rgba_path, rgba)
        else:
            cv2.imwrite(im_name, original_im_path)


def detect_3d_diff_average(detect_im_3c: np.ndarray, target_im_4c: np.ndarray):
    target_im = target_im_4c[:, :, 0:3]
    shield = (target_im_4c[:, :, [3]] // 255).astype(np.uint8)
    target_im = target_im * shield

    test_im = detect_im_3c * shield
    sum = np.sum(test_im - target_im)
    average = sum / np.sum(shield)
    return average


def get_same_image4c(im1_3c: np.ndarray, im2_3c: np.ndarray):
    im_diff = np.abs(im1_3c - im2_3c)
    im_diff = np.sum(im_diff, axis=-1)
    alpha = np.where(im_diff > 0, 1, 0)
    alpha = alpha[:, :, np.newaxis]
    im1_3c *= alpha
    alpha = alpha.astype(np.uint8)
    im1_3c = im1_3c.astype(np.uint8)

    rgba = np.concatenate((im1_3c, alpha), axis=-1)
    return rgba
