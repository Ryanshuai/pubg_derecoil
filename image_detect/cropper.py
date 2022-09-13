import random
import cv2
import win32con
import win32gui
import win32ui
import os
from os.path import join
from screeninfo import get_monitors

crop_position = {  # y, x, h, w   #crop_position_3840_2160
    'fire_mode': [1988, 1810, 64, 49],
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


class Cropper:
    def __init__(self, pos_name):
        monitors = get_monitors()
        self.main_monitor = [monitor for monitor in monitors if monitor.is_primary][0]

        if abs(self.main_monitor.width / self.main_monitor.height - 3840 / 2160) < 1e-6:
            factor = self.main_monitor.width / 3840
            yxhw = crop_position[pos_name]
            self.yxhw = [int(x * factor) for x in yxhw]

    def __call__(self):
        return win32_cap(self.yxhw)


def win32_cap(yxhw):
    i = random.randrange(1, 1000)
    os.makedirs('temp_image', exist_ok=True)
    filename = join('temp_image', str(i) + '.png')

    y, x, h, w = yxhw
    hwnd = 0
    hwndDC = win32gui.GetWindowDC(hwnd)
    mfcDC = win32ui.CreateDCFromHandle(hwndDC)
    saveDC = mfcDC.CreateCompatibleDC()
    saveBitMap = win32ui.CreateBitmap()
    saveBitMap.CreateCompatibleBitmap(mfcDC, w, h)
    saveDC.SelectObject(saveBitMap)
    saveDC.BitBlt((0, 0), (w, h), mfcDC, (x, y), win32con.SRCCOPY)
    saveBitMap.SaveBitmapFile(saveDC, filename)
    im = cv2.imread(filename)
    return im
