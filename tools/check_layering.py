"""Enforce the package layering. Parses imports; runs nothing.

The layers, and the one-line test for which one a module belongs to:

    detector/   frame -> meaning. Can it run on a PNG with no game and no
                hardware? Then it goes here.
    press/      the HAL. Knows devices, not the game.
    control/    closed loops: observe -> act -> verify. Needs to know what is
                happening in the game.

    calibration/ says WHAT to measure, HOW to compute it, and WHERE the
                artifacts go. Driving the game is control/'s job. Not a
                package boundary in the import graph, but the same idea, and
                rule 6 below is the part of it a machine can check.

Dependencies run one way: control -> detector, control -> press. The rules
below are what that sentence means in code, so it stays true without anyone
having to remember it.

    pixi run layering
"""
import ast
import pathlib
import sys

try:            # the ledger's reasons contain em-dashes; a cp936 console dies
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP = {'.pixi', '__pycache__', '.git', 'training_data', 'InGameScreenshot',
        'temp_debug', 'docs'}

# (name, applies-to predicate, forbidden import predicate, why)
RULES = [
    ('detector must not reach for the hardware',
     lambda p: p.parts[0] == 'detector',
     lambda m: m == 'press' or m.startswith('press.'),
     'detector is the offline layer: it has to run on a stored PNG, which is '
     'what makes the regression suite possible.'),

    ('press must not know about windows',
     lambda p: p.parts[0] == 'press',
     lambda m: m in ('win32gui', 'win32process', 'win32con', 'win32api'),
     'taking the foreground is a closed loop, so it is control/focus.py. '
     'press only knows devices.'),

    ('the entry point must not reach past control',
     lambda p: len(p.parts) == 1 and p.name != 'config.py',
     lambda m: m == 'press' or m.startswith('press.'),
     'robot.py assembles; driving hardware is control/.'),

    ('nothing below control may import control',
     lambda p: p.parts[0] in ('detector', 'press'),
     lambda m: m == 'control' or m.startswith('control.'),
     'the dependency is one-way. A detector that calls a driver cannot be '
     'tested on a file.'),

    ('detector must not depend on calibration',
     lambda p: p.parts[0] == 'detector',
     lambda m: m == 'calibration' or m.startswith('calibration.'),
     'calibration produces what detector consumes, never the reverse.'),

    ('harness must not drive the hardware itself',
     lambda p: p.parts[0] == 'harness',
     lambda m: (m == 'press' or m.startswith('press.')
                or m == 'detector' or m.startswith('detector.')),
     'harness/ decides WHETHER a measurement is usable and WHEN to stop. It '
     'reads numbers, not frames, and it drives nothing: reaching for a device '
     'or a detector here means a second measurement path beside the one it is '
     'supposed to be judging.'),

    ('calibration must not drive the hardware itself',
     lambda p: p.parts[0] == 'calibration' and p.as_posix() not in LEDGER,
     lambda m: m == 'press' or m.startswith('press.'),
     'calibration/ declares what to measure; control/ knows how to make the '
     'game do it. A press import here means a parallel driver, and every one '
     'of them so far has drifted from the control/ version it duplicates.'),

    ('only the spawner detector may recompute its anchor box',
     lambda p: p.as_posix() not in ICON_BOX_OWNERS,
     lambda m: m.startswith('config:SPAWNER_ICON_'),
     'detector.spawner_detector.ICON_BOX is that box, computed once and '
     'clamped. The four constants have no other honest use: every caller so '
     'far wanted the box, and the one that imported them instead '
     '(tools/probe_toggle_latency.py) reimplemented the arithmetic without '
     "anchor_box's max(0, ...) clamp. Import ICON_BOX."),
]

# Rule 7's owners. Same discipline as the rule 6 ledger: a reason that belongs
# to the CODE, not to the schedule.
ICON_BOX_OWNERS = {
    'detector/spawner_detector.py':
        'defines ICON_BOX. It has to read the constants to compute it.',
    'tools/test_frames.py':
        'is anchor_box()\'s test. It must be able to feed it the real inputs '
        'and pin the result to the measured literal (964, 2490, 311, 118); a '
        'test that imported the answer would assert nothing.',
}

# ════════════════════════════════════════════════════════════
# Rule 6's ledger — a RATCHET, not an excuse list
# ════════════════════════════════════════════════════════════
#
# Two kinds of entry, and keeping them in one structure is the point: the
# reason string is what separates them, so nobody can quietly file a piece of
# unfinished work under "by design".
#
# EXEMPT   the reason is the CODE's. It does not expire.
# DEBT     the reason is the SCHEDULE's. It is an open item in
#          docs/refactor_plan.md, and it is meant to leave this list.
#
# The ratchet is the second check in check_ledger(): a DEBT file that has
# STOPPED importing press is itself a failure. Without that, the list rots
# into a permanent amnesty — someone fixes a file, forgets the entry, and the
# rule silently stops covering it forever after. The plan warned about exactly
# this ("白名单必须写清理由再放行，否则它会长成「谁红了就加谁」"), so the
# defence is mechanical rather than a note asking people to be careful.

EXEMPT = {
    'calibration/sweep.py':
        'assembly shell. Rig owns the one Pointer and hands it to the '
        'control/ drivers — the same job robot.py does for the live loop. '
        'Somebody has to build the object; that somebody is allowed to.',
    'calibration/calibrate_k.py':
        'K is the mapping under test: "send N counts, the view turns how '
        'far". Every ViewDriver method closes the loop against the screen, so '
        'measuring K through one would be measuring the instrument with '
        'itself. mouse.move(0, sent) must stay bare. NOTE this covers only '
        'that measurement — the ADS toggle (:117) and the recentre (:273) in '
        'the same file have no such excuse and are still debt.',
    'calibration/state.py':
        'the device IS the subject. --pico reports whether the Pico is there '
        'and whether hand reporting is alive; routing that through a driver '
        'would report on the driver. It drives nothing, which is what rule 6 '
        'is actually about. The real hazard here was never the import — it '
        'was taking a single-tenant port from a running agent, and pico_state '
        'now refuses when other_agents() names one.',
}

DEBT = {}

LEDGER = {**EXEMPT, **DEBT}


def check_ledger(offenders):
    """The ratchet. -> [(path, message)]

    `offenders` is every calibration/ file that actually imports press right
    now, ledger entries included.
    """
    out = []
    for rel in sorted(DEBT):
        if rel in offenders:
            continue
        if not (ROOT / rel).exists():
            out.append((rel, 'listed as debt but the file is gone — delete '
                             'the entry'))
            continue
        out.append((rel, 'no longer imports press — the debt is PAID. Delete '
                         'its entry so the rule starts covering this file '
                         'again.'))
    return out


def imports_of(path):
    """(module, lineno) for every import, including function-local ones.

    `from X import a, b` also yields "X:a" and "X:b" so a rule can name a
    SYMBOL and not just a module. The colon keeps those entries away from the
    module-level predicates: "config:SPAWNER_ICON_W" is neither == 'config'
    nor startswith('config.').
    """
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError, UnicodeDecodeError):
        return []
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [(a.name, n.lineno) for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            out.append((n.module, n.lineno))
            out += [(f'{n.module}:{a.name}', n.lineno) for a in n.names]
    return out


def main():
    violations = []
    offenders = set()
    checked = 0
    for f in sorted(ROOT.rglob('*.py')):
        rel = f.relative_to(ROOT)
        if any(p in SKIP for p in rel.parts):
            continue
        checked += 1
        imps = imports_of(f)
        if rel.parts[0] == 'calibration' and any(
                m == 'press' or m.startswith('press.') for m, _ in imps):
            offenders.add(rel.as_posix())
        for name, applies, forbidden, why in RULES:
            if not applies(rel):
                continue
            for mod, lineno in imps:
                if forbidden(mod):
                    violations.append((name, rel.as_posix(), lineno, mod, why))

    stale = check_ledger(offenders)

    print(f'checked {checked} files against {len(RULES)} rules')
    if DEBT:
        # Printed on every green run ON PURPOSE. This list IS the remaining
        # work in docs/refactor_plan.md section 5, derived from the code
        # rather than from someone remembering to update a table -- the table
        # had a 5g marked done that never happened.
        paid = {r for r, _ in stale}
        print(f'\n{len(DEBT) - len(paid)} calibration file(s) still driving '
              f'hardware directly (rule 6 debt):')
        for rel in sorted(DEBT):
            if rel in paid:
                continue
            print(f'  {rel:34s} {DEBT[rel].split(" — ")[0]}')

    if not violations and not stale:
        print('\nlayering holds')
        return 0

    for name, rel, lineno, mod, why in violations:
        print(f'\n  {rel}:{lineno}  imports {mod}')
        print(f'    rule: {name}')
        print(f'    why:  {why}')
    for rel, msg in stale:
        print(f'\n  {rel}  (tools/check_layering.py ledger)')
        print(f'    {msg}')
    print(f'\n{len(violations) + len(stale)} violation(s)')
    return 1


if __name__ == '__main__':
    sys.exit(main())
