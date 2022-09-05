import cv2
import numpy as np
import json
from scipy.ndimage import gaussian_filter1d

from screen_capture import win32_cap


def detect_bullet(img_uint8):
    img = img_uint8.astype(np.float32) / 255
    img = np.max(img, axis=2)
    img = np.where(img < 1 / 255, 1, 0).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    img = cv2.dilate(img, kernel)

    img = img * 255
    img = img.astype(np.uint8)
    num_labels, labels, stats, centers = cv2.connectedComponentsWithStats(img, 4, cv2.CV_32S)
    centers = centers[1:]
    centers = centers[np.argsort(centers[:, 0])]

    for idx, (cx, cy) in enumerate(centers):
        cv2.circle(img_uint8, (int(cx), int(cy)), 1, (0, 0, 255), 2)
        cv2.putText(img_uint8, f"{idx}", (int(cx), int(cy + 30)), cv2.FONT_HERSHEY_PLAIN, 1.0, (0, 0, 255),
                    2)
    cv2.imshow("img", img_uint8)
    cv2.waitKey(1)

    center_y_s = centers[:, 1]
    centers_y_diff = -np.diff(center_y_s)
    centers_y_diff = np.insert(centers_y_diff, 0, 0)
    print(centers_y_diff)
    centers_y_diff /= 2
    return centers_y_diff


class Updater:
    def __init__(self):
        with open(r"calibrate_distance\distance_dict.json", "r") as f:
            self.distance_dict = json.load(f)
        with open(r"calibrate_distance\time_dict.json", "r") as f:
            self.time_dict = json.load(f)

        # with open(r"distance_dict.json", "r") as f:
        # self.distance_dict = json.load(f)
        # with open(r"time_dict.json", "r") as f:
        #     self.time_dict = json.load(f)

    def update(self, gun_name):
        self.gun_name = gun_name
        im = win32_cap(yxhw=(300, 960, 600, 1920))
        self.detect_diff = detect_bullet(im)

    def determine(self):
        original_distance = np.array(self.distance_dict[self.gun_name])
        n = min(len(original_distance), len(self.detect_diff))
        detect_diff = self.detect_diff[:n]
        original_distance = original_distance[:n]

        t = self.time_dict[self.gun_name]
        detect_res = original_distance + detect_diff

        distance = (original_distance * t + detect_res) / (t + 1)
        distance[10:] = gaussian_filter1d(distance[10:], 1)
        for i in range(n):
            self.distance_dict[self.gun_name][i] = distance[i]
        self.time_dict[self.gun_name] = t + 1

        with open(r"calibrate_distance\distance_dict.json", "w") as f:
            f.write(json.dumps(self.distance_dict))
        with open(r"calibrate_distance\time_dict.json", "w") as f:
            f.write(json.dumps(self.time_dict))


if __name__ == '__main__':
    # image = cv2.imread("1.png")
    # image = image[:900, 960: 3840 - 960]
    # detect_bullet(image)

    updater = Updater()
    updater.update("akm")
    updater.determine()
