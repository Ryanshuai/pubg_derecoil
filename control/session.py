"""One gate that says the game is in a known state, for every script to open.

    from control.session import ensure_ready
    if not ensure_ready(label='the pitch probe')['ok']:
        return 1

Nothing here is new behaviour. It is `ensure_focus` + `ensure_in_match` +
`ensure_tab(False)` + `ensure_panel(False)`, in the one order that works, in
one call — because the alternative is every tool remembering four things, and
they do not. WHAT GETS FORGOTTEN IS NEVER THE FIRST ONE.

The reason it exists, in full, because the shape repeats:

tools/probe_pitch_range.py checked focus, got True, and drove three postures'
worth of state machine into the LOBBY SCREEN. Every posture came back "posture
unreadable"; the crops behind the failures were bare scenery where the HUD
should have been. Focus is not playability — the window title matches in the
lobby, on the loading screen and in the ESC menu, and control/CLAUDE.md has
said so for months. Adding the lobby check moved the failure exactly one step
along: in the match now, but no weapon, because a fresh training-range spawn is
empty-handed.

Both failures took a live run to find, and both were checks that already
existed and were simply not called. That is what this is for.

WHY EACH ONE, AND IN THIS ORDER:

  focus    everything else drives the mouse or the keyboard, and driving them
           at a window that is not the game types into whatever IS foreground.
  match    the lobby, the loading screen, the ESC menu and the results screen
           all render, all match the window title, and all swallow input.
           `playable` is the only state where a keypress reaches the game.
  Tab      the 1/2 weapon keys are SWALLOWED while the inventory is up
           (docs/game_quirks.md), so a script that opens with a weapon switch
           silently gets no weapon and no error.
  panel    the spawner panel is modal over the world: the HUD is there but the
           character does not move, so a view-driving probe measures a frozen
           screen and reports a clean zero.

The order is not arrangeable. Tab and the panel cannot be read without focus,
and neither can be read meaningfully outside a match.

WHAT IT DELIBERATELY DOES NOT DO: put a weapon in your hands, or anything else
about the loadout. That is the experiment's business, not the session's — see
control/stock.py's restock() and InventoryControl.ensure_kit(). This answers
"can the game hear me", nothing more.

Every step is skipped by argument (`match=False` and so on) for the scripts
that genuinely mean it — reading the lobby, probing the panel itself. Skipping
one to make a red run go green is how the failures above were built.
"""
from control.focus import ensure_focus


def ensure_ready(label='this script', countdown_s=6, focus=True, match=True,
                 tab=True, panel=True, verbose=True):
    """Focus, in a match, Tab down, spawner panel down. -> {'ok', 'steps', ...}

    Returns rather than raises: a probe that cannot run should say which of the
    four it could not get and stop, not stack a traceback on top of it.

    The Pointer each control object opens is CLOSED before returning, so a
    caller that goes on to build a sweep.Rig is not fighting itself for the
    one serial port this project has.
    """
    out = {'ok': False, 'steps': [], 'failed': None}

    def step(name, fn):
        try:
            got = fn()
        except Exception as e:                  # noqa: BLE001 — reported, not hidden
            got, e = False, e
            out['steps'].append({'step': name, 'ok': False, 'error': str(e)})
            if verbose:
                print(f'[ready] {name}: {e}')
            return False
        out['steps'].append({'step': name, 'ok': bool(got)})
        if verbose:
            print(f"[ready] {name}: {'ok' if got else 'FAILED'}")
        return bool(got)

    if focus and not step('focus', lambda: ensure_focus(countdown_s=countdown_s,
                                                        label=label)):
        out['failed'] = 'focus'
        return out

    if match:
        from control.lobby import LobbyControl
        with LobbyControl() as lc:
            if not step('in a match', lambda: lc.ensure_in_match()['ok']):
                out['failed'] = 'match'
                return out

    if tab:
        from control.inventory import InventoryControl
        ac = InventoryControl()
        try:
            if not step('Tab closed', lambda: ac.ensure_tab(False)):
                out['failed'] = 'tab'
                return out
        finally:
            ac.close()

    if panel:
        from control.spawner import SpawnerControl
        with SpawnerControl() as sc:
            if not step('spawner panel closed',
                        lambda: sc.ensure_panel(False)):
                out['failed'] = 'panel'
                return out

    out['ok'] = True
    return out
