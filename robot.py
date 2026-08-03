"""Robot — assembly entry point.

Creates components, registers detectors, starts threads. No business logic.
All behavior is driven by config tables.
"""
import sys

# Force UTF-8 stdout so CN attachment names don't crash print_status
# (Windows defaults to cp936/cp1252 which can't encode them). Must run
# before any module-level print.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import torch

from detector.game_state import GameState
from detector.weapon_dl_detector import WeaponClassifier
from detector.fire_mode_detector import FireModeDetector
from detector.posture_detector import PostureDetector
from detector.highlight_detector import HighlightDetector
from detector.tab_detector import TabTypeDetector
from detector.weapon_template_detector import TabWeaponDetector
from detector.attachment_detector import AttachmentDetector
from screen_capture import ScreenCapture
from key_poller import KeyPoller
from control.match import Dispatcher


class Robot:
    def __init__(self):
        self.state = GameState()
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        # Components
        self.capture = ScreenCapture()
        self.poller = KeyPoller()
        self.dispatcher = Dispatcher(self.state, self.capture, self.poller)

        # Register detectors
        self.dispatcher.register('weapon_hud', WeaponClassifier(device))
        self.dispatcher.register('fire_mode', FireModeDetector(device))
        self.dispatcher.register('posture', PostureDetector())
        self.dispatcher.register('highlight', HighlightDetector(self.state))
        self.dispatcher.register('tab_type', TabTypeDetector(device))
        self.dispatcher.register('tab_weapon', TabWeaponDetector())
        self.dispatcher.register('tab_attachment', AttachmentDetector())

        # Start threads
        self.capture.start()
        self.poller.start()
        self.dispatcher.start()
        print("init done", flush=True)

    def shutdown(self):
        """Stop the threads, THEN disarm.

        This used to be stop() plus save_scales(), and stop() only clears a
        flag -- so every exit that was not the f13 key (Ctrl-C, an exception,
        join() returning) left the firmware compensating and the pattern
        loaded. Dispatcher.shutdown() is where the hardware reset lives; the
        join() in between is what stops the loop from re-arming it.

        It also saves the scales, so this no longer does.
        """
        self.poller.stop()
        self.capture.stop()
        self.dispatcher.stop()
        self.dispatcher.join()
        self.dispatcher.shutdown()


if __name__ == '__main__':
    robot = Robot()
    try:
        robot.dispatcher.join()
    finally:
        robot.shutdown()
