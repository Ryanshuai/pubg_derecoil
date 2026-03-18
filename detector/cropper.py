import numpy as np
import win32con
import win32gui
import win32ui


def win32_cap(yxhw):
    y, x, h, w = yxhw
    hwnd = 0
    hwndDC = win32gui.GetWindowDC(hwnd)
    mfcDC = win32ui.CreateDCFromHandle(hwndDC)
    saveDC = mfcDC.CreateCompatibleDC()
    saveBitMap = win32ui.CreateBitmap()
    saveBitMap.CreateCompatibleBitmap(mfcDC, w, h)
    saveDC.SelectObject(saveBitMap)
    saveDC.BitBlt((0, 0), (w, h), mfcDC, (x, y), win32con.SRCCOPY)

    bmpstr = saveBitMap.GetBitmapBits(True)
    im = np.frombuffer(bmpstr, dtype=np.uint8).reshape(h, w, 4)
    im = im[:, :, :3].copy()  # BGRA -> BGR

    win32gui.DeleteObject(saveBitMap.GetHandle())
    saveDC.DeleteDC()
    mfcDC.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwndDC)
    return im
