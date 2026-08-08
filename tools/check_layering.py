"""Enforce the package layering. Parses imports; runs nothing.

The layers, and the one-line test for which one a module belongs to:

    capture/    the INPUT HAL. Knows the screen and the keyboard, not the
                game. Grabbers, the frame ring, the key poller.
    press/      the OUTPUT HAL. Knows devices, not the game. THREE SEGMENTS
                since 2026-08-08, all of them failing the "knows the game"
                test the same way: the .py files (PC side), protocol/ (the
                wire contract, generated), and firmware/ (C, on the RP2350).
                protocol/ and firmware/ were top-level directories until they
                moved in; nothing about them was ever a layer of its own.
    detector/   frame -> meaning. Can it run on a PNG with no game and no
                hardware? Then it goes here.
    control/    closed loops: observe -> act -> verify. Needs to know what is
                happening in the game.

capture/ is the youngest (2026-08-08) and it was carved out of the other two
rather than added: cropper.py sat inside detector/ with 34 importers across
six directories, only 3 of them detectors, so half the repo reached into the
MEANING layer to get pixels. screen_capture.py and key_poller.py sat at the
repo root, which admits exactly two kinds of module -- the assembly root that
knows every layer (robot.py) and layer-less primitives that know none
(config.py, daemon_loop.py, whose docstring says so). Those two knew config
and cropper, so they were neither; they were on the root floor by default,
not by criterion.

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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _ledger import (Reason, CODE, INFERRED, audit, calls,  # noqa: E402
                     defines, has_cli_flag, imports,
                     mentions_literal)

try:            # the ledger's reasons contain em-dashes; a cp936 console dies
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
# 'build' is here for press/firmware/build -- a CMake tree of ~235 files.
# It holds zero .py TODAY, so rglob('*.py') happens to miss it; that is
# luck, not a rule, and pico-sdk generators can emit Python.
SKIP = {'.pixi', '__pycache__', '.git', 'docs', 'build'}

# (name, applies-to predicate, forbidden import predicate, why)
RULES = [
    ('detector must not reach for the hardware',
     lambda p: p.parts[0] == 'detector',
     lambda m: m == 'press' or m.startswith('press.'),
     'detector is the offline layer: it has to run on a stored PNG, which is '
     'what makes the regression suite possible.'),

    ('capture must not know about the game',
     lambda p: p.parts[0] == 'capture',
     lambda m: m.split('.')[0] in ('detector', 'control', 'calibration',
                                   'press', 'harness'),
     'capture/ is the input HAL, the mirror of press/: it knows the screen '
     'and the keyboard, not what is drawn on them. config and daemon_loop '
     'are allowed -- both are layer-less, and a grabber has to be told which '
     'rectangles to copy. A layer import here is the edge that put cropper.py '
     'inside detector/ in the first place, and once it exists nothing stops '
     'the input side from growing an opinion about what the pixels MEAN.'),

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

    ('protocol/ is generated constants and imports nothing',
     lambda p: p.parts[:2] == ('press', 'protocol'),
     lambda m: m != 'struct',
     'press/protocol/__init__.py is GENERATED from the protocol.toml beside '
     'it, and is '
     'the one thing press/ and the firmware both read. It may hold only what '
     'both ends must agree on, which is bytes on a wire -- never a helper '
     'that encodes one, because the firmware cannot call it and the two '
     'sides would diverge again in behaviour instead of in numbers. An '
     'import here is the first sign of that drift, and it also means the '
     'next `pixi run gen-protocol` will silently delete it.'),

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

    ('a calibration module has ONE name: calibration.X',
     lambda p: p.parts[0] in ('calibration', 'tools', 'harness'),
     lambda m: m.split(':')[0] in CALIBRATION_SIBLINGS,
     'a bare `from sweep import Rig` alongside `from calibration.sweep import '
     'Rig` loads the file TWICE, under two names, with two copies of every '
     'class and constant in it. Measured 2026-08-07: harness/adapter.py built '
     'a calibration.sweep.Rig and handed it to the recoil sweep, which '
     'held sweep.Rig — the two Rig classes were not the same class for '
     'the whole unattended night. Duck typing hides this until something asks '
     'an identity question, and then the symptom cannot name the cause. '
     'Write calibration.X; `import rpm_store` becomes `from calibration '
     'import rpm_store`, so the local name stays bound.'),
]

# Every module under calibration/, by bare stem — the spelling rule 10 forbids.
# DERIVED, not listed: a new calibration module is covered the moment it
# exists, which is the difference between a rule and a snapshot.
#
# ⚠ The sys.path.insert lines that make the bare spelling RESOLVE are still
# there, deliberately. Removing them is a separate and riskier change (a file
# run directly as a script leans on the root insert beside it), and while they
# stay, this rule is the only thing between the repo and a silent second copy
# of sweep.py. That is exactly what makes it a rule and not a cleanup.
CALIBRATION_SIBLINGS = frozenset(
    p.stem for p in (ROOT / 'calibration').glob('*.py')
    if p.stem != '__init__')

# Rule 7's owners. Same discipline as the rule 6 ledger: a reason that belongs
# to the CODE, not to the schedule.
ICON_BOX_OWNERS = {
    'detector/spawner_detector.py': Reason(
        'defines ICON_BOX. It has to read the constants to compute it.',
        CODE,
        lambda: defines('detector/spawner_detector.py', 'ICON_BOX')),
    'tools/test_frames.py': Reason(
        "is anchor_box()'s test. It must be able to feed it the real inputs "
        'and pin the result to the measured literal (964, 2490, 311, 118); a '
        'test that imported the answer would assert nothing.',
        CODE,
        # Both halves of the claim, because either one can go without the
        # other: it must still exercise anchor_box, and it must still PIN the
        # four numbers rather than import them. A test that switched to
        # importing ICON_BOX would keep passing and stop asserting.
        lambda: (calls('tools/test_frames.py', 'anchor_box')
                 and mentions_literal('tools/test_frames.py',
                                      964, 2490, 311, 118))),
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
    'calibration/sweep.py': Reason(
        'assembly shell. Rig owns the one Pointer and hands it to the '
        'control/ drivers — the same job robot.py does for the live loop. '
        'Somebody has to build the object; that somebody is allowed to.',
        CODE,
        # The reason is "it is the assembly shell", so the check asks whether
        # it still assembles: a Rig that takes the mouse and hands it to the
        # three control/ drivers. The day this stops being true the press
        # import stops being excusable, and nothing else in the repo would
        # notice — rule 6 only ever asked whether the import is there.
        lambda: (defines('calibration/sweep.py', 'Rig')
                 and calls('calibration/sweep.py', 'get_mouse')
                 and imports('calibration/sweep.py', 'control.aim'))),

    'calibration/calibrate_k.py': Reason(
        'K is the mapping under test: "send N counts, the view turns how '
        'far". Every ViewDriver method closes the loop against the screen, so '
        'measuring K through one would be measuring the instrument with '
        'itself. mouse.move(0, sent) must stay bare.',
        CODE,
        # Two halves, and the second is the one that rots: it must still send
        # bare moves, AND it must still not have reached for ViewDriver. A
        # file that starts importing control.aim has given up the exact thing
        # this exemption is buying.
        lambda: (calls('calibration/calibrate_k.py', 'move')
                 and not imports('calibration/calibrate_k.py', 'control.aim'))),

    'calibration/state.py': Reason(
        'the device IS the subject. --pico reports whether the Pico is there '
        'and whether hand reporting is alive; routing that through a driver '
        'would report on the driver. It drives nothing.',
        CODE,
        # `--pico` disappearing is the falsifier: without it the file is not
        # reporting on the device any more, and a press import in something
        # that merely reads state is what rule 6 exists to catch.
        lambda: has_cli_flag('calibration/state.py', '--pico')),
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


# ════════════════════════════════════════════════════════════
# Rule 9's ledger — same two kinds, same ratchet, as rule 6's above
# ════════════════════════════════════════════════════════════
#
# READY_EXEMPT  the reason is the CODE's. It does not expire.
# READY_DEBT    the reason is the SCHEDULE's. It is meant to leave this list,
#               and check_ready_ledger() fails the run when an entry has been
#               paid but not deleted — otherwise the list rots into a
#               permanent amnesty exactly as rule 6's would.
#
# The debt was seeded, not invented: 31 live probes predate control/session.py
# and every one of them opens with a bare ensure_focus. Retrofitting them is
# real work with a live game attached, and doing it in a hurry is how a probe
# starts requiring a match it is supposed to be measuring the way out of. What
# matters is that NEW scripts are covered from the first line, which they are.

READY_EXEMPT = {
    'tools/focus_trace.py': Reason(
        'the subject under test IS taking the foreground. Routing it through '
        'the gate that takes it would be reporting the driver to itself.',
        CODE,
        # It reads the foreground machinery and drives nothing else. The
        # falsifier is it growing a second control import: at that point it
        # has stopped being an instrument pointed at focus and become a script
        # that drives the game, which is what rule 9 is for.
        lambda: (imports('tools/focus_trace.py', 'control.focus')
                 and not any(imports('tools/focus_trace.py', m) for m in (
                     'control.lobby', 'control.spawner', 'control.inventory',
                     'control.map', 'control.gun', 'control.fire',
                     'control.aim', 'control.session')))),

    'tools/probe_lobby_transition.py': Reason(
        'drives the lobby<->match transition on purpose, so it cannot open by '
        'requiring a match — that is the thing it is measuring.',
        CODE,
        # It measures the transition by classifying screens while sending F
        # itself. Requiring control.session would mean requiring the end state
        # it exists to time. Falsifier: it stops reading lobby_detector, i.e.
        # stops measuring the transition.
        lambda: (imports('tools/probe_lobby_transition.py',
                         'detector.lobby_detector')
                 and not imports('tools/probe_lobby_transition.py',
                                 'control.session'))),

    'tools/snap_on_key.py': Reason(
        'a shutter. It photographs whatever is on screen ON PURPOSE, '
        'including the lobby and the menus, and putting the game into a known '
        'state first would destroy the only shots that need taking.',
        CODE,
        # "It photographs whatever is on screen" is checkable as "it drives
        # nothing": no control import that could change what is on screen.
        # focus is allowed — a shutter still has to be pointed at the game.
        lambda: not any(imports('tools/snap_on_key.py', m) for m in (
            'control.lobby', 'control.spawner', 'control.inventory',
            'control.map', 'control.gun', 'control.fire', 'control.session'))),
}

READY_DEBT = {
    # Seeded 2026-08-07 when rule 9 widened past tools/. Same ratchet: these
    # are scheduled, not excused, and check_ready_ledger() fails the run when
    # one is paid and left on the list.
    # The four seeded on 2026-08-07 were paid on 2026-08-07 and are gone from
    # this table, which is the ratchet working rather than a note about it:
    # posture_axis, scan_fits and weapon_axis each had two of the five legs
    # open-coded, and harness/adapter.py's open_rig -- the door every
    # unattended night goes through -- had focus, a match and the spawner
    # probe but never Tab and never the 200m lane. That last one had ALREADY
    # been fixed on the re-entry path and still shipped broken on the START
    # path, because session.ensure() returns without calling enter() when the
    # game is already in a match.

    # 27 entries left this table on 2026-08-06, when every probe that opens by
    # taking the foreground was switched to ensure_ready -- the same change
    # that put goto_range('200m') in front of every training-range run.
    #
    # One probe did NOT move here: it was in READY_EXEMPT, and a batch rewrite
    # changed it anyway before the exemption was read. The ledger is the thing
    # that knew, which is the argument for it existing. (That probe was deleted
    # 2026-08-08 along with its entry — a ledger row naming a file that is gone
    # is itself a red, which is how this paragraph got updated.)
    # tools/drive_screen.py left on 2026-08-07, and its entry is worth one
    # more paragraph than the others because THE REASON WAS FALSE, not merely
    # stale. It read: "its ensure_focus is inside drive(), a LIBRARY function
    # that calibration/scan_compat.py calls once per weapon -- 30 times a run.
    # ensure_ready there would teleport 30 times."
    #
    # scan_compat imports SCREENS, not drive(). `grep 'drive('` finds one
    # caller in the repo: drive_screen's own main(). And scan_compat has
    # opened with ensure_ready since 2026-08-06, so the 30 teleports could not
    # have happened even if it did call it. Every clause was checkable in one
    # grep, and the exemption stood for a year on none of them being checked.
    #
    # AN ENTRY IN EITHER TABLE IS A CLAIM ABOUT THE CODE. The ratchet already
    # tests the claim "this file still offends"; nothing tests the claim in
    # the prose, and this is the argument for keeping these reasons short and
    # falsifiable rather than persuasive.
}

READY_LEDGER = {**READY_EXEMPT, **READY_DEBT}


def check_ready_ledger(offenders):
    """The rule 9 ratchet. -> [(path, message)]

    `offenders` is every tools/ file that would be flagged right now, ledger
    entries included.
    """
    out = []
    for rel in sorted(READY_DEBT):
        if rel in offenders:
            continue
        if not (ROOT / rel).exists():
            out.append((rel, 'listed as ensure_ready debt but the file is '
                             'gone — delete the entry'))
        else:
            out.append((rel, 'now opens with ensure_ready (or no longer takes '
                             'the foreground) — the debt is PAID. Delete its '
                             'entry so rule 9 starts covering this file '
                             'again.'))
    return out


def check_ready(path, rel, imps):
    """A tools/ script that takes the foreground must open with ensure_ready().

    THE PREDICATE IS `ensure_focus`, NOT "imports control". Importing control
    is not driving the game — half of tools/ is offline regressions that read
    a constant or replay a stored PNG, and an earlier draft of this rule
    flagged 48 files, most of which never touch the game. Taking the
    FOREGROUND is the honest declaration of intent: nothing does that except
    to drive, and the moment a script has it, every driver it calls will
    report success against whatever screen happens to be up.

    Which is the failure. It does not look like one: focus is granted, the
    state machines run, the readbacks come back empty, and the script reports
    the game's behaviour instead of its own. probe_pitch_range.py drove three
    postures into the LOBBY SCREEN that way and printed 'posture unreadable'
    three times — a true statement about a screen with no posture icon,
    because it had no HUD at all.

    ensure_ready() calls ensure_focus() itself, so the fix is a substitution,
    not an addition. See control/session.py for the four checks and for why
    skipping one to turn a red run green rebuilds exactly this.
    """
    # ⚠ WIDENED 2026-08-07 from tools/ to every layer that opens a script.
    # The narrow version was seeded from a real audit of tools/, and then the
    # scope stuck: calibration/scan_fits.py and the axis scripts
    # and harness/adapter.py all take the foreground with a bare ensure_focus
    # and none of them was ever flagged, while calibration/CLAUDE.md said "这
    # 一层的六个入口开场统一调 ensure_ready" -- a count nobody was checking.
    # harness/adapter.open_rig is the expensive one: it opens every unattended
    # night, and it never verified Tab or walked to the 200m lane.
    if rel.parts[0] not in ('tools', 'calibration', 'harness'):
        return None
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError, UnicodeDecodeError):
        return None
    called = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(
                f, 'id', None)
            if name:
                called.add(name)
    if 'ensure_ready' in called or 'ensure_focus' not in called:
        return None
    return ('takes the game foreground (ensure_focus) but never calls '
            'control.session.ensure_ready(). Focus is not playability: the '
            'window title matches in the lobby, on the loading screen and in '
            'the ESC menu, all of which swallow input while every driver '
            'reports success. ensure_ready() wraps ensure_focus, so this is a '
            'substitution. Or add a READY_EXEMPT entry with a reason about '
            'the code.')


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


def check_eager_pointer(path, rel):
    """A control/ class must not build a Pointer in __init__. -> [(sym, why)]

    THE RULE HAS BEEN IN control/CLAUDE.md FOR MONTHS -- "`Pointer` 是懒构造的
    ——只读状态的调用方不会去占串口。别在构造函数里提前建它" -- and two of the
    four driver classes broke it, which is the ratio that says prose is not
    the right container for a rule.

    What it costs is not tidiness. Constructing a Pointer calls
    press.pico_mouse.get_mouse(), which opens COM10. Measured 2026-08-07:

        >>> sc = SpawnerControl(verbose=False)
        [pico] connected on COM10

    `sc.plan()` is documented as 纯离线 and took the port to answer. So did
    every ensure_ready(), which builds a SpawnerControl only to ask whether a
    panel is shut. Several agents share one Pico and one game window.

    ⚠ Syntactic, and deliberately blunt: any `Pointer(...)` call anywhere in
    an __init__ under control/. There is no legitimate one -- control.driver
    .Driver has the lazy property, and a class that needs the device in its
    constructor is a class that has decided for its callers.
    """
    out = []
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError, UnicodeDecodeError):
        return out
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for m in cls.body:
            if not (isinstance(m, ast.FunctionDef) and m.name == '__init__'):
                continue
            for n in ast.walk(m):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)                         and n.func.id == 'Pointer':
                    out.append((f'{cls.name}.__init__', n.lineno))
    return out


def main():
    violations = []
    offenders = set()
    unready = []
    not_ready = set()
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
        if rel.parts[0] == 'control':
            for sym, lineno in check_eager_pointer(f, rel):
                violations.append((
                    'a control/ class must not build a Pointer in __init__',
                    rel.as_posix(), lineno, f'Pointer() in {sym}',
                    'constructing a Pointer opens COM10, so merely '
                    'CONSTRUCTING the driver takes the shared serial port -- '
                    'sc.plan() is documented 纯离线 and did exactly that. '
                    'Subclass control.driver.Driver and use its lazy '
                    '`pointer` property.'))
        why = check_ready(f, rel, imps)
        if why:
            not_ready.add(rel.as_posix())
            if rel.as_posix() not in READY_LEDGER:
                unready.append((rel.as_posix(), why))

    stale = check_ledger(offenders) + check_ready_ledger(not_ready)

    print(f'checked {checked} files against {len(RULES) + 2} rules')
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
    if READY_DEBT:
        # Same reason rule 6's list is printed on every green run: this IS the
        # remaining work, derived from the code rather than from a table
        # someone has to remember to update.
        paid = {r for r, _ in stale}
        left = [r for r in sorted(READY_DEBT) if r not in paid]
        print(f'\n{len(left)} script(s) still opening with a bare '
              f'ensure_focus (rule 9 debt) — swap in '
              f'control.session.ensure_ready')

    # ⚠ THE OTHER HALF OF EVERY LEDGER. The three ratchets above test "is
    # this file still offending"; this tests "is the stated REASON still
    # true", which nothing did until 2026-08-08 — and a reason nothing checks
    # is how tools/drive_screen.py sat on the debt list for a year behind
    # three clauses that were each one grep from being disproven.
    led_lines, led_bad = audit([('rule6 EXEMPT', EXEMPT),
                                ('rule6 DEBT', DEBT),
                                ('rule9 EXEMPT', READY_EXEMPT),
                                ('rule9 DEBT', READY_DEBT),
                                ('rule7 owners', ICON_BOX_OWNERS)])
    for line in led_lines:
        print(line)

    if not violations and not stale and not unready and not led_bad:
        print('\nlayering holds')
        return 0

    for name, rel, lineno, mod, why in violations:
        print(f'\n  {rel}:{lineno}  imports {mod}')
        print(f'    rule: {name}')
        print(f'    why:  {why}')
    for rel, msg in stale:
        print(f'\n  {rel}  (tools/check_layering.py ledger)')
        print(f'    {msg}')
    for rel, msg in unready:
        print(f'\n  {rel}')
        print('    rule: a tools/ script that drives the game opens with '
              'ensure_ready()')
        print(f'    why:  {msg}')
    for key, why in led_bad:
        print(f'\n  {key}  (ledger reason)')
        print(f'    {why}')
        print('    A ledger entry is a CLAIM ABOUT THE CODE. Either the code '
              'moved and the entry should go, or the reason was never true.')
    n = len(violations) + len(stale) + len(unready) + len(led_bad)
    print(f'\n{n} violation(s)')
    return 1


if __name__ == '__main__':
    sys.exit(main())
