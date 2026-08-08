"""Every path named in prose must point INTO a directory that exists.

    pixi run pointers

THE HOLE THIS FILLS, measured 2026-08-08. A refactor moved the measurement
artifacts out of `docs/` (into `calibration/artifacts/`) and the checked-in
assets into `data/`, and its own commit message said it had repointed the
skills. Twenty-odd references across five skill and agent files were still on
the old layout, and three of them were load-bearing:

    calibrate-recoil/SKILL.md   "samples are in docs/recoil/samples/"
    timing-analyst.md           "docs/recoil/samples/*.jsonl is the ONLY
                                 per-frame raw data" -- that agent's whole
                                 discipline is built on reading it
    calibrate-recoil/SKILL.md   "detector/weapon.py reads
                                 docs/recoil/curves_time/" -- wrong twice, it
                                 reads config.CURVES_DIR

`pixi run layering`, `surface --check` and `params` were all green through
every one of them, and they always will be: **a step written in Markdown is
invisible to every import graph.** The same shape is already recorded in
tools/CLAUDE.md as "删探针之前先查有没有代码在引用它" -- probe_icon_threshold is
a step in a skill and nothing in any import graph points at it.

THE CRITERION IS THE CONTAINING DIRECTORY, NOT THE FILE. A gate demanding that
every named file exist would be useless: half of these paths are outputs a
step is about to create (`--shoot baseline` lands at
calibration/artifacts/tab/baseline.png), and half carry placeholders
(`<weapon>__<config>.jsonl`) or globs. But a path whose PARENT DIRECTORY does
not exist cannot be an output either -- nothing is going to write into a tree
that is gone. That is exactly and only the failure above, and it is decidable.

⚠ WHAT THIS DOES NOT CATCH, said out loud because a gate whose limits are
unstated gets read as covering more than it does:

  * a pointer that names a directory that EXISTS but is the wrong one. If the
    refactor had moved samples from docs/recoil/ to docs/measurements/ while
    leaving docs/recoil/ populated, every stale pointer here would pass.
  * a function, flag or task name that moved. Only paths are checked.
  * prose that is simply wrong about what a path contains.
"""
import io
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {'.pixi', '.git', '__pycache__', 'node_modules', '.agent-refactor'}

# Prose that names paths. Deliberately NOT every .md in the repo: docs/attic is
# a graveyard on purpose and its pointers describe a layout that is gone by
# design, which is the one place a stale path is the correct content.
SCAN = ['.claude/**/*.md', '.claude/**/*.py', '**/CLAUDE.md', 'MODEL.md',
        # ⚠ SOURCE TOO, ADDED THE SAME DAY AND FOR A WORSE CASE. The first
        # version scanned prose only, on the reasoning that code that names a
        # missing directory fails on its own. It does not: `pixi run tab-open`
        # went red with "no stored 3440x1440 shots under docs/" and stayed
        # that way, because a corpus scan that finds NOTHING looks exactly
        # like a corpus that is empty. Eleven source files were still on the
        # old layout; eight of them held live paths.
        #
        # Comments are skipped by classify()'s caller below -- a comment that
        # names a deleted tree is usually the sentence saying it was deleted.
        '*.py', 'control/**/*.py', 'detector/**/*.py', 'calibration/**/*.py',
        'press/**/*.py', 'harness/**/*.py', 'tools/**/*.py']
SKIP_FILES = {'docs/attic', 'tools/check_pointers.py'}

# A reference has to START with a real top-level entry, and the list is READ
# FROM DISK rather than written here. A hard-coded list is the same defect the
# gate is about: a new top-level directory would silently stop being covered,
# and nothing would say so.
def _top_level():
    return {p.name for p in ROOT.iterdir()
            if p.name not in SKIP_DIRS and not p.name.startswith('.')}


def _pattern():
    names = sorted(_top_level(), key=len, reverse=True)
    alt = '|'.join(re.escape(n) for n in names)
    # One or more segments after the top-level name. `<` and `*` are allowed
    # inside a segment so placeholders and globs are captured whole and
    # trimmed below rather than cutting the match short.
    return re.compile(rf'\b(?:{alt})(?:/[A-Za-z0-9_.*<>-]+)+/?')


# Paths that legitimately point nowhere. Each entry is a claim about the
# repository and carries its reason, the same rule the layering ledger uses.
EXEMPT = {
    # calibration/build_kit_factors.py names its ELEVEN input runs one by one,
    # and every one of them is deleted. That is not a stale pointer, it is the
    # file's central claim: press/kit_factors.json is UNREBUILDABLE, and the
    # list is the only surviving record of what it was built FROM. Globbing
    # them was rejected for the same reason (its own docstring, "SOURCES ARE
    # LISTED, NOT GLOBBED"). Repointing them would erase the provenance of 28
    # measured rows that nothing can reproduce.
    #
    # ⚠ THE EXEMPTION IS THE DIRECTORY, NOT EACH FILE, so a NEW name added
    # under it is still exempt — accepted deliberately: the alternative is
    # eleven near-identical entries that all say this paragraph.
    'calibration/artifacts/recoil/runs':
        'eleven deleted source runs, listed by name in build_kit_factors.py '
        'because they are the only surviving provenance for kit_factors.json. '
        'The file says so itself: UNREBUILDABLE.',
    'docs/recoil/curves':
        'named inside the sentence that says it was DELETED (1184 curves '
        'fitted in the retired bullet-bin coordinate, 2026-08-08). A pointer '
        'to a deleted thing is the correct content when the prose is about '
        'the deletion — rewriting it to an existing path would make the '
        'paragraph false.',
}


def _is_directory_list(ref):
    """`control/calibration/harness/press/detector` is FIVE DIRECTORIES, not a
    path, and this prose style appears in four CLAUDE.md files.

    Fixed in the extractor rather than by growing EXEMPT: an exemption is a
    claim about one string, and this is a claim about a shape. Every segment
    being a top-level entry is decidable and no real path looks like it --
    `detector/press` would need a `press` inside `detector`, which the layering
    rules forbid outright.
    """
    top, segs = _top_level(), ref.rstrip('/').split('/')
    return len(segs) > 1 and all(s in top for s in segs)


def literal_prefix(ref):
    """The longest leading part of `ref` with no placeholder or glob in it.

    Returns (prefix, had_wildcard). A segment containing `*` or `<` ends the
    prefix and is dropped whole -- half a segment names nothing.
    """
    parts, out = ref.rstrip('/').split('/'), []
    for seg in parts:
        if '*' in seg or '<' in seg:
            return '/'.join(out), True
        out.append(seg)
    return '/'.join(out), False


def classify(ref):
    """-> 'ok' | 'output' | 'stale'

    ok      the literal part exists
    output  it does not, but its parent directory does — something is going to
            write it, or a variant of it has not been collected yet
    stale   the parent directory does not exist either, so nothing can write
            there and nothing can read it
    """
    if _is_directory_list(ref):
        return 'ok'
    prefix, _ = literal_prefix(ref)
    if not prefix or '/' not in prefix:
        return 'ok'                      # bare top-level name; nothing claimed
    if (ROOT / prefix).exists():
        return 'ok'
    return 'output' if (ROOT / prefix).parent.exists() else 'stale'


def scan_text(text, skip_comments=False):
    """Every distinct reference in one blob, with its verdict.

    `skip_comments` drops whole-line `#` comments, for source files. A comment
    naming a tree that is gone is usually the sentence explaining that it went
    -- config.py alone carries four of those, and they are the content.
    """
    if skip_comments:
        text = '\n'.join(l for l in text.split('\n')
                         if not l.lstrip().startswith('#'))
    pat = _pattern()
    seen = {}
    for m in pat.finditer(text):
        # The trailing slash goes too, so `data/curves/` and `data/curves` are
        # ONE reference rather than two. Keeping them apart cost the self-test
        # its first run: the case asked for `data/curves` and the extractor had
        # keyed `data/curves/`, which is the gate reporting a difference that
        # does not exist anywhere except in its own dictionary.
        ref = m.group(0).rstrip('.,);:`*').rstrip('/')
        if ref not in seen:
            seen[ref] = classify(ref)
    return seen


def _files():
    out = []
    for pattern in SCAN:
        for p in ROOT.glob(pattern):
            rel = p.relative_to(ROOT).as_posix()
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            if any(rel.startswith(s) for s in SKIP_FILES):
                continue
            out.append(p)
    return sorted(set(out))


def selftest():
    """The gate has to be able to say NO, and to say yes to the near misses.

    Every case is built from paths that really are or are not in this
    repository, so a case cannot pass by describing a world that does not
    exist -- which is what a fixture of invented paths would do.
    """
    cases = [
        # (reference, expected, why)
        ('docs/recoil/samples/m416__bare.jsonl', 'stale',
         'THE ONE THAT BIT: docs/recoil/ is gone, so nothing writes here'),
        ('docs/recoil/samples/*.jsonl', 'stale',
         '...and a glob does not excuse it'),
        ('calibration/artifacts/recoil/samples/<weapon>__<config>.jsonl', 'ok',
         'the real location, reached through two placeholders'),
        ('calibration/artifacts/recoil/samples', 'ok', 'the directory itself'),
        ('calibration/artifacts/tab/baseline.png', 'output',
         'a file a documented step CREATES; the tree it lands in exists'),
        ('data/curves', 'ok', 'where the fitted curves live now'),
        ('docs/game_quirks.md', 'ok',
         'docs/ still holds prose — the move took the ARTIFACTS, and a gate '
         'that flagged all of docs/ would be wrong about the whole file'),
        ('config.py', 'ok', 'a bare top-level file claims no directory'),
        ('control/calibration/harness/press/detector', 'ok',
         'FIVE DIRECTORIES in prose, not a path — every segment is top-level'),
        ('detector/press', 'ok', 'the two-segment version of the same shape'),
        ('detector/weapon.py', 'ok',
         'and the near miss it must NOT swallow: a real file under a real dir'),
        ('detector/nonexistent.py', 'output',
         '...whose sibling that does not exist is still inside a live tree'),
    ]
    # Printed from the table rather than written above it: the first version
    # said "3 of the 8" and was wrong within the hour, which is the same class
    # of stale claim this whole file exists to catch.
    must_bite = sum(1 for _, want, _ in cases if want != 'ok')
    print(f'self-test ({must_bite} of the {len(cases)} cases must NOT come '
          f'back ok, so "always ok" does not pass)')
    bad = []
    for ref, want, why in cases:
        got = classify(ref)
        print(f'  {"ok  " if got == want else "FAIL"}  {ref:<52} {got:<7} {why}')
        if got != want:
            bad.append(f'{ref}: wanted {want}, got {got}')
    # ...and the extractor has to FIND them in prose, not just judge them.
    found = scan_text('see `docs/recoil/samples/x.jsonl` and data/curves/ '
                      'plus a bare word docs and a url http://x/docs/y')
    for need in ('docs/recoil/samples/x.jsonl', 'data/curves'):
        if need not in found:
            bad.append(f'extractor missed {need}')
    print(f'  {"ok  " if len(found) >= 2 else "FAIL"}  extractor found '
          f'{len(found)} reference(s) in one line of prose')
    return bad


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    show_all = '--all' in sys.argv
    bad = selftest()
    if bad:
        print('\nthe checker itself is wrong:')
        for b in bad:
            print(f'  {b}')
        return 1

    stale, outputs, total = [], 0, 0
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        try:
            text = io.open(p, encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        for ref, verdict in scan_text(text, rel.endswith('.py')).items():
            total += 1
            if verdict == 'output':
                outputs += 1
                if show_all:
                    print(f'  output   {ref:<52} {rel}')
            elif verdict == 'stale' and not any(
                    ref == e or ref.startswith(e + '/') for e in EXEMPT):
                stale.append((rel, ref))

    print(f'\nchecked {total} path reference(s) across {len(_files())} '
          f'prose file(s)')
    print(f'  {outputs} name something that does not exist YET, inside a tree '
          f'that does (--all lists them)')
    if not stale:
        print('  0 point into a tree that is gone')
        return 0
    print(f'\n{len(stale)} pointer(s) into a directory that does not exist:')
    for rel, ref in sorted(stale):
        print(f'  {ref:<54} {rel}')
    print('\nEach one is a step someone will follow to a place that is not '
          'there. Repoint it, or add it to EXEMPT with the reason.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
