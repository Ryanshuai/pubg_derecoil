"""Fire magazines into the sample store. MODEL.md's collection path.

    pixi run collect-timed --weapon m416 --mags 6
    pixi run collect-timed --weapon m416 --mags 6 --no-comp

⚠ THIS PARAGRAPH USED TO SAY "no spawning, no kitting", and by 2026-08-08 it
was false in both halves: `--kit` fits a config and the run spawns the gun
through control.stock.ensure_weapon_in_hand. Kept as a correction rather than
deleted, because the original reason was sound and still constrains the
design: the kitting machinery was the single largest source of wasted runs in
this project, so everything it touches here is guarded by a REFUSAL rather
than by a retry.

FOUR THINGS IT REFUSES TO GUESS, each paid for on the day it was added:

  the weapon    `--weapon` names what should be held; read_config compares it
                against the rack and stops rather than filing samples under
                the name that was typed.
  the config    what `--kit` ASKED for must appear in the readback. A part
                that went on but reads as empty, and a part that never went
                on, are the same picture from here — and both end with
                magazines filed under a config that did not fire them.
  the optic     `--sight` picks the profile K comes from; the gun wears
                whatever PUBG auto-fitted. A count is worth ~3x more through
                a red dot than through iron sights, so a disagreement scales
                every number in the run.
  ADS           the burst is bracketed by ensure_ads() before and a read of
                `in_ads()` after. Same 3x, same silence: `ads_frac` was `nan`
                on all 167 magazines in the store before this existed.

The shape they share is worth stating once, because every failure this file
has had is an instance of it: THE RECORD DESCRIBED A DIFFERENT OBJECT THAN THE
ONE THAT FIRED. Two mp5ks in the rack, a kitted gun filed as bare, a red dot
analysed as iron. None of them raised, none of them looked wrong in the
printed numbers, and each was caught only by asking a second, independent
source about the same object.

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
from config import RECOIL_COMP_LAG_MS                             # noqa: E402


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
    # ⚠ EXACTLY ONE GUN, AND THIS FILE READS SLOT 1 WHILE THE TRIGGER FIRES
    # WHATEVER IS IN HAND. Nothing in the HUD distinguishes them: the name
    # plate says `mp5k` for both, the ammo counter says 40 for both, and the
    # burst looks identical. So a second gun is not a mess to tidy, it is an
    # unanswerable question, and the answer this function would give is a
    # confident description of the wrong object.
    #
    # Measured 2026-08-08. ensure_weapon_in_hand spawned a second mp5k (its own
    # bug, fixed in control/stock.py), put THAT one in hand, and ensure_kit
    # stripped and read the other -- it skips hold() when its only step is an
    # unequip. Five magazines out of a gun wearing comp_smg + vert_grip +
    # tactical_stock landed in the cell named `grip-vert_grip`:
    #
    #     gun1  red dot, no muzzle, vert_grip, no stock     <- read and filed
    #     gun2  red dot, comp_smg,  vert_grip, tactical     <- fired
    #
    # cv was 1.4% across the five. Nothing about the run looked wrong, and the
    # only tell was that the cell reproduced a DIFFERENT cell's number to 1%.
    others = {s: g for s, g in (lo.get('guns') or {}).items() if g and s != 1}
    if others:
        print(f'  [!] the rack also holds {others} — refusing. This reads slot '
              f'1 and the trigger fires whatever is in hand, and with two guns '
              f'out nothing on screen can say those are the same gun. Drop one.')
        return None
    slots = lo['slots'].get(1) or {}
    out = {}
    for slot in RECOIL_SLOTS:
        asset = slots.get(slot)
        if not asset:
            continue
        out[slot] = by_asset.get(asset, 'unreadable' if asset == '?' else asset)
    return out


def read_sight():
    """The gun's optic, twice: -> (profile_name, asset). (None, None) if unread.

    ⚠ THIS WAS READ AND THEN THROWN AWAY, and it is the one quantity in this
    file that nothing could check after the fact. `scope` is deliberately not
    in RECOIL_SLOTS -- the config key has to be stable, and the magazine slot
    already destabilised it once -- but "not in the key" got implemented as
    "not recorded at all", and those are different decisions.

    What it cost, 2026-08-08: two mp5k cells whose muzzle/grip/stock all read
    empty came out 901 and 435 counts, a factor of 2.07. The only difference
    anything could name was the optic, because PUBG auto-fits whatever the
    backpack holds and --kit has never managed the scope slot. A count is worth
    a different angle through iron sights than through a red dot -- roughly a
    third, per detector/weapon._sight_of -- so K is wrong whenever the optic is
    not the one --sight named. NEITHER CELL RECORDED WHICH IT HAD, so neither
    can be rescued, only re-fired.

    A program can only check a thing that exists in two places. This one
    existed in zero.

    Returns the ASSET alongside the profile because two different consumers
    need two different spellings of one reading, and taking it twice would be
    two readings: `Weapon.set('scope', ...)` keys `_SCOPE_TO_MAG` on the raw
    asset, while everything downstream of K wants the profile name.
    """
    from control.inventory import InventoryControl
    from detector.weapon import _sight_of
    with InventoryControl() as ac:
        with ac.tab_up():
            lo = ac.loadout()
    if not lo or not lo.get('slots'):
        return None, None
    asset = (lo['slots'].get(1) or {}).get('scope') or ''
    return _sight_of(asset), asset


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
                 config, posture, note='', fire_delay_ms=None):
    """Fire one, measure it, and return the record. Does not write.

    ⚠ ADS IS BRACKETED, NOT SAMPLED, AND THE DIFFERENCE IS STATED BECAUSE THIS
    FIELD ALREADY LIED ONCE BY BEING ABSENT. `ads_frac` -- the fraction of
    polls that read as scoped -- is computed by the OLD fire_magazine(), and
    this path uses fire_magazine_timed(), which never wired it up. Every one of
    the 167 magazines in the store carries `nan`: 143 m416 including all eight
    cells of the 2x2x2, and 24 mp5k.

    That matters because ADS is worth about 3x in K, exactly like the optic --
    a magazine fired out of ADS is analysed with a constant that is wrong by
    the same factor, and nothing said so.

    The timed grabber cannot supply the fraction: it captures the tracker's
    patches, and AdsDetector reads the SCREEN CENTRE, which is not among them.
    So this reads ADS ONCE, right after the trigger releases, off a GDI frame.
    Together with aim_and_scope's ensure_ads() before the burst it brackets the
    magazine: both ends scoped.

    WHAT THAT CANNOT SEE, said plainly: a dropout in the middle that recovers
    before the end. It catches the failure that actually happens -- dropping
    out and staying out -- and it is two points, not a rate. `ads_frac` stays
    nan rather than being filled with 1.0, because writing a rate this did not
    measure is how the field became untrustworthy in the first place.
    """
    out = rig.fire.fire_magazine_timed(grabber, mag_size, interval_s)
    # Before the reload: R drops out of ADS on its own (docs/game_quirks.md),
    # so a read taken after it would report the reload, not the burst.
    try:
        ads_end = bool(rig.gun.in_ads())
    except Exception as e:                       # noqa: BLE001
        print(f'      [!] could not read ADS after the burst: {e}')
        ads_end = None
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
        # ⚠ STAMPED AT FIRING TIME, not looked up when the magazine is read
        # back. It is how long an emitted count took to reach the screen ON
        # THIS MACHINE ON THIS DAY, and y_true = y_obs + C(t - L) is wrong by
        # L*F' without it -- which the fit then amplifies, because it consumes
        # its own output. See samples.Magazine.comp_lag_s.
        comp_lag_s=RECOIL_COMP_LAG_MS / 1000.0,
        # ⚠ ALSO STAMPED, because it is NOT recoverable from the curve. The
        # fold in upload_pattern collapses every negative offset to
        # curve[0]['t_ms'] == 0; see samples.Magazine.fire_delay_ms for the 50
        # magazines this already cost.
        fire_delay_ms=fire_delay_ms,
        fps=(len(t) - 1) / span if span > 0 else float('nan'),
        ads_end=ads_end,
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
    ap.add_argument('--fire-delay-ms', type=float, default=None,
                    help='override RECOIL_FIRE_DELAY_MS for THIS RUN ONLY. '
                         'upload_pattern adds it to every knot time, so it '
                         'decides WHEN the compensation plays relative to the '
                         'click. RECORDED on the magazine. This used to say '
                         '"nothing needs recording, curve[0][\'t_ms\'] IS the '
                         'offset that played" and it was checked against the '
                         'one batch with a POSITIVE offset; the fold collapses '
                         'every negative one to 0. See Magazine.fire_delay_ms.')
    ap.add_argument('--fire-delay-sweep', default=None,
                    help='comma-separated offsets in ms, ROTATED PER MAGAZINE '
                         'off one fit. This is how the offset gets measured '
                         'rather than guessed: the curve is held fixed and '
                         'only the offset moves, and the arms interleave in '
                         'time so session drift cannot align with an arm.')
    ap.add_argument('--scale-sweep', default=None,
                    help='comma-separated curve multipliers, ROTATED PER '
                         'MAGAZINE off one fit — the amplitude twin of '
                         '--fire-delay-sweep, and the only well-posed way to '
                         'measure how much of the curve actually arrives. '
                         'residual(s) = y_true - s*(1-eps)*F is LINEAR in s '
                         'with a zero crossing at s = 1/(1-eps), and a +-10%% '
                         'sweep moves s by six times eps itself, so nothing '
                         'here is collinear with anything. --scale does the '
                         'same thing ONE RUN AT A TIME and that is exactly the '
                         'mistake the offset sweep already paid for: 30 counts '
                         'of between-session drift on the same gun twenty '
                         'minutes apart (see config.RECOIL_FIRE_DELAY_MS).')
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
        # ⚠ THE GUN IS ESTABLISHED BEFORE THE CURVE IS BUILT, AND IT USED TO BE
        # THE OTHER WAY ROUND. `build_weapon(weapon, posture, {})` stood at the
        # top of this block and armed the firmware from an EMPTY config, then
        # the kit went on and the readback happened underneath it. So every
        # compensating run on a kitted gun played the curve stored for a bare
        # one through iron sights — `[curves] no fitted curve for mp5k bare
        # standing iron` printed on a gun that was about to wear a compensator,
        # a foregrip and a red dot (2026-08-08).
        #
        # `--no-comp` hid it completely: the curve is never played, so the only
        # symptom was one log line that read like a missing measurement rather
        # than like a wrong lookup.
        #
        # It cannot be fixed by passing `--kit` into build_weapon either. The
        # config that decides the curve has to be the READBACK — what the gun
        # turned out to be wearing — and that does not exist until the kitting
        # is done. So the kitting comes first, and everything downstream reads
        # ONE established configuration instead of a request and a hope.
        if a.kit:
            # Declarative: say what the gun should WEAR, not which drags to
            # make. ensure_kit reads back and retries on its own.
            want = {}
            for part in a.kit.split(','):
                k, _, v = part.partition('=')
                want[k.strip()] = (v.strip() or None)
            from control.inventory import InventoryControl
            from control.spawner import SpawnerControl
            from control.stock import restock as _restock
            keep = {v for v in want.values() if v}
            with InventoryControl() as ac, SpawnerControl(verbose=False) as sc:
                # ⚠ THE HOOK IGNORES ITS ARGUMENT, ON PURPOSE. ensure_kit hands
                # it only the keys that are MISSING, and `restock`'s first
                # argument doubles as the KEEP-LIST -- everything nameable in
                # 库存 and not in it goes on the floor. Handing it the missing
                # subset would therefore drop the parts already on hand, which
                # is the same shape as the bug control/stock.py records under
                # "want DOUBLES AS THE KEEP-LIST": a {slot: key} table once
                # emptied the whole backpack for exactly this reason.
                #
                # So the keep-list is the WHOLE kit, every time.
                # `_missing` underscored so tools/check_params.py reads the
                # signature the way the paragraph above does: the argument is
                # ensure_kit's to pass, not this body's to use.
                def _fill(_missing):
                    return _restock(ac, sc, keep, leave='shut', verbose=True)

                # ⚠ WITHOUT A HOOK, AN EMPTY BACKPACK IS AN UNCONDITIONAL
                # REFUSAL. Measured 2026-08-08: the game had restarted, 库存
                # held nothing, and `--kit` could not fit a single part -- so
                # read_config saw a bare gun, the disagreement check fired, and
                # the run ended before a shot. Nothing was wrong except that
                # this layer knew how to ask for parts and not how to get them.
                #
                # `weapon` is the catalogue gate and was missing too: without
                # it a fit can be planned onto a slot the gun does not have,
                # and that part lands on the floor.
                with ac.tab_up():
                    r = ac.ensure_kit(1, want, weapon=a.weapon, restock=_fill)
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

        # ⚠ THE READBACK IS AUTHORITATIVE ABOUT WHAT IT SEES, AND SILENT ABOUT
        # WHAT IT MISSES — which is the hole the refusal above cannot cover.
        # `read_config` returning {} means "no attachments", and that is
        # exactly what an unreadable-but-fitted part looks like.
        #
        # ⚠⚠ THIS PARAGRAPH USED TO NAME A CASE THAT NEVER HAPPENED, and the
        # way it was wrong is worth more than the example was. It said:
        # `--kit stock=heavy_stock` fitted on the mp5k, the gun fired 435
        # against bare's 901, "so the stock was ON", and the readback saw
        # nothing — therefore heavy_stock does not read on this gun.
        #
        # Every step of that is an inference except the 435, and the inference
        # is arithmetic that was never done. heavy_stock on this gun is 0.817
        # (data/kit_factors.json, 2026-08-05), so a stocked mp5k fires ~740.
        # 435/903 = 0.48, which is not the stock — it is comp_smg+vert_grip,
        # 0.479 in the same table. The batch was a KITTED GUN read as bare, the
        # same two-guns-on-the-rack failure as the two batches either side of
        # it, and "the template cannot read this part" was a story invented to
        # explain a number that already had an explanation.
        #
        # Measured properly 2026-08-08 16:24, one gun on the rack: the readback
        # says `{'stock': 'heavy_stock'}` first try, and the gun fires 751.2
        # against a predicted 738. IT READS FINE. IT ALWAYS DID.
        #
        # What survives is the reason for the check, which the fake example was
        # only illustrating: `read_config` returning {} means "no attachments",
        # and an unreadable-but-fitted part looks exactly like that. That much
        # is a property of slot_detector's deliberate trade and needs no
        # example. What does NOT survive is trusting a diagnosis built from one
        # cell's total — see MODEL.md §5之二, and note that this is that law
        # biting through a number that was measured correctly.
        #
        # So: what you ASKED for is the one thing this layer knows that the
        # readback does not. A disagreement is not a mislabel to file, it is a
        # measurement whose subject is unknown.
        # ⚠ ONLY AGAINST SLOTS `config` CAN CONTAIN, and getting that wrong made
        # the check UNPASSABLE. read_config returns RECOIL_SLOTS and nothing
        # else — `scope` is excluded from the key on purpose, three screens up —
        # so `--kit scope=red_dot` compared a request against a dict that
        # structurally could not hold the answer, and refused a gun that was
        # wearing the right optic all along (2026-08-08, mp5k, ensure_kit's own
        # plan said "3 slots already right" in the line above the refusal).
        #
        # A checker that cannot pass is not a strict checker, it is a broken
        # one: it stops the good runs and says nothing about the bad ones. The
        # scope is checked ten lines down by read_sight(), and MORE strictly —
        # that one also compares against --sight, which decides K.
        if a.kit:
            unverifiable = sorted(k for k in want
                                  if k not in RECOIL_SLOTS and k != 'scope')
            if unverifiable:
                print(f'  [!] REFUSING: --kit names {unverifiable}, and nothing '
                      f'in this run can read those slots back. read_config '
                      f'reports {list(RECOIL_SLOTS)} and read_sight reports the '
                      f'optic; a request nobody checks is a request that can '
                      f'silently not happen.')
                return 5
            # Both directions. A part that was asked for and is not there is
            # the obvious half; a STRIP that did not happen is the other, and
            # it is the one that kept coming back — PUBG bolts whatever the
            # backpack holds onto a gun the moment it arrives, so `stock=`
            # failing quietly hands back the previous cell's stock.
            #
            # ⚠ THE STRIP HALF CANNOT BE FULLY VERIFIED and must not be read as
            # if it could: an unreadable part reads `empty` (slot_detector's
            # deliberate trade), so this catches a strip that left something
            # RECOGNISABLE and misses one that left something the templates do
            # not know. Partial, and better than nothing, and that is all.
            missing = {k: v for k, v in want.items()
                       if k in RECOIL_SLOTS and v and config.get(k) != v}
            stuck = {k: config[k] for k, v in want.items()
                     if k in RECOIL_SLOTS and not v and config.get(k)}
            if missing or stuck:
                if missing:
                    print(f'  [!] REFUSING: asked for {missing} and the readback '
                          f'does not show it (reads {config or "nothing"}). '
                          f'Either the part did not go on — in which case this '
                          f'is the wrong gun to measure — or it went on and '
                          f'cannot be read, in which case every magazine would '
                          f'be filed under a config that is not what fired. '
                          f'Both end the same way, so neither is worth a run.\n'
                          f'      To measure it anyway, fit it by hand and drop '
                          f'--kit: the readback is only consulted for slots it '
                          f'can see, and without a request there is nothing to '
                          f'contradict.')
                if stuck:
                    print(f'  [!] REFUSING: asked to STRIP {sorted(stuck)} and '
                          f'the gun still wears {stuck}. This run would measure '
                          f'a config it was not asked for — five magazines '
                          f'filed under the wrong cell, all of them plausible.')
                return 5

        # ⚠ THE OPTIC DECIDES K, AND NOTHING WAS CHECKING IT. `--sight` picks
        # the profile the Rig's K comes from; the gun wears whatever PUBG
        # auto-fitted out of the backpack. When those disagree every count in
        # the run is scaled wrong -- by about 3x between iron sights and a red
        # dot -- and the magazine records the FLAG, so nothing downstream can
        # even see the disagreement.
        #
        # Refused rather than corrected: swapping in the right K would file
        # magazines under a sight the caller did not ask for, and a cell that
        # silently changes its own measurement conditions is the thing this
        # whole file exists to stop.
        worn, scope_asset = read_sight()
        if worn is None:
            print('  [!] REFUSING: could not read the scope slot. K comes from '
                  'the optic, so an unread one is an unknown scale on every '
                  'count in this run.')
            return 6
        if worn != rig.sight:
            print(f'  [!] REFUSING: --sight says {rig.sight!r} (K={rig.K}) and '
                  f'the gun is wearing {worn!r}. A count is worth a different '
                  f'angle through each, so every number this run produced '
                  f'would be scaled by the wrong constant — and the magazine '
                  f'records the FLAG, so nothing downstream could see it.\n'
                  f'      Either fit a {rig.sight} or pass --sight {worn}.')
            return 7
        print(f'  sight : {worn} (K={rig.K}, read back off the gun)')

        # ---------------------------------------------------------------
        # The gun is now established. Build the curve FOR IT.
        # ---------------------------------------------------------------
        # `config` is the readback, `scope_asset` is the optic as the raw asset
        # string Weapon.set('scope', ...) keys _SCOPE_TO_MAG on. Together they
        # are the same four things set_seq looks the curve up by, so what the
        # firmware plays is what was fitted for this exact configuration —
        # under plan A there is no interpolation, so a wrong key is not a
        # slightly-wrong curve, it is another gun's.
        w = build_weapon(a.weapon, a.posture, dict(config, scope=scope_asset))
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
            # ⚠ THIS USED TO TAKE ITS OWN read_config(), and that read happened
            # BEFORE the kitting — so --from-fit fitted from the configuration
            # the gun had on ARRIVAL and then fired it at the one it was kitted
            # into. One established readback now serves both, which is also the
            # only way the two can be guaranteed to agree.
            #
            # The old comment here is still the reason `config` may not be
            # rounded to {}: None means the slots could not be read, and
            # falling back to {} turns a refusal into the claim "the gun is
            # bare" -- which then loads the wrong file, or none at all, and
            # reports it as "nothing stored". The refusal now lives at the
            # readback itself (return 4), which is strictly earlier.
            prev = [m for m in S.load(a.weapon, config)
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
            # a pair: the local `interval_s` read just above is what reaches
            # fire_magazine_timed, and IT must stay the gun's real rate or the
            # trigger is released a fifth of the way through.
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
        # ⚠ ON THE INSTANCE, FOR THIS RUN, AND NOT IN config.py. The constant
        # governs every collection this repository does, so editing it to run
        # one sweep would make the next batch incomparable with the 204 stored.
        #
        # Nothing extra needs recording: upload_pattern shifts the knot TIMES
        # and read_pattern returns the shifted ones, so `curve[0]['t_ms']` on
        # every magazine already on disk IS the offset that played. Verified on
        # the 2026-08-08 17:29 batch, which reads 13.
        if a.fire_delay_ms is not None:
            was = rig.mouse.RECOIL_FIRE_DELAY_MS
            rig.mouse.RECOIL_FIRE_DELAY_MS = a.fire_delay_ms
            print(f'  RECOIL_FIRE_DELAY_MS {was} -> {a.fire_delay_ms} '
                  f'(this run only)')
        rig.arm(w)
        if a.no_comp:
            rig.fire.disarm()
        curve = rig.mouse.read_pattern() or []
        if curve:
            print(f'  the curve starts at t={curve[0]["t_ms"]} ms — that IS '
                  f'the fire delay that will play')
        if not curve and not a.no_comp:
            print('  [!] the firmware reports no pattern — refusing. A '
                  'magazine whose curve is unknown cannot be added back, '
                  'and MODEL.md needs y_comp to pool it with the others.')
            return 2
        print(f'  firmware holds {len(curve)} knots'
              f'{" (compensation OFF)" if a.no_comp else ""}')

        # ⚠ SAY WHICH GUN FIRES. Everything above reads RACK SLOT 1; the
        # trigger fires whatever is in hand, and until this line nothing joined
        # the two. ensure_kit does not close the gap either -- it takes the gun
        # in hand only when one of its steps is an EQUIP, so a cell whose only
        # step is a strip leaves in hand whatever was there before.
        #
        # With read_config now refusing a second gun this is no longer load
        # bearing for correctness, and it stays anyway: a precondition that
        # holds by luck reads exactly like one that is enforced, and this one
        # cost five magazines on 2026-08-08 the last time it held by luck.
        from control.inventory import InventoryControl as _IC
        with _IC() as _ac:
            if not _ac.hold(1):
                print('  [!] REFUSING: rack slot 1 would not come to hand. It '
                      'is the slot every readback above describes, so firing '
                      'now would measure a gun nothing in this run has read.')
                return 8

        rig.ensure_posture(a.posture)

        # ⚠ THE CAPACITY IS THE MAGAZINE'S IDENTITY, so it has to be the SAME
        # capacity as every magazine this weapon's cube is being compared
        # against. The operator's rule (2026-08-08): read the number after the
        # reload and let it decide, because the icon cannot -- see
        # samples.Magazine.magazine_size for the measured reason.
        #
        # Constant, not looked up. Nothing in this repository has a measured
        # base-capacity table, and inventing one would be a game fact asserted
        # rather than observed. What the store already knows is what every
        # earlier magazine of this weapon fired, and a cube whose cells differ
        # in burst LENGTH is not a cube -- a base magazine is 10 rounds and
        # ~0.65 s shorter, which looks exactly like a very effective attachment.
        prior = {m.magazine_size for m in S.all_magazines(a.weapon)
                 if m.magazine_size}
        if prior:
            print(f'  magazine: every stored {a.weapon} magazine holds '
                  f'{sorted(prior)} rounds')

        sweep = ([float(x) for x in a.fire_delay_sweep.split(',')]
                 if a.fire_delay_sweep else None)
        if sweep:
            print(f'  sweeping the fire delay per magazine over {sweep} ms, '
                  f'off ONE fitted curve')

        scale_sweep = ([float(x) for x in a.scale_sweep.split(',')]
                       if a.scale_sweep else None)
        # ⚠ ONE ROTATION AT A TIME. Two arms turning together make every
        # magazine a different (offset, scale) pair, and with five magazines an
        # arm neither term is identified -- which is the same ill-conditioning
        # this sweep exists to escape.
        if scale_sweep and sweep:
            print('  [!] REFUSING --scale-sweep with --fire-delay-sweep. Two '
                  'things rotating per magazine confounds them; run the '
                  'amplitude sweep at a FIXED offset.')
            return 10
        # The base curve, captured BEFORE any magazine scales it, so arm k is
        # base*s_k and not base*s_0*s_1*...*s_k. Compounding would look exactly
        # like a very strong amplitude effect and would be monotone in time,
        # which is the shape everything here is trying not to fake.
        base_dy, base_dx = list(w.dy_s), list(w.dx_s)
        if scale_sweep:
            print(f'  sweeping the curve scale per magazine over {scale_sweep}, '
                  f'off ONE fitted curve at a fixed {rig.mouse.RECOIL_FIRE_DELAY_MS:+g} ms')

        grabber = DXGISyncGrabber(rig.tracker.regions())
        for i in range(a.mags):
            mag_size, _ = rig.fire.top_up()
            if not mag_size:
                print(f'  mag {i}: no ammo counter — stopping')
                break
            if prior and mag_size not in prior:
                print(f'  [!] REFUSING: this magazine holds {mag_size} rounds '
                      f'and every {a.weapon} magazine already stored holds '
                      f'{sorted(prior)}. The capacity IS the magazine (the icon '
                      f'reads 591.9 MSE against 32-51 for every other slot, so '
                      f'it cannot say), and a cell whose burst is a different '
                      f'LENGTH cannot be compared with the others — a base '
                      f'magazine looks like a very effective attachment.\n'
                      f'      Fit the same magazine, or start a separate study '
                      f'and say so.')
                return 9
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
            # ⚠ THE SWEEP ROTATES PER MAGAZINE, AND THAT IS THE WHOLE DESIGN.
            # The first offset sweep ran one offset per RUN, and `--from-fit`
            # re-fitted at the top of each run off a store that had just grown
            # by five magazines -- so the curve moved WITH the offset (917.9,
            # 917.9, 917.9, then 943.3) and the arms stopped being comparable.
            # The slope came out at 64% of the linear prediction and the four
            # points would not lie on a line.
            #
            # Here the fit happened once, above, and only the offset changes.
            # Rotating per magazine also interleaves the arms in TIME, so any
            # drift over the session -- the range emptying, the light moving,
            # the frame rate wandering 110..150 -- lands on every arm equally
            # instead of on whichever ran last.
            if sweep:
                d = sweep[i % len(sweep)]
                rig.mouse.RECOIL_FIRE_DELAY_MS = d
                rig.arm(w)
                curve = rig.mouse.read_pattern() or []
                if not curve:
                    print(f'  mag {i}: the firmware took no pattern — stopping')
                    break
                print(f'  mag {i}: fire delay {d:+g} ms '
                      f'(curve starts at t={curve[0]["t_ms"]} ms, '
                      f'{len(curve)} knots, {sum(k["dy"] for k in curve):.0f} '
                      f'counts)')
            mag_scale = a.scale
            if scale_sweep:
                mag_scale = scale_sweep[i % len(scale_sweep)]
                w.dy_s = [v * mag_scale for v in base_dy]
                w.dx_s = [v * mag_scale for v in base_dx]
                rig.arm(w)
                curve = rig.mouse.read_pattern() or []
                if not curve:
                    print(f'  mag {i}: the firmware took no pattern — stopping')
                    break
                # ⚠ THE TOTAL IS READ BACK, NOT COMPUTED. The scale that
                # matters is the one the firmware holds, and int16 quantisation
                # with a carry sits between the multiply and the wire. Printing
                # the commanded number here would describe the request; this
                # prints the object that fires.
                print(f'  mag {i}: scale x{mag_scale:g} '
                      f'({len(curve)} knots, '
                      f'{sum(k["dy"] for k in curve):.1f} counts, '
                      f'starts at t={curve[0]["t_ms"]} ms)')
            if not aim_and_scope(rig, a.posture):
                print(f'  mag {i}: could not re-aim — stopping')
                break
            mag, out = one_magazine(
                rig, grabber, a.weapon, mag_size, interval_s,
                [] if a.no_comp else curve, config, a.posture,
                note='no-comp' if a.no_comp else f'scale={mag_scale:g}',
                fire_delay_ms=float(rig.mouse.RECOIL_FIRE_DELAY_MS))
            # ⚠ THE MAGAZINE IS STILL WRITTEN. `ads_end` False means the
            # burst ended out of the scope, so K is wrong by ~3x -- but
            # MODEL.md's store never deletes, and a magazine dropped at
            # collection time is one the fitter's clustering can never show
            # you next to its siblings. It is RECORDED and SAID OUT LOUD; the
            # fit is what decides.
            if mag.ads_end is False:
                print(f'      [!] mag {i} ENDED OUT OF ADS — K is the scoped '
                      f'{rig.K}, so this magazine is scaled by the wrong '
                      f'constant. Stored and flagged, not dropped.')
            elif mag.ads_end is None:
                print(f'      [!] mag {i}: ADS could not be read after the '
                      f'burst — unknown, not assumed good.')
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
