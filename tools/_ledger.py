"""A ledger entry is a CLAIM ABOUT THE CODE. This is what checks the claim.

    from _ledger import Reason, CODE, MEASURED, INFERRED, audit

    EXEMPT = {
        'calibration/sweep.py': Reason(
            'assembly shell — Rig owns the one Pointer and hands it to the '
            'control/ drivers.',
            CODE, lambda: 'Rig' in classes_of('calibration/sweep.py')),
    }

WHY THIS EXISTS. Every ratchet in this repo tests one half of its ledger — is
this file still offending — and NEVER the other half, which is the prose
saying why it is allowed to. The two halves rot at different speeds, and the
one nothing checks rots silently.

Measured 2026-08-07, and it is the reason this module got written:
tools/drive_screen.py sat on rule 9's debt list for a year behind

    "its ensure_focus is inside drive(), a LIBRARY function that
     calibration/scan_compat.py calls once per weapon -- 30 times a run.
     ensure_ready there would teleport 30 times."

scan_compat imports SCREENS, not drive(). `grep 'drive('` finds ONE caller in
the repo — drive_screen's own main(). And scan_compat had opened with
ensure_ready for two days by then, so the 30 teleports could not have happened
even if it did call it. Three clauses, three greps, none of them ever run.

THE THREE SOURCES, and they are not degrees of rigour. They answer one
question: can the next reader use this as a premise?

    CODE      ran a command / read the AST. MUST carry a `check`, and the
              check runs on every green build. A false reason goes red.
    MEASURED  somebody ran it and looked. MUST carry a date and an artifact
              path. Not re-run — an eviction that took 20.5 minutes cannot be
              re-derived from source — but it AGES, and the age is printed.
    INFERRED  read the code and concluded. Allowed, and MUST NOT carry a
              check, because a check would make it look verified. It is
              counted separately and printed on every run.

⚠ THE POINT OF THE THIRD ONE IS THAT IT IS COUNTABLE. An entry nobody can
check is not a scandal; an entry nobody can check, sitting in the same table
and the same font as one that is checked every build, is how the first kind
gets read as the second. `audit()` prints the split, so "how many of this
repo's exemptions rest on nothing" is a number instead of a feeling.

⚠ A CHECK IS NOT A TEST OF THE RULE, IT IS A TEST OF THE REASON. rule 6 asks
"does calibration/sweep.py import press" — that is the rule, and the ratchet
already covers it. This asks "is sweep.py still the assembly shell", because
the day it stops being one the import stops being excusable and nothing else
in the repo would notice.
"""
import ast
import datetime
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

CODE, MEASURED, INFERRED = 'code', 'measured', 'inferred'

# A MEASURED reason older than this gets a line in the report. Not an error:
# re-measuring costs a live game, and a stale measurement is still the only
# thing anybody has. 180 days is one "has the game had a major patch since"
# horizon; the number is a convention, not a finding.
STALE_DAYS = 180


class Reason:
    """Why an entry is in a ledger, plus how anyone could tell.

    `why` is for a human. `source` and `check` are for the next build.
    """

    def __init__(self, why, source, check=None, measured=None, artifact=None):
        if source not in (CODE, MEASURED, INFERRED):
            raise ValueError(f'source must be code/measured/inferred: {source!r}')
        if source == CODE and check is None:
            raise ValueError(f'a CODE reason must carry a check: {why[:40]!r}')
        if source == INFERRED and check is not None:
            # Not pedantry. A check on an INFERRED entry makes it read as
            # verified in every listing, which is the exact confusion the
            # three sources exist to end.
            raise ValueError(f'an INFERRED reason must NOT carry a check: '
                             f'{why[:40]!r}')
        if source == MEASURED and not measured:
            raise ValueError(f'a MEASURED reason must carry a date: {why[:40]!r}')
        self.why, self.source, self.check = why, source, check
        self.measured, self.artifact = measured, artifact

    # The ledgers are read as {path: str} in a dozen places (printing, the
    # `.split(' — ')[0]` in check_layering's debt listing). Behaving like the
    # string keeps every one of those working unchanged.
    def __str__(self):
        return self.why

    def __getitem__(self, i):
        return self.why[i]

    def split(self, *a, **k):
        return self.why.split(*a, **k)

    def strip(self, *a, **k):
        return self.why.strip(*a, **k)

    def __contains__(self, x):
        return x in self.why

    def __len__(self):
        return len(self.why)


# ── the vocabulary the checks are written in ──────────────────────────────
#
# Deliberately tiny and deliberately AST-based. A check written as a regex
# over source text would pass on a mention inside a comment, which is exactly
# the class of evidence this module exists to stop accepting.

def _tree(rel):
    p = ROOT / rel
    if not p.exists():
        return None
    try:
        return ast.parse(p.read_text(encoding='utf-8'))
    except (SyntaxError, UnicodeDecodeError):
        return None


def defines(rel, name):
    """Does `rel` define this class, function or module-level constant?"""
    t = _tree(rel)
    if t is None:
        return False
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and n.name == name:
            return True
        if isinstance(n, ast.Assign) and any(
                isinstance(x, ast.Name) and x.id == name for x in n.targets):
            return True
    return False


def calls(rel, name, inside=None):
    """Does `rel` (optionally: its function `inside`) call `name()`?

    Counts `x.name(...)` and `name(...)`, and also `f(..., x.name)` — handing a
    bound method to a pump is how half of control/ presses a key until the
    screen agrees, and a check that missed it would be wrong about the shape
    the repo actually uses.
    """
    t = _tree(rel)
    if t is None:
        return False
    scope = t
    if inside:
        scope = next((n for n in ast.walk(t)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.name == inside), None)
        if scope is None:
            return False
    for n in ast.walk(scope):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr == name:
            return True
        if isinstance(f, ast.Name) and f.id == name:
            return True
        for a in list(n.args) + [k.value for k in n.keywords]:
            if isinstance(a, ast.Attribute) and a.attr == name:
                return True
    return False


def imports(rel, module):
    """Does `rel` import `module` (or something out of it)?"""
    t = _tree(rel)
    if t is None:
        return False
    for n in ast.walk(t):
        if isinstance(n, ast.ImportFrom) and n.module and (
                n.module == module or n.module.startswith(module + '.')):
            return True
        if isinstance(n, ast.Import) and any(
                a.name == module or a.name.startswith(module + '.')
                for a in n.names):
            return True
    return False


def mentions_literal(rel, *values):
    """Are all of these numeric literals present in `rel`?

    For a test that must PIN a measured number rather than import it. Reading
    the literals is the point: a test that imported the answer would assert
    nothing.
    """
    t = _tree(rel)
    if t is None:
        return False
    seen = {n.value for n in ast.walk(t)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))}
    return all(v in seen for v in values)


def has_cli_flag(rel, flag):
    """Does `rel` register this argparse flag?"""
    t = _tree(rel)
    if t is None:
        return False
    for n in ast.walk(t):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == 'add_argument':
            for a in n.args:
                if isinstance(a, ast.Constant) and a.value == flag:
                    return True
    return False


def callers_of(name, dirs=('control', 'calibration', 'harness', 'tools',
                           'detector', 'press')):
    """Every file that calls `name()`. -> [rel, ...]

    For the "zero callers" claim, which is the one that rots fastest — a
    function is kept for a stated reason, somebody adds a caller, and the
    reason silently becomes false in the direction that matters.
    """
    out = []
    for d in dirs:
        for p in sorted((ROOT / d).rglob('*.py')):
            if '__pycache__' in p.parts:
                continue
            rel = p.relative_to(ROOT).as_posix()
            t = _tree(rel)
            if t is None:
                continue
            for n in ast.walk(t):
                if isinstance(n, ast.Call) and (
                        (isinstance(n.func, ast.Attribute) and n.func.attr == name)
                        or (isinstance(n.func, ast.Name) and n.func.id == name)):
                    out.append(rel)
                    break
    return out


# ── the audit ─────────────────────────────────────────────────────────────

def audit(tables, today=None):
    """Run every CODE check. -> (lines to print, [(key, failure)])

    `tables` is [(label, {key: Reason|str})]. Plain strings are counted as
    UNDECLARED — not as inferred. Calling an old entry a guess is itself a
    guess, and the whole point of this module is to stop doing that.
    """
    today = today or datetime.date.today()
    counts = {CODE: 0, MEASURED: 0, INFERRED: 0, 'undeclared': 0}
    bad, stale = [], []
    for label, tbl in tables:
        for key, r in tbl.items():
            if not isinstance(r, Reason):
                counts['undeclared'] += 1
                continue
            counts[r.source] += 1
            if r.source == CODE:
                try:
                    ok = bool(r.check())
                except Exception as e:              # noqa: BLE001 — reported
                    bad.append((f'{label}:{key}',
                                f'its check raised {type(e).__name__}: {e}'))
                    continue
                if not ok:
                    bad.append((f'{label}:{key}',
                                f'the reason no longer holds — "{r.why[:90]}"'))
            elif r.source == MEASURED:
                d = datetime.date.fromisoformat(r.measured)
                age = (today - d).days
                if age > STALE_DAYS:
                    stale.append((f'{label}:{key}', age, r.artifact))

    lines = []
    total = sum(counts.values())
    lines.append(f'  {total} ledger entr(ies): {counts[CODE]} code-checked, '
                 f'{counts[MEASURED]} measured, {counts[INFERRED]} inferred, '
                 f'{counts["undeclared"]} undeclared')
    if counts[INFERRED]:
        lines.append(f'    ⚠ {counts[INFERRED]} rest on reading the code and '
                     f'concluding. They are allowed; they are not premises.')
    if counts['undeclared']:
        lines.append(f'    ⚠ {counts["undeclared"]} predate this module and '
                     f'declare no source. NOT counted as inferred — deciding '
                     f'that for them would be the same mistake.')
    for key, age, art in stale:
        lines.append(f'    · {key}: measured {age} days ago'
                     + (f' ({art})' if art else ''))
    return lines, bad
