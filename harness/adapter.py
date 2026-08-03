"""The three things the harness needs from below, and nothing else.

**This is the only file in harness/ that imports calibration.** That is
deliberate: calibration/ is being refactored, and a harness that reaches into
it from six places breaks in six places. Everything else here talks to the
signatures below.

The three, in the order they matter:

    measure(rigging, cell, mags)  -> dict     what happened, as NUMBERS
    reset(rigging, level)         -> bool     back to a known state
    dump(where, why)              -> str      the evidence for a failure

plus `open_rig()`, which builds the objects the first two need. That is here
rather than in night.py for the same reason as everything else: night.py must
not know how a Rig, a Kitter or a SpawnerControl is constructed, or the
refactor breaks the loop as well as the adapter.

WHY THE RECORD IS NUMBERS AND NOT A VERDICT: judging belongs to
harness/verdict.py, and it has to be somewhere the measurement cannot reach.
A measure() that returned ok=True would be grading its own homework — the
failure mode Anthropic's harness work names explicitly, and the same shape as
this project's closed-loop blindness. So measure() reports, verdict judges.

WHAT IS DELIBERATELY NOT MEASURED HERE: `impulse_off_rounds`. The impulse
check fires a curve that is zero except one spiked bullet, which is not a
recoil measurement and cannot be taken during one. It is a per-SESSION gate,
not a per-cell one — run tools/probe_impulse_ab.py, and pass the result in
via open_rig(impulse_off=...). Left None it fails every cell closed, which is
the right default: an unverified timing chain makes every curve in the night
worthless, and finding that out in the morning is the whole point.
"""
import os
import statistics
import sys
import time
from datetime import datetime

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
    'rate_resid_ms':      'float — how far the per-magazine fire rates '
                          'disagree, in ms of bullet interval',
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
LIGHT = 1     # collapse the panel, close Tab, into ADS, stand
HEAVY = 2     # re-enter the training range: RangeSession.ensure(force=True)


class Rigging:
    """Everything measure() drives, built once and handed back to the loop.

    Deliberately a bag rather than a facade: adding a method here would put
    behaviour in the adapter, and the adapter's whole job is to be the one
    place that BREAKS when calibration moves, not a second place where the
    measurement lives.
    """

    def __init__(self, rig, kit, sc, session, log, facts, parts, impulse_off):
        self.rig = rig
        self.kit = kit
        self.sc = sc
        self.session = session
        self.log = log
        self.facts = facts
        self.parts = parts
        self.impulse_off = impulse_off
        self.apply_ema = True

    def close(self):
        for name in ('rig', 'kit', 'session'):
            try:
                getattr(self, name).close()
            except Exception:
                pass
        try:
            self.log.close()
        except Exception:
            pass
        try:
            self.facts.save()
        except Exception:
            pass


def open_rig(sight, out_dir, home=True, apply_ema=True, countdown=6,
             impulse_off=None, weapons=(), configs=('bare',)):
    """Build the rig, take the foreground, prove we are in the range.

    -> (Rigging, None) or (None, why). Never raises for a game-state problem:
    "PUBG is not running" is an answer the morning can read, and a traceback
    out of a 4 a.m. process is not.
    """
    from calibration.harvest import (Kitter, MAG_FOR_CLASS, PART_FOR_CLASS,
                                     SCOPE_PART, parse_config, stock_parts)
    from calibration.kit_facts import KitFacts
    from calibration.range_session import get_session
    from calibration.sweep import Rig
    from control.focus import ensure_focus
    from control.spawner import ROSTER, SpawnerControl

    # What the run needs on hand. Same derivation as harvest's, because it is
    # the same question: only the slots some config actually fills, plus the
    # pinned sight and magazine.
    wanted_slots = frozenset().union(*(parse_config(c) for c in configs)) \
        if configs else frozenset()
    parts = {SCOPE_PART}
    for w in weapons:
        cls = ROSTER.get(w, (None,))[0]
        table = PART_FOR_CLASS.get(cls, {})
        parts.update(x for x in
                     [table.get(s) for s in wanted_slots] +
                     [MAG_FOR_CLASS.get(cls)] if x)

    rig = Rig(sight)
    rig.use_homing = home
    sc = SpawnerControl()
    kit = Kitter(rig)
    kit.restock_fn = lambda need: stock_parts(sc, kit, set(need) | parts)

    if not ensure_focus(countdown_s=countdown, label='the night'):
        rig.close()
        kit.close()
        return None, 'PUBG is not in the foreground and would not take it'
    time.sleep(0.6)

    session = get_session('auto')
    ok, _ = session.ensure()
    if not ok:
        rig.close()
        kit.close()
        return None, 'could not get into a match'

    # "Are we in the training range?" has one honest answer: the item spawner
    # opens. Nothing else tells the range apart from any other match.
    in_range = sc.ensure_panel(True)
    sc.ensure_panel(False)
    if not in_range:
        rig.close()
        kit.close()
        session.close()
        return None, ('in a match, but the item spawner will not open — not '
                      'the training range, or not at a spawn point')

    os.makedirs(out_dir, exist_ok=True)
    log = open(os.path.join(out_dir, 'cells.jsonl'), 'a', encoding='utf-8')
    log.write('{"type": "header", "sight": "%s", "ts": "%s"}\n'
              % (sight, datetime.now().isoformat(timespec='seconds')))
    log.flush()

    r = Rigging(rig, kit, sc, session, log, KitFacts(), parts, impulse_off)
    r.apply_ema = apply_ema
    return r, None


def measure(rigging, cell, mags):
    """Measure one cell. -> a record with every RECORD_FIELDS key.

    MUST NOT raise for a game-state problem: a weapon that would not spawn is
    `reached=False` with a reason, not an exception. Exceptions are for the
    harness being wrong (bad arguments, missing hardware), and the loop lets
    those out.
    """
    from calibration.harvest import (SIGHT_FOR, measure_cell, note_fits,
                                     stock_parts, want_for)
    from control.spawner import ROSTER

    weapon, posture = cell['weapon'], cell['posture']
    rig, kit, sc = rigging.rig, rigging.kit, rigging.sc
    rec = _blank(cell)

    rig.set_sight(SIGHT_FOR.get(weapon, cell.get('sight', 'red_dot')))

    # Clear the rack before spawning, do not merely strip one slot of it.
    # Relying on the incoming gun to evict the old one is what leaked a
    # magazine per cell: eviction only happens once the rack is FULL, and an
    # evicted gun leaves wearing everything it had on. See Kitter.clear_rack.
    kit.clear_rack()
    if not stock_parts(sc, kit, rigging.parts, also=(weapon,),
                       loose_only=True):
        rec['reached_why'] = f'could not stock the parts or produce {weapon}'
        return rec
    # Where the gun actually landed, read rather than assumed. An empty rack
    # takes the first gun into slot 1, and range re-entry empties the rack.
    if kit.find_gun(weapon) is None:
        rec['reached_why'] = f'{weapon} is not in the rack after the spawn'
        return rec

    # Bare: pinned sight and magazine, every other controlled slot forced
    # EMPTY rather than left alone. The rule and the reason live in
    # harvest.want_for, in one copy, on purpose.
    want = want_for(weapon, ROSTER.get(weapon, (None,))[0])
    if kit.apply(want, weapon=weapon) is None:
        for slot_name, key, why in kit.last_bad:
            if key:
                rigging.facts.note_failure(weapon, slot_name, key, note=why)
        rec['reached_why'] = ('kit: ' + '; '.join(
            f'{s} {w}' for s, _, w in kit.last_bad)) if kit.last_bad \
            else 'the kit would not go on'
        return rec
    note_fits(rigging.facts, weapon, want)

    cellrec = measure_cell(rig, weapon, posture, mags, kit.slot, rigging.log,
                           'bare', want, apply_ema=rigging.apply_ema)
    if cellrec is None:
        # measure_cell prints its own reason and returns None for all of them:
        # no curve, wrong fire mode, posture refused, every magazine dropped.
        # Naming the stage is honest; naming a cause would be a guess, and the
        # evidence dump is what settles it.
        rec['reached_why'] = ('fired nothing usable — see cells.jsonl and the '
                              'run log for which stage refused')
        return rec

    return _fill(rec, cellrec, rigging.impulse_off)


def _blank(cell):
    """A record with every field present and nothing claimed.

    Present-and-None, not absent: verdict.judge() fails closed on a missing
    field precisely so a stage that never ran cannot read as a pass, and that
    only works if the record says which fields were never filled.
    """
    rec = {k: None for k in RECORD_FIELDS}
    rec.update({'reached': False, 'reached_why': '', 'cell': cell['id']})
    return rec


def _fill(rec, cellrec, impulse_off):
    """Turn one measure_cell record into the harness's numbers."""
    rows = cellrec.get('mags') or []
    kept = len(rows)
    fired = kept + len(cellrec.get('mags_discarded') or [])

    rec['reached'] = True
    rec['mags_kept'] = kept
    rec['rounds'] = int(cellrec.get('bullets_fired') or 0)
    rec['impulse_off_rounds'] = impulse_off
    rec['curve'] = _mean_curve(rows)

    # ADS over the magazines that were KEPT, and the WORST of them rather than
    # the mean: a cell is only as good as its weakest accepted magazine, and
    # averaging lets four clean ones carry one fired half from the hip.
    ads = [r['ads_cross_frac'] for r in rows
           if r.get('ads_cross_frac') == r.get('ads_cross_frac')]
    rec['ads_frac'] = float(min(ads)) if ads else None

    # THE RATE CHECK, and it is not the fit residual. interval_from_span uses
    # two endpoints, so its residual is zero by construction — reporting that
    # would satisfy verdict's rate gate without measuring anything. What the
    # function's own docstring asks for instead is agreement BETWEEN
    # magazines: "a missed LAST change shortens the span and reads as a faster
    # gun ... it shows up as a rate that disagrees between magazines of the
    # same cell, so the caller should require agreement before storing one."
    # No caller did. This one does.
    ivs = [r['measured_interval_ms'] for r in rows
           if r.get('measured_interval_ms')]
    if len(ivs) >= 2:
        rec['rate_resid_ms'] = float(statistics.pstdev(ivs))
    elif len(ivs) == 1:
        # One magazine cannot disagree with itself. Left None rather than 0.0:
        # zero would claim a check that was never possible, and judge() fails
        # closed on None, which is the honest outcome.
        rec['rate_note'] = ('only one magazine produced a rate — nothing to '
                            'compare it against')
    else:
        rec['rate_note'] = 'no magazine produced a fire rate'

    # Tracking, over every magazine FIRED rather than every one kept. A rate
    # measured over survivors is a rate among survivors: with four of five
    # magazines thrown away for lost tracking, the fifth still reports ~100%.
    if fired:
        oor = sum(r.get('n_out_of_range') or 0 for r in rows)
        n = sum(len(r.get('per_bullet_counts') or ()) for r in rows)
        per_mag = (n / kept) if kept else float(cellrec.get('bullets_fired') or 0)
        # Discarded magazines contribute their rounds to the denominator and
        # nothing to the numerator. That is the point: they are the rounds the
        # tracker did not hold.
        total = n + per_mag * (fired - kept)
        rec['track_alive_frac'] = float((n - oor) / total) if total else None

    # Carried for the log and the morning, not judged by anything.
    rec['residual_counts_mean'] = cellrec.get('residual_counts_mean')
    rec['true_counts'] = cellrec.get('true_counts')
    rec['mags_discarded'] = cellrec.get('mags_discarded')
    return rec


def _mean_curve(rows):
    """Per-bullet residual, averaged over the kept magazines.

    Ragged rows are truncated to the shortest rather than padded: a bullet
    only some magazines reached would otherwise be averaged over a changing
    denominator, which puts a step in the tail of the curve exactly where the
    compensation is largest.
    """
    seqs = [r.get('per_bullet_counts') or [] for r in rows]
    seqs = [s for s in seqs if s]
    if not seqs:
        return None
    n = min(len(s) for s in seqs)
    return [float(sum(s[i] for s in seqs) / len(seqs)) for i in range(n)]


def reset(rigging, level=LIGHT):
    """Back to a known state. -> True when it got there.

    LIGHT undoes the states a failed cell leaves behind, and every one of them
    is a TOGGLE — comma opens AND closes the spawner, Tab opens AND closes the
    inventory, right click enters AND leaves ADS. Pressing one blind lands in
    the wrong state half the time and the failure is silent, so each goes
    through the control/ call that watches for the state it wants.

    The rack is deliberately NOT cleared here. Every cell strips and re-spawns
    on its way in, so clearing costs a Tab session per failure and buys
    nothing — and drop_weapon is the one action in this list that can put a
    gun on the floor, where the next spawn's eviction rules change under it.

    HEAVY re-enters the training range. Being evicted at 20 minutes IS a full
    reset (empty backpack, empty rack, random spawn point); this triggers the
    same thing on purpose rather than by the clock.
    """
    if level >= HEAVY:
        got, _ = rigging.session.ensure(force=True)
        if not got:
            return False
        # The measurable band is a property of where the character stands and
        # what they face, and re-entry moves both. Dropped rather than carried
        # over: a stale band puts the aim somewhere the tracker cannot see.
        rigging.rig.pitch_centre = 0
        # The rack and the backpack came back empty. measure() re-stocks on
        # its way into every cell, so there is nothing else to undo.
        return True

    ok = True
    try:
        # The panel first: comma's menu covers the screen and swallows Tab, so
        # every check below reads the panel instead of the game if it is up.
        ok &= bool(rigging.sc.ensure_panel(False))
    except Exception:
        ok = False
    try:
        # Then Tab, for the same reason one level down: the inventory hides
        # the posture icon and swallows C and Z.
        rigging.rig.ensure_inventory_closed()
    except Exception:
        ok = False
    try:
        # INTO ADS, not out of it. The contract this replaces said "stand,
        # hip-fire", which is backwards on both halves: there is no method
        # that leaves ADS (right click is a toggle and only ensure_ads()
        # watches it), and ensure_posture REQUIRES ADS because the posture
        # icon does not render from the hip. Resetting to the hip would make
        # the posture unreadable and the next cell would toggle blind.
        #
        # Nothing is lost by leaving the sight up: every cell enters ADS on
        # its way in regardless.
        if rigging.rig.ensure_ads():
            ok &= bool(rigging.rig.ensure_posture('standing'))
        else:
            ok = False
    except Exception:
        ok = False
    return ok


def dump(where, why, frames=None, state=None):
    """Write the evidence for a failure. -> the directory written.

    Exists because of a concrete afternoon: a spawner failure reported
    `<panel open, col1_row02 expanded, 12 entries>` and saved no frame, so
    diagnosing it needed a new probe and a live game session. The three
    numbers that settled it — 12 entries read, 13 in the ground truth, 5 in
    the row it was attributed to — were all available in that frame. With the
    frame on disk it is an offline question; without it, it is a night lost.

    Confirmed again the day this was wired: a kit failure was dumped four
    minutes late, by which time the game had logged us off for inactivity and
    the rack was gone. control/evidence.py writes its frame BEFORE it presses
    anything, for that reason.
    """
    from control.evidence import dump_state
    path, _ = dump_state(where, why, frames=frames, state=state)
    return path


def contract():
    """The contract as text, for --dry and for the refactor conversation."""
    out = ['measure(rigging, cell, mags) -> dict']
    for k, v in RECORD_FIELDS.items():
        out.append(f'    {k:<20} {v}')
    out += ['', 'reset(rigging, level) -> bool',
            f'    level={LIGHT} LIGHT  panel down, Tab shut, ADS up, standing',
            f'    level={HEAVY} HEAVY  re-enter the range — RangeSession'
            f'.ensure(force=True)',
            '', 'dump(where, why, frames=None, state=None) -> path',
            '    the failure frame and the state readout, on disk',
            '',
            'impulse_off_rounds is a per-SESSION gate, not a per-cell one:',
            '    pixi run impulse-ab, then night --impulse-off <rounds>.',
            '    Left unset every cell fails closed on `impulse`, which is',
            '    correct: an unverified timing chain makes the night worthless.']
    return '\n'.join(out)
