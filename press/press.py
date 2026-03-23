import time
import threading
from pico_mouse import get_mouse


class Press(threading.Thread):
    def __init__(self, dx_s, dy_s, t_s, start_time=None):
        threading.Thread.__init__(self, daemon=True)
        self.dx_s = dx_s
        self.dy_s = dy_s
        self.t_s = t_s
        self._loop = True
        self._start_time = start_time or time.perf_counter()

    def run(self):
        mouse = get_mouse()
        start = self._start_time
        accum_x, accum_y = 0.0, 0.0
        for dx, dy, t in zip(self.dx_s, self.dy_s, self.t_s):
            if not self._loop:
                break
            target = start + t
            while time.perf_counter() < target:
                pass
            if self._loop:
                accum_x += dx
                accum_y += dy
                move_x = int(accum_x)
                move_y = int(accum_y)
                if move_x != 0 or move_y != 0:
                    mouse.move(move_x, move_y)
                    accum_x -= move_x
                    accum_y -= move_y

    def stop(self):
        self._loop = False


if __name__ == '__main__':
    import numpy as np

    t_s = np.arange(0, 1, 0.01)
    l = len(t_s)
    pr = Press([1000 / l] * l, [1000 / l] * l, t_s)

    pr.start()
    pr.join()
