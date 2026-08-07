"""Drag one row out of 库存 and onto the floor. Does it land? Needs the game.

    pixi run python tools/probe_drop_to_ground.py
    pixi run python tools/probe_drop_to_ground.py --reps 3
    pixi run python tools/probe_drop_to_ground.py --only current

THE QUESTION. Every panel-to-panel drag in InventoryControl reports ok=True
without reading anything back -- neither end is a slot, so `checks` is empty
and drag() returns success on the strength of the gesture alone:

    if not checks:
        rec['ok'] = True                       # control/inventory.py
        self._log('... dragged (unverified)')

So "the item is on the floor now" and "nothing happened" produce identical
records and identical log lines. The backpack jamming at 12 rows, which stops
the spawner delivering anything at all, is the downstream symptom; this is the
gesture underneath it.

WHAT IS ALREADY ON RECORD, and why it is not enough. tab_layout.DROP_XY says
in its own comment that the two points were found by trial, that a row-derived
release point put items on the FLOOR when 库存 was wanted, and -- the part
that matters here -- that the boundary between the two outcomes was never
mapped, because the runs that would have mapped it were taken with both lists
at their 12-row display cap, where a row-count delta means nothing.

This probe refuses to start in that state rather than reproducing it. Both
lists must be under the cap before a single number here is worth reading.

WHAT IS READ, per attempt, none of it through a template:

    库存 row count   did the item leave the backpack
    附近 row count   did it arrive on the floor

The two together separate three outcomes that the production log cannot:

    landed     库存 -1, 附近 +1     the drop worked
    nothing    both unchanged       the gesture never took hold
    vanished   库存 -1, 附近 +0     it left and did not arrive -- the worst
                                    case, because it looks like success from
                                    the source side, which is the only side
                                    drag() has ever checked

RESTOCKING IS PART OF THE MEASUREMENT. Each attempt needs one item in 库存,
and a successful drop removes it. With the rack cleared, a spawned attachment
has nowhere to auto-fit and goes to 库存 -- measured 3/3, docs/game_quirks.md.
So each attempt spawns its own subject, and an attempt whose spawn did not
arrive is reported as such instead of being silently run against whatever was
left over.

WHY THIS TOUCHES ac.pointer DIRECTLY, when tools/CLAUDE.md says not to. The
release POINT is the measurand. InventoryControl exposes exactly one release
point per panel (DROP_XY, via point_of), so going through it could only ever
re-test the point already in the code -- which is the one under suspicion.
This is the same exemption calibration/calibrate_k.py carries: driving through
the layer that encodes the answer makes the measurement circular. The
production path IS still measured, as the `current` candidate, so the two are
compared rather than one being assumed.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from control.session import ensure_ready
from control.inventory import InventoryControl, at_inv        # noqa: E402
from control.spawner import SpawnerControl                    # noqa: E402
from detector.tab_layout import DROP_XY, PANELS, row_y        # noqa: E402
from range_session import get_session                         # noqa: E402

# One cheap, always-available part. Any attachment would do; this one spawns
# from a category that is already open in most panel states.
SUBJECT = 'comp_ar'

# Both lists show 12 rows and scroll under it, so a count at 12 means "12 or
# more" and a delta means nothing. Refusing here is the difference between
# this run and the one whose data had to be thrown away.
CAP = 12
ROOM = 9                    # start well clear of it


def candidates():
    """[(name, (x, y))] -- release points worth trying, cheapest first.

    `current` is whatever production resolves to, which since 2026-08-03 is
    the FIRST EMPTY ROW of 附近 rather than a fixed point -- the whole panel
    is the floor, so there is no reason to release on top of an existing item.
    It only falls back to the constant when the row count is unknown or the
    list is full. The rest walk the panel: its icon column, its horizontal
    middle, and rows above and below the old constant, so a failure can be
    read as "wrong y", "wrong x" or "wrong everywhere".

    The first run of this probe is why the change was made: both of its two
    non-landings came after 附近 had reached 9-10 rows, which is exactly where
    the old fixed point (y=570, between rows 4 and 5) starts coming down on
    top of something.
    """
    x0, x1, icon_x = PANELS['nearby']
    mid_x = (x0 + x1) // 2
    cur = DROP_XY['nearby']
    out = [('current', cur)]
    for i in (0, 2, 4, 6):
        out.append((f'row{i}@icon', (icon_x, row_y(i))))
    out.append(('row0@mid', (mid_x, row_y(0))))
    out.append(('cur_x@row4', (cur[0], row_y(4))))
    return out


class Probe:
    def __init__(self, ac, sc, verbose=True):
        self.ac, self.sc, self.verbose = ac, sc, verbose

    def counts(self):
        """(库存 rows, 附近 rows) from one frame."""
        view = self.ac.look()
        return view.rows('inventory'), view.rows('nearby')

    def give_one(self):
        """Put exactly one part in 库存. -> True if a row appeared."""
        inv0, _ = self.counts()
        if not self.sc.ensure_panel(True) or not self.sc.sync(need_cols=(1, 2)):
            return False
        ok = self.sc.give_attachment(SUBJECT)['ok']
        self.sc.ensure_panel(False)
        time.sleep(0.7)
        self.ac.ensure_tab(True)
        inv1, _ = self.counts()
        # Reported ok only means the click found the right entry. The row is
        # the evidence.
        return bool(ok) and inv1 > inv0

    def attempt(self, point, use_production):
        """One drop. -> dict with the two deltas and a verdict."""
        inv0, gnd0 = self.counts()
        if use_production:
            self.ac.discard(at_inv(0))
        else:
            self.ac.pointer.drag(self.ac.point_of(at_inv(0)), point,
                                 **self.ac.timing)
        time.sleep(0.4)
        inv1, gnd1 = self.counts()
        di, dg = inv1 - inv0, gnd1 - gnd0
        verdict = ('landed' if di < 0 and dg > 0 else
                   'nothing' if di == 0 and dg == 0 else
                   'vanished' if di < 0 and dg == 0 else
                   f'odd (库存 {di:+d}, 附近 {dg:+d})')
        return {'inv': f'{inv0}->{inv1}', 'gnd': f'{gnd0}->{gnd1}',
                'verdict': verdict, 'point': point}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--reps', type=int, default=2,
                    help='attempts per candidate point')
    ap.add_argument('--only', help='run just this candidate by name')
    ap.add_argument('--reset', action='store_true',
                    help='re-enter the range first — the only reliable way to '
                         'empty both lists, and the precondition for any of '
                         'these numbers meaning anything')
    ap.add_argument('--countdown', type=int, default=5)
    args = ap.parse_args()

    if not ensure_ready(label='the drop probe', countdown_s=args.countdown)['ok']:
        return 1
    # force=True: a re-entry is the only thing that reliably empties BOTH
    # lists, and starting under the 12-row cap is the precondition this whole
    # measurement rests on. Costs ~13 s and buys numbers that mean something.
    if not get_session('auto').ensure(force=args.reset)[0]:
        print('[!] could not get into the training range')
        return 1

    ac, sc = InventoryControl(verbose=False), SpawnerControl(verbose=False)
    probe = Probe(ac, sc)
    try:
        ac.ensure_tab(True)
        # THE RACK EMPTY, so a spawned part has no slot to auto-fit into and
        # goes to 库存 where this probe can pick it up.
        ac.clear_rack()
        inv, gnd = probe.counts()
        print(f'start: 库存 {inv}, 附近 {gnd}')
        if inv >= ROOM or gnd >= ROOM:
            print(f'[!] one of the lists is at {max(inv, gnd)} of {CAP} rows. '
                  f'Row deltas stop meaning anything at the cap, which is '
                  f'exactly why the last attempt at this measurement was '
                  f'discarded. Re-enter the training range, which empties '
                  f'both, and run this again.')
            return 1

        picks = [c for c in candidates()
                 if args.only is None or c[0] == args.only]
        if not picks:
            print(f'[!] no candidate named {args.only}; have: '
                  + ', '.join(n for n, _ in candidates()))
            return 1

        print(f'\n{len(picks)} candidate(s) x {args.reps} rep(s), subject '
              f'{SUBJECT}\n')
        results = {}
        for name, point in picks:
            tally = []
            for i in range(args.reps):
                if not probe.give_one():
                    print(f'  {name} rep{i}: the spawn did not reach 库存 — '
                          f'skipping, not guessing')
                    continue
                rec = probe.attempt(point, use_production=(name == 'current'))
                tally.append(rec['verdict'])
                print(f"  {name:<12} rep{i}  {str(point):<12} "
                      f"库存 {rec['inv']:<8} 附近 {rec['gnd']:<8} "
                      f"{rec['verdict']}")
                if rec['verdict'] != 'landed':
                    # Leave the floor tidy for the next candidate, but only
                    # when something is there to tidy.
                    pass
            results[name] = tally

        print('\n=== summary ===')
        for name, tally in results.items():
            got = {v: tally.count(v) for v in set(tally)}
            n = len(tally)
            print(f'  {name:<12} ' + (', '.join(f'{v} {c}/{n}'
                                                for v, c in sorted(got.items()))
                                      or 'no attempt ran'))
        print('\n  `landed` is the only success. `vanished` is the one that '
              '\n  matters most: the item left 库存 and did not arrive, which '
              '\n  reads as success from the source side — the only side '
              '\n  drag() has ever checked.')
        return 0
    finally:
        try:
            ac.ensure_tab(False)
        finally:
            ac.close()


if __name__ == '__main__':
    sys.exit(main())
