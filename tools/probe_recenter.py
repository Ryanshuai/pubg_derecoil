"""Does the view actually come back between magazines?

    pixi run python tools/probe_recenter.py --weapon m416 --mags 3

The compensation is wrong by exactly the residual being measured, so every
burst walks the view a few hundred counts. If that walk is not undone, it
accumulates in one direction until the view hits PUBG's pitch clamp — and at
the clamp the view stops moving, so the next magazine measures near-zero
recoil and reports nothing wrong. Silently corrupted cells are worse than
failed ones, and the whole campaign sits downstream of this.

Open-loop recentring did not come back. Magazine after magazine the harvest
log read "residual +197, recentred +66", and the leftover accumulated. Two
causes, neither visible without a check: the recording's drift figure stops
while PUBG is still pulling the view back, and nothing measured the result.

This prints, per magazine, where the view ended up relative to the reference
taken before the first round. That column is the answer: it should stay flat
near zero, not walk.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))), 'calibration'))

from press.pointer import ensure_focus
from detector.weapon import Weapon
from sweep import Rig, analyse
from harvest import Panel
from spawner_control import SpawnerControl


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--weapon', default='m416')
    ap.add_argument('--mags', type=int, default=3)
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--spawn', action='store_true',
                    help='spawn the weapon first (needs the item spawner)')
    ap.add_argument('--home', action='store_true',
                    help='re-home against the pitch clamp and rise to the '
                         'middle of the travel before every magazine, instead '
                         'of returning to a remembered reference')
    args = ap.parse_args()

    if not ensure_focus(countdown_s=5, label='the recentre probe'):
        print('[!] could not focus the game')
        return 1
    time.sleep(0.6)

    rig = Rig(args.sight)
    panel = Panel(rig.mouse)
    try:
        if args.spawn:
            sc = SpawnerControl(verbose=False)
            if panel.ensure_open() and sc.sync(need_cols=(1,)):
                sc.give_weapon(args.weapon)
            panel.ensure_closed()
            time.sleep(0.6)

        w = Weapon()
        w.set('name', args.weapon)
        w.set('posture', 'standing')
        w.set_seq()
        if not len(w.t_s):
            print(f'[!] no curve for {args.weapon}')
            return 1
        rig.mouse.upload_pattern(w.dx_s, w.dy_s, w.t_s, w.bullet_interval_s)
        rig.mouse.set_recoil_enabled(True)
        time.sleep(0.3)

        if not rig.ensure_ads():
            print('[!] could not enter ADS')
            return 1
        rig.flush(6)
        if args.home:
            rig.calibrate_pitch()
            rig.goto_pitch_centre()
        rig.set_reference()
        print(f'reference taken; the correlator responds to a test move: '
              f'{rig.tracking_confirmed()}')

        print(f'\n{"mag":>4}{"rounds":>8}{"residual":>10}{"drift":>8}'
              f'{"recentred":>11}{"OFFSET":>9}   note')
        print('-' * 62)
        for i in range(args.mags):
            back = 0
            if i:
                if not rig.ensure_ads():
                    print('  [!] could not re-enter ADS after the reload')
                    break
                if args.home:
                    # Homing lands somewhere the old reference cannot
                    # describe, so the reference is retaken there.
                    back = rig.goto_pitch_centre()
                    rig.set_reference()
                else:
                    back = rig.recenter()
            rec, fire_s, steps, fire_end = rig.fire_magazine()
            if steps == 0:
                print(f'{i:>4}{"-":>8}   no rounds fired (still reloading?)')
                time.sleep(1.5)
                continue
            a = analyse(rec.finish(), rig.K, w.bullet_interval_s, fire_end)
            if a is None:
                continue
            rig.pending_pitch += a['view_drift_counts']
            off = rig.absolute_offset()
            print(f'{i:>4}{steps:>8}{a["cum_counts"]:>+10.0f}'
                  f'{a["view_drift_counts"]:>+8.0f}{back:>+11d}'
                  f'{"  n/a" if off is None else f"{off:>+9.1f}"}'
                  f'   {"LOST" if rig.tracking_lost else ""}')
            if rig.tracking_lost:
                print('  [!] the view position is no longer known — stopping')
                break
            rig.wait_reload()

        print('\nOFFSET is where the view sits relative to the reference, '
              'measured\nagainst it rather than accumulated. Flat near zero '
              'is the pass;\na column that walks in one direction is the bug '
              'this probe exists for.')
    except KeyboardInterrupt:
        print('\ninterrupted')
    finally:
        panel.close_grabber()
        rig.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
