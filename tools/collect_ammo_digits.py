"""Harvest the 0-9 ammo templates unattended: spawn an M249, tap out a dozen
rounds, and read the labels off the counter's own arithmetic.

    pixi run python tools/collect_ammo_digits.py                  # dry run
    pixi run python tools/collect_ammo_digits.py --write
    pixi run python tools/collect_ammo_digits.py --no-spawn       # gun in hand

Why the M249 and why only a dozen rounds: its magazine starts in three
digits, so the counter walks 100, 99, 98 ... 90 and that alone contains every
digit 0-9 — 1 and 0 from 100, then 9 paired with 9,8,7,6,5,4,3,2,1,0. An
extended magazine (150) works just as well and is not worth fitting: it adds
a whole kitting step that can fail, and buys nothing the base 100 does not
already cover. The 100 -> 99 step is a bonus, and the reason to start here
rather than at a two-digit gun: it is the only place the three-digit layout
can be measured, and detector/CLAUDE.md's 1686..1752 span for it is so far
arithmetic, not observation.

**The labels are not guessed and not typed in.** The counter falls by exactly
one per round, so the k-th distinct reading IS `start - k`. Two things make
that safe to rely on:

  * `start` is *inferred, not assumed*. PUBG auto-fits whatever the backpack
    is holding onto a gun the moment the spawner produces it (harvest.py was
    bitten by this with a cheek pad), so an M249 can arrive at 100 or at 150
    and nothing in the request says which. Both are three glyphs; their
    middle digit is 0 vs 5, and both of those already have templates. So the
    HUD is asked, and a start that cannot be pinned to exactly one candidate
    is an abort, not a default.

  * every reading the installed templates can verify must agree with the
    sequence. 0/4/5/8 cover 40% of the digits, so a dropped or doubled round
    is caught within a step or two — the run is thrown away whole rather than
    written partly. A template installed against a shifted sequence would be
    a *silently* wrong digit, which is the failure this detector can least
    afford.

Rounds are tapped one at a time, but nothing assumes a tap fires exactly one:
the frame loop runs continuously at ~115 fps against an 80 ms/round cadence,
so a double never hides an intermediate reading, and the anchor check would
catch it if it did.

Each template is the per-pixel majority over every frame that showed it, so
no single frame's compression noise reaches the asset.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'calibration'))

import cv2
import numpy as np

from config import HUD_REGIONS
from detector.ammo_detector import (ASSETS_DIR, AmmoDetector, _iou, _place,
                                    segment)
from detector.cropper import make_grabber
from detector.lobby_detector import LobbyState, snapshot as lobby_state
from press.pico_mouse import get_mouse, other_agents
from control.focus import game_focused, raise_game

WEAPON = 'm249'
START_CANDIDATES = (100, 150)   # base drum, and the drum with an extended mag

HOLD_MS = 400           # one arm of a held trigger; re-armed well before it
REARM_S = 0.15          # expires, so a dropped CDC packet still leaves 250 ms

TAP_MS = 20             # ~750 rpm is 80 ms/round, so this is well inside one
TAP_SETTLE_S = 0.30     # counter still after this long => the tap is done
TAP_TIMEOUT_S = 1.5
TAP_RETRIES = 3

# Below this an installed template is not asserting anything. It has to clear
# the *impostor* band, not merely be the best of a bad set: a digit with no
# template still matches something, and on the 150->121 walk it does so with
# eerie consistency — every '3' read as 8 at 0.748 and every '9' as 0 at
# 0.800, which is self-consistent enough to look like a finding. Genuine
# matches sit at 1.000 (same font, same size, same renderer), so 0.90 splits a
# 0.19-wide gap. At 0.70 those impostors were treated as evidence and rejected
# a sequence that was in fact correct.
ANCHOR_IOU = 0.90


# Two canvases of the same digit, one frame apart, agree at ~0.98+; the closest
# *different* pair of digits in the installed set is 0 vs 8 at 0.79. So this
# threshold is not tuned, it sits in the gap.
#
# Exact byte equality is what a first version used, and it is wrong: the HUD
# antialiases against a moving scene, so one reading fragments into several
# "states" — a 14-tap run produced 34 of them, some only 2 frames long, and
# every label after the first fragment was off by one. detector/CLAUDE.md
# already records the same effect from the other side (27 apparent glyph shapes
# in the offline captures were really 4 digits plus compression noise).
SAME_IOU = 0.92


# A reading has to hold this long to count. The HUD redraws a digit over a
# frame or two, and mid-redraw the strip segments into one or two glyphs
# instead of three — which the first version recorded as its own state, so a
# 4-tap run reported 16 counts and every label after the first fragment was
# wrong. The gap is enormous: transition frames last ~2 ms, while the shortest
# real count is one round of the fastest weapon here (750 rpm = 80 ms).
MIN_STATE_S = 0.015


def same_reading(prev_slots, glyphs):
    """Is this frame still showing the count the last one showed?"""
    if len(prev_slots) != len(glyphs):
        return False
    return all(_iou(slot[-1], _place(g)) >= SAME_IOU
               for slot, (_, g) in zip(prev_slots, glyphs))


def same_state(a_slots, b_slots):
    """Do two recorded states show the same count? Used to stitch a reading
    back together when a transition frame split it in two."""
    if len(a_slots) != len(b_slots):
        return False
    return all(_iou(a[-1], b[0]) >= SAME_IOU for a, b in zip(a_slots, b_slots))


def _frozen(gap_s=0.4):
    """Two frames a moment apart, identical to the bit — the game is not
    rendering, so it is not really in front however the title bar reads."""
    from detector.cropper import capture_screen
    a = capture_screen()
    time.sleep(gap_s)
    return bool(np.array_equal(a, capture_screen()))


class Counter:
    """The ammo strip, sampled as a sequence of distinct readings.

    Blank frames are dropped rather than recorded: the HUD hides the counter
    briefly during the shot animation, and a blank between two identical
    readings would split one count in two and shift every label after it.
    """

    def __init__(self):
        self.grabber, self.paced = make_grabber({'ammo': HUD_REGIONS['ammo']})
        self.states = []        # [[_, [[canvas per frame] per glyph]]]
        self._pend = None       # (slots, first_seen, confirmed)

    def close(self):
        self.grabber.close()

    def flush(self, n=8):
        for _ in range(n):
            self.grabber.grab()

    def sample(self):
        """One frame. Returns True if it confirmed a new count."""
        glyphs = segment(self.grabber.grab()['ammo'])
        if not glyphs:
            return False
        now = time.perf_counter()
        p = self._pend
        if p is None or not same_reading(p[0], glyphs):
            # something different — start timing it, commit to nothing
            self._pend = ([[_place(g)] for _, g in glyphs], now, False)
            return False

        for slot, (_, g) in zip(p[0], glyphs):
            slot.append(_place(g))
        if p[2] or now - p[1] < MIN_STATE_S:
            return False

        # Held long enough to be real. If it is the count we were already on,
        # a transition frame merely interrupted it — stitch the frames back
        # together instead of recording a second state for the same number.
        if self.states and same_state(self.states[-1][1], p[0]):
            for a, b in zip(self.states[-1][1], p[0]):
                a.extend(b)
            self._pend = (self.states[-1][1], p[1], True)
            return False
        self.states.append([None, p[0]])
        self._pend = (p[0], p[1], True)
        return True

    def watch(self, settle_s, timeout_s):
        """Sample until the reading has held still for settle_s. Returns the
        number of new states seen, or None if focus was lost."""
        t0 = last_change = time.perf_counter()
        seen = 0
        while True:
            if not game_focused():
                return None
            if self.sample():
                seen += 1
                last_change = time.perf_counter()
            now = time.perf_counter()
            if now - last_change > settle_s:
                return seen
            if now - t0 > timeout_s:
                return seen
            if not self.paced:
                time.sleep(0.002)


def spawn_weapon():
    """Produce an M249 through the training-range spawner.

    SpawnerControl.ensure_panel() is the piece that refuses to assume comma
    opened anything -- it reads before and after every press."""
    from control.spawner import SpawnerControl

    mouse = get_mouse()
    sc = SpawnerControl()
    raise_game()        # SpawnerControl's own init can cost enough time to
                        # lose focus again before the first comma
    try:
        # Re-grab focus between attempts rather than once up front. Focus here
        # is not a state that stays put — the terminal that launched this keeps
        # taking it back, and a comma sent while it is gone is simply lost.
        # Interleaving the two is what turned an intermittent "panel would not
        # open" into a reliable open.
        for attempt in range(3):
            if attempt:
                print(f'  panel did not open; re-grabbing focus '
                      f'(attempt {attempt + 1}/3)')
            raise_game()
            if sc.ensure_panel(True):
                break
        else:
            print('  [!] spawner panel would not open, and the game would not '
                  'come to the front either.\n'
                  '      Comma works from anywhere in the training range, so '
                  'position is not it.\n'
                  '      Click into the game window and run this again.')
            return False
        if not sc.sync():
            print('  [!] spawner layout would not read')
            return False
        if not sc.give_weapon(WEAPON):
            print(f'  [!] spawner would not produce {WEAPON}')
            return False
        if not sc.ensure_panel(False):
            print('  [!] spawner panel would not close')
            return False
    finally:
        pass
    return True


def read_state(det, slots):
    """[(iou, digit) per glyph] for a state, using its middle frame."""
    out = []
    for frames in slots:
        scored = det._score(frames[len(frames) // 2])
        out.append(scored[0] if scored else (0.0, -1))
    return out


def infer_start(det, slots, candidates):
    """Which candidate start values the first reading is consistent with."""
    read = read_state(det, slots)
    ok = []
    for cand in candidates:
        s = str(cand)
        if len(s) != len(read):
            continue
        if all(d == int(ch)
               for ch, (iou, d) in zip(s, read) if iou >= ANCHOR_IOU):
            ok.append(cand)
    return ok, read


def validate(det, states, start):
    """Anchor every state against the templates already installed."""
    bad = []
    for k, (_, slots) in enumerate(states):
        want = start - k
        s = str(want)
        if len(slots) != len(s):
            bad.append(f'state {k}: {len(slots)} glyph(s), but {want} '
                       f'needs {len(s)}')
            continue
        for pos, (ch, (iou, d)) in enumerate(zip(s, read_state(det, slots))):
            if iou >= ANCHOR_IOU and d != int(ch):
                bad.append(f'state {k}: sequence says {want}, but the '
                           f'installed template reads {d} at position {pos} '
                           f'(iou {iou:.3f})')
    return bad


def verify_live(counter, mouse, det, start, max_fire_ms=25000):
    """Empty the whole magazine and check the detector reads every count.

    The templates are all cut from three-digit readings (150..121), so the
    obvious question is whether a glyph is drawn the same width once the count
    drops to two digits and then one. Asking the HUD is better than measuring
    the templates: hold the trigger down, read every count with the shipped
    detector, and require the readings to fall by exactly one from `start` to
    zero. That sweeps 100->99 and 10->9 — both width changes — and the ground
    truth is the arithmetic, not another template.

    Firing continuously rather than tapping is deliberate: it is the fastest
    the counter ever moves, so it is the hardest case for the sampler too.
    """
    print(f'emptying the magazine to check every count from {start} down '
          f'(~{start * 0.4:.0f}s) ...')
    # Tapped, not held. A held trigger is the obvious way to empty a magazine
    # and it does not work from here: CMD_CLICK carries a duration rather than
    # a held flag, and both a single 25 s arm and a 400 ms re-arm loop left the
    # counter sitting at 150 — not one round fired. The tap path is the one
    # already proven on this gun (29 taps produced 29 rounds, 1:1), so verify
    # uses it even though it costs a minute.
    taps = misses = 0
    try:
        while len(counter.states) < start + 1 and taps < start + 20:
            taps += 1
            mouse.click(buttons=0x01, duration_ms=TAP_MS)
            seen = counter.watch(TAP_SETTLE_S, TAP_TIMEOUT_S)
            if seen is None:
                print('[!] lost focus mid-burst')
                return None
            misses = 0 if seen else misses + 1
            if misses >= TAP_RETRIES:
                print(f'  counter stopped after {len(counter.states)} counts '
                      f'— magazine out, or the game stopped taking input')
                break
    finally:
        mouse.click(buttons=0x00, duration_ms=0)

    rows = []
    for k, (_, slots) in enumerate(counter.states):
        want = start - k
        mid = [f[len(f) // 2] for f in slots]
        scored = [det._score(c) for c in mid]
        digits = [s[0][1] if s else -1 for s in scored]
        ious = [s[0][0] if s else 0.0 for s in scored]
        got = int(''.join(str(d) for d in digits)) if all(
            d >= 0 for d in digits) else None
        rows.append((want, got, min(ious), len(slots)))
    return rows


def report_verify(rows, start):
    """Judge the sweep on the readings themselves, not on positional labels.

    Pairing the k-th recorded state with `start - k` looks like the obvious
    check and is the wrong one: it fails whenever the *sampler* records one
    reading twice, which says nothing about whether a digit was read right.
    Measured that way the first attempt called 143 of 151 counts wrong while
    every single reading was in fact correct and falling by one — the labels
    had simply drifted by the number of duplicated states.

    What actually has to be true is that the counter visited every value from
    `start` down to 0 and the detector named each one. So compare the SET of
    readings against that range. A digit misread at some width shows up
    unmissably: the true value goes missing and an impossible one appears.
    """
    if not rows:
        print('no counts observed')
        return 1

    seq = [got for _, got, _, _ in rows]
    # 1, not 0. The magazine cannot be left sitting on zero: PUBG auto-reloads
    # the instant it empties (docs/game_quirks.md), so the counter goes 1 ->
    # reload -> full and a lone 0 is never there to be sampled. The 0 glyph is
    # covered many times over anyway, at 150/140/130/.../100/90/.../10.
    want = set(range(1, start + 1))
    got = set(v for v in seq if v is not None)
    missing = sorted(want - got, reverse=True)
    spurious = sorted(got - want, reverse=True)
    unread = sum(1 for v in seq if v is None)

    # Duplicated states are a sampling artefact, worth reporting separately so
    # it is never mistaken for a detection error.
    dups = len(seq) - len(set(seq))

    print(f'\n{len(rows)} states recorded, readings span '
          f'{min(got, default="-")}..{max(got, default="-")}')
    print(f'  covered   {len(got & want)}/{len(want)} of {start}..1')
    print(f'  unread    {unread}   (glyphs that matched no template)')
    print(f'  duplicate {dups}   (same reading recorded twice — sampling, '
          f'not detection)')

    by_width = {}
    for v in sorted(got & want, reverse=True):
        by_width.setdefault(len(str(v)), []).append(v)
    print(f'\n{"digits":>7} {"expected":>9} {"read":>6}')
    for w in sorted(by_width, reverse=True):
        n_expected = sum(1 for v in want if len(str(v)) == w)
        print(f'{w:>7} {n_expected:>9} {len(by_width[w]):>6}')

    if missing or spurious or unread:
        if missing:
            print(f'\nMISSING {len(missing)}: {missing[:30]}')
        if spurious:
            print(f'IMPOSSIBLE {len(spurious)}: {spurious[:30]}')
        if unread:
            print(f'{unread} state(s) matched no template')
        return 1
    print(f'\nevery value from {start} down to 1 was read, and nothing '
          f'impossible appeared — three digits, two and one. Glyph width does '
          f'not depend on how many digits are showing.')
    return 0


def vote(canvases):
    """Per-pixel majority over every frame that showed this glyph."""
    stack = np.stack(canvases).astype(np.uint16)
    return (stack.sum(0) * 2 > len(canvases)).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    # 11 counts is the minimum that contains every digit, but there is no
    # reason to stop there: the magazine holds 150, each extra count costs one
    # round and a fraction of a second, and it buys both more frames to vote
    # over and more anchors for the sequence check. 30 walks 150 -> 121, so
    # every units digit appears three times and the tens place sweeps 5,4,3,2.
    ap.add_argument('--states', type=int, default=30,
                    help='distinct counts to collect (11 would already cover '
                         'every digit; more means more samples and anchors)')
    ap.add_argument('--max-taps', type=int, default=45,
                    help='give up after this many taps however few counts came')
    ap.add_argument('--write', action='store_true',
                    help='install the templates (default is a dry run)')
    ap.add_argument('--force', action='store_true',
                    help='overwrite digits that are already installed')
    ap.add_argument('--no-spawn', action='store_true',
                    help='skip the spawner; the gun is already in hand')
    ap.add_argument('--start', type=int, default=0,
                    help='assert the full magazine size instead of inferring it')
    ap.add_argument('--ignore-busy', action='store_true',
                    help='run even if another project process is live')
    ap.add_argument('--verify', action='store_true',
                    help='write nothing; empty a whole magazine and check the '
                         'detector reads every count, three digits down to one')
    ap.add_argument('--countdown', type=int, default=6,
                    help='seconds to switch to the game before anything fires')
    args = ap.parse_args()

    # The tool is launched from a terminal, so at t=0 the terminal has focus and
    # the game does not. harvest.py counts down for exactly this reason; without
    # it every run aborts on the focus check, or worse, presses keys into
    # whatever window is actually in front.
    # Unconditionally, not just when the check says we lost it. game_focused()
    # is read at t=0; by the time the interpreter is warm and the first key
    # goes out, the terminal that launched us may have taken focus back — which
    # is exactly how "the spawner panel would not open" happened on a run whose
    # first line said focused=True. raise_game() is idempotent and cheap.
    raise_game()
    if not game_focused() and args.countdown:
        print(f'>>> switch to the game now — starting in {args.countdown}s')
        for s in range(args.countdown, 0, -1):
            print(f'    {s} ...', flush=True)
            time.sleep(1.0)

    if not game_focused():
        print('game is not the foreground window — nothing fired')
        return 1
    # Focused is necessary but nowhere near sufficient. The window keeps focus
    # and the title keeps matching in the lobby, and the training range evicts
    # you after 20 minutes whether or not a tool is mid-run. Without this check
    # the symptom is "the spawner panel would not open" — which reads like a
    # broken detector and cost several rounds of diagnosis on 2026-08-02, when
    # the real answer was that the session had simply expired.
    state = lobby_state()
    if not state.playable:
        where = {LobbyState.LOBBY: 'in the lobby — the training-range session '
                                   'has ended (it evicts you after 20 min)',
                 LobbyState.FULLBLEED: 'on a loading screen, or the net-debug '
                                       'overlay is off (see LobbyDetector.'
                                       'selftest)'}[state]
        print(f'the game is {where}.\nRe-enter the training range, then run '
              f'this again.')
        return 1

    # A still picture means it is not really in front however the title reads:
    # PUBG stops rendering when it loses input focus, so two frames 0.4 s apart
    # come back bit-identical.
    if _frozen():
        print('the game window has focus but is not drawing — it is not '
              'really in front, or the client is paused. Click into the game '
              'and try again.')
        return 1

    # Checked before anything is pressed, not after. Two runs sharing the game
    # window trade keystrokes and each sees the other's UI state, which
    # surfaces as "the spawner panel would not open" three comma presses
    # later — a symptom that reads like a broken detector.
    busy = other_agents()
    if busy and not args.ignore_busy:
        print(f'another run is already driving this game:\n    {busy}\n'
              f'Wait for it rather than killing it — it is probably another '
              f'agent mid-run. Pass --ignore-busy to override.')
        return 1

    det = AmmoDetector()
    print(f'templates already installed: {det.digits_known or "none"}')
    if not det.digits_known:
        print('  [!] with no templates nothing can anchor the sequence. '
              'Extract at least one digit first (tools/probe_ammo_ocr.py).')
        return 1

    if not args.no_spawn:
        print(f'spawning {WEAPON} ...')
        if not spawn_weapon():
            return 1

    mouse = get_mouse()
    try:
        mouse.set_recoil_enabled(False)
    except Exception:
        pass

    counter = Counter()
    try:
        counter.flush()
        if counter.watch(TAP_SETTLE_S, 2.0) is None:
            print('[!] lost focus before firing')
            return 1
        if not counter.states:
            print('[!] no digits on the HUD — is a weapon equipped?')
            return 1

        candidates = [args.start] if args.start else list(START_CANDIDATES)
        ok, read = infer_start(det, counter.states[0][1], candidates)
        shown = ' '.join(f'{d}@{iou:.2f}' if iou >= ANCHOR_IOU else f'?@{iou:.2f}'
                         for iou, d in read)
        print(f'HUD reads {len(read)} glyph(s): [{shown}]')
        if len(ok) != 1:
            print(f'[!] that matches {ok or "no"} candidate(s) of {candidates} '
                  f'— cannot pin the starting count. Pass --start explicitly '
                  f'once you know it.')
            return 1
        start = ok[0]
        print(f'starting count: {start}  (inferred, not assumed)')

        if args.verify:
            rows = verify_live(counter, mouse, det, start)
            if rows is None:
                return 1
            return report_verify(rows, start)

        # Counting taps is the wrong loop variable: a 20 ms tap on a 750 rpm
        # LMG fired 2-3 rounds, so 14 taps walked the counter down 33. What the
        # harvest needs is distinct *readings*, and from 150 the first 11 of
        # them (150..140) already contain every digit.
        print(f'tapping until {args.states} distinct counts ...')
        taps = misses = 0
        while len(counter.states) < args.states and taps < args.max_taps:
            taps += 1
            mouse.click(buttons=0x01, duration_ms=TAP_MS)
            seen = counter.watch(TAP_SETTLE_S, TAP_TIMEOUT_S)
            if seen is None:
                print('[!] lost focus mid-run — nothing written')
                return 1
            misses = misses + 1 if not seen else 0
            if misses >= TAP_RETRIES:
                print(f'  [!] the counter has not moved in {TAP_RETRIES} taps '
                      f'— out of ammo, or the game is not taking input')
                break
        print(f'  {taps} taps -> {len(counter.states)} counts')
    finally:
        try:
            mouse.click(buttons=0x00, duration_ms=0)
        except Exception:
            pass
        counter.close()

    states = counter.states
    frames = [sum(len(s) for s in slots) for _, slots in states]
    print(f'\n{len(states)} distinct counts '
          f'({start} -> {start - len(states) + 1}), '
          f'{min(frames)}-{max(frames)} frames each')

    bad = validate(det, states, start)
    if bad:
        print('\nREJECTED — the sequence does not check out:')
        for b in bad[:10]:
            print(f'  {b}')
        if len(bad) > 10:
            print(f'  ... and {len(bad) - 10} more')
        print('\nNothing written. A shifted sequence would install silently '
              'wrong digits, so the whole run is discarded.')
        return 1
    n_anchored = sum(1 for k, (_, slots) in enumerate(states)
                     for iou, _ in read_state(det, slots) if iou >= ANCHOR_IOU)
    print(f'sequence checks out: {n_anchored} glyph readings anchored against '
          f'the installed templates, all agreeing')

    per_digit = {}
    for k, (_, slots) in enumerate(states):
        for ch, fr in zip(str(start - k), slots):
            per_digit.setdefault(int(ch), []).extend(fr)

    print(f'\n{len(per_digit)} digits observed:')
    os.makedirs(ASSETS_DIR, exist_ok=True)
    wrote = 0
    for d in sorted(per_digit):
        canvases = per_digit[d]
        merged = vote(canvases)
        ys, xs = np.where(merged)
        if not len(ys):
            print(f'  {d}: majority vote came out empty — skipped')
            continue
        tight = merged[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        out = os.path.join(ASSETS_DIR, f'digit_{d}.png')
        exists = os.path.exists(out)
        agree = float(np.mean([np.mean(_place(tight) == c) for c in canvases]))
        if exists and not args.force:
            note = 'installed, kept'
        elif exists:
            note = 'overwriting'
        else:
            note = 'new'
        print(f'  {d}: {len(canvases):4d} frames  {tight.shape[1]}x'
              f'{tight.shape[0]}  agree {agree:.4f}   ({note})')
        if args.write and not (exists and not args.force):
            cv2.imwrite(out, tight * 255)
            wrote += 1

    if not args.write:
        print('\ndry run; pass --write to install')
        return 0
    fresh = AmmoDetector()
    print(f'\nwrote {wrote}; digits now {fresh.digits_known}')
    missing = [d for d in range(10) if d not in fresh.digits_known]
    if missing:
        print(f'still missing: {missing} — fire more rounds, or start from a '
              f'count that contains them')
    else:
        print('full set 0-9 installed')
    print('now run: pixi run python tools/probe_ammo_ocr.py --confusion')
    return 0


if __name__ == '__main__':
    sys.exit(main())
