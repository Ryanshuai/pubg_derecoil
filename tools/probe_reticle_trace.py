"""Does the BARREL leave the PICTURE, and when? -> reads stored magazines.

    pixi run reticle-trace --weapon mg3
    pixi run reticle-trace --weapon mg3 --frames        # every frame, not slices

THE TWO QUANTITIES, and why they are not the same one
-----------------------------------------------------
    camera_aim(t)   = cumsum(dy_px)          seven patches, world content
    divergence(t)   = y_dot(pre) - y_dot(t)  the dot moving on SCREEN
    barrel_aim(t)   = camera_aim + divergence

The compensation moves the view, and the gun rides the view -- so `y_comp`
enters camera_aim and barrel_aim identically and CANCELS in the divergence.
That is what makes this measurable with compensation armed, on an ordinary
collection magazine, instead of needing a bare arm that flies into the sky.

⚠ THE PREDICTION WAS WRITTEN BEFORE THE FIRST MAGAZINE WAS FIRED, and it is
printed next to the result so a reader can see which way it went:

    t < 0          the dot sits still; barrel and camera are the same thing
    from shot 1    the dot departs, and the barrel out-climbs the camera
    early rounds   divergence reaches ~32 px per round
                   (the wall says 43.81 px/round, the fitted curve 12.0)
    later          divergence flattens -- otherwise mid-burst compensation
                   could not feel right, and it does

Read <32 px and the hypothesis that the opening rounds are under-compensated
BECAUSE the barrel leads the picture is dead, whatever else the trace shows.

⚠ AND THE DETECTOR IS ON TRIAL HERE TOO. Every stored frame it was tuned on
is a settled one; the burst is the condition it has never seen, and muzzle
flash already leaked past the area gate once (group g5 of
calibration/artifacts/holes/manual read 638.87 where the dot was elsewhere).
A dot that JUMPS is the gate failing, not the barrel moving, so the jump size
per frame is printed and gated rather than averaged away.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import config as cfg                                          # noqa: E402
from calibration import rpm_store, samples as S               # noqa: E402

# A real barrel cannot cross this much between two frames at 144 fps: the
# whole first round is ~44 px. Anything past it is the flash, not the gun.
JUMP_MAX_PX = 25.0


def trace(mag):
    """-> dict of aligned arrays, or None when this magazine has neither route.

    ⚠ EVERYTHING IS RETURNED ON t[1:], and the two barrel routes arrive on
    DIFFERENT time bases. `weapon_dy_px` is per PAIR (len == len(t) - 1, same
    as dy_px); `reticle_y` is per FRAME (len == len(t)), so lining it up means
    dropping its first entry, not padding the shifts.

    Both are the weapon's motion in SCREEN coordinates, i.e. already relative
    to the camera -- the compensation moves the view and the gun rides it, so
    y_comp cancels here and this works on an ordinary compensated magazine.

        divergence(t) = -cumsum(weapon_dy)     barrel relative to camera
        barrel_aim(t) =  cumsum(dy) + divergence
    """
    t = np.asarray(mag.t, dtype=float)
    dy = np.asarray(mag.dy_px, dtype=float)
    if t.size < 3 or dy.size != t.size - 1:
        return None
    cam = np.cumsum(np.nan_to_num(dy))            # aligned to t[1:]
    ts = t[1:]
    out = {'t': ts, 'cam': cam}

    wdy = np.asarray(mag.weapon_dy_px, dtype=float)
    if wdy.size == dy.size:
        # ⚠ nan_to_num books an unreadable pair as "the weapon did not move",
        # which is the same one-sided loss `pixi run dropped-pairs` audits on
        # the camera side. Counted and printed rather than hidden.
        out['div'] = -np.cumsum(np.nan_to_num(wdy))
        out['w_dead'] = int((~np.isfinite(wdy)).sum())
        out['w_jump'] = np.abs(np.nan_to_num(wdy))
        out['route'] = 'ring'

    ret = np.asarray(mag.reticle_y, dtype=float)
    if ret.size == t.size:
        ry = ret[1:]
        pre = ry[(ts < 0) & np.isfinite(ry)]
        if pre.size:
            base = float(np.median(pre))
            out['dot'] = ry
            out['dot_div'] = base - ry
            out['pre_n'] = int(pre.size)
            out['pre_sd'] = float(np.std(pre)) if pre.size > 1 else 0.0
    return out if 'div' in out or 'dot_div' in out else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', required=True)
    ap.add_argument('--config', default='bare')
    ap.add_argument('--frames', action='store_true')
    a = ap.parse_args()

    path = S.path_for(a.weapon, {}, None) if a.config == 'bare' else None
    mags = S.load(a.weapon, path=path) if path else S.load(a.weapon)
    have = [m for m in mags if m.reticle_y or m.weapon_dy_px]
    n_ring = sum(1 for m in mags if m.weapon_dy_px)
    print(f'{len(mags)} magazine(s) stored, {len(have)} with a barrel trace '
          f'({n_ring} of them with the sight-tube ring)')
    if not have:
        print('\nnone yet — every magazine fired before the reticle box was '
              'added is EMPTY here, and cannot be backfilled: those pixels sat '
              'inside RECOIL_KEEPOUT and were never captured.')
        return 1

    rec = rpm_store.load().get(a.weapon)
    dt = (rec.get('interval_ms') if isinstance(rec, dict) else None) or 0.0
    S_ms = cfg.RECOIL_SHOT_VISIBLE_MS
    print(f'interval {dt:.2f} ms   shot visible at {S_ms:.0f} ms\n')

    for k, m in enumerate(have):
        tr = trace(m)
        if tr is None:
            print(f'  magazine {k}: reticle length does not match t — skipped')
            continue
        ts = tr['t'] * 1000.0
        print(f'magazine {k}  comp={"ON" if m.comp_enabled else "OFF"}  '
              f'{len(ts)} pairs   barrel route: {tr.get("route", "dot only")}')
        # ⚠ BOTH DETECTORS GO ON TRIAL BEFORE EITHER NUMBER IS READ. The jump
        # test is the same one for both: a barrel cannot cross JUMP_MAX_PX
        # between two frames at 144 fps, so anything past it is the picture
        # winning, not the gun moving.
        if 'dot' in tr:
            ok = np.isfinite(tr['dot'])
            fin = np.where(ok)[0]
            dj = np.abs(np.diff(tr['dot'][fin])) if fin.size > 1 else np.array([0.0])
            print(f'  red dot   readable {int(ok.sum())}/{len(ok)} '
                  f'({100.0 * ok.mean():.1f}%)  pre-click sd {tr["pre_sd"]:.2f} px'
                  f'  max jump {dj.max():7.2f}  over {JUMP_MAX_PX:.0f}: '
                  f'{int((dj > JUMP_MAX_PX).sum())}')
        if 'div' in tr:
            wj = tr['w_jump']
            bad = int((wj > JUMP_MAX_PX).sum())
            print(f'  ring      unread  {tr["w_dead"]}/{len(wj)}'
                  f'{"":18}{"":18}  max jump {wj.max():7.2f}  '
                  f'over {JUMP_MAX_PX:.0f}: {bad}'
                  f'{"   <- LEAKED, do not read the rest" if bad else "   OK"}')
        d = tr.get('div', tr.get('dot_div'))
        if a.frames:
            dot = tr.get('dot')
            for i in range(len(ts)):
                if -40 <= ts[i] <= 400:
                    ds = f'{dot[i]:7.2f}' if dot is not None else '     --'
                    print(f'     t {ts[i]:7.1f} ms   dot {ds}   '
                          f'div {d[i]:+7.2f}   cam {tr["cam"][i]:+8.2f}')
        else:
            # ⚠ BOTH COLUMNS ARE THE SAME KIND OF NUMBER. The first version
            # took the MEDIAN of the cumulative divergence (a level) and put it
            # beside last-minus-first of the camera (a change) -- the root
            # CLAUDE.md's rule about a criterion that cannot see its own
            # dimension, in one table row.
            edges = [(-1e9, 0)] + [(S_ms + i * dt, S_ms + (i + 1) * dt)
                                   for i in range(6)]
            print(f'    {"window ms":>16}{"d camera":>10}{"d barrel":>10}'
                  f'{"barrel-cam":>12}{"cum barrel":>12}{"cum cam":>10}')
            for lo, hi in edges:
                sel = np.flatnonzero((ts >= lo) & (ts < hi))
                if sel.size == 0:
                    continue
                i0, i1 = sel[0], sel[-1]
                bar = tr['cam'] + d
                d_cam = tr['cam'][i1] - tr['cam'][i0]
                d_bar = bar[i1] - bar[i0]
                nm = 'pre-click' if lo < 0 else f'{lo:.0f}..{hi:.0f}'
                print(f'    {nm:>16}{d_cam:+10.2f}{d_bar:+10.2f}'
                      f'{d_bar - d_cam:+12.2f}{bar[i1]:+12.2f}'
                      f'{tr["cam"][i1]:+10.2f}')
        print()

    print('PREDICTION, written before the first magazine was fired:')
    print('  divergence ~0 before the click, reaching ~32 px per round early,')
    print('  flattening later. Under 32 px and the barrel-leads-the-picture')
    print('  explanation for the opening rounds is dead.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
