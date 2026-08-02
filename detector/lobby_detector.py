"""Lobby vs match detector — is the game in the menus or in a round?

Two independent signals, deliberately not merged into one score:

  letterbox  The lobby renders 16:9 centred on the 21:9 screen, always. The
             right bar is exact-zero black there and cannot be in a match.
             This alone answers "is this the lobby".
  ping       The net-debug overlay, which only draws in a match. Independent
             of the bars, so it separates a match from any other full-bleed
             screen (loading, black transitions).

Keeping them separate is what makes the failure modes readable: the ping
overlay is a user setting and can be switched off, and when it is, the
detector degrades to "lobby / not lobby" instead of silently guessing. Call
selftest() once at startup to find out which mode you are in.

Owns its own RegionGrabber — see the Lobby section of config.py for why these
ROIs must stay out of HUD_REGIONS.

    from detector.lobby_detector import LobbyDetector, LobbyState
    det = LobbyDetector()
    if det.state() is LobbyState.LOBBY:
        ...
"""
import enum
import os

import cv2
import numpy as np

from config import (LOBBY_BAR_MAX, LOBBY_BAR_ROI, LOBBY_ERROR_MIN_SCORE,
                    LOBBY_RECONNECT_MIN_SCORE, LOBBY_RECONNECT_TEXT_ROI,
                    LOBBY_ERROR_TEXT_ROI, LOBBY_EXIT_MIN_SCORE,
                    LOBBY_EXIT_SEARCH, LOBBY_EXIT_TEXT_ROI, LOBBY_EXIT_THRESH,
                    LOBBY_LEAVE_CONFIRM_MIN_SCORE,
                    LOBBY_LEAVE_CONFIRM_TEXT_ROI,
                    LOBBY_LEAVE_MIN_SCORE, LOBBY_LEAVE_TEXT_ROI,
                    LOBBY_MENU_MIN_SCORE, LOBBY_MENU_SEARCH,
                    LOBBY_MENU_THRESH, LOBBY_MENU_TITLE_ROI,
                    LOBBY_PING_MIN_FRAC, LOBBY_PING_ROI, LOBBY_PING_THRESH)
from detector.cropper import RegionGrabber, win32_cap

_TMPL_DIR = os.path.join(os.path.dirname(__file__), '..', 'training_data',
                         'pubg_assets', 'lobby')
EXIT_TMPL_PATH = os.path.join(_TMPL_DIR, 'exit_to_lobby_mask.png')
MENU_TMPL_PATH = os.path.join(_TMPL_DIR, 'system_menu_mask.png')
LEAVE_TMPL_PATH = os.path.join(_TMPL_DIR, 'leave_training_mask.png')
LEAVE_CONFIRM_TMPL_PATH = os.path.join(_TMPL_DIR, 'leave_confirm_mask.png')
ERROR_TMPL_PATH = os.path.join(_TMPL_DIR, 'error_title_mask.png')
RECONNECT_TMPL_PATH = os.path.join(_TMPL_DIR, 'reconnect_mask.png')

BAR = 'lobby_bar'
PING = 'lobby_ping'
REGIONS = {BAR: LOBBY_BAR_ROI, PING: LOBBY_PING_ROI}


class LobbyState(enum.Enum):
    LOBBY = 'lobby'          # menus: letterboxed
    IN_GAME = 'in_game'      # full-bleed, net overlay drawing, no pause menu
    MENU = 'menu'            # in a round but the ESC menu is up
    FULLBLEED = 'fullbleed'  # full-bleed, no overlay — loading, or ping is off
    DISCONNECTED = 'disconnected'   # dropped by the server; RECONNECT is up

    @property
    def playable(self):
        """True only when the round is running AND accepting input.

        The two states that are deliberately NOT playable are the two a caller
        is most likely to mistake for a running match:

        FULLBLEED covers the loading screen, where clicks and keys go nowhere.
        MENU is a live round with the ESC menu over it — the scene is still
        drawing and the ping overlay is still up, so every pixel probe says
        "in a match", but keys go to the menu instead of the character.
        DISCONNECTED is a black screen with a RECONNECT button, which the
        letterbox probe cannot tell from the lobby.
        """
        return self is LobbyState.IN_GAME


def bar_max(crop):
    """Brightest pixel in the letterbox probe. 0 means the bar is not drawn."""
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return int(g.max())


def ping_fraction(crop):
    """Fraction of the net-overlay probe that is overlay-bright text."""
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return float((g > LOBBY_PING_THRESH).sum()) / g.size


def classify(bar_crop, ping_crop, menu_open=False, disconnected=False):
    """State from the two probes. Pure function — takes crops, no capture.

    `menu_open` and `disconnected` are passed in rather than measured here:
    both probes live in different parts of the screen and are only worth
    grabbing once the cheap probes have narrowed things down.
    """
    # Checked first because it is invisible to everything below: the drop
    # screen is almost entirely black, so the letterbox probe reads 0 and
    # calls it the lobby. Three PLAY clicks and thirty seconds went into that.
    if disconnected:
        return LobbyState.DISCONNECTED
    if bar_max(bar_crop) <= LOBBY_BAR_MAX:
        return LobbyState.LOBBY
    if ping_fraction(ping_crop) >= LOBBY_PING_MIN_FRAC:
        return LobbyState.MENU if menu_open else LobbyState.IN_GAME
    return LobbyState.FULLBLEED


def classify_frame(frame):
    """Same verdict from a full-screen BGR frame, for offline use on saved
    screenshots. The live path uses the grabber instead."""
    def cut(roi):
        y, x, h, w = roi
        return frame[y:y + h, x:x + w]
    return classify(cut(LOBBY_BAR_ROI), cut(LOBBY_PING_ROI),
                    is_system_menu(frame), reconnect_visible(frame))


# ── Results screen ───────────────────────────────────────────────────────
# Splits FULLBLEED into "match over, there is a button to click" and
# "everything else full-bleed" (loading). Deliberately NOT part of
# LobbyDetector's grabber: its ROI sits at y=1350, far from the other two, and
# _cluster would merge the bands into a 3120x1176 box grabbed at every poll.
# It is only ever needed once FULLBLEED is already established, so it pays for
# its own one-shot grab of 210x26 px.

def _text_mask(img, thresh):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return ((g > thresh) * 255).astype(np.uint8)


def _load_template(path):
    t = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    # ultralytics swaps cv2.imread for a wrapper that defaults to IMREAD_COLOR;
    # any process that imported it gets 3 channels back. See detector/CLAUDE.md.
    if t is not None and t.ndim == 3:
        t = t[:, :, 0]
    return t


_EXIT_TMPL = _load_template(EXIT_TMPL_PATH)
_MENU_TMPL = _load_template(MENU_TMPL_PATH)
_LEAVE_TMPL = _load_template(LEAVE_TMPL_PATH)
_LEAVE_CONFIRM_TMPL = _load_template(LEAVE_CONFIRM_TMPL_PATH)
_ERROR_TMPL = _load_template(ERROR_TMPL_PATH)
_RECONNECT_TMPL = _load_template(RECONNECT_TMPL_PATH)


def _score(crop, tmpl, thresh):
    """Best template match inside a search window.

    Returns 0.0 when the template is missing, so every caller fails closed:
    no EXIT template means waiting the results screen out instead of clicking
    blind, and no menu template means MENU is never reported.
    """
    if tmpl is None:
        return 0.0
    win = _text_mask(crop, thresh)
    if win.shape[0] < tmpl.shape[0] or win.shape[1] < tmpl.shape[1]:
        return 0.0
    return float(cv2.matchTemplate(win, tmpl, cv2.TM_CCOEFF_NORMED).max())


def _search_roi(roi, pad):
    y, x, h, w = roi
    return (max(0, y - pad), max(0, x - pad), h + 2 * pad, w + 2 * pad)


def _grab(roi, frame=None):
    if frame is None:
        return win32_cap(roi)
    y, x, h, w = roi
    return frame[y:y + h, x:x + w]


def exit_button_score(crop):
    """Match the EXIT TO LOBBY glyphs in a crop of the search window."""
    return _score(crop, _EXIT_TMPL, LOBBY_EXIT_THRESH)


def menu_title_score(crop):
    """Match the SYSTEM MENU title in a crop of the search window."""
    return _score(crop, _MENU_TMPL, LOBBY_MENU_THRESH)


def is_results_screen(frame=None):
    """True when the match-end screen with its EXIT TO LOBBY button is up.

    Pass a full-screen frame for offline use; with no argument it grabs the
    button's search window itself.
    """
    roi = _search_roi(LOBBY_EXIT_TEXT_ROI, LOBBY_EXIT_SEARCH)
    return exit_button_score(_grab(roi, frame)) >= LOBBY_EXIT_MIN_SCORE


def is_system_menu(frame=None):
    """True when the ESC / system menu is over the scene.

    Matched on the SYSTEM MENU title, not on the entries: the entry list
    differs between the training range (LEAVE TRAINING) and a real match, but
    the title is the same screen either way.
    """
    roi = _search_roi(LOBBY_MENU_TITLE_ROI, LOBBY_MENU_SEARCH)
    return menu_title_score(_grab(roi, frame)) >= LOBBY_MENU_MIN_SCORE


def leave_entry_score(crop):
    """Match the LEAVE TRAINING glyphs in a crop of the search window."""
    return _score(crop, _LEAVE_TMPL, LOBBY_MENU_THRESH)


def leave_entry_confirmed(frame=None):
    """True only when LEAVE TRAINING really is at the coordinate we click.

    This gate is not optional. EXIT TO DESKTOP sits one 85 px pitch below
    LEAVE TRAINING, so on any menu whose entries are ordered differently — a
    real match, a future patch — a blind click at LOBBY_MENU_LEAVE_XY quits
    the game instead of returning to the lobby. Measured confusion against
    every entry in the captured menu: 1.000 for LEAVE TRAINING, 0.152 for
    EXIT TO DESKTOP.

    Fails closed: a missing template scores 0.0 and nothing gets clicked.
    """
    roi = _search_roi(LOBBY_LEAVE_TEXT_ROI, LOBBY_MENU_SEARCH)
    return leave_entry_score(_grab(roi, frame)) >= LOBBY_LEAVE_MIN_SCORE


def reconnect_score(crop):
    """Match the RECONNECT button's glyphs in a crop of the search window."""
    return _score(crop, _RECONNECT_TMPL, LOBBY_MENU_THRESH)


def reconnect_visible(frame=None):
    """True when the server has dropped the session and offers RECONNECT.

    Gated on the button rather than on the ERROR title. There are two error
    screens: this one and the inactivity logout, whose titles sit 38 px apart
    — close enough that telling them apart by position is a coincidence
    waiting to break. Only this one has a RECONNECT button, and the two want
    different clicks in different places.
    """
    roi = _search_roi(LOBBY_RECONNECT_TEXT_ROI, LOBBY_MENU_SEARCH)
    return reconnect_score(_grab(roi, frame)) >= LOBBY_RECONNECT_MIN_SCORE


def error_dialog_score(crop):
    """Match the ERROR title in a crop of the search window."""
    return _score(crop, _ERROR_TMPL, LOBBY_MENU_THRESH)


def error_dialog_visible(frame=None):
    """True while a modal ERROR dialog is up — most often the inactivity
    logout, which drops the session and then blocks re-entry behind itself.

    Deliberately not specific to one message: OK is the only button on any of
    them, and a campaign stuck behind an undismissed dialog is worse than one
    that clicked OK on something unexpected. Callers log it.
    """
    roi = _search_roi(LOBBY_ERROR_TEXT_ROI, LOBBY_MENU_SEARCH)
    return error_dialog_score(_grab(roi, frame)) >= LOBBY_ERROR_MIN_SCORE


def leave_confirm_score(crop):
    """Match the LEAVE TRAINING dialog title in a crop of the search window."""
    return _score(crop, _LEAVE_CONFIRM_TMPL, LOBBY_MENU_THRESH)


def leave_confirm_visible(frame=None):
    """True while the "Do you want to exit training?" dialog is up.

    Clicking the menu entry does not leave — it raises this, and the game
    waits. The dialog dims the ping overlay enough that classify() calls it
    FULLBLEED, i.e. indistinguishable from a loading screen, which is why the
    caller has to ask this question separately rather than reading the state.

    Fails closed: a missing template scores 0.0 and nothing gets clicked.
    """
    roi = _search_roi(LOBBY_LEAVE_CONFIRM_TEXT_ROI, LOBBY_MENU_SEARCH)
    return leave_confirm_score(_grab(roi, frame)) >= LOBBY_LEAVE_CONFIRM_MIN_SCORE


class LobbyDetector:
    """Live lobby/match state off its own two-band grab."""

    def __init__(self, grabber=None):
        self._grabber = grabber if grabber is not None else RegionGrabber(REGIONS)
        self._own = grabber is None

    def probes(self):
        """Raw measurements, for logging and for calibration scripts."""
        crops = self._grabber.grab()
        return {'bar_max': bar_max(crops[BAR]),
                'ping_frac': ping_fraction(crops[PING]),
                'menu_score': menu_title_score(
                    _grab(_search_roi(LOBBY_MENU_TITLE_ROI,
                                      LOBBY_MENU_SEARCH)))}

    def state(self):
        crops = self._grabber.grab()
        base = classify(crops[BAR], crops[PING])
        # The menu probe is a second grab in a different part of the screen,
        # so it is only paid for once the cheap pair has said "in a match".
        if base is LobbyState.IN_GAME and is_system_menu():
            return LobbyState.MENU
        # A dropped session is a black screen, so it arrives here disguised as
        # the lobby — and only here, since nothing else makes the letterbox
        # read zero. Paid for only in that branch.
        if base is LobbyState.LOBBY and reconnect_visible():
            return LobbyState.DISCONNECTED
        return base

    def selftest(self):
        """Report which signals are usable right now.

        The ping overlay is a game setting. If it is off, `state()` can never
        return IN_GAME and every match reads as FULLBLEED — a failure that is
        otherwise silent and looks exactly like a permanent loading screen.
        Returns (ok, message); ok is False when the caller must not rely on
        IN_GAME.
        """
        p = self.probes()
        letterboxed = p['bar_max'] <= LOBBY_BAR_MAX
        if letterboxed:
            return True, (f'in the lobby (bar max {p["bar_max"]}); '
                          f'cannot check the ping overlay from here')
        if p['ping_frac'] >= LOBBY_PING_MIN_FRAC:
            return True, (f'in a match, ping overlay present '
                          f'(frac {p["ping_frac"]:.3f})')
        return False, (f'full-bleed but no ping overlay (frac '
                       f'{p["ping_frac"]:.3f}). Either this is a loading '
                       f'screen, or the net-debug overlay is switched off — '
                       f'if it is off, IN_GAME is unreachable. Turn it on in '
                       f'the game settings, or re-run this while a round is '
                       f'definitely up.')

    def close(self):
        if self._own:
            self._grabber.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def snapshot():
    """One-shot state without holding a grabber open. For scripts."""
    return classify(win32_cap(LOBBY_BAR_ROI), win32_cap(LOBBY_PING_ROI))
