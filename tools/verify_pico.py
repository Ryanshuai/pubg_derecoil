"""Firmware acceptance. Bench test — no game, no window, no cursor movement.

    pixi run verify-pico

Run this after every flash, and before any calibration run. A curve is
measured THROUGH this firmware, so a systematic error in here is copied into
every weapon's stored curve, and the residual stays clean while it happens:
the calibration fits against what it observes, so it absorbs the error and
reports success.

Two real bugs lived behind a clean residual until 2026-08-02:

    rng_float() returned [0, +2) instead of [-1, +1). "Zero-mean jitter" was a
    mean +2% and a mean +0.2 counts, always downward -- about +29 counts on an
    AUG magazine, 2.8% of the pattern, quietly absorbed into the stored curve.

    the last bullet's compensation was spread over a hardcoded 100 ms, which
    is no weapon's interval. On a Vector (54.5 ms) the round carrying the most
    recoil was smeared over nearly two rounds.

Both are fixed. This is what stops them coming back. Every check below is a
number against a threshold that was written down before the check ran; none of
them asks anyone to look at a trace and judge.

What this does NOT cover: whether the compensation lands on the right ROUND.
That is not observable from the wire -- it needs the game and the screen, and
it is tools/probe_impulse_align.py.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from press.pico_mouse import PicoMouse, other_agents      # noqa: E402

# ── thresholds ──
#
# Integers in, integers out: the pattern the firmware holds must be the
# pattern that was uploaded, exactly. There is no rounding step between the
# two, so any tolerance here would only hide a protocol bug.
#
# The jitter bound is the one number with a scale to argue about. The bug it
# guards against was a mean of +0.2 counts per bullet on the additive term
# alone (plus +2% multiplicative); 0.05 counts per bullet is four times
# smaller than that, and with SIM_ITERS x bullets samples the standard error
# of the mean is far below it -- so this rejects the old behaviour with room
# to spare while not tripping on honest noise.
JITTER_MEAN_MAX = 0.05        # counts per bullet
SIM_ITERS = 1000

# A test pattern with the shapes that have gone wrong: a first bullet of
# nearly nothing (its compensation is under a count on a real weapon, which is
# what made the +0.2 additive bias a 60% perturbation), a ramp, and a NON-100
# ms interval so a reintroduced hardcoded 100 shows up on the last bullet.
INTERVAL_MS = 83              # AUG, measured: 719.7 rpm
TEST_N = 12


def build_pattern():
    """(dx, dy, t_ms) per bullet. Deliberately not a real curve — a real one
    would be dominated by its plateau and hide the endpoints."""
    return [(0, 1 + 3 * i, INTERVAL_MS * i) for i in range(TEST_N)]


class Report:
    def __init__(self):
        self.rows = []

    def check(self, name, ok, detail):
        self.rows.append((name, bool(ok), detail))
        print(f'  {"PASS" if ok else "FAIL"}  {name:<34} {detail}')
        return ok

    def failed(self):
        return [r for r in self.rows if not r[1]]


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    busy = other_agents()
    if busy:
        print(f'[!] another process is on the Pico: {busy}\n'
              f'    Wait for it rather than killing it.')
        return 2

    rep = Report()
    m = PicoMouse()

    pts = build_pattern()
    # Uploaded straight, NOT through upload_pattern(): that method merges
    # samples into bullets and applies RECOIL_FIRE_DELAY_MS, and this is a
    # test of the firmware, not of the host's merge. The merge has its own
    # bugs and they belong to a different test.
    import struct
    from press.pico_mouse import CMD_PATTERN_UPLOAD
    body = b''.join(struct.pack('<hhH', dx, dy, t) for dx, dy, t in pts)
    m._write(struct.pack('<BH', CMD_PATTERN_UPLOAD, len(pts)) + body)

    print(f'\nfirmware acceptance — {len(pts)} bullets at {INTERVAL_MS} ms\n')

    got = m.read_pattern()
    if got is None:
        print('  FAIL  pattern readback                 no [pat] reply — this '
              'firmware predates\n                                            '
              'CMD_PATTERN_READ. Flash pico_firmware.')
        return 1

    # ── 1. the pattern survives the round trip ──
    rep.check('stored length', len(got) == len(pts),
              f'{len(got)} bullets, uploaded {len(pts)}')
    if len(got) == len(pts):
        bad = [(i, g, p) for i, (g, p) in enumerate(zip(got, pts))
               if (g['dx'], g['dy'], g['t_ms']) != p]
        rep.check('stored values', not bad,
                  'every bullet matches' if not bad else
                  f'{len(bad)} differ, first: got '
                  f'({bad[0][1]["dx"]},{bad[0][1]["dy"]},{bad[0][1]["t_ms"]}) '
                  f'want {bad[0][2]}')

    # ── 2. the last bullet is spread over the INTERVAL, not a constant ──
    #
    # This is the regression guard. A reintroduced hardcoded 100 shows here and
    # nowhere else: every other bullet's duration comes from the next bullet's
    # timestamp and would be right even with the bug.
    if got:
        durs = [g['dur_ms'] for g in got]
        mid_ok = all(d == INTERVAL_MS for d in durs[:-1])
        rep.check('bullets 0..n-2 spread', mid_ok,
                  f'all {INTERVAL_MS} ms' if mid_ok else f'{durs[:-1]}')
        rep.check('LAST bullet spread', durs[-1] == INTERVAL_MS,
                  f'{durs[-1]} ms, want {INTERVAL_MS} — a 100 here is the '
                  f'hardcoded-window bug' if durs[-1] != INTERVAL_MS
                  else f'{durs[-1]} ms')

    # ── 3 & 4. total conservation and zero-mean jitter ──
    #
    # One measurement answers both: run the real per-bullet maths many times
    # and compare what came out with what went in. Jitter that is genuinely
    # zero-mean conserves the total; jitter with a bias does not, and the bias
    # IS the difference.
    sim = m.simulate_recoil(iters=SIM_ITERS)
    if sim is None:
        rep.check('jitter simulation', False, 'no [sim] reply')
    else:
        n = sim['iters'] * sim['bullets']
        rep.check('simulation ran', n > 0,
                  f"{sim['iters']} x {sim['bullets']} = {n} bullets")
        if n:
            bias_y = (sim['emit_dy'] - sim['cmd_dy']) / n
            bias_x = (sim['emit_dx'] - sim['cmd_dx']) / n
            rep.check('jitter mean, dy', abs(bias_y) <= JITTER_MEAN_MAX,
                      f'{bias_y:+.4f} counts/bullet '
                      f'(limit {JITTER_MEAN_MAX}); total emitted '
                      f"{sim['emit_dy']:.1f} vs commanded {sim['cmd_dy']}")
            rep.check('jitter mean, dx', abs(bias_x) <= JITTER_MEAN_MAX,
                      f'{bias_x:+.4f} counts/bullet (limit {JITTER_MEAN_MAX})')

    m.clear_pattern()

    bad = rep.failed()
    print()
    if bad:
        print(f'{len(bad)} of {len(rep.rows)} checks FAILED:')
        for name, _, detail in bad:
            print(f'  {name}: {detail}')
        print('\nDo not start a calibration run. Every curve measured through '
              'this firmware\nwould absorb the fault and report a clean '
              'residual while doing it.')
        return 1
    print(f'all {len(rep.rows)} checks pass.\n\n'
          'Not covered here: whether the compensation lands on the right '
          'ROUND. That is\nnot observable from the wire — '
          'tools/probe_impulse_align.py, and it needs the game.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
