"""Offline gate on ONE decision: when does entering a match teleport?

    pixi run placement

`LobbyControl.ensure_in_match` places the character on the practice lane, and
which of four branches it takes is the whole correctness of that feature. This
drives nothing — `_pump` and `MapControl` are replaced — so it asks only
whether the branch is the one claimed.

⚠ FOUR OF THE NINE CASES MUST NOT TELEPORT, and they are the reason this file
exists. A gate that only checks "it teleported when it should" passes just as
well when the answer is always yes, and always-yes costs a map open, a read
and a map close on every ensure_ready — which is once per weapon, and is
exactly what the operator asked to stop paying on 2026-08-06 ("不用每次都进那个
M 点那个 Range 二百，只有第一次进游戏的时候需要").

⚠ THE RULE GOT SIMPLER ON 2026-08-08, AND THIS FILE IS WHERE THE CHANGE IS
VISIBLE. It used to be two clauses — walked in, OR this process has not placed
anyone yet — with a module-level flag behind the second. It is now one clause:

    teleport  ⟺  THIS CALL walked into the match

Stated by the operator as「每次进训练场的时候做一次那个地图的那个切换就行了，
其他过程中不需要切换」. So the case that flipped is 4 (already in, fresh
process): it used to teleport and must now SKIP, and it is checked below in
that direction. Case 3 is unchanged and is the one that must never regress —
a call that WALKED IN re-places unconditionally, because entering resets the
world and the character is at the spawn no matter what anyone believes. That
is the case the predecessor of this code got wrong, and it cost the back half
of a 45-minute harvest (see control/CLAUDE.md).
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

    # ── it teleports: ONLY on the entry event, and always on it ──
    r = run(['lobby', 'fullbleed', 'in_game'])
    check('1  walked in via loading              -> teleport',
          len(CALLS) == 1, f'{CALLS} {r}')

    r = run(['lobby', 'in_game'])
    check('3  walked in, right after another     -> teleport anyway',
          len(CALLS) == 1, f'{CALLS}')

    # ⚠ 3b IS CASE 3 WITH THE ONLY STATE THAT COULD BLUR IT. A modal cleared
    # over a running match must NOT read as an entry: states[0] is in_game
    # there, which is why the code reads states[0] and not `actions > 0`.
    r = run(['in_game', 'menu', 'in_game'])
    check('3b modal cleared over a live match    -> SKIP',
          not CALLS and r.get('range', {}).get('skipped'), f'{CALLS} {r}')

    # ── it does NOT teleport: the half a one-sided gate would miss ──
    #
    # ⚠ CASE 4 IS THE ONE THAT FLIPPED (2026-08-08). It teleported until the
    # rule became "bound to the entry event and nothing else". A fresh process
    # attaching to a running match now leaves the character where it is —
    # being in the training range IS being placed. Reverting the rule to the
    # old two-clause form turns this red, which is the point of keeping it.
    r = run(['in_game'])
    check('4  already in, fresh process          -> SKIP',
          not CALLS and r.get('range', {}).get('skipped'), f'{CALLS} {r}')

    r = run(['in_game'])           # ...and again: no state accumulates
    check('2  already in, second call            -> SKIP',
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
    # itself in the trace. So ok=False rather than a warning.
    r = run(['lobby', 'in_game'], teleport_ok=False)
    check('5  teleport failed                    -> the call fails',
          r['ok'] is False and bool(r['error']), r)
    check('5b the match-settle retry fired',
          len(CALLS) == 2, f'{CALLS}')

    # ⚠ TWO CASES WERE REMOVED HERE ON 2026-08-08, and neither was deleted for
    # going green. They lost their referent:
    #
    #   5c  "already in, teleport failed -> retry fires ANYWAY" asked whether
    #       the retry was gated on `entered`. The `already in` path does not
    #       teleport at all now, so there is no attempt to retry. The retry
    #       being unconditional is still checked, by 5b, on the path that has
    #       one.
    #   8   "exit_to_lobby -> belief dropped" checked that leaving a match
    #       cleared the module-level placement flag. THERE IS NO FLAG. Nothing
    #       carries across calls for a transition to invalidate, which is the
    #       property that replaced it rather than a check that was dropped —
    #       case 4 above is what proves it.

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
    print('11 branches, 6 of them negative — the placement table holds')
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
