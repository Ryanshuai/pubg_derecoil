"""Pull the Kava4 community recoil patterns out of their Lua and into JSON.

    pixi run python tools/import_kava4.py --write     # -> docs/kava4/patterns.json
    pixi run kava4                                    # parse + report, no write

WHY THIS EXISTS, AND WHY IT IS MEANT TO BE THROWN AWAY.

A gun with no fitted curve fires with NO compensation, and for a high-recoil
weapon that is not merely inaccurate, it is unmeasurable: docs/timing.md
records an AUG climbing 883 counts on a zero baseline, 1366 px, 5.3 patch
heights, and once the view is in open sky there is no texture -- phase
correlation then returns 0 CONFIDENTLY and the magazine is lost without
anything reporting a failure. The m762 is bigger. Measured pitch travel is
3450 counts through a red dot with the midline at 1725, and an m762 magazine
is on the order of 2000 counts, so a burst fired from level walks into the
top clamp before it ends.

⚠ THE BASELINE DOES NOT HAVE TO BE RIGHT. It has to be KNOWN. MODEL.md's
identity is

    y_true(t) = y_obs(t) + C(t - M)

with C read back FROM THE DEVICE after upload, so any curve at all yields the
same y_true -- a wrong baseline moves counts between the two terms and cancels.
docs/timing.md says the same thing from the other side: a differential only
requires the two arms be IDENTICAL, never that either be zero. So an
approximate community pattern is worth exactly what is needed of it: keeping
the picture on screen while the gun's own curve is measured.

⚠ AND IT IS A SEED, NOT A SOURCE. The operator's instruction on 2026-08-09 was
"将来可能我们就有了自己的，以后它这个就不要了" -- once a weapon has a fitted
curve of its own, this file stops being consulted for it. Nothing here is
evidence about the game; MODEL.md's rules on provenance apply to measurements,
and these are somebody else's guesses that happen to be close enough to aim
with.

FORMAT, read off the Lua rather than assumed:

    csm(17)                                        sleep 17 ms
    MoveMouseRelativeFractional(dx * kava * SensSetting,
                                dy * kava * SensSetting)

so each weapon is a list of per-step (dx, dy) on a fixed 17 ms grid -- the same
grid MODEL.md fits on, which is a convenience and not a coincidence: both are
chasing the same ~60 Hz update. `kava` is a per-weapon-per-scope scalar set by
the kavaXXX() functions, and SensSetting is the player's own sensitivity. ⚠
NEITHER IS APPLIED HERE. They are recorded alongside so the consumer can choose;
folding them in would bake this machine's settings into a shared file.

⚠ 18327 of the sleeps are 17 ms and the rest belong to menu and mode-switch
code, not to patterns -- so a block whose sleeps are not all 17 is NOT a
pattern and is skipped rather than resampled.

NAMES ARE NOT OURS. The Lua calls the m762 BERRYLL (the Beryl), and doubles the
last letter of most others: AUGG, VECTORR, MP5KK, M4166. The `att` suffix is
the attachment variant and the trailing `_` is the crouching one, matching the
four-variant layout detector/weapon.py's BulletCalculator already documents.

⚠ THE VSS IS NOT IN THIS SCRIPT AT ALL, and that is reported rather than
patched around. It is a suppressed DMR firing at 704 RPM; the community scripts
cover assault rifles and SMGs. A VSS baseline has to come from somewhere else,
or its first magazines have to be fired from a low aim.
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

SRC = os.path.join(ROOT, 'docs', 'kava4', 'PUBG-Logitech-No-Recoil.lua')
OUT = os.path.join(ROOT, 'docs', 'kava4', 'patterns.json')
UPSTREAM = ('https://github.com/Kava4/PUBG-LOGITECH-NO-RECOIL'
            ' (branch main, PUBG-Logitech-No-Recoil.lua)')
RAW = ('https://raw.githubusercontent.com/Kava4/PUBG-LOGITECH-NO-RECOIL'
       '/main/PUBG-Logitech-No-Recoil.lua')
# ⚠ THE .lua IS NOT IN GIT AND DOES NOT NEED TO BE. It is 2.8 MB of somebody
# else's script, the pre-commit hook refuses new files that size, and the hook
# is right -- it is this tool's INPUT, not this repo's source. The record that
# matters is tracked: docs/kava4/patterns.json holds every weapon's per-step
# dy on the 17 ms grid plus the kava scalars, and these two lines say where the
# input came from and which bytes were read. Re-fetching is one curl.
SRC_SHA256 = 'e5b5edbe6dd78a3046c625f70c69aeb724895cc516b97cb46ceb417a96d89b3c'

# Their name -> ours. Only the guns this project measures; the rest are parsed
# and reported so a later weapon does not need this file edited to find out
# whether it is covered.
OURS = {
    'BERRYLL': 'm762', 'AUGG': 'aug', 'VECTORR': 'vector', 'MP5KK': 'mp5k',
    'M4166': 'm416', 'AKMM': 'akm',
    # ⚠ 'scar', not 'scar_l'. ROSTER holds one SCAR and calls it `scar`
    # (display name SCAR-L), so this pattern hung on a weapon name nothing
    # in the repository uses -- `pixi run kava4` reported the SCAR as
    # having NO seed while its pattern sat in the file, parsed and unused.
    # A gun with no seed fires uncompensated, and for an AR that means the
    # view reaches open sky where phase correlation returns 0 CONFIDENTLY.
    'SCAR_LL': 'scar', 'QBZZ': 'qbz',
    'G36CC': 'g36c', 'GROZAA': 'groza', 'FAMASS': 'famas', 'K22': 'k2',
    'UMP455': 'ump45', 'VECTORR_': 'vector', 'BIZONN': 'bizon',
    'TOMMYY': 'tommy', 'UZII': 'uzi', 'MP99': 'mp9', 'P900': 'p90',
    'M2499': 'm249', 'DP288': 'dp28', 'MG33': 'mg3', 'JS99': 'js9',
    'ACE322': 'ace32',
}
GRID_MS = 17

FN = re.compile(r'\n\tfunction ([A-Za-z0-9_]+)\(\)')
MOVE = re.compile(r'MoveMouseRelativeFractional\(\s*([-\d.]+)\s*\*[^,]*,'
                  r'\s*([-\d.]+)\s*\*')
CSM = re.compile(r'csm\((\d+)\)')
KAVA = re.compile(r'\n\s*function (kava[A-Za-z0-9_]+)\(\)\s*\n\s*kava\s*=\s*([\d.]+)')


def blocks(src):
    """{function name: body} -- split at top-level `\\tfunction NAME()`.

    ⚠ NOT a brace/`end` match. Lua closes `if` with `end` too, and matching the
    first one takes 6 lines of a 700-line pattern; the first attempt at this
    returned 0 shots for every weapon and looked like "the format changed".
    Splitting on the NEXT function declaration needs no nesting analysis.
    """
    hits = list(FN.finditer(src))
    out = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(src)
        out[m.group(1)] = src[m.end():end]
    return out


# A number is an optional sign, digits, optional fraction -- written out
# because `[-\d.]+` also matches a bare "-" (the Lua has `dx - 1` arithmetic in
# places) and float() then raises on it.
NUM = r'-?\d+(?:\.\d+)?'
STEP = re.compile(r'csm\((\d+)\)|'
                  r'MoveMouseRelativeFractional\(\s*(%s)[^,]*,\s*(%s)'
                  % (NUM, NUM))


def parse_pattern(body):
    """-> (shots, why_not). shots is [(dx, dy)] on the 17 ms grid.

    ⚠ THE PATTERN IS THE LEADING RUN OF 17 ms STEPS, and the block does not end
    there. After the last compensated round every weapon has a tail --
    csm(400)/csm(100)/csm(25)/csm(970) around a zero move -- which is the
    repeat loop idling until the button comes up. The first version of this
    rejected any block containing a sleep other than 17 and therefore rejected
    ALL 175 of them, reporting "not a pattern" for every weapon in the file.

    Cutting at the first non-17 sleep AFTER the first move is not a loosened
    threshold, it is the actual boundary: 17 ms is the schedule, anything else
    is the loop waiting. csm(1) before the first move is the settle.
    """
    shots = []
    started = False
    for m in STEP.finditer(body):
        if m.group(1) is not None:
            ms = int(m.group(1))
            if ms == GRID_MS:
                continue
            if started:
                break          # the tail: this block's schedule is over
            continue           # csm(1) etc. before the first move
        started = True
        shots.append((float(m.group(2)), float(m.group(3))))
    if not shots:
        return None, 'no MoveMouseRelativeFractional calls'
    return shots, None


# Kava4 raw unit -> our mouse counts.
#
# ⚠ ONE ANCHOR, AND IT IS NOT A VALIDATION. m416 is the only weapon this
# project has measured that is also in the script: our bare fit is 1446.5
# counts, their M4166 sums to 3200 raw, so 0.4520. Two things agree with it
# cheaply and neither was solicited:
#
#   config.COUNTS_PER_RECOIL_UNIT = 0.4     already in the tree, set for
#                                           exactly this conversion, 13% off
#   mp5k                                    their 781 counts against our
#                                           measured 939 -- 17% LOW
#
# So the seed lands within about 20% of the truth on a second weapon. That is
# ample, because a seed is not required to be right (MODEL.md reads C back off
# the device, so any known baseline cancels out of y_true) -- it is required to
# keep the picture on screen.
#
# ⚠ LOW IS THE SAFE SIDE and that is why this number rather than a bigger one.
# Under-compensating leaves the view still climbing, slowly, with the whole
# pitch range above it. OVER-compensating drives it into the ground, which
# costs texture the same way the sky does and reaches a clamp just as fast.
UNIT_COUNTS = 0.4520

# How long a seed must keep compensating for.
#
# ⚠ THE SCRIPT'S PATTERNS ARE SHORTER THAN A MAGAZINE AND THE TAIL IS THE
# DANGEROUS END. Kava4's m762 is 148 knots = 2.50 s; an m762 magazine measured
# here runs 3.79 s. So the last 1.29 SECONDS OF EVERY BURST HAD NO
# COMPENSATION AT ALL, which is exactly where the recoil has been accumulating
# longest and the view is highest. Said from the chair on 2026-08-09:
#
#     如果弹夹比他那个卡瓦提供的长，那你就要把那个用最后一发的那个后座力
#     填补到后边，不然后面空了直接飞了
#
# m762 survived it -- 1962 counts of truth against a 1523-count seed leaves
# ~440 of rise, inside the pitch headroom -- but that is luck, not design, and
# the guns whose patterns are shortest (vector, 0.94 s) have the least of it.
#
# ⚠ OVERSHOOTING IS FREE, which is why this is a flat number rather than a
# per-weapon magazine length nobody has measured yet. The firmware plays the
# curve only while the trigger is down, so a curve longer than the burst costs
# nothing; a curve SHORTER than the burst costs the end of every magazine.
# 4.0 s clears the longest span measured here (3.81 s, m416).
SEED_SPAN_S = 4.0


PLATEAU_N = 10


def _plateau(shots):
    """The steady-state per-knot (dx, dy) to extend a pattern with.

    ⚠ NOT THE LAST KNOT, and that mistake survived one run. Kava4 ends several
    patterns on a SENTINEL rather than on the plateau -- VECTORR's last dy is
    0.0 and AUGG's is 1.0 against plateaus of 19 and 23 -- so repeating the
    final value padded vector with 179 knots of ZERO. The report said
    "+179 padded knots" and the total stayed 378: the tail was still empty and
    the fix looked like it had worked.

    The median of the last few non-zero knots is the plateau by construction
    and cannot be fooled by one or two sentinels.
    """
    nz = [(dx, dy) for dx, dy in shots if dy]
    if not nz:
        return shots[-1]
    tail = nz[-PLATEAU_N:]
    med = sorted(dy for _, dy in tail)[len(tail) // 2]
    dxs = sorted(dx for dx, _ in tail)
    return dxs[len(tail) // 2], med


def _pad_to_span(shots, span_s):
    """Extend at the plateau rate out to `span_s`. -> (shots, n_added)."""
    want = int(span_s * 1000 / GRID_MS)
    if len(shots) >= want:
        return shots, 0
    return shots + [_plateau(shots)] * (want - len(shots)), want - len(shots)


def _magazine_span_s(weapon, rounds):
    """How long a full burst lasts, from the MEASURED fire rate.

    ⚠ A FLAT NUMBER WAS WRONG IN BOTH DIRECTIONS. Padding every gun to 4.0 s
    took the m762 seed to 2624 counts against a measured truth of 1942 -- 35%
    over, and over is the direction that drives the view into the ground. The
    guns differ by more than 2x in fire rate (vector 1130 rpm, m762 706), so
    the span has to come from the gun.

    detector.weapon.WEAPON_RPM is the wiki table already overwritten by
    calibration/artifacts/recoil/weapon_rpm.json where anything has been timed
    -- m762's 85.00 ms there comes from 24 magazines agreeing to 0.81 ms.
    """
    from detector.weapon import WEAPON_RPM
    rpm = WEAPON_RPM.get(weapon)
    if not rpm:
        return SEED_SPAN_S
    return rounds * 60.0 / float(rpm)


def write_seeds(names, pats, posture, sight, config=None, span_s=None,
                rounds=40, borrow=None):
    """Write seed curves for `names`. -> exit code.

    `config` is a dict like {'muzzle': 'comp_ar', 'grip': 'vert_grip'}; the
    bare pattern is scaled by that kit's measured recoil factor.

    ⚠ THE FACTOR DOES NOT HAVE TO BE RIGHT, and that is not a shrug -- it is
    the same property that licenses the whole file. C is read back off the
    device, so y_true = y_obs + C is exact whatever C was. A kit factor that
    is 15% off moves counts between the two terms and nowhere else. Asked in
    those words: 「配的系数不就有问题吗？反正就说随便一个作为基准」.
    """
    import config as cfg
    rev = {}
    for lua, ours in OURS.items():
        rev.setdefault(ours, lua)
    os.makedirs(cfg.CURVES_DIR, exist_ok=True)
    rc = 0
    for ours in names:
        # ⚠ BORROWING ANOTHER GUN'S PATTERN IS LEGITIMATE AND MUST BE SAID.
        # The mk14 and the vss have no Kava4 pattern at all -- the community
        # scripts cover ARs and SMGs -- and a gun with no curve fires with NO
        # compensation, which for these is not "inaccurate" but unmeasurable:
        # the view reaches open sky, and phase correlation there returns 0
        # CONFIDENTLY.
        #
        # The seed doctrine already licenses this: C is read back off the
        # device, so y_true = y_obs + C is exact whatever C was, and a borrowed
        # baseline moves counts between the two terms and nowhere else. What it
        # must NOT do is look like a measurement of this weapon, so the source
        # line names the gun it came from and `borrowed_from` is a field.
        #
        # Asked for 2026-08-09:「mk14 / vss 用冲锋枪 mp5k 先顶一下 最初曲线」.
        src_name = (borrow or ours)
        lua = rev.get(src_name)
        if lua is None or lua not in pats:
            print(f'  ✗ {ours}: no Kava4 pattern '
                  f'({lua or "no name mapping"} for {src_name})')
            rc = 1
            continue
        # ⚠ THE STORE OUTRANKS THE SEED, not just the file. A seed exists to
        # get a gun through its FIRST magazines; once the store can answer for
        # this configuration, a community guess sitting in the read path is a
        # trap -- it is what `collect_timed` fires when nobody passes
        # --from-fit, and it would be silently worse than the data already on
        # disk. mp5k is the live example: 128 compensated magazines saying 939
        # counts, against a seed of 781.
        try:
            from calibration import samples as _S
            from calibration.fit_time_curve import fit as _fit
            # ⚠ load(weapon, cfg), NOT a filter on all_magazines. samples
            # .config_key({}) is 'bare', not '' -- filtering on '' silently
            # matches NOTHING, so this whole check would pass every gun
            # through while looking like it ran. It did exactly that on its
            # first run, and only mp5k having obvious data caught it.
            have = [m for m in _S.load(ours, config or {}) if m.comp_enabled]
            got = _fit(have) if have else {'ok': False}
            if got.get('ok'):
                print(f'  ✗ {ours}: SKIPPED — the store already fits this '
                      f'configuration ({got["n_kept"]}/{got["n_total"]} '
                      f'magazines, {got["total_counts"]:.0f} counts). Use '
                      f'`--from-fit`; a seed here would be a guess in the '
                      f'path a measurement already covers.')
                continue
        except Exception as e:                       # noqa: BLE001
            print(f'    ({ours}: could not consult the store — {e})')
        span = span_s if span_s else _magazine_span_s(ours, rounds)
        raw, padded = _pad_to_span(pats[lua], span)
        kit_f = 1.0
        if config:
            # ⚠ ASSETS, NOT CATALOGUE KEYS. attachment_factor's slots are
            # "attachment_detector class names" (its docstring) and it looks
            # them up through _ASSET_TO_KEY, whose ⚠ says an unrecognised part
            # POISONS THE WHOLE ANSWER on purpose -- `_worn_keys` returns None
            # and the lookup drops to the wiki tier. `comp_smg` is not an
            # asset, so this asked for a MEASURED 0.7197 and got 1.0 back,
            # printing `kit x1.0000` on every kitted seed while looking exactly
            # like a gun with no measurements. The poison rule did its job; the
            # caller was speaking the wrong language.
            from detector.attachment_catalog import ATTACHMENTS
            from detector.weapon_attachments import explain_factor

            def _asset(key):
                return (ATTACHMENTS.get(key) or {}).get('asset', '') if key else ''

            kit_f, src, _ = explain_factor(ours,
                                           _asset(config.get('muzzle')),
                                           _asset(config.get('grip')),
                                           _asset(config.get('stock')), posture)
            # A seed does not have to be RIGHT, but it does have to be KNOWN --
            # so say which tier answered rather than letting a wiki 1.0 pass
            # for a measurement.
            kit_src = src
        shots = [{'delay_ms': GRID_MS if i else 0,
                  'dx': round(dx * UNIT_COUNTS * kit_f, 4),
                  'dy': round(dy * UNIT_COUNTS * kit_f, 4)}
                 for i, (dx, dy) in enumerate(raw)]
        total = sum(s['dy'] for s in shots)
        doc = {
            'weapon': ours, 'sight': sight, 'posture': posture,
            'config': dict(config or {}),
            'shots': shots,
            'padded_knots': padded,
            'kit_factor': kit_f,
            # ⚠ THE FIELD THE RUNTIME PRINTS ON. detector/weapon.py warns when
            # it fires one of these, because a seed and a fit are
            # indistinguishable from the outside otherwise -- root CLAUDE.md's
            # second law, 记录描述的对象必须是被测量的那个对象.
            'seed': True,
            'borrowed_from': (borrow if borrow and borrow != ours else None),
            'source': (f'SEED BORROWED FROM {borrow.upper()} — this weapon has '
                       f'NO pattern of its own in the source script, so the '
                       f'shape belongs to that gun and only the SPAN was '
                       f'fitted to this one. It exists to keep the view on '
                       f'screen, not to describe this weapon. '
                       if borrow and borrow != ours else '')
                      + f'SEED, NOT A FIT — {UPSTREAM}, x{UNIT_COUNTS} '
                      f'counts/unit. Community guesses, close enough to keep '
                      f'the view on screen while this gun is measured. The '
                      f'first `collect-timed` + `fit_time_curve` for this '
                      f'configuration OVERWRITES this file, which is the '
                      f'intent.',
            'grid_ms': GRID_MS,
            'total_counts': total,
            'span_s': (len(shots) - 1) * GRID_MS / 1000.0,
            'scaled_by': 'NOTHING further. These are final mouse counts; '
                         'set_seq emits them with no factors.',
        }
        from calibration.samples import config_key as _ck
        path = os.path.join(cfg.CURVES_DIR, f'{ours}__{_ck(config or {})}.json')
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                old = json.load(f)
            if not old.get('seed'):
                # ⚠ NEVER over a fit. A seed replacing a measurement is the
                # one direction that loses information, and it would do it
                # silently -- the file keeps the same name either way.
                print(f'  ✗ {ours}: {os.path.basename(path)} is a FITTED '
                      f'curve ({old.get("total_counts", 0):.0f} counts from '
                      f'{old.get("n_magazines", "?")} magazines). Refusing to '
                      f'overwrite a measurement with a guess.')
                rc = 1
                continue
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=1, ensure_ascii=False)
        note = []
        if padded:
            note.append(f'+{padded} padded to {span:.2f}s at the plateau')
        if config:
            note.append(f'kit x{kit_f:.4f} ({kit_src})')
        print(f'  seeded {ours:8s} {total:7.0f} counts over {doc["span_s"]:.2f}s '
              f'({len(shots)} knots{", " + ", ".join(note) if note else ""}) '
              f'-> {os.path.relpath(path, ROOT)}')
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--write', action='store_true',
                    help=f'write {os.path.relpath(OUT, ROOT)}')
    ap.add_argument('--seed', default=None,
                    help='comma-separated weapons (ours, e.g. "m762,aug") to '
                         'write a BARE STANDING seed curve for, into '
                         'config.CURVES_DIR. That is where fit_time_curve '
                         'writes, so the first real fit REPLACES the seed — '
                         'which is the whole intent')
    ap.add_argument('--posture', default='standing')
    ap.add_argument('--sight', default='red_dot')
    ap.add_argument('--config', default=None,
                    help='kit for the seed, as "muzzle=comp_ar,grip=vert_grip"'
                         '. The bare pattern is scaled by that kit\'s measured '
                         'recoil factor — which does NOT have to be right, '
                         'because C is read back off the device and cancels '
                         'out of y_true either way')
    ap.add_argument('--rounds', type=int, default=40,
                    help='magazine size, for deriving how long the seed must '
                         'keep compensating from the MEASURED fire rate '
                         '(default 40)')
    ap.add_argument('--span-s', type=float, default=None,
                    help=f'pad the tail out to this many seconds by repeating '
                         f'the last knot (default {SEED_SPAN_S}). Kava4\'s '
                         f'patterns are SHORTER than a magazine — m762 is '
                         f'2.50s against a 3.79s burst — so without this the '
                         f'end of every magazine fires uncompensated, which is '
                         f'the highest the view ever gets')
    a = ap.parse_args()

    if not os.path.exists(SRC):
        # ⚠ NOT AN ERROR IF ALL YOU WANT IS THE DATA. patterns.json is tracked
        # and holds the parsed result; this file is only needed to re-derive
        # it. Gitignored on purpose (2.8 MB of third-party Lua).
        print(f'✗ {os.path.relpath(SRC, ROOT)} is not here — it is gitignored, '
              f'because it is this tool\'s INPUT and not this repo\'s source.\n'
              f'  The parsed result is tracked at '
              f'{os.path.relpath(OUT, ROOT)}; fetch the input only to redo it:'
              f'\n\n    curl -sL {RAW} \\\n         -o '
              f'{os.path.relpath(SRC, ROOT)}\n\n'
              f'  Expected sha256: {SRC_SHA256}')
        return 2

    src = open(SRC, encoding='utf-8', errors='replace').read()
    import hashlib
    got = hashlib.sha256(open(SRC, 'rb').read()).hexdigest()
    if got != SRC_SHA256:
        # ⚠ REPORTED, NOT REFUSED. Upstream is a live repo and may legitimately
        # have moved; what must not happen is the numbers changing under
        # somebody without a word, which is the whole reason the hash is here.
        print(f'⚠ the .lua is NOT the revision this file was written against\n'
              f'    expected {SRC_SHA256}\n    got      {got}\n'
              f'  Re-read the parse before trusting the output; upstream may '
              f'have changed the format, not just the numbers.\n')
    bl = blocks(src)
    scales = {m.group(1): float(m.group(2)) for m in KAVA.finditer(src)}
    print(f'{len(bl)} function blocks, {len(scales)} kava scale setters\n')

    pats, skipped = {}, []
    for name, body in sorted(bl.items()):
        shots, why = parse_pattern(body)
        if shots is None:
            skipped.append((name, why))
            continue
        pats[name] = shots

    want = ('BERRYLL', 'AUGG', 'VECTORR', 'MP5KK', 'M4166')
    print('  the guns this project is collecting:\n')
    print(f'  {"lua":10s} {"ours":8s} {"shots":>5s} {"sum dy":>8s} '
          f'{"sum dx":>7s} {"span":>7s}')
    missing = []
    for lua in want:
        ours = OURS.get(lua, '?')
        if lua not in pats:
            print(f'  {lua:10s} {ours:8s}   -- NOT IN THIS SCRIPT')
            missing.append(ours)
            continue
        sh = pats[lua]
        sdy = sum(b for _, b in sh)
        sdx = sum(x for x, _ in sh)
        print(f'  {lua:10s} {ours:8s} {len(sh):5d} {sdy:8.1f} {sdx:7.1f} '
              f'{len(sh) * GRID_MS / 1000.0:6.2f}s')
    print(f'\n  ⚠ vss is not in this script at all — it is a suppressed DMR and '
          f'these\n     scripts cover ARs and SMGs. Its first magazines need a '
          f'low aim instead.')

    if skipped:
        print(f'\n  {len(skipped)} block(s) parsed as NOT a pattern '
              f'(reported, not silently dropped):')
        for name, why in skipped[:6]:
            print(f'    {name:16s} {why}')
        if len(skipped) > 6:
            print(f'    ... and {len(skipped) - 6} more')

    if a.seed:
        kit = None
        if a.config:
            kit = {}
            for part in a.config.split(','):
                k, _, v = part.partition('=')
                if k.strip():
                    kit[k.strip()] = v.strip() or None
            kit = {k: v for k, v in kit.items() if v}
        return write_seeds([s.strip() for s in a.seed.split(',') if s.strip()],
                           pats, a.posture, a.sight, config=kit,
                           span_s=a.span_s, rounds=a.rounds)

    if not a.write:
        print(f'\n  (nothing written — pass --write for '
              f'{os.path.relpath(OUT, ROOT)})')
        return 0

    doc = {
        '_what': 'Community recoil patterns, imported. A SEED FOR AIMING, NOT '
                 'A MEASUREMENT. See tools/import_kava4.py for why an '
                 'approximate baseline is sufficient and why this file is '
                 'meant to stop being consulted per-weapon as soon as that '
                 'weapon has a fitted curve of its own.',
        '_source': UPSTREAM,
        '_units': 'dy/dx are the raw numbers in the Lua. The script sends '
                  'them as dy * kava * SensSetting, where kava is a '
                  'per-weapon-per-scope scalar and SensSetting is the '
                  "player's own sensitivity. NEITHER IS APPLIED HERE -- "
                  'folding in this machine\'s settings would make the file '
                  'wrong everywhere else.',
        '_grid_ms': GRID_MS,
        '_variants': 'plain = standing, trailing _ = crouching, att = with '
                     'attachments, att_ = crouching with attachments. Same '
                     'four-variant layout BulletCalculator documents.',
        '_kava_scales': scales,
        '_lua_to_ours': OURS,
        'patterns': {k: {'grid_ms': GRID_MS, 'shots': v} for k, v in pats.items()},
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=1)
    print(f'\n  wrote {os.path.relpath(OUT, ROOT)} — {len(pats)} patterns')
    return 0


if __name__ == '__main__':
    sys.exit(main())
