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
    'tools/focus_trace.py':
        'the subject under test IS taking the foreground. Routing it through '
        'the gate that takes it would be reporting the driver to itself.',
    'tools/probe_lobby_transition.py':
        'drives the lobby<->match transition on purpose, so it cannot open by '
        'requiring a match — that is the thing it is measuring.',
    'tools/probe_tab_watch_live.py':
        'watches the Tab screen go up and down; ensure_ready shuts it.',
    'tools/snap_on_key.py':
        'a shutter. It photographs whatever is on screen ON PURPOSE, '
        'including the lobby and the menus, and putting the game into a known '
        'state first would destroy the only shots that need taking.',
}

READY_DEBT = {
    'tools/drive_screen.py': 'pre-dates control/session.py',
    'tools/dump_state.py': 'pre-dates control/session.py',
    'tools/probe_ammo_during_fire.py': 'pre-dates control/session.py',
    'tools/probe_autofit.py': 'pre-dates control/session.py',
    'tools/probe_backpack_depth.py': 'pre-dates control/session.py',
    'tools/probe_click_speed.py': 'pre-dates control/session.py',
    'tools/probe_drag_speed.py': 'pre-dates control/session.py',
    'tools/probe_drop_to_ground.py': 'pre-dates control/session.py',
    'tools/probe_drop_weapon.py': 'pre-dates control/session.py',
    'tools/probe_equip_gesture.py': 'pre-dates control/session.py',
    'tools/probe_fit_smoke.py': 'pre-dates control/session.py',
    'tools/probe_gun_grab.py': 'pre-dates control/session.py',
    'tools/probe_impulse_ab.py': 'pre-dates control/session.py',
    'tools/probe_impulse_align.py': 'pre-dates control/session.py',
    'tools/probe_input_latency.py': 'pre-dates control/session.py',
    'tools/probe_kick_profile.py': 'pre-dates control/session.py',
    'tools/probe_rack_cycle.py': 'pre-dates control/session.py',
    'tools/probe_recenter.py': 'pre-dates control/session.py',
    'tools/probe_shot_latency.py': 'pre-dates control/session.py',
    'tools/probe_slot_boxes.py': 'pre-dates control/session.py',
    'tools/probe_spawn_wait.py': 'pre-dates control/session.py',
    'tools/probe_spawner_layers.py': 'pre-dates control/session.py',
    'tools/probe_submenu_hover.py': 'pre-dates control/session.py',
    'tools/probe_toggle_latency.py': 'pre-dates control/session.py',
    'tools/probe_transfer.py': 'pre-dates control/session.py',
    'tools/probe_unequip_gesture.py': 'pre-dates control/session.py',
    'tools/probe_unequip_where.py': 'pre-dates control/session.py',
    'tools/verify_kit.py': 'pre-dates control/session.py',
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
    if rel.parts[0] != 'tools':
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
        why = check_ready(f, rel, imps)
        if why:
            not_ready.add(rel.as_posix())
            if rel.as_posix() not in READY_LEDGER:
                unready.append((rel.as_posix(), why))

    stale = check_ledger(offenders) + check_ready_ledger(not_ready)

    print(f'checked {checked} files against {len(RULES) + 1} rules')
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
        print(f'\n{len(left)} tools/ probe(s) still opening with a bare '
              f'ensure_focus (rule 9 debt) — swap in '
              f'control.session.ensure_ready')

    if not violations and not stale and not unready:
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
    print(f'\n{len(violations) + len(stale) + len(unready)} violation(s)')
    return 1


if __name__ == '__main__':
    sys.exit(main())
