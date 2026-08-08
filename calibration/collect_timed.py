"""Fire magazines into the sample store. MODEL.md's collection path.

    pixi run collect-timed --weapon m416 --mags 6
    pixi run collect-timed --weapon m416 --mags 6 --no-comp

Takes the gun that is ALREADY IN HAND -- no spawning, no kitting. That is a
deliberate first cut: the kitting machinery in harvest.py is the single largest
source of wasted runs in this project, and it has nothing to do with whether
the model works. `--weapon` names what is held and the HUD detector is asked to
agree; a disagreement stops the run rather than labelling the samples with the
name that was typed.

WHAT ONE MAGAZINE PRODUCES
--------------------------
One line in calibration/artifacts/recoil/samples/<weapon>__<config>.jsonl, holding every frame's
present time and view shift, plus THE CURVE THAT WAS PLAYING, read back from
the firmware rather than assumed. Nothing is filtered here: a magazine that
looks bad is a magazine the fitter's clustering will put outside the main
cluster, with every other magazine visible, instead of being dropped at
collection time by whichever gate happened to be in fashion.

⚠ THE TWO CAPTURE PATHS. The burst samples patches through a DXGISyncGrabber
on DXGI; everything else -- ammo, ADS, posture -- reads through the Rig on GDI.
They coexist because they are different APIs. They HAVE to: DXGI allows one
duplication interface per output per process, and putting the HUD in the same
box stretches the per-frame grab from 1.72 ms to 3.90 ms against a 6.06 ms
budget.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ⚠ AT IMPORT, NOT UNDER `if __name__`, and it is not a style preference. The
# strings that kill this process are not written here: control/inventory.py
# logs its gestures in Chinese, and on a cp1252 console `库存` is an
# UnicodeEncodeError -- not mangled output, a TRACEBACK. Measured 2026-08-08:
# an orthogonality run died at `ensure_kit` after spawning the gun, walking to
# the lane and reading the rack back, with five magazines still to fire and
# nothing written to the store.
#
# Nineteen other entry points in this repository carry their own copy of these
# three lines. That is the real defect and it is not fixed here: a library that
# can only be called from a console someone remembered to reconfigure is a
# library with an undeclared precondition, and the ratchet for it belongs in
# tools/check_layering.py, not in a twentieth copy.
#
# errors='replace' rather than a hard failure: a log line that cannot be
# spelled must not be able to end a run that is holding the game.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from calibration import samples as S                              # noqa: E402
from calibration.sweep import Rig                                 # noqa: E402
from control.session import ensure_ready                          # noqa: E402
from capture.cropper import DXGISyncGrabber                      # noqa: E402
from detector.view_tracker import MagazineRecorder                # noqa: E402
from calibration.weapon_build import build_weapon                 # noqa: E402


def measure(tracker, ts, patches, human_fn=None):
    """Frame pairs -> per-frame view shift. -> (t, dy_px, human_dy, oor).

    ⚠ drop_duplicates is OFF, and that is the point of the synchronous
    grabber. On the threaded path DXGI's video_mode COPIES the previous frame
    when nothing was presented, so identical patches had to be discarded --
    a repair downstream of a fabrication. Here a frame exists only if the
    compositor presented one, so two identical patches mean the view genuinely
    did not move, and dropping them would delete a true zero.
    """
    rec = MagazineRecorder(tracker, max_frames=len(patches) + 8,
                           drop_duplicates=False, human_fn=human_fn)
    kept = []
    for t, f in zip(ts, patches):
        p = tracker.slice_frame(f)
        if p is None:
            continue
        rec.push_patches(t, p)
        kept.append(t)
    res = rec.finish()
    return kept, res.dy, res.human_dy, res.out_of_range


# The slots that change the RECOIL, and therefore the only ones that belong in
# the pooling key. Same three the repository's own attachment_factor(gun,
# muzzle, grip, stock, posture) has always taken; `scope` is excluded because
# the optic is carried as `sight` and changes K rather than the curve.
#
# ⚠ THE MAGAZINE IS DELIBERATELY OUT, and leaving it in broke a run. Its slot
# is the one the templates cannot separate reliably -- the Tab panel is
# translucent, so the same magazine read '?' against one backdrop and
# 'quickext_ar' against another (detector/CLAUDE.md, "读不出 ≠ 没装上"). With
# the magazine in the key, one gun produced TWO filenames an hour apart and
# --from-fit reported "nothing stored" while 34 of its own magazines sat on
# disk. What the magazine determines is capacity, and that is already recorded
# per magazine as `magazine_size`.
RECOIL_SLOTS = ('muzzle', 'grip', 'stock')


def read_config(weapon=None):
    """What is ACTUALLY on the gun, as catalog keys. None if it cannot be read.

    ⚠ `weapon` IS NOT OPTIONAL IN PRACTICE. Without it, a rack with NO GUN
    reads as `{}` -- every slot is empty, the loop below skips empty slots,
    and the result is indistinguishable from a genuinely bare gun. That is the
    same failure this function was written to fix, reproduced inside the fix
    itself, and it cost a --from-fit run that reported "nothing stored for
    this config" while the real fault was an empty rack. Passing the weapon
    turns "there is no gun" into a refusal.

    ⚠ THIS IS NOT BOOKKEEPING, IT IS THE POOLING KEY. MODEL.md pools every
    magazine stored under one (weapon, config) as estimates of the same
    y_true(t). A wrong key silently pools two different guns, and the fit then
    averages them into a curve that is right for neither.

    ⚠ AND `config={}` IS A LIE THIS FILE ALREADY TOLD. The first 28 magazines
    of 2026-08-08 were stored as `m416__bare` because nothing read the slots.
    Reading them back afterwards found a compensator, a foregrip, a composite
    stock and an extended magazine -- PUBG fits whatever the backpack holds,
    which tools/CLAUDE.md has recorded for months as "刷出来的枪不是裸枪". The
    40-round count was sitting in every log line as evidence (a bare AR
    magazine is 30) and nothing looked at it.

    An unreadable slot is recorded as 'unreadable', not dropped and not
    guessed. detector/CLAUDE.md's rule: "读不出" is a third answer, and
    folding it into "empty" is how a gun with an attachment gets filed next to
    one without.
    """
    from control.inventory import InventoryControl
    from detector.attachment_catalog import ATTACHMENTS
    by_asset = {v['asset']: k for k, v in ATTACHMENTS.items()
                if isinstance(v, dict) and v.get('asset')}
    with InventoryControl() as ac:
        with ac.tab_up():
            lo = ac.loadout()
    if not lo or not lo.get('slots'):
        return None
    held = (lo.get('guns') or {}).get(1)
    if not held:
        print('  [!] rack slot 1 is EMPTY — that is not a bare gun, it is no '
              'gun, and the two read identically once the empty slots are '
              'skipped.')
        return None
    if weapon and held != weapon:
        print(f'  [!] rack slot 1 holds {held!r}, not {weapon!r} — refusing '
              f'rather than filing this magazine under the wrong weapon.')
        return None
    slots = lo['slots'].get(1) or {}
    out = {}
    for slot in RECOIL_SLOTS:
        asset = slots.get(slot)
        if not asset:
            continue
        out[slot] = by_asset.get(asset, 'unreadable' if asset == '?' else asset)
    return out


# The key `travel()` and calibration/artifacts/pitch/pitch_travel.json use for "not looking
# through anything". Spelled once, here, because the one thing that must not
# happen is this move reading a ruler measured through a sight.
HIP_SIGHT = 'hipfire'


def aim_and_scope(rig, posture):
    """Hip fire -> move the view -> ADS -> take the reference. -> bool

    ⚠ THE ORDER IS IRON, and collect_timed had it BACKWARDS until 2026-08-08:
    it scoped first and then dragged the view to the midline, taking the
    reference while scoped-and-still-settling. Three things break in the wrong
    order, and two of them break silently:

      move while scoped   drags in `pitch_scale`, a model NOTHING in this
                          repository has ever validated. Every magnified-sight
                          attempt of 2026-08-05 died on it.
      scope before still  ADS is a toggle with an animation. Scope a view that
                          is still settling and it settles INSIDE the
                          measurement.
      reference too early the tracker's patches are per-sight (7 columns on the
                          red dot, 3 through the VSS's PSO-1), so a reference
                          taken in hip fire describes a picture this magazine
                          will never see again. Hence set_ref=False on the
                          move: goto_midline takes one by default, and by
                          default is before the scope comes up.

    ⚠ `sight=HIP_SIGHT` IS THE WHOLE POINT OF PASSING IT, and leaving it out
    cost the first 40 magazines of 2026-08-08 their anchor. goto_midline
    defaults to `self.sight` -- the sight the Rig was BUILT with, i.e. the one
    the magazine will be fired through -- and this move happens in hip fire,
    where the ruler is a different length:

        red_dot.standing   3400 counts stop to stop
        hipfire.standing   8034                        2.36x

    Both numbers were already in calibration/artifacts/pitch/pitch_travel.json. Nothing was
    missing; the wrong one was being read.

    And the damage is not "it rises 42% as far", which is how it was reported
    from the chair ("抬的不够多"). goto_midline shoves `t * CLAMP_OVERSHOOT`
    into the BOTTOM STOP first, because the stop is what makes the rise
    meaningful -- and 3400 * 1.5 = 5100 does not reach 8034. THE STOP IS NEVER
    TOUCHED, so the rise starts from wherever the last magazine happened to
    end. It self-anchors after a magazine or two (the view ratchets down until
    the shove finally clips) and then sits at bottom + 1530 of 8034, i.e. 19%
    up from straight down instead of 45%. Repeatable, and pointed at the
    ground.

    ⚠ REFUSED RATHER THAN FALLING BACK. travel() answers 0 for a
    (sight, posture) it has never measured, and goto_midline then moves
    nothing and returns 0 -- which the first version of this function threw
    away. `hipfire` currently holds STANDING ONLY, so crouching and prone stop
    here with a message instead of quietly borrowing the red dot's ruler,
    which is the exact substitution that produced the paragraph above.

    Only these three return a verdict. `set_reference` reports nothing that
    means "the view is where I asked".
    """
    if not rig.gun.ensure_hip():
        print('  [!] could not get back to hip fire — moving the view from '
              "here would go through the scope's own sensitivity")
        return False
    risen = rig.view.goto_midline(posture, sight=HIP_SIGHT, set_ref=False)
    if not risen:
        print(f'  [!] no stop-to-stop travel stored for {HIP_SIGHT}/{posture} '
              f'— the view has no anchor and the magazine would start from '
              f'wherever the last one ended. Measure it once:\n'
              f'      pixi run python tools/probe_pitch_range.py '
              f'--sight {HIP_SIGHT} --postures {posture}')
        return False
    if not rig.gun.ensure_ads():
        print('  [!] could not get back into ADS')
        return False
    rig.view.set_reference()
    return True


def one_magazine(rig, grabber, weapon, mag_size, interval_s, curve,
                 config, posture, note=''):
    """Fire one, measure it, and return the record. Does not write."""
    out = rig.fire.fire_magazine_timed(grabber, mag_size, interval_s)
    t, dy_px, human_dy, oor = measure(rig.tracker, out['t'], out['patches'],
                                      human_fn=getattr(rig.mouse,
                                                       'human_totals', None))
    span = (max(t) - min(t)) if len(t) > 1 else 0.0
    return S.Magazine(
        weapon=weapon, sight=rig.sight, K=rig.K, config=dict(config or {}),
        posture=posture, curve=curve, comp_enabled=bool(curve),
        t=[float(x) for x in t],
        dy_px=[float(x) for x in dy_px],
        human_dy=[float(x) for x in human_dy],
        oor=[bool(x) for x in oor],
        magazine_size=int(mag_size or 0),
        hold_s=float(out['hold_s']),
        fps=(len(t) - 1) / span if span > 0 else float('nan'),
        ts=datetime.now().strftime('%m%d_%H%M%S'),
        note=note,
    ), out



def collect_into_store(rig, weapon, config, posture, mags, arm_plan,
                       note_prefix=''):
    """Fire `mags` magazines into the sample store, alternating curve arms.

    -> (fired, error|None). Never raises for a game problem: a burst that threw
    leaves the magazines before it on disk, because samples.append writes one
    line at a time and this returns what it managed.

    ⚠ IT LIVES HERE AND NOT IN harness/adapter.py, AND `pixi run layering`
    is why. Rule 5 forbids harness/ from importing detector/ — it "reads
    numbers, not frames, and it drives nothing" — and the first version of the
    night port built a DXGISyncGrabber in the adapter. The rule caught it on
    the same run. The grabber, the tracker and the frame timing are the
    measurement, and the measurement belongs to calibration/.

    `arm_plan` is a sequence of bools: True fires under the uploaded curve,
    False uploads it and then disarms so the burst measures y_true directly.
    More than one distinct arm is what makes the cell checkable at all --
    harness/verdict.py's out-of-loop check compares them.
    """
    from calibration import samples as S
    from calibration.weapon_build import build_weapon
    from capture.cropper import DXGISyncGrabber

    w = build_weapon(weapon, posture=posture)
    if w is None or not len(getattr(w, 't_s', ())):
        return 0, f'no pattern for {weapon}'

    grabber = DXGISyncGrabber(rig.tracker.regions())
    fired = 0
    try:
        for i in range(max(1, mags)):
            comp = arm_plan[i % len(arm_plan)]
            if comp:
                curve = rig.arm(w)
            else:
                # The pattern is still UPLOADED so the firmware holds the same
                # one either way; only the enable differs. The empty list is
                # what samples.Magazine reads as "nothing played", which is
                # what y_true = y_obs requires.
                rig.arm(w)
                rig.fire.disarm()
                curve = []
            mag, _out = one_magazine(
                rig, grabber, weapon, w.magazine_size, w.bullet_interval_s,
                curve, config, posture,
                note=f'{note_prefix}arm={"comp" if comp else "none"}')
            S.append(mag)
            fired += 1
    except Exception as e:                      # noqa: BLE001 — reported
        return fired, f'{type(e).__name__} during magazine {fired + 1}: {e}'
    finally:
        try:
            grabber.close()
        except Exception:
            pass
    return fired, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weapon', required=True)
    ap.add_argument('--mags', type=int, default=6)
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--posture', default='standing')
    ap.add_argument('--no-comp', action='store_true',
                    help='upload the curve but leave it OFF, so the magazine '
                         'measures the raw recoil directly')
    ap.add_argument('--scale', type=float, default=1.0,
                    help='multiply the curve before uploading. THE POINT IS '
                         'THE SWEEP: MODEL.md claims y_true does not depend on '
                         'which curve was playing, so y_true must come out '
                         'FLAT across scales. If it varies monotonically the '
                         'game is reacting to where the view is, and the '
                         'claim is false — which is the one thing the two-arm '
                         'measurement on 2026-08-08 could not separate.')
    ap.add_argument('--from-fit', action='store_true',
                    help='fire the curve fitted from what is already stored '
                         'for this weapon+config, instead of the one on disk')
    ap.add_argument('--fit-all', action='store_true',
                    help='with --from-fit, include the compensation-OFF '
                         'magazines in the fit. Off by default: their y_obs is '
                         'the furthest from zero of any arm, which is exactly '
                         'where the 2026-08-08 sweep found the estimate least '
                         'trustworthy')
    ap.add_argument('--kit', default=None,
                    help='fit exactly this before collecting, as '
                         '"muzzle=comp_ar,grip=vert_grip,stock=" — an empty '
                         'value strips the slot. The config is still READ BACK '
                         'afterwards and it is the readback, never this '
                         'string, that keys the samples.')
    ap.add_argument('--countdown', type=int, default=6)
    a = ap.parse_args()

    if not ensure_ready(label='timed collection',
                        countdown_s=a.countdown)['ok']:
        return 1

    # The rack empties on its own over a long run -- measured 2026-08-08: the
    # gun was simply gone after ~18 magazines, twice, with no error anywhere.
    # Re-spawning here rather than refusing keeps an unattended run going, and
    # it is safe because read_config below verifies what actually came back
    # instead of trusting that the spawn did what was asked.
    try:
        from control.inventory import InventoryControl
        from control.spawner import SpawnerControl
        with SpawnerControl(verbose=False) as sc:
            sc.ensure_panel(False)
        with InventoryControl() as ac, SpawnerControl(verbose=False) as sc:
            from control.stock import ensure_weapon_in_hand
            ensure_weapon_in_hand(ac, sc, weapon=a.weapon)
    except Exception as e:
        print(f'  [!] could not confirm a {a.weapon} in hand ({e}) — carrying '
              f'on; read_config will refuse if the rack is still empty')

    # GDI here on purpose -- the burst path owns DXGI. See _build_grabber.
    rig = Rig(a.sight, prefer_dxgi=False)
    grabber = None
    # Bound before the try: the fit below runs after the finally, and an
    # exception on any earlier line would otherwise turn a real failure into
    # a NameError three screens away from its cause.
    config, written = None, 0
    try:
        w = build_weapon(a.weapon, a.posture, {})
        interval_s = w.bullet_interval_s
        # The curve is reported by its TIME SPAN, not by a round count:
        # curve_bullets() went with the bullet-bucket coordinate, and the span
        # is what the firmware actually plays.
        span_ms = w.t_s[-1] * 1000.0 if len(w.t_s) else 0.0
        print(f'  {a.weapon}: interval {interval_s*1000:.2f} ms, '
              f'curve spans {span_ms:.0f} ms over {len(w.t_s)} knots')

        if a.from_fit:
            # Fit from what is already stored and fire THAT, rather than the
            # curve on disk. This is the iteration MODEL.md calls for: each
            # round the compensation lands closer, |y_obs| shrinks, and the
            # curve-dependence measured on 2026-08-08 shrinks with it.
            from calibration.fit_time_curve import fit
            # ⚠ NO `or {}` HERE. None means the slots could not be read, and
            # falling back to {} turns a refusal into the claim "the gun is
            # bare" -- which then loads the wrong file, or none at all, and
            # reports it as "nothing stored". That is the third time in one
            # night that an unreadable state was quietly rounded to an empty
            # one; the first two were read_config's own `{}` and the missing
            # gun check inside it.
            cfg_now = read_config(a.weapon)
            if cfg_now is None:
                print('  [!] --from-fit needs to know which config to fit '
                      'from, and the slots did not read.')
                return 5
            prev = [m for m in S.load(a.weapon, cfg_now)
                    if a.fit_all or m.comp_enabled]
            if not prev:
                print('  [!] --from-fit with nothing stored for this config')
                return 5
            r = fit(prev)
            if not r['ok']:
                print(f'  [!] {r["why"]}')
                return 5
            ks = r['knots']
            w.t_s = [k['t_ms'] / 1000.0 for k in ks]
            w.dy_s = [k['dy'] for k in ks]
            w.dx_s = [k['dx'] for k in ks]
            # ⚠ `w.bullet_interval_s = grid_ms/1000` STOOD HERE, to stop
            # upload_pattern re-binning a 225-knot curve back to 41. That
            # merge is gone and so is the parameter, so this assignment now
            # defends against nothing -- and it was the more dangerous half of
            # a pair: the local `interval_s` read at the top of main() is what
            # reaches fire_magazine_timed, and IT must stay the gun's real
            # rate or the trigger is released a fifth of the way through.
            print(f'  --from-fit: {r["n_kept"]}/{r["n_total"]} stored magazines '
                  f'-> {len(ks)} knots @ {r["grid_ms"]:.1f} ms, '
                  f'{r["total_counts"]:.1f} counts')

        if a.scale != 1.0:
            # Scale the curve, not the analysis. What gets stored is read back
            # from the firmware afterwards, so the record describes the curve
            # that actually played whatever is done here.
            w.dy_s = [v * a.scale for v in w.dy_s]
            w.dx_s = [v * a.scale for v in w.dx_s]
            print(f'  curve scaled x{a.scale}')
        rig.arm(w)
        if a.no_comp:
            rig.fire.disarm()
        curve = rig.mouse.read_pattern() or []
        if not curve and not a.no_comp:
            print('  [!] the firmware reports no pattern — refusing. A '
                  'magazine whose curve is unknown cannot be added back, '
                  'and MODEL.md needs y_comp to pool it with the others.')
            return 2
        print(f'  firmware holds {len(curve)} knots'
              f'{" (compensation OFF)" if a.no_comp else ""}')

        if a.kit:
            # Declarative: say what the gun should WEAR, not which drags to
            # make. ensure_kit reads back and retries on its own.
            want = {}
            for part in a.kit.split(','):
                k, _, v = part.partition('=')
                want[k.strip()] = (v.strip() or None)
            from control.inventory import InventoryControl
            with InventoryControl() as ac:
                with ac.tab_up():
                    r = ac.ensure_kit(1, want)
            print(f'  kit {want} -> ok={r.get("ok") if isinstance(r, dict) else r}')
            # ⚠ NOT checked here on purpose. ensure_kit's ok=False can mean
            # UNREADABLE rather than unfitted, and eleven cells of the
            # 2026-08-05 factorial were binned for exactly that. The authority
            # is read_config() below: whatever the gun turns out to be wearing
            # is what these samples get filed under, so a partial fit costs a
            # mislabelled cell only if the READBACK is also wrong.

        # BEFORE ensure_ads: opening Tab drops the character out of ADS
        # (docs/game_quirks.md), so reading the loadout after scoping would
        # silently un-scope the whole run.
        config = read_config(a.weapon)
        if config is None:
            print('  [!] REFUSING: could not read the attachment slots. The '
                  'config is the key every magazine gets pooled under, and a '
                  'guessed one merges two different guns.')
            return 4
        print(f'  fitted: {config or "(nothing)"}')

        rig.ensure_posture(a.posture)

        grabber = DXGISyncGrabber(rig.tracker.regions())
        for i in range(a.mags):
            mag_size, _ = rig.fire.top_up()
            if not mag_size:
                print(f'  mag {i}: no ammo counter — stopping')
                break
            # ⚠ ONE HOMING PER MAGAZINE, NOT TWO. There used to be a
            # goto_midline right after ensure_posture as well, so the first
            # magazine dipped the view to the bottom clamp TWICE before firing
            # -- once in setup and once here. Reported from the chair: "压枪的
            # 时候会低两次头".
            #
            # The one that went is the setup one, and the loop's is the one
            # that must stay: every magazine has to START at the midline
            # because the burst walks the view up from wherever it begins, and
            # the previous magazine ended several hundred counts high. Homing
            # only at setup would leave magazine 2 onwards starting from
            # wherever magazine 1 finished.
            #
            # ⚠ THE TRAVEL IS NOT RE-MEASURED BY EITHER OF THEM. travel() reads
            # a stored per-(sight, posture) constant and goto_midline is then
            # two mouse moves -- shove past the clamp by a known multiple, come
            # back up half. The dip is the shove, not a measurement. Measuring
            # is measure_travel(), which this path never calls.
            if not aim_and_scope(rig, a.posture):
                print(f'  mag {i}: could not re-aim — stopping')
                break
            mag, out = one_magazine(
                rig, grabber, a.weapon, mag_size, interval_s,
                [] if a.no_comp else curve, config, a.posture,
                note='no-comp' if a.no_comp else f'scale={a.scale:g}')
            p = S.append(mag)
            written += 1
            t, y = mag.y_true_counts()
            pre = sum(1 for x in mag.t if x < 0)
            print(f'  mag {i}: {mag.n_frames():4d} frames '
                  f'({mag.fps:5.1f} fps, {pre} prefire, '
                  f'{out["n_missed"]} missed), '
                  f'y_true {y[-1]:7.1f} counts over {t[-1]:.2f} s')
            rig.fire.wait_reload(expect=mag_size)
        print(f'\n  {written} magazine(s) -> {S.path_for(a.weapon, config)}')
    finally:
        if grabber is not None:
            grabber.close()
        rig.close()

    if written:
        from calibration.fit_time_curve import fit
        mags = S.load(a.weapon, config)
        r = fit(mags)
        if r['ok']:
            print(f'\n  fit over {r["n_kept"]}/{r["n_total"]} stored '
                  f'magazines: {len(r["knots"])} knots @ {r["grid_ms"]:.1f} ms, '
                  f'{r["total_counts"]:.1f} counts')
            print(f'  {r["cluster_why"]}')
            print(f'  samples per knot {r["samples_per_knot"]:.1f}, '
                  f'spread {r["spread_counts"]:.1f} counts')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
