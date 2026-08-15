"""A parameter no body reads is a promise nothing keeps. -> exit 1

    pixi run params

WHY THIS IS A GATE AND NOT A LINT NOTE. The failure it catches does not look
like a failure. `control/session.py:ensure_ready` grew a `match_timeout` on
2026-08-07 and the change that added it ALSO rerouted range re-entry through
`ensure_ready` -- at which point `AutoSession.enter(timeout_s=300)` was still
passing its budget, the parameter was still in the signature, still in the
docstring, and read by nothing. Every re-entry silently got ENTER_TIMEOUT
instead of the caller's 300 s. Nothing else in the repo could have found that:
the call succeeded, the argument was accepted, and the only symptom was a
timeout five minutes shorter than the one the caller asked for.

That is the shape: a dead parameter is not dead code. Code that is never
reached is merely wasted. A parameter that is never READ is a LIE IN THE
DECLARATION -- the caller reads the signature, believes the knob is connected,
and passes a value that changes nothing. This repo's whole surface discipline
rests on the declaration being true (tools/surface.py, control/CLAUDE.md), so
a signature that advertises control it does not have is the one defect that
attacks the discipline itself.

⚠ IT MUST NOT SEE THE STUBS. An abstract method's parameters are read by its
implementors, not by its body, and flagging those would push the ledger toward
being an amnesty for the thing it is meant to catch. Bodies that are a
docstring, a `pass`, a bare `...` or a single `raise` are skipped in the
scanner rather than in the ledger, because the reason belongs to the SHAPE of
the code and not to any one file.

THE LEDGER is the same two-kind ratchet rules 6 and 9 use in check_layering.py:

  EXEMPT  the reason is the CODE's -- an API-mandated callback signature, an
          override that must match its base. It does not expire.
  DEBT    the reason is the SCHEDULE's. It must LEAVE the list, and an entry
          that has been paid and not deleted FAILS THE RUN -- otherwise the
          list rots into a permanent amnesty exactly as rule 6's would.

Both tables are empty right now, on purpose: the eight entries this scanner
found on 2026-08-07 were all removed rather than filed. An empty ledger is the
state to defend.
"""
import argparse
import ast
import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _ledger import Reason, CODE, audit          # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIRS = ('control', 'calibration', 'harness', 'press', 'detector')


# ════════════════════════════════════════════════════════════
# A measured constant may be WRITTEN AS A NUMBER in exactly one place
# ════════════════════════════════════════════════════════════
#
# Asked for by the operator on 2026-08-09, looking at a docstring example:
#
#     这个怎么还 hard code，code 那么 hard code 那么多地方呢？
#
# The diagnosis turned out to be narrower than the complaint and the complaint
# was still right. NO RUNNING CODE HARDCODES K -- config.RECOIL_SIGHT_PROFILES
# owns it and every path reads it (sweep.Rig.K -> rig.K -> Magazine.K). What
# was scattered is the NUMBER WRITTEN IN PROSE: docstring examples, comments
# citing it as evidence, an illustration of the 3x scope ratio. Six copies, and
# the red dot's K has moved three times (1.5474 -> 1.5128 -> the value config
# holds now), so each move left every copy quietly wrong. Two separate passes
# hand-patched them. ⚠ Note this sentence names the two SUPERSEDED values and
# not the live one -- that is the rule below, obeyed by its own docstring.
#
# ⚠ THE STALENESS IS SILENT AND THAT IS THE WHOLE POINT. A wrong number in a
# docstring does not fail anything; it gets read and believed. This repo's own
# first law says the same thing about templates -- drift does not raise, it
# returns a confident wrong answer.
#
# THE RULE: the value config CURRENTLY holds may not appear as a literal
# anywhere else under DIRS + tools. Superseded values (1.5474, 1.5128) are
# FINE and deliberately allowed -- a record that says "X was replaced by Y" is
# how this repo keeps its retractions, and a superseded number cannot be
# mistaken for a live second source. Only the live one can.
#
# ⚠ THE CHECK READS config AT RUNTIME, so it cannot itself go stale. That is
# the property that makes it worth having rather than a second copy of the
# list. It also means moving K in config immediately reds any prose that
# happens to name the new value -- which is exactly the event to catch.
#
# ⚠ IT IS DELIBERATELY PYTHON-ONLY. Markdown legitimately publishes the value
# -- MODEL.md IS the spec and docs/ carries supersession tables. The failure
# being prevented is a reader of SOURCE believing there is a second authority.
OWNED_CONSTANTS = {}        # filled by _owned_constants(), keyed value -> name


def _owned_constants():
    """{float: 'where it lives'} for constants config is the sole author of."""
    if OWNED_CONSTANTS:
        return OWNED_CONSTANTS
    sys.path.insert(0, str(ROOT))
    import config                                            # noqa: E402
    for sight, prof in config.RECOIL_SIGHT_PROFILES.items():
        # ⚠ AN ALIAS IS NOT A SECOND AUTHOR, AND THIS DICT IS KEYED BY VALUE, so
        # without this line the LAST profile holding a number wins the author
        # slot and the report cites it. `iron` was added 2026-08-12 carrying the
        # red dot's K by assignment (config.py says why), and the report
        # immediately started naming iron as the author of that K -- a number
        # measured on a red dot, with its provenance under `red_dot`. Nothing
        # failed: the count stayed right, the copies stayed right, only the
        # sentence telling you where to go changed, and its own fix advice
        # ("name the constant instead of the number") would then have written
        # the wrong name into a caller.
        if prof.get('alias_of'):
            continue
        k = prof.get('K')
        # A value that rounds the same at 2dp as at 4dp (the hip-fire 0.50) is
        # too round to police -- it collides with unrelated arithmetic, and a
        # rule that cries wolf gets muted, which is worse than no rule. Every
        # value carrying real precision is policed, scoped optics included.
        if k is not None and round(k, 4) != round(k, 2):
            OWNED_CONSTANTS[round(float(k), 4)] = (
                f"config.RECOIL_SIGHT_PROFILES['{sight}']['K']")
    return OWNED_CONSTANTS


def _literal_copies(files=None):
    """-> [(rel, line, text, owner)] for live constants written out elsewhere."""
    owned = _owned_constants()
    if not owned:
        return []
    out, seen = [], set()
    paths = files if files is not None else [
        p for d in DIRS + ('tools',) for p in (ROOT / d).rglob('*.py')]
    for path in paths:
        # A (rel, text) pair is the self-test's seam -- the same predicate runs
        # over invented source and over the tree, so the cases below prove the
        # thing main() uses rather than a parallel copy of it.
        if isinstance(path, tuple):
            rel, text = path
        else:
            p = pathlib.Path(path)
            try:
                rel = p.relative_to(ROOT).as_posix()
            except ValueError:
                rel = p.as_posix()
            try:
                text = p.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
        for i, line in enumerate(text.splitlines(), 1):
            for value, owner in owned.items():
                # One line naming two owned constants is ONE offence. Reporting
                # it twice makes the count a lie and invites fixing half of it.
                if f'{value:g}' in line and (rel, i) not in seen:
                    seen.add((rel, i))
                    out.append((rel, i, line.strip()[:88], owner))
    return out


# ════════════════════════════════════════════════════════════
# The ledger
# ════════════════════════════════════════════════════════════
#
# Keyed 'path:function' -- the same key the report prints, so pasting a line
# from a red run into the right table is the whole workflow.

# ⚠ EXEMPT IS EMPTY TOO, AND ITS ONE ENTRY DIED THE WAY ITS OWN PREDICATE SAID
# IT WOULD. It exempted the unread `timeout_s` on an abstract `switch_to`, and
# its reason was written as a predicate ending in "delete the ABC and the
# parameter becomes ordinary dead weight that should go". The module it
# described was deleted on 2026-08-15 -- its only edge was an import nobody
# called -- so the entry went with it rather than sitting here matching
# nothing.
#
# ⚠ AND THAT IS THE ASYMMETRY WORTH KEEPING: `audit` below reports a DEBT key
# that no longer matches as an error, and an EXEMPT key that no longer matches
# as NOTHING. A stale EXEMPT is a permanent silent amnesty for a path that can
# come back under the same name -- so an EXEMPT entry has to be struck BY HAND
# by whoever deletes the code it describes, and nothing here will remind them.
# What did remind me was `pixi run names`, one layer over: it refused the first
# version of this very comment for naming the file I had just deleted.
EXEMPT = {
    # ⚠ A parameter named `_` or starting with `_` never reaches this table:
    # the scanner skips it, because that name IS the author saying "required
    # by something else, deliberately unread". control/focus.py's EnumWindows
    # callback is written that way and is the reason the rule exists.
}

# ⚠ EMPTY, AND THAT IS THE POINT. The one entry here was
# press/pico_mouse.py:upload_pattern's `bullet_interval_s` -- a parameter that
# outlived the merge it drove, because two callers passed it POSITIONALLY and
# dropping it silently would have slid `t_s` into its place. The debt carried
# its own exit condition as a predicate ("does anyone still pass a fourth
# positional argument"), so when the callers were fixed on 2026-08-08 this
# ledger went RED demanding the line be struck, rather than quietly granting
# that file a permanent amnesty. A debt whose reason belongs to scheduling
# must LEAVE this table; only EXEMPT reasons, which belong to the code, stay.
DEBT = {
}

LEDGER = {**EXEMPT, **DEBT}


def _is_stub(node):
    """A body that cannot read its parameters because it has no body.

    Abstract methods, protocol declarations, not-implemented-yet raisers. Their
    parameters are read by implementors, and the signature is the contract --
    which is the opposite of a dead parameter, not an instance of one.
    """
    body = [s for s in node.body
            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    if not body:
        return True
    return len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Raise))


def _reads(node):
    """Every name this body could be reading a parameter through.

    ⚠ ast.Name AND NOTHING ELSE, and both exclusions were written the wrong way
    round first — the self-test below caught both on its first run, which is
    the entire argument for it existing:

      ast.arg   is the PARAMETER'S OWN DECLARATION. Counting it made every
                parameter read by itself, so scan() returned [] for the whole
                repo and printed ✓. A gate that cannot fail is worse than no
                gate: it is a gate everyone believes in.
      ast.keyword.arg  is the keyword NAME at a call site. Counting it made
                `g(a, timeout=3)` look like a read of `timeout` — which is
                precisely the ensure_ready/match_timeout shape this file was
                written to catch, waved through by its own scanner.

    The value side of `g(timeout=timeout)` is an ast.Name and is already
    covered, so nothing is lost by dropping both.
    """
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _private(name):
    """`_foo`, and not `__dunder__`. The only names arity() will judge.

    ⚠ THIS IS THE WHOLE FALSE-POSITIVE DEFENCE, arrived at after trying the
    obvious rule first. "Unique definition in the repo" flagged fifteen sites
    and every one was a collision with something the repo does not own:

        np.where(cond, a, b)        vs a local two-parameter `where`
        rows.index(x)               vs a local `index`
        estimator.fit(X, y)         vs detector/'s own `fit`
        bettercam.create(...)       vs a local `create`
        screen.verify(frame)        NOT A METHOD AT ALL — Screen.__init__
                                    stores a function on the instance, so no
                                    static rule about method definitions can
                                    ever be right about it

    A leading underscore says this repo wrote it AND this repo is the only
    thing that calls it, which is exactly the premise the check needs. It also
    loses nothing that matters: the edit this exists to catch is removing a
    parameter, and the parameters safe to remove are the private ones.
    """
    return name.startswith('_') and not name.startswith('__')


def _sig(node):
    """(min positional, max positional or None, {every accepted name})."""
    a = node.args
    pos = [x.arg for x in a.posonlyargs + a.args]
    req = len(pos) - len(a.defaults or ())
    names = set(pos) | {x.arg for x in a.kwonlyargs}
    if a.vararg or a.kwarg:
        return req, None, names
    return req, len(pos), names


def arity(dirs=DIRS + ('tools',)):
    """Call sites that cannot possibly bind. -> [(where, lineno, message)]

    ⚠ THE DEAD-PARAMETER SCAN CANNOT SEE THIS, and 2026-08-07 proved it inside
    one session. `TabWatch._set_open(value, now)` was correctly flagged and
    correctly fixed — and two callers in tools/ still passed `now`. scan()
    was green (it only reads control/…), and the TypeError surfaced twenty
    minutes later out of `pixi run tab-watch`. Removing a parameter is exactly
    the edit that breaks callers, so the two checks belong in the same file.

    NARROW ON PURPOSE, because Python method dispatch is not statically
    knowable. Four restrictions, and the first is the one that makes the rest
    hold:

      - PRIVATE NAMES ONLY (`_foo`, not `__dunder__`). A leading underscore
        means this repo wrote it and this repo calls it, which is what lets a
        single definition stand for the whole call graph. Without it the check
        is unusable: the first run flagged fifteen sites, and EVERY ONE was a
        collision with something outside the repo — np.where, list.index, a
        sklearn .fit, bettercam.create, and `screen.verify`, which is not a
        method at all but a function stored on an instance. Private names
        cover 212 definitions with zero of that.
      - only `something.NAME(...)` calls, never bare NAME(...) — a bare name
        can be a local, a builtin or an import.
      - only NAMEs with exactly ONE definition in the tree. `_log` has many
        owners with different signatures; a mismatch against one says nothing.
        Same homonym rule tools/surface.py's driving classifier needed, for
        the same reason, in the same repo.
      - only TOO MANY arguments, never too few. Too few is often a bound
        method, a partial or a decorator; too many binds nothing under any of
        those.
    """
    files = []
    for d in dirs:
        for p in sorted((ROOT / d).rglob('*.py')):
            if '__pycache__' in p.parts:
                continue
            try:
                tree = ast.parse(p.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            files.append((p.relative_to(ROOT).as_posix(), tree))
    return arity_over(files)


def arity_over(files):
    """The predicate itself, over [(label, tree)]. -> [(label, lineno, msg)]

    Split out from arity() so ARITY_SELFTEST runs the SAME code the repo scan
    runs. A self-test against a reimplementation tests the reimplementation.
    """
    defs = collections.defaultdict(list)
    for _, tree in files:
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and _private(n.name):
                defs[n.name].append(n)

    unique = {k: v[0] for k, v in defs.items() if len(v) == 1}
    out = []
    for rel, tree in files:
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if not isinstance(f, ast.Attribute) or f.attr not in unique:
                continue
            node = unique[f.attr]
            req, cap, names = _sig(node)
            # The definition's `self`/`cls` is not passed at an attribute call
            # site. Only subtract it when the def really is a method.
            first = ([x.arg for x in node.args.posonlyargs + node.args.args]
                     or [None])[0]
            bound = 1 if first in ('self', 'cls') else 0
            if any(isinstance(x, ast.Starred) for x in n.args) or \
                    any(k.arg is None for k in n.keywords):
                continue                       # *args / **kwargs at the call
            given = len(n.args)
            if cap is not None and given > cap - bound:
                out.append((rel, n.lineno,
                            f'{f.attr}() takes at most {cap - bound} '
                            f'positional argument(s), {given} given'))
                continue
            if node.args.kwarg:
                continue          # **kw accepts any keyword; nothing to check
            bad = [k.arg for k in n.keywords if k.arg not in names]
            if bad:
                out.append((rel, n.lineno,
                            f'{f.attr}() has no parameter '
                            f'{", ".join(repr(b) for b in bad)}'))
    return sorted(out)


def scan(dirs=DIRS):
    """-> [(key, lineno, [dead params])], sorted."""
    out = []
    for d in dirs:
        for p in sorted((ROOT / d).rglob('*.py')):
            if '__pycache__' in p.parts:
                continue
            rel = p.relative_to(ROOT).as_posix()
            try:
                tree = ast.parse(p.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                # A file that will not parse is check_layering's problem and
                # smoke's problem, both of which say so far more clearly than
                # this would. Skipping keeps one failure from being reported
                # by three gates in three different vocabularies.
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if _is_stub(node):
                    continue
                a = node.args
                names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
                names = [n for n in names
                         if n not in ('self', 'cls') and not n.startswith('_')]
                if not names:
                    continue
                # locals() / vars() / eval() can read anything. Rare, and a body
                # that does it is beyond static reach -- so it is skipped rather
                # than guessed at.
                src = ast.dump(node)
                if "id='locals'" in src or "id='vars'" in src or "id='eval'" in src:
                    continue
                read = _reads(node)
                dead = [n for n in names if n not in read]
                if dead:
                    out.append((f'{rel}:{node.name}', node.lineno, dead))
    return sorted(out)


# ════════════════════════════════════════════════════════════
# Two-sided self-test
# ════════════════════════════════════════════════════════════
#
# ⚠ A GREEN RUN ON THIS REPO PROVES NOTHING BY ITSELF. The repo currently has
# zero offenders, so a scanner that returned [] unconditionally would also
# print ✓ -- and would keep printing it while the next dead parameter walked
# in. That is the failure `probe_ads_after_reload` had: a criterion whose
# "should not hold" side was never looked at.
#
# So every case below is paired: the MUST-BITE half and the MUST-NOT half of
# the same rule, so a hatch widened to let one through is caught by the other.

SELFTEST = [
    # (source, function name, expected dead params)
    ('def f(a, b):\n    return a', 'f', ['b']),
    ('def f(a, b):\n    return a + b', 'f', []),
    # the `_` convention: an API-mandated slot, deliberately unread
    ('def f(hwnd, _):\n    return hwnd', 'f', []),
    ('def f(hwnd, extra):\n    return hwnd', 'f', ['extra']),
    # stubs declare, they do not read -- and the non-stub next to it must bite
    ('def f(a, timeout_s=1):\n    raise NotImplementedError', 'f', []),
    ('def f(a, timeout_s=1):\n    """doc"""', 'f', []),
    ('def f(a, timeout_s=1):\n    """doc"""\n    return a', 'f', ['timeout_s']),
    # read only through a keyword pass-through -- the ensure_ready/match_timeout
    # shape, which is exactly the regression this file exists for
    ('def f(a, timeout):\n    return g(a, timeout=timeout)', 'f', []),
    ('def f(a, timeout):\n    return g(a, timeout=3)', 'f', ['timeout']),
    # self/cls are never parameters in this sense; a real dead one beside them is
    ('class C:\n    def m(self, a):\n        return 1', 'm', ['a']),
    ('class C:\n    def m(self):\n        return 1', 'm', []),
    # kwonly and posonly reach the same rule as ordinary args
    ('def f(a, *, verbose=False):\n    return a', 'f', ['verbose']),
    ('def f(a, /, b):\n    return a', 'f', ['b']),
    # a body that can read anything is beyond static reach -- skipped, not guessed
    ('def f(a, b):\n    return locals()', 'f', []),
]

# arity()'s half, same two-sided shape. Each source is scanned whole and the
# expected list is every message's leading symbol, so a rule that stops biting
# and a rule that starts over-biting both show up here.
ARITY_SELFTEST = [
    # THE 2026-08-07 BUG ITSELF: the parameter went, the caller did not.
    ('class C:\n    def _set_open(self, v):\n        return v\n'
     'w = C()\nw._set_open(True, 1.0)\n', ['_set_open']),
    ('class C:\n    def _set_open(self, v):\n        return v\n'
     'w = C()\nw._set_open(True)\n', []),
    # a keyword the signature does not have
    ('class C:\n    def _go(self, a, b=1):\n        return a\n'
     'C()._go(1, c=2)\n', ['_go']),
    ('class C:\n    def _go(self, a, b=1):\n        return a\n'
     'C()._go(1, b=2)\n', []),
    # **kw takes anything; *args takes any count
    ('class C:\n    def _go(self, a, **kw):\n        return a\n'
     'C()._go(1, whatever=2)\n', []),
    ('class C:\n    def _go(self, *a):\n        return a\n'
     'C()._go(1, 2, 3, 4)\n', []),
    # too FEW is not judged — bound methods and decorators make it meaningless
    ('class C:\n    def _go(self, a, b):\n        return a\n'
     'C()._go(1)\n', []),
    # public names are out of scope, however wrong they look — that is the
    # false-positive defence, and it has to be tested from the side that would
    # break it or the next person will "improve" it back
    ('class C:\n    def go(self, v):\n        return v\n'
     'C().go(1, 2, 3)\n', []),
    ('def _free(a):\n    return a\n_free(1, 2)\n', []),   # bare call, not x.f()
    # two owners disagree, so neither can be judged
    ('class A:\n    def _m(self, a):\n        return a\n'
     'class B:\n    def _m(self, a, b):\n        return b\n'
     'x._m(1, 2)\n', []),
]


def selftest():
    """-> exit code. Each case run through the SAME predicates main() uses."""
    bad = 0
    for src, want_fn, want in SELFTEST:
        tree = ast.parse(src)
        got = None
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != want_fn:
                continue
            if _is_stub(node):
                got = []
                break
            a = node.args
            names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
            names = [n for n in names
                     if n not in ('self', 'cls') and not n.startswith('_')]
            dump = ast.dump(node)
            if any(f"id='{f}'" in dump for f in ('locals', 'vars', 'eval')):
                got = []
                break
            read = _reads(node)
            got = [n for n in names if n not in read]
            break
        if got != want:
            bad += 1
            print(f'  ✗ {src.splitlines()[0]!r} -> {got}, expected {want}')
    for src, want in ARITY_SELFTEST:
        got = [m.split('(')[0] for _, _, m in
               arity_over([('<test>', ast.parse(src))])]
        if got != want:
            bad += 1
            print(f'  ✗ arity {src.splitlines()[-1]!r} -> {got}, '
                  f'expected {want}')
    # ⚠ BOTH SIDES, because a copy-scan that only ever sees clean files is a
    # scan that has been watched passing. The live value must BITE and a
    # superseded one must NOT -- that second case is the whole design, since
    # retraction records are how this repo keeps what it disproved.
    owned = _owned_constants()
    if not owned:
        bad += 1
        print('  ✗ no owned constants resolved — the copy-scan is inert')
    else:
        live = next(iter(owned))
        cases = [(f'    K = {live:g}   # a prose copy', 1),
                 ('    K = 1.5474  # superseded, a record', 0),
                 ('    K = prof["K"]  # reads config', 0)]
        for src, want in cases:
            got = len(_literal_copies([('<test>', src)]))
            if got != want:
                bad += 1
                print(f'  ✗ copy-scan {src.strip()!r} -> {got}, expected {want}')

    n = len(SELFTEST) + len(ARITY_SELFTEST) + 3
    bite = (sum(1 for _, _, w in SELFTEST if w)
            + sum(1 for _, w in ARITY_SELFTEST if w) + 1)
    if bad:
        print(f'\n✗ self-test {n - bad}/{n}')
        return 1
    print(f'  ✓ self-test {n}/{n} '
          f'({bite} of them must BITE, so "always return []" does not pass)')
    return 0


def unreachable(dirs=DIRS + ('tools',)):
    """Statements a `return`/`raise`/`break`/`continue` makes dead. -> [(k, why)]

    ⚠ CODE THAT CANNOT RUN AND READS LIKE POLICY IS WORSE THAN NO CODE. Ten
    lines sat after a `return` in detector/weapon.set_seq describing how a
    kitted gun gets compensated -- scope_factor * naked_scale *
    attachment_factor * posture_f -- and under plan A there ARE no factors: the
    curve is looked up by the exact configuration and emitted with none
    applied. Anyone reading that file for how attachments reach the firmware
    found the wrong answer, in code that looked live.

    It also called attachment_factor with CATALOGUE KEYS where that function
    wants ASSET names, so had it ever run it would have applied 1.0 to every
    gun and looked entirely reasonable doing it (the same vocabulary error was
    found live in tools/import_kava4.py the same day).

    Only the FIRST dead statement per block is reported: the rest are dead for
    the same reason and listing them buries the cause.
    """
    term = (ast.Return, ast.Raise, ast.Continue, ast.Break)
    out = []
    for d in dirs:
        for path in sorted((ROOT / d).rglob('*.py')):
            try:
                tree = ast.parse(path.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            rel = path.relative_to(ROOT).as_posix()
            for node in ast.walk(tree):
                body = getattr(node, 'body', None)
                if not isinstance(body, list):
                    continue
                for i, st in enumerate(body[:-1]):
                    if isinstance(st, term):
                        out.append((f'{rel}:{body[i + 1].lineno}',
                                    f'unreachable — the {type(st).__name__} '
                                    f'on line {st.lineno} always fires first'))
                        break
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--all', action='store_true',
                    help='list ledger entries too, not just the offenders')
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    print(f'checked {"/".join(DIRS)} for parameters no body reads\n')
    rc_self = selftest()

    found = scan()
    keys = {k for k, _, _ in found}
    live = [(k, ln, d) for k, ln, d in found if k not in LEDGER]

    if a.all:
        for table, label in ((EXEMPT, 'EXEMPT'), (DEBT, 'DEBT')):
            for k, why in sorted(table.items()):
                mark = '' if k in keys else '  (no longer offending)'
                print(f'  {label:<6s} {k}{mark}\n           {why}')
        if EXEMPT or DEBT:
            print()

    rc = rc_self
    dead = unreachable()
    if dead:
        rc = 1
        print(f'✗ {len(dead)} unreachable statement(s) — delete them or move '
              f'them above the exit. Dead code that reads like policy is how a '
              f'wrong answer survives in a file people consult:')
        for k, why in dead:
            print(f'    {k}  {why}')
        print()
    else:
        print('✓ nothing sits after a return/raise/break/continue')

    # The ratchet: a DEBT entry that has been paid must be deleted. Without
    # this the table silently stops covering the file it names, which is how a
    # ledger becomes an amnesty.
    stale = [k for k in DEBT if k not in keys]
    if stale:
        rc = 1
        print(f'✗ {len(stale)} DEBT entr(y/ies) already paid — delete the line, '
              f'or the ledger stops covering that file:')
        for k in sorted(stale):
            print(f'    {k}')
        print()

    led_lines, led_bad = audit([('params EXEMPT', EXEMPT),
                                ('params DEBT', DEBT)])
    for line in led_lines:
        print(line)
    for key, why in led_bad:
        rc = 1
        print(f'✗ {key}  (ledger reason)\n    {why}')

    copies = _literal_copies()
    if copies:
        rc = 1
        owner = copies[0][3]
        print(f'✗ {len(copies)} prose cop(y/ies) of a constant config owns. '
              f'It has one author ({owner}); a number written out here is a '
              f'second one that nothing keeps in step:')
        for rel, ln, text, _ in copies:
            print(f'    {rel}:{ln}  {text}')
        print('\n  Name the constant instead of the number, or cite the value '
              'it REPLACED — a superseded number is a record and passes.\n')

    calls = arity()
    if calls:
        rc = 1
        print(f'✗ {len(calls)} call site(s) cannot bind. Removing a parameter '
              f'is what breaks these, and the scan above cannot see it:')
        for rel, ln, msg in calls:
            print(f'    {rel}:{ln}  {msg}')
        print()

    if live:
        rc = 1
        print(f'✗ {len(live)} function(s) take a parameter no body reads. '
              f'A caller reads the signature and believes the knob is wired:')
        for k, ln, dead in live:
            path, fn = k.rsplit(':', 1)
            print(f'    {path}:{ln}  {fn}({", ".join(dead)})')
        print('\n  Remove it, wire it, or file it in EXEMPT/DEBT at the top of '
              'this file with the reason.')
    else:
        print('✓ no unread parameters '
              f'(ledger: {len(EXEMPT)} exempt, {len(DEBT)} in debt)')
    if not calls:
        print('✓ every private call site binds, across '
              f'{"/".join(DIRS + ("tools",))}')
    return rc


if __name__ == '__main__':
    sys.exit(main())
