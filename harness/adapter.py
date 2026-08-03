"""The three things the harness needs from below, and nothing else.

**This is the only file in harness/ that imports calibration.** That is
deliberate: calibration/ is being refactored, and a harness that reaches into
it from six places breaks in six places. Everything else here talks to the
signatures below.

The three, in the order they matter:

    measure(rig, ac, cell, mags)  -> dict     what happened, as NUMBERS
    reset(session, level)         -> bool     back to a known state
    dump(where, why)              -> str      the evidence for a failure

None of them exists yet in the shape below. Until they do, this module raises
NotImplemented with the exact signature wanted, so `pixi run night --dry`
prints the contract rather than a stack trace three frames deep. See
harness/CONTRACT.md, which is the version to hand to whoever is refactoring.

WHY THE RECORD IS NUMBERS AND NOT A VERDICT: judging belongs to
harness/verdict.py, and it has to be somewhere the measurement cannot reach.
A measure() that returned ok=True would be grading its own homework — the
failure mode Anthropic's harness work names explicitly, and the same shape as
this project's closed-loop blindness. So measure() reports, verdict judges.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The record measure() must return. Every key is REQUIRED; verdict.judge()
# fails closed on a missing one, because "nobody measured it" and "it was
# fine" are the two states this whole layer exists to keep apart.
RECORD_FIELDS = {
    'reached':            'bool  — did the cell reach the weapon/sight/posture '
                          'it is labelled with? Everything else is a lie if '
                          'this is False.',
    'reached_why':        'str   — why not, when reached is False',
    'mags_kept':          'int   — magazines that survived to the fit',
    'rate_resid_ms':      'float — fit residual of the measured fire rate',
    'rounds':             'int   — rounds the rate was fitted over',
    'impulse_off_rounds': 'float — out-of-loop timing check: how far the '
                          'commanded spike landed from where it was asked '
                          'for. None if no check was run.',
    'ads_frac':           'float — fraction of polls the crosshair said aiming',
    'track_alive_frac':   'float — fraction of fired rounds the tracker held',
    'curve':              'list  — the measured per-bullet dy, for the log',
}

# reset() levels, cheapest first. The loop escalates: a first failure gets
# LIGHT, a second gets HEAVY, and a cell that fails both is a cell whose
# problem is not state.
LIGHT = 1     # collapse the panel, close Tab, clear the rack, stand, hip-fire
HEAVY = 2     # re-enter the training range: RangeSession.ensure(force=True)


def measure(rig, ac, cell, mags):
    """Measure one cell. -> a record with every RECORD_FIELDS key.

    MUST NOT raise for a game-state problem: a weapon that would not spawn is
    `reached=False` with a reason, not an exception. Exceptions are for the
    harness being wrong (bad arguments, missing hardware), and the loop lets
    those out.
    """
    raise NotImplementedError(
        'measure(rig, ac, cell, mags) -> dict with keys '
        + ', '.join(sorted(RECORD_FIELDS)))


def reset(session, level=LIGHT):
    """Back to a known state. -> True when it got there.

    LIGHT is the one that does not exist yet. HEAVY already does, as
    calibration/range_session.py's RangeSession.ensure(force=True) — being
    evicted at 20 minutes IS a full reset (empty backpack, empty rack, random
    spawn point), it is just currently triggered only by the clock.
    """
    raise NotImplementedError(f'reset(session, level={level}) -> bool')


def dump(where, why, frames=None, state=None):
    """Write the evidence for a failure. -> the directory written.

    Exists because of a concrete afternoon: a spawner failure reported
    `<panel open, col1_row02 expanded, 12 entries>` and saved no frame, so
    diagnosing it needed a new probe and a live game session. The three
    numbers that settled it — 12 entries read, 13 in the ground truth, 5 in
    the row it was attributed to — were all available in that frame. With the
    frame on disk it is an offline question; without it, it is a night lost.
    """
    raise NotImplementedError(f'dump({where!r}, {why!r}) -> path')


def contract():
    """The contract as text, for --dry and for the refactor conversation."""
    out = ['measure(rig, ac, cell, mags) -> dict']
    for k, v in RECORD_FIELDS.items():
        out.append(f'    {k:<20} {v}')
    out += ['', 'reset(session, level) -> bool',
            f'    level={LIGHT} LIGHT  panel/Tab/rack/posture — DOES NOT EXIST',
            f'    level={HEAVY} HEAVY  re-enter the range — RangeSession'
            f'.ensure(force=True)',
            '', 'dump(where, why, frames=None, state=None) -> path',
            '    the failure frame and the state readout, on disk']
    return '\n'.join(out)
