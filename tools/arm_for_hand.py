"""Fit one cell, upload it, arm it, and leave it there for a HUMAN to hold.

    pixi run python tools/arm_for_hand.py --weapon mp5k
    pixi run python tools/arm_for_hand.py --weapon mp5k --config grip-vert_grip

Everything else in calibration/ arms the curve, fires it under a script and
disarms on the way out, so the Pico ends every run empty. This leaves it
loaded, because "what does the compensation feel like" is not a question a
measurement answers -- the operator asked to hold the trigger themselves, and
the firmware has to still have the curve in it when they do.

⚠ IT IS ONE CELL'S CURVE, NOT A WEAPON'S. Under MODEL.md plan A a curve is
fitted for an exact (weapon, config, posture, sight) and there is no
interpolation between them: firing it on a gun wearing something else, or
through a different optic, plays a curve for a gun that is not in your hands.
The four conditions are printed so they can be checked before the trigger goes
down. robot.py is the thing that picks the right curve by itself; this is
deliberately dumber and says so.

⚠ AND IT DOES NOT VERIFY THE GUN. Nothing here reads the rack -- the point is
to hand control back, and a script that keeps checking is a script that is
still driving. What it prints is what the curve EXPECTS; matching it is the
operator's.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from calibration import samples as S
from calibration.fit_time_curve import fit
from press.pico_mouse import other_agents
from press.pointer import Pointer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', required=True)
    ap.add_argument('--config', default=None,
                    help="config_key fragment, e.g. grip-vert_grip. "
                         "Default 'bare'.")
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--posture', default='standing')
    ap.add_argument('--hold-s', type=float, default=1800.0,
                    help='how long to keep the port (and therefore the '
                         'pattern) alive. See the note at the bottom.')
    ap.add_argument('--fire-delay-ms', type=float, default=None,
                    help='override RECOIL_FIRE_DELAY_MS for THIS PROCESS ONLY. '
                         'See the note where it is applied.')
    ap.add_argument('--off', action='store_true',
                    help='disarm and clear instead')
    a = ap.parse_args()

    # The Pico is shared. Refusing beats taking it out from under a run that is
    # halfway through a magazine.
    busy = other_agents()
    if busy:
        print(f'[!] another agent holds the Pico ({busy}) — refusing. Ask it '
              f'to finish rather than taking the device from under it.')
        return 1

    p = Pointer()
    if a.off:
        p.pico.upload_pattern([], [], [])
        p.pico.set_recoil_enabled(False)
        print('cleared and disarmed')
        return 0

    path = os.path.join(S.SAMPLE_DIR,
                        f'{a.weapon}__{a.config or "bare"}.jsonl')
    mags = S.load(None, None, path=path)
    if not mags:
        print(f'[!] nothing stored at {path}')
        return 2
    r = fit(mags)
    if not r['ok']:
        print(f'[!] {r["why"]}')
        return 3
    for b in r.get('not_bursts', ()):
        print(f'  [!] excluded {b["ts"]}: not a burst '
              f'({b["hold_drawdown"]:.1%} drawdown under the trigger)')

    ks = r['knots']

    # ⚠ IN MEMORY, FOR THIS PROCESS, AND NOT IN config.py. RECOIL_FIRE_DELAY_MS
    # is added to every knot time by upload_pattern, and it governs every
    # collection run in this repository -- so editing the constant to try a
    # feel would make the next batch of samples incomparable with today's 204.
    # Same reason build_weapon's `rpm` override exists: one measurement is not
    # yet a fact, and the store happens after magazines AGREE.
    #
    # What it is: 13 ms, measured once on an AUG (n=36) in the BULLET-BIN
    # coordinate, where the curve was indexed by round and the offset aligned
    # "round k" with the click. In the time coordinate the fitted curve is
    # ALREADY y_true(t) with t measured from the click, so the offset is a
    # straight delay -- harmless mid-burst, where a shifted ramp is still a
    # ramp, and at the very start "late" means "absent". Reported from the
    # chair: 压得挺准，就是第一发没压.
    #
    # ⚠ 13 ms does NOT account for the size. The residual works out to an
    # equivalent lag of 75..138 ms, so this flag can only test one term of
    # several. A run that comes back unchanged has REFUTED this suspect, which
    # is worth as much as one that improves.
    if a.fire_delay_ms is not None:
        was = p.pico.RECOIL_FIRE_DELAY_MS
        p.pico.RECOIL_FIRE_DELAY_MS = a.fire_delay_ms
        print(f'\n  RECOIL_FIRE_DELAY_MS {was} -> {a.fire_delay_ms} '
              f'(this process only; config.py is untouched)')

    p.pico.upload_pattern([k['dx'] for k in ks], [k['dy'] for k in ks],
                          [k['t_ms'] / 1000.0 for k in ks])
    p.pico.set_recoil_enabled(True)
    back = p.pico.read_pattern() or []
    print(f'\nfitted from {r["n_kept"]}/{r["n_total"]} magazines '
          f'({r["n_stored"]} stored)')
    print(f'  {len(ks)} knots @ {r["grid_ms"]:.1f} ms, '
          f'{r["total_counts"]:.1f} counts over {r["span_s"]:.2f} s')
    print(f'  firmware reads back {len(back)} knots — '
          f'{"armed" if back else "UPLOAD DID NOT LAND"}')
    print(f'\nit is the curve for EXACTLY this, and nothing else:')
    print(f'    weapon   {a.weapon}')
    print(f'    fitted   {a.config or "bare (no muzzle, no grip, no stock)"}')
    print(f'    sight    {a.sight}')
    print(f'    posture  {a.posture}')

    # ⚠ THE PATTERN DOES NOT SURVIVE THIS PROCESS, so this process does not
    # exit. Measured 2026-08-08: upload, read back 177 knots in-process, exit,
    # and the next reader gets `[pat] n 0`. It is not code clearing it --
    # _connect() sends no reset and the read was taken over a connection opened
    # with DTR held LOW, so the open is not the culprit either. Closing the
    # port drops DTR, the RP2350 reboots, and the pattern lives in RAM.
    #
    # That makes "arm and hand over" a thing only a LIVE process can do. Every
    # other tool here uploads and fires within one process, so nothing had ever
    # needed the curve to outlive one -- and the first attempt at this tool
    # armed the firmware, printed "hold the trigger", and wiped it on the way
    # out.
    print(f'\nholding the serial port open for {a.hold_s:.0f} s — THE PATTERN '
          f'LIVES IN THE PICO\'S RAM AND DIES WITH THIS PROCESS.')
    print(f'hold the trigger. Ctrl-C, or `--off`, to end it.')
    try:
        t_end = time.time() + a.hold_s
        while time.time() < t_end:
            time.sleep(5.0)
            left = int(t_end - time.time())
            if left % 60 < 5:
                print(f'  still armed, {left // 60} min left', flush=True)
    except KeyboardInterrupt:
        print('\ninterrupted')
    print('releasing the port — the pattern goes with it')
    return 0


if __name__ == '__main__':
    sys.exit(main())
