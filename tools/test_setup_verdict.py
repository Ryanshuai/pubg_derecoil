"""Why a weapon failed to set up — both sides of the gate.

    pixi run setup-verdict

WHAT THIS GUARDS. On 2026-08-06 a vss run printed three true sentences and
one false diagnosis:

    [stock] spawned 1 in 2 clicks: vss
    [!] vss is not in the rack, and both slots are empty — the spawn did not land.
    [!] posture unreadable (want standing)

The screen said 「因长时间没有动作, 您已被踢出游戏」. The spawner was fine,
the rack reader was fine, the posture detector was fine; the character was not
in the game. `harvest.setup_verdict` is the branch that now tells those apart,
and this is its offline check.

TWO-SIDED, deliberately. The failure this gate is named after wants it to
RETRY; the failure next to it wants it to REFUSE, because retrying a genuine
spawner failure costs another 20 s of panel clicks and then reports an
eviction that never happened. A test that only proved the retry fires would
pass on a gate that retries everything.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from control.kitting import SETUP_SAYS, SETUP_TRIES, setup_verdict

# (in_range, attempt, expected, why this case exists)
CASES = [
    (False, 0, 'retry',
     'evicted, budget left — the 2026-08-06 vss failure, the whole point'),
    (False, SETUP_TRIES - 1, 'exhausted',
     'evicted and out of attempts — stop, do not loop on the lobby'),
    (True, 0, 'spawner',
     'IN the range: the spawner or the readback really did fail. Refusing '
     'here is the other side of the gate'),
    (True, SETUP_TRIES - 1, 'spawner',
     'still in the range on the last attempt — the reason does not become '
     'an eviction just because the budget ran out'),
    (None, 0, 'no-session',
     'no session to ask; must not claim either cause'),
    (None, SETUP_TRIES - 1, 'no-session',
     'same with no budget left — still must not claim a cause'),
]


def main():
    bad = 0
    for in_range, attempt, want, why in CASES:
        got = setup_verdict(in_range, attempt)
        ok = got == want
        bad += not ok
        print(f"  {'ok ' if ok else 'FAIL'}  in_range={str(in_range):5s} "
              f"attempt={attempt}  -> {got:11s} (want {want})\n"
              f"        {why}")

    # A verdict with no sentence behind it reaches the operator as a KeyError
    # in the middle of a run, which is how a diagnostic becomes a second fault.
    for name in {v for _, _, v, _ in CASES}:
        if name not in SETUP_SAYS:
            print(f"  FAIL  verdict {name!r} has no message in SETUP_SAYS")
            bad += 1
    # And every message has to survive the formatting the caller does.
    for name, text in SETUP_SAYS.items():
        try:
            text.format(weapon='vss', n=SETUP_TRIES)
        except (KeyError, IndexError) as e:
            print(f"  FAIL  SETUP_SAYS[{name!r}] will not format: {e}")
            bad += 1

    # ⚠ Only 'retry' may retry. This is the assertion that would have caught a
    # gate written as "attempt + 1 < tries -> retry", which passes every
    # positive case above and silently retries spawner failures too.
    retrying = {v for _, _, v, _ in CASES if v == 'retry'}
    for in_range in (True, None):
        if setup_verdict(in_range, 0) in retrying:
            print(f"  FAIL  in_range={in_range} must not lead to a retry")
            bad += 1

    print(f"\n{len(CASES)} cases, {bad} bad")
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
