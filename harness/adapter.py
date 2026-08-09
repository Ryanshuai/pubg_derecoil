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

WHAT REPLACED THE ROUND-ALIGNMENT CHECK (2026-08-08). This file used to carry
an `impulse_off_rounds`: spike one bullet, watch which round moves.

⚠ THAT TECHNIQUE IS REJECTED, not merely retired. Three independent reasons,
any one of them fatal: the instant is not recorded accurately, the two
coordinates cannot be put in correspondence to begin with, and THE AMMO
COUNTER DOES NOT RESOLVE ROUNDS -- control/fire.py records that it reads about
five times in a 42-round magazine while firing. A check whose observable
resolves five times cannot say which of forty-two rounds moved.

It also has no referent under MODEL.md: there is ONE origin, the click, and
this repo sends it.

The NEED for a check the fit could not arrange did not go away, and `measure()`
now produces one per cell: it fires the magazines under MORE THAN ONE
compensation curve, and `_agreement()` asks whether adding each one's own
y_comp back makes them the same measurement. That is the assumption pooling
rests on, the fitter never sees which arm a magazine came from, and
docs/model_error_history.md records it failing at one end.
"""
import os
import sys
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
    'config_read':        'dict  — what the gun turned out to be WEARING, as '
                          'catalogue keys. THE POOLING KEY, not bookkeeping: '
                          'magazines are stored and fitted under it, so a '
                          'wrong one merges two different guns into one curve.',
    'sight_read':         'str   — the optic read off the gun, not the flag. '
                          'K comes from it and is worth ~3x.',
    'scope_asset':        'str   — the same optic as the raw asset the curve '
                          'lookup keys on.',
    'n_kept':             'int   — magazines in the fitter\'s main cluster, '
                          'over the WHOLE accumulated pool, not tonight\'s',
    'n_total':            'int   — magazines in the pool',
    'fired':              'int   — magazines this cell actually fired',
    'ads_frac':           'float — fraction of polls the crosshair said aiming. '
                          '⚠ nan on every magazine this path writes: the '
                          'timed grabber has no screen-centre crop.',
    'ads_end_ok':         'float — fraction of the pool whose burst ENDED in '
                          'ADS. Two endpoints, not a ratio, and the one ADS '
                          'reading this collection path can actually make. '
                          'An unreadable end counts as a failure.',
    'track_alive_frac':   'float — frame pairs the correlator could place',
    'agree_arms':         'int   — how many DIFFERENT compensation curves the '
                          'pool contains. Under 2 the pooling assumption is '
                          'untested and judge() fails the cell closed.',
    'agree_spread':       'float — how far those arms disagree about y_true in '
                          'the mid-band, as a fraction. THE out-of-loop check.',
    'agree_band':         '(lo, hi) — the seconds the arms were compared over. '
                          'NOT the constant: the upper end is capped by the '
                          'burst, because 2.4 s is an m416 number and a vector '
                          'magazine is empty by 1.7 s.',
    'span_s':             'float — how long the fitted curve runs',
    'total_counts':       'float — y_true at the end of the span',
    'spread_counts':      'float — median disagreement between kept magazines',
    'curve':              'list  — the fitted knots, for the log',
    'dropped':            'list  — what the clustering pushed out, and how far '
                          'each sat from the kept cluster. A gate that cannot '
                          'say what it refused cannot be retuned.',
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

    def __init__(self, rig, kit, sc, session, log, facts, parts):
        self.rig = rig
        self.kit = kit
        self.sc = sc
        self.session = session
        self.log = log
        self.facts = facts
        self.parts = parts

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


def open_rig(sight, out_dir, home=True, countdown=6,
             weapons=(), configs=('bare',)):
    """Build the rig, take the foreground, prove we are in the range.

    -> (Rigging, None) or (None, why). Never raises for a game-state problem:
    "PUBG is not running" is an answer the morning can read, and a traceback
    out of a 4 a.m. process is not.
    """
    from control.kitting import (Kitter, MAG_FOR_CLASS, PART_FOR_CLASS,
                                     SCOPE_PART, parse_config, stock_parts)
    from calibration.kit_facts import KitFacts
    from calibration.range_session import get_session
    from calibration.sweep import Rig
    from control.session import ensure_ready
    from control.spawner import ROSTER, SpawnerControl

    # What the run needs on hand. Same derivation as harvest's, because it is
    # the same question: only the slots some config actually fills, plus the
    # pinned sight and magazine.
    #
    # ⚠ THE PARTS THE CONFIGS NAME, NOT JUST THE SLOTS THEY FILL. This read
    # `[table.get(s) for s in wanted_slots]` while a config was only "which
    # slots", so it could only ever stock the class representative. The moment
    # a config could say `grip=half_grip` that became a silent hole: the
    # planner schedules the cell, the backpack never holds the part, ensure_kit
    # cannot fit it, and read_config refuses -- one failure per cell, four in a
    # row, and the night halts on a stocking bug wearing a kitting bug's face.
    fills = [f for f in (parse_config(c) for c in (configs or ())) if f]
    parts = {SCOPE_PART}
    for w in weapons:
        cls = ROSTER.get(w, (None,))[0]
        table = PART_FOR_CLASS.get(cls, {})
        for fill in fills:
            parts.update(x for x in (p or table.get(s)
                                     for s, p in fill.items()) if x)
        parts.update(x for x in [MAG_FOR_CLASS.get(cls)] if x)

    # ⚠ THE FIVE LEGS BEFORE ANYTHING IS BUILT, and this door of all doors.
    # It used to open with a bare ensure_focus + sleep(0.6) and then lean on
    # session.ensure(), which on the happy path — already in a match, budget
    # not spent — RETURNS WITHOUT CALLING enter() AT ALL. So on a normal night
    # start, AutoSession.enter's careful ensure_ready() never ran: Tab was
    # never checked and the character was never walked to the 200m lane. The
    # re-entry path had been fixed; the START path had not, and the start path
    # is the one every unattended night takes.
    #
    # Two of them are not decoration here. Tab swallows the 1/2 weapon keys,
    # so a night that starts with the inventory up spawns a gun it never
    # holds. And the teleport — which rides along with the MATCH leg since
    # 2026-08-08, rather than being a leg of its own — is the difference
    # between measuring recoil and measuring recoil plus whoever drove through
    # the spawn compound. A 45-minute run at the compound is exactly what this
    # harness is for.
    #
    # BEFORE the Rig, so a refusal has nothing to close, and so the Pointer
    # ensure_ready opens is shut before Rig takes the port (its docstring says
    # so, and fit_pitch_level.py takes the same order).
    rec = ensure_ready(label='the night', countdown_s=countdown, verbose=True)
    if not rec['ok']:
        where = rec.get('failed')
        if where == 'range':
            return None, ('in a match, but could not reach the 200m lane — '
                          'the lobby may have been left on a mode other than '
                          'the training range')
        return None, {
            'focus': 'PUBG is not in the foreground and would not take it',
            'match': 'could not get into a match',
            'tab': 'the inventory screen would not close',
            'panel': 'the item spawner panel would not close',
        }.get(where, f'not ready: failed at {where!r}')

    # ⚠ GDI HERE, BECAUSE THE BURST PATH OWNS DXGI. There is one duplication
    # interface per output per process -- DXGISyncGrabber's own docstring says
    # "ONE CAMERA PER OUTPUT, PROCESS-WIDE ... The rig picks one" -- and
    # collect_timed.main has always built its Rig with prefer_dxgi=False for
    # exactly this. This one did not, so collect_into_store's grabber was
    # handed the RIG'S camera, and the first cell's `finally: grabber.close()`
    # released it out from under the rig. Measured 2026-08-09: one cell died of
    # an unrelated fault and the next three all read `CaptureLost: bettercam
    # capture thread is gone`, which is the night's halt streak spent on a
    # resource conflict rather than on anything about the game.
    rig = Rig(sight, prefer_dxgi=False)
    rig.view.use_homing = home
    sc = SpawnerControl()
    kit = Kitter(rig)
    kit.restock_fn = lambda need: stock_parts(sc, kit, set(need) | parts)

    # Constructed AFTER the gate rather than driving it: its job is the
    # 17-minute recycle during the night, and ensure() is a cheap no-op here
    # (in_range() is true, the budget is fresh) — kept so `_entered` is stamped
    # from a session that has actually been verified.
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

    r = Rigging(rig, kit, sc, session, log, KitFacts(), parts)
    return r, None


def measure(rigging, cell, mags):
    """Measure one cell. -> a record with every RECORD_FIELDS key.

    MUST NOT raise for a game-state problem: a weapon that would not spawn is
    `reached=False` with a reason, not an exception. Exceptions are for the
    harness being wrong (bad arguments, missing hardware), and the loop lets
    those out.
    """
    from control.kitting import SIGHT_FOR

    weapon, posture = cell['weapon'], cell['posture']
    # WHICH SLOTS THIS CELL FILLS. Defaulted to 'bare' so a manifest written
    # before configs existed still resumes, and so a caller that does not care
    # gets what this always did.
    cfg = cell.get('config') or 'bare'
    rig, kit = rigging.rig, rigging.kit
    rec = _blank(cell)

    rig.set_sight(SIGHT_FOR.get(weapon, cell.get('sight', 'red_dot')))

    # ⚠ ONE TRIP INTO THE BACKPACK FOR THE WHOLE CELL. Everything from here to
    # the readback needs the Tab screen UP and none of it needs it OPENED, so
    # the state is established once and declared -- Kitter.session()'s own
    # docstring has said exactly that since 2026-08-06, and until now its ONLY
    # caller was apply(), one of its own methods. The mechanism was built, the
    # four internal closes were gated on it, and nobody held it for a weapon.
    #
    # What it cost, counted on this function: find_gun opened and closed,
    # clear_rack opened and closed, stock_parts opened, the second find_gun
    # closed, apply opened and closed, read_loadout opened and closed. FIVE
    # cycles per cell, in the one path that runs unattended all night.
    #
    # The spawner still needs the screen DOWN, and it takes it: restock presses
    # Tab shut before spawn_missing and tidy re-opens through read_stock. That
    # is a real close/open pair with a reason, which is the distinction this
    # block is drawing -- not "never toggle Tab", but "toggle it for the
    # spawner, not for a dict comparison".
    #
    # ⚠ AND IT MUST NOT COVER THE FIRING. _collect is deliberately outside:
    # magazines are fired with the inventory shut, and holding it here would be
    # the same class of error one level up.
    with kit.session():
        reached = _reach(rec, rigging, weapon, cfg)
    if not reached:
        return rec

    # ── the measurement, MODEL.md's way ──────────────────────────────────
    #
    # ⚠ WHAT CHANGED IS NOT THE ARITHMETIC, IT IS WHAT COMES BACK. measure_cell
    # returned a JUDGED cell: magazines already dropped by collection-time
    # gates, per-bullet counts already binned, and an EMA already applied to
    # the curve on disk. Three decisions taken before anything else was
    # visible. Now the run APPENDS RAW SAMPLES and decides nothing: the
    # clustering picks the main cluster at fit time with every magazine in
    # view, and the fit is a full refit over the accumulated pool.
    #
    # ⚠ TWO ARMS, DELIBERATELY. verdict.judge's out-of-loop check needs
    # magazines fired under DIFFERENT curves — that is the only way to test
    # the assumption pooling rests on, and a fit cannot arrange it. The split
    # is here rather than in the night loop because it is a property of one
    # cell's measurement, and a caller that asked for one magazine should not
    # silently get an unverifiable cell.
    return _collect(rec, rigging, weapon, posture, mags, rec['scope_asset'])


def _reach(rec, rigging, weapon, cfg):
    """Spawn, kit and read the gun back. -> bool, with `reached_why` set on False.

    ⚠ EVERY LINE IN HERE NEEDS THE TAB SCREEN UP AND NONE OF THEM OPENS IT.
    That is the whole reason it is a function: measure() holds one
    kit.session() around the call, so the `with` can end before the firing
    starts. Splitting it changed no refusal — each one below is the one
    measure() has always made, in the order it always made it.
    """
    from control.kitting import (note_fits, parse_config, stock_parts,
                                 want_for)
    from control.spawner import ROSTER

    kit, sc = rigging.kit, rigging.sc

    # ⚠ KEEP THE GUN IF IT IS ALREADY THERE. The plan runs every config of one
    # weapon before moving to the next, so consecutive cells almost always
    # want the SAME gun with different attachments -- and throwing it away to
    # spawn an identical one costs a drop, a spawner visit and a restock, per
    # cell. 37 cells over 8 weapons needs 8 spawns, not 37. calibration's own
    # harvest_weapon has always worked this way; only this adapter did not,
    # because it was written when a cell meant a weapon.
    #
    # The clear is still right when the gun DOES change, and the reason it was
    # written is unchanged: relying on the incoming gun to evict the old one
    # leaks a magazine per cell, because eviction only happens once the rack is
    # FULL and an evicted gun leaves wearing everything it had on. See
    # Kitter.clear_rack.
    #
    # Nothing is inherited by keeping it: want_for pins every controlled slot,
    # filled or empty, so kit.apply below strips whatever the previous config
    # left on. That is the same guarantee the spawn-fresh path relied on.
    already = kit.find_gun(weapon) is not None
    if not already:
        kit.clear_rack()
    if not stock_parts(sc, kit, rigging.parts, also=() if already else (weapon,),
                       loose_only=True):
        rec['reached_why'] = f'could not stock the parts or produce {weapon}'
        return False
    # Where the gun actually landed, read rather than assumed. An empty rack
    # takes the first gun into slot 1, and range re-entry empties the rack --
    # which is why this is re-read even on the `already` path.
    if kit.find_gun(weapon) is None:
        rec['reached_why'] = f'{weapon} is not in the rack after the spawn'
        return False

    # Bare: pinned sight and magazine, every other controlled slot forced
    # EMPTY rather than left alone. The rule and the reason live in
    # harvest.want_for, in one copy, on purpose.
    # ⚠ EVERY SLOT THIS CELL DOES NOT FILL IS FORCED EMPTY, not left alone.
    # PUBG auto-fits whatever the backpack holds onto a gun the moment it
    # arrives, so an unmentioned slot is not empty -- it is whatever the last
    # teardown left lying around, and a cell labelled `bare` then quietly ran
    # wearing a grip. want_for is the one copy of that rule.
    fill = parse_config(cfg)
    if fill is None:
        rec['reached_why'] = f'unknown config {cfg!r}'
        return False
    want = want_for(weapon, ROSTER.get(weapon, (None,))[0], fill)
    if kit.apply(want, weapon=weapon) is None:
        # ⚠ FOUR FIELDS, AND THE FOURTH DECIDES WHETHER THIS IS EVIDENCE.
        # `verifiable` is False when the readback could not JUDGE the slot --
        # an AMBIGUOUS icon over a dark backdrop, or a part with no template --
        # as opposed to reading a different part, which is the game refusing.
        # Recording the first kind as a compatibility failure is how "the
        # templates cannot separate these two magazines" became "this weapon
        # will not take ext_smg" in kit_facts.json, and kit_facts is read by
        # humans deciding whether to edit the catalogue.
        #
        # It became a 4-tuple on 2026-08-07 and this unpack was still on three,
        # so the harness would have raised ValueError on the first cell that
        # failed to kit -- at 4 a.m., unattended, with the loop's own comment
        # about a KeyError costing a whole night one screen away.
        for slot_name, key, why, verifiable in kit.last_bad:
            if key and verifiable:
                rigging.facts.note_failure(weapon, slot_name, key, note=why)
        rec['reached_why'] = ('kit: ' + '; '.join(
            f'{s} {w}' for s, _, w, _ in kit.last_bad)) if kit.last_bad \
            else 'the kit would not go on'
        return False
    note_fits(rigging.facts, weapon, want)

    # ⚠ WHAT THE GUN TURNED OUT TO BE WEARING IS THE POOLING KEY, AND NOTHING
    # HERE WAS READING IT. `_collect` did `rec.get('config_read') or {}` and
    # NOTHING IN THIS REPOSITORY HAS EVER WRITTEN THAT FIELD -- so every cell
    # of every night appended its magazines to `<weapon>__bare`, fitted the
    # bare pool, and reported a number. Fourteen different configurations of
    # one gun would land in one file, each magazine plausible, the cv fine, and
    # the curve right for none of them. It is the repository's second
    # cross-layer law failing in the one place that fires unattended.
    #
    # It survived because night defaults to `--configs bare`, where the wrong
    # key and the right key are the same string. The moment a campaign names a
    # part, they stop being the same string.
    #
    # ⚠ ONE TRIP, TWO ANSWERS. read_loadout takes a single loadout and
    # read_config/read_sight are pure functions of it, so the config and the
    # optic describe the same observation by construction rather than by a
    # check somebody has to remember (calibration/CLAUDE.md, rule 14).
    from calibration.collect_timed import (read_config, read_loadout,
                                           read_sight)
    lo = read_loadout()
    config_read = read_config(lo, weapon)
    if config_read is None:
        # read_config prints WHICH of its refusals fired -- no gun, the wrong
        # gun, a second gun on the rack, or unreadable slots.
        rec['reached_why'] = 'could not read the attachment slots back'
        return False
    worn_sight, scope_asset = read_sight(lo)
    if worn_sight is None or worn_sight != rigging.rig.sight:
        # K comes from the optic. The magazine records the FLAG, so a
        # disagreement here is invisible to everything downstream -- and it is
        # worth about 3x between iron sights and a red dot.
        rec['reached_why'] = (f'the cell says sight {rigging.rig.sight!r} and '
                              f'the gun wears {worn_sight!r}')
        return False
    rec['config_read'] = config_read
    rec['sight_read'] = worn_sight
    rec['scope_asset'] = scope_asset
    print(f'      wearing {config_read or "(nothing)"}, sight {worn_sight}')
    return True


# How the magazines of one cell are split between compensation arms. The
# second arm is what makes the cell checkable at all (verdict.judge check 4),
# and one magazine is enough to compare against — the arms are compared as
# whole trajectories, not averaged.
#
# ⚠ `False` MEANS NO CURVE AT ALL, which measures y_true directly. It is the
# cheapest second arm and the one the mid-band measurement used
# (docs/model_error_history.md). It is NOT a better
# arm: that section measured its y_true at t=3.8 s as the lowest of four, 13%
# under the plateau, and named "the no-comp arm is an unbiased anchor" as one
# of the things it got wrong.
ARM_PLAN = (True, True, False)


def _blank(cell):
    """A record with every field present and nothing claimed.

    Present-and-None, not absent: verdict.judge() fails closed on a missing
    field precisely so a stage that never ran cannot read as a pass, and that
    only works if the record says which fields were never filled.
    """
    rec = {k: None for k in RECORD_FIELDS}
    rec.update({'reached': False, 'reached_why': '', 'cell': cell['id']})
    return rec


def _collect(rec, rigging, weapon, posture, mags, scope_asset=None):
    """Fire into the sample store, then fit the whole accumulated pool."""
    from calibration import samples as S
    from calibration.collect_timed import collect_into_store
    from calibration.fit_time_curve import fit

    # ⚠ NO `or {}` FALLBACK. It used to be `rec.get('config_read') or {}`,
    # which turned "nobody read it" into the positive claim "the gun is bare" —
    # and since nothing wrote the field, that claim was made on every cell.
    # measure() refuses above when the readback fails, so by here it is real.
    config = rec['config_read']
    fired, err = collect_into_store(
        rigging.rig, weapon, config, posture, mags, ARM_PLAN,
        note_prefix=f'night {rec["cell"]} ', scope_asset=scope_asset)
    if err:
        rec['reached_why'] = err
        if not fired:
            return rec

    # ⚠ THE FIT SEES THE WHOLE POOL, NOT TONIGHT'S MAGAZINES. Samples
    # accumulate across runs, curves and days (calibration/samples.py), so a
    # thin night on top of a fat history is a good cell. Fitting only what was
    # just fired would throw that away and reintroduce the per-round thinking
    # the store exists to end.
    pool = S.load(weapon, config)
    res = fit(pool)
    if not res.get('ok'):
        rec['reached_why'] = f'fit refused: {res.get("why")}'
        return rec
    return _fill(rec, res, pool, fired)


def _fill(rec, res, pool, fired):
    """Turn one fit over the accumulated pool into the harness's numbers."""
    rec['reached'] = True
    rec['fired'] = fired
    rec['n_kept'] = res['n_kept']
    rec['n_total'] = res['n_total']
    rec['span_s'] = res['span_s']
    rec['total_counts'] = res['total_counts']
    rec['spread_counts'] = res['spread_counts']
    rec['curve'] = res['knots']
    rec['dropped'] = res['dropped']

    # ADS over the pool, and the WORST magazine rather than the mean: a cell is
    # only as good as its weakest accepted magazine, and averaging lets four
    # clean ones carry one fired half from the hip.
    ads = [m.ads_frac for m in pool if m.ads_frac == m.ads_frac]
    rec['ads_frac'] = float(min(ads)) if ads else None

    # ⚠ AND `ads_frac` IS `nan` ON EVERY MAGAZINE THIS PATH HAS EVER WRITTEN.
    # The timed grabber captures the tracker's patches and AdsDetector reads the
    # SCREEN CENTRE, which is not among them, so the per-frame fraction cannot
    # be formed here at all -- 167 magazines in the store carry nan, and the
    # min() above therefore hands judge() a None on every cell. Another gate
    # that could not pass.
    #
    # What DOES exist is `ads_end`: ensure_ads before the trigger, one in_ads()
    # read at release. Two endpoints, not a ratio. It cannot see a burst that
    # dropped out and came back; it catches one that dropped out and stayed out,
    # which is the case worth ~3x in K.
    #
    # ⚠ THREE-VALUED ON PURPOSE. None means the read failed, and it is counted
    # as a failure, not skipped -- "nobody could tell" is the state this whole
    # layer exists to keep apart from "it was fine". The fraction is over the
    # WHOLE pool for the same reason the min() above is.
    ends = [m.ads_end for m in pool]
    rec['ads_end_ok'] = (float(sum(1 for e in ends if e is True)) / len(ends)
                         if ends else None)


    # Frame pairs the correlator placed, over the pool. `oor` is recorded per
    # pair and NOT dropped at collection (calibration/samples.py), so this is a
    # count of a real quantity rather than a rate among survivors — which is
    # what the old track_alive_frac had to work around.
    n = sum(len(m.oor) for m in pool)
    bad = sum(int(sum(bool(x) for x in m.oor)) for m in pool)
    rec['track_alive_frac'] = float((n - bad) / n) if n else None

    arms, spread, band = _agreement(pool)
    rec['agree_arms'] = arms
    rec['agree_spread'] = spread
    # ⚠ THE BAND THAT WAS ACTUALLY USED, because it is no longer the constant.
    # verdict's failure line quotes it, and quoting AGREE_BAND_S while the
    # comparison ran somewhere else is a report describing a different
    # measurement from the one it judged.
    rec['agree_band'] = band
    return rec


def _agreement(pool):
    """THE OUT-OF-LOOP CHECK. -> (n_arms, spread, band) — spread/band may be None.

    Magazines fired under DIFFERENT curves must, once each one's own y_comp is
    added back, estimate the SAME y_true. That is the assumption that makes
    pooling legal (calibration/samples.py), and it is the one thing here a fit
    cannot arrange: the fitter never sees which arm a magazine came from.

    Arms are keyed by the TOTAL the curve commands, rounded, so `--no-comp`,
    x0.5, x1 and x1.5 land in four groups without anybody labelling them. A
    pool with one arm returns (1, None) and judge() refuses the cell — "not
    checked" is not "passed".

    ⚠ THE MID-BAND, NOT THE ENDPOINT. docs/model_error_history.md measured
    the four arms
    agreeing to 0.9% at t=1.5 s and diverging to 15% by t=3.8 s, with the
    divergence coming entirely from the strongest arm in the last 1.1 s. That
    end is a known unexplained region; judging the model there would be judging
    it on the one part MODEL.md says is not understood. It is also the reason
    this returns a fraction OF y_true rather than counts: a 30-count
    disagreement means something different on a Vector than on an MG3.

    ⚠ AND THE UPPER END IS CAPPED BY THE BURST, WHICH IS NOT A DETAIL. 2.4 s is
    an m416 number and an m416 magazine runs 3.81 s. The vector fires 1130 rpm
    and empties in about 1.7 s, so NOT ONE of its magazines reaches 2.4 s, the
    `tt[-1] < hi` line below skips every single one, `curves` comes back empty
    and the cell fails on "agree_arms=1" -- with perfect data, forever. That is
    an unpassable gate, and this repository has already binned one for exactly
    that (the impulse check: nothing had written its field since the coordinate
    changed, so item 4 answered "not checked" for every cell). A gate that
    cannot pass is not a strict gate, it is a broken one.
    """
    import numpy as np
    from harness.verdict import AGREE_BAND_S, AGREE_BAND_EDGE_S, AGREE_BAND_MIN_S

    lo, hi = AGREE_BAND_S
    groups = {}
    for m in pool:
        key = int(round(sum(float(k.get('dy', 0.0)) for k in (m.curve or []))))
        groups.setdefault(key, []).append(m)
    if len(groups) < 2:
        return len(groups), None, None

    # ⚠ THE MEDIAN END, NOT THE MINIMUM. Burst length inside one cell is almost
    # a constant -- 115 m416 magazines span 3.80-3.81 s, 173 mp5k ones
    # 2.97-2.99 -- so the median is the weapon's own reach, while the minimum
    # is whatever the worst magazine did. Taking the minimum would let one
    # trajectory truncated by a lost tracker pull the band in on everybody
    # else, which is a cell quietly changing its own measurement conditions.
    traj = {}
    for m in pool:
        tt, yy = m.y_true_counts()
        if len(tt) >= 2:
            traj[id(m)] = (tt, yy)
    ends = sorted(t[-1] for t, _ in traj.values())
    if not ends:
        return 1, None, None
    hi = min(hi, float(ends[len(ends) // 2]) - AGREE_BAND_EDGE_S)
    if hi - lo < AGREE_BAND_MIN_S:
        # Still FAILS CLOSED, and for a reason worth telling apart from the one
        # above: there is no band to compare in, not "the arms disagreed".
        return 1, None, (lo, hi)

    grid = np.linspace(lo, hi, 25)
    curves = []
    for key, mags in sorted(groups.items()):
        ys = []
        for m in mags:
            got = traj.get(id(m))
            if got is None or got[0][-1] < hi:
                continue
            ys.append(np.interp(grid, got[0], got[1]))
        if ys:
            curves.append(np.nanmedian(np.vstack(ys), axis=0))
    if len(curves) < 2:
        # Arms exist but not enough of them reach the band. Reported as one
        # arm rather than as agreement: a comparison that could not be made is
        # not a comparison that passed.
        return 1, None, (lo, hi)
    M = np.vstack(curves)
    ref = float(np.nanmedian(M[:, -1]))
    if not ref:
        return len(curves), None, (lo, hi)
    return (len(curves),
            float(np.nanmax(np.nanmax(M, 0) - np.nanmin(M, 0)) / abs(ref)),
            (lo, hi))


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
        rigging.rig.view.pitch_centre = 0
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
        rigging.rig.gun.ensure_inventory_closed()
    except Exception:
        ok = False
    try:
        # INTO ADS, not out of it. The contract this replaces said "stand,
        # hip-fire", and the reason that is backwards is ensure_posture:
        # it REQUIRES ADS, because the posture icon does not render from the
        # hip. Resetting to hip fire would make the posture unreadable and the
        # next cell would toggle blind.
        #
        # ⚠ THE OTHER HALF OF THAT SENTENCE USED TO SAY "there is no method
        # that leaves ADS". There is one now -- GunDriver.ensure_hip, added
        # 2026-08-06 so the pitch can be positioned in one fixed aim state --
        # so if this ever wants hip fire it is available. It still does not
        # want it, for the posture reason above.
        #
        # Nothing is lost by leaving the sight up: every cell enters ADS on
        # its way in regardless.
        if rigging.rig.gun.ensure_ads():
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
            'the out-of-loop check is PER CELL and measure() arranges it:',
            '    magazines are fired under more than one compensation curve,',
            '    and agree_spread asks whether adding each one back makes',
            '    them the same y_true. One arm = untested = the cell fails.']
    return '\n'.join(out)
