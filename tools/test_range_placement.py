"""Offline gate on ONE decision: when does entering a match teleport?

    pixi run placement

`LobbyControl.ensure_in_match` places the character on the practice lane, and
which of four branches it takes is the whole correctness of that feature. This
drives nothing — `_pump` and `MapControl` are replaced — so it asks only
whether the branch is the one claimed.

⚠ THREE OF THE NINE CASES MUST NOT TELEPORT, and they are the reason this file
exists. A gate that only checks "it teleported when it should" passes just as
well when the answer is always yes, and always-yes costs a map open, a read
and a map close on every ensure_ready — which is once per weapon, and is
exactly what the operator asked to stop paying on 2026-08-06 ("不用每次都进那个
M 点那个 Range 二百，只有第一次进游戏的时候需要").

The other half is why the belief is not simply "have I placed anyone yet":
case 3. A call that WALKED IN re-places unconditionally, because entering
resets the world and the character is at the spawn no matter what any flag
says. That is the case the predecessor of this code got wrong, and it cost the
back half of a 45-minute harvest — see control/CLAUDE.md.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import control.lobby as L                                    # noqa: E402
import control.map as M                                      # noqa: E402

# Captured before main() swaps M.MapControl for FakeMap. The branch table
# needs the fake; the click cases at the bottom need the real thing.
_REAL_MAPCONTROL = M.MapControl

CALLS = []


class FakeMap:
    """Records that a teleport was asked for. `ok` is what the game said."""
    ok = True

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def goto_range(self, name):
        CALLS.append(name)
        return {'ok': FakeMap.ok, 'player': (1977, 450), 'steps': [],
                'elapsed': 1.0,
                'error': None if FakeMap.ok
                else 'teleport to 200m: 4 attempts had no effect'}


def fake_pump(states, ok=True):
    """A _pump that arrives (or does not) having passed through `states`.

    ⚠ `states` IS THE INPUT UNDER TEST. ensure_in_match reads states[0] to
    decide whether THIS call walked in, so a fake that returned a fixed
    sequence would be testing nothing.
    """
    def _p(self, target, timeout, act, tag, **kw):
        return {'ok': ok, 'elapsed': 1.0, 'states': list(states), 'state': None,
                'retries': 0, 'actions': 0, 'error': None if ok else 'stuck'}
    return _p


def main():
    M.MapControl = FakeMap
    # No Driver.__init__: that opens the Pointer, and this test must not touch
    # the serial port other agents share.
    lc = L.LobbyControl.__new__(L.LobbyControl)
    lc.verbose = False
    fails = []

    def run(states, ok=True, teleport_ok=True, range_name='200m'):
        CALLS.clear()
        FakeMap.ok = teleport_ok
        L.LobbyControl._pump = fake_pump(states, ok)
        return L.LobbyControl.ensure_in_match(lc, range_name=range_name)

    def check(label, cond, detail=''):
        print(f'  {"ok  " if cond else "FAIL"}  {label}'
              + (f'   {detail}' if detail and not cond else ''))
        if not cond:
            fails.append(label)

    # ── it teleports ──
    L.forget_placement()
    r = run(['lobby', 'fullbleed', 'in_game'])
    check('1  walked in, nothing believed        -> teleport',
          len(CALLS) == 1 and L.placed_at() == '200m', f'{CALLS} {r}')

    r = run(['lobby', 'in_game'])
    check('3  walked in, ALREADY believed        -> teleport anyway',
          len(CALLS) == 1, f'{CALLS}')

    L.forget_placement()
    r = run(['in_game'])
    check('4  already in, fresh process          -> teleport',
          len(CALLS) == 1, f'{CALLS}')

    # ── it does NOT teleport: the half a one-sided gate would miss ──
    r = run(['in_game'])           # placed by case 4 above, and nobody moved
    check('2  already in, believed               -> SKIP',
          not CALLS and r.get('range', {}).get('skipped'), f'{CALLS} {r}')

    r = run(['lobby'], ok=False)
    check('6  never got into a match             -> map untouched',
          not CALLS and 'range' not in r, f'{CALLS} {r}')

    r = run(['lobby', 'in_game'], range_name=None)
    check('9  range_name=None                    -> map untouched',
          not CALLS and 'range' not in r, f'{CALLS} {r}')

    # ── a teleport that does not land fails the whole call ──
    #
    # ⚠ THE SEVERITY IS THE POINT. Being in a match with the character in the
    # compound is WORSE than not being in a match: every gate downstream
    # passes and the magazines are fired in traffic, which does not announce
    # itself in the trace. So ok=False, and the belief is cleared rather than
    # left naming a lane nobody reached.
    r = run(['lobby', 'in_game'], teleport_ok=False)
    check('5  teleport failed                    -> the call fails',
          r['ok'] is False and bool(r['error']) and L.placed_at() is None, r)
    check('5b the match-settle retry fired',
          len(CALLS) == 2, f'{CALLS}')

    # ⚠ AND ON THE `already in` PATH TOO, which is what this used to skip. The
    # retry was gated on `entered`, reasoning from the CAUSE it was written for
    # (a match too fresh to take input) rather than from the symptom. A process
    # that found the game already in a match hit the same map_open false
    # positive on 2026-08-08, got no retry, and failed hard twice in a row --
    # while the run after it succeeded on its first try.
    L.forget_placement()
    r = run(['in_game'], teleport_ok=False)
    check('5c already in, teleport failed        -> retry fires ANYWAY',
          len(CALLS) == 2, f'{CALLS}')

    # ── leaving forgets ──
    run(['lobby', 'in_game'])
    L.LobbyControl._pump = fake_pump(['in_game', 'lobby'])
    L.LobbyControl.exit_to_lobby(lc)
    check('8  exit_to_lobby                      -> belief dropped',
          L.placed_at() is None, str(L.placed_at()))

    # ── and MapControl itself: WHEN MAY IT CLICK? ──
    #
    # Everything above replaces MapControl wholesale, so nothing up here can
    # see the one decision that costs ammunition. `map_open` can answer True on
    # a screen with no map on it -- the left panel's yellow selection border --
    # and a click aimed at the 200m box then lands in the WORLD, where a left
    # click fires the weapon. Measured 2026-08-08: two runs, eight clicks, the
    # counter went 40 -> 32 on the gun under test.
    #
    # The disagreement was already being logged ("player is at None") and
    # already documented in ensure_map's docstring. It clicked anyway.
    print()
    print('MapControl.goto_range — when may it click?')
    fails += _map_click_cases()

    print()
    if fails:
        print(f'{len(fails)} branch(es) are not what the code claims')
        return 1
    print('12 branches, 5 of them negative — the placement table holds')
    return 0


def _map_click_cases():
    """goto_range with the SCREEN faked, not MapControl. -> [failed labels]"""
    # ⚠ THE REAL CLASS, not M.MapControl -- main() replaced that name with
    # FakeMap for the branch table above, and reading it here would test the
    # fake against itself. _REAL_MAPCONTROL is captured at import, before any
    # patching, for exactly this.
    import control.map as MC
    real_cls = _REAL_MAPCONTROL
    bad = []

    def check(label, cond, detail=''):
        print(f'  {"ok  " if cond else "FAIL"}  {label}'
              + (f'   {detail}' if detail and not cond else ''))
        if not cond:
            bad.append(label)

    class FakePointer:
        def __init__(self):
            self.clicks = []

        def click_at(self, x, y):
            self.clicks.append((x, y))

        class pico:
            @staticmethod
            def key(*a, **k):
                pass

    def build(marker, at_range=False):
        """A MapControl whose screen says map_open=True and player_xy=marker."""
        mc = real_cls.__new__(real_cls)
        mc.verbose = False
        # `pointer` is a PROPERTY whose getter opens the serial port other
        # agents share. Setting `_pointer` is what keeps this test off the
        # Pico -- see Driver.pointer on why it is lazy in the first place.
        mc._pointer = FakePointer()
        mc.det = type('D', (), {
            'state': lambda self: type('S', (), {'playable': True,
                                                 'value': 'in_game'})(),
            'close': lambda self: None})()
        return mc

    real = (MC.capture_screen, MC.map_open, MC.player_xy, MC.at_range_xy,
            MC.move_cursor, MC.focus_keeper, MC.MAP_SETTLE, MC.MAP_RETRY_AFTER)
    try:
        MC.capture_screen = lambda *a, **k: 'FRAME'
        MC.map_open = lambda f: True          # the lie under test
        MC.move_cursor = lambda *a, **k: None
        MC.focus_keeper = lambda: type('F', (), {'ok': lambda s, t: True})()
        MC.MAP_SETTLE = 0.0
        MC.MAP_RETRY_AFTER = 0.0

        # 10. The false positive: map "open", no player marker anywhere.
        MC.player_xy = lambda f: None
        MC.at_range_xy = lambda w, n: False
        mc = build(None)
        r = real_cls.goto_range(mc, '200m', timeout=0.2)
        check('10 map_open lies, no marker        -> NO click',
              mc.pointer.clicks == [], f'clicked {mc.pointer.clicks}')
        check('11 ...and it reports the refusal', not r['ok'] and
              'marker' in (r['error'] or ''), r.get('error'))

        # 12. The real thing still works: marker present, not at the range.
        seen = {'n': 0}

        def _xy(f):
            seen['n'] += 1
            return (1900, 1000)
        MC.player_xy = _xy
        MC.at_range_xy = lambda w, n: seen['n'] > 2   # arrives after a click
        mc = build((1900, 1000))
        r = real_cls.goto_range(mc, '200m', timeout=1.0)
        check('12 marker present, not at range    -> it DOES click',
              len(mc.pointer.clicks) >= 1, f'clicked {mc.pointer.clicks}')
    finally:
        (MC.capture_screen, MC.map_open, MC.player_xy, MC.at_range_xy,
         MC.move_cursor, MC.focus_keeper, MC.MAP_SETTLE,
         MC.MAP_RETRY_AFTER) = real
    return bad


if __name__ == '__main__':
    sys.exit(main())
