"""Offline regression for LobbyDetector against docs/lobby/.

Ground truth is asserted per shot rather than eyeballed. Add a row whenever a
new state gets captured — the states listed under "not covered" at the bottom
are the ones that would silently misclassify today.

    pixi run python tools/verify_lobby_detector.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from detector.geometry import cut

from config import (LOBBY_BAR_ROI, LOBBY_ERROR_TEXT_ROI,
                    LOBBY_RECONNECT_TEXT_ROI,
                    LOBBY_LEAVE_CONFIRM_TEXT_ROI,
                    LOBBY_LEAVE_TEXT_ROI, LOBBY_MENU_SEARCH, LOBBY_PING_ROI)
from detector.lobby_detector import (LobbyState, _search_roi, bar_max,
                                     classify_frame, error_dialog_score,
                                     error_dialog_visible, is_results_screen,
                                     leave_confirm_score, leave_confirm_visible,
                                     leave_entry_confirmed, leave_entry_score,
                                     ping_fraction, reconnect_score,
                                     reconnect_visible)

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      'docs', 'lobby')

#            file, expected state, expected is_results_screen, note
CASES = [
    ('lobby.png',       LobbyState.LOBBY,     False,
     'lobby, PLAY / TRAINING selected'),
    ('in_game.png',     LobbyState.IN_GAME,   False,
     'training range, no UI'),
    ('in_game_tab.png', LobbyState.IN_GAME,   False,
     'training range, Tab open'),
    # The confirm dialog raised by clicking LEAVE TRAINING. It dims the ping
    # overlay below the threshold, so it lands in FULLBLEED alongside loading
    # screens -- and the two want opposite treatment, which is what
    # leave_confirm_visible() is for.
    ('leave_confirm.png', LobbyState.FULLBLEED, False,
     '"Do you want to exit training?" CONFIRM / CANCEL'),
    # Idling gets the session dropped, and the dialog then blocks re-entry
    # behind itself. It is drawn OVER the lobby, but its dimming overlay lifts
    # the letterbox bar from 0 to 60 -- past LOBBY_BAR_MAX -- so it reads
    # FULLBLEED. That makes three unrelated screens sharing that state, all
    # wanting different treatment, which is the whole argument for the gates
    # below.
    ('error_inactivity.png', LobbyState.FULLBLEED, False,
     '"You have been logged off due to inactivity" OK'),
    # Dropped by the server. Almost entirely black, so the letterbox probe
    # reads 0 and calls it the lobby -- which is why DISCONNECTED had to
    # become a state rather than another gate: PLAY does nothing here, and
    # three retries went into finding that out.
    ('error_disconnected.png', LobbyState.DISCONNECTED, False,
     '"The service is not available" RECONNECT'),
    # Results screen: full-bleed, and its top gradient covers the ping overlay
    # completely (ping_frac 0.000, lower than the lobby's 0.021). FULLBLEED is
    # the right answer -- nothing is drivable here -- but it lands there by
    # occlusion, not because the state was designed for it. The EXIT template
    # is what separates it from a loading screen.
    ('results.png',     LobbyState.FULLBLEED, True,
     'WINNER WINNER, "exit to lobby in 18s"'),
    # ESC menu: scene still full-bleed, ping overlay still drawing, so the two
    # cheap probes both say IN_GAME. Without the title template this reports
    # playable=True while every key goes to the menu.
    ('system_menu.png', LobbyState.MENU,      False,
     'SYSTEM MENU over the training range'),
]

UNCOVERED = ['loading screen (LOBBY -> IN_GAME transition)',
             'ping overlay switched off',
             'a real match in progress (only the training range is captured)',
             "a real match's ESC menu (entry list differs from the training "
             "range's LEAVE TRAINING)"]


def confusion():
    """The click-gate templates must fire on exactly one screen each.

    A gate that fires nowhere is merely broken and says so the first time it
    is used. A gate that fires on the WRONG screen clicks a coordinate that
    means something else there — which is the failure mode the gates exist to
    prevent, and the one no single screenshot can reveal.
    """
    gates = [('leave_confirm', leave_confirm_visible, leave_confirm_score,
              LOBBY_LEAVE_CONFIRM_TEXT_ROI, 'leave_confirm.png'),
             ('leave_entry', leave_entry_confirmed, leave_entry_score,
              LOBBY_LEAVE_TEXT_ROI, 'system_menu.png'),
             ('error_dialog', error_dialog_visible, error_dialog_score,
              LOBBY_ERROR_TEXT_ROI, 'error_inactivity.png'),
             ('reconnect', reconnect_visible, reconnect_score,
              LOBBY_RECONNECT_TEXT_ROI, 'error_disconnected.png')]
    bad = 0
    print(f'\n{"gate":<16}{"screen":<22}{"score":>7}  fires')
    for gate, visible, score, roi, expect_on in gates:
        y, x, h, w = _search_roi(roi, LOBBY_MENU_SEARCH)
        for name, *_ in CASES:
            frame = cv2.imread(os.path.join(ASSETS, name))
            if frame is None:
                continue
            s = score(frame[y:y + h, x:x + w])
            fires = visible(frame)
            want = (name == expect_on)
            ok = fires == want
            bad += not ok
            print(f'{gate:<16}{name:<22}{s:>7.3f}  '
                  f'{str(fires):<6}{"" if ok else "  <-- WRONG"}')
    return bad


def main():
    bad = 0
    print(f'{"file":<18} {"expect":<10} {"got":<10} {"play":>5} '
          f'{"bar_max":>7} {"ping_frac":>9} {"results?":>9}  note')
    for name, expect, expect_results, note in CASES:
        frame = cv2.imread(os.path.join(ASSETS, name))
        if frame is None:
            print(f'{name:<18} MISSING from docs/lobby/')
            bad += 1
            continue
        got = classify_frame(frame)
        got_results = is_results_screen(frame)
        ok = got is expect and got_results == expect_results
        bad += not ok
        print(f'{name:<18} {expect.value:<10} {got.value:<10} '
              f'{str(got.playable):>5} '
              f'{bar_max(cut(frame, LOBBY_BAR_ROI)):>7} '
              f'{ping_fraction(cut(frame, LOBBY_PING_ROI)):>9.3f} '
              f'{str(got_results):>9}  '
              f'{"OK " if ok else "FAIL"} {note}')

    bad += confusion()

    print(f'\n{bad} mismatch(es)' if bad else '\nall cases match')
    print('\nnot covered by any case yet:')
    for u in UNCOVERED:
        print(f'  - {u}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
