"""Tab scan — composite detector for inventory screen.

Captures fullscreen, verifies Tab is open, dispatches to sub-detectors.
Each sub-detector updates GameState directly.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from detector.cropper import win32_cap
from detector.tab_detector import TabDetector, SLOT_RECT as TAB_RECT
from detector.weapon_template_detector import WeaponTemplateDetector, OCR_RECTS
from detector.attachment_detector import AttachmentDetector
from config import SCREEN_W, SCREEN_H


class TabScan:

    def __init__(self, device, state):
        self.state = state
        self._tab = TabDetector(device, state)
        self._weapon_tpl = WeaponTemplateDetector(state)
        self._attach = AttachmentDetector(device, state)

    def query(self):
        """Capture fullscreen, verify tab open, read weapons + attachments."""
        screen = win32_cap((0, 0, SCREEN_H, SCREEN_W))

        tab_crop = screen[TAB_RECT[0]:TAB_RECT[0]+TAB_RECT[2],
                          TAB_RECT[1]:TAB_RECT[1]+TAB_RECT[3]].copy()
        if not self._tab.classify(tab_crop):
            return

        r1, r2 = OCR_RECTS[1], OCR_RECTS[2]
        self._weapon_tpl.read_from_crops(
            screen[r1[0]:r1[0]+r1[2], r1[1]:r1[1]+r1[3]].copy(),
            screen[r2[0]:r2[0]+r2[2], r2[1]:r2[1]+r2[3]].copy())

        for gun_id in [1, 2]:
            self._attach.classify_gun(screen, gun_id)

        self.state._print_status()
