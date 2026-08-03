"""Names used but never defined. Parses; runs nothing.

    pixi run names

Python resolves a global at CALL time, so a name that is only mentioned inside
a method is not checked by importing the module, by `python -c "import x"`, or
by any test that does not take that branch. It is checked the first time that
line executes — which, for a repository whose branches need a running game, can
be minutes into a live run.

That is not hypothetical. `SLOT_NAMES` was used in collect_templates.Collector.
slot_detail and never imported. The module imported fine, `pixi run layering`
passed (it reads imports, and there was nothing wrong with the imports that
were there), `pixi run runs` passed (it imports the module and calls a pure
function), and the collector died with NameError two rounds into a run that had
already spent four minutes spawning and photographing.

WHAT THIS IS NOT. It is not a type checker and not pyflakes. It answers one
question -- is every bare name that is READ somewhere reachable as a builtin, a
module-level name, or something bound in an enclosing function -- and it is
deliberately conservative about saying yes:

  * a module containing `from x import *` is skipped entirely, because after a
    star import nothing can be said about what is defined.
  * a function using `global`/`nonlocal`, or any name bound ANYWHERE in the
    function, counts as bound for the whole function. Python's own scoping is
    function-wide, so this matches rather than approximates.
  * attribute access (`self.x`, `mod.y`) is not a bare name and is not checked.

A false positive here would be worse than the bug: a check people learn to
ignore stops being a check. So it errs to silence, and what it does report is
a name that genuinely cannot resolve.
"""
import ast
import builtins
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SKIP_DIRS = {'.pixi', '.git', 'temp_debug', 'temp_image', 'training_data',
             'docs', '__pycache__', 'node_modules', '.claude'}

BUILTINS = set(dir(builtins)) | {'__file__', '__name__', '__doc__',
                                 '__package__', '__spec__', '__builtins__'}


def bound_by(node):
    """Every name this scope binds, anywhere in it.

    Python binds per FUNCTION, not per statement, so a name assigned on the
    last line is local from the first. Walking the whole body and collecting
    every binding form is therefore exact for this purpose, not a shortcut.
    """
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split('.')[0])
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars is not None:
                    for m in ast.walk(item.optional_vars):
                        if isinstance(m, ast.Name):
                            out.add(m.id)
    return out


def loads(node):
    """Bare names READ in this scope, with their line numbers.

    Nested functions are skipped: they get their own pass, with this scope as
    an enclosing one, so a name they bind for themselves is not charged here.
    """
    out = []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            out.append((n.id, n.lineno))
        stack.extend(ast.iter_child_nodes(n))
    return out


def scopes(node, enclosing, out):
    """Walk every function, checking its reads against what can reach it."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.Lambda)):
            here = enclosing | bound_by(child)
            for name, line in loads(child):
                if name not in here and name not in BUILTINS:
                    out.append((line, name))
            scopes(child, here, out)
        elif isinstance(child, ast.ClassDef):
            # A class body's names are NOT visible to its methods, so the
            # methods are checked against the enclosing scope, not the class.
            scopes(child, enclosing, out)
        else:
            scopes(child, enclosing, out)


def check(path):
    src = open(path, encoding='utf-8').read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [(e.lineno or 0, f'SyntaxError: {e.msg}')]
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and any(a.name == '*' for a in n.names):
            return []                      # nothing can be said after a star
    module = bound_by(tree)
    out = []
    for name, line in loads(tree):
        if name not in module and name not in BUILTINS:
            out.append((line, name))
    scopes(tree, module, out)
    return sorted(set(out))


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    bad, n = {}, 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if not f.endswith('.py'):
                continue
            p = os.path.join(dirpath, f)
            n += 1
            hits = check(p)
            if hits:
                bad[os.path.relpath(p, ROOT)] = hits
    print(f'checked {n} files')
    if not bad:
        print('\nevery name resolves')
        return 0
    total = sum(len(v) for v in bad.values())
    print(f'\n{total} name(s) that cannot resolve:')
    for path, hits in sorted(bad.items()):
        for line, name in hits:
            print(f'  {path}:{line}  {name}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
