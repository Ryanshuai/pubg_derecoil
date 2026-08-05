"""A scheduled detection that reads NOTHING must ask again. Offline.

    pixi run detect-retry

WHAT THIS GUARDS. `Dispatcher._run_detect` used to `return` on a None result,
and for most detectors that is right — a fire mode that will not read now will
not read 100 ms from now either. The posture icon is different in kind: it is
only PAINTED while the sight is up, so None there means "ask again", and all
three of its triggers fire at moments when the sight usually is not up. The
stale posture then survived into a compensation factor wrong by up to 2x.

Measured 2026-08-05 over six viewpoints (tools/probe_posture_trace.py):
sight up, the icon follows a posture key in 34..68 ms and reads 3786/3787;
sight down, it reads 0 of 3787 across full 2000 ms windows. The delay was
never short. There was simply nothing on the screen to read.

WHY THIS IS A TEST AND NOT A LIVE CHECK. The requeue has to land in the deque
`_process_pending` is BUILDING, not the one it is replacing — append to
`self._pending` there and the retry is silently dropped on the very next line.
That bug reproduces as "compensation is occasionally wrong after going prone",
which is indistinguishable in the game from the bug it was meant to fix. It
costs one scripted detector to rule out, and a live session to guess at.
"""
import os
import sys
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import control.match as match                                  # noqa: E402
from control.match import Dispatcher                           # noqa: E402


class FakeCapture:
    """Always has a frame; the crops are never looked at."""

    def get_crops(self, ts, regions):
        return {r: None for r in regions}


class ScriptedDetector:
    """Returns None for the first `blank` calls, then `value` forever."""

    def __init__(self, blank, value='prone'):
        self.blank = blank
        self.value = value
        self.calls = 0

    def classify(self, crops):
        self.calls += 1
        if self.calls <= self.blank:
            return None
        return self.value


class FakeState:
    posture = 'standing'
    tab_open = False
    stop_recoil = False

    def __init__(self):
        self.set_calls = []

    def set_posture(self, p):
        self.set_calls.append(p)
        self.posture = p

    def print_status(self):
        pass


def _dispatcher(detector):
    d = Dispatcher.__new__(Dispatcher)      # no poller, no threads, no HW
    d.state = FakeState()
    d.capture = FakeCapture()
    d._detectors = {'posture': detector}
    d._pending = deque()
    d._hw_calls = []
    d._apply_hw = d._hw_calls.append
    return d


def _drain(d, max_ticks=200):
    """Run the REAL `_process_pending` to completion on a fake clock.

    Through _process_pending, not by re-queueing here, and that distinction is
    the whole reason this file exists. That method REBUILDS the deque and
    replaces `self._pending` at the end, so a retry appended to `self._pending`
    from inside `_run_detect` is discarded one line later — a bug that leaves
    `_run_detect` perfectly correct in isolation. A harness that does its own
    re-queueing tests the return value and misses it entirely.

    The clock is faked rather than slept through: real retries would put 1.5 s
    of wall time into a test, and a slow test is a test nobody runs. It also
    lets the "drop if >1 s stale" branch be reached without waiting a second.
    """
    now = [0.0]
    real = match.time.perf_counter
    match.time.perf_counter = lambda: now[0]
    try:
        for tick in range(max_ticks):
            if not d._pending:
                return tick
            now[0] = min(ts for ts, _ in d._pending)
            d._process_pending()
    finally:
        match.time.perf_counter = real
    return max_ticks


ENTRY = {'key': 'z', 'event': 'press', 'detect': 'posture',
         'regions': ['posture'], 'delay': 200, 'retry_ms': 100,
         'retries': 10, 'cond': '!tab_open', 'result': 'posture'}

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def reads_first_time():
    """No retry needed: one call, one write, queue empty."""
    det = ScriptedDetector(blank=0)
    d = _dispatcher(det)
    d._pending.append((0.0, dict(ENTRY)))
    _drain(d)
    return (det.calls == 1 and d.state.set_calls == ['prone']
            and not d._pending), f'calls={det.calls} set={d.state.set_calls}'


@case
def retries_until_the_icon_appears():
    """Blank for three reads, then the value lands. THE REGRESSION: if the
    requeue goes to the wrong deque, calls stops at 1 and nothing is set."""
    det = ScriptedDetector(blank=3)
    d = _dispatcher(det)
    d._pending.append((0.0, dict(ENTRY)))
    _drain(d)
    return (det.calls == 4 and d.state.set_calls == ['prone']
            and not d._pending), f'calls={det.calls} set={d.state.set_calls}'


@case
def gives_up_after_the_budget():
    """Never readable: bounded at 1 + retries calls, queue drains, no write.

    Unbounded retries would keep re-reading a key press long after it
    mattered, and write its answer into a state that has moved on."""
    det = ScriptedDetector(blank=10 ** 6)
    d = _dispatcher(det)
    d._pending.append((0.0, dict(ENTRY)))
    _drain(d)
    return (det.calls == 1 + ENTRY['retries'] and not d.state.set_calls
            and not d._pending), f'calls={det.calls} pending={len(d._pending)}'


@case
def entries_without_retry_are_unchanged():
    """Everything else in DETECT_TABLE keeps one-shot behaviour."""
    det = ScriptedDetector(blank=3)
    d = _dispatcher(det)
    entry = {k: v for k, v in ENTRY.items() if k not in ('retry_ms', 'retries')}
    d._pending.append((0.0, entry))
    _drain(d)
    return (det.calls == 1 and not d.state.set_calls
            and not d._pending), f'calls={det.calls}'


@case
def a_failed_cond_does_not_retry():
    """The panel is up: this read is not late, it is not wanted. Retrying
    would keep the entry alive across the whole time Tab is open."""
    det = ScriptedDetector(blank=10 ** 6)
    d = _dispatcher(det)
    d.state.tab_open = True
    d._pending.append((0.0, dict(ENTRY)))
    _drain(d)
    return (det.calls == 0 and not d._pending), f'calls={det.calls}'


@case
def retry_spacing_is_the_configured_one():
    """Each retry is retry_ms later than the attempt before it."""
    det = ScriptedDetector(blank=2)
    d = _dispatcher(det)
    d._pending.append((10.0, dict(ENTRY)))
    seen = []
    now = [0.0]
    real = match.time.perf_counter
    match.time.perf_counter = lambda: now[0]
    try:
        for _ in range(4):
            if not d._pending:
                break
            now[0] = min(ts for ts, _ in d._pending)
            seen.append(now[0])
            d._process_pending()
    finally:
        match.time.perf_counter = real
    gaps = [round(b - a, 6) for a, b in zip(seen, seen[1:])]
    want = ENTRY['retry_ms'] / 1000.0
    return all(g == want for g in gaps) and len(gaps) == 2, f'gaps={gaps}'


def main():
    bad = 0
    for fn in CASES:
        try:
            ok, detail = fn()
        except Exception as e:                                # noqa: BLE001
            ok, detail = False, f'{type(e).__name__}: {e}'
        print(f'  {"ok  " if ok else "FAIL"}  {fn.__name__:38} {detail}')
        bad += not ok
    print(f'\n{len(CASES) - bad}/{len(CASES)} cases')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
