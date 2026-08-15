"""One white-text mask, and the only thing its two callers may disagree about.

    pixi run text-mask          # offline, no game, no hardware

`detector.weapon_template_detector._white_text_mask` reads weapon name plates;
`detector.row_name_detector.text_mask` reads 库存 / 附近 row labels. They were
the same criterion written twice until 2026-08-15 -- and one of them said so in
a comment ("Same threshold pair as `_white_text_mask`"), which is a declaration
that one fact had two authors with nothing checking it.

⚠ THIS FILE IS THE THING THAT WAS MISSING, not the merge. The merge was
verified by fingerprinting both functions over fixed inputs before and after,
20 rows byte-identical -- but that probe lived in a scratch directory and was
deleted with it. **A one-off comparison proves the move; only a standing gate
proves it stays moved.** What would silently undo it is somebody inlining the
thresholds back into either caller, and neither the test suite nor
`pixi run params` can see that: a literal inside an `if` is not a parameter.

So the gate asserts the two claims that survive without a baseline to diff
against:

  1. the two entry points are the SAME function -- give the row reader the
     plate reader's kernel and the output is byte-identical
  2. the kernel is the ONLY difference, and it is the measured one: 3x3
     MORPH_OPEN empties row-sized glyphs (row_name_detector.OPEN_KERNEL
     records ink 908 -> 0 over thirteen real rows), so the row reader must
     keep passing None
"""
import hashlib
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from detector import row_name_detector as ROW                    # noqa: E402
from detector import weapon_template_detector as PLATE           # noqa: E402

FAILS = []


def check(what, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {what:<54} {got!r}'
          + ('' if ok else f'  != {want!r}'))
    if not ok:
        FAILS.append(what)


def fixtures():
    """Crops that exercise BOTH halves of the criterion.

    ⚠ `bright_and_grey` carries sky (bright, chromatic) and concrete (grey,
    dim) as well as glyph-white, because the pair test is exactly what
    separates those. A fixture with only white-on-black passes under any
    threshold and would let a broken merge through.
    """
    rng = np.random.default_rng(20260815)
    out = [('rng', rng.integers(0, 255, (40, 200, 3), dtype=np.uint8))]
    b = np.zeros((40, 200, 3), np.uint8)
    b[:, :60] = (230, 200, 120)
    b[:, 60:120] = (95, 95, 95)
    cv2.putText(b, 'M416', (125, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (255, 255, 255), 1, cv2.LINE_AA)
    out.append(('bright_and_grey', b))
    # Row-sized glyphs: the case the kernel decides. Smaller than a plate's.
    r = np.full((22, 220, 3), 70, np.uint8)
    cv2.putText(r, 'Extended Mag', (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                (255, 255, 255), 1, cv2.LINE_AA)
    out.append(('row_label', r))
    return out


def sha(a):
    return hashlib.sha1(np.ascontiguousarray(a)).hexdigest()[:16]


print('=== 一个实现，不是两个碰巧一致的实现 ===')
# ⚠ 第一版这里写的是 `sha(f(x)) == sha(f(x))`——同一个调用比它自己，**构造上不
# 可能红**。那正是 Step 2 点名的「测试表演」，而且它出现在一道**为了防止重新分叉
# 而写**的闸里：把实现抄回 row_name_detector、结果碰巧一样，它照样绿。
#
# 所以结构判据在前：行读者用的必须**就是**板读者那个函数对象。行为判据在后，
# 因为行为一致是这个结构的推论，不是它的替代品。
check('row 模块引用的就是 plate 那个函数',
      ROW._white_text_mask is PLATE._white_text_mask, True)
src = open(os.path.join(ROOT, 'detector', 'row_name_detector.py'),
           encoding='utf-8').read()
check('而且 row 里没有第二份算术（cvtColor 不在它自己的文件里做）',
      'COLOR_BGR2GRAY' in src, False)
for name, img in fixtures():
    check(f'{name}: text_mask == _white_text_mask(open_kernel=None)',
          sha(ROW.text_mask(img)),
          sha(PLATE._white_text_mask(img, None)))

print('\n=== 阈值只有一个作者 ===')
# 抄件的形状是「别处再写一次 180 / 30」。这里不内置那两个数——从作者读出来，
# 然后要求两个入口都跟着它走。内置一份就是又造了一个会说「全都对得上」的抄件。
probe = np.zeros((10, 6, 3), np.uint8)
probe[:, :] = (PLATE.GRAY_MIN + 5,) * 3          # 灰、无色、刚过线 -> 必须是字
just_under = np.zeros((10, 6, 3), np.uint8)
just_under[:, :] = (PLATE.GRAY_MIN - 5,) * 3     # 差 5 -> 必须不是字
chromatic = np.zeros((10, 6, 3), np.uint8)
chromatic[:, :] = (255, 255, 255 - PLATE.SPREAD_MAX - 5)   # 够亮但有色 -> 不是字
check('刚过 GRAY_MIN 的灰算字', int(ROW.text_mask(probe).max()), 255)
check('差 5 灰不算字', int(ROW.text_mask(just_under).max()), 0)
check('亮但有色不算字', int(ROW.text_mask(chromatic).max()), 0)
check('两个入口用的是同一对阈值',
      (PLATE.GRAY_MIN, PLATE.SPREAD_MAX) == (180, 30)
      and int(PLATE._white_text_mask(probe, None).max()) == 255, True)

print('\n=== 而 kernel 的差别是量出来的，不是风格 ===')
# row_name_detector.OPEN_KERNEL 上面记着实测：3x3 之后十三行的 ink 908 -> 0。
# 一个空 mask 下游读成「这一行什么都没有」，所以行读者不许用 3x3。
row = fixtures()[2][1]
kept = int((ROW.text_mask(row) > 0).sum())
opened = int((PLATE._white_text_mask(row, PLATE._OPEN_KERNEL) > 0).sum())
print(f'  row_label ink: 不开运算 {kept}  ·  3x3 开运算 {opened}')
check('行标签有墨可读', kept > 100, True)
check('3x3 会把它吃掉（所以行读者传 None）', opened * 4 < kept, True)
check('行读者确实传 None', ROW.OPEN_KERNEL, None)

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
    sys.exit(1)
print('all ok')
