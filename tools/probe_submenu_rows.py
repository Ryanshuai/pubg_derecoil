"""Does find_submenu_items() get every entry of every category?

Ground truth comes from detector/attachment_catalog.py, which was read off
this very run: the weapon classes and the five attachment slots have known
counts, so the detector can be checked against them rather than against
eyeballing.
"""
import glob
import os
import sys

import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from detector.spawner_layout import find_menu, column_boxes, find_submenu_items
from detector.attachment_catalog import ROSTER, ATTACHMENTS
from control.spawner import SPAWNER_EXTRAS

RUN = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(
    os.path.join(ROOT, 'docs', 'spawner', 'runs', '*')))[-1]


def n_class(c):
    return sum(1 for v in ROSTER.values() if v[0] == c)


def n_slot(s):
    # Plus whatever the spawner lists that the catalogue has no key for —
    # 握把 carries 箭袋 (十字弩), which is why this is not just a count.
    return (sum(1 for v in ATTACHMENTS.values() if v['slot'] == s)
            + len(SPAWNER_EXTRAS.get(s, {})))


# category key -> (label, expected count or None if not catalogued)
EXPECT = {
    'col1_row01': ('突击步枪', n_class('AR')),
    'col1_row02': ('狙击步枪', None),
    'col1_row03': ('射手步枪', n_class('DMR')),
    'col1_row04': ('霰弹枪', None),
    'col1_row05': ('冲锋枪', n_class('SMG')),
    'col1_row06': ('手枪', None),
    'col1_row07': ('可投掷物品', None),
    'col1_row08': ('近战', None),
    'col1_row09': ('其他', None),
    'col1_row10': ('轻机枪', n_class('LMG')),
    'col2_row01': ('握把', n_slot('grip')),
    'col2_row02': ('弹匣', n_slot('magazine')),
    'col2_row03': ('枪口', n_slot('muzzle')),
    'col2_row04': ('枪托', n_slot('stock')),
    'col2_row05': ('瞄准镜', n_slot('scope')),
    'col3_row01': ('汽油桶', None),
    'col3_row02': ('能量物品', None),
    'col3_row03': ('治疗物品', None),
    'col3_row04': ('头盔', None),
    'col3_row05': ('防弹衣', 3),
    'col3_row06': ('背包', 3),
}

base = cv2.imread(os.path.join(RUN, '00_baseline.png'))
boxes = column_boxes(find_menu(base, verbose=False))
print(f'{os.path.basename(RUN)}  boxes {boxes}\n')
print(f'{"key":<12}{"label":<12}{"found":>6}{"expect":>8}   pitch   y range')

bad = 0
for f in sorted(glob.glob(os.path.join(RUN, '*_open.png'))):
    key = os.path.basename(f)[:-len('_open.png')]
    label, exp = EXPECT.get(key, ('?', None))
    items = find_submenu_items(cv2.imread(f), boxes[int(key[3])])
    ys = [i['y'] for i in items]
    pitch = (ys[-1] - ys[0]) / (len(ys) - 1) if len(ys) > 1 else 0.0
    flag = ''
    if exp is not None and len(items) != exp:
        flag, bad = '   <-- MISMATCH', bad + 1
    print(f'{key:<12}{label:<12}{len(items):>6}{str(exp or "-"):>8}   '
          f'{pitch:5.1f}   {ys[0] if ys else "-"}..{ys[-1] if ys else "-"}{flag}')

print(f'\n{bad} mismatches against the catalogue')

