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
    def update(self, gun_name):
        self.gun_name = gun_name
        im = win32_cap(yxhw=(0, 960, 900, 3840))
        self.detect_diff = detect_bullet(im)

    def determine(self):
        with open(r"calibrate_distance\distance_dict.json", "r") as f:
            self.distance_dict = json.load(f)
        with open(r"calibrate_distance\time_dict.json", "r") as f:
            self.time_dict = json.load(f)

        original_distance = np.array(self.distance_dict[self.gun_name])
        detect_diff = np.pad(self.detect_diff, (0, 50 - len(self.detect_diff)), 'constant', constant_values=0)
        original_distance = np.pad(original_distance, (0, 50 - len(original_distance)), 'constant',
                                   constant_values=original_distance[-1])

        detect_res = original_distance + detect_diff

        t = self.time_dict[self.gun_name]
        distance = (original_distance * (t - 0.2) + detect_res * 1.2) / (t + 1)
        distance[10:] = gaussian_filter1d(distance[10:], 1)
        self.distance_dict[self.gun_name] = distance.tolist()
        self.time_dict[self.gun_name] = min(t + 1, 5)

        with open(r"calibrate_distance\distance_dict.json", "w") as f:
            f.write(json.dumps(self.distance_dict))
        with open(r"calibrate_distance\time_dict.json", "w") as f:
            f.write(json.dumps(self.time_dict))

        cv2.destroyAllWindows()


if __name__ == '__main__':
    # image = cv2.imread("1.png")
    # image = image[:900, 960: 3840 - 960]
    # detect_bullet(image)

    updater = Updater()
    updater.update("akm")
    updater.determine()
