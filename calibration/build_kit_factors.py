"""Measured recoil factors, keyed by the KIT that was actually on the gun.

    pixi run python calibration/build_kit_factors.py            # report only
    pixi run python calibration/build_kit_factors.py --write    # -> data/kit_factors.json

WHY A TABLE AND NOT A PRODUCT OF PER-SLOT COEFFICIENTS. `analyse_factors.py`
posed this as the question its whole multiplicativity test exists to settle:

    "If the slots multiply, N slots cost N measurements and the boolean
     becomes a product of N numbers. If they do not, it is 2^N curves per
     weapon and the whole approach has to change."

They do not. Measured 2026-08-05, eight weapons, 28 cells:

  * slots multiply on ARs (<=2.8%) and FAIL on SMGs -- mp5k muzzle+grip+stock
    is +17.0% off the product, vector +10.1%
  * the same single part is a different number on different weapons --
    comp_smg is 0.594 on the mp5k and 0.711 on the vector, 5.5 sigma
  * which EDGE couples is per-weapon too: muzzle x grip for the mp5k, but the
    vector fails on muzzle+stock and grip+stock while muzzle x grip multiplies
  * posture x kit interacts with OPPOSITE SIGNS on two ARs (m416 +13.8/+21.6%,
    m762 -6.2/-7.0%, all >3.7 sigma), so one correction curve cannot cover it

A product of per-slot numbers cannot represent any of that. A table can. The
same conclusion had already been reached for the two weapons with complete
data, in a factor_model.md that is NOT on disk any more: "mp5k / vector: don't
multiply, and you don't need to -- all eight configurations are measured in
the table." The quote is kept because it is the finding; the file it was in
went with the corpus.

WHAT IS KEYED, AND WHY IT IS `want` RATHER THAN THE CONFIG LABEL. The label
collides: measuring vert_grip and thumb_grip both produce cells named `grip`,
and analyse_factors carries a warning about exactly that (its "fix" of
relabelling the config silently deleted the entire muzzle axis). The cell also
carries `want` -- the kit that was requested and read back -- so the collision
is a property of the LABEL, not of the data. Key on the kit.

⚠ ONLY muzzle / grip / stock. `scope` is applied separately as `scope_factor`
(PUBG scales ADS sensitivity by magnification, which is not a recoil property
of the gun) and `magazine` changes capacity, not recoil. Putting either in the
key would split every row into duplicates that differ by nothing.

⚠ RATIOS ARE TAKEN INSIDE ONE RUN ONLY. Two cells from different sittings
cannot be compared more finely than BETWEEN_RUN_REL (2.7%), which is larger
than most of the effects here -- `ratio()` widens the error bar and marks the
row when it happens, and every source below carries its own same-run bare so
it should not.

⚠ SOURCES ARE LISTED, NOT GLOBBED. calibration/artifacts/recoil/runs/ held 121 files including
runs taken before the magnification factor reached the curve, before posture
was re-checked per magazine, and before a magazine fired into the pitch clamp
was caught. Those produce confident wrong numbers, and a glob would eat them
without a word. Each entry below says why it is trusted; anything not listed
is not in the table.

⚠⚠ AS OF 2026-08-08 THIS FILE CANNOT REBUILD ANYTHING, AND THAT IS WHY THE
GUARD BELOW EXISTS. Two of its three legs are gone:

    calibration/analyse_factors.py   deleted with the bullet-bucket coordinate
    every path in SOURCES            not on disk; runs.legacy.tar.gz holds
                                     only .log and .png, zero .jsonl

Neither loss was about attachment factors. A kit factor is a MULTIPLIER on the
curve, not a point on it, so it survives the coordinate change untouched --
it just lost the reader and the corpus it happened to share a directory with.

So `data/kit_factors.json` and `data/kit_records.jsonl` ARE THE RECORD now.
They cannot be regenerated, and `detector/weapon_attachments.py` reads the
first one on import, on every run. The dangerous move is not that build()
fails -- it is that build() SUCCEEDS on zero sources, returns {}, and --write
lays an empty table over a live file. Every gun would then fall through to the
wiki coefficients, which this file's own coupling report measures at a median
34.7% off. That is the exact shape this project keeps paying for: a confident
wrong number, arrived at by a path where every step reported success.

main() refuses to write unless every SOURCE is on disk. --selftest still runs
and is still the thing worth running -- it asserts the runtime reads the table
and still falls through for kits that are not in it, which needs no sources at
all. That is why the analyse_factors import is inside build() rather than at
module scope: a checker that cannot import is a checker nobody runs.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

import config  # noqa: E402  (needs the sys.path.insert above)

OUT = config.KIT_FACTORS_PATH
RECORDS = config.KIT_RECORDS_PATH

# The slots that change RECOIL, and why scope and magazine are not among them,
# both live on the tuple itself in detector/attachment_catalog.py. It is the
# one module every layer may import, which is what lets the decoder
# (detector/weapon_attachments.py) read the same value instead of a copy.
from detector.attachment_catalog import RECOIL_SLOTS  # noqa: E402

# A measurement too loose to act on must not ship, because the fallback is not
# "nothing" -- it is 1.0, or the wiki product, and both are better than a
# number that could be anywhere.
#
# The one row this excludes is vss/cheek_pad at 0.8161 +- 0.2304 (rel 0.28,
# 0.80 sigma from identity). The since-deleted factor_model.md said of it
# "it establishes nothing"; shipping it would mean compensating as though the
# part removes 18% of the recoil when the same data is consistent with it
# removing none. Every other measured row is at rel <= 0.031, so this floor is
# not a close call anywhere -- it separates one useless cell from 28 good ones.
MAX_REL = 0.10

# Two runs measuring one cell should agree inside the cross-run floor
# (BETWEEN_RUN_REL, 2.7%). When they do not, the disagreement is a fact about
# the data and gets printed -- picking the longer cell silently would hide
# exactly the signal that says one of the two runs is wrong.
DISAGREE_REL = 0.027

# (path, why this run is trusted). All post-date the 2026-08-05 fixes:
# magnification into the curve (a3f4965), posture re-checked per magazine
# (b13c174), reload waited out before ADS (52a12be), clamp magazine kept out
# of the EMA (242ba14), weapon identity verified (ece73d2).
SOURCES = [
    ('calibration/artifacts/recoil/runs/ortho8_0805.jsonl',
     '8-weapon full factorial, 28 cells, 6 magazines each, same-run bares'),
    ('calibration/artifacts/recoil/runs/grips_m762_thumb_grip_0805.jsonl', 'm762 grip axis'),
    ('calibration/artifacts/recoil/runs/grips_m762_tilted_grip_0805.jsonl', 'm762 grip axis'),
    ('calibration/artifacts/recoil/runs/grips_m762_half_grip_0805.jsonl', 'm762 grip axis'),
    ('calibration/artifacts/recoil/runs/grips_m762_light_grip_0805.jsonl', 'm762 grip axis'),
    ('calibration/artifacts/recoil/runs/grips_m762_laser_0805.jsonl',
     'm762 grip axis -- the null with a positive control in the same run'),
    ('calibration/artifacts/recoil/runs/posture_x_kit_0805_m762.jsonl',
     'm762 posture x kit, the interaction a single posture factor cannot hold'),
    ('calibration/artifacts/recoil/runs/vss_still2_0805.jsonl',
     'vss cheek_pad, measured with NOTHING touching the pitch between '
     'magazines -- 0.7620 +- 0.0676 (3.5 sigma) against the same cell\'s '
     '0.8161 +- 0.2304 (0.80 sigma) when the aim was homed into the ground. '
     'Same magazine count; the error bar shrank 3.4x because the view stopped '
     'being shoved around, not because more was fired.'),

    # famas, 2026-08-07. One muzzle per file, each with its OWN bare fired in
    # the same invocation, 12-15 magazines a cell. With comp_ar (already in
    # ortho8_0805) this completes the weapon: famas has no lower rail and no
    # stock slot, so four muzzles plus bare IS its whole product -- the first
    # weapon with nothing left falling back to a wiki coefficient.
    #
    # Trusted because they were fired after the night's fixes, each of which
    # was producing confident wrong numbers before it: attachment cells no
    # longer write the base curve (the configs used to drag one shared file
    # in opposite directions), the AMBIGUOUS re-read finally moves the
    # backdrop it claims to move, the retry re-finds its row instead of
    # clicking a shifted one, and an early outlier is re-judged once there is
    # a distribution to judge it against.
    ('calibration/artifacts/recoil/runs/harvest_red_dot_0807_0805.jsonl',
     'famas muzzle=brake_ar, own same-run bare'),
    ('calibration/artifacts/recoil/runs/harvest_red_dot_0807_0810.jsonl',
     'famas muzzle=flash_ar, own same-run bare'),
    ('calibration/artifacts/recoil/runs/harvest_red_dot_0807_0822.jsonl',
     'famas muzzle=supp_ar, own same-run bare -- 0.975, and the suppressor '
     'measured 1.0016 +- 0.0040 across six other weapons, so this is the '
     'seventh point on that identity rather than a famas oddity'),
]

# ── the orthogonality assumption is OUT OF THIS FILE ───────────────────────
#
# It used to fill every unmeasured kit with the product of that weapon's
# measured single-slot factors (falling back to the wiki coefficient per slot),
# tagged src='derived'. The operator ended that on 2026-08-06: "配件表不要正交。
# 全部死记住，因为可能有耦合。" -- every combination gets its own remembered
# number, because the slots may couple.
#
# That is the instruction, and this project's own data is the argument for it:
#
#   ARs   slots multiply to within 2.8%
#   SMGs  off by up to 17.0% (mp5k muzzle+grip+stock), and WHICH edge couples
#         is per-weapon -- mp5k fails on muzzle x grip, while the vector
#         multiplies there and fails on muzzle+stock and grip+stock
#
# So there is no rule for when the product is safe: "ARs are fine" is a
# generalisation from four ARs, and the one thing measurement has established
# about coupling is that it is per-weapon and per-edge. 267 derived rows on
# 28 measurements was 90% of the table being a guess wearing the same shape as
# a fact -- and `src='derived'` protected a reader of the FILE, not the runtime,
# which read row['f'] either way.
#
# WHAT A MISS DOES NOW. It falls through to the wiki product in
# detector/weapon_attachments.attachment_factor, exactly as it did before this
# table existed. That path is also orthogonal, so this does not make the
# runtime free of the assumption -- it makes the TABLE free of it, so that
# "measured" and "assumed" stop being the same lookup. The difference matters
# the next time someone asks what this project actually knows.
#
# The unmeasured combinations are enumerated and counted instead (--todo), so
# "记住全部" is a work list rather than a wish.
DERIVE = False


def kit_key(want):
    """The kit as a stable string. '' is bare.

    Sorted so that muzzle+grip and grip+muzzle are the same row, and written
    slot=part so a reader can see which slot a part went in without consulting
    the catalogue.
    """
    return '+'.join(sorted(f'{s}={want[s]}' for s in RECOIL_SLOTS
                           if want.get(s)))


def missing_sources(sources=None):
    """Which SOURCES are not on disk. -> [rel, ...]

    Separate from build() because the answer decides whether build() may run at
    all, and a caller that has to parse `skipped` strings to find that out will
    eventually not bother.
    """
    return [rel for rel, _why in (sources or SOURCES)
            if not os.path.exists(os.path.join(ROOT, rel))]


def build(sources, verbose=True):
    """-> {weapon: {posture: {kit_key: row}}}, and a list of complaints.

    ⚠ Imported here rather than at module scope: calibration/analyse_factors.py
    was deleted 2026-08-08 and a module-scope import would take --selftest down
    with it, which is the one entry point that still works. See the guard in
    main() and the module docstring.
    """
    from calibration.analyse_factors import load, ratio, stats
    table, skipped = {}, []
    for rel, why in sources:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            skipped.append(f'{rel}: missing')
            continue
        cells = load(path)
        # Group by the two things a ratio must hold fixed. Posture is a level
        # of the table rather than a fixed slice because the kit factor
        # genuinely MOVES with posture -- that interaction is the thing a
        # single posture coefficient cannot carry.
        groups = {}
        for (weapon, _config, posture), cell in cells.items():
            groups.setdefault((weapon, posture), []).append(cell)
        for (weapon, posture), group in sorted(groups.items()):
            bare = [c for c in group if not kit_key(c['want'])]
            if not bare:
                skipped.append(f'{rel}: {weapon}/{posture} has no bare cell')
                continue
            base = bare[0]
            for cell in group:
                key = kit_key(cell['want'])
                if not key:
                    continue
                got = ratio(cell, base)
                if got is None:
                    skipped.append(
                        f'{rel}: {weapon}/{posture}/{key} not comparable to '
                        f'its bare')
                    continue
                f, relerr, note = got
                mean, _sd, _sem = stats(cell['samples'])
                row = {'f': round(f, 4), 'rel': round(relerr, 4),
                       'n': len(cell['samples']),
                       'counts': round(mean, 1),
                       'run': os.path.basename(rel),
                       'sight': cell.get('sight')}
                if note:
                    row['note'] = note
                prev = table.setdefault(weapon, {}).setdefault(posture, {})
                # More magazines wins. A cell measured twice is two estimates
                # of one number, and the longer one is the better estimate --
                # but say so, because silently dropping a disagreeing
                # measurement is how a table stops matching the runs.
                if key in prev:
                    old = prev[key]
                    gap = abs(row['f'] - old['f']) / max(old['f'], 1e-9)
                    keep = row if row['n'] > old['n'] else old
                    if verbose:
                        flag = ('  ⚠ BEYOND THE CROSS-RUN FLOOR'
                                if gap > DISAGREE_REL else '')
                        print(f'  ! {weapon}/{posture}/{key} measured twice: '
                              f'{old["f"]} ({old["run"]}, n={old["n"]}) vs '
                              f'{row["f"]} ({row["run"]}, n={row["n"]}) '
                              f'-- {gap * 100:.1f}% apart{flag}')
                    if gap > DISAGREE_REL:
                        # Neither is trustworthy enough to ship on its own:
                        # one of the two runs is wrong and this cannot say
                        # which. Fall back rather than pick.
                        skipped.append(
                            f'{weapon}/{posture}/{key}: two runs disagree by '
                            f'{gap * 100:.1f}% (floor {DISAGREE_REL * 100:.1f}%)'
                            f' -- {old["run"]} {old["f"]} vs {row["run"]} '
                            f'{row["f"]}')
                        prev.pop(key, None)
                        continue
                    prev[key] = keep
                else:
                    prev[key] = row

    # Drop what is too loose to act on, and say so by name.
    for weapon in list(table):
        for posture in list(table[weapon]):
            for key, r in list(table[weapon][posture].items()):
                if r['rel'] > MAX_REL:
                    skipped.append(
                        f'{weapon}/{posture}/{key}: rel {r["rel"]:.3f} > '
                        f'{MAX_REL} -- measured {r["f"]} +- '
                        f'{r["f"] * r["rel"]:.4f}, too loose to compensate on')
                    del table[weapon][posture][key]
            if not table[weapon][posture]:
                del table[weapon][posture]
        if not table[weapon]:
            del table[weapon]
    return table, skipped


# (label, expected, args to attachment_factor). The first group must come from
# the TABLE and the second must fall through to the product -- a regression
# that silently stopped consulting the table would still return plausible
# numbers, so both directions are asserted.
_COMP_AR = 'Muzzle_Compensator_Large_C'
_COMP_SMG = 'Muzzle_Compensator_Medium_C'
_VERT = 'Lower_Foregrip_C'
_HEAVY = 'Stock_Heavy_C'
_TAC = 'Stock_AR_Composite_C'
_CHEEK = 'Stock_SniperRifle_CheekPad_C'
_LASER = 'Lower_LaserPointer_C'
SELFTEST = [
    ('mp5k comp+vert+heavy', 0.4216, ('mp5k', _COMP_SMG, _VERT, _HEAVY)),
    ('mp5k heavy only', 0.8170, ('mp5k', '', '', _HEAVY)),
    ('mp5k comp+heavy', 0.4767, ('mp5k', _COMP_SMG, '', _HEAVY)),
    ('vector comp+vert+heavy', 0.5364, ('vector', _COMP_SMG, _VERT, _HEAVY)),
    ('aug comp+vert', 0.5969, ('aug', _COMP_AR, _VERT)),
    ('m762 crouching comp+vert', 0.5954,
     ('m762', _COMP_AR, _VERT, '', 'crouching')),
    ('m762 prone comp+vert', 0.5901, ('m762', _COMP_AR, _VERT, '', 'prone')),
    # Fall-through: not in the table, must keep the old behaviour exactly.
    ('mp5k tactical_stock -> no coefficient exists', 1.0,
     ('mp5k', '', '', _TAC)),
    ('m416 comp+vert -> wiki product', 0.7225, ('m416', _COMP_AR, _VERT)),
    # Was 1.0 here: the first attempt measured 0.8161 +- 0.2304 (rel 0.28) and
    # MAX_REL correctly kept it out. Re-measured 2026-08-05 with nothing
    # touching the pitch between magazines -- 0.7620 +- 0.0676, rel 0.089 --
    # so it ships now. Same gun, same magazine count; only the aim changed.
    ('vss cheek_pad', 0.762, ('vss', '', '', _CHEEK)),

    # ── the middle tier: this gun's OWN parts, added 2026-08-06 ──
    # Nobody has fired comp+vert+tactical on an mp5k, but all three slots are
    # answerable for that gun: 0.5907 * 0.7470 * (no stock number) = 0.4413.
    # Before this tier it returned the wiki 0.7225 -- 64% shallower than the
    # measured comp+vert alone, on a gun whose measured parts were sitting
    # right there.
    ('mp5k comp+vert+tactical -> own parts', 0.4413,
     ('mp5k', _COMP_SMG, _VERT, _TAC)),
    # THE SAME PART IS A DIFFERENT NUMBER ON A DIFFERENT GUN, which is the
    # whole reason the parts table is per weapon. 5.5 sigma apart, one wiki
    # number of 0.85 for both.
    ('mp5k comp_smg alone', 0.5907, ('mp5k', _COMP_SMG, '', '')),
    ('vector comp_smg alone', 0.7197, ('vector', _COMP_SMG, '', '')),
    # AND A MEASUREMENT MUST NOT LEAK ACROSS GUNS. laser is measured on the
    # m762 (1.0058) and nowhere else; asking the mp5k for it has to reach the
    # wiki's 1.0, not borrow the m762's number. Same call, two guns, so a
    # lookup that lost the weapon key would show up here rather than as a
    # plausible number in a run.
    ('m762 laser -> its own measurement', 1.0058, ('m762', '', _LASER, '')),
    ('mp5k laser -> wiki, NOT the m762 number', 1.0,
     ('mp5k', '', _LASER, '')),
]


def selftest():
    from detector.weapon_attachments import (attachment_factor,
                                             measured_kit_factor)
    bad = 0
    for label, want, args in SELFTEST:
        got = attachment_factor(*args)
        ok = abs(got - want) < 1e-3
        bad += not ok
        print(f'  {"OK  " if ok else "FAIL"} {label:44s} '
              f'{got:.4f} (want {want:.4f})')
    # An asset this build cannot name must miss the WHOLE row rather than be
    # dropped from the kit -- answering for "the kit minus that part" is the
    # confident wrong number this table exists to stop.
    got = measured_kit_factor('mp5k', 'standing', '', '', 'Stock_NOT_A_REAL_C')
    ok = got is None
    bad += not ok
    print(f'  {"OK  " if ok else "FAIL"} '
          f'{"unknown asset misses the whole row":44s} {got}')

    # A DERIVED ROW LEFT ON DISK MUST MISS. This build no longer writes any, so
    # the check has to plant one -- otherwise the refusal is untested code
    # guarding against a file this test never produces, which is how the last
    # three gates in this project turned out to reject nothing. Two sides: the
    # planted row is refused, and a measured row at the SAME key is not.
    import detector.weapon_attachments as wa
    real = wa._kit_factors
    try:
        for src, want in (('derived', None), ('measured', 0.5)):
            wa._kit_factors = {'mp5k': {'standing': {
                'stock=tactical_stock': {'f': 0.5, 'src': src}}}}
            got = measured_kit_factor('mp5k', 'standing', '', '', _TAC)
            ok = got == want
            bad += not ok
            print(f'  {"OK  " if ok else "FAIL"} '
                  f'{f"a row marked {src!r} is " + ("refused" if want is None else "used"):44s} '
                  f'{got}')
    finally:
        wa._kit_factors = real
    total = len(SELFTEST) + 3
    print(f'\n{total - bad}/{total} passed')
    return 1 if bad else 0


def records(table):
    """The table as a FLAT list, one record per measurement. -> [dict, ...]

    This is the shape the operator asked for on 2026-08-06 -- "每个枪每个配件
    都一条记录，反正就是一个系数，然后后边再分析". The nesting in
    kit_factors.json is a runtime index; this is the thing it is an index OF,
    and it is what any later analysis should read. A record whose `kit` has one
    entry IS a per-weapon-per-part coefficient; one with several is the
    coupling evidence. Nothing here is combined, ordered or collapsed.
    """
    out = []
    for weapon in sorted(table):
        for posture in sorted(table[weapon]):
            for key, r in sorted(table[weapon][posture].items()):
                kit = dict(p.split('=', 1) for p in key.split('+'))
                out.append(dict(weapon=weapon, posture=posture, kit=kit,
                                slots=len(kit), **r))
    return out


def parts(table):
    """The single-slot rows, per weapon. -> {weapon: {posture: {key: row}}}

    THE SECOND TABLE, and it is a PROJECTION rather than a second measurement:
    every row in it is also a row of `kits`, selected by having exactly one
    slot filled. Written out because it is the table that answers "what does
    THIS part do on THIS gun", which is the question MUZZLE_FACTOR and
    GRIP_FACTOR answer globally and wrongly -- comp_smg is 0.5907 on the mp5k
    and 0.7197 on the vector, 5.5 sigma apart, and the wiki has one number for
    both. Regenerated on every build, so it cannot drift from `kits`.
    """
    out = {}
    for weapon in sorted(table):
        for posture in sorted(table[weapon]):
            for key, r in sorted(table[weapon][posture].items()):
                if '+' not in key:
                    out.setdefault(weapon, {}).setdefault(posture, {})[key] = r
    return out


def coupling_report(table):
    """For every measured multi-slot kit whose parts are ALSO measured singly:
    how far off is the product? -> [(weapon, posture, kit, measured, per-weapon
    product, wiki product)]

    This is the "后边再分析" half, and it is the only thing that can say
    whether the middle tier of the runtime lookup is worth having. It compares
    three numbers on the same cell, which is what makes it an argument rather
    than an assertion.
    """
    from detector.attachment_catalog import ATTACHMENTS
    from detector.weapon_attachments import GRIP_FACTOR, MUZZLE_FACTOR

    def wiki(part):
        asset = (ATTACHMENTS.get(part) or {}).get('asset') or ''
        for tbl in (MUZZLE_FACTOR, GRIP_FACTOR):
            for k, f in tbl.items():
                if k in asset:
                    return f
        return 1.0

    out = []
    for weapon in sorted(table):
        for posture in sorted(table[weapon]):
            rows = table[weapon][posture]
            for key, r in sorted(rows.items()):
                if '+' not in key:
                    continue
                singles = [rows.get(p) for p in key.split('+')]
                if any(s is None for s in singles):
                    continue        # cannot compare; not evidence either way
                own = 1.0
                for s in singles:
                    own *= s['f']
                wik = 1.0
                for p in key.split('+'):
                    wik *= wiki(p.split('=', 1)[1])
                out.append((weapon, posture, key, r['f'], own, wik))
    return out


def every_kit(weapon):
    """Every muzzle x grip x stock combination this weapon can wear. Bare
    excluded -- bare IS the denominator every row is a ratio against."""
    from detector.attachment_catalog import compatible
    import itertools

    fits = compatible(weapon)
    choices = [[(slot, p) for p in [None] + sorted(fits.get(slot, ()) or ())]
               for slot in RECOIL_SLOTS]
    out = []
    for combo in itertools.product(*choices):
        worn = [(s, p) for s, p in combo if p]
        if worn:
            out.append('+'.join(sorted(f'{s}={p}' for s, p in worn)))
    return sorted(out)


def backlog(table, weapons=None):
    """What is NOT measured yet. -> {weapon: {posture: [kit, ...]}}

    This is what replaced derive(). The combinations are the same ones it used
    to fill in with a product; the difference is that they are now a work list
    instead of 267 numbers that read like measurements at the point of use.

    Only postures the table already has are enumerated: a posture nobody has
    measured at all is a different and larger gap, and burying it inside a
    per-kit list would make one missing SITTING look like fifty missing kits.
    """
    out = {}
    for weapon in sorted(weapons or table):
        want = every_kit(weapon)
        if not want:
            continue
        for posture in sorted(table.get(weapon, {})) or ['standing']:
            have = set(table.get(weapon, {}).get(posture, {}))
            missing = [k for k in want if k not in have]
            if missing:
                out.setdefault(weapon, {})[posture] = missing
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--selftest', action='store_true',
                    help='assert the runtime reads the table AND still falls '
                         'through for kits that are not in it')
    ap.add_argument('--todo', action='store_true',
                    help='list every unmeasured combination by name, not just '
                         'the counts. This is the measurement programme that '
                         'replaced the derived rows.')
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    # ── the guard ─────────────────────────────────────────────────────────
    # An empty table is not an empty answer, it is a WRONG one: every kit
    # falls through to the wiki coefficients (median 34.7% off, measured by
    # coupling_report below) and nothing anywhere reports it. So a run with
    # nothing to read must stop here rather than reach --write.
    gone = missing_sources()
    if gone:
        print(f'{len(gone)} of {len(SOURCES)} source run(s) are not on disk, '
              f'so there is nothing to build from:')
        for rel in gone:
            print(f'  - {rel}')
        print('\ndata/kit_factors.json and data/kit_records.jsonl ARE the '
              'record now — they cannot be rebuilt and must not be '
              'overwritten. See the ⚠⚠ note at the top of this file.\n'
              'What still works: --selftest (asserts the runtime reads the '
              'table and still falls through for kits that are not in it).')
        return 1

    table, skipped = build(SOURCES)
    measured = sum(len(k) for w in table.values() for k in w.values())
    for w in table.values():
        for k in w.values():
            for r in k.values():
                r.setdefault('src', 'measured')
    assert not DERIVE, 'the orthogonal fill is gone; see the DERIVE comment'
    print(f'{measured} MEASURED kit(s) over {len(table)} weapon(s). '
          f'Nothing is derived — see DERIVE in this file.\n')
    for weapon in sorted(table):
        for posture in sorted(table[weapon]):
            for key, r in sorted(table[weapon][posture].items()):
                note = f'  [{r["note"]}]' if r.get('note') else ''
                print(f'  {weapon:7s} {posture:9s} {key:34s} '
                      f'{r["f"]:.4f} +- {r["f"] * r["rel"]:.4f}  '
                      f'n={r["n"]}{note}')
    if skipped:
        print(f'\nnot in the table ({len(skipped)}):')
        for s in skipped:
            print(f'  - {s}')

    # ── 后边再分析: does a per-weapon product beat the global wiki one? ──
    comp = coupling_report(table)
    if comp:
        print(f'\ncoupling, on the {len(comp)} kit(s) whose parts are also '
              f'measured singly:')
        print(f'  {"":7s} {"":9s} {"kit":46s} {"measured":>9s} '
              f'{"own x":>9s} {"":>7s} {"wiki x":>9s}')
        own_err, wiki_err = [], []
        for weapon, posture, key, f, own, wik in comp:
            own_err.append(abs(own - f) / f)
            wiki_err.append(abs(wik - f) / f)
            print(f'  {weapon:7s} {posture:9s} {key:46s} {f:9.4f} '
                  f'{own:9.4f} {100 * (own - f) / f:+6.1f}% {wik:9.4f} '
                  f'{100 * (wik - f) / f:+6.1f}%')
        print(f'  median error: this weapon\'s own parts '
              f'{100 * sorted(own_err)[len(own_err) // 2]:.1f}%, '
              f'global wiki {100 * sorted(wiki_err)[len(wiki_err) // 2]:.1f}%')

    todo = backlog(table)
    n_todo = sum(len(v) for w in todo.values() for v in w.values())
    print(f'\nstill to measure: {n_todo} combination(s) — each one answers on '
          f"this weapon's own single parts where they exist, and on the wiki "
          f'where they do not')
    for weapon in sorted(todo):
        for posture in sorted(todo[weapon]):
            miss = todo[weapon][posture]
            print(f'  {weapon:7s} {posture:9s} {len(miss):3d} unmeasured')
            if a.todo:
                for k in miss:
                    print(f'      {k}')
    if not a.todo:
        print('  (--todo for the names)')

    if not a.write:
        print('\n(--write to store)')
        return 0
    payload = {
        '_note': 'MEASURED recoil factor per (weapon, posture, kit), relative '
                 'to that weapon\'s BARE cell in the same run. EVERY ROW HERE '
                 'WAS FIRED. Nothing is derived from an orthogonality '
                 'assumption -- the slots are known to couple (up to 17% on '
                 'the mp5k) and WHICH edge couples is per-weapon, so a kit '
                 'gets a number by being measured or not at all. Built by '
                 'calibration/build_kit_factors.py from the runs listed in its '
                 'SOURCES -- do not hand-edit, rebuild. `rel` is the relative '
                 'standard error. A kit that is not in `kits` is answered from '
                 '`parts` (this weapon\'s own single-slot numbers, multiplied '
                 '-- median 6.7% off the measured whole kit) and only then '
                 'from the global wiki coefficients (median 34.7% off). '
                 'detector/weapon_attachments.explain_factor names which tier '
                 'answered.',
        '_slots': list(RECOIL_SLOTS),
        '_sources': [s for s, _ in SOURCES],
        '_unmeasured': {w: {p: len(v) for p, v in ps.items()}
                        for w, ps in backlog(table).items()},
        # TWO TABLES, one measurement set. `kits` is the whole thing keyed by
        # the kit that was on the gun; `parts` is the single-slot subset of the
        # same rows, which is what "what does this part do on THIS gun" needs.
        # parts is regenerated here on every build and must never be edited on
        # its own -- it is a projection, not a second opinion.
        'kits': table,
        'parts': parts(table),
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'\nwrote {OUT}')

    # The flat record store. JSONL because that is what a record is in this
    # repo (calibration/artifacts/recoil/runs, calibration/artifacts/drag/journal) and because appending one
    # measurement should not mean rewriting a tree.
    with open(RECORDS, 'w', encoding='utf-8') as f:
        for rec in records(table):
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    print(f'wrote {RECORDS}  ({len(records(table))} record(s), '
          f'{sum(1 for r in records(table) if r["slots"] == 1)} of them single '
          f'part)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
