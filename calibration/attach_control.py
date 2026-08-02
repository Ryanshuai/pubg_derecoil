"""Move attachments around the Tab inventory screen, by dragging.

The hands to detector/tab_items.py's eyes. That module says what is on every
row and in every slot, and hands back the point to grab each item at; this one
turns those points into press-move-release gestures and reads the result back
to confirm they landed.

    from calibration.attach_control import AttachControl, at_ground, at_inv, at_slot

    ac = AttachControl()
    ac.sync()                                # Tab open? game focused?
    view = ac.look()                         # a TabItemDetector TabView

    ac.equip(1, 'muzzle', view.find('comp_ar'))     # 库存/地面 -> gun 1
    ac.unequip(1, 'muzzle')                         # slot -> 库存
    ac.discard(view.find('uzi_stock'))              # -> the floor
    ac.stow(0)                                      # ground row 0 -> 库存
    ac.drag(at_slot(1, 'scope'), at_slot(2, 'scope'))    # gun 1 -> gun 2

    # everything the gun can take, from both lists, in one call
    ac.build(2, weapon='m416')      # weapon= only when the plate will not read

Every one of those is the same primitive, `drag(src, dst)`. A location is any
of these, and an `Item` straight out of a TabView is accepted wherever one is:

    at_ground(i)     row i of 附近 / 地面        ('nearby', i)
    at_inv(i)        row i of 库存               ('inventory', i)
    at_slot(g, s)    attachment slot s of gun g  ('weapon', g, s)

A panel location with no row — at_ground(), at_inv() — means "into this
panel, anywhere", which is what makes dropping to the floor and stowing into
the backpack the same call as everything else. `look()` records how many rows
each list is showing, so those land on the first empty row.

ROWS MOVE UNDER YOU
    A row index is only valid for the detection pass it came from. Pulling row
    i out shifts every row below it up by one, and an attachment displaced by a
    swap lands back in 库存 as a *new* row. So build()/run_plan() re-detect
    before each drag by default and re-find the attachment by name; passing
    redetect=None turns that off and falls back to plan_equip()'s
    descending-row ordering, which survives removals but not insertions.

VERIFICATION
    Anything with a weapon slot at either end is checked by reading that slot
    back: the target must hold the named item — or, when the item has no icon
    template, must at least have *changed* — and a slot dragged *from* must
    end up empty. Panel-to-panel drags cannot be checked here at all:
    rec['verified'] is False for those, and it is on the caller to re-detect.

    A failed drag is retried only when nothing changed, which is the one case
    where the source row is provably still where it was. If the slot changed
    into something unexpected, the retry is skipped and the record says so.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import HUD_REGIONS
from detector.attachment_catalog import ATTACHMENTS, ROSTER, fits, has_slot, is_live
from detector.attachment_detector import SLOT_NAMES
from detector.cropper import win32_cap
from detector.tab_detector import TabTypeDetector
from detector.tab_items import TabGrabber, TabItemDetector
from detector.tab_layout import INV_ROWS, PARK_XY, att_slot_point, row_point
from detector.weapon_template_detector import TabWeaponDetector
from press.pointer import Pointer, game_focused, ensure_focus

PANEL_KINDS = ('nearby', 'inventory')
GUNS = (1, 2)

# Verification targets.
EMPTY = ''          # the slot must read as nothing
ANY_ITEM = '*'      # the slot must read as something, no matter what

VERIFY_TIMEOUT = 0.8    # the item animates into the slot; polling beats one
                        # fixed sleep long enough to cover the worst case
VERIFY_POLL = 0.08
PARK_SETTLE = 0.06      # cursor off the slot -> tooltip gone, before a read

# Releasing over a panel means "put it in this container". PUBG has no
# per-row drop semantics in the loot lists, so any point inside the list is
# equivalent — but only points that are actually inside it. Rows past the end
# of a short list are not, hence set_rows(): with the row count known, the
# drop goes to the first empty row, and without it to row 0, which exists
# whenever the panel does.
DEFAULT_DROP_ROW = 0


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


def as_loc(x):
    """A location tuple out of either a location tuple or a TabView Item."""
    where = getattr(x, 'where', None)
    return where if where is not None else x


def is_slot(loc):
    return loc[0] == 'weapon'


def loc_str(loc):
    loc = as_loc(loc)
    if is_slot(loc):
        return f'gun{loc[1]}.{loc[2]}'
    return f'{loc[0]}' + ('' if loc[1] is None else f'[{loc[1]}]')


def parse_loc(text):
    """'inv:3' / 'ground' / 'slot:1:muzzle' -> a location tuple. For the CLI."""
    parts = text.split(':')
    kind = parts[0].lower()
    if kind in ('slot', 'gun', 'weapon'):
        if len(parts) != 3:
            raise ValueError(f'{text!r}: expected slot:<gun>:<slot>')
        return at_slot(int(parts[1]), parts[2])
    if kind in ('inv', 'inventory'):
        return at_inv(int(parts[1]) if len(parts) > 1 else None)
    if kind in ('ground', 'nearby', 'floor'):
        return at_ground(int(parts[1]) if len(parts) > 1 else None)
    raise ValueError(f'{text!r}: expected inv[:row], ground[:row] or '
                     f'slot:<gun>:<slot>')


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


# ════════════════════════════════════════════════════════════
# Control
# ════════════════════════════════════════════════════════════

class AttachControl:
    """Drag attachments between the ground, the backpack and the two guns."""

    def __init__(self, backend='auto', verbose=True):
        self.pointer = Pointer(backend)
        self.items = TabItemDetector()
        self.grabber = TabGrabber()
        self.tab = TabTypeDetector()          # device=None: pixel check only
        self.ocr = TabWeaponDetector()
        self.verbose = verbose
        self.rows = {'nearby': None, 'inventory': None}
        self.guns = {1: None, 2: None}        # catalog key per weapon slot

    def _log(self, msg):
        if self.verbose:
            print(f'[attach] {msg}', flush=True)

    def close(self):
        """Release the GDI objects TabGrabber holds open."""
        self.grabber.close()

    # ── Screen state ──

    def set_rows(self, nearby=None, inventory=None):
        """Override how many rows each panel is showing.

        Only used to pick the drop point for a panel target: with a row count
        the drop lands on the first empty row instead of on top of an existing
        item. look() sets this from what it saw, so calling it by hand is only
        for driving a drag without a detection pass.
        """
        if nearby is not None:
            self.rows['nearby'] = int(nearby)
        if inventory is not None:
            self.rows['inventory'] = int(inventory)

    def tab_open(self):
        return bool(self.tab.classify({'type': win32_cap(HUD_REGIONS['type'])}))

    def sync(self):
        """False unless the game is focused with the Tab screen up."""
        if not game_focused():
            self._log('game is not the foreground window')
            return False
        self.park()
        if not self.tab_open():
            self._log('Tab inventory is not open')
            return False
        return True

    def park(self):
        """Move the cursor off every interactive element, then let the hover
        highlight and any tooltip fade before a read.

        A no-op when the cursor is already parked, so polling a slot does not
        pay the settle time on every pass.
        """
        if self.pointer.cursor_pos() == PARK_XY:
            return
        self.pointer.move_to(*PARK_XY)
        time.sleep(PARK_SETTLE)

    def look(self):
        """Grab the Tab screen and read it. Returns a TabView.

        Also caches what it learned: which weapon is in each slot (so slot
        reads can narrow their template bank) and how many rows each list is
        showing (so a drop into a panel lands past the end of it).
        """
        frame = self._frame()
        self.guns = self._read_guns(frame)
        view = self.items.detect(frame, self.guns)
        self.set_rows(nearby=view.rows('nearby'),
                      inventory=view.rows('inventory'))
        return view

    def read_weapons(self):
        """{1: key, 2: key} off the two name plates; None where unmatched."""
        return self._read_guns(self._frame())

    def read_slots(self, gun=None):
        """What the guns are wearing, as template names ('' when empty).

        gun=None -> {1: {slot: name}, 2: {slot: name}}; gun=1|2 -> {slot: name}.
        """
        out = self._slot_states(self._frame())
        return out if gun is None else out[gun]

    # ── The primitive ──

    def drag(self, src, dst, want=None, retries=1, weapon=None):
        """Drag whatever is at `src` onto `dst`.

        want     what the destination slot should read as afterwards. Defaults
                 to ANY_ITEM when dst is a slot; ignored when it is a panel.
        weapon   ROSTER key of the gun `dst` belongs to. Given one, a drag
                 onto a slot that weapon does not have is refused before the
                 mouse moves — an attachment released over a slot that is not
                 drawn goes back where it came from, or onto the floor.

        Returns a record:
            {'ok', 'verified', 'src', 'dst', 'checks', 'attempts', 'error'}
        ok is True when the gesture went through *and* every check that could
        be made passed. verified is False when nothing could be checked, which
        is every panel-to-panel drag.

        `src` and `dst` may be TabView Items as well as location tuples.
        """
        src, dst = as_loc(src), as_loc(dst)
        err = self._reject(src, dst, weapon)
        if err:
            self._log(f'{loc_str(src)} -> {loc_str(dst)}: refused, {err}')
            return {'ok': False, 'verified': False, 'src': src, 'dst': dst,
                    'checks': [], 'attempts': 0, 'error': err}

        checks = []
        if is_slot(dst):
            checks.append((dst[1], dst[2], want or ANY_ITEM))
        if is_slot(src):
            checks.append((src[1], src[2], EMPTY))
        # The pre-drag reading is needed twice: ANY_ITEM on a slot that was
        # already occupied would otherwise pass without the drag doing
        # anything, and a retry is only safe while nothing has changed.
        before = self._slot_states(self._frame()) if checks else None

        rec = {'ok': False, 'verified': bool(checks), 'src': src, 'dst': dst,
               'checks': [], 'attempts': 0, 'error': None}
        p0, p1 = self.point_of(src), self.point_of(dst)

        for attempt in range(retries + 1):
            rec['attempts'] = attempt + 1
            if not self.pointer.drag(p0, p1):
                rec['error'] = 'cursor placement failed'
                return rec
            if not checks:
                # Nothing on the right-hand side to read back. The gesture is
                # all this module can honestly report on.
                rec['ok'] = True
                self._log(f'{loc_str(src)} -> {loc_str(dst)}: dragged '
                          f'(unverified)')
                return rec

            results = self._await(checks, before)
            rec['checks'] = [{'gun': g, 'slot': s, 'want': w, 'seen': seen,
                              'ok': ok} for g, s, w, ok, seen in results]
            if all(r['ok'] for r in rec['checks']):
                rec['ok'] = True
                self._log(f'{loc_str(src)} -> {loc_str(dst)}: ok '
                          f'({self._checks_str(rec["checks"])})')
                return rec

            if attempt >= retries:
                break
            # Retrying is safe only if the screen is exactly as it was: then
            # the item never left, so the source row is still the source row.
            moved = [(g, s) for g, s, _, _ok, seen in results
                     if seen != before[g][s]]
            if moved:
                rec['error'] = ('drag had an effect but not the expected one; '
                                'not retrying, re-detect first')
                self._log(f'{loc_str(src)} -> {loc_str(dst)}: '
                          f'{rec["error"]} ({self._checks_str(rec["checks"])})')
                return rec
            self._log(f'{loc_str(src)} -> {loc_str(dst)}: nothing changed, '
                      f'retry {attempt + 2}/{retries + 1}')

        rec['error'] = 'verification failed'
        self._log(f'{loc_str(src)} -> {loc_str(dst)}: failed '
                  f'({self._checks_str(rec["checks"])})')
        return rec

    # ── The four directions, named ──

    def equip(self, gun, slot=None, src=None, att=None, weapon=None, retries=1):
        """Put the attachment at `src` into weapon `gun`'s `slot`.

        Hand it a TabView Item and everything but the gun is implied:

            ac.equip(1, view.find('comp_ar'))

        att is a catalogue key ('comp_ar'). Given one — or inferred from an
        Item — the slot is verified against that exact template rather than
        "occupied by anything", which is the only way to tell a successful
        swap from a no-op when the slot was already full.
        """
        if src is None:                 # equip(gun, item) shorthand
            src, slot = slot, None
        if att is None:
            att = getattr(src, 'key', None)
        if slot is None:
            slot = getattr(src, 'slot', None) or (ATTACHMENTS[att]['slot']
                                                  if att in ATTACHMENTS else None)
        if weapon is None:
            weapon = self.guns.get(gun)
            if weapon is None:
                self._log(f'gun{gun} is unnamed: dragging without the '
                          f'catalogue check that the slot exists')
        if slot is None:
            return {'ok': False, 'verified': False, 'src': as_loc(src),
                    'dst': None, 'checks': [], 'attempts': 0,
                    'error': 'no target slot given, and none could be inferred'}

        want = ANY_ITEM
        if att:
            spec = ATTACHMENTS.get(att)
            if spec is None:
                return {'ok': False, 'verified': False, 'src': as_loc(src),
                        'dst': at_slot(gun, slot), 'checks': [], 'attempts': 0,
                        'error': f'unknown attachment {att!r}'}
            if spec['slot'] != slot:
                return {'ok': False, 'verified': False, 'src': as_loc(src),
                        'dst': at_slot(gun, slot), 'checks': [], 'attempts': 0,
                        'error': f'{att} is a {spec["slot"]}, not a {slot}'}
            want = spec['asset'] or ANY_ITEM
            if want == ANY_ITEM:
                self._log(f'{att} has no icon template: gun{gun}.{slot} can '
                          f'only be checked for having changed, not for '
                          f'holding {att}')
        return self.drag(src, at_slot(gun, slot), want=want, retries=retries,
                         weapon=weapon)

    def unequip(self, gun, slot, to=None, retries=1):
        """Pull weapon `gun`'s `slot` off, into 库存 by default."""
        return self.drag(at_slot(gun, slot), to or at_inv(), retries=retries)

    def strip(self, gun, to=None, retries=1):
        """Take every attachment off `gun`. Returns one record per slot."""
        worn = self.read_slots(gun)
        return [self.unequip(gun, s, to=to, retries=retries)
                for s in SLOT_NAMES if worn[s]]

    def discard(self, src, retries=1):
        """Drop whatever is at `src` on the floor. Works from a slot too."""
        return self.drag(src, at_ground(), retries=retries)

    def stow(self, row, retries=1):
        """Pick row `row` off the ground into 库存."""
        return self.drag(at_ground(row), at_inv(), retries=retries)

    # ── Batch ──

    def build(self, gun, view=None, weapon=None, replace=False,
              require_weapon=True, **kw):
        """Fit `gun` with everything compatible that is loose on screen.

        The whole loop: look, plan against the catalogue, drag, re-look. Pass
        a `view` to plan off a detection pass you already have.

        weapon          ROSTER key of what is in that slot. Defaults to what
                        the name plate read, but pass it when you know — the
                        spawner just told you, say — because three plates
                        cannot be read at all: 自动装填步枪 / 汤姆逊冲锋枪 /
                        德拉贡诺夫 have English templates (SLR, Tommy Gun,
                        Dragunov) and match nothing.
        require_weapon  without a weapon key there is no compatibility gate at
                        all: `fits()` cannot run, and an attachment released
                        over a slot the gun does not have goes on the floor.
                        So an unnamed gun plans nothing unless this is False.

        Returns (records, skipped) — skipped is plan_equip()'s, so an
        attachment that went untouched says why.
        """
        view = view if view is not None else self.look()
        weapon = weapon or self.guns.get(gun)
        if weapon is None and require_weapon:
            self._log(f'gun{gun} is unnamed: nothing planned, because without '
                      f'a weapon key the catalogue cannot say which slots it '
                      f'has. Pass weapon=, or require_weapon=False.')
            return [], [(item.key, item.where, 'weapon unknown')
                        for item in view.inventory + view.nearby
                        if item is not None and item.key]
        current = {s: (it.asset if it else '')
                   for s, it in view.weapons[gun].items()}
        drags, skipped = plan_equip(weapon, view, current, replace=replace)
        self._log(f'gun{gun} ({weapon or "ungated"}): {len(drags)} to fit, '
                  f'{len(skipped)} skipped')
        return self.run_plan(gun, drags, weapon=weapon, **kw), skipped

    def run_plan(self, gun, drags, weapon=None, redetect=True,
                 stop_on_fail=False):
        """Execute plan_equip()'s output against weapon slot `gun`.

        redetect  True (default) re-reads the screen before every drag and
                  re-finds the attachment by name, so the plan survives rows
                  reflowing underneath it — which they do the moment a swap
                  displaces something into 库存. Pass None/False to trust the
                  plan's descending-row order instead, which holds only as
                  long as nothing is inserted. A callable is used in place of
                  look() for tests.

        Returns the list of per-drag records; a record whose source vanished
        gets error='source no longer on screen' and attempts=0.
        """
        if redetect is True:
            redetect = self.look
        out = []
        for d in drags:
            src = d['src']
            if redetect:
                found = loose_items(redetect() or {})
                hits = [loc for loc, att in found.items() if att == d['att']]
                if not hits:
                    rec = {'ok': False, 'verified': False, 'src': d['src'],
                           'dst': at_slot(gun, d['slot']), 'checks': [],
                           'attempts': 0,
                           'error': 'source no longer on screen'}
                    self._log(f'{d["att"]}: {rec["error"]}, skipped')
                    out.append(rec)
                    if stop_on_fail:
                        break
                    continue
                src = hits[0]
            rec = self.equip(gun, d['slot'], src, att=d['att'], weapon=weapon)
            out.append(rec)
            if stop_on_fail and not rec['ok']:
                break
        return out

    # ── Geometry ──

    def point_of(self, loc):
        """Where to press or release for a location."""
        loc = as_loc(loc)
        if is_slot(loc):
            _, gun, slot = loc
            return att_slot_point(gun, slot)
        kind, row = loc[0], (loc[1] if len(loc) > 1 else None)
        if row is None:
            # First empty row when the count is known, else the top of the
            # list, which exists whenever the panel does.
            known = self.rows.get(kind)
            row = DEFAULT_DROP_ROW if known is None else min(known, INV_ROWS - 1)
        return row_point(row, kind)

    # ── Internals ──

    @staticmethod
    def _reject(src, dst, weapon):
        """Why this drag must not be attempted, or None."""
        for loc, side in ((src, 'source'), (dst, 'target')):
            if is_slot(loc):
                _, gun, slot = loc
                if gun not in GUNS:
                    return f'{side} gun {gun} is not 1 or 2'
                if slot not in SLOT_NAMES:
                    return f'{side} slot {slot!r} is not one of {SLOT_NAMES}'
            elif loc[0] in PANEL_KINDS:
                row = loc[1] if len(loc) > 1 else None
                if row is not None and not 0 <= row < INV_ROWS:
                    return f'{side} row {row} is outside 0..{INV_ROWS - 1}'
            else:
                return f'{side} {loc!r} is not a location'
        if src == dst:
            return 'source and target are the same place'
        if weapon is not None:
            if weapon not in ROSTER:
                return f'unknown weapon {weapon!r}'
            if is_slot(dst) and not has_slot(weapon, dst[2]):
                return f'{weapon} has no {dst[2]} slot'
        return None

    def _frame(self):
        """A Tab-screen frame, cursor out of the way first.

        The cursor sits on the drop target the moment a drag ends, and a
        hovered slot draws a tooltip over itself.
        """
        self.park()
        return self.grabber.grab()

    def _read_guns(self, frame):
        """{1: key, 2: key} off the name plates, None where not usable.

        Anything outside the live roster becomes None on purpose: an
        unrecognised key would narrow every slot's template bank to nothing
        and read the whole gun as empty. A wider bank beats a blind one.
        """
        crops = {}
        for key in ('gun_name_1', 'gun_name_2'):
            y, x, h, w = HUD_REGIONS[key]
            crops[key] = frame[y:y + h, x:x + w]
        names = self.ocr.classify(crops)
        return {g: (n if n in ROSTER and is_live(n) else None)
                for g, n in zip(GUNS, names)}

    def _slot_states(self, frame):
        """{1: {slot: template name}, 2: {...}}, '' for an empty slot."""
        worn = self.items.read_weapons(frame, self.guns)
        return {g: {s: (it.asset if it is not None else '')
                    for s, it in slots.items()}
                for g, slots in worn.items()}

    def _await(self, checks, before, timeout=VERIFY_TIMEOUT):
        """Poll the weapon slots until every check passes, or time runs out.

        Returns [(gun, slot, want, ok, seen), ...]. ANY_ITEM additionally
        demands the slot differ from `before`: dropping onto a slot that
        already reads as *something* would otherwise pass on the strength of
        what was there before the drag, so a swap that never happened would
        report success.
        """
        deadline = time.perf_counter() + timeout
        while True:
            states = self._slot_states(self._frame())
            out = []
            for gun, slot, want in checks:
                seen = states[gun][slot]
                ok = (seen != '' and seen != before[gun][slot]
                      if want == ANY_ITEM else seen == want)
                out.append((gun, slot, want, ok, seen))
            if all(r[3] for r in out) or time.perf_counter() >= deadline:
                return out
            time.sleep(VERIFY_POLL)

    @staticmethod
    def _checks_str(checks):
        out = []
        for c in checks:
            line = f'gun{c["gun"]}.{c["slot"]}={c["seen"] or "<empty>"}'
            if not c['ok']:
                line += f' (wanted {c["want"] or "<empty>"})'
            out.append(line)
        return ', '.join(out)


# ════════════════════════════════════════════════════════════
# CLI — one drag at a time, for checking the geometry by hand
# ════════════════════════════════════════════════════════════

def dump(view, guns):
    """Print a TabView the way the CLI wants it."""
    for panel in PANEL_KINDS:
        rows = getattr(view, panel)
        n = view.rows(panel)
        print(f'{panel} ({n} rows):' if n else f'{panel}: empty')
        for i in range(n):
            item = rows[i]
            if item is not None:
                print(f'   row{i:2d} {item.key:<14} {item.zh}')
            else:
                print(f'   row{i:2d} {"?":<14} <occupied, no template>')
    for g in GUNS:
        worn = '  '.join(
            f'{s}={(view.weapons[g][s].key or view.weapons[g][s].asset) if view.weapons[g][s] else "-"}'
            for s in SLOT_NAMES)
        print(f'gun{g} {guns.get(g) or "?":<10} {worn}')


def main():
    try:            # item names are Chinese; a cp1252 console dies on 倍
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(
        description='Drag attachments around the Tab inventory screen.',
        epilog='locations: inv[:row] | ground[:row] | slot:<gun>:<slot>')
    ap.add_argument('--read', action='store_true',
                    help='read the screen and print it, drag nothing')
    ap.add_argument('--drag', nargs=2, metavar=('SRC', 'DST'))
    ap.add_argument('--equip', metavar='GUN:ATT',
                    help='find an attachment by catalog key and fit it, '
                         'e.g. 1:comp_ar')
    ap.add_argument('--build', type=int, metavar='GUN',
                    help='fit this gun with everything compatible on screen')
    ap.add_argument('--weapon', help='ROSTER key of the gun --build targets, '
                                     'when the name plate does not read')
    ap.add_argument('--replace', action='store_true',
                    help='--build swaps into occupied slots too')
    ap.add_argument('--points', action='store_true',
                    help='print every click point, no game needed')
    ap.add_argument('--rows', help='override the row counts, e.g. 5,5 '
                                   '(nearby,inventory)')
    ap.add_argument('--retries', type=int, default=1)
    ap.add_argument('--countdown', type=int, default=5)
    ap.add_argument('--backend', default='auto',
                    choices=('auto', 'pico', 'sendinput'))
    args = ap.parse_args()

    if args.points:
        for kind in PANEL_KINDS:
            pts = ' '.join(f'{i}:{row_point(i, kind)}' for i in range(INV_ROWS))
            print(f'{kind:10s} {pts}')
        for g in GUNS:
            print(f'gun{g}      ' + ' '.join(f'{s}:{att_slot_point(g, s)}'
                                             for s in SLOT_NAMES))
        return 0

    actions = [args.read, args.drag, args.equip, args.build is not None]
    if not any(actions):
        ap.error('give --read, --drag SRC DST, --equip GUN:ATT, --build GUN, '
                 'or --points')

    src = dst = None
    if args.drag:
        src, dst = parse_loc(args.drag[0]), parse_loc(args.drag[1])
        print(f'{loc_str(src)} -> {loc_str(dst)}')

    print('>>> Taking the foreground. The Tab inventory must be OPEN.')
    if not ensure_focus(countdown_s=args.countdown, label='the inventory'):
        print('[!] ABORT: could not focus the game.')
        return 1
    time.sleep(0.6)

    ac = AttachControl(args.backend)
    try:
        if not ac.sync():
            return 1

        view = ac.look()
        dump(view, ac.guns)
        if args.rows:                   # after look(), so it really overrides
            n, i = (int(v) for v in args.rows.split(','))
            ac.set_rows(nearby=n, inventory=i)

        if args.build is not None:
            recs, skipped = ac.build(args.build, view, weapon=args.weapon,
                                     replace=args.replace)
            for att, loc, why in skipped:
                print(f'  skip {att:<14} {loc_str(loc):<14} {why}')
            print()
            for r in recs:
                print(f'  {loc_str(r["src"]):<14} -> {loc_str(r["dst"]):<14} '
                      f'{"ok" if r["ok"] else r["error"]}')
            return 0 if all(r['ok'] for r in recs) else 1

        if args.equip:
            gun, key = args.equip.split(':')
            item = view.find(key)
            if item is None:
                print(f'{key} is not in either list')
                return 1
            print(f'\n{key} at {loc_str(item.where)} -> gun{gun}.{item.slot}')
            rec = ac.equip(int(gun), item, retries=args.retries)
            print(f'{rec}')
            return 0 if rec['ok'] else 1

        if not args.drag:
            return 0

        rec = ac.drag(src, dst, retries=args.retries)
        print(f'\n{rec}')
        return 0 if rec['ok'] else 1
    finally:
        ac.close()


if __name__ == '__main__':
    sys.exit(main())

