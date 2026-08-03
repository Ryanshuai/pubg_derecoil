"""What does the game actually say, right now? Reads, never drives.

    pixi run python tools/dump_state.py
    pixi run python tools/dump_state.py --why "kit read '' on both slots"

A CLI over control/evidence.dump_state — the reasoning, and what a '' reading
actually means, are documented there. This exists so a state can be
photographed by hand mid-debug, using the same code path an unattended night
uses at 04:00.
"""
import argparse
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from control.evidence import dump_state
from control.focus import ensure_focus

OUT = os.path.join(ROOT, 'docs', 'state_dumps')


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--why', default='manual dump')
    ap.add_argument('--out', default='')
    args = ap.parse_args()

    if not ensure_focus(countdown_s=3, label='the state dump'):
        print('[!] could not focus the game')
        return 1
    time.sleep(0.5)

    where = args.out or os.path.join(
        OUT, datetime.now().strftime('%m%d_%H%M%S'))
    where, note = dump_state(where, args.why)

    print(f'\nrack   : {note.get("guns")}')
    for g, slots in (note.get('slots') or {}).items():
        worn = {k: v for k, v in (slots or {}).items() if v}
        print(f'  gun{g}: {worn if worn else "— nothing, or not a gun —"}')
    for k in ('sync', 'error'):
        if note.get(k):
            print(f'[!] {note[k]}')
    print(f'\n-> {where}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
