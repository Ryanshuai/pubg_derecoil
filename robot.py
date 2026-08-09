"""Robot — assembly entry point.

Creates components, registers detectors, starts threads. No business logic.
All behavior is driven by config tables.

⚠ IT ALSO KEEPS A LOG, AND UNTIL 2026-08-09 IT DID NOT. Everything this
process knows about why it is not compensating -- which curve it looked up,
which one it could not find, which part it could not name, whether the upload
to the Pico raised -- was printed to a terminal and then scrolled away. The
first time somebody said "I just tried it and it does not hold the gun down",
the answer was already gone.

That is the same shape as the rule in the root CLAUDE.md about not grepping a
live run: a session you cannot replay is the one session whose evidence you
must not throw away. A play session is exactly that.
"""
import datetime
import os
import sys

# Force UTF-8 stdout so CN attachment names don't crash print_status
# (Windows defaults to cp936/cp1252 which can't encode them). Must run
# before any module-level print.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass


class _Tee:
    """Write to the terminal AND to a file. Never swallows the terminal.

    ⚠ THE FILE IS THE COPY, NOT THE DESTINATION. If the log file cannot be
    opened or a write to it raises, the terminal write has already happened
    and the failure is dropped -- a broken log must not be able to take down
    a session that is otherwise fine, and least of all one that is driving
    hardware.
    """

    def __init__(self, stream, fh):
        self._stream, self._fh = stream, fh

    def write(self, s):
        n = self._stream.write(s)
        try:
            self._fh.write(s)
            self._fh.flush()
        except (OSError, ValueError):
            pass
        return n

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def start_log(root=None):
    """Tee stdout+stderr into calibration/artifacts/robot/<stamp>.log.

    -> the path, or None if it could not be opened. One file per run, because
    the question a log answers here is always "what happened in THAT session".
    """
    root = root or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'calibration', 'artifacts', 'robot')
    try:
        os.makedirs(root, exist_ok=True)
        stamp = datetime.datetime.now().strftime('%m%d_%H%M%S')
        path = os.path.join(root, f'{stamp}.log')
        fh = open(path, 'a', encoding='utf-8', errors='replace')
    except OSError as e:
        print(f'[log] cannot open a log file ({e}) -- terminal only',
              flush=True)
        return None
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    print(f'[log] {path}', flush=True)
    return path

from detector.game_state import GameState
from detector.weapon_hud_detector import WeaponHudDetector
from detector.fire_mode_detector import FireModeDetector
from detector.posture_detector import PostureDetector
from detector.highlight_detector import HighlightDetector
from detector.tab_detector import TabTypeDetector
from detector.weapon_template_detector import TabWeaponDetector
from detector.attachment_detector import AttachmentDetector
from capture.screen_capture import ScreenCapture
from capture.key_poller import KeyPoller
from control.match import Dispatcher


class Robot:
    def __init__(self):
        self.state = GameState()
        # ⚠ There was a `device = torch.device(...)` here until 2026-08-08.
        # FireModeDetector was the last torch user in the whole detector graph,
        # and it is a RandomForest now. Nothing on the frame path imports torch.

        # Components
        self.capture = ScreenCapture()
        self.poller = KeyPoller()
        self.dispatcher = Dispatcher(self.state, self.capture, self.poller)

        # Register detectors
        self.dispatcher.register('weapon_hud', WeaponHudDetector())
        self.dispatcher.register('fire_mode', FireModeDetector())
        self.dispatcher.register('posture', PostureDetector())
        self.dispatcher.register('highlight', HighlightDetector())
        self.dispatcher.register('tab_type', TabTypeDetector())
        self.dispatcher.register('tab_weapon', TabWeaponDetector())
        self.dispatcher.register('tab_attachment', AttachmentDetector())

        # Start threads
        self.capture.start()
        self.poller.start()
        self.dispatcher.start()
        print("init done", flush=True)

    # How long the dispatcher gets to notice stop() before we disarm anyway.
    # A tick is 10 ms plus whatever one pass of detectors costs, so this is
    # two orders of margin -- it is a deadline, not an expected duration.
    JOIN_TIMEOUT_S = 2.0

    def shutdown(self):
        """Stop the threads, THEN disarm.

        This used to be stop() plus save_scales(), and stop() only clears a
        flag -- so every exit that was not the f13 key (Ctrl-C, an exception,
        join() returning) left the firmware compensating and the pattern
        loaded. Dispatcher.shutdown() is where the hardware reset lives; the
        join() in between is what stops the loop from re-arming it.

        It also saves the scales, so this no longer does.

        The join is BOUNDED, and the disarm runs whether or not it came back
        in time. Ordering the two is a preference; disarming is not. A loop
        wedged inside a detector would otherwise take the Pico with it, and
        three agents share that port -- the next run would measure a gun
        nobody is holding.
        """
        self.poller.stop()
        self.capture.stop()
        self.dispatcher.stop()
        self.dispatcher.join(self.JOIN_TIMEOUT_S)
        if self.dispatcher.is_alive():
            print(f'[shutdown] dispatcher still running after '
                  f'{self.JOIN_TIMEOUT_S}s -- disarming anyway', flush=True)
        self.dispatcher.shutdown()


if __name__ == '__main__':
    # ⚠ BEFORE Robot(), because the lines worth having start at import: which
    # curves loaded, which ones are seeds, and the first `[curves] no fitted
    # curve for ...` a gun produces the moment it is picked up.
    start_log()
    robot = Robot()
    try:
        # NOT dispatcher.join(). See DaemonLoop.wait -- a no-timeout join on
        # Windows holds a pending SIGINT until it returns, and this loop only
        # returns on f13, so Ctrl-C was never delivered at all.
        robot.dispatcher.wait()
    except KeyboardInterrupt:
        print('\n[robot] Ctrl-C', flush=True)
    finally:
        robot.shutdown()
