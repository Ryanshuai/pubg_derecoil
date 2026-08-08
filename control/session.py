"""One gate that says the game is in a known state, for every script to open.

    from control.session import ensure_ready
    if not ensure_ready(label='the pitch probe')['ok']:
        return 1

Nothing here is new behaviour. It is `ensure_running` + `ensure_focus` +
`ensure_in_match` + `ensure_tab(False)` + `ensure_panel(False)`, in the one
order that works, in one call — because the alternative is every tool
remembering five things, and they do not. WHAT GETS FORGOTTEN IS NEVER THE
FIRST ONE.

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

⚠ **THERE USED TO BE A FIFTH LEG, and moving it out is the one real change
this file has had.** Standing on the 200m lane rather than the spawn compound
is not about being heard — it answers "will anything hit me" — and it was put
here on the argument that fits the other four: one door, one precaution nobody
has to remember.

That argument was wrong about a fact. THIS IS NOT THE ONLY DOOR BACK INTO A
MATCH. calibration.range_session.AutoSession.enter() walked back in through
LobbyControl, and so does a human alt-tabbing to the game; both left the
process-local "already on the lane" flag standing, so the next ensure_ready
skipped a teleport that had never happened. A harvest evicted at 17 minutes
fired the whole back half of a 45-minute run in the middle of the compound,
and every gate stayed green. The repair at the time was a public
`forget_range()` for those callers to call — the same bug with a manual step
in front of it.

It lives in `LobbyControl.ensure_in_match` now, next to the transition that
moves the character. Nothing has to be remembered, because the module that
moves it is the module that knows. `range_name` below is a pass-through to
that call, kept here only so a caller can turn it OFF.

WHAT IT DELIBERATELY DOES NOT DO: put a weapon in your hands, or anything else
about the loadout. That is the experiment's business, not the session's — see
control/stock.py's restock() and InventoryControl.ensure_kit().

Every step is skipped by argument (`match=False` and so on) for the scripts
that genuinely mean it — reading the lobby, probing the panel itself. Skipping
one to make a red run go green is how the failures above were built.
"""
from control.focus import ensure_focus


def _say_what_is_actually_up(out, verbose):
    """On a failed leg, read the screen once and record what it says.

    ⚠ A LEG'S NAME IS NOT A DIAGNOSIS. Twice on 2026-08-07 the failure named
    the leg that noticed rather than the thing that was wrong:

      the game was not running     -> "failed at 'focus'", and eight seconds of
                                      "switch to the game" for a window that
                                      did not exist
      the client dropped its match -> "failed at 'range'. Is PUBG running and
                                      in the training range?" -- while a modal
                                      reconnect dialog sat on the screen, which
                                      LobbyControl knows by name and can clear

    The first was fixed by adding a leg in front. That does not generalise:
    ANY leg can fail because of something a later leg owns. What does
    generalise is asking the screen at the moment of failure, which is cheap
    (one classify) and only happens when something already went wrong.

    Failures are swallowed on purpose -- this runs on the error path, and a
    diagnostic that can raise turns a bad message into no message at all.
    """
    try:
        from control.lobby import LobbyControl
        with LobbyControl(verbose=False) as lc:
            st = lc.state()
        out['state_at_failure'] = str(getattr(st, 'state', st))
        if verbose:
            print(f"[ready] what the screen says right now: "
                  f"{out['state_at_failure']}"
                  + ('' if getattr(st, 'playable', False)
                     else '  <- not playable, so the leg above was reporting a '
                          'symptom. LobbyControl.ensure_in_match() clears the '
                          'modal and error states by name.'))
    except Exception:                       # noqa: BLE001 — see the docstring
        pass


def ensure_ready(label='this script', countdown_s=6, running=True, focus=True,
                 match=True, tab=True, panel=True, range_name='200m',
                 verbose=True, match_timeout=None, launch_timeout=None):
    """L2 — Running, focused, in a match, Tab down, panel down. -> {'ok',
    'steps', ...}. The preconditions no script remembers; each leg is its own
    L1 (ensure_running / ensure_focus / ensure_in_match / ensure_tab /
    ensure_panel).

    The 200m lane comes with the match leg — `range_name` is handed to
    `ensure_in_match`, which teleports when IT put the character at a spawn.
    Pass None to skip it. `rec['range']` is copied out to `out['range']` so a
    caller reads the teleport's own record here rather than digging.

    Returns rather than raises: a probe that cannot run should say which of the
    legs it could not get and stop, not stack a traceback on top of it.

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
        if not got:
            _say_what_is_actually_up(out, verbose)
        return bool(got)

    # ⚠ BEFORE FOCUS, because focus cannot answer the question focus fails on.
    # 2026-08-07: the game was not running, ensure_ready reported
    # `failed at 'focus'` and told the operator to "switch to the game within
    # 8s" -- eight seconds of counting down for a window that did not exist.
    # The message names the leg that noticed, and the leg that noticed is one
    # past the leg that was wrong.
    #
    # It is cheap when the game is up: ensure_running's first poll reads the
    # state and returns, no launch and no click, which is what makes it safe
    # to put in front of everything. game_hwnd() is 1.4 ms; the process table
    # (18.7 ms) is only consulted when the window answers None.
    #
    # Asked for in those terms after watching the above: "LobbyControl
    # .ensure_running() 自动加到脚本里嘛."
    if running:
        from control.lobby import LobbyControl

        def _running():
            with LobbyControl(verbose=verbose) as lc:
                kw = {'timeout': launch_timeout} if launch_timeout else {}
                rec = lc.ensure_running(**kw)
                if not rec['ok']:
                    raise RuntimeError(
                        f"{rec.get('error') or 'could not get a game screen'} "
                        f"(states {rec.get('states')})")
                return True

        if not step('running', _running):
            out['failed'] = 'running'
            return out

    if focus and not step('focus', lambda: ensure_focus(countdown_s=countdown_s,
                                                        label=label)):
        out['failed'] = 'focus'
        return out

    if match:
        from control.lobby import LobbyControl
        with LobbyControl() as lc:
            # ⚠ `match_timeout` FORWARDS THE CALLER'S BUDGET, and it exists
            # because dropping it was a regression introduced on 2026-08-07 by
            # the change that routed range re-entry through here. AutoSession
            # .enter(timeout_s=300) had been passing that straight to
            # ensure_in_match; after the reroute the argument was still in the
            # signature, still documented, and read by nothing -- so a
            # re-entry silently got ENTER_TIMEOUT instead. Caught by scanning
            # for parameters no body reads, which is the same scan that found
            # spawner.expand's `retries`.
            kw = {} if match_timeout is None else {'timeout': match_timeout}
            rec = {}

            def _match():
                rec.update(lc.ensure_in_match(range_name=range_name, **kw))
                return rec['ok']

            # The name says both halves, because a failure here is now two
            # different failures and 'match: FAILED' would not say which.
            leg = 'in a match' if not range_name else \
                  f'in a match, on the {range_name} lane'
            if not step(leg, _match):
                out['failed'] = 'range' if rec.get('range') else 'match'
                out['range'] = rec.get('range')
                return out
            if 'range' in rec:
                out['range'] = rec['range']

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
