"""What is measured, what is guessed, and what has never been fired.

Read-only. Touches no hardware and writes nothing.

⚠ THE STATES ARE NOT A SPECTRUM, AND CONFLATING THEM IS THE POINT OF THIS
FILE. A cell is one of

    measured   fitted from the store, and the arms agree about y_true
    DISAGREE   the arms exist and they do NOT agree. Fitted, shipped, and
               failing the one check the fitter cannot arrange
    ONE-ARM    fitted, but every magazine saw the same curve, so the model's
               own pooling licence has never been checked here. `verdict.py`
               fails such a cell closed, and this file must too -- "not
               checked" and "fine" are the two things that gate exists to keep
               apart
    MIXED      the file holds magazines fired through more than one optic, so
               it is not one cell. `fit_time_curve` refuses such a pool, and
               NO SPREAD IS PRINTED for it -- see below
    SEED       a shape imported from a community pattern. It plays, so the
               view stays on texture and the gun becomes measurable. It is
               not a measurement and never was

Counting seeds as coverage is how a roster reads 72/72 while a third of it has
never been fired. `source` in the curve file is what separates them, so read
the file rather than the directory listing.

⚠ THE AGREEMENT IS NOT RECOMPUTED HERE. `harness.adapter._agreement` is the
author of that gate and this imports it, because a survey that re-derives the
criterion it is surveying will eventually disagree with the judge and be
believed -- it is the one printing the summary.

⚠ AND IT MUST DESCRIBE THE POOL THE FITTER WOULD ACTUALLY USE. The sample
path keys on (weapon, config, fire_mode) and NOT on the optic, so a scope
experiment firing one gun through four sights lands every magazine in one
file. Run over that raw file, the agreement came back 83.2% for mp5k bare
where the red-dot magazines alone give 3.7% -- a number that reads exactly
like a verdict on the cell and is a verdict on nothing. `fit_time_curve`
refuses a mixed pool outright, so this reports MIXED and prints no spread:
a survey whose object differs from the judge's object is this repository's
second cross-layer law, failed by the file written to survey it.

⚠ THIS IS A REPORT, NOT A GATE. It always exits 0 on purpose, and that is
exactly the shape `report_goto_paths` was deleted for (tools/CLAUDE.md): a
task that runs every time and cannot go red. The difference that earns this
one its keep is that its answer is a FUNCTION OF THE STORE and changes with
every magazine fired, so it can never be written down in a doc and retired --
which is precisely what happened to that one. If a day comes when this prints
a fixed table, delete it.
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg                                       # noqa: E402
from calibration import samples as S                       # noqa: E402
from control.spawner import ROSTER                         # noqa: E402
from detector.attachment_catalog import ATTACHMENTS, fits, has_slot  # noqa: E402
from harness.adapter import _agreement                     # noqa: E402
from harness.verdict import AGREE_ARMS_MIN, AGREE_SPREAD_MAX  # noqa: E402

# ⚠ "IS A CURVE THE RIGHT SHAPE FOR THIS GUN" IS NOT A CLASS QUESTION, and
# this file asked it as one until 2026-08-10. `SPRAY_CLASSES = ('AR','SMG',
# 'LMG')` put the M16A4 and the Mk47 -- both burst-and-single, no full auto --
# in the "never fired, and a curve is right for it" list, i.e. it reported two
# gaps that are not gaps. Seventeen curve files were seeded off that report
# before anyone noticed. config.sprays() is the one author now.

# Parts the campaign does not fire. THE OPERATOR'S CALL, 2026-08-09, taken on
# the gap list this file printed: these three grips are not worth a cell.
#
# ⚠ IT IS A PLAN, NOT A CAPABILITY. `kitting.ensure_kit` will still fit any of
# them on request and `fits()` still says yes — a gun CAN wear a laser. What
# this says is that nobody is going to spend magazines finding out what it
# does, and the difference matters because the first is a fact about the game
# and the second is a decision about tonight.
#
# The one data-side note, and it covers exactly one of the three: `laser`
# measured 1.0058 on the m762 (n=5, data/kit_factors.json) — an identity, so
# a cell for it would cost 10 magazines to re-learn that it changes nothing.
# `light_grip` 0.8748 and `thumb_grip` 0.7847 are NOT identities; they are
# skipped because they were not asked for, and saying otherwise would dress a
# preference up as a measurement.
#
# ⚠ NEVER DROPPED SILENTLY. `--gaps` prints what this removed on its own line,
# because a plan that quietly bounds its own coverage reads afterwards as
# "we covered everything".
NOT_WORTH_FIRING = ('laser', 'light_grip', 'thumb_grip')


def _curve_state(path):
    """('seed'|'fit', n_magazines) for a curve file on disk."""
    try:
        d = json.load(open(path, encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return 'unreadable', 0
    if d.get('seed'):
        return 'seed', 0
    return 'fit', int(d.get('n_magazines') or 0)


def _cells():
    """Every cell that has a curve file, a sample file, or both.

    Keyed by the curve file's basename, which config.py authors -- so a cell
    that exists in both places lines up by construction rather than by two
    spellings agreeing.
    """
    out = collections.defaultdict(dict)
    for name in sorted(os.listdir(cfg.CURVES_DIR)):
        if not name.endswith('.json'):
            continue
        stem = name[:-len('.json')]
        state, n = _curve_state(os.path.join(cfg.CURVES_DIR, name))
        out[stem]['curve'] = state
        out[stem]['curve_mags'] = n
    if os.path.isdir(S.SAMPLE_DIR):
        for name in sorted(os.listdir(S.SAMPLE_DIR)):
            if not name.endswith('.jsonl'):
                continue
            out[name[:-len('.jsonl')]]['samples'] = os.path.join(
                S.SAMPLE_DIR, name)
    return out


def _round1(weapon):
    """{slot: [part, ...]} — every part that fits, per slot the gun HAS.

    ⚠ ASK THE CATALOGUE, NOT THE STORE. An axis a gun does not own is not a
    cheap cell, it is four guaranteed failures and a halted night (the
    calibrate-recoil skill's round-1 rule), and `has_slot` carries
    `conf=measured` per weapon rather than a guess off the wiki.
    """
    out = {}
    for slot in ('muzzle', 'grip', 'stock'):
        if not has_slot(weapon, slot):
            continue
        parts = sorted(k for k, at in ATTACHMENTS.items()
                       if at['slot'] == slot and fits(weapon, k))
        if parts:
            out[slot] = parts
    return out


def gaps():
    """What a gun that HAS been fired still owes round 1."""
    have = {n[:-len('.jsonl')] for n in os.listdir(S.SAMPLE_DIR)
            if n.endswith('.jsonl')}
    fired = sorted({h.split('__', 1)[0] for h in have})
    total, skipped = 0, 0
    for weapon in fired:
        missing = collections.OrderedDict()
        for slot, parts in _round1(weapon).items():
            gone = [p for p in parts
                    if f'{weapon}__{slot}-{p}' not in have]
            skipped += sum(1 for p in gone if p in NOT_WORTH_FIRING)
            gone = [p for p in gone if p not in NOT_WORTH_FIRING]
            if gone:
                missing[slot] = gone
        if f'{weapon}__bare' not in have:
            missing['bare'] = ['']
        n = sum(len(v) for v in missing.values())
        total += n
        cls = (ROSTER.get(weapon) or ('?',))[0]
        if not missing:
            print(f'{weapon:<8} {cls:<4} round 1 COMPLETE')
            continue
        print(f'{weapon:<8} {cls:<4} {n} cell(s) short')
        for slot, gone in missing.items():
            print(f'    {slot:<7} {" ".join(gone)}')
    print(f'\n{total} round-1 cells across {len(fired)} fired weapon(s).')
    print(f'{skipped} more are NOT PLANNED — {", ".join(NOT_WORTH_FIRING)}. '
          f'They fit and they are unmeasured; nobody is firing them.')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--weapon', help='only this gun')
    ap.add_argument('--cells', action='store_true',
                    help='one row per cell instead of one per gun')
    ap.add_argument('--gaps', action='store_true',
                    help='round-1 cells a FIRED gun still owes: bare plus '
                         'every compatible part alone, minus what is in the '
                         'store')
    a = ap.parse_args()
    if a.gaps:
        return gaps()

    cells = _cells()
    by_weapon = collections.defaultdict(list)
    rows = []

    for stem, rec in sorted(cells.items()):
        weapon = stem.split('__', 1)[0]
        if a.weapon and weapon != a.weapon:
            continue
        n_mags = n_arms = 0
        spread, worn = None, set()
        if rec.get('samples'):
            mags = S.load(weapon, path=rec['samples'])
            n_mags = len(mags)
            worn = {m.sight for m in mags if m.sight}
            # ⚠ ONLY WHEN IT IS ONE CELL. See the header: the agreement of a
            # pool the fitter would refuse is not a fact about the cell.
            if mags and len(worn) == 1:
                n_arms, spread, _band = _agreement(mags)
        curve = rec.get('curve', '-')
        if curve == 'seed':
            state = 'SEED'
        elif len(worn) > 1:
            state = 'MIXED'
        elif curve == '-':
            state = 'samples only'
        elif n_arms < AGREE_ARMS_MIN:
            state = 'ONE-ARM' if n_mags else 'fit, store gone'
        elif spread is None:
            state = 'NO BAND'
        elif spread > AGREE_SPREAD_MAX:
            state = 'DISAGREE'
        else:
            state = 'measured'
        rows.append((stem, weapon, state, n_mags, n_arms, spread, worn))
        by_weapon[weapon].append(state)

    if a.cells or a.weapon:
        print(f'{"cell":<52} {"state":<9} {"mags":>5} {"arms":>5} '
              f'{"spread":>7}  sights')
        for stem, _w, state, n_mags, n_arms, spread, worn in rows:
            sp = f'{100 * spread:.1f}%' if spread is not None else '-'
            print(f'{stem:<52} {state:<9} {n_mags:>5} {n_arms:>5} {sp:>7}  '
                  f'{",".join(sorted(worn)) or "-"}')
        print()

    print(f'{"weapon":<12} {"class":<5} {"cells":>5} {"measured":>8} '
          f'{"disagree":>8} {"one-arm":>7} {"mixed":>5} {"seed":>5}')
    seen = set()
    for weapon in sorted(by_weapon):
        seen.add(weapon)
        st = by_weapon[weapon]
        cls = (ROSTER.get(weapon) or ('?',))[0]
        print(f'{weapon:<12} {cls:<5} {len(st):>5} '
              f'{st.count("measured"):>8} {st.count("DISAGREE"):>8} '
              f'{st.count("ONE-ARM"):>7} {st.count("MIXED"):>5} '
              f'{st.count("SEED"):>5}')

    if a.weapon:
        return 0

    print()
    missing = [(w, c) for w, (c, _) in sorted(ROSTER.items())
               if w not in seen and cfg.sprays(w, c)]
    print(f'NEVER FIRED, and a spray weapon so a curve is the right shape '
          f'for it ({len(missing)}):')
    for w, c in missing:
        slots = [s for s in ('muzzle', 'grip', 'stock') if has_slot(w, s)]
        print(f'  {w:<10} {c:<4} slots: {", ".join(slots) or "none"}')

    other = [w for w, (c, _) in sorted(ROSTER.items())
             if w not in seen and not cfg.sprays(w, c)]
    print(f'\nNo curve, and NOT a spray weapon -- one kick per click, which is '
          f'not what this repo fits ({len(other)}):')
    print('  ' + ' '.join(other))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
