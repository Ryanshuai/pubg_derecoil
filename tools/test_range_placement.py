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

    # ── leaving forgets ──
    run(['lobby', 'in_game'])
    L.LobbyControl._pump = fake_pump(['in_game', 'lobby'])
    L.LobbyControl.exit_to_lobby(lc)
    check('8  exit_to_lobby                      -> belief dropped',
          L.placed_at() is None, str(L.placed_at()))

    print()
    if fails:
        print(f'{len(fails)} branch(es) are not what the code claims')
        return 1
    print('9 branches, 3 of them negative — the placement table holds')
    return 0


if __name__ == '__main__':
    sys.exit(main())
