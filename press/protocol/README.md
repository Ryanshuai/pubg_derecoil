# `press/protocol/` — 一约两端

这个目录里只有**一个文件是人写的**：

| | | |
|---|---|---|
| `protocol.toml` | **真相源** | 手写。改协议只改这里 |
| `protocol.h` | 生成物 | `press/firmware/src/main.c` `#include` 它 |
| `__init__.py` | 生成物 | `press/pico_mouse.py` import 它 |

```
pixi run gen-protocol         重新生成
pixi run protocol-check       生成物过期就 exit 1
```

生成物**提交进仓库**，因为 Pico 的构建工具链里不能有 Python，而一份新 clone 必须能直接编译。代价是生成物会过期——`--check` 就是为这个存在的，`flash.py` 在写任何东西之前会跑它。

---

## `press/` 里这三样东西是什么关系

```
        press/protocol/          声明（不执行任何东西）
       ╱                ╲
press/*.py            press/firmware/
Python，跑在 PC        C，跑在 RP2350
```

**不是「一体三面」，是一约两端。** 左右两个是活的进程，跑在两个不同的处理器上；中间那个不执行任何东西，它是把两端绑住的声明。

这个区别决定了**什么能放进这个目录**。见下一节。

---

## 什么能进来

一条判据，而且不是「是不是跟 Pico 有关」：

> **固件和 PC 必须同意这个值，否则出错。**

| | | |
|---|---|---|
| `CMD_MOVE = 0x13` | 两端都要解码它 | ✅ 进 |
| `MAX_PATTERN_POINTS` | PC 超了会被**静默截断** | ✅ 进 |
| 弹道点的 6 字节布局 | 两端都要打包/解包 | ✅ 进 |
| `RECOIL_FIRE_DELAY_MS` | **固件从没听说过它**。那是 PC 相对点击排时间表用的，Pico 只管播它收到的东西 | ❌ 去 `config.py` |
| `HID_KEY_R = 0x15` | 固件把 keycode 原样转发给 HID 描述符，不解释。要同意的是 PC 和**游戏**，不是两端 | ❌ 留 `press/pico_mouse.py` |

不满足这条却硬放进来，这个目录就会变成「跟 Pico 沾边的常量」的垃圾场，下一个人分不出哪些是承重的。

## 什么不能进来，即使它满足上面那条

**任何可执行的东西。** 哪怕两端都想要。

固件是 C，它调用不了 Python 函数。真放一个编码 helper 进来，结果是 Python 用它、C 那边照样手写一份——两边从「数字不一致」升级成「**行为**不一致」，而后者连 diff 都看不出来。

`pixi run layering` 有一条规则守着这个：`press/protocol/` 只准 import `struct`，多一个就红。

> 这里的每样东西必须是**同一份字段声明的两种投影**，不是一份两端共享的实现。
>
> `MOVE_CLICK` 在 toml 里声明 5 个字段，投影成 Python 的 `'<BhhBHH'` 和 C 的 `CMD_MOVE_CLICK_LEN = 10`。两种记法，一个来源，谁都不调用谁。

---

## 为什么要有这个目录（2026-08-08 之前是什么样）

同一张表**抄在两处**，靠注释维系：

```python
# press/pico_mouse.py:203
MAX_POINTS = 300  # must match Pico firmware MAX_PATTERN_POINTS
```

**那句注释不是机制。** 没有东西读它、没有东西检查它，而它防的那个失效是静默的最坏一种——固件对超长的上传是**钳位**（`n > MAX_PATTERN_POINTS ? MAX_PATTERN_POINTS : n`）而不是拒绝。只在 PC 一侧把 300 调大，第 300 个结之后的整条尾巴被丢掉，上传「成功」，残差回来看着像是枪变了。

抄漏的部分也已经发生了：

- `CMD_RAZER_READ` / `CMD_REBOOT_BOOTSEL` **从没被抄进 Python 侧**，于是 `flash.py` 只能写裸的 `bytes([0xFF])`，名字只活在注释里
- `press/pico_mouse.py` 的模块 docstring 列了 13 条命令里的 3 条
- wire 格式在两边各写一遍：Python 是 `struct.pack('<BhhBHH', ...)`，C 是 `pos + 10 > cdc_len`

现在这些是一条 `#include` 和一条 `import`。

## 改协议的正确顺序

```
1. 改 protocol.toml
2. pixi run gen-protocol
3. 重新编译固件      cd press/firmware && cmake --build build
4. python press/firmware/flash.py
```

跳过 2 或 3 都会被 `flash.py` 拦下来，而且是两道不同的闸：

- `--check` 比生成物和 `.toml` —— 抓「改了没生成」
- `.uf2` 的 mtime 比 `protocol.h` 的 mtime —— 抓「生成了没重编译」

第二道是必要的，因为第一道对「生成物是新的、但 `.uf2` 是用旧头编译的」这种情况会报绿。
