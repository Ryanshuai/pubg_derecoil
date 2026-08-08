"""Offline check: nothing may end up with two guns on the rack — no game.

    pixi run one-gun

TWO GUNS ON THE SHELF IS THE FAILURE THIS PROJECT KEEPS PAYING FOR, twice on
2026-08-08 alone, and it is invisible in every number a run prints: both plates
say `mp5k`, both counters say 40, both bursts look the same, and the five
magazines of the bad cell agree with each other to 1.4%. The only tell either
time was that a cell reproduced ANOTHER cell's number.

Two independent ways in, so two ways barred, and both are checked here because
a fix with no test is what the first one had:

  spawning     control.stock.ensure_weapon_in_hand used to fall through to the
               spawner whenever a RACKED gun would not come to hand -- "I could
               not confirm it" rounded to "it is not there". It printed its own
               contradiction, `no mp5k in the rack (holds {1: 'mp5k'})`, and
               spawned a second one.
  measuring    calibration.collect_timed.read_config reads RACK SLOT 1 while
               the trigger fires whatever is in HAND. With one gun those are
               the same object by construction; with two, nothing on screen can
               say so.

The fakes here are the thinnest thing each function actually touches. That is
deliberate: a fake elaborate enough to be wrong in the same way as the real
object cannot catch anything.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from control import stock as stock_mod

FAILS = []


def check(label, got, expect):
    ok = got == expect
    if not ok:
        FAILS.append(label)
    print(f'  {"ok  " if ok else "FAIL"} {label}: {got!r}'
          f'{"" if ok else f"  expected {expect!r}"}')


class FakeTab:
    def __enter__(self):
        return True

    def __exit__(self, *a):
        return False


class FakeAC:
    """Just enough InventoryControl for ensure_weapon_in_hand.

    `hold_ok` is the whole point: a rack that reads fine and a hold that does
    not is the state the bug lived in.
    """

    def __init__(self, racked, hold_ok=True):
        self.racked = racked
        self.hold_ok = hold_ok
        self.holds = []

    def tab_up(self):
        return FakeTab()

    def loadout(self):
        if self.racked is None:
            return None
        return {'guns': dict(self.racked), 'slots': {}}

    def hold(self, slot):
        self.holds.append(slot)
        return self.hold_ok


class FakeSC:
    """A spawner that RECORDS being used. Nothing here should ever reach it in
    the cases that matter, so the assertion is on `panels` staying empty."""

    def __init__(self):
        self.panels = []

    def ensure_panel(self, on):
        self.panels.append(on)
        return False          # so the call ends immediately if it ever happens


def main():
    print('ensure_weapon_in_hand — when may it spawn?')

    # The bug, exactly: one mp5k racked, hold fails. Before the fix this
    # returned a NEW slot after spawning a second gun.
    ac = FakeAC({1: 'mp5k', 2: None}, hold_ok=False)
    sc = FakeSC()
    got = stock_mod.ensure_weapon_in_hand(ac, sc, weapon='mp5k', verbose=False)
    check('racked + hold fails -> refuse', got, None)
    check('racked + hold fails -> spawner never touched', sc.panels, [])
    check('racked + hold fails -> it did try to hold', ac.holds, [1])

    # The same shape from slot 2, because `slots` is (1, 2) and a gun in the
    # second slot is just as racked as one in the first.
    ac = FakeAC({1: None, 2: 'mp5k'}, hold_ok=False)
    sc = FakeSC()
    got = stock_mod.ensure_weapon_in_hand(ac, sc, weapon='mp5k', verbose=False)
    check('racked in slot 2 + hold fails -> refuse', got, None)
    check('racked in slot 2 + hold fails -> no spawn', sc.panels, [])

    # Two already: refused before anything is pressed.
    ac = FakeAC({1: 'mp5k', 2: 'mp5k'})
    sc = FakeSC()
    got = stock_mod.ensure_weapon_in_hand(ac, sc, weapon='mp5k', verbose=False)
    check('two mp5ks -> refuse', got, None)
    check('two mp5ks -> nothing held', ac.holds, [])

    # Unreadable rack: refuse, never spawn. Already true before today; here so
    # a later edit cannot quietly take it away.
    ac = FakeAC(None)
    sc = FakeSC()
    got = stock_mod.ensure_weapon_in_hand(ac, sc, weapon='mp5k', verbose=False)
    check('unreadable rack -> refuse', got, None)
    check('unreadable rack -> no spawn', sc.panels, [])

    # And the case that MUST still work, or the refusals above are just a
    # broken function: racked and holdable.
    # weapon_in_hand() reads the ammo counter off the screen, so it is the one
    # thing here that has to be replaced rather than faked around.
    ac = FakeAC({1: 'mp5k', 2: None})
    sc = FakeSC()
    real_wih = stock_mod.weapon_in_hand
    try:
        stock_mod.weapon_in_hand = lambda *a, **k: 40
        got = stock_mod.ensure_weapon_in_hand(ac, sc, weapon='mp5k',
                                              verbose=False)
    finally:
        stock_mod.weapon_in_hand = real_wih
    check('racked + holdable -> that slot', got, 1)
    check('racked + holdable -> no spawn', sc.panels, [])

    # An EMPTY rack still spawns. This is the one path that should reach the
    # spawner, and without it the four refusals above could be a function that
    # returns None unconditionally.
    ac = FakeAC({1: None, 2: None})
    sc = FakeSC()
    got = stock_mod.ensure_weapon_in_hand(ac, sc, weapon='mp5k', verbose=False)
    check('empty rack -> reaches the spawner', sc.panels, [True])
    check('empty rack + spawner refuses -> None', got, None)

    print()
    print('read_config — does a second gun stop the measurement?')
    from calibration import collect_timed as ct

    class LoadoutAC:
        def __init__(self, guns, slots):
            self.guns, self.slots = guns, slots

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def tab_up(self):
            return FakeTab()

        def loadout(self):
            return {'guns': self.guns, 'slots': self.slots}

    import control.inventory as inv_mod
    real = inv_mod.InventoryControl
    bare = {'scope': '', 'muzzle': '', 'grip': '', 'magazine': '', 'stock': ''}
    grip = dict(bare, grip='Lower_Foregrip_C')
    try:
        # The 2026-08-08 state, reproduced: slot 1 clean, slot 2 kitted.
        inv_mod.InventoryControl = lambda *a, **k: LoadoutAC(
            {1: 'mp5k', 2: 'mp5k'}, {1: grip, 2: dict(grip,
                                     muzzle='Muzzle_Compensator_Medium_C')})
        check('two guns -> None', ct.read_config('mp5k'), None)

        # One gun, same slot 1 contents: the reading it would have given.
        inv_mod.InventoryControl = lambda *a, **k: LoadoutAC(
            {1: 'mp5k', 2: None}, {1: grip, 2: bare})
        check('one gun -> the config', ct.read_config('mp5k'),
              {'grip': 'vert_grip'})

        # A second gun of a DIFFERENT weapon is just as unanswerable: the
        # refusal is about which one is in hand, not about which model it is.
        inv_mod.InventoryControl = lambda *a, **k: LoadoutAC(
            {1: 'mp5k', 2: 'm416'}, {1: grip, 2: bare})
        check('second gun, other weapon -> None', ct.read_config('mp5k'), None)
    finally:
        inv_mod.InventoryControl = real

    print()
    if FAILS:
        print(f'{len(FAILS)} FAILED: {FAILS}')
        return 1
    print('one gun holds')
    return 0


if __name__ == '__main__':
    sys.exit(main())
