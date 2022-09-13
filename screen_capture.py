import random
import cv2
import win32con
import win32gui
import win32ui
import os
from os.path import join
from pynput import keyboard
from screeninfo import get_monitors

main_monitor = [monitor for monitor in get_monitors() if monitor.is_primary][0]


def win32_cap(filename=None, yxhw=None):
    if filename == None or filename[-4:] != '.png':
        i = random.randrange(1, 1000)
        file_dir = filename if filename else 'temp_image'
        os.makedirs(file_dir, exist_ok=True)
        filename = join(file_dir, str(i) + '.png')
    else:
        file_dir = os.path.dirname(filename)
        os.makedirs(file_dir, exist_ok=True)

    y, x, h, w = yxhw or (0, 0, main_monitor.height, main_monitor.width)

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


class Key:
    def __init__(self, i=0):
        self.i = i
        self.listener = keyboard.Listener(on_press=self.on_press)

    def on_press(self, key):
        print(key)
        if key == keyboard.Key.ctrl_l:
            if not os.path.exists('ctrl_cap'):
                os.makedirs('ctrl_cap', exist_ok=True)
            win32_cap('ctrl_cap/' + str(self.i) + ".png")
            self.i += 1


if __name__ == '__main__':
    kl = Key(1)
    kl.listener.run()
