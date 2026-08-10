"""What to drag, in what order, to get from the kit on the gun to the kit asked for.

    from control.kit_plan import plan_kit, kit_faults

Pure. Given what is worn and what is wanted it returns a list of steps; it
never touches the screen, the game or a detector. `plan_kit` is the planner
and `kit_faults` is its counterpart after the fact — which slots disagree with
what was asked for, and whether the disagreement can even be read.

⚠ kit_faults RETURNS THREE ANSWERS, NOT TWO, and the third one cost eleven
cells on 2026-08-05. A slot reads the wanted part, or another part, or it
CANNOT BE READ — the Tab panel is translucent, a slot icon is composited over
the world, and against a dark backdrop the margin between neighbours collapses
(same vector magazine: 1.02 on a dark frame, 1.67–2.74 on six others). The
part was on the gun the whole time. Treating unreadable as "did not land" is
what retried, failed and threw the cell away, so every entry carries
`verifiable`, and only the unverifiable ones are worth re-reading after a view
nudge. A slot that names a DIFFERENT part is a real disagreement and re-reading
it says nothing new.

⚠ A PLAN IS NOT A SEQUENCE OF INSTRUCTIONS TO REPLAY. Every step's coordinates
must be recomputed at the moment it runs: fitting a part into an occupied slot
evicts the old one INTO THE BACKPACK, which adds a row and moves everything
under it. Three separate functions were written on 2026-08-07 that planned
once and then executed, and all three failed the same way with clean geometry
in the log, because the gesture was fine and the target had moved. The worst
of them put the evicted magazine back on the gun and wrote the combination
into kit_facts.json as incompatible. `pixi run gestures` is the ratchet.

WHY IT IS ITS OWN FILE (2026-08-08). It was 300 lines inside
control/inventory.py's 3776, referencing neither InventoryControl nor `self`.
control/inventory.py re-exports the public names, so no call site changed.
"""
from detector.attachment_catalog import ATTACHMENTS, ROSTER, fits, has_slot
from detector.attachment_detector import AMBIGUOUS, SLOT_NAMES
from control.locations import is_slot

# ════════════════════════════════════════════════════════════
# Planning — pure, no game needed
# ════════════════════════════════════════════════════════════

def loose_items(found):
    """{loc: att_key} for everything sitting in the two lists.

    Accepts a TabView or an already-flattened mapping, so a caller can plan
    straight off a detection pass or hand-build one for a test.
    """
    if not hasattr(found, 'inventory'):
        return dict(found)
    return {item.where: item.key
            for panel in ('inventory', 'nearby')
            for item in getattr(found, panel)
            if item is not None and item.key}


def plan_equip(weapon, found, current=None, replace=False):
    """Turn one detection pass into an ordered list of drags.

    weapon   ROSTER key of the gun being built, or None to skip the
             compatibility gate (then every found attachment is attempted)
    found    a TabView, or {loc: att_key} — what is loose in the two left
             panels, e.g. {('inventory', 3): 'comp_ar', ('nearby', 0): 'vert_grip'}
    current  {slot: template_name} of what the gun already wears. An occupied
             slot is left alone unless replace=True.
    replace  drag onto occupied slots too; the game swaps, and the old
             attachment lands in 库存 as a new row

    Returns (drags, skipped):
        drags    [{'att', 'src', 'slot'}, ...], sorted by source row
                 descending within each panel so that pulling one row out
                 cannot invalidate the rows of the drags still queued
        skipped  [(att, loc, reason), ...] — every candidate that was dropped,
                 so a caller can print why an attachment went unused
    """
    current = current or {}
    drags, skipped, claimed = [], [], set()

    for loc, att in loose_items(found).items():
        spec = ATTACHMENTS.get(att)
        if spec is None:
            skipped.append((att, loc, 'not in the attachment catalogue'))
            continue
        slot = spec['slot']
        if weapon is not None and not fits(weapon, att):
            reason = ('weapon has no {} slot'.format(slot)
                      if not has_slot(weapon, slot)
                      else f'{weapon} does not take {att}')
            skipped.append((att, loc, reason))
            continue
        if slot in claimed:
            skipped.append((att, loc, f'{slot} already claimed by an earlier '
                                      f'candidate'))
            continue
        if current.get(slot) and not replace:
            skipped.append((att, loc, f'{slot} already holds '
                                      f'{current[slot]}'))
            continue
        claimed.add(slot)
        drags.append({'att': att, 'src': loc, 'slot': slot})

    # Descending row order per panel: removing row i only shifts rows > i.
    def key(d):
        src = d['src']
        row = -1 if is_slot(src) or src[1] is None else src[1]
        return (src[0], -row)

    drags.sort(key=key)
    return drags, skipped


# ── The kit: what a gun should be wearing ──

# Where a part is preferred from when both lists have one. 库存 first, which is
# what TabView.find() already answers with, and it is also the cheaper source:
# a swap displaces the old part back into the panel the new one came from, so
# equipping out of 库存 keeps the floor clean.
_PANEL_RANK = {'inventory': 0, 'nearby': 1}


def _src_rank(loc):
    row = loc[1] if len(loc) > 1 and loc[1] is not None else 0
    return (_PANEL_RANK.get(loc[0], 2), row)


def slot_matches(readback, key):
    """Does a slot readback name attachment `key`?

    Two vocabularies meet here. read_slots() answers in AttachmentDetector
    template stems (Muzzle_Compensator_Large_C) and every caller speaks
    catalogue keys (comp_ar). ATTACHMENTS[key]['asset'] is the catalogue's own
    bridge between them and is what this trusts first.

    The substring fallbacks are not sloppiness, they are the template bank
    being wider than the catalogue: 'laser' is catalogued as
    Lower_LaserPointer_C and the bank ships SideRail_LaserPointer_C, so an
    exact comparison reads a fitted laser as "not a laser" and the kitter
    takes it off and puts it back on forever.

    It stays a *narrow* fallback on purpose. The pair this must never confuse
    is 扩容弹匣 against 加长快速弹匣 — weapon_axis swaps one for the other and
    calls the difference a measurement — and neither name contains the other.
    """
    if not readback:
        return False
    r = str(readback).lower()
    asset = (ATTACHMENTS.get(key) or {}).get('asset') or ''
    if asset:
        a = asset.lower()
        if r == a or a in r or r in a:
            return True
    return bool(key) and str(key).lower() in r


def _slot_order(want):
    """want's slots, in SLOT_NAMES order, with anything unrecognised last."""
    return ([s for s in SLOT_NAMES if s in want]
            + [s for s in want if s not in SLOT_NAMES])


def _kit_refuse(weapon, slot, key):
    """Why `key` cannot go in `slot` of `weapon`, or None.

    Emptying a slot is never refused: a slot the weapon does not have reads
    exactly like one that is drawn empty, so "must be empty" is already true
    there and asking for it costs nothing.
    """
    if slot not in SLOT_NAMES:
        return f'{slot!r} is not one of {SLOT_NAMES}'
    if key is None:
        return None
    spec = ATTACHMENTS.get(key)
    if spec is None:
        return f'unknown attachment {key!r}'
    if spec['slot'] != slot:
        return f'{key} is a {spec["slot"]}, not a {slot}'
    if weapon is not None:
        if weapon not in ROSTER:
            return f'unknown weapon {weapon!r}'
        if not has_slot(weapon, slot):
            return f'{weapon} has no {slot} slot'
        if not fits(weapon, key):
            return f'{weapon} does not take {key}'
    return None


def _kit_step(action, slot, key, src, was, error):
    return {'action': action, 'slot': slot, 'key': key, 'src': src,
            'was': was, 'error': error}


def plan_kit(want, worn, found=None, weapon=None):
    """The shortest way from `worn` to `want`. Pure — no game, no screen.

    want    {slot: att_key or None}. None means the slot must END UP EMPTY,
            which is not the same as leaving it alone: PUBG auto-fits whatever
            the backpack holds onto a gun the moment it arrives, so a slot
            nobody named is not empty, it is whatever the last strip left
            lying around. A run labelled BARE came back wearing a cheek pad,
            and a cheek pad reduces recoil. A slot ABSENT from `want` is
            explicitly unmanaged and this will not touch or check it.
    worn    {slot: template name} as read_slots() gives it, '' for empty
    found   what is loose in the two panels: a TabView, or {loc: att_key}.
            None means "do not check availability" — every equip is planned
            with src=None and the executor re-finds the part by name.
    weapon  ROSTER key, for the catalogue gate. Without one a drag can be
            planned onto a slot the weapon does not have, and a part released
            over a slot that is not drawn goes on the floor.

    Returns {'ok', 'steps', 'unchanged', 'missing', 'error'}:

        steps      [{'action': 'unequip'|'equip', 'slot', 'key', 'src',
                     'was', 'error'}, ...] — removals first, then fits, each
                   in SLOT_NAMES order. A step carrying an `error` is NOT
                   executable and is in the list so that the impossible slot
                   is reported rather than silently dropped.
        unchanged  slots already correct. These are never touched, which is
                   the whole point of asking before acting.
        missing    keys that are wanted, legal, and nowhere on screen
        ok         no step carries an error

    ONE ACTION PER WRONG SLOT is what "shortest" means here. A part dropped on
    an occupied slot swaps, and the displaced one goes back to the panel the
    new one came from (docs/game_quirks.md), so a replacement is one step and
    not an unequip followed by an equip. Removals go first because they are
    the steps that cannot fail for want of a part: if a fit later turns out to
    be impossible, the gun is at least in the state its `None`s asked for
    rather than half of two configurations.
    """
    loose = None if found is None else loose_items(found)
    steps, unchanged, missing = [], [], []

    for slot in _slot_order(want):
        key = want[slot]
        cur = (worn or {}).get(slot, '') or ''
        err = _kit_refuse(weapon, slot, key)
        if err:
            steps.append(_kit_step('equip' if key is not None else 'unequip',
                                   slot, key, None, cur, err))
            continue
        if key is None:
            if cur:
                steps.append(_kit_step('unequip', slot, None, None, cur, None))
            else:
                unchanged.append(slot)
            continue
        if slot_matches(cur, key):
            unchanged.append(slot)
            continue
        src = None
        if loose is not None:
            hits = sorted((loc for loc, k in loose.items() if k == key),
                          key=_src_rank)
            if not hits:
                missing.append(key)
                steps.append(_kit_step('equip', slot, key, None, cur,
                                       'not on screen'))
                continue
            src = hits[0]
        steps.append(_kit_step('equip', slot, key, src, cur, None))

    # Stable, so slots keep SLOT_NAMES order inside each half.
    steps.sort(key=lambda s: 0 if s['action'] == 'unequip' else 1)
    bad = [f'{s["slot"]}: {s["error"]}' for s in steps if s['error']]
    return {'ok': not bad, 'steps': steps, 'unchanged': unchanged,
            'missing': missing, 'error': '; '.join(bad) or None}


def kit_faults(want, worn):
    """Slots whose readback disagrees with what was asked for. [] is clean.

    -> [{'slot', 'key', 'why', 'verifiable'}, ...]

    `verifiable` is False when the wanted part has no icon template: the slot
    cannot be read as holding it, only as holding *something*, so the fault
    means "cannot be proven" rather than "is wrong". Both are reasons not to
    record a measurement, but only one of them is a reason to go looking for a
    failed drag.

    As of 2026-08-03 no attachment in the catalogue is in that state — the
    three that were (brake_ar, heavy_stock, variable, all added to the game
    after this repo's art dump) now carry icons recovered off the screen by
    calibration/legacy_solve_template.py. The branch stays for the next one the game adds.
    """
    out = []
    for slot in _slot_order(want):
        key = want[slot]
        cur = (worn or {}).get(slot, '') or ''
        if key is None:
            if cur == AMBIGUOUS:
                # "Something is there and the bank cannot name it" is not
                # evidence that the slot is occupied — a translucent panel over
                # a dark backdrop drags an EMPTY tile's best match under
                # MSE_EMPTY_TH with no margin, and out comes the sentinel. Same
                # cause as the fitted case below; the `key is None` branch was
                # simply missed when that was fixed, and it cost two of three
                # mk14 EMA passes to `muzzle should be empty, reads '?'`.
                out.append({'slot': slot, 'key': None, 'verifiable': False,
                            'why': f'reads {cur!r} — cannot tell an occupied '
                                   f'slot from a dark backdrop; wanted empty'})
            elif cur:
                out.append({'slot': slot, 'key': None, 'verifiable': True,
                            'why': f'reads {cur!r}, should be empty'})
            continue
        if slot_matches(cur, key):
            continue
        if cur == AMBIGUOUS:
            # Occupied, and the bank cannot separate its top two candidates
            # (AttachmentDetector.MARGIN_MIN). That is the same KIND of fault
            # as a part with no template: the slot cannot be read as holding
            # this, only as holding something. NOT verifiable, so ensure_kit
            # reports it rather than treating it as a drag that missed and
            # dragging again.
            #
            # ⚠ This used to end "— a retry cannot improve a reading", and
            # that is false: the panel is TRANSLUCENT, so re-reading against a
            # different backdrop can and does resolve it (see ensure_kit's
            # AMBIGUOUS_REREADS). Re-DRAGGING still cannot, which is the part
            # that was right. `verifiable: False` is what tells the two apart.
            out.append({'slot': slot, 'key': key, 'verifiable': False,
                        'why': f'holds something the templates cannot '
                               f'separate; wanted {key}'})
            continue
        spec = ATTACHMENTS.get(key) or {}
        if spec.get('asset'):
            out.append({'slot': slot, 'key': key, 'verifiable': True,
                        'why': f'reads {cur!r}'})
        else:
            out.append({'slot': slot, 'key': key, 'verifiable': False,
                        'why': f'{key} has no icon template; slot reads '
                               f'{cur!r}'})
    return out


# ════════════════════════════════════════════════════════════
