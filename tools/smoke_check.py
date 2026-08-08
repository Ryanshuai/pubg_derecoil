"""Cold-start check — does the whole stack still come up?

Compiles every module, builds every detector (which loads the weights and
templates), runs the capture backend against whatever is on screen, and looks
for the Pico on the serial bus. Injects no input and needs no game window, so
it is safe to run any time.

    pixi run smoke
"""
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

_failures = []


def _section(title):
    print(f"\n=== {title} ===")


def _check(name, fn):
    """Run fn, print one aligned OK/FAIL line, remember failures."""
    try:
        result = fn()
        detail = '' if result is None else f'  {result}'
        print(f"  OK    {name}{detail}")
        return True
    except Exception as e:
        print(f"  FAIL  {name}  {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)
        _failures.append(name)
        return False


# ── 1. interpreter + third-party stack ──────────────────────
_section("environment")
print(f"  python   {sys.version.split()[0]}  {sys.executable}")

import importlib.metadata as md

# ⚠ torch, torchvision and ultralytics are NOT in this list any more, and
# their absence is the point. Nothing in this repo imports them as of
# 2026-08-08: the fire-mode CNN went (2 answers in 859 crops) and the aim-assist
# trees went with the operator's call. They are still installed, because pulling
# a multi-GB CUDA wheel out of the environment is its own change and other
# agents run out of this same env. Listing them here would make a smoke check
# FAIL on a package the repo has no opinion about — a gate that guards an
# unused dependency teaches people to ignore gate output.
for dist, mod in [('numpy', 'numpy'), ('opencv-python', 'cv2'),
                  ('scikit-learn', 'sklearn'),
                  ('scipy', 'scipy'), ('pyserial', 'serial'), ('pywin32', 'win32gui'),
                  ('bettercam', 'bettercam'), ('hidapi', 'hid')]:
    try:
        __import__(mod)
        # The runtime version is what matters — a dist can be shadowed by another
        # copy earlier on sys.path, which is exactly the bug this line catches.
        runtime = getattr(sys.modules[mod], '__version__', '?')
        print(f"  {dist:<15} {runtime:<12} (dist {md.version(dist)})")
    except Exception as e:
        print(f"  {dist:<15} MISSING  {type(e).__name__}: {e}")
        _failures.append(dist)

# ⚠ A `cuda available?` line stood here. It is gone because the answer stopped
# mattering: no detector, no calibration path and no control loop imports torch
# any more. Verified rather than assumed — `import robot; 'torch' in sys.modules`
# is False.

# ── 2. detectors + model weights ────────────────────────────
_section("detectors")

from detector.ammo_detector import AmmoDetector
from detector.game_state import GameState
from detector.weapon_hud_detector import WeaponHudDetector
from detector.fire_mode_detector import FireModeDetector
from detector.posture_detector import PostureDetector
from detector.highlight_detector import HighlightDetector
from detector.tab_detector import TabTypeDetector
from detector.weapon_template_detector import TabWeaponDetector
from detector.attachment_detector import AttachmentDetector
from detector.spawner_detector import SpawnerDetector
from detector.slot_detector import SlotDetector
from detector.view_tracker import ViewTracker

state = GameState()
_check('GameState', lambda: None)
# `ready` is checked, not just construction: a missing bank file makes this
# detector read '' for every weapon instead of raising, which is the right
# runtime behaviour and exactly the failure a smoke check has to catch.
_check('WeaponHudDetector', lambda: None if WeaponHudDetector().ready
       else (_ for _ in ()).throw(RuntimeError('bank missing: '
             'pixi run python calibration/build_weapon_hud_bank.py')))
_check('FireModeDetector', lambda: FireModeDetector() and None)
_check('PostureDetector', lambda: PostureDetector() and None)
_check('HighlightDetector', lambda: HighlightDetector() and None)
_check('TabTypeDetector', lambda: TabTypeDetector() and None)
_check('TabWeaponDetector', lambda: TabWeaponDetector() and None)
_check('AttachmentDetector', lambda: AttachmentDetector() and None)
_check('SpawnerDetector', lambda: SpawnerDetector() and None)
_check('SlotDetector', lambda: SlotDetector() and None)
_check('ViewTracker', lambda: ViewTracker() and None)


def _ammo():
    """An incomplete digit set is reported, not failed: a missing digit makes
    those counts read None, which is honest — unlike a drifted template, which
    would read a wrong number. tools/collect_ammo_digits.py fills the gaps."""
    have = AmmoDetector().digits_known
    missing = [d for d in range(10) if d not in have]
    return f"digits {''.join(map(str, have)) or 'none'}" + (
        f"  MISSING {''.join(map(str, missing))} "
        f"-> tools/collect_ammo_digits.py" if missing else "  (full set)")


_check('AmmoDetector', _ammo)

# ── 3. recoil patterns ──────────────────────────────────────
_section("recoil data")
import json

from detector.weapon import SCALES_PATH


def _scales():
    with open(SCALES_PATH, encoding='utf-8') as f:
        return f"{len(json.load(f))} weapons"


_check('weapon_scales.json', _scales)

# ── 4. capture backend ──────────────────────────────────────
_section("capture")


def _capture():
    from capture.screen_capture import ScreenCapture
    cap = ScreenCapture()
    cap.start()
    try:
        time.sleep(1.5)
        fps = cap.fps
        if cap.latest() is None:
            raise RuntimeError("no frame in the buffer after 1.5s")
        return f"{cap.backend}  {fps:.0f} fps"
    finally:
        cap.stop()


_check('ScreenCapture', _capture)

# ── 5. Pico link ────────────────────────────────────────────
_section("hardware")


def _pico():
    # press.pico_mouse.find_pico, NOT a second comports() scan of our own.
    # This used to repeat both the VID and the PID tuple, and a smoke check
    # that recognises the Pico by its own list can report green for a device
    # the driver would then refuse to open.
    from press.pico_mouse import find_pico
    p = find_pico()
    if p is None:
        raise RuntimeError("Pico not on the serial bus (check USB / PICO_PORT)")
    return f"{p.device}  vid=0x{p.vid:04X} pid=0x{p.pid:04X}"


_check('pico mouse', _pico)

# ── verdict ─────────────────────────────────────────────────
print()
if _failures:
    print(f"FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("all green")
