"""一个后台循环的起停 —— 这个仓库里有三个，它们曾经是三份逐字相同的代码。

`Dispatcher`（实时按键→状态→硬件）、`ScreenCapture`（抓帧环）、`KeyPoller`（键盘
轮询）各自写了一份 `start` / `stop` / `join`，连 `daemon=True` 都一样。三份一样的
生命周期意味着三个可以各自漂移的地方，而它们漂开的那一天不会有任何东西报错：一个
非守护线程会让整个进程在退出时挂住，症状是「robot 按了 Ctrl-C 不退」，而没人会想到
去比对三个 start()。

**`daemon=True` 是这里唯一的决定，其余都是样板。** 它的意思是：这些循环没有一个
拥有需要落盘的状态，进程要走就让它们跟着走，别拦着。抓帧环和按键轮询显然如此；
`Dispatcher` 也如此——它写的是硬件指令，而硬件指令没有「写到一半」这种状态。

⚠ **但上面那句「症状是『robot 按了 Ctrl-C 不退』」，`daemon=True` 只挡住了其中一
半，另一半是 `join()` 自己，而它 2026-08-09 才被查出来。** 两个不同的死法共用同一
句症状：非守护线程拦的是**进程退出**，而无超时的 `join()` 拦的是**异常抛出**——
主线程压根没走到退出那一步。

Windows 上 `thread_nt.h` 的 `PyThread_acquire_lock_timed` **忽略 `intr_flag`**，无
超时的 join 落进 `EnterNonRecursiveMutex` 的无限等待，控制台 Ctrl-C 处理器记下了
SIGINT 但没人去查——异常要等 join **返回之后**才抛。实测（`python 3.12.13`，两条
臂只差有没有超时，`_thread.interrupt_main` 在 0.5 s 发信号）：

    join()            KeyboardInterrupt at 3.01s   ← 3.0 s 是我让线程自己退出的时刻
    join(0.2) loop    KeyboardInterrupt at 0.62s

**而 `robot.py` 等的那个线程只有 f13 能让它退出**，所以「等 join 返回」这件事在真实
运行里永远不发生：Ctrl-C 一次都送不到。`wait()` 因此存在——**超时不是为了少等，是
为了周期性地回到解释器**，那里才是挂起信号变成异常的地方。

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

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout=None):
        """等这个循环退出。**返回不代表它退出了**——超时了也返回。

        所以给了 `timeout` 的调用方必须自己问 `is_alive()`。没给的那些是在赌
        循环一定会停，而这个赌注只有在已经 `stop()` 过之后才成立。
        """
        if self._thread:
            self._thread.join(timeout)

    def wait(self, poll_s=0.2):
        """等到循环自己停下来，**而且等的过程里 Ctrl-C 有效**。

        ⚠ 不是 `join()`。理由整段在模块文档字符串里，一句话是：Windows 上无超时
        的 join 会把挂起的 SIGINT 一直压到 join 返回为止，而这个循环只有 f13 能
        让它返回。**超时是唯一让主线程周期性回到解释器的东西**，也就是唯一让那个
        信号变成 KeyboardInterrupt 的东西。

        `poll_s` 是退出的粒度，不是等待的总长——它只在循环真的停了之后才决定还
        要多睡多久，所以往小了调没有意义。
        """
        while self.is_alive():
            self._thread.join(poll_s)
