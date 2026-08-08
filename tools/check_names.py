"""Undefined names, statically. -> exit 1 if any

    pixi run names

WHY THIS EXISTS, and it is one bug not a category. On 2026-08-07 a filter was
added to Kit._swap_back referencing `weapon`, a parameter that method does not
take. `py_compile` passes -- a NameError is a runtime event -- and the method's
own `except Exception` turned it into a log line, so the collector kept going.
Every teardown threw the run's parts away with the gun, restock respawned them,
and the loop measured nothing for a whole invocation before anyone looked.

WHAT IT DOES NOT DO: it is not a linter and does not try to be. It resolves
names against builtins, module globals, imports, and the enclosing function and
class scopes, and reports what is left. Anything it cannot resolve confidently
it stays quiet about -- a checker that cries wolf gets muted, and a muted
checker is worse than none.

⚠ SO A GREEN RUN IS NOT A PROOF. It catches the shape that has actually bitten:
a name used inside a function that no enclosing scope defines.
"""
import argparse
import ast
import builtins
import os
import pathlib
import re
import sys

from _ledger import CODE, Reason, audit, defines, has_cli_flag

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

# Directories that are not ours to police.
SKIP_DIRS = {'.pixi', '.git', 'docs', '__pycache__',
             'node_modules', '.venv'}

BUILTINS = set(dir(builtins)) | {'__file__', '__name__', '__doc__',
                                 '__package__', '__spec__'}


def _bound_by(node):
    """Every name a scope binds: params, assignments, imports, defs, etc."""
    out = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = node.args
        for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
            out.add(arg.arg)
        if a.vararg:
            out.add(a.vararg.arg)
        if a.kwarg:
            out.add(a.kwarg.arg)
    for sub in ast.walk(node):
        # Do not descend into nested functions for THEIR bindings -- those
        # belong to the nested scope. Their names are checked separately.
        if isinstance(sub, (ast.Name,)) and isinstance(sub.ctx, ast.Store):
            out.add(sub.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            out.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for al in sub.names:
                out.add((al.asname or al.name).split('.')[0])
        elif isinstance(sub, ast.Global) or isinstance(sub, ast.Nonlocal):
            out.update(sub.names)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            out.add(sub.name)
        elif isinstance(sub, ast.comprehension):
            for n in ast.walk(sub.target):
                if isinstance(n, ast.Name):
                    out.add(n.id)
        elif isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                if item.optional_vars is not None:
                    for n in ast.walk(item.optional_vars):
                        if isinstance(n, ast.Name):
                            out.add(n.id)
    return out


def check(path):
    """[(line, name, func)] for names nothing in scope defines."""
    try:
        tree = ast.parse(open(path, encoding='utf-8').read(), path)
    except (SyntaxError, UnicodeDecodeError):
        return []          # py_compile's job, not this one's
    module = _bound_by(tree) | BUILTINS
    bad = []

    def walk(node, scopes, where):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, scopes + [_bound_by(child)],
                     f'{where}.{child.name}' if where else child.name)
            elif isinstance(child, ast.Lambda):
                # A lambda binds its parameters, and `key=lambda kv: kv[1]`
                # is everywhere in this repo. Missing this produced 20 of the
                # first run's 32 hits -- see the docstring on crying wolf.
                walk(child, scopes + [_bound_by(child)], where)
            elif isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp,
                                    ast.GeneratorExp)):
                # Comprehensions are their own scope in Python 3, and their
                # targets are visible only inside. Pushing them here is what
                # keeps `[r for r in rows if r.get(...)]` quiet.
                inner = set()
                for gen in child.generators:
                    for n in ast.walk(gen.target):
                        if isinstance(n, ast.Name):
                            inner.add(n.id)
                walk(child, scopes + [inner], where)
            elif isinstance(child, ast.ClassDef):
                # A class body's names are NOT visible to its methods, so the
                # class scope is deliberately not pushed.
                walk(child, scopes,
                     f'{where}.{child.name}' if where else child.name)
            else:
                if isinstance(child, ast.Name) and isinstance(child.ctx,
                                                              ast.Load):
                    if not any(child.id in s for s in scopes):
                        bad.append((child.lineno, child.id, where))
                walk(child, scopes, where)

    walk(tree, [module], '')
    return bad



# ── the other kind of name that will not resolve ──────────────────────────

# Paths this repo's own comments point at. Restricted to the source layers: a
# comment naming docs/ is pointing at data that is deliberately not in git and
# legitimately comes and goes.
#
# ⚠ `dl_models` was added 2026-08-08 after four comments in config.py and one in
# game_state.py were caught pointing at dl_models/icon_layout.py, deleted with
# the fire-mode CNN. They survived BECAUSE this list did not name the directory,
# and config.py's own comment says why that is the dangerous shape: "dl_models/
# is not on any layering rule and not walked by most audits, so 'some directory
# over there uses these' was unfalsifiable from where anyone was standing."
_REF = re.compile(r'\b((?:calibration|tools|harness|control|detector|press|'
                  r'dl_models)/[\w/]+\.(?:py|c|h|md))')


def dangling_refs(path, root):
    """File paths named in this file that do not exist. -> [(line, ref)]

    ⚠ THE STRONGEST REFERENCE IN THIS REPO IS A CODE COMMENT, and tools/
    CLAUDE.md says so after auditing every probe: "探针从来不被 import，所以
    「谁 import 我」对这个目录是个恒零的量具；对它们成立的问题是「哪个常量的
    出处是我」". press/pico_mouse.py cites a probe for L = 38 ms;
    detector/slot_detector.py cites one for its bleed threshold.

    So deleting a file silently orphans every number whose provenance was that
    file — and the 2026-08-08 removal of the bullet-bucket coordinate did
    exactly that to sixteen comments in one afternoon. The measurement usually
    survives somewhere; what is lost is the ability to find out where.

    Marked-as-deleted references are still flagged. The fix is to say where
    the number lives NOW, not to note that its old home is gone.

    ⚠ THE ONE EXCEPTION IS REF_DEBT, and it is narrow on purpose: a file whose
    ABSENCE is the fact being recorded. "This builder cannot run because X was
    deleted" has to name X, and there is nowhere else the number lives. Left
    unexempted, this gate goes permanently red over four lines of true prose —
    and a gate nobody can get green is a gate everybody learns to ignore, which
    this module's own docstring warns about two paragraphs up.
    """
    out = []
    try:
        text = pathlib.Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return out
    rel = pathlib.Path(path).resolve().relative_to(
        pathlib.Path(root).resolve()).as_posix()
    for i, line in enumerate(text.split('\n'), 1):
        for m in _REF.finditer(line):
            if (pathlib.Path(root) / m.group(1)).exists():
                continue
            if (rel, m.group(1)) in REF_DEBT:
                continue
            out.append((i, m.group(1)))
    return out


# A dead path named on purpose, because its deletion IS the fact. Keyed by
# (the file doing the naming, what it names) so an exemption cannot leak to a
# second citation of the same path somewhere nobody thought about.
REF_DEBT = {
    ('calibration/build_kit_factors.py', 'calibration/analyse_factors.py'): Reason(
        'names the module whose deletion is why this builder cannot build. '
        'There is no "where the number lives now" to point at instead: the '
        'reader is gone AND so is every run in SOURCES, which is precisely '
        'what the ⚠⚠ note and the --write guard exist to say.',
        CODE,
        lambda: (defines('calibration/build_kit_factors.py', 'missing_sources')
                 and has_cli_flag('calibration/build_kit_factors.py', '--selftest'))),
    ('tools/check_names.py', 'calibration/analyse_factors.py'): Reason(
        'the checker cites the case that produced it. dangling_imports exists '
        'because this exact import was invisible to layering, params and '
        'smoke, and a check whose docstring cannot name its own origin is the '
        'kind nobody can tell is still earning its runtime.',
        CODE,
        lambda: defines('tools/check_names.py', 'dangling_imports')),

    # ── the fire-mode CNN, deleted 2026-08-08 ──
    # Same shape as the two above: the file's ABSENCE is the recorded fact.
    # These three appeared the moment `dl_models` was added to _REF, which is
    # the check working: config.py had been pointing at icon_layout.py for a
    # day with nothing looking, and config.py's own comment says why that was
    # the dangerous shape ("not on any layering rule and not walked by most
    # audits, so 'some directory over there uses these' was unfalsifiable").
    ('config.py', 'dl_models/icon_layout.py'): Reason(
        'FIRE_MODE and POSTURE have zero readers now, and icon_layout.py was '
        'the last one. Naming it is how the block says so — without the name, '
        '"nothing reads these" is an assertion the next reader cannot check.',
        CODE,
        lambda: defines('detector/fire_mode_detector.py', 'FIRE_MODE_CLASSES')),
    ('detector/fire_mode_detector.py', 'dl_models/icon_layout.py'): Reason(
        'FIRE_MODE_CLASSES moved here from that file. Saying where a symbol '
        'CAME FROM is the one citation a deleted module still earns — the '
        'order constraint that used to travel with it (a softmax head indexed '
        'off it) is exactly what stopped applying.',
        CODE,
        lambda: defines('detector/fire_mode_detector.py', 'FIRE_MODE_CLASSES')),
    ('tools/check_names.py', 'dl_models/icon_layout.py'): Reason(
        'the _REF comment names the five citations that adding `dl_models` '
        'caught. A rule whose docstring cannot say what it found on its first '
        'run is one nobody can tell is still earning its runtime.',
        CODE,
        lambda: 'dl_models' in _REF.pattern),
}


# ── an import that will not resolve ───────────────────────────────────────

# This repo's own packages. A third-party import that goes missing announces
# itself the moment anything runs; one of OURS goes missing when a sibling
# module is deleted, and the file that imported it just stops being runnable
# while still reading like live code.
_PKGS = ('calibration', 'control', 'detector', 'harness', 'press', 'tools',
         'dl_models')


def _module_exists(dotted, root):
    p = pathlib.Path(root) / pathlib.Path(*dotted.split('.'))
    return p.with_suffix('.py').exists() or (p / '__init__.py').exists()


def dangling_imports(path, root):
    """Imports of this repo's own modules that are not on disk. -> [(line, mod)]

    ⚠ THIS IS THE CHECK THAT FOUND calibration/build_kit_factors.py ON 2026-08-08,
    and it is in this file because that one was invisible to every other gate.
    `layering` parses imports but only asks which LAYER they cross. `params`
    reads signatures. `smoke` builds the detectors. None of them opens a tools/
    script, because nothing imports one — so a script can stop being runnable
    and stay green everywhere for as long as nobody types its name.

    What it cost: build_kit_factors imports calibration/analyse_factors.py,
    deleted with the bullet-bucket coordinate. It is the only writer of
    data/kit_factors.json, which detector/weapon_attachments.py reads on
    import on every single run. A kit factor is a MULTIPLIER on the curve, not
    a point on it, so nothing about the coordinate change touched it — it just
    shared a directory with what did.

    Walks ALL imports, not just module-scope ones. Moving a broken import into
    a function is how it gets quiet, not how it gets fixed, and the entry in
    IMPORT_DEBT below is exactly that case declared out loud.
    """
    out = []
    try:
        tree = ast.parse(pathlib.Path(path).read_text(encoding='utf-8',
                                                      errors='replace'))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return out
    for n in ast.walk(tree):
        mods = []
        if isinstance(n, ast.ImportFrom) and n.module and not n.level:
            mods.append(n.module)
            # `from calibration import rpm_store` — rule 10 mandates this form,
            # so the name being imported is a MODULE more often than not.
            if n.module in _PKGS:
                mods += [f'{n.module}.{a.name}' for a in n.names]
        elif isinstance(n, ast.Import):
            mods += [a.name for a in n.names]
        for m in mods:
            if m.split('.')[0] not in _PKGS or '.' not in m:
                continue
            if not _module_exists(m, root):
                # `from calibration import rpm_store` also yields the bare
                # package, which exists; only report the leaf that does not.
                out.append((n.lineno, m))
    return out


# An import that cannot resolve, kept on purpose. Same shape as every other
# ledger in this repo: the reason is a claim about the code, and the check is
# what keeps the claim honest.
IMPORT_DEBT = {
    'calibration/build_kit_factors.py': Reason(
        'calibration/analyse_factors.py is gone AND so is every path in '
        'SOURCES, so build() cannot run at all — but --selftest can, and it is '
        'the half worth keeping: it asserts the runtime still reads '
        'data/kit_factors.json and still falls through for kits that are not '
        'in it. The import is inside build() so the checker survives the '
        'builder. main() refuses --write when a source is missing, because an '
        'empty table is not an empty answer — it silently demotes every gun to '
        'the wiki coefficients, median 34.7% off.',
        CODE,
        lambda: (has_cli_flag('calibration/build_kit_factors.py', '--selftest')
                 and defines('calibration/build_kit_factors.py', 'missing_sources')
                 and not imports_at_module_scope('calibration/build_kit_factors.py',
                                                 'calibration.analyse_factors'))),
}


def imports_at_module_scope(rel, module):
    """Is `module` imported at the TOP of `rel`, rather than inside a function?

    The distinction is the whole reason the debt entry above is allowed: at
    module scope a dead import takes every entry point down with it, and inside
    a function it takes down only the one that needs it.
    """
    p = pathlib.Path(ROOT) / rel
    try:
        tree = ast.parse(p.read_text(encoding='utf-8'))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module == module:
            return True
        if isinstance(n, ast.Import) and any(a.name == module for a in n.names):
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='*', default=None)
    a = ap.parse_args()

    files = []
    for base in (a.paths or [ROOT]):
        if os.path.isfile(base):
            files.append(base)
            continue
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            files += [os.path.join(root, n) for n in names if n.endswith('.py')]

    hits = 0
    for f in sorted(files):
        for line, name, where in check(f):
            hits += 1
            rel = os.path.relpath(f, ROOT).replace(os.sep, '/')
            print(f'{rel}:{line}  undefined name {name!r} in {where or "<module>"}')

    refs = 0
    for f in sorted(files):
        for line, ref in dangling_refs(f, ROOT):
            refs += 1
            rel = os.path.relpath(f, ROOT).replace(os.sep, '/')
            print(f'{rel}:{line}  points at {ref}, which does not exist')

    imps = 0
    for f in sorted(files):
        rel = os.path.relpath(f, ROOT).replace(os.sep, '/')
        for line, mod in dangling_imports(f, ROOT):
            if rel in IMPORT_DEBT:
                continue
            imps += 1
            print(f'{rel}:{line}  imports {mod}, which is not on disk — this '
                  f'file cannot run')

    # The other half of the ratchet: an entry that is no longer broken, or
    # whose stated reason has stopped being true, has to leave the table.
    for rel, r in IMPORT_DEBT.items():
        if not dangling_imports(os.path.join(ROOT, rel), ROOT):
            imps += 1
            print(f'{rel}: on IMPORT_DEBT but every import resolves now — '
                  f'delete the entry')
    # Both ledgers ratchet the same way: an entry whose subject came back has
    # to leave, or the exemption becomes permanent and silent.
    for (rel, target), _r in REF_DEBT.items():
        if (pathlib.Path(ROOT) / target).exists():
            refs += 1
            print(f'{rel}: on REF_DEBT for {target}, but that file exists '
                  f'again — delete the entry')
    lines, bad = audit([('import-debt', IMPORT_DEBT),
                        ('ref-debt', {f'{a} -> {b}': r
                                      for (a, b), r in REF_DEBT.items()})])

    print(f'\nchecked {len(files)} file(s)')
    for ln in lines:
        print(ln)
    for key, why in bad:
        imps += 1
        print(f'  ✗ {key}: {why}')
    rc = 0
    if hits:
        print(f'{hits} undefined name(s) — these are NameErrors waiting for a '
              f'run to reach them')
        rc = 1
    if imps:
        print(f'{imps} unresolvable import(s) of this repo\'s own modules. A '
              f'file that cannot import is not "stale", it is not code — and '
              f'nothing else in the gate set opens a tools/ script, because '
              f'nothing imports one.')
        rc = 1
    if refs:
        print(f'{refs} reference(s) to files that are gone. In this repo the '
              f'strongest citation a measurement has IS a code comment '
              f'(tools/CLAUDE.md), so a dead path orphans whatever number it '
              f'was the provenance for. Say where the number lives now.')
        rc = 1
    if not rc:
        print('no undefined names, no dangling file references')
    return rc


if __name__ == '__main__':
    sys.exit(main())
