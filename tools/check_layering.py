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


# ── Rule 15: a loop that holds the screen must be escapable ──────────────
#
# ⚠ THE OPERATOR HAS TO BE ABLE TO TAKE THE MACHINE BACK, and on 2026-08-08
# they could not. tools/probe_delivery_path.py --hold-sweep sat in
#
#     prev = None
#     while prev is None:
#         _t, f = grabber.grab_timed()
#         prev = rig.tracker.slice_frame(f) if f is not None else None
#
# with the mouse button about to go down, driving the cursor, for eight
# minutes until it was killed from another session. slice_frame returns None
# whenever the tracker cannot place its patch -- a blank sky is enough -- so
# the exit condition is a thing the WORLD has to provide, and nothing in the
# loop had an opinion about how long to wait for it.
#
# The escape hatch already exists and that file simply never called it:
# control.focus.focus_keeper().ok(tag) returns False once a human has taken
# the foreground MAX_REGAINS times, and its own docstring says why -- "either
# something is contending, or a human is trying to get out. Both mean stop."
# A run that never asks cannot be stopped by asking.
#
# ⚠ SCOPE IS THE SCRIPT LAYERS ONLY. control/ has `while True` loops by the
# dozen and they are fine: every one is inside a driver that carries its own
# timeout, and _await_frame already consults focus_keeper. The rule is about
# the layer that OWNS a run -- the thing that took the foreground and will
# hold it for minutes.
_CLOCK = {'perf_counter', 'monotonic', 'time', 'sleep', 'deadline', 'timeout',
          'elapsed', 'budget', 'until', 'end', 't0', 'tries', 'attempts'}

# ⚠ NINE LOOPS AT THE MOMENT THE RULE WAS WRITTEN, five of them the identical
# `while prev is None` frame-grab. Same ratchet as rules 6 and 9: the reason
# belongs to SCHEDULE, so an entry that stops offending must be deleted or
# this reports it. New files are covered from their first line.
# ⚠ INFERRED, NOT CODE, AND THE DISTINCTION IS _ledger.py's OWN. A CODE
# reason must carry a check, and a check tests the REASON rather than the
# rule. Here the reason IS the rule -- "this file still has a loop nothing can
# interrupt" is precisely what check_escape_hatch already answers on every
# build. Attaching a check would re-run the rule and make the entry look
# doubly verified when it is verified exactly once.
ESCAPE_DEBT = {
    'calibration/capture_ads.py':
        Reason('`while len(out) < n` collecting frames — bound it or ask '
               'focus_keeper', INFERRED),
    'calibration/collect_templates.py':
        Reason('`while remaining` over the spawn list — one pass that spawns '
               'nothing must end it', INFERRED),
    'tools/fit_pitch_level.py':
        Reason('`while rises < BAND_MAX` climbing the pitch band', INFERRED),
    'tools/probe_additivity.py':
        Reason('`while prev is None` frame grab', INFERRED),
    'tools/probe_human_sign.py':
        Reason('`while prev is None` frame grab', INFERRED),
    'tools/probe_input_latency.py':
        Reason('`while prev is None` frame grab', INFERRED),
}


def _names_in(node):
    """Every called name, attribute and bare name under `node`."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            nm = _call_name(n)
            if nm:
                out.add(nm)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
    return out


def check_escape_hatch(path, rel):
    """while-loops in a foreground-holding script must be escapable.

    -> [(lineno, test_source)]

    A loop passes if anything in it -- test or body -- mentions the clock or
    calls focus_keeper. Deliberately generous: the point is not to audit how
    the loop ends, it is to catch loops that have NO opinion about ending.
    `while prev is None` mentions neither, and that is exactly the shape that
    held the machine.

    ⚠ THE BODY COUNTS, NOT JUST THE TEST, and the first draft checked only
    the test. That flagged every `while True:` in control/ whose body does
    `if elapsed > timeout: return` -- 28 findings, almost all of them correct
    code. Reading the body drops it to 9, and all 9 are real.

    ⚠ A `break` IS NOT ACCEPTED as an escape. All nine offenders have no
    break at all, so nothing is lost today, and accepting one would take any
    `if x: break` as proof of termination when x is exactly the world-provided
    condition in question.
    """
    if rel.parts[0] not in ('tools', 'calibration', 'harness'):
        return []
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError, UnicodeDecodeError):
        return []
    # Taking the foreground is the honest declaration that this holds the
    # screen -- the same predicate rule 9 uses, for the same reason.
    if not ({'ensure_ready', 'ensure_focus'} & _names_in(tree)):
        return []
    out = []
    for w in [n for n in ast.walk(tree) if isinstance(n, ast.While)]:
        scope = _names_in(w.test)
        for st in w.body:
            scope |= _names_in(st)
        if _CLOCK & scope or 'focus_keeper' in scope:
            continue
        out.append((w.lineno, ast.unparse(w.test)[:48]))
    return out


ESCAPE_WHY = (
    'holds the foreground and loops with no clock and no focus check, so a '
    'human cannot take the machine back by clicking away — the loop exits '
    'only when the WORLD provides its condition, and nothing bounds the wait. '
    'This is what sat on the cursor for eight minutes on 2026-08-08. Fix: '
    '`if not focus_keeper().ok("<tag>"): break` in the loop (focus_keeper '
    'returns False once a human has taken the foreground MAX_REGAINS times), '
    'or give the loop a deadline. Or add an ESCAPE_DEBT entry with a date.')

# ⚠ SIX CASES, THREE OF WHICH MUST BITE. Case 4 is the shape that actually
# held the machine; 5 and 6 are the two false-positive families the drafts
# produced (a body-level clock, and a library loop in a file that never takes
# the foreground).
_ESCAPE_CASES = [
    ('the loop that held the machine', True, '''
from control.session import ensure_ready
def probe(grabber, rig):
    ensure_ready(label='x')
    prev = None
    while prev is None:
        _t, f = grabber.grab_timed()
        prev = rig.tracker.slice_frame(f)
'''),
    ('bare while True with no clock', True, '''
from control.session import ensure_ready
def probe():
    ensure_ready(label='x')
    while True:
        step()
'''),
    ('a counter the world has to satisfy', True, '''
from control.session import ensure_ready
def probe():
    ensure_ready(label='x')
    rises = 0
    while rises < 12:
        rises += look()
'''),
    ('asks focus_keeper', False, '''
from control.session import ensure_ready
from control.focus import focus_keeper
def probe(grabber):
    ensure_ready(label='x')
    prev = None
    while prev is None:
        if not focus_keeper().ok('grab'):
            break
        prev = grabber.grab()
'''),
    ('deadline in the BODY, not the test', False, '''
from control.session import ensure_ready
import time
def probe(grabber):
    ensure_ready(label='x')
    end = time.perf_counter() + 5
    while True:
        if time.perf_counter() > end:
            break
        grabber.grab()
'''),
    ('never takes the foreground', False, '''
def analyse(rows):
    prev = None
    while prev is None:
        prev = rows.pop()
'''),
]


def selftest_escape():
    """-> (passed, total, bitten). Rule 15 proving it can be both."""
    import tempfile
    passed = bitten = 0
    for label, must_bite, src in _ESCAPE_CASES:
        with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False,
                                         encoding='utf-8') as fh:
            fh.write(src)
            tmp = pathlib.Path(fh.name)
        try:
            got = bool(check_escape_hatch(tmp, pathlib.Path('tools/x.py')))
        finally:
            tmp.unlink(missing_ok=True)
        ok = got == must_bite
        passed += ok
        bitten += must_bite
        if not ok:
            print(f'  SELFTEST FAIL  rule 15 / {label}: expected '
                  f'{"a bite" if must_bite else "silence"}, got the other')
    return passed, len(_ESCAPE_CASES), bitten


def check_escape_ledger(offenders):
    """The rule 15 ratchet. -> [(path, message)]"""
    out = []
    for rel in sorted(ESCAPE_DEBT):
        if rel in offenders:
            continue
        if not (ROOT / rel).exists():
            out.append((rel, 'listed as escape-hatch debt but the file is '
                             'gone — delete the entry'))
        else:
            out.append((rel, 'every loop in it can now be escaped — the debt '
                             'is PAID. Delete its ESCAPE_DEBT entry so rule 15 '
                             'starts covering this file again.'))
    return out


# ── Rule 14: one trip into the backpack ──────────────────────────────────
#
# ⚠ THE RULE WAS ALREADY WRITTEN DOWN, IN InventoryControl.tab_up's OWN
# DOCSTRING, and it was broken anyway:
#
#     Hold ONE across the whole flow; do not wrap each read in its own.
#
# calibration/collect_timed.py's read_config() and read_sight() each wrapped
# their own. Between the two entries: TWELVE calls, every one of them print /
# sorted / dict.get. The character walked out of the backpack and straight back
# in to compare two dicts.
#
# And the cost was never just time. Two loadout() calls are TWO OBSERVATIONS,
# so the config and the optic were read a Tab-toggle apart and then stored as
# one description of one gun -- the repository's second cross-layer law with
# the two readings a second apart instead of a day.
#
# Same shape, same file family, once before: ensure_kit's post-restock hold()
# produced blocks of `False -> True -> True -> False -> True`, 49 of them, five
# Tab presses to accomplish one keypress. Measured over the shared journal at
# the time: 975 of 1836 real presses (58%) sat inside blocks that ended in the
# state they began in. TWO counterexamples to one sentence of prose is the
# ratio that says prose is not the container.

# Calls that cannot drive anything: builtins and container methods. The
# predicate is deliberately INVERTED -- anything not on this list makes the
# rule shut up, so a second trip is only reported when NOTHING happened in
# between. A list of "things that need the backpack shut" would have to be
# maintained and would rot; this one is closed by the language.
_PURE = set('''print len sorted list dict set tuple str int float bool round
abs min max sum any all enumerate zip range reversed isinstance getattr setattr
hasattr format repr type id join get items keys values append extend pop split
rsplit strip lstrip rstrip partition replace startswith endswith lower upper
add update copy index count remove insert sort clear discard setdefault
fromkeys ljust rjust zfill title capitalize splitlines encode decode Counter
defaultdict deepcopy dumps loads'''.split())


def _call_name(c):
    f = c.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)


def _own_calls(st):
    """(lineno, name, node) for calls in THIS statement's own expressions.

    Stops at any nested STATEMENT, which is what makes the whole rule work
    with no line arithmetic: a `with ac.tab_up():` yields its own tab_up()
    call (the context expression) and NOT the body, because the body is
    statements. Same for `if`, `for`, `try` -- the test is this statement's,
    the block is somebody else's.

    ⚠ THE LINE-RANGE VERSION OF THIS WAS WRONG IN BOTH DIRECTIONS and its own
    self-test caught the first: reading the inside of a held-open block as
    "between the trips" hid two back-to-back `with tab_up():`. The second it
    could not catch, and the repository did: the rule fired on a `tab_up()`
    inside `if a.kit:` and a `read_loadout()` inside `if lo is None:` --
    MUTUALLY EXCLUSIVE branches that can never both run. Comparing within one
    block list is the fix for both, and it needs no control-flow analysis:
    different branches are different block lists by construction.
    """
    out = []

    def walk(n):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, ast.stmt):
                continue                       # another block, scanned on its own
            if isinstance(c, ast.Call):
                nm = _call_name(c)
                if nm:
                    out.append((c.lineno, nm, c))
            walk(c)
    walk(st)
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def _blocks_of(st):
    """The nested statement blocks of one statement, each scanned fresh."""
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return []                              # its own function, its own scan
    out = []
    for field in ('body', 'orelse', 'finalbody'):
        blk = getattr(st, field, None)
        if blk and all(isinstance(x, ast.stmt) for x in blk):
            out.append(blk)
    for h in getattr(st, 'handlers', []) or []:
        out.append(h.body)
    return out


def _tab_event(nm, call):
    """'open' | 'close' | None for one direct Tab operation.

    ⚠ `ensure_tab(False)` IS NOT AN ENTRY, and conflating the two was the first
    draft's false positive: control/stock.py's restock() reads the shelf and
    then SHUTS Tab to reach the spawner, which is the opposite of churn.
    A non-literal argument (`ensure_tab(want)`) is unreadable, so it answers
    None and the rule falls silent rather than guessing.
    """
    if nm == 'tab_up':
        return 'open'
    if nm != 'ensure_tab' or not call.args:
        return None
    a = call.args[0]
    return ('open' if a.value else 'close') if isinstance(a, ast.Constant) \
        else None


def _first_tab_event(fn):
    """'open' | 'close' | None — what this function needs the backpack to be.

    Walks everything, blocks included: the question is what the function does
    at all, not where in it.
    """
    best = None
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        e = _tab_event(_call_name(n), n)
        if e and (best is None or n.lineno < best[0]):
            best = (n.lineno, e)
    return best[1] if best else None


def check_tab_churn(path, rel):
    """Two backpack ENTRIES in one body with only pure computation between.

    -> [(fn, line_a, name_a, line_b, name_b)]

    ⚠ WHAT SEPARATES AN ENTRY FROM A TRIP OUTSIDE IS THE FIRST TAB EVENT IN
    THE CALLEE, and getting that wrong was the other false positive. hold()
    presses 1/2, which are SWALLOWED while the inventory is up, so it opens
    with `ensure_tab(False)` -- it needs the backpack SHUT. ensure_kit calling
    hold() and then reopening is the correct shape, not churn. read_config()
    opened with tab_up(): it needs the backpack UP. So the callee's own first
    event classifies it, and no hand-maintained list of "outside" work exists
    to go stale.

    Resolution is WITHIN ONE FILE and one hop, on purpose. A transitive
    closure over function NAMES was tried first and collapsed: `__exit__` ->
    `close` -> `ensure_tab` painted 711 names, `main` and `read` and `get`
    among them, because names are not identities across modules. One file, one
    hop, is where a name IS an identity -- and it is exactly the reach the
    failure had (read_config and read_sight are siblings, called from main()).
    """
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError, UnicodeDecodeError):
        return []
    needs_open, needs_shut = set(), set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        e = _first_tab_event(fn)
        if e == 'open':
            needs_open.add(fn.name)
        elif e == 'close':
            needs_shut.add(fn.name)
    out = []

    def scan(stmts, fname):
        """One block list, left to right. Nested blocks start over."""
        prev = None
        for st in stmts:
            for ln, nm, c in _own_calls(st):
                e = _tab_event(nm, c)
                kind = e or ('open' if nm in needs_open else
                             'pure' if (nm in _PURE and nm not in needs_shut)
                             else 'other')
                if kind == 'open':
                    if prev is not None:
                        out.append((fname, prev[0], prev[1], ln, nm))
                    prev = (ln, nm)
                elif kind != 'pure':
                    prev = None      # real work happened; a new trip is earned
            for blk in _blocks_of(st):
                scan(blk, fname)

    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        scan(fn.body, fn.name)
    return out


TAB_CHURN_WHY = (
    'enters the Tab screen twice with nothing but pure computation in '
    'between — the character walks out of the backpack and straight back in. '
    'InventoryControl.tab_up() is as-found and nests for free, so hold ONE '
    'across the whole flow and pass the reading down. Beyond the keypresses, '
    'two reads a Tab-toggle apart are TWO OBSERVATIONS being recorded as one '
    'description of one object, which is what calibration/collect_timed.py '
    'read_config/read_sight cost before they were merged.')

# ⚠ EIGHT CASES, FOUR OF WHICH MUST BITE. A rule that only checks "it fired
# when it should" passes just as well when the answer is always yes, and
# always-yes here would flag every legitimate second trip in the repository.
# Case 5 is the shape that was actually shipped (read_config / read_sight);
# cases 6-8 are the two false positives the first two drafts produced, kept as
# cases so the discrimination cannot be lost again.
_CHURN_CASES = [
    ('one trip, two reads inside', False, '''
def main(ac):
    with ac.tab_up():
        lo = ac.loadout()
    print(lo)
'''),
    ('a trip per loop iteration', False, '''
def main(ac, xs):
    for x in xs:
        with ac.tab_up():
            ac.loadout()
'''),
    ('two entries, real work between', False, '''
def main(ac, sc):
    with ac.tab_up():
        ac.loadout()
    sc.give_many(['m416'])
    with ac.tab_up():
        ac.loadout()
'''),
    ('entry, explicit close, entry', False, '''
def main(ac):
    with ac.tab_up():
        ac.loadout()
    ac.ensure_tab(False)
    ac.ensure_tab(True)
'''),
    ('read_config/read_sight, as shipped', True, '''
def read_config(ac):
    with ac.tab_up():
        return ac.loadout()
def read_sight(ac):
    with ac.tab_up():
        return ac.loadout()
def main(ac):
    config = read_config(ac)
    print(f'fitted: {config}')
    worn = read_sight(ac)
'''),
    ('two tab_up back to back', True, '''
def main(ac):
    with ac.tab_up():
        ac.loadout()
    with ac.tab_up():
        ac.loadout()
'''),
    ('hold() needs Tab SHUT, so reopening is earned', False, '''
def hold(self, gun):
    self.ensure_tab(False)
    self.press_key()
    self.ensure_tab(True)
def ensure_kit(self):
    with self.tab_up():
        self.hold(1)
        self.ensure_tab(True)
'''),
    ('ensure_tab(False) is not an entry', False, '''
def restock(ac):
    with ac.tab_up():
        ac.read_stock()
    ac.ensure_tab(False)
'''),
    # ⚠ THE REPOSITORY TAUGHT THIS ONE, not the author. The line-ordered draft
    # fired on collect_timed's `if a.kit:` / `if lo is None:` pair -- two
    # entries that can never both run. Blocks, not lines.
    ('mutually exclusive branches', False, '''
def main(a, ac):
    lo = None
    if a.kit:
        with ac.tab_up():
            lo = ac.loadout()
    if lo is None:
        lo = read_loadout()
'''),
    # ...and the negative of THAT: same two entries, no guard, one after the
    # other. If the block rule silenced this too it would be silencing
    # everything.
    ('same two entries, unguarded', True, '''
def main(ac):
    with ac.tab_up():
        lo = ac.loadout()
    lo2 = read_loadout()
def read_loadout():
    with InventoryControl() as ac:
        with ac.tab_up():
            return ac.loadout()
'''),
]


def selftest_tab_churn():
    """-> (passed, total, bitten). Rule 14 proving it can be both."""
    import tempfile
    passed = bitten = 0
    for label, must_bite, src in _CHURN_CASES:
        with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False,
                                         encoding='utf-8') as fh:
            fh.write(src)
            tmp = pathlib.Path(fh.name)
        try:
            got = bool(check_tab_churn(tmp, tmp))
        finally:
            tmp.unlink(missing_ok=True)
        ok = got == must_bite
        passed += ok
        bitten += must_bite
        if not ok:
            print(f'  SELFTEST FAIL  {label}: expected '
                  f'{"a bite" if must_bite else "silence"}, got the other')
    return passed, len(_CHURN_CASES), bitten


def main():
    violations = []
    offenders = set()
    unready = []
    not_ready = set()
    no_escape = set()
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
        # ⚠ EVERY LAYER, not just calibration/. The shipped instance was in
        # calibration/, the near-miss before it was in control/, and a rule
        # scoped to where the last one happened is the shape check_ready spent
        # a year being (tools/ only, while three other layers offended).
        for fn, la, na, lb, nb in check_tab_churn(f, rel):
            violations.append((
                'one trip into the backpack, not one per read',
                rel.as_posix(), lb,
                f'{na}() at line {la}, then {nb}() in {fn}()',
                TAB_CHURN_WHY))

        loose = check_escape_hatch(f, rel)
        if loose:
            no_escape.add(rel.as_posix())
            if rel.as_posix() not in ESCAPE_DEBT:
                for lineno, test in loose:
                    violations.append((
                        'a loop that holds the screen must be escapable',
                        rel.as_posix(), lineno, f'while {test}', ESCAPE_WHY))

        why = check_ready(f, rel, imps)
        if why:
            not_ready.add(rel.as_posix())
            if rel.as_posix() not in READY_LEDGER:
                unready.append((rel.as_posix(), why))

    stale = (check_ledger(offenders) + check_ready_ledger(not_ready)
             + check_escape_ledger(no_escape))

    print(f'checked {checked} files against {len(RULES) + 4} rules')
    ep, et, eb = selftest_escape()
    print(f'  {"✓" if ep == et else "✗"} rule 15 self-test {ep}/{et} '
          f'({eb} of them must BITE, so "never report" does not pass)')
    if ep != et:
        violations.append(('rule 15 self-test', 'tools/check_layering.py', 0,
                           'the escape-hatch rule itself',
                           f'{et - ep} of {et} cases answered wrong.'))
    # ⚠ PRINTED EVEN WHEN GREEN, because rule 14 has no ledger and therefore
    # no other evidence that it is still able to fire. tools/CLAUDE.md's line:
    # a gate nobody can say what would turn red has not been verified.
    cp, ct, cb = selftest_tab_churn()
    print(f'  {"✓" if cp == ct else "✗"} rule 14 self-test {cp}/{ct} '
          f'({cb} of them must BITE, so "never report" does not pass)')
    if cp != ct:
        violations.append(('rule 14 self-test', 'tools/check_layering.py', 0,
                           'the churn rule itself',
                           f'{ct - cp} of {ct} cases answered wrong — the rule '
                           f'cannot be trusted about the repository until its '
                           f'own cases pass.'))
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
    if ESCAPE_DEBT:
        left = [r for r in sorted(ESCAPE_DEBT)
                if r not in {x for x, _ in stale}]
        print(f'\n{len(left)} script(s) still holding the screen in a loop '
              f'nobody can interrupt (rule 15 debt) — add '
              f'`if not focus_keeper().ok("<tag>"): break`')
        for rel in left:
            print(f'  {rel:38s} {ESCAPE_DEBT[rel].why.split(" — ")[0]}')

    led_lines, led_bad = audit([('rule6 EXEMPT', EXEMPT),
                                ('rule6 DEBT', DEBT),
                                ('rule9 EXEMPT', READY_EXEMPT),
                                ('rule9 DEBT', READY_DEBT),
                                ('rule15 DEBT', ESCAPE_DEBT),
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
