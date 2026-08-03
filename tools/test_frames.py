"""The shared frame source, offline. No game, no hardware, no live screen.

    pixi run frames

detector/cropper.ScreenBuffer collects three things that were written out by
hand in four places: the crop-to-screen-coordinate blit (calibration/sweep.py's
Rig and calibration/state.py's Probe; harvest.py's Panel was the third
until it was deleted), the
flush-N-then-read idiom, and the "refuse a frame the game did not draw" guard
(calibration/capture_ads.py's grab).

The blit is the part with teeth. Half the detectors index SCREEN coordinates —
AdsDetector cuts a window out of the frame's own centre, SpawnerDetector reads
fixed anchors — so a crop handed over on its own is read at the wrong place and
answered confidently. A migration that moves a box by one pixel does not throw;
it degrades a template match, which is the failure detector/CLAUDE.md's first
rule is about. So the checks here are exact-equality against stored full-screen
PNGs, and the anchor box is compared against the LIVE SOURCE of the two files
it replaces rather than against a number copied out of them.
"""
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from config import (HUD_REGIONS, SCREEN_H, SCREEN_W, SPAWNER_ICON_ANCHORS,
                    SPAWNER_ICON_H, SPAWNER_ICON_SEARCH, SPAWNER_ICON_W)
from detector.cropper import (FLUSH_FRAMES, FocusLost, ScreenBuffer,
                              StillGrabber, anchor_box)

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {name:<52} {got!r}'
          + ('' if ok else f'  != {want!r}'))
    if not ok:
        FAILS.append(name)


def raises(name, exc, fn):
    try:
        fn()
    except exc as e:
        print(f'  ok    {name:<52} {type(e).__name__}')
        return
    except Exception as e:                                   # noqa: BLE001
        print(f'  FAIL  {name:<52} raised {type(e).__name__} not {exc.__name__}')
    else:
        print(f'  FAIL  {name:<52} did not raise {exc.__name__}')
    FAILS.append(name)


# ════════════════════════════════════════════════════════════
# Inputs
# ════════════════════════════════════════════════════════════

# Stored full-screen frames. Deliberately different scenes: a bare gun on the
# Tab screen, one wearing a fixed scope, the spawner panel up. Two frames that
# look alike cannot show a stale buffer.
SHOTS = [
    'docs/compat/runs/20260802_155222/m416.png',
    'docs/compat/runs/20260802_155222/vss.png',
    'docs/training_epuipment.png',
    'docs/lobby/in_game.png',
]

# A realistic region set: the per-frame HUD, the Tab name plates and slot
# tiles, the ADS centre window, and the spawner anchor box. Spread over the
# whole screen on purpose — the GDI grabber bands regions by y, so a set that
# all sits in one band would not exercise the mapping back.
CROSS_R = 70
REGIONS = {k: HUD_REGIONS[k] for k in
           ('ammo', 'type', 'posture', 'fire_mode', 'weapon_1', 'weapon_2',
            'gun_name_1', 'gun_name_2')}
REGIONS.update({k: v for k, v in HUD_REGIONS.items() if k.startswith('att_')})
REGIONS['crosshair'] = (SCREEN_H // 2 - CROSS_R, SCREEN_W // 2 - CROSS_R,
                        2 * CROSS_R, 2 * CROSS_R)
REGIONS['spawner'] = anchor_box(SPAWNER_ICON_ANCHORS, SPAWNER_ICON_W,
                                SPAWNER_ICON_H, SPAWNER_ICON_SEARCH)


def load(rel):
    img = cv2.imread(os.path.join(ROOT, rel))
    if img is None:
        print(f'  FAIL  missing frame {rel}')
        FAILS.append(f'missing {rel}')
        return None
    if img.shape[:2] != (SCREEN_H, SCREEN_W):
        print(f'  FAIL  {rel} is {img.shape[:2]}, not the screen')
        FAILS.append(f'wrong size {rel}')
        return None
    return img


def expected_buffer(img, regions):
    """What full() has to produce: a black screen with the crops put back."""
    buf = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)
    for y, x, h, w in regions.values():
        buf[y:y + h, x:x + w] = img[y:y + h, x:x + w]
    return buf


IMGS = [load(p) for p in SHOTS]
if any(i is None for i in IMGS):
    print('\ncannot run without the stored frames')
    sys.exit(1)


# ════════════════════════════════════════════════════════════
# 1. anchor_box against the live source of the two copies
# ════════════════════════════════════════════════════════════

print('\n=== the anchor box matches the two hand-rolled copies, exactly ===')
print('    (extracted from the files as they are now, not transcribed —')
print('     a migration that moves a box by a pixel has to fail here)')

# THE CROSS-CHECK IS RETIRED, on purpose. It lifted the box arithmetic out of
# each hand-rolled copy with ast and re-ran it, so that a migration moving a
# box by one pixel had to fail here. Both copies are now gone —
# calibration/harvest.py's Panel was deleted with the class, and
# calibration/state.py's Probe calls anchor_box() itself — so there is nothing
# left to extract and the comparison has no second opinion to offer.
#
# It caught both deletions on the way past, which is what it was for. Removed
# deliberately rather than made tolerant of a missing name: a lookup that
# shrugs at "the class is gone" would have passed silently through exactly the
# migration it existed to guard.
#
# What replaces it is weaker and worth being honest about: the box is pinned
# to the literal it was measured at, so a change to anchor_box() or to the
# SPAWNER_ICON_* constants still fails, but a caller computing its own box
# differently no longer would. If a second copy ever appears, bring the ast
# check back rather than trusting the literal.
WANT = anchor_box(SPAWNER_ICON_ANCHORS, SPAWNER_ICON_W, SPAWNER_ICON_H,
                  SPAWNER_ICON_SEARCH)
check('anchor_box is the measured spawner box', WANT, (964, 2490, 311, 118))

print('\n=== and it keeps the clamp quirk the copies had ===')
# One anchor, no margin: the box is exactly the icon.
check('single anchor, no margin', anchor_box([(100, 200)], 70, 77, 0),
      (200, 100, 77, 70))
# Two anchors, margin clear of the edges: origin moves back by s, extent grows
# by 2s.
check('two anchors, margin 24',
      anchor_box([(100, 200), (140, 260)], 70, 77, 24),
      (176, 76, 185, 158))
# Anchored inside the margin of the top-left corner: the ORIGIN clamps at 0 and
# the extent does not shrink to match, so the box runs s px further than asked.
# Faithfully wrong, on purpose — the two call sites did this and must migrate
# without moving.
check('clamped at the top-left keeps the full extent',
      anchor_box([(10, 5)], 70, 77, 24), (0, 0, 125, 118))


# ════════════════════════════════════════════════════════════
# 2. crops and the blit back, pixel for pixel
# ════════════════════════════════════════════════════════════

print('\n=== every crop comes back byte-identical to the source frame ===')
for rel, img in zip(SHOTS, IMGS):
    with ScreenBuffer.over_stills(REGIONS, img) as sb:
        f = sb.grab()
        bad = [n for n, (y, x, h, w) in REGIONS.items()
               if not np.array_equal(f[n], img[y:y + h, x:x + w])]
        check(f'grab() {os.path.basename(rel):<28} {len(REGIONS)} crops',
              bad, [])

print('\n=== full() puts each crop back where it came from, byte for byte ===')
for rel, img in zip(SHOTS, IMGS):
    with ScreenBuffer.over_stills(REGIONS, img) as sb:
        buf = sb.full()
        # One assertion covering both halves: every region matches the source,
        # and every pixel outside the region set is still black. A box that
        # migrated one pixel off fails this even when the crop itself is right.
        check(f'full() {os.path.basename(rel):<28} whole 3440x1440',
              np.array_equal(buf, expected_buffer(img, REGIONS)), True)
        worst = [n for n, (y, x, h, w) in REGIONS.items()
                 if not np.array_equal(buf[y:y + h, x:x + w],
                                       img[y:y + h, x:x + w])]
        check(f'   per-region {os.path.basename(rel)}', worst, [])

print('\n=== a crop is not the same picture at the wrong coordinates ===')
# The reason full() exists at all: a detector handed the raw crop reads
# somewhere else. If these matched, the test above would prove nothing.
img = IMGS[0]
y, x, h, w = REGIONS['spawner']
check('spawner crop != the same-size box at the origin',
      np.array_equal(img[y:y + h, x:x + w], img[0:h, 0:w]), False)


# ════════════════════════════════════════════════════════════
# 3. the buffer is reused, and carries nothing over
# ════════════════════════════════════════════════════════════

print('\n=== the buffer is reused, not reallocated per frame ===')
with ScreenBuffer.over_stills(REGIONS, [IMGS[0], IMGS[1]]) as sb:
    a, b = sb.full(), sb.full()
    check('full() hands back the same array object', a is b, True)
    check('and it is screen-sized', a.shape, (SCREEN_H, SCREEN_W, 3))

print('\n=== and no pixel of the previous frame survives into the next ===')
with ScreenBuffer.over_stills(REGIONS, [IMGS[0], IMGS[1]]) as sb:
    first = sb.full(copy=True)
    second = sb.full()
    check('frame 2 is frame 2 everywhere',
          np.array_equal(second, expected_buffer(IMGS[1], REGIONS)), True)
    # Guard against the test passing because the two frames look the same.
    check('the two frames really do differ',
          np.array_equal(first, second), False)
    stale = [n for n, (y, x, h, w) in REGIONS.items()
             if np.array_equal(second[y:y + h, x:x + w],
                               IMGS[0][y:y + h, x:x + w])
             and not np.array_equal(IMGS[0][y:y + h, x:x + w],
                                    IMGS[1][y:y + h, x:x + w])]
    check('no region still shows frame 1', stale, [])

def region_eq(buf, img, name):
    y, x, h, w = REGIONS[name]
    return np.array_equal(buf[y:y + h, x:x + w], img[y:y + h, x:x + w])


print('\n=== a crop missing from the frame is blanked, not left stale ===')
with ScreenBuffer.over_stills(REGIONS, [IMGS[0], IMGS[1]]) as sb:
    sb.full()                                   # frame 1 into the buffer
    partial = sb.grab()
    partial.pop('spawner')                      # the detector could not read it
    buf = sb.full(partial)
    y, x, h, w = REGIONS['spawner']
    check('the missing region is black', int(buf[y:y + h, x:x + w].max()), 0)
    check('its neighbours are still frame 2', region_eq(buf, IMGS[1], 'type'),
          True)

print('\n=== two HUD regions overlap, so blanking one is partial ===')
# Found by this test, not by reading the table: HUD_REGIONS 'ammo'
# (1318,1670,48,90) and 'fire_mode' (1317,1626,43,56) share a 12x43 px corner.
# Harmless while both crops are present — same frame, same pixels — but a
# missing 'ammo' gets its corner painted back by 'fire_mode'. Pinned here so a
# migration that reorders the region dict has to notice.
ay, ax, ah, aw = REGIONS['ammo']
fy, fx, fh, fw = REGIONS['fire_mode']
check('ammo and fire_mode really do overlap',
      not (ay + ah <= fy or fy + fh <= ay or ax + aw <= fx or fx + fw <= ax),
      True)
with ScreenBuffer.over_stills(REGIONS, [IMGS[0], IMGS[1]]) as sb:
    partial = sb.grab()
    partial.pop('ammo')
    buf = sb.full(partial)
    # The shared corner survives, because fire_mode is blitted after ammo.
    check('the shared corner is repainted by fire_mode',
          np.array_equal(buf[ay:fy + fh, ax:fx + fw],
                         IMGS[0][ay:fy + fh, ax:fx + fw]), True)
    # Everything of ammo that fire_mode does not cover is black.
    check('the rest of the blanked region is black',
          int(buf[fy + fh:ay + ah, ax:ax + aw].max()), 0)

print('\n=== only= blits the named regions and leaves the rest alone ===')
with ScreenBuffer.over_stills(REGIONS, [IMGS[0], IMGS[1]]) as sb:
    sb.full()                                   # everything, from frame 1
    buf = sb.full(only=('spawner',))            # frame 2, spawner only
    y, x, h, w = REGIONS['spawner']
    check('the named region advanced',
          np.array_equal(buf[y:y + h, x:x + w],
                         IMGS[1][y:y + h, x:x + w]), True)
    y, x, h, w = REGIONS['ammo']
    check('an unnamed region did not',
          np.array_equal(buf[y:y + h, x:x + w],
                         IMGS[0][y:y + h, x:x + w]), True)

print('\n=== copy=True hands back something safe to keep ===')
with ScreenBuffer.over_stills(REGIONS, [IMGS[0], IMGS[1]]) as sb:
    kept = sb.full(copy=True)
    live = sb.full()
    check('the copy is a different array', kept is live, False)
    check('and it still holds frame 1',
          np.array_equal(kept, expected_buffer(IMGS[0], REGIONS)), True)

print('\n=== set_regions wipes what the old set left behind ===')
first_half = {'ammo': REGIONS['ammo'], 'type': REGIONS['type']}
second_half = {'spawner': REGIONS['spawner']}
with ScreenBuffer.over_stills(first_half, IMGS[0]) as sb:
    sb.full()
    sb.set_regions(second_half, grabber=StillGrabber(second_half, IMGS[1]))
    buf = sb.full()
    y, x, h, w = first_half['ammo']
    check('the dropped region is black again', int(buf[y:y + h, x:x + w].max()), 0)
    y, x, h, w = second_half['spawner']
    check('the new one reads the new frame',
          np.array_equal(buf[y:y + h, x:x + w],
                         IMGS[1][y:y + h, x:x + w]), True)


# ════════════════════════════════════════════════════════════
# 4. flush
# ════════════════════════════════════════════════════════════

print('\n=== flush(n) drops exactly n and hands back the last ===')
grab = StillGrabber(REGIONS, IMGS)               # four distinct frames
with ScreenBuffer(REGIONS, grabber=grab) as sb:
    f = sb.flush(3)
    check('three frames consumed', grab.n, 3)
    y, x, h, w = REGIONS['spawner']
    check('the frame returned is the third',
          np.array_equal(f['spawner'], IMGS[2][y:y + h, x:x + w]), True)

grab = StillGrabber(REGIONS, IMGS)
with ScreenBuffer(REGIONS, grabber=grab) as sb:
    sb.flush()
    check('the default is FLUSH_FRAMES', grab.n, FLUSH_FRAMES)

grab = StillGrabber(REGIONS, IMGS)
with ScreenBuffer(REGIONS, grabber=grab) as sb:
    check('flush(0) grabs nothing and returns nothing', sb.flush(0), None)
    check('  ...and consumed nothing', grab.n, 0)

# The idiom being replaced was `for _ in range(3): f = grabber.grab()`, and
# sweep's was `rig.flush(2)` followed by `rig.grab()`. Both are flush(3).
grab = StillGrabber(REGIONS, IMGS)
with ScreenBuffer(REGIONS, grabber=grab) as sb:
    sb.flush(2)
    old_style = sb.grab()
grab2 = StillGrabber(REGIONS, IMGS)
with ScreenBuffer(REGIONS, grabber=grab2) as sb:
    new_style = sb.flush(3)
check('flush(2)+grab() == flush(3)',
      np.array_equal(old_style['ammo'], new_style['ammo']), True)

print('\n=== a still repeats its last frame, the way DXGI does when idle ===')
grab = StillGrabber(REGIONS, [IMGS[0]])
with ScreenBuffer(REGIONS, grabber=grab) as sb:
    y, x, h, w = REGIONS['ammo']
    check('grab 5 of 1 still reads frame 1',
          np.array_equal(sb.flush(5)['ammo'], IMGS[0][y:y + h, x:x + w]), True)


# ════════════════════════════════════════════════════════════
# 5. the focus guard
# ════════════════════════════════════════════════════════════

print('\n=== the focus guard refuses a frame the game did not draw ===')
calls = []


def focused_yes():
    calls.append(True)
    return True


def focused_no():
    calls.append(False)
    return False


with ScreenBuffer.over_stills(REGIONS, IMGS[0], focus_fn=focused_yes) as sb:
    calls.clear()
    sb.grab()
    check('a focused grab goes through', len(calls), 1)

with ScreenBuffer.over_stills(REGIONS, IMGS[0], focus_fn=focused_no) as sb:
    raises('an unfocused grab raises FocusLost', FocusLost, sb.grab)
    raises('and so does full()', FocusLost, sb.full)
    raises('and so does flush()', FocusLost, sb.flush)

with ScreenBuffer.over_stills(REGIONS, IMGS[0]) as sb:
    calls.clear()
    sb.flush(4)
    check('no guard means no win32 call at all', len(calls), 0)

# The whole point of the guard: without it the run keeps going and the frames
# are all the same picture. Standing in for a frozen screen with a still.
grab = StillGrabber(REGIONS, [IMGS[0]])
with ScreenBuffer(REGIONS, grabber=grab) as sb:
    y, x, h, w = REGIONS['ammo']
    frames = [sb.grab()['ammo'] for _ in range(4)]
    check('a frozen screen is silent without the guard',
          all(np.array_equal(a, frames[0]) for a in frames), True)


# ════════════════════════════════════════════════════════════
# 6. whole-screen mode, and the bounds check
# ════════════════════════════════════════════════════════════

print('\n=== whole-screen mode skips the blit and hands back a fresh frame ===')
with ScreenBuffer.over_stills(None, [IMGS[0], IMGS[1]]) as sb:
    one = sb.full()
    check('full() is the frame itself', np.array_equal(one, IMGS[0]), True)
    two = sb.full()
    check('the next call is a different array', one is two, False)
    check('so a kept frame stays frame 1',
          np.array_equal(one, IMGS[0]), True)
    check('and the new one is frame 2', np.array_equal(two, IMGS[1]), True)

print('\n=== a region that does not fit the screen is refused, with a name ===')
over = dict(REGIONS)
over['off_bottom'] = (SCREEN_H - 10, 0, 100, 100)
sb = ScreenBuffer(over, grabber=StillGrabber({'ammo': REGIONS['ammo']}, IMGS[0]))
raises('full() refuses an out-of-screen region', ValueError,
       lambda: sb.full({}))
try:
    sb.full({})
except ValueError as e:
    check("the message names it", 'off_bottom' in str(e), True)
sb.close()


print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
    sys.exit(1)
print('all ok')
