"""Attachment factors from a harvest log, with the error bars that decide.

    pixi run python calibration/analyse_factors.py --jsonl calibration/ortho_0802.jsonl

harvest.py's own report prints point estimates, which is enough to notice a
pattern and not enough to act on one. The game randomises每 shot by about ±5%
of a magazine, so at three magazines a cell a 3% gap and a 0% gap can be the
same measurement. Everything here is about telling those apart:

  * per-slot factor with a standard error, from the per-magazine spread
  * whether combinations equal the product of their parts, in sigma
  * whether a factor is the same on every weapon, in sigma

The multiplicativity test is done in log space, where the ratios become
differences and the bare measurement's own noise cancels the way it actually
does rather than being double-counted:

    log(measured/predicted) = log m_C - sum_s log m_s + (n-1) log m_bare

Why it matters: the compensation model this feeds has a single has_att
boolean. If the slots multiply, N slots cost N measurements and the boolean
becomes a product of N numbers. If they do not, it is 2^N curves per weapon
and the whole approach has to change.
"""
import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The only import outside the standard library, and it is a lookup table of
# strings — this file stays runnable with no game, no hardware and no torch,
# which is the property that makes it usable on a laptop against a copied
# JSONL. See analysis.py's "numpy and nothing else" for the same rule.
from detector.attachment_catalog import ROSTER            # noqa: E402

TEST_SLOTS = ('muzzle', 'grip', 'stock')


def parse_config(name):
    if name == 'bare':
        return frozenset()
    if name == 'both':
        return frozenset(('muzzle', 'grip'))
    return frozenset(p for p in name.split('+') if p)

# ⚠ TWO PARTS IN ONE SLOT STILL COLLIDE, and the fix is one file per part, not
# a cleverer key. Measuring vert_grip and thumb_grip both give cells keyed
# (weapon, 'grip', posture), so loading them together keeps only the second.
#
# 2026-08-05 this was "fixed" by relabelling the config `grip[thumb_grip]`.
# That collides no more and DELETED THE WHOLE MUZZLE AXIS from the factor
# table without a word: "config name == slot name" is assumed in several
# places, not just parse_config, so every muzzle cell stopped being a muzzle
# cell. Reverted. Give each part its own --out and compare the per-file
# factors; each carries its own same-run bare, and the cross-part comparison is
# then an ordinary cross-run one, which BETWEEN_RUN_REL already widens.


# HOW FAR APART TWO MEASUREMENTS OF THE SAME CELL LAND IN DIFFERENT RUNS,
# over and above the magazine-to-magazine spread inside one. MEASURED on the
# m416's bare standing cell, the one every ratio in a factorial divides by,
# across the four runs that hold it at a full magazine:
#
#   ortho_0802b 1469 (sem 3.4%)   ortho_0802d   1564 (sem 1.6%)
#   ortho_0802c 1443 (sem 5.9%)   posture_0802  1564 (sem 1.9%)
#
#   spread of the four means 4.2%, mean within-cell sem 3.2%
#   -> sqrt(4.2^2 - 3.2^2) = 2.7% that belongs to the RUN, not the magazines
#
# It does not shrink with more magazines, so it is a floor: two cells measured
# in different sittings cannot be compared more finely than this however long
# either one is fired. Anything below ~5% needs the comparison to live inside
# one run instead.
#
# MOST OF IT IS THE REFERENCE METHOD, NOT THE GAME, which is worth knowing
# before spending magazines to beat it. The four runs above all returned each
# magazine to the cell's own remembered reference. Two runs an hour apart that
# HOMED instead (posture_axis_0804 / 0804b, --home, back to the pitch clamp)
# reproduce the same cell far tighter:
#
#   m416 standing 865 / 863   0.3%      m249 crouching  677 / 682   0.7%
#   mp5k standing 458 / 460   0.5%      m249 standing  1191 /1171   1.7%
#   ump45 crouching 437 / 427 2.3%      m416 crouching  729 / 750   2.9%
#
#   six of seven pairs within 2.9%, mean 1.4%
#
# The seventh, ump45 prone, disagrees by 13.1% (363 vs 414) and is independently
# suspect: its residual was 42% of the reconstructed truth and it puts that
# weapon's prone/crouch at 0.970 against 0.83-0.89 in three other runs. That is
# the shape to expect -- homed runs do not carry a blanket floor, they carry
# occasional cells that are individually identifiable as wrong.
#
# The value stays at the non-homed 0.027 because it is the conservative one and
# the archive is mostly non-homed. A run that homes throughout can be compared
# more finely than this says.
BETWEEN_RUN_REL = 0.027


def load(path, run=None):
    """(weapon, config, posture) -> one cell's per-magazine true recoils.

    A magazine is the unit of measurement, not a cell: the spread between
    magazines IS the game's randomness, and it is the only estimate of it
    available. Later cells win, so a --resume rerun replaces rather than
    doubles.

    `run` is carried on every cell because a ratio between two of them is only
    as good as the sitting they share -- see BETWEEN_RUN_REL, and `comparable`
    for what is done about it.
    """
    run = run or os.path.basename(path)
    cells = {}
    for line in open(path, encoding='utf-8'):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get('type') != 'cell':
            continue
        comp = r['comp_over_fired']
        samples = [comp + m['cum_counts'] for m in r.get('mags', [])]
        if samples:
            cells[(r['weapon'], r['config'], r.get('posture', 'standing'))] = {
                'samples': samples, 'want': r.get('want', {}),
                'fired': r.get('bullets_fired'),
                'run': run,
                'sight': r.get('sight'),
                'uncovered': r.get('bullets_uncompensated', 0),
                'oor': sum(m.get('n_out_of_range', 0) for m in r['mags']),
            }
    return cells


def comparable(a, b):
    """Why these two cells must not be ratioed, or None if they may be.

    TWO CELLS OF DIFFERENT LENGTHS ARE NOT TWO MEASUREMENTS OF THE SAME THING.
    Recoil accumulates over the magazine, so a cell cut short measures less of
    the curve, not less recoil per bullet. ortho_0802.jsonl holds an m416 bare
    standing cell that fired 16 rounds where its siblings fired 42; taken as a
    denominator it puts the crouching factor at 2.29, and nothing about the
    number says it came from a truncated magazine.

    Different runs are allowed but not free -- the caller widens the error by
    BETWEEN_RUN_REL instead. Different lengths are refused outright, because
    there is no widening that makes them mean the same thing.
    """
    if a['fired'] is not None and b['fired'] is not None:
        if abs(a['fired'] - b['fired']) > 1:
            return f"{a['fired']} rounds vs {b['fired']}"
    return None


def ratio(cell, base):
    """(factor, relative error, note) for cell/base, or None if incomparable.

    The note is empty for the clean case and names the reason the error bar
    was widened otherwise, so a row that carries one is visibly weaker rather
    than quietly weaker.
    """
    why = comparable(cell, base)
    if why:
        return None
    m, _, sem = stats(cell['samples'])
    b, _, bsem = stats(base['samples'])
    if not b:
        return None
    rel = math.hypot(log_sem(m, sem), log_sem(b, bsem))
    note = ''
    if cell['run'] != base['run']:
        rel = math.hypot(rel, BETWEEN_RUN_REL)
        note = 'cross-run'
    return m / b, rel, note


def stats(xs):
    n = len(xs)
    m = sum(xs) / n
    if n < 2:
        return m, 0.0, 0.0
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    return m, sd, sd / math.sqrt(n)


def log_sem(mean, sem):
    """Relative error, which is the standard error of the log."""
    return sem / mean if mean else float('inf')


def posture_check(cells, base_posture):
    """Is the posture factor the same whatever is bolted to the gun?

    The compensation model multiplies a posture factor by an attachment factor
    and never asked whether it may. If crouching costs a fixed fraction of the
    recoil, the crouch/stand ratio is the same on a bare gun as on a kitted
    one; if the two interact, it is not, and every kitted crouching shot is
    compensated with a number nobody measured.

    Same weapon, same config, two postures — so the weapon's curve and the
    attachments both cancel and what is left is the posture alone.
    """
    postures = sorted({k[2] for k in cells})
    others = [p for p in postures if p != base_posture]
    if not others:
        return
    print('\n' + '=' * 74)
    print(f'IS POSTURE ORTHOGONAL TO THE ATTACHMENTS?   ratio against '
          f'{base_posture}')
    print('=' * 74)
    for p in others:
        rows = []
        for (w, c, pp), cell in cells.items():
            if pp != p:
                continue
            base = cells.get((w, c, base_posture))
            if not base:
                continue
            got = ratio(cell, base)
            if got is None:
                print(f'    {w:<9}{c:<20}skipped — {comparable(cell, base)}')
                continue
            r, rel, note = got
            rows.append((w, c, r, rel, note))
        if len(rows) < 2:
            continue
        print(f'\n  {p}:')
        for w, c, r, rel, note in sorted(rows):
            print(f'    {w:<9}{c:<20}{r:>8.4f}  +-{r*rel:.4f}  {note}')
        rows = [(w, c, r, rel) for w, c, r, rel, _ in rows]

        # Two different questions live in these rows and pooling them answers
        # neither. Spread across CONFIGS within one weapon is the interaction
        # being tested; spread across WEAPONS is whether the posture factor is
        # weapon-independent, which is a separate claim entirely. Pooled
        # together, an m416/ump45 difference reads as "posture and attachments
        # interact" -- it once printed 3.5 sigma on exactly that.
        by_w = defaultdict(list)
        for w, c, r, rel in rows:
            by_w[w].append((c, r, rel))
        tested = False
        for w in sorted(by_w):
            group = by_w[w]
            if len(group) < 2:
                continue
            tested = True
            (c1, r1, e1), (c2, r2, e2) = sorted(group)[:2]
            gap = r2 / r1 - 1
            sig = abs(math.log(r2 / r1)) / math.hypot(e1, e2)
            print(f'    -> {w}: {c2} is {100*gap:+.1f}% off {c1}  '
                  f'({sig:.1f} sigma) '
                  f'{"— consistent with one factor" if sig < 2 else "— INTERACTION"}')
        if not tested:
            print('    -> no weapon has two configs here; the interaction '
                  'cannot be tested, only the weapon spread below')

        # And the other question, kept apart.
        by_c = defaultdict(list)
        for w, c, r, rel in rows:
            by_c[c].append((w, r, rel))
        for c in sorted(by_c):
            group = by_c[c]
            if len(group) < 2:
                continue
            (w1, r1, e1), (w2, r2, e2) = sorted(group)[:2]
            sig = abs(math.log(r2 / r1)) / math.hypot(e1, e2)
            print(f'    -> weapon spread at {c}: {w1} {r1:.4f} vs {w2} '
                  f'{r2:.4f} ({sig:.1f} sigma)')

        # ── does a CLASS share one factor? ──
        #
        # The shipped model already assumes it does: detector/weapon.py keys
        # _POSTURE_DEFAULTS by weapon TYPE, so every AR without a per-weapon
        # override fires on the same number. Nothing had ever tested it -- the
        # 0802 data had one AR and one SMG, which can say the classes differ
        # and cannot say anything about the guns inside one.
        #
        # Reported per class rather than pooled into a single verdict: the
        # classes are already known to disagree with each other (prone reads
        # ~0.51 on the m416, ~0.64 on the ump45, ~0.24 on the mg3), so a pooled
        # spread would be dominated by that and would say nothing about the
        # question here.
        by_cls = defaultdict(list)
        for w, c, r, rel in rows:
            cls = (ROSTER.get(w) or ('?', None))[0]
            by_cls[cls].append((w, r, rel))
        multi = {k: v for k, v in by_cls.items() if len(v) > 1}
        if multi:
            print(f'\n    does a CLASS share one {p} factor?')
        for cls, group in sorted(multi.items()):
            ws = [(math.log(r), 1 / rel ** 2) for _, r, rel in group if rel]
            if not ws:
                continue
            mu = sum(l * wt for l, wt in ws) / sum(wt for _, wt in ws)
            print(f'      {cls}: pooled {math.exp(mu):.4f}')
            for w, r, rel in sorted(group):
                dev = (math.log(r) - mu) / rel if rel else float('inf')
                print(f'        {w:<9}{r:>8.4f}   {dev:+5.1f} sigma')
            worst = max(abs((math.log(r) - mu) / rel)
                        for _, r, rel in group if rel)
            print(f'        -> {"one factor fits the class" if worst < 2 else "PER-WEAPON, not per-class"}'
                  f' (worst {worst:.1f} sigma, n={len(group)})')
        for cls, group in sorted(by_cls.items()):
            if len(group) == 1:
                print(f'      {cls}: only {group[0][0]} ({group[0][1]:.4f}) — '
                      f'one weapon cannot answer this')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # action='append' as well as nargs='+', so BOTH shapes work:
    #     --jsonl a.jsonl b.jsonl        (one flag, several files)
    #     --jsonl a.jsonl --jsonl b.jsonl
    # Plain nargs='+' silently DISCARDS every repeat but the last, and this
    # analysis is exactly where that is expensive: a repeated flag looks like
    # pooling two runs and instead reports the second one alone, with the
    # missing cells showing up as "no combination cells with all their
    # single-slot cells present" rather than as an error. Cost one wrong
    # write-up on 2026-08-05.
    ap.add_argument('--jsonl', nargs='+', action='append',
                    help='run logs to read. Defaults to the most recently '
                         'written one under docs/recoil/runs/, which is the '
                         'question this is nearly always asked — "what did '
                         'the run I just finished say". Naming several pools '
                         'them; see BETWEEN_RUN_REL for what that costs.')
    ap.add_argument('--posture', default='standing')
    args = ap.parse_args()

    # action='append' nests: [[a, b], [c]] -> [a, b, c]
    paths = [p for group in (args.jsonl or []) for p in group]
    if not paths:
        runs = sorted(glob.glob(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'docs', 'recoil', 'runs', '*.jsonl')), key=os.path.getmtime)
        if not runs:
            print('[!] no run logs under docs/recoil/runs/ — name one with '
                  '--jsonl')
            return 1
        paths = [runs[-1]]
        print(f'reading the newest run: {os.path.relpath(paths[0])}\n')

    cells = {}
    for p in paths:
        # LATER FILES OVERWRITE EARLIER ONES for the same cell, which is what
        # --resume wants and is a trap everywhere else: pooling two runs of
        # one factorial silently keeps the second and reports it as though the
        # first had never been fired. Said out loud rather than fixed, because
        # merging them is also wrong -- two sittings are two measurements, not
        # six magazines (BETWEEN_RUN_REL).
        fresh = load(p)
        clash = [k for k in fresh if k in cells]
        if clash:
            print(f'[!] {os.path.basename(p)} replaces {len(clash)} cell(s) '
                  f'already read: {", ".join(f"{w}/{c}/{po}" for w, c, po in sorted(clash)[:4])}'
                  + (' ...' if len(clash) > 4 else ''))
        cells.update(fresh)

    # ── the sight axis ──
    #
    # Cells are keyed (weapon, config, posture) and NOT by sight, so the same
    # gun measured at two magnifications collides on that key and the second
    # file silently replaces the first. That makes the one comparison the
    # scope axis needs impossible to ask for.
    #
    # The comparison itself is already designed, in harvest.py's SIGHT_SCOPE:
    # fire the same bare/part pair at two sights and compare the FACTORS. The
    # sight cancels inside each ratio, so equal factors mean the sight is
    # orthogonal to that slot and unequal ones mean it is not.
    #
    # So when more than one sight is present, the sight goes into the weapon
    # LABEL. Everything downstream then works unchanged — single-slot factors
    # per column, and the pooled comparison with its inverse-variance weights
    # and cross-run widening — and the column header says which axis is being
    # compared instead of leaving the reader to assume "weapon".
    sights = {v.get('sight') for v in cells.values() if v.get('sight')}
    by_sight = len(sights) > 1
    if by_sight:
        cells = {(f"{w}@{v.get('sight') or '?'}", c, p): v
                 for (w, c, p), v in cells.items()}
        print(f'[i] {len(sights)} sights present ({", ".join(sorted(sights))}) '
              f'— columns are weapon@sight, and the "same part, different '
              f'columns" table below is comparing SIGHTS where the weapon '
              f'matches.')
    all_cells = dict(cells)
    cells = {k: v for k, v in cells.items() if k[2] == args.posture}
    if not cells:
        print('[!] no cells')
        return 1

    weapons = sorted({k[0] for k in cells})
    configs = sorted({k[1] for k in cells},
                     key=lambda c: (len(parse_config(c)), c))

    # ── per cell ──
    print('=' * 74)
    print(f'CELLS  ({args.posture})   true recoil in counts over one magazine')
    print('=' * 74)
    print(f'{"weapon":<9}{"config":<20}{"n":>3}{"mean":>9}{"sd":>8}'
          f'{"sem":>7}{"cv":>7}   flags')
    for w in weapons:
        for c in configs:
            cell = cells.get((w, c, args.posture))
            if not cell:
                continue
            m, sd, sem = stats(cell['samples'])
            flags = []
            if cell['oor']:
                flags.append(f"oor={cell['oor']}")
            if cell['uncovered']:
                flags.append(f"{cell['uncovered']} past curve")
            if len(cell['samples']) < 2:
                flags.append('single magazine — no spread')
            print(f'{w:<9}{c:<20}{len(cell["samples"]):>3}{m:>9.1f}{sd:>8.1f}'
                  f'{sem:>7.1f}{100*sd/m if m else 0:>6.1f}%   '
                  f'{", ".join(flags)}')

    # ── single-slot factors ──
    print('\n' + '=' * 74)
    print('SINGLE-SLOT FACTORS   ratio to that weapon\'s own bare')
    print('=' * 74)
    fac = {}
    print(f'{"weapon":<9}{"slot":<10}{"part":<16}{"factor":>9}{"+-":>8}')
    for w in weapons:
        bare = cells.get((w, 'bare', args.posture))
        if not bare:
            print(f'{w:<9} no bare cell — every ratio for this weapon is '
                  f'unavailable')
            continue
        for s in TEST_SLOTS:
            cell = cells.get((w, s, args.posture))
            if not cell:
                continue
            part = (cell.get('want') or {}).get(s) or s
            got = ratio(cell, bare)
            if got is None:
                print(f'{w:<9}{s:<10}{part:<16}   skipped — '
                      f'{comparable(cell, bare)}')
                continue
            r, rel, note = got
            fac[(w, s)] = (r, rel, part)
            print(f'{w:<9}{s:<10}{part:<16}{r:>9.4f}{r*rel:>8.4f}  {note}')

    # ── is a factor the same on every weapon? ──
    # Grouped by PART, not by slot. The classes fit different hardware in the
    # same slot — comp_ar on a rifle, comp_smg on an SMG — so a "muzzle"
    # column pooled across them compares two different objects and calls the
    # difference a weapon effect.
    per_slot = defaultdict(list)
    for (w, s), (r, rel, part) in fac.items():
        per_slot[part].append((w, r, rel))
    if any(len(v) > 1 for v in per_slot.values()):
        print('\n' + '=' * 74)
        print('IS THE FACTOR THE SAME IN EVERY COLUMN?   same part, '
              'different weapon (or sight), in sigma')
        print('=' * 74)
        for s, entries in sorted(per_slot.items()):
            if len(entries) < 2:
                print(f'\n  {s}: only measured on '
                      f'{entries[0][0]} ({entries[0][1]:.4f}) — one weapon '
                      f'cannot answer this')
                continue
            # Inverse-variance weighted mean in log space.
            ws = [(math.log(r), 1 / rel ** 2) for _, r, rel in entries if rel]
            mu = sum(l * wt for l, wt in ws) / sum(wt for _, wt in ws)
            pooled = math.exp(mu)
            print(f'\n  {s}: pooled {pooled:.4f}')
            for w, r, rel in sorted(entries):
                dev = (math.log(r) - mu) / rel if rel else float('inf')
                print(f'    {w:<9}{r:>9.4f}   {dev:+6.1f} sigma from pooled')
            worst = max(abs((math.log(r) - mu) / rel) for _, r, rel in entries
                        if rel)
            print(f'    -> {"consistent" if worst < 2 else "NOT the same in every column"}'
                  f' (worst {worst:.1f} sigma)')

    # ── multiplicativity ──
    print('\n' + '=' * 74)
    print('DO THE SLOTS MULTIPLY?   measured / (product of single-slot factors)')
    print('=' * 74)
    print(f'{"weapon":<9}{"config":<20}{"pred":>8}{"meas":>8}{"gap":>8}'
          f'{"sigma":>8}   verdict')
    any_combo = False
    for w in weapons:
        bare = cells.get((w, 'bare', args.posture))
        if not bare:
            continue
        bm, _, bsem = stats(bare['samples'])
        b_rel = log_sem(bm, bsem)
        for c in configs:
            slots = parse_config(c)
            if len(slots) < 2:
                continue
            cell = cells.get((w, c, args.posture))
            if not cell or any((w, s) not in fac for s in slots):
                continue
            got = ratio(cell, bare)
            if got is None:
                print(f'{w:<9}{c:<20}   skipped — {comparable(cell, bare)}')
                continue
            any_combo = True
            meas, _, note = got
            pred = 1.0
            for s in slots:
                pred *= fac[(w, s)][0]
            # log(meas/pred) = log m_C - sum log m_s + (n-1) log m_bare
            #
            # THE BARE CELL ENTERS WITH WEIGHT (n-1) AND IT IS COMMON-MODE.
            # Every combo in a run divides by the same bare, so an error there
            # does not average out across the rows -- it slides all of them the
            # same way, and the spread AMONG the rows cannot see it. Measured:
            # ortho_0802c and 0802d are the same m416 factorial run twice, and
            # all four gaps flipped sign together, -6.0/-6.2/-5.6/-9.9% against
            # +0.5/+2.2/-0.8/+5.9%. c's bare cell read 8% low with a 5.9% sem;
            # that one cell is the whole difference.
            #
            # So: fire more magazines into `bare` than into anything else, and
            # measure it again at the end of the run as a control.
            # RAW CELL ERRORS, NOT RATIO ERRORS. The identity above already
            # accounts for bare exactly once per term plus (n-1) at the end;
            # feeding it ratio() errors instead double-counts it, because each
            # ratio carries bare's error inside it. That mistake made the
            # variance too big and every sigma too small -- ump45's
            # muzzle+grip moved from 2.8 ("NOT multiplicative") to 1.5
            # ("multiplicative") on unchanged data, which is a verdict flipped
            # by an accounting error rather than by a measurement.
            m, _, sem = stats(cell['samples'])
            var = log_sem(m, sem) ** 2
            for s in slots:
                cs = cells[(w, s, args.posture)]
                sm, _, ssem = stats(cs['samples'])
                var += log_sem(sm, ssem) ** 2
                if cs['run'] != bare['run']:
                    note = note or 'cross-run'
            var += ((len(slots) - 1) * b_rel) ** 2
            # Once, not once per cell: the runs are what differ, so pooling
            # cells from two sittings costs one BETWEEN_RUN_REL however many
            # of them crossed.
            if note == 'cross-run':
                var += BETWEEN_RUN_REL ** 2
            sig = math.sqrt(var)
            delta = math.log(meas) - math.log(pred)
            gap = 100 * math.expm1(delta)
            nsig = delta / sig if sig else float('inf')
            verdict = ('multiplicative' if abs(nsig) < 2 else
                       'NOT multiplicative')
            print(f'{w:<9}{c:<20}{pred:>8.4f}{meas:>8.4f}{gap:>7.1f}%'
                  f'{nsig:>+8.1f}   {verdict} {note}')
    if not any_combo:
        print('  no combination cells with all their single-slot cells present')

    posture_check(all_cells, args.posture)

    print('\nReading this: a gap is only real if sigma says so. At three')
    print('magazines a cell the game\'s own randomness alone produces gaps of')
    print('a few percent, so "3% off" and "exactly right" can be the same')
    print('measurement. Add magazines before believing a small effect.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
