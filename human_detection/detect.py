import torch
import cv2
import numpy as np
import time
import win32api
import win32con

from image_detect.cropper import win32_cap


class Detector:
    def __init__(self):
        screen_w = 3840
        screen_h = 2160
        detect_window_w = 800
        detect_window_h = 400

        detect_start_x = (screen_w - detect_window_w) // 2
        detect_start_y = (screen_h - detect_window_h) // 2
        self.detect_rect = (detect_start_y, detect_start_x, 500, detect_window_w)

        self.person_xcyc = np.array([0, 0])
        self.model = torch.hub.load('ultralytics/yolov5', 'yolov5s', device='cpu')  # or yolov3-spp, yolov3-tiny, custom

    def detect(self, image):  # or file, Path, PIL, OpenCV, numpy, list
        output = self.model(image)
        # print(output.pandas().xywhn[0])
        xcycwhn = output.pandas().xywhn[0].to_numpy()
        person_xcycwhn = xcycwhn[xcycwhn[:, 5] == 0]
        if not len(person_xcycwhn):
            self.person_xcyc = np.array([0, 0])
            return self.person_xcyc
        person_xcycwhn = sorted(person_xcycwhn, key=lambda x: x[4], reverse=True)
        person_xcyc = person_xcycwhn[0][:2]
        self.person_xcyc = person_xcyc * self.im_wh - self.im_wh / 2
        return self.person_xcyc

    def run(self):
        while True:
            im = win32_cap(yxhw=self.detect_rect)
            print(im.shape)
            self.im_wh = np.array([im.shape[1], im.shape[0]])
            tic = time.time()
            self.detect(im)
            print(time.time() - tic, self.person_xcyc)
            self.person_xcyc = self.person_xcyc.astype(np.int32)
            win32api.mouse_event(win32con.MOUSEEVENTF_MOVE, int(self.person_xcyc[0] // 2),
                                 int(self.person_xcyc[1] // 2))
            cv2.circle(im, self.person_xcyc + self.im_wh // 2, 5, (0, 0, 255), -1)
            cv2.imshow('im', im)
            cv2.waitKey(1)


if __name__ == '__main__':
    detector = Detector()
    detector.run()
