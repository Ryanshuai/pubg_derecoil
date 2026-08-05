"""When is the posture icon readable, and does it agree with the character?

WHY NOT MORE ICON SAMPLES. Recognition is not the problem. Over the 1714
labelled crops the current reader scores 0.993, and three alternatives
(background-subtracted mask, corpus-solved templates, both) land between 0.993
and 0.995 — none wins, and none changes the one recorded "failure", because
that crop is labelled prone and DRAWS A CROUCH. More icons cannot move a
number that is already right.

WHAT IS PROBABLY WRONG IS THE TIMING. config.py reads posture on three events:

    c press          + 200 ms
    z press          + 200 ms
    right RELEASE    + 350 ms

and docs/game_quirks.md records that the icon is drawn ONLY while ADS is up.
Pressing C usually happens before aiming, and 350 ms after letting go of RMB
the sight is long gone — so all three may sample a moment when the icon is not
on screen. A read of None is discarded by GameState.set_posture, the STALE
posture survives, and compensation runs on a factor wrong by up to 2x
(standing 1.0 against prone 0.50). That is a vertical swing mid-burst.

⚠ THE TRAP THIS PROBE HAD TO BE REDESIGNED AROUND. C and Z are TOGGLES, so
after a press the probe does not know which posture the character is in — and
the only reader that could say is the icon under test. Judging the icon by the
icon is circular, and this repo has already been burnt by exactly that.

So posture truth comes from somewhere the icon cannot reach: THE CAMERA
HEIGHT. Crouching and going prone lower the viewpoint, which slides the world
UP the screen, and ViewTracker measures that with phaseCorrelate on the
centre band — no mouse input, no icon, no templates.

    dy < 0 over the transition   went LOWER   (standing->crouch->prone)
    dy > 0                        went HIGHER (stood up)
    dy ~ 0                        THE KEY NEVER ARRIVED — itself a finding

Two presses of Z home the character to standing without ever reading the icon,
after which every toggle's destination is known and the icon can be JUDGED
rather than trusted.

⚠ And a caveat on that: a camera height change is a 3-D translation, so near
objects move further than far ones and it is not the pure translation
phaseCorrelate assumes. The magnitude is therefore not a physical quantity.
Only the SIGN and the PRESENCE are used here, and step 0 exists to check that
even those survive — if the transitions do not separate, this whole approach
is wrong and the probe says so instead of reporting numbers.

    pixi run python tools/probe_posture_trace.py
    pixi run python tools/probe_posture_trace.py --shots 0   # numbers only

Output: docs/posture/traces/<stamp>/{trace.jsonl, *.png}
"""
import argparse
import json
import os
import random
import sys
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from calibration.sweep import Rig                                  # noqa: E402
from control.inventory import InventoryControl                     # noqa: E402
from control.session import ensure_ready                           # noqa: E402
from control.spawner import SpawnerControl                         # noqa: E402
from control.stock import ensure_weapon_in_hand                    # noqa: E402
from detector.posture_detector import (MIN_AREA,                   # noqa: E402
                                       _extract_silhouette, _load_templates)
from press.pico_mouse import (get_mouse, other_agents,            # noqa: E402
                              HID_KEY_C, HID_KEY_Z)

OUT_ROOT = os.path.join(ROOT, 'docs', 'posture', 'traces')
WATCH_S = 2.0
SAMPLE_HZ = 60
# A transition has to clear this much accumulated camera travel to count as
# real. Step 0 reports what the transitions actually measure, so this is a
# starting guess to be replaced by a measured one -- it is deliberately small,
# because "the key never arrived" is the finding it must not hide.
MOVE_MIN_PX = 6.0
# Raw counts per random view swing. Yaw is unbounded; pitch is NOT — swing far
# enough up and the backdrop is empty sky, far enough down and it is the
# character's own back, and neither is a backdrop the HUD is ever read against
# in a real run.
YAW_MAX = 3000
PITCH_MAX = 400

# What the CAMERA established the posture to be for each watch window. Every
# entry here is the destination of a transition whose direction step 0/1
# verified — nothing in this table came from reading the icon, which is the
# whole point of the probe. `*_ads_idle` is the posture BEFORE that window's
# key, and it is standing both times because each pair re-homes first.
EXPECT = {
    'to_crouch': 'crouching', 'to_prone': 'prone',
    'crouch_ads_idle': 'standing', 'crouch_after_key': 'crouching',
    'prone_ads_idle': 'standing', 'prone_after_key': 'prone',
}


def _ious(crop, templates):
    """All three IoUs, not just the winner: 'read the wrong posture' and
    'could not read' are the same word in the verdict and nothing alike here."""
    mask = _extract_silhouette(crop)
    out = {'area': int(mask.sum())}
    if out['area'] < MIN_AREA:
        return out
    for cls, t in templates.items():
        if t.shape != mask.shape:
            t = cv2.resize(t, (mask.shape[1], mask.shape[0]),
                           interpolation=cv2.INTER_NEAREST)
        inter = int((mask & t).sum())
        union = int((mask | t).sum())
        out[cls] = round(inter / union if union else 0.0, 3)
    return out


class Watcher:
    """Samples icon + ADS + camera travel into one trace.

    The frame source is the Rig's BANDED grabber, not `capture_screen()`. The
    first live run used the full 3440x1440 grab and managed 13 samples in 2000
    ms — 6.5 Hz, a 150 ms grid, on a probe whose entire output is millisecond
    thresholds. The bands carry exactly the three things read here (the posture
    icon, the crosshair window, the tracker patches) and `ScreenBuffer` raises
    on lost focus instead of handing back the frozen picture PUBG leaves up.
    """

    def __init__(self, rig, out_dir, shots):
        self.rig = rig
        self.out_dir = out_dir
        self.shots = shots
        self.det = rig.posture_det
        self.ads = rig.ads_det
        self.tracker = rig.tracker
        self.templates = _load_templates()
        self.rows = []
        # Which backdrop this sample was taken against. Labels repeat across
        # rounds on purpose — the whole point is the same measurement at
        # several viewpoints — so without this they would pool into one line
        # and a background-dependent failure would average away.
        self.round = 0

    def watch(self, label, seconds=WATCH_S):
        """-> (rows, cumulative_dy). Call immediately after driving a key."""
        rows, cum, prev = [], 0.0, None
        t0 = time.perf_counter()
        shot_at = [0.10, 0.30, 0.80, 1.5][:self.shots]
        nxt = 0
        while True:
            el = time.perf_counter() - t0
            if el > seconds:
                break
            frame = self.rig.grab()
            cur = self.tracker.slice_frame(frame)
            if prev is not None and cur is not None:
                m = self.tracker.measure_pair(prev, cur)
                if np.isfinite(m.dy) and not m.out_of_range and m.n_valid:
                    cum += m.dy
            prev = cur

            pc = frame['posture']
            rec = {'label': label, 'round': self.round,
                   'ms': round(el * 1000, 1),
                   'posture': self.det.classify({'posture': pc}),
                   'ads': bool(self.ads.scoped_crop(frame['crosshair'])),
                   'cum_dy': round(cum, 2)}
            rec.update(_ious(pc, self.templates))
            if nxt < len(shot_at) and el >= shot_at[nxt]:
                fn = f'r{self.round}_{label}_{int(el * 1000):04d}ms.png'
                # The posture crop itself, not the screen: it is the thing
                # being judged, and at 3440x1440 the icon is 60 px of a 5 MB
                # file nobody zooms into.
                cv2.imwrite(os.path.join(self.out_dir, fn), pc)
                rec['shot'] = fn
                nxt += 1
            rows.append(rec)
            time.sleep(max(0.0, 1.0 / SAMPLE_HZ
                           - (time.perf_counter() - t0 - el)))
        self.rows += rows
        return rows, cum


def home_to_standing(m, w):
    """Get the character upright using the CAMERA only. -> (ok, notes)

    Z toggles prone. From anywhere, one press either drops the view (now
    prone) or raises it (now standing); a second press from prone finishes the
    job. Nothing here reads the posture icon, which is the point.
    """
    notes = []
    for attempt in range(3):
        m.key(HID_KEY_Z, 60)
        _, dy = w.watch(f'home{attempt}')
        notes.append(f'Z -> cum_dy {dy:+.1f}px')
        if abs(dy) < MOVE_MIN_PX:
            notes.append('the view did not move: the key did not arrive')
            return False, notes
        if dy > 0:                      # content slid down = view rose
            return True, notes
    return False, notes + ['never settled upright in 3 presses']


def _fmt(v):
    return f'{v:.0f}' if v is not None else '  --'


def summarise(rows, label, expect=None):
    """One line per (label, round). The failures are DIFFERENT THINGS.

    'never readable', 'readable but wrong', and 'still reporting the old
    posture' all end up downstream as one stale factor, and only the last is
    fixed by moving a delay. A `*_after_key` window legitimately contains BOTH
    postures — it straddles the change — so demanding a single value there
    reports a disagreement that is really the measurement working. What that
    window is asked for instead is WHEN IT FLIPS: the last stale read and the
    first correct one bracket the delay a trigger has to clear.
    """
    ok = [r for r in rows if r['posture']]
    span = rows[-1]['ms'] if rows else 0
    if not ok:
        return (f'  {label:16} icon NEVER readable over {span:.0f} ms   '
                f'(ADS up at any point: {any(r["ads"] for r in rows)})')

    head = (f'  {label:16} readable {len(ok):3d}/{len(rows):3d} samples'
            f'  ({span:.0f} ms window)')
    read = sorted({r['posture'] for r in ok})
    if expect is None:
        return f'{head}  read {"/".join(read)}'
    if not label.endswith('_after_key'):
        return (f'{head}  read {"/".join(read)}'
                + ('  AGREES' if read == [expect]
                   else f'  !! DISAGREES, camera says {expect}'))

    stale = [r for r in ok if r['posture'] != expect]
    hit = [r for r in ok if r['posture'] == expect]
    if not hit:
        return (f'{head}  !! NEVER reached {expect}; read '
                f'{"/".join(read)} throughout')
    # Anything read AFTER the flip that is not `expect` means it went back —
    # a different defect from a slow flip, and one a delay cannot fix.
    late = [r for r in stale if r['ms'] > hit[0]['ms']]
    return (f'{head}  last stale {_fmt(stale[-1]["ms"] if stale else None)} ms'
            f' -> first {expect} {hit[0]["ms"]:.0f} ms'
            + (f'  !! {len(late)} sample(s) reverted afterwards' if late
               else ''))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--shots', type=int, default=4)
    ap.add_argument('--weapon', default='m416')
    ap.add_argument('--sight', default='hipfire',
                    help='Rig sight profile; only the tracker patches matter '
                         'here, nothing in this probe uses K')
    ap.add_argument('--rounds', type=int, default=6,
                    help='backdrops to repeat the measurement against')
    ap.add_argument('--seed', type=int, default=0,
                    help='the view swings are random but REPRODUCIBLE; a '
                         'result that only appears at one seed is a result '
                         'about that viewpoint')
    ap.add_argument('--idle-s', dest='idle_s', type=float, default=0.6,
                    help='the sight-up-no-change window; short because it '
                         'only has to show the icon is there at all')
    a = ap.parse_args()

    # One Pico, one game window, several agents. Refusing here is the point:
    # taking focus mid-collection silently zeroes the other run.
    busy = other_agents()
    if busy:
        print('another agent holds the game / Pico — not taking focus:')
        for b in busy:
            print(f'  {b}')
        return 1

    r = ensure_ready(label='probe_posture_trace')
    if not r['ok']:
        print(f'not ready: {r.get("failed")}')
        return 1

    # A weapon HAS to be out before any of this means anything, and the first
    # live run is why this line exists: `ensure_ready` leaves the character
    # empty handed, and an empty-handed HUD draws no posture icon AND no
    # crosshair. The probe then reported "icon NEVER readable" for all ten
    # transitions with "ADS up: True" throughout — a confident, wrong,
    # entirely explainable answer to a question nobody could have asked.
    with SpawnerControl() as sc:
        ac = InventoryControl(verbose=False)
        try:
            slot = ensure_weapon_in_hand(ac, sc, weapon=a.weapon)
        finally:
            ac.close()
    if slot is None:
        print('no weapon in hand — nothing below could be measured')
        return 1
    print(f'weapon out in slot {slot}')

    stamp = time.strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(OUT_ROOT, stamp)
    os.makedirs(out_dir, exist_ok=True)

    rig = Rig(a.sight)
    w = Watcher(rig, out_dir, a.shots)
    # get_mouse(), not PicoMouse(): everything above already opened the port,
    # and COM10 admits exactly one handle. Constructing a second one fails with
    # "something else already has it" and names the holder "unknown" — which is
    # this very process. The Rig holds the same singleton.
    m = get_mouse()
    try:
        print('\n-- step 0/1: home to standing, using the camera only')
        ok, notes = home_to_standing(m, w)
        for n in notes:
            print(f'   {n}')
        if not ok:
            print('   [!] could not establish a known posture — everything '
                  'below would be guessing, so stopping here.')
            _dump(w, out_dir)
            return 1

        print('\n-- step 0: what does each transition move the camera by')
        steps = {}
        for label, key in (('to_crouch', HID_KEY_C), ('to_prone', HID_KEY_Z)):
            m.key(key, 60)
            _, dy = w.watch(label)
            steps[label] = dy
            print(f'   {label:10} cum_dy {dy:+7.1f} px')
            # NOT a bare Z. Z from a CROUCH goes to prone, not upright, so the
            # first version of this measured `to_prone` starting from prone —
            # and reported +142 px (standing up) for a transition named "go
            # prone". home_to_standing is the camera-verified way back.
            if not home_to_standing(m, w)[0]:
                print(f'   [!] could not get upright after {label}')
                _dump(w, out_dir)
                return 1
        if all(abs(v) < MOVE_MIN_PX for v in steps.values()):
            print('   [!] no transition moved the view — the camera signal '
                  'does not work here, so this approach is wrong. Stopping.')
            _dump(w, out_dir)
            return 1

        print('\n-- step 2: SIGHT UP FIRST, then move, then watch')
        # RIGHT CLICK IS A TOGGLE, not a hold (control/gun.py:216), and the
        # first live attempt got that wrong in a way worth recording: it did
        # `click(0x02, 1200)` expecting a 1.2 s hold, so the click TOGGLED ADS
        # ON and the button duration meant nothing. With the icon appearing
        # ~0.85 s after a click, ADS then showed up one window LATE and the
        # next click toggled it back OFF — the trace read `ads=False` during
        # `crouch_ads` and `ads=True` during `prone_hipfire`, exactly inverted,
        # and every window still read the icon as absent.
        #
        # So: get the sight up through the driver that watches it to
        # completion, prove it, and only then move. Anything measured while
        # ADS is merely REQUESTED is measuring the ADS animation.
        # ROUNDS, EACH BEHIND A DIFFERENT BACKDROP. One viewpoint measures one
        # background, and background is exactly what this detector has a
        # history with: the silhouette is "near-white and low-saturation", and
        # the training range's pale wood passes the same gate. A latency
        # measured against one patch of sky says nothing about the wall.
        # ViewDriver.turn() is the named open-loop swing that exists for this —
        # where the view lands is irrelevant, different is the requirement.
        rnd = random.Random(a.seed)
        for rd in range(a.rounds):
            w.round = rd
            if rd:
                yaw = rnd.randint(-YAW_MAX, YAW_MAX)
                pitch = rnd.randint(-PITCH_MAX, PITCH_MAX)
                print(f'   round {rd}: turning yaw {yaw:+5d} pitch {pitch:+4d}')
                rig.view.turn(yaw, pitch, settle_s=0.35)
            for name, key in (('crouch', HID_KEY_C), ('prone', HID_KEY_Z)):
                # Re-home between pairs. The crouch pair leaves the character
                # crouching, and Z from a crouch is a different transition than
                # Z from standing — EXPECT would then describe a posture nobody
                # established.
                ok, notes = home_to_standing(m, w)
                if not ok:
                    print(f'   [!] could not re-home before {name}: {notes[-1]}')
                    return _finish(w, out_dir, a)
                if not rig.gun.ensure_ads():
                    print(f'   [!] could not confirm ADS before {name} — '
                          f'stopping rather than reporting a window that was '
                          f'never aimed')
                    return _finish(w, out_dir, a)
                # With the sight already up and the posture unchanged, the icon
                # should simply BE there. If it is not, this is not timing.
                w.watch(f'{name}_ads_idle', seconds=a.idle_s)
                m.key(key, 60)
                w.watch(f'{name}_after_key')

        # Leave the character upright and the sight down for whoever is next.
        m.key(HID_KEY_Z, 60)
        time.sleep(0.4)
        if rig.gun.in_ads():
            m.click(buttons=0x02, duration_ms=60)
    finally:
        rig.close()

    return _finish(w, out_dir, a)


def _finish(w, out_dir, a):
    _dump(w, out_dir)
    print('\n=== when is the icon readable ===')
    print('    (*_ads_idle = sight already up, posture unchanged; '
          '*_after_key = the posture key pressed at t=0 with the sight up)')
    seen = dict.fromkeys((r['round'], r['label']) for r in w.rows)
    for rd, label in seen:
        rows = [x for x in w.rows
                if x['label'] == label and x['round'] == rd]
        print(summarise(rows, f'r{rd} {label}', EXPECT.get(label)))

    # The number the three config triggers have to clear, pooled across
    # backdrops. Reported as a spread, not a mean: one slow viewpoint is the
    # finding, and a mean would hide it behind the fast ones.
    print('\n=== flip latency, sight already up, across backdrops ===')
    for label, want in (('crouch_after_key', 'crouching'),
                        ('prone_after_key', 'prone')):
        firsts = []
        for rd in {r['round'] for r in w.rows}:
            hit = [x['ms'] for x in w.rows
                   if x['label'] == label and x['round'] == rd
                   and x['posture'] == want]
            if hit:
                firsts.append(min(hit))
        if firsts:
            print(f'  {label:16} n={len(firsts)}  '
                  f'min {min(firsts):.0f}  max {max(firsts):.0f} ms')
        else:
            print(f'  {label:16} never reached {want} in any round')
    unread = [x for x in w.rows if x['ads'] and not x['posture']]
    print(f'  unreadable WHILE the sight was up: {len(unread)}/'
          f'{sum(1 for x in w.rows if x["ads"])} samples')

    print(f'\n{len(w.rows)} samples -> {out_dir}')
    print('LOOK AT THE CROPS before trusting any of it: the one stored '
          'failure sample was labelled prone and drew a crouch.')
    return 0


def _dump(w, out_dir):
    with open(os.path.join(out_dir, 'trace.jsonl'), 'w', encoding='utf-8') as f:
        for r in w.rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    sys.exit(main())
