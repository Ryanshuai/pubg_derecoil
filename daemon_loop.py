"""一个后台循环的起停 —— 这个仓库里有三个，它们曾经是三份逐字相同的代码。

`Dispatcher`（实时按键→状态→硬件）、`ScreenCapture`（抓帧环）、`KeyPoller`（键盘
轮询）各自写了一份 `start` / `stop` / `join`，连 `daemon=True` 都一样。三份一样的
生命周期意味着三个可以各自漂移的地方，而它们漂开的那一天不会有任何东西报错：一个
非守护线程会让整个进程在退出时挂住，症状是「robot 按了 Ctrl-C 不退」，而没人会想到
去比对三个 start()。

**`daemon=True` 是这里唯一的决定，其余都是样板。** 它的意思是：这些循环没有一个
拥有需要落盘的状态，进程要走就让它们跟着走，别拦着。抓帧环和按键轮询显然如此；
`Dispatcher` 也如此——它写的是硬件指令，而硬件指令没有「写到一半」这种状态。

放在仓库根而不是任何一层里，是因为它**一层都不认识**：只 import threading，既不
知道设备也不知道游戏。分层 lint 的规则 3 管着根目录的模块不许伸进 press，这里
连那个诱惑都没有。
"""
import threading


class DaemonLoop:
    """混入 `_loop()` 的类，得到 start / stop / join。

    子类只需要提供 `_loop`，并在自己的 `__init__` 里调一次 `super().__init__()`
    或者干脆什么都不做 —— `_running` / `_thread` 都是惰性建立的，这样混入它不会
    要求已有的三个类改动自己的构造函数。

    `stop()` 只放下旗子，不 join。**两件事分开是故意的**：调用方常常要先让几个
    循环一起停下来、再逐个等，合成一个的话最后一个循环会白等前面每一个的一整轮。
    """

    _running = False
    _thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def join(self):
        if self._thread:
            self._thread.join()
