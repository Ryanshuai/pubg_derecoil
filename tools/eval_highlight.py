"""Does HighlightDetector still pick the weapon that is in hand?

Tested the way the detector is used: a highlighted and a non-highlighted crop
of the SAME weapon go in, and the pair is scored correctly when the
highlighted one scores higher. That framing matters — the detector never
judges one crop on its own, it compares the two HUD slots.

    pixi run python tools/eval_highlight.py

Was temp_debug/eval_highlight_jitter.py, whose question (is the 5x5 jitter
loop worth 14x the cost?) is settled and gone: the loop, the icon templates it
aligned, and the ALIGN_JITTER knob were all removed on 2026-08-05 after
measuring 254/254 with them and 254/254 without. What is left is worth keeping
as a regression, because the detector is still in the per-keypress path.
"""
import os
import re
import sys
import time
from collections import defaultdict

import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from detector.highlight_detector import _combined_max   # noqa: E402

EVAL = os.path.join(ROOT, 'data', 'templates', 'highlight_eval')
BASELINE = 254      # of 254 pairs; raise this if it ever improves


def load(folder, tag):
    out = defaultdict(list)
    d = os.path.join(EVAL, folder)
    for f in sorted(os.listdir(d)):
        m = re.match(rf'^(.+)_{tag}_[0-9a-f]+\.png$', f)
        if not m:
            continue
        img = cv2.imread(os.path.join(d, f))
        if img is not None:
            out[m.group(1)].append(img)
    return out


def main():
    hi = load('highlighted', 'h')
    lo = load('non_highlighted', 'l')
    pairs = [(w, a, b) for w in sorted(set(hi) & set(lo))
             for a, b in zip(hi[w], lo[w])]
    print(f'{len(pairs)} pairs over {len({w for w, _, _ in pairs})} weapons')

    ok, wrong = 0, defaultdict(int)
    t0 = time.perf_counter()
    for weapon, a, b in pairs:
        if _combined_max(a) > _combined_max(b):
            ok += 1
        else:
            wrong[weapon] += 1
    dt = time.perf_counter() - t0
    print(f'{ok}/{len(pairs)} = {ok / max(len(pairs), 1):.2%}   '
          f'{dt * 1e3 / max(len(pairs), 1):.2f} ms/pair')
    for w, n in sorted(wrong.items(), key=lambda kv: -kv[1]):
        print(f'    missed {n}x  {w}')

    if ok < BASELINE:
        print(f'\n[!] DOWN from the {BASELINE} baseline')
        return 1
    if ok > BASELINE:
        print(f'\n[!] UP from the {BASELINE} baseline — re-measure and raise it')
        return 1
    print(f'\nOK  at the {BASELINE} baseline')
    return 0


if __name__ == '__main__':
    sys.exit(main())
