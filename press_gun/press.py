import threading
import win32api
import win32con
from state.time_periods_constant import time_periods


class Press(threading.Thread):
    def __init__(self, dist_seq, time_seq, is_calibrate=False):
        threading.Thread.__init__(self)
        self.is_calibrate = is_calibrate
        self.len = min(len(dist_seq), len(time_seq))
        self.dist_seq, self.time_seq = dist_seq[:self.len], time_seq[:self.len]
        self._loop = True
        self.i = 0
        self.x_move = 1500 // (len(dist_seq) + 0.00001)
        # self.mouse = Controller()

    def run(self):
        if self.is_calibrate:
            for time, dist in zip(self.time_seq, self.dist_seq):
                threading.Timer(time, self.mouse_down, args=[self.x_move, dist]).start()
        else:
            for time, dist in zip(self.time_seq, self.dist_seq):
                threading.Timer(time, self.mouse_down, args=[0, dist]).start()

    def mouse_down(self, x, y):
        if self._loop:
            win32api.mouse_event(win32con.MOUSEEVENTF_MOVE, int(x), int(y))

    def stop(self):
        self._loop = False


if __name__ == '__main__':
    pr = Press([10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, ],
               [1, 2, 3, 4, 5, 6, 7, 8, 9])

    pr.run()
