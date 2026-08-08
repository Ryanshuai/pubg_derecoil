"""Where things are on the Tab screen, and which drags exist between them.

    from control.locations import at_gun, at_slot, at_inv, at_ground

Pure. No game, no hardware, no screen — a location is a tuple and this module
only ever builds, parses, prints and compares them. `pixi run locations` has
81 offline cases over it.

⚠ IT IS A VOCABULARY, AND THE ADDRESSES ARE NOT INTERCHANGEABLE. at_gun(1) is
the WEAPON in rack slot 1; at_slot(1, 'muzzle') is one of its attachment
slots. Confusing them is not a typo with a typo's consequences: `_reject()`
once did not recognise ('gun', n) at all, so every attempt to drop a weapon
was refused before the mouse moved — reported as "the drag failed" — while the
address, the grab point and the method all existed and were correct. That bug
lived until somebody wrote the offline test.

MOVES is the other half and the sharper one: two valid addresses do not imply
the move between them exists. ('gun','inventory') is two good addresses and a
gesture the game does not have. Each entry carries the gesture, whether the
result can be read back, and the EVIDENCE LEVEL behind it — measured / used /
untested — because "we have always done it this way" and "we measured it" are
different claims and the table is where they stop looking alike.

WHY IT IS ITS OWN FILE (2026-08-08). It was the first thousand lines of
control/inventory.py, which had grown to 3776. The cut is not cosmetic: this
half references neither InventoryControl nor `self` anywhere, so it was never
part of the driver — it was a pure module living inside one, where nothing
could see that it was pure. control/inventory.py re-exports every public name
here, so no call site changed.
"""
from detector.tab_layout import att_slot_point, gun_tag_point, row_point  # noqa: F401

PANEL_KINDS = ('nearby', 'inventory')




def panel_counts(src, dst):
    """Which lists can be counted to see whether this drag landed.

    -> (source panel or None, destination panel) | None

    `dst` with no row means "anywhere in this list", and a list fills from the
    top with no gaps, so its row count answers "did something arrive". That is
    the ONLY reading available for a drop into a panel, and it is available
    whatever the source is — which matters, because the source tells you much
    less than it appears to:

        unequip() releases a slot onto the floor and verifies the SLOT IS
        EMPTY. It is empty either way. docs/game_quirks.md has the record: the
        part reached the floor instead of the backpack and the slot check
        passed, for months.

    So the source is returned only when it is itself a list row (then its
    departure is a second, independent signal), and the destination always.
    """
    if is_slot(dst) or is_gun(dst) or dst[0] not in PANEL_KINDS:
        return None
    if len(dst) > 1 and dst[1] is not None:
        return None
    src_panel = (src[0] if src[0] in PANEL_KINDS and len(src) > 1
                 and src[1] is not None else None)
    return (src_panel, dst[0])
GUNS = (1, 2)

# Verification targets.
EMPTY = ''          # the slot must read as nothing
ANY_ITEM = '*'      # the slot must read as something, no matter what

# ════════════════════════════════════════════════════════════
# MOVES — which src -> dst pairs exist, and what is KNOWN about each
# ════════════════════════════════════════════════════════════
#
# This was prose in the module docstring, which meant nothing could check it
# and nothing could read it. Now `_reject` gates on it and another agent
# composing a flow can look the answer up:
#
#     from control.inventory import MOVES, kind_of, at_slot, at_inv
#     MOVES[(kind_of(at_inv(0)), kind_of(at_slot(1, 'muzzle')))]
#     -> {'gesture': 'click', 'verified': True, 'evidence': 'measured', ...}
#
# EVERY ENTRY CARRIES `evidence`, AND THAT IS THE POINT. attachment_catalog's
# SLOTS table shipped as 22 wiki readings, 6 guesses and 2 screenshot reads
# with 0 measured, all indistinguishable from each other, and it cost two
# entries that silently dropped attachments on the floor. A capability table
# that cannot say how it knows repeats that.
#
#     'measured'  a probe ran it and the numbers are in docs/game_quirks.md
#     'used'      no dedicated probe, but calibration runs take this path
#                 constantly and would fail loudly if it did not work
#     'untested'  believed to exist, never confirmed. transfer() refuses to
#                 default to one of these.
#
# `gesture` is the one that LANDS, which is not always the obvious one — see
# the 0/4 below. `verified` is whether *this module* can confirm the outcome
# by re-reading; a panel-to-panel move has no slot to read, so it cannot.
MOVES = {
    ('inventory', 'weapon'): {
        'gesture': 'click', 'verified': True, 'evidence': 'measured',
        'note': 'RIGHT-CLICK, not drag. The drag measured 0/4 — it does not '
                'land at all — while right-click is 4/4 at 0.35 s. It equips '
                'onto the gun IN HAND, so hold(gun) first.'},
    ('nearby', 'weapon'): {
        'gesture': 'click', 'verified': True, 'evidence': 'used',
        'note': 'same gesture as from 库存; build() fits off the ground this '
                'way on every range entry.'},
    ('weapon', 'inventory'): {
        'gesture': 'drag', 'verified': True, 'evidence': 'measured',
        'note': 'the direction that DOES drag. Right-click on a fitted part '
                'also sends it to the pack (measured 2026-08-02), which is '
                'what unequip(gesture="click") uses.'},
    ('weapon', 'nearby'): {
        'gesture': 'drag', 'verified': True, 'evidence': 'used',
        'note': 'strip(to=at_ground()) — a part straight from the slot to '
                'the floor, skipping the pack.'},
    ('gun', 'nearby'): {
        'gesture': 'click', 'verified': True, 'evidence': 'measured',
        'carries_attachments': True,
        'note': 'the whole weapon, WEARING its parts: 0.66 s by right-click '
                'vs 1.15 s by a 1621 px drag, both 1/1. Two runs confirmed '
                'rack empty, 库存 zero growth, ground +1 row. Stripping '
                'first is worse — PUBG auto-fits the pack onto the next gun '
                'to arrive, which is how a cell labelled BARE ran wearing a '
                'grip and a quickdraw magazine.'},
    ('nearby', 'inventory'): {
        'gesture': 'drag', 'verified': False, 'evidence': 'used',
        'note': 'stow(). Nothing here can confirm it — see `verified`.'},
    ('inventory', 'nearby'): {
        'gesture': 'drag', 'verified': False, 'evidence': 'used',
        'note': 'discard(). stock.tidy() drops the surplus this way and '
                'confirms by re-reading the whole panel, which is the '
                'caller-side check `verified: False` is asking for.'},
    ('weapon', 'weapon'): {
        'gesture': 'drag', 'verified': True, 'evidence': 'untested',
        'note': 'slot to slot, including gun 1 -> gun 2. The module '
                'docstring has always advertised it and nothing has ever '
                'measured it — and the neighbouring fact is discouraging: a '
                'drag INTO a weapon slot from 库存 is 0/4. If that failure '
                'is about the drop target rather than the source, this does '
                'not work either. transfer() therefore does NOT default to '
                'it. What would settle it: drag slot->slot N times and read '
                'BOTH ends back — source empty AND destination filled.'},
}


def kind_of(loc):
    """The MOVES key for a location tuple. ('weapon', 1, 'muzzle') -> 'weapon'"""
    return loc[0] if isinstance(loc, (tuple, list)) and loc else None


def move_info(src, dst):
    """What is known about dragging src -> dst, or None if it is not a move."""
    return MOVES.get((kind_of(src), kind_of(dst)))




# ════════════════════════════════════════════════════════════
# Locations
# ════════════════════════════════════════════════════════════

def at_ground(row=None):
    """Row `row` of 附近 / 地面, or the panel itself when row is None."""
    return ('nearby', row)


def at_inv(row=None):
    """Row `row` of 库存, or the panel itself when row is None."""
    return ('inventory', row)


def at_slot(gun, slot):
    """Attachment slot `slot` of weapon `gun` (1 = top / key 1, 2 = bottom).

    Spelled ('weapon', ...) rather than ('slot', ...) because that is what
    TabItemDetector already stamps on every Item it finds in a gun — one
    vocabulary, so an Item can be handed straight back as a drag source.
    """
    return ('weapon', gun, slot)


def at_gun(gun):
    """The WEAPON itself in rack slot `gun`, not one of its attachment slots.

    Spelled ('gun', n) so it cannot be confused with ('weapon', n, slot),
    which is an attachment slot on that gun.

    The drag point is the boxed slot number at the left end of the row --
    tab_layout.gun_tag_point, measured off calibration/artifacts/tab_inventory.png. That is the
    handle for the weapon itself; the name plate beside it and the attachment
    tiles below it are not.
    """
    return ('gun', gun)


def is_gun(loc):
    return isinstance(loc, tuple) and len(loc) == 2 and loc[0] == 'gun'


def as_loc(x):
    """A location tuple out of either a location tuple or a TabView Item."""
    where = getattr(x, 'where', None)
    return where if where is not None else x


def is_slot(loc):
    return loc[0] == 'weapon'


    return None


def loc_str(loc):
    loc = as_loc(loc)
    if is_gun(loc):
        return f'gun{loc[1]}'           # the weapon, vs gun1.muzzle for a slot
    if is_slot(loc):
        return f'gun{loc[1]}.{loc[2]}'
    return f'{loc[0]}' + ('' if loc[1] is None else f'[{loc[1]}]')


def parse_loc(text):
    """Location tuple from CLI text. -> at_inv / at_ground / at_slot / at_gun

        inv:3          库存 row 3          ground / ground:0   附近
        slot:1:muzzle  gun 1's muzzle      gun:1               gun 1 ITSELF

    `gun:1` and `gun:1:muzzle` mean different things -- the whole weapon
    against one of its slots -- which is why at_gun is spelled ('gun', n) and
    a slot ('weapon', n, slot). The three-part form is kept for both spellings
    because it was already accepted.
    """
    parts = text.split(':')
    kind = parts[0].lower()
    if kind in ('slot', 'gun', 'weapon'):
        if len(parts) == 2 and kind in ('gun', 'weapon'):
            return at_gun(int(parts[1]))
        if len(parts) != 3:
            raise ValueError(f'{text!r}: expected slot:<gun>:<slot> for a '
                             f'slot, or gun:<n> for the weapon itself')
        return at_slot(int(parts[1]), parts[2])
    if kind in ('inv', 'inventory'):
        return at_inv(int(parts[1]) if len(parts) > 1 else None)
    if kind in ('ground', 'nearby', 'floor'):
        return at_ground(int(parts[1]) if len(parts) > 1 else None)
    raise ValueError(f'{text!r}: expected inv[:row], ground[:row], '
                     f'slot:<gun>:<slot> or gun:<n>')
