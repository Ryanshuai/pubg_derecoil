"""Measure the pitch travel clamp-to-clamp, per posture, and aim at its middle.

    pixi run python tools/probe_pitch_range.py --postures standing,crouching

THE MEASUREMENT. Push the view down until the screen stops changing — that is
the bottom stop, and it is where the view now physically is, so call it 0.
Push it up, counting, until the screen stops changing again — that is the top
stop. Level is halfway between. Nothing in here interprets the scene: no sky,
no horizon, no terrain. The only question asked of the screen is "did anything
change", and the only ruler is mouse counts, which are exact.

Output goes to calibration/artifacts/pitch/pitch_range.json, which control/aim.py's goto_level()
reads: home to the bottom stop, rise by level_up, done. That replaces the
ground-to-sky band scan, which was slow, visible, and not repeatable — it kept
whatever pitch happened to have texture, so it came back 100..1900 in one run
and 800..2200 in the next, and two cells aimed at different pitches measure
different recoil.

WHY NOT THE HORIZON. This file used to find level by locating the sky/terrain
boundary and rising until it crossed the screen centre. That detector was
broken in two independent ways, each fatal, measured 2026-08-04 on
calibration/artifacts/pitch/standing_1700.png (a frame whose boundary is plainly at y≈600):

  * it excluded the screen's left and right quarters "so the HUD down either
    edge does not vote" — but the COMPASS STRIP runs across the top CENTRE,
    squarely inside what was left. Row 6 read detail 2.62 against a 2.0
    threshold, so the scan stopped there and returned 2.
  * real sky failed the test anyway. Row 100 of that frame is clear sky and
    reads 4.23.

`horizon has reached the screen centre` therefore fired 0 times in a 4-bearing,
90-step scan, and the number that got reported instead was "first height at
which any sky became visible" — which measures how tall the buildings in front
of the character are. It came back 1800 / 1500 / 200 / 1700 at four bearings
and looked like the horizon jumping around. The whole approach is gone.

WHY NOT THE VIEW TRACKER, EITHER. The obvious way to ask "did the view move" is
detector/view_tracker, and it is the wrong tool at exactly the two places this
probe has to work. Phase correlation on centre-band patches goes blind at both
clamps — bare close ground below, empty sky above — and blind reads as still.
control/aim.py's home_to_clamp() carries the scar: "the first version of this
tried to detect the stop by watching the view halt and reported the game's
entire pitch travel as 13 counts while the character was looking straight
down."

So the signal here is raw frame difference over a central crop, which does not
care about texture: a 100-count step slides the world ~155 px and changes
essentially every pixel, whether or not anything in it is correlatable.

AND EVERY STEP CARRIES ITS OWN CONTROL. Idle sway is not a constant — close
ground at the bottom stop swings far more pixels per degree of breathing than
distant sky at the top does — so a fixed "still" threshold would be too tight
at one end and too loose at the other. That is the same mistake the horizon
detector made with its literal 2.0. Instead each step measures the frame change
over its own duration a second time WITH NO COMMAND ISSUED, at the same pitch,
and moved/still is decided by the ratio. The threshold comes from the frame.

WHAT MIDPOINT = LEVEL ASSUMES. That the game clamps symmetrically about
horizontal. Two falsifiable predictions come out of it, and the run prints
both:

  * the travel does not depend on what the character is facing (--yaw runs it
    at several bearings). The old band was a property of the scenery; this one
    should be a property of the game.
  * standing and crouching give the same travel.

Prone is the one to distrust: if the game clips prone's travel at one end only,
the midpoint is not level there and nothing in the counts can tell. Its
screenshot is saved for that reason — look at it.

Run on ground you can turn around on. Unlike the old version this does not care
what is in front of the character, but it does care that the character stays
put: walking during a pass changes the screen and reads as movement.
"""
import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))

import cv2
import numpy as np

from control.session import ensure_ready
from control.aim import ViewDriver
# THE WALK ITSELF NOW LIVES ON ViewDriver, and these are aliases so the prose
# above and below still names the same things. It moved on 2026-08-06 because
# control/aim.py's goto_midline() has to put the view at the midpoint before
# EVERY magazine, and a second copy of "walk into the stop, decide moved vs
# still" is how the numbers in two files quietly stop being the same number --
# tools/CLAUDE.md's rule is that the thing to add is a control/ method, not a
# fifth loop here. What stayed is what this file is for: several bearings,
# several postures, the screenshots, and the predictions checked out loud.
from control.aim import (TRAVEL_MAX as MAX_COUNTS, TRAVEL_STEP as STEP,
                         MIDLINE_FRAC)
from config import SCREEN_W, SCREEN_H
from capture.cropper import win32_cap
from calibration.sweep import Rig, POSTURES

OUT = os.path.join(ROOT, 'calibration', 'artifacts', 'pitch')
STORE = os.path.join(OUT, 'pitch_range.json')

# --sight names a MEASUREMENT PROFILE; this names the part that gets fitted.
# They are not the same string and conflating them crashes: spawn_gun used
# --sight directly as an attachment key, so `--sight hipfire` asked the
# spawner for a part called 'hipfire'. The recoil sweep had made this exact
# split already (SIGHT_SCOPE) for the same reason, and made it again after
# forgetting -- which is why it is written down here rather than there.
#
# `hipfire` still wears a red dot. The measurement never looks through it --
# the walk happens with the button untouched -- but the gun has to be ARMED
# and in hand, which is precisely what config.py flags as missing from the old
# hipfire K ("TPP, no weapon"). ensure_posture needs one optic or another to
# read the posture icon through, and the red dot is what the range fits by
# default anyway.
SCOPE_FOR = {'hipfire': 'red_dot', 'red_dot': 'red_dot', '2x': 'scope_2x',
             '3x': 'scope_3x', '4x': 'scope_4x', 'vss_pso1': None}

# The per-step decision, the stop walk and the three-pass bracket all live on
# ViewDriver now -- see the TRAVEL block in control/aim.py, which carries the
# measured provenance of MOVED_FRAC / CTRL_MULT / BLIND_MULT with it. These
# two wrappers keep the names this file's docstring and --selftest talk about.


def step_once(rig, counts):
    return rig.view.step_once(counts)


def to_stop(rig, direction, step, label, max_counts=MAX_COUNTS):
    return rig.view.to_stop(direction, step, label, max_counts)



def band_from(rows, step):
    """Where the view tracker actually worked, in counts above the bottom stop.

    Free, and it is the number the old ground-to-sky band scan produced, so a
    file written by this probe can still be compared with one written by that
    one. It is a diagnostic here and nothing more — the aim no longer depends
    on it, which was the point.
    """
    ok = [r['at'] for r in rows if abs(r['tracked']) > step * 0.5]
    return (ok[0], ok[-1]) if ok else (None, None)


def linearity(rows, step):
    """tracked/commanded over the steps where the tracker had something to
    lock onto. A tight spread means every commanded count was consumed."""
    r = [abs(x['tracked']) / step for x in rows if abs(x['tracked']) > step * 0.5]
    if len(r) < 3:
        return None
    return float(np.median(r)), float(np.std(r))


def shoot(posture, tag, yaw):
    """Save a half-scale screenshot for a human to look at."""
    name = f'{posture}{"" if not yaw else f"_yaw{yaw:+d}"}_{tag}.png'
    frame = win32_cap((0, 0, SCREEN_H, SCREEN_W))
    cv2.imwrite(os.path.join(OUT, name),
                cv2.resize(frame, (frame.shape[1] // 2, frame.shape[0] // 2)))
    return name


def draw_weapon(rig, slots=(1, 2), timeout_s=3.0):
    """Get a gun in hand, and prove one is there. -> the slot, or None.

    Postures are read off the posture icon, which only renders while aiming,
    and aiming needs a weapon out — an empty-handed character reports 'no
    posture icon' for all three and nothing works.

    InventoryControl.hold() rather than a bare key press, because the 1/2 keys
    are SWALLOWED while the Tab screen is up (docs/game_quirks.md) and hold()
    is what brackets them with a close/open. It shares the Pico with the Rig:
    Pointer and get_mouse() hand back the same single-tenant connection.

    ensure_ads() CANNOT be the proof that a weapon is out, and the first live
    run of this file is why. AdsDetector answers "is the crosshair absent",
    and the crosshair is absent in the lobby, in a menu, and whenever no
    weapon is out — so with the game on the lobby screen it returned True, all
    three postures came back 'posture unreadable', and the crops behind the
    failures were bare scenery where the HUD should have been. An
    absence-based test says yes to everything when the screen is empty.

    So this asks for something PRESENT: the ammo counter. A number there means
    a weapon is out and the HUD is drawn. Both slots are tried because which
    one a gun lands in is the spawner's business — a fresh rack fills slot 1,
    a full one evicts to the floor (docs/game_quirks.md).
    """
    from control.inventory import InventoryControl
    ac = InventoryControl(verbose=False)
    try:
        for slot in slots:
            if not ac.hold(slot):
                continue
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < timeout_s:
                if rig.fire.read_ammo() is not None:
                    return slot if rig.gun.ensure_ads() else None
                time.sleep(0.15)
    finally:
        ac.close()
    print(f'[!] no ammo counter after holding slot(s) {list(slots)} — the rack '
          f'is empty. Spawn a gun first (control.spawner.give_many).')
    return None


def spawn_gun(weapon, sight_profile):
    """Put a gun with a sight on it in the rack. -> True if the rack has one.

    Needed because re-entering the training range EMPTIES THE RACK, and
    ensure_ready() re-enters whenever it finds the game back in the lobby —
    which it did mid-session on 2026-08-04, on an error dialog it cleared
    itself. A probe that cannot survive that has to be babysat.

    Every step is a control/ entry point, and the panel bracket is not
    optional: `sc.collapse_all()` called on a CLOSED panel collapses nothing
    and reports nothing, so give_many then clicks from a stale layout. That is
    what `ensure_panel(True)` ... `finally ensure_panel(False)` is for, and it
    is the shape control/stock.py's ensure_weapon_in_hand() uses, and it is
    the one to copy — the spawn_pair() this line used to name went with the
    recoil sweep on 2026-08-08.
    """
    from control.spawner import SpawnerControl
    from control.inventory import InventoryControl
    from control.stock import restock
    sight = SCOPE_FOR.get(sight_profile, sight_profile)
    ac = InventoryControl(verbose=False)
    try:
        with SpawnerControl() as sc:
            if not sc.ensure_panel(True):
                print('  [!] spawner panel would not open')
                return False
            try:
                sc.sync()
                sc.collapse_all()
                r = sc.give_many([weapon], switch=False, weapon_times=1)
                if not r['ok']:
                    print(f"  [!] spawner: {r['error']}")
                    return False
            finally:
                sc.ensure_panel(False)
            # loose_only: the sight ends up ON the gun, so what the pack is
            # short of is what is not already fitted. `sight` is None for a
            # weapon that carries its own optic (the VSS), and asking the
            # spawner for None is how a set of parts once got a None in it.
            if sight:
                restock(ac, sc, {sight}, loose_only=True, per=1,
                        drop_unwanted=False, verbose=False)
        with ac.tab_up():
            guns = ac.loadout()['guns']
            slot = next((n for n, w in guns.items() if w), None)
            if slot is None:
                print('  [!] spawned but the rack still reads empty')
                return False
            if not sight:
                print(f"  spawned {guns[slot]} in slot {slot}, no sight to fit")
                return True
            k = ac.ensure_kit(slot, {'scope': sight}, weapon=guns[slot])
            print(f"  spawned {guns[slot]} in slot {slot}, sight {sight} "
                  f"{'fitted' if k['ok'] else 'NOT fitted — ' + str(k['bad'])}")
            return True
    finally:
        ac.close()


def probe(rig, posture, step=None, yaw=0, base='standing', store=True):
    """Bottom stop, top stop, and back down. Returns a result dict or None.

    Three passes rather than two, and the third is nearly free because the view
    has to come back down anyway. It buys a second, independent bracket on the
    travel from the other direction: up says T is in (up_ok, up_ok+step], down
    says T is in (down_ok, down_ok+step], and the intersection is narrower than
    either. It also says whether the measurement repeats at all.
    """
    if not rig.ensure_posture(posture):
        print(f"  [!] could not reach {posture}")
        return None

    # ⚠ MEASURE IN THE STATE THE FILE WILL BE LABELLED WITH. ensure_posture
    # needs ADS -- the posture icon does not render from the hip -- so a
    # `--sight hipfire` run that just returned from it would walk both stops
    # SCOPED and store the result under 'hipfire'. That is the same shape as
    # the bug that had every run measuring a 4x through the red dot's K: the
    # label and the state came apart and nothing said so.
    want_hip = getattr(rig, 'sight', None) == 'hipfire'
    if hasattr(rig, 'ensure_hip'):
        ok = rig.gun.ensure_hip() if want_hip else rig.gun.ensure_ads()
        if not ok:
            print(f"  [!] could not put the character "
                  f"{'out of' if want_hip else 'into'} ADS — refusing rather "
                  f"than measuring one state and storing it as another")
            return None

    shots = {}
    # store: THIS is the separate measurement run, and its whole purpose is
    # that no calibration run ever has to do it. ViewDriver.goto_midline reads
    # calibration/artifacts/pitch/pitch_travel.json and does not measure.
    #
    # ⚠ AND --selftest MUST PASS store=False. It drives this same function
    # against a SYNTHETIC scene, and on 2026-08-06 the first version of this
    # line wrote the fake rig's travel (3700 counts at K=1.55, off a generated
    # gradient) straight into the real file, over a number that came from the
    # game. An offline test that can overwrite a measured fact is worse than no
    # test: it corrupts exactly the file nobody re-derives.
    travel = rig.view.measure_travel(
        posture, step=step, store=store,
        on_stop=lambda where: shots.setdefault(where, shoot(posture, where, yaw)))
    d = rig.view.travel_detail
    up_rows, down_rows, down0 = d.get('up', []), d.get('down_out', []), \
        d.get('down_in', [])
    if not travel:
        return None         # measure_travel has already said which pass failed
    lo, hi = d['bracket']
    agreed, up_ok, down_ok = d['agreed'], d['up_last_moving'], d['down_last_moving']
    bottom_shot, top_shot = shots.get('bottom'), shots.get('top')

    # WHERE THIS POSTURE'S BOTTOM STOP SITS RELATIVE TO STANDING'S, and it is
    # not bookkeeping — without it prone's midpoint is a guess.
    #
    # Measured 2026-08-04: standing and crouching both travel 3450 counts, to
    # the count. Prone travels 1450. So prone is clipped by 2000, and half of
    # 1450 is only level if the clipping took the same amount off each end —
    # which nothing in prone's own counts can say. Worse, if prone's bottom
    # stop WERE standing's, prone's whole range would be 0..1450 and level at
    # 1725 would be outside it, which is absurd: you can obviously aim at the
    # horizon lying down.
    #
    # So measure it instead of assuming it. The view is against this posture's
    # bottom stop right now. Stand up WITHOUT touching the mouse and push down
    # again: standing's stop is at or below this one, and how far the view
    # falls IS the offset. Then only standing's midpoint needs the symmetry
    # assumption, and every other posture inherits level geometrically.
    below = 0
    if posture != base:
        if rig.ensure_posture(base):
            print(f'  how far {base} can still look down from here:')
            fell, _ = to_stop(rig, +1, step, 'drop')
            below = None if fell is None else fell + step // 2
            print(f"  -> {posture}'s bottom stop sits {below} counts above "
                  f"{base}'s")
        else:
            below = None
            print(f'  [!] could not stand up to measure the offset')
        rig.ensure_posture(posture)     # the view snaps back to this stop

    # MIDLINE_FRAC, not half. The screenshot below is the ONLY check on
    # where the aim really lands, so it has to be taken at the pitch
    # goto_midline will actually use -- a shot at travel/2 would verify a
    # position nothing fires from.
    level = int(round(travel * MIDLINE_FRAC))
    # The view is at the bottom stop, which is exactly where goto_level()
    # starts from. Rise by the same amount it will and photograph the result —
    # that picture is the only check on whether the midpoint really is level.
    rig.view.turn(0, -level, settle_s=0.4)
    level_shot = shoot(posture, 'level', yaw)

    step = d.get('step', step or STEP)     # what measure_travel really used
    lin = linearity(up_rows, step)
    tf, tt = band_from(up_rows, step)
    print(f"  -> travel {travel} counts (bracket {lo}..{hi}), "
          f"level at +{level}")
    if lin:
        print(f"     count ruler: tracked/commanded {lin[0]:.3f} ± {lin[1]:.3f} "
              f"over the trackable part"
              + ('' if lin[1] < 0.05 else '  [!] not flat — see the note in '
                                          'step_once'))
    print(f"     the old band scan would have found {tf}..{tt} and aimed at "
          f"{None if tf is None else int(tf + (tt - tf) * 0.5)}")
    print(f"     look at {level_shot} — is it level?")

    return {
        'level_up': level,
        'travel': travel,
        'agreed': agreed,
        'below_base': below,
        'base': base,
        'travel_bracket': [lo, hi],
        'up_last_moving': up_ok,
        'down_last_moving': down_ok,
        'step': step,
        'yaw': yaw,
        'linearity': None if lin is None else {'median': round(lin[0], 4),
                                               'sd': round(lin[1], 4)},
        'tracks_from': tf,
        'tracks_to': tt,
        'shots': {'bottom': bottom_shot, 'top': top_shot, 'level': level_shot},
        'steps': {'down_in': down0, 'up': up_rows, 'down_out': down_rows},
    }


def summarise(result):
    """The predictions midpoint-is-level makes, checked out loud. -> trust?

    The return value GATES THE WRITE, and that is not decoration. The first
    live run printed "Do not trust level_up until this is understood" and then
    wrote the file anyway, four lines later — a travel of 550 counts for
    standing, straight into the path control/aim.py reads at import. A warning
    the program itself ignores is worth nothing.
    """
    print('\n=== travel, which should not depend on anything ===')
    for k, v in result.items():
        flag = '' if v['agreed'] else '   [!] the two passes did not overlap'
        print(f"  {k:24s} travel {v['travel']:7.0f}   level +{v['level_up']}"
              f"{flag}")
    trust = all(v['agreed'] for v in result.values())
    if not trust:
        print('  -> at least one posture measured its travel differently going '
              'up than coming down. That is one measurement disagreeing with '
              'itself; nothing else here can rescue it.')

    # WHICH TRAVELS ARE SUPPOSED TO MATCH, and the first version of this got it
    # wrong: it demanded all of them, so a run whose standing and crouching
    # agreed TO THE COUNT (3450 and 3450) was failed by prone's 1450 — which is
    # not an error, it is the game. Lying down really does cost you 2000 counts
    # of pitch. What the facing-independence prediction actually says is that
    # postures with the same travel keep it whatever they face, so the check is
    # among EQUALS: the ones sharing the largest travel.
    step = max(v['step'] for v in result.values())
    top = max(v['travel'] for v in result.values())
    full = {k: v for k, v in result.items() if top - v['travel'] <= step}
    if len(full) > 1:
        spread = max(v['travel'] for v in full.values()) - \
            min(v['travel'] for v in full.values())
        print(f"\n  {', '.join(full)} agree to within {spread:.0f} counts "
              f"(one step is {step}) -> the travel is a property of the game, "
              f"not of the scenery.")
    clipped = {k: v for k, v in result.items() if k not in full}
    for k, v in clipped.items():
        print(f"  {k} is clipped by {top - v['travel']:.0f} counts")

    # Only the base posture's midpoint rests on the symmetry assumption. Every
    # other one is placed by measurement: its bottom stop was found to sit
    # `below_base` counts above the base's, so the same horizontal is
    # `level_base - below_base` above ITS bottom stop. That is what makes a
    # clipped posture usable instead of a guess.
    base = next((v['base'] for v in result.values()), None)
    lvl = result.get(base, {}).get('level_up')
    if lvl is None and len(result) > 1:
        # Everything below places itself against the base posture. Without it
        # the other postures keep half their OWN travel, which is a different
        # quantity that happens to look like an answer — and on 2026-08-04
        # this branch wrote exactly that, because standing had failed its blind
        # check and nothing noticed the anchor was missing.
        trust = False
        print(f"\n  [!] {base} was not measured, so nothing can be placed "
              f"against it. The level_up values above are each half their own "
              f"posture's travel, which is not the same quantity.")
    if lvl is not None:
        print(f"\n=== level, placed against {base}'s bottom stop ===")
        for k, v in result.items():
            b = v['below_base']
            if b is None:
                trust = False
                print(f"  {k:12s} offset NOT MEASURED — its level_up is "
                      f"half its own travel, which is only level if the clip "
                      f"is symmetric. Refusing to trust it.")
                continue
            up = lvl - b
            v['level_up'], v['level_from_midpoint'] = int(up), b == 0
            inside = 0 <= up <= v['travel']
            print(f"  {k:12s} bottom stop +{b:5d}   level at +{up:5d} of its "
                  f"own {v['travel']:.0f} of travel"
                  + ('' if inside else '   [!] OUTSIDE its range'))
            if not inside:
                trust = False
    print(f"\n  Still assumed, and not checkable from counts alone: that "
          f"{base}'s clamps sit symmetrically about horizontal. Everything "
          f"else is placed against them by measurement. Look at "
          f"{base}_level.png.")
    return trust


class _FakeRig:
    """A game with a known pitch travel, for --selftest.

    The scene is deliberately hostile in exactly the way the real one is:
    smooth sky at the top, fine detail at the bottom. If the frame-difference
    signal only worked on textured content it would fail here at the top stop,
    which is the whole failure mode this probe was built to survive.
    """

    def __init__(self, travel, start, K=1.55, noise=0.4, flat_rows=0,
                 up_gain=1.0):
        self.travel, self.pos, self.K, self.noise = travel, start, K, noise
        # How much of a commanded count the view actually consumes going UP.
        # 1.0 is a perfectly symmetric ruler, which is what the fake was until
        # 2026-08-06 -- and a symmetric fake cannot tell "aim on the up pass"
        # apart from "average the two passes", so it silently passed both.
        # calibrate_k re-measured the real up/down asymmetry at 5.37%.
        self.up_gain = up_gain
        rng = np.random.default_rng(7)
        h = int(936 + travel * K) + 16
        # bottom (high row index) = close ground, fine detail; top = sky, a
        # smooth vertical gradient with almost no high-frequency content.
        ramp = np.linspace(210, 120, h)[:, None] * np.ones((1, 1720))
        detail = rng.normal(0, 30, (h, 1720))
        weight = np.clip(np.linspace(0.0, 1.0, h)[:, None] ** 3, 0, 1)
        self.scene = np.clip(ramp + detail * weight, 0, 255).astype(np.uint8)
        # flat_rows makes the top of the sky UNIFORM: no gradient either. This
        # is the case the probe must refuse rather than answer — sliding a
        # blank picture leaves a blank picture, so "nothing changed" carries no
        # information about whether the view moved.
        if flat_rows:
            self.scene[:flat_rows] = 200
        self.rng = rng
        self.tracker = self
        self.moves = 0
        self.last = 0
        # A REAL ViewDriver over a fake world. The point of the selftest is to
        # exercise the code harvest actually runs, and since 2026-08-06 that is
        # ViewDriver.measure_travel -- a fake that reimplemented the walk would
        # only be testing itself. `world_fn` is the seam; everything else the
        # driver touches (mouse, tracker, frames) is this object.
        self.view = ViewDriver(self, self, self, K=K)
        self.view.world_fn = self.frame

    # -- the surface ViewDriver actually touches --
    def frame(self):
        top = int(round((self.travel - self.pos) * self.K))
        out = self.scene[top:top + 936].astype(np.float32)
        return np.clip(out + self.rng.normal(0, self.noise, out.shape),
                       0, 255).astype(np.uint8)

    def move(self, dx, dy):
        """ViewDriver.turn() goes out through the mouse, and this is it. The
        clamping IS the thing under test: counts past a stop are swallowed."""
        self.moves += 1
        want = -dy * (self.up_gain if dy < 0 else 1.0)
        self.last = max(0, min(self.travel, self.pos + want)) - self.pos
        self.pos += self.last

    def grab(self):
        return None

    def slice_frame(self, _):
        # None all the way through, so step_once takes its `tracked` shortcut
        # and never enters the correlator. That is honest rather than lazy: the
        # tracker DECIDES NOTHING here, and a fake reading fed into the
        # `linearity` line would be this file reporting its own arithmetic as
        # evidence about whether PUBG consumes every count it is sent.
        return None

    def flush(self, n=8):
        pass

    def ensure_posture(self, p, tries=4):
        return True


def _selftest():
    """Drive probe() against _FakeRig and check the arithmetic, offline.

    What this can prove: the bracket maths, that a dead input is refused rather
    than reported as a travel of zero, and that the moved/still call survives a
    featureless top stop. What it cannot: that PUBG consumes every count it is
    sent, which is what the `linearity` line in a live run is for.
    """
    g = globals()
    real_sleep, real_shoot = time.sleep, g['shoot']
    fails = []
    cases = [               # travel, start, flat sky rows, expected level
        (3000, 1500, 0, 1500),
        (2600, 100, 0, 1300),
        (3000, 3000, 0, 1500),     # starts jammed against the top stop
        (1700, 800, 0, 850),
        (0, 0, 0, None),           # nothing takes input
        (3000, 1500, 1500, None),  # blank sky: must refuse, not read short
    ]
    try:
        time.sleep = lambda *_a, **_k: None
        for travel, start, flat, want in cases:
            # `want` in the table is just 'is there an answer'; the value
            # follows MIDLINE_FRAC so this table does not have to be
            # re-typed every time the aim fraction is re-checked by eye.
            want = None if want is None else round(travel * MIDLINE_FRAC)
            rig = _FakeRig(travel, start, flat_rows=flat)
            g['shoot'] = lambda *a, **k: 'x.png'
            r = probe(rig, 'standing', step=STEP, store=False)
            got = None if r is None else r['level_up']
            ok = (got is None and want is None) or \
                 (got is not None and want is not None and
                  abs(got - want) <= STEP // 2)
            print(f"  travel {travel:5d} from {start:5d} flat {flat:5d} -> "
                  f"level {got}  (want {want})  {'ok' if ok else 'FAIL'}")
            if not ok:
                fails.append((travel, start, got, want))

        # AN ASYMMETRIC COUNT RULER. Going up costs more commands than coming
        # down, which is what a 5.37% up/down gap in K means in practice. The
        # aim has to come out of the UP pass, because up is the only direction
        # goto_midline ever travels; averaging the two passes lands the midline
        # short by a quarter of the gap, and nothing downstream would say so.
        # The check is on where the view ACTUALLY ends up, not on the counts.
        print('\n  an asymmetric ruler still lands level:')
        for travel, gain in ((3000, 0.95), (3000, 1.05), (3450, 0.947)):
            rig = _FakeRig(travel, travel // 3, up_gain=gain)
            r = probe(rig, 'standing', step=STEP, store=False)
            level = None if r is None else r['level_up']
            # Command the midline the way harvest does, then ask the fake where
            # the view is. Half the travel in ANGLE is what has to be hit.
            rig.pos = 0
            if level:
                rig.move(0, -level)
            off = None if level is None else rig.pos - travel * MIDLINE_FRAC
            ok = off is not None and abs(off) <= STEP // 2
            print(f"    travel {travel} up_gain {gain}  -> rose {level} "
                  f"commanded, view {off:+.0f} off level"
                  if off is not None else
                  f"    travel {travel} up_gain {gain}  -> no answer")
            print(f"      {'ok' if ok else 'FAIL'}")
            if not ok:
                fails.append(('asym', travel, gain, level, off))

        # goto_midline() is what harvest calls before EVERY magazine, and its
        # failure is the silent kind: the view ends up somewhere either way,
        # and a magazine fired 700 counts low reads as a mild gun rather than
        # as a broken aim. So drive it against a known travel from three very
        # different starting pitches -- including jammed against the top stop,
        # which is where a burst leaves the view -- and check where it stopped.
        print('\n  goto_midline lands at half the travel:')
        for travel, start in ((3000, 0), (3000, 2999), (1450, 700)):
            rig = _FakeRig(travel, start)
            # Keyed (sight, posture) since 2026-08-06: goto_midline positions
            # from the hip whatever optic is up, so the sight it looks the
            # travel up under is an argument, not self.sight.
            rig.view._travel = {('red_dot', 'standing'): travel}
            half = rig.view.goto_midline()
            want = round(travel * MIDLINE_FRAC)
            ok = half == want and abs(rig.pos - want) <= 1
            print(f"    travel {travel:5d} from {start:5d} -> rose {half}, "
                  f"view at {rig.pos}  {'ok' if ok else 'FAIL'}")
            if not ok:
                fails.append(('midline', travel, start, half, rig.pos))
    finally:
        time.sleep, g['shoot'] = real_sleep, real_shoot
        g['time'] = sys.modules['time']
    print('selftest:', 'all ok' if not fails else f'{len(fails)} FAILED')
    return 0 if not fails else 1


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--postures', default='standing,crouching,prone')
    ap.add_argument('--yaw', default='0',
                    help='comma list of yaw offsets in counts from where the '
                         'character starts. This is the validation, not a '
                         'setting: the travel between two hard stops should '
                         'be the same at every bearing, and the old band was '
                         'not. More than one bearing writes the scan file '
                         'instead of pitch_range.json.')
    # DEFAULT None, NOT STEP. measure_travel scales the step by the sight's
    # pitch_scale so every profile takes about the same number of steps; passing
    # STEP unconditionally overrode that, and on 2026-08-06 that is exactly how
    # the hip-fire run died. `predict` -- what a step WOULD change the picture
    # by -- is proportional to the step, so a step 3x too small makes predict 3x
    # too small, and up near the sky where the weapon model's idle sway pushes
    # `control` to 11.5 the ratio fell to 1.79 against the 2.0 blind floor. The
    # probe refused, correctly, at 7000 counts with the view still moving.
    ap.add_argument('--step', type=int, default=None,
                    help='counts per probe. Default: scaled per sight, which '
                         'is what keeps `predict` above the idle sway.')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--slot', type=int, default=0,
                    help='weapon slot to hold; 0 tries 1 then 2')
    ap.add_argument('--countdown', type=int, default=5)
    ap.add_argument('--dry-run', action='store_true',
                    help='measure and print, write nothing')
    ap.add_argument('--weapon', default='m416',
                    help='what to spawn if the rack is empty')
    ap.add_argument('--no-spawn', action='store_true',
                    help='fail instead of spawning a gun when the rack is '
                         'empty')
    ap.add_argument('--force', action='store_true',
                    help='write even when the consistency checks failed')
    ap.add_argument('--selftest', action='store_true',
                    help='check the arithmetic against a simulated game; '
                         'touches neither the real one nor the Pico')
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    postures = [p.strip() for p in args.postures.split(',') if p.strip()]
    bad = [p for p in postures if p not in POSTURES]
    if bad:
        print(f'[!] unknown posture(s): {bad}')
        return 1
    yaws = [int(y) for y in args.yaw.split(',') if y.strip()]
    os.makedirs(OUT, exist_ok=True)

    print('>>> Stand still. What the character faces no longer matters; that '
          'it does not walk does.')
    # Focus, in a match, Tab down, panel down. Focused is not playable, and
    # this probe's first live run is why control/session.py exists: it drove
    # three postures' worth of state machine into the LOBBY SCREEN and
    # reported 'posture unreadable' three times. Runs before the Rig so the
    # Pointer it opens is closed before the Rig takes the serial port.
    ready = ensure_ready(label='the pitch probe', countdown_s=args.countdown)
    if not ready['ok']:
        print(f"[!] the game is not ready to be driven ({ready['failed']})")
        return 1

    rig = Rig(args.sight)
    # Lets ensure_posture nudge the view when the posture icon cannot be read
    # (it fails on pale wood — see detector/CLAUDE.md). Safe here in a way it
    # is not during a cell: the nudge destroys the view driver's running total,
    # and this probe keeps no running total to destroy.
    rig.view.use_homing = True
    result = {}
    # Counts are not comparable between optics: a 4x buys 3.3x the rotation per
    # count, so the travel measured through one is 3.3x the travel measured
    # through the other. goto_level() multiplies the stored number by
    # pitch_scale() for whatever optic is up, so what is stored has to be red
    # dot counts. Read before the rig closes.
    scale = rig.view.pitch_scale()
    try:
        want = (args.slot,) if args.slot else (1, 2)
        slot = draw_weapon(rig, slots=want)
        if slot is None and not args.no_spawn:
            # An empty rack is the normal state of a freshly entered training
            # range, and ensure_ready() re-enters it whenever it finds the
            # game back in the lobby. Spawning here is what makes the probe
            # survive that instead of stopping to be handed a gun.
            print('  rack is empty — spawning one')
            if spawn_gun(args.weapon, args.sight):
                slot = draw_weapon(rig, slots=want)
        if slot is None:
            return 1
        print(f'  weapon in slot {slot}, scoped in')
        turned = 0
        for yaw in yaws:
            if len(yaws) > 1:
                rig.view.turn(yaw - turned, 0, settle_s=0.4)
                turned = yaw
                print(f'\n=== yaw {yaw:+d} counts from the start ===')
            for p in postures:
                print(f'\n{p}:')
                r = probe(rig, p, step=args.step,
                          yaw=yaw if len(yaws) > 1 else 0,
                          base=postures[0])
                if r:
                    result[p if len(yaws) == 1 else f'{p}@yaw{yaw:+d}'] = r
    finally:
        try:
            rig.ensure_posture('standing')
        except Exception:
            pass
        rig.close()

    if not result:
        print('\n[!] nothing measured')
        return 1
    trust = summarise(result)

    if abs(scale - 1.0) > 1e-6:
        print(f"\n[!] measured through {args.sight}, which needs {scale:.3f}x "
              f"the counts the red dot does for the same rotation. Dividing "
              f"through, so the file stores red dot counts and goto_level() "
              f"can scale them back up for whatever optic is up.")
    if args.dry_run:
        print('\n--dry-run: nothing written')
        return 0
    if not trust:
        print('\n[!] NOT WRITING. The checks above failed, and control/aim.py '
              'reads this file at import — a level_up written here is what '
              'every cell aims at. Fix what the summary named and run it '
              'again, or pass --force if you know better than the check.')
        return 1

    if len(yaws) > 1:
        # A yaw scan is an experiment about facings, not a measurement of level,
        # and STORE is read by control/aim.py keyed by bare posture names.
        # Writing `standing@yaw+3000` there leaves a file whose every key
        # misses, silently replacing whatever was measured before — which is
        # what happened the first time this flag existed, and pitch_range.json
        # is not in git to restore from.
        scan = STORE.replace('.json', '_yaw_scan.json')
        json.dump(result, open(scan, 'w', encoding='utf-8'), indent=2)
        print(f'\nyaw scan -> {os.path.relpath(scan)}  (NOT '
              f'{os.path.basename(STORE)}: that file is keyed by posture and '
              f'read by goto_level)')
        return 0

    if os.path.exists(STORE):
        shutil.copyfile(STORE, STORE + '.prev')
        print(f'\nprevious {os.path.basename(STORE)} -> '
              f'{os.path.basename(STORE)}.prev  (this file is not in git; a '
              f'bad run must not be the only copy)')
    payload = {'_method': 'clamp-to-clamp midpoint, tools/probe_pitch_range.py',
               '_sight': args.sight,
               '_counts_are': 'red dot counts above the bottom pitch stop'}
    payload.update({k: dict(v, level_up=int(round(v['level_up'] / scale)))
                    for k, v in result.items()})
    json.dump(payload, open(STORE, 'w', encoding='utf-8'), indent=2)
    print(f'wrote {os.path.relpath(STORE)}')
    print(f'\n[!] control/aim.py reads this at import. goto_level() now takes '
          f'over from the ground-to-sky band scan for '
          f'{", ".join(result)} — every cell measured from now on aims at '
          f'these counts. Check the level shots before running a sweep.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
