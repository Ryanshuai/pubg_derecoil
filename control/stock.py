"""Look in the backpack first. Spawn only what is missing, throw out the rest.

The item spawner has no idea what you already own. Clicking 垂直握把 always
produces another 垂直握把 — so a run that spawns its whole parts list on every
range entry (and harvest.py re-stocks after every eviction, plus once at the
start) ends up with a backpack full of duplicates. It fills, and then the next
spawn has nowhere to land at all.

Worse than the waste: every spare is one more thing `TabView.find()` can pick
instead of the one meant. The lists reflow as rows come and go, so "the second
comp_ar" is not a stable place — it is whatever row the game felt like.

So the order here is always look, tidy, top up:

    from control.stock import restock, read_stock

    restock(ac, sc, want={'comp_ar', 'vert_grip', 'red_dot'})

  1. read what is in 库存 and on the two guns
  2. spawn a backpack if none is worn — an attachment with no backpack does
     not fail cleanly, it lands somewhere else and every drag after it is
     aimed at the wrong row
  3. drop every duplicate on the floor, and anything not on the wanted list
  4. spawn only the keys that are still missing

Only NAMED attachments are ever dropped — an item whose icon has no template
comes back as `unknown` and is left strictly alone. That is what keeps ammo,
meds and the guns themselves out of this, none of which have templates.

WHY THIS IS control/ AND NOT calibration/
    It was calibration/stocktake.py until 2026-08-02, and everything in it is
    a driver: it presses Tab, reads the screen, drags parts onto the floor and
    clicks through the spawner panel. calibration/'s own criterion ("needs to
    know what is happening in the game -> control/") put the whole file here.

    The Rig went with the move, and that is the point of it rather than a side
    effect: `open_tab(rig, ac)` and `restock(rig, ac, sc, want)` took a Rig
    solely to reach ensure_inventory_open/closed, while InventoryControl has
    ensure_tab()/tab_up() of its own. A function needing three driver objects
    as arguments is the direct symptom of standing in the wrong layer — the
    same diagnosis 5f made when it removed the fourth (`panel`).

WHAT THIS CANNOT SEE
    The lists show 12 rows before scrolling and nothing here scrolls them. A
    backpack holding more than that is drained a dozen rows at a time: rows
    below scroll up as the ones above are dropped, so tidy() simply repeats
    until a pass changes nothing.
"""
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detector.attachment_catalog import ATTACHMENTS, ROSTER
from detector.cropper import win32_cap
from detector.geometry import detail
from detector.tab_layout import equip_region
from control.focus import ensure_focus, game_focused

# Worn vs not, off the character's backpack slot. Measured on the reference
# captures (temp_debug/probe_backpack_slot.py): a backpack draws its artwork at
# ~2900 Laplacian variance, an empty slot shows the blurred world behind the
# panel at ~44. Same test tab_items uses for an undrawn weapon slot, and the
# same reason — there is no template to match when there is no UI on screen.
#
# Presence only. A level 1 backpack reads exactly like a level 3, so this
# cannot confirm the SIZE of what is worn; it can only say the spawn is not
# needed. In the training range that is enough, since re-entry empties the
# character and the only backpack ever spawned here is the level 3.
BACKPACK_DETAIL_MIN = 300

# Bound on tidy()'s repeat-until-clean loop. Each pass drops every duplicate it
# can see, so this only matters for a backpack far deeper than the 12 visible
# rows, or for a drop that silently does nothing.
TIDY_PASSES = 6

# The level 3 pack, and the only one this range ever spawns. It is a default
# rather than a caller's argument because every caller passed the same value —
# harvest.BACKPACK was this string.
BACKPACK = 'backpack3'


def backpack_worn(crop=None):
    """Is the character wearing a backpack? Reads the equipment slot."""
    if crop is None:
        crop = win32_cap(equip_region('backpack'))
    return detail(crop) >= BACKPACK_DETAIL_MIN


def open_tab(ac, label='the backpack'):
    """Tab open, focused, and InventoryControl synced. False with a reason.

    This docstring has been wrong twice, so here is what each claim was.

    It first said two INDEPENDENT detectors had to agree. They were never
    independent judgements, only two drifted copies of one — and the copies
    disagreed, which is the opposite of a cross-check. There is one predicate
    now, detector/tab_detector.TabTypeDetector, and control/gun.py calls the
    same one.

    It then said the second call still earned a second CAPTURE path: the Rig
    read 'type' out of the banded grabber it already had while
    InventoryControl win32_cap's it fresh, so a stale or mis-blitted grabber
    showed up as a disagreement. True, and beside the point — that grabber is
    the FIRE loop's frame source and no drag ever reads it. Checking it here
    was checking a frame nothing downstream uses, and the Rig is gone.

    What is left is the part that actually fails in practice: ac.sync() demands
    the foreground and parks the cursor, and a dropped foreground between
    opening the panel and dragging in it is the common failure, not a misread
    panel. "Opened but would not sync" is a real state, and naming it is the
    difference between a fixable failure and a cell that dies as an
    unexplained "could not reach config".
    """
    if not ac.ensure_tab(True):
        # NAMED HERE TOO, which is the point the paragraph above makes and
        # this branch used to skip. The foreground is the common failure and
        # ensure_tab cannot see it: it presses Tab three times and polls the
        # screen, and a key sent to a window that is not frontmost changes
        # nothing that a poll can distinguish from a key the game ignored.
        # The check only existed one line further down, in sync(), which never
        # runs when this branch is taken.
        #
        # Measured 2026-08-04: the ace32 lost three of four configs to a bare
        # "inventory would not open for kitting" with nothing to act on, in a
        # run whose other three weapons kitted normally.
        print(f"      [!] inventory would not open for {label}: "
              f"focused={game_focused()}, "
              f"screen reads tab_open={ac.tab_open()}, "
              f"pico={'yes' if ac.pointer.pico else 'NO — Tab cannot be sent'}")
        return False
    if not ac.sync():
        print(f"      [!] the Tab screen would not sync: "
              f"focused={game_focused()}, "
              f"inventory sees tab_open={ac.tab_open()}")
        return False
    return True


class Stock:
    """One reading of what is on hand: 库存 rows plus what the guns wear."""

    __slots__ = ('view', 'counts', 'fitted', 'rows', 'unknown', 'backpack')

    def __init__(self, view, backpack):
        self.view = view
        self.backpack = backpack
        self.counts = Counter(item.key for item in view.inventory
                              if item is not None and item.key)
        self.fitted = {g: {s: (it.key if it is not None else None)
                           for s, it in slots.items()}
                       for g, slots in view.weapons.items()}
        self.rows = view.rows('inventory')
        self.unknown = sum(1 for p, _ in view.unknown if p == 'inventory')

    # ── questions ──

    def in_pack(self, key):
        """Copies sitting loose in 库存."""
        return self.counts.get(key, 0)

    def on_guns(self, key):
        """Copies fitted to either weapon — owned, just not loose."""
        return sum(1 for slots in self.fitted.values()
                   for k in slots.values() if k == key)

    def have(self, key):
        return self.in_pack(key) + self.on_guns(key)

    def missing(self, want, loose_only=False, per=1):
        """Keys from `want` that are not on hand, each repeated as needed.

        `per` is how many copies the caller needs LOOSE at once. The weapon
        axis fills both rack slots per batch and PUBG auto-fits from the
        backpack, so one comp_ar dresses the first gun and leaves the second
        bare — a pair needs two of everything. A returned key appears once per
        missing copy, which is what give_many counts to decide how many times
        to click.

        `loose_only` counts rows in 库存 and ignores anything worn. Use it when
        the caller is about to FIT parts: a part bolted to the other weapon is
        owned but not available, and nothing in the run goes and gets it.
        strip() only undresses the slot being measured.

        Counting worn parts as owned is what produced "magazine should be
        ext_ar, reads ''" on a run whose backpack plainly held one: the other
        gun was wearing the rest, `missing` came back empty, and then spawning
        the new weapon evicted that gun -- and an evicted gun leaves wearing
        everything it had on, so the parts left the inventory entirely between
        the stock read and the fitting.
        """
        count = self.in_pack if loose_only else self.have
        out = []
        for k in want:
            out += [k] * max(0, per - count(k))
        return out

    def duplicates(self, keep=1):
        """The surplus copies, keeping the topmost of each. [Item, ...]

        A part on a gun does not count towards the limit: it is not taking up
        a row, and the next strip() puts it back in the pack where this will
        see it as the duplicate it then is.
        """
        seen, out = Counter(), []
        for item in self.view.inventory:
            if item is None or not item.key:
                continue
            seen[item.key] += 1
            if seen[item.key] > keep:
                out.append(item)
        return out

    def unwanted(self, want):
        """Named attachments in 库存 that nothing in this run asks for.

        Deliberately restricted to keys in the attachment catalogue: an
        unrecognised row is `unknown`, never `unwanted`, so ammunition and
        med kits are not eligible to be thrown away by a bad match.
        """
        return [item for item in self.view.inventory
                if item is not None and item.key and item.key not in want
                and item.key in ATTACHMENTS]

    def summary(self):
        parts = ', '.join(f'{k}x{n}' if n > 1 else k
                          for k, n in sorted(self.counts.items()))
        extra = f', +{self.unknown} unnamed' if self.unknown else ''
        return (f"{self.rows} row(s)"
                f"{': ' + parts if parts else ''}{extra}"
                f"{'' if self.backpack else '  [NO BACKPACK]'}")


def read_stock(ac, close=True):
    """Open the Tab screen, read it, and (by default) close it again.

    Returns a Stock, or None if the screen could not be reached. Pass
    close=False to keep it up for a drag that follows.
    """
    if not open_tab(ac):
        return None
    try:
        view = ac.look()
        worn = backpack_worn()
    finally:
        if close:
            ac.ensure_tab(False)
    return Stock(view, worn)


def _row_of(item):
    """Sort key that puts the bottom rows first — see tidy()."""
    where = item.where
    return where[1] if where[0] == 'inventory' else -1


def _view_sig(stock):
    """What the twelve visible rows are, in order. Unnamed rows count.

    Used only to tell "nothing moved" from "things moved" — see tidy(). Named
    keys alone are not enough: an unnamed row (ammo, a med kit) scrolling into
    a slot a dropped part just left is movement, and reading it as stillness
    stops the tidy one pass early.
    """
    # `i.key`, not getattr: Item declares key in __slots__ and always assigns
    # it. The `or '?'` stays — key is None for an asset with no catalogue
    # entry, and those rows still have to count as occupied here.
    return tuple((i.key or '?') if i is not None else '-'
                 for i in stock.view.inventory)


def tidy(ac, want, drop_unwanted=True, verbose=True, keep=1):
    """Throw the surplus on the floor. Returns (n_dropped, final Stock|None).

    Drops bottom-up within a pass: pulling row i out shifts only the rows
    below it, so a descending order stays valid without re-reading between
    drags. Between passes the screen IS re-read, because rows from further
    down the backpack scroll up into view as the ones above leave.

    A pass that drops nothing ends it. Nothing else does, short of
    TIDY_PASSES.

    BOTH EARLIER STOP CONDITIONS WERE WRONG, and for one reason: the Tab list
    shows twelve rows while the backpack holds far more, so anything read off
    those twelve describes a WINDOW and not the pack.

      * "the item count went down" -- rows scroll up from below as the ones
        above leave, so a pass that successfully drops five parts can come
        back holding MORE named attachments than it started with. It quit
        after two passes on a pack carrying six surplus SMG parts and left the
        junk in place for every run after.

      * "the view did not move" -- same defect, one level subtler. Twelve
        rows leaving and twelve similar ones arriving is an unchanged
        signature, and a pack full of duplicates produces exactly that. It
        cost an unattended run four cells: tidy stopped early, the pack stayed
        deep, ext_smg sat below the window where find() cannot see it, restock
        spawned more that landed below the window too, and every SMG cell
        failed with `magazine reads ''` while the magazine was in the backpack
        the whole time.

    So there is no view-derived stop condition here any more. The drags do
    land -- tools/probe_backpack_depth.py settles it by dropping exactly ONE
    item and watching a previously invisible row take its place -- and
    TIDY_PASSES is the bound.

    Leaves the Tab screen SHUT, whatever state it was found in. Not tab_up():
    the caller's next move is the spawner panel or the range, and both of
    those swallow a screen left up.
    """
    dropped = 0
    stock = None
    try:
        for _ in range(TIDY_PASSES):
            stock = read_stock(ac, close=False)
            if stock is None:
                return dropped, None
            targets = stock.duplicates(keep=keep)
            if drop_unwanted:
                seen = {id(t) for t in targets}
                targets += [t for t in stock.unwanted(want)
                            if id(t) not in seen]
            if not targets:
                break
            before = _view_sig(stock)
            if verbose:
                names = Counter(t.key for t in targets)
                print(f"      [stock] dropping {len(targets)}: "
                      + ', '.join(f'{k}x{n}' if n > 1 else k
                                  for k, n in sorted(names.items())))
            for item in sorted(targets, key=_row_of, reverse=True):
                ac.discard(item)
                dropped += 1
            stock = read_stock(ac, close=False)
            if stock is None:
                return dropped, None
            # An unchanged view is REPORTED and no longer stops the loop.
            #
            # It was a stop condition on the reading that it "is what a release
            # landing somewhere other than the ground panel looks like". It is
            # also what a successful pass looks like when the rows scrolling up
            # from below happen to match the ones that left -- which a backpack
            # full of duplicates produces routinely, and which this same
            # docstring already describes happening for the count test.
            #
            # Settled by experiment rather than by argument
            # (tools/probe_backpack_depth.py): sixteen DISTINCT parts spawned
            # into an empty pack, the reader saw twelve, and dropping exactly
            # ONE moved that row out and scrolled a previously invisible one
            # in. The drags land. Stopping here cost an unattended run four
            # cells: the pack stayed deep, ext_smg sat below the window where
            # find() cannot see it, restock kept spawning more, and every SMG
            # cell failed with `magazine reads ''` while the magazine was in
            # the backpack the whole time.
            #
            # TIDY_PASSES already bounds the loop, so a drag that genuinely
            # stopped landing costs a handful of passes and says so every time
            # rather than being guessed at once.
            if _view_sig(stock) == before and verbose:
                print("      [stock] the twelve visible rows read the same "
                      "after dropping "
                      f"{len(targets)} — expected when the pack is deeper "
                      "than the window; continuing")
    finally:
        ac.ensure_tab(False)
    return dropped, stock


def spawn_missing(sc, keys, backpack=None, verbose=True):
    """Spawn everything wanted in ONE pass through the panel.

    One `give_many` for the whole list, not a loop over `give_*`. Each
    individual give_ returns the panel to fully collapsed, so N items from N
    categories used to pay 2N category clicks and 2N screenshots -- a run
    spawning a backpack and four parts opened and closed the panel five times
    over. `give_many` orders the list so items sharing a category are
    consecutive, keeps the panel open across the whole thing, and collapses
    once at the end. See control/CLAUDE.md and SpawnerControl.plan.

    The gear-first ordering that this used to enforce by hand is inside
    `plan()` now, and for the same reason: `give_gear` is driven blind off
    fixed coordinates, so it needs the panel with nothing expanded and cannot
    run in the middle of a sequence. An attachment spawns INTO the backpack,
    and with none worn it does not fail cleanly -- the parts land elsewhere,
    the inventory rows shift under the drag targets, and kitting reads back a
    part nobody asked for.
    """
    wanted = ([backpack] if backpack else []) + list(keys)
    if not wanted:
        return True
    try:
        # Sync for STABILITY only, not for a full row count.
        #
        # give_many would otherwise demand every column its list touches be
        # complete before it starts, and this panel cannot promise that: the
        # column segmentation thresholds absolute brightness through a
        # translucent panel, so facing bright terrain it reads 17 categories
        # where it read 21 a minute earlier, on a panel nobody touched. When
        # the weapon and the parts were separate trips each needed one column
        # and mostly got it; batching them made both a precondition, and the
        # first batched run died on "column 1 not drawn yet" four times over.
        #
        # Walking to one node does not need a complete map. sync() already
        # re-reads until the layout stops changing, which is the part that
        # matters, and spawn() verifies the category it lands on against the
        # catalogue's own entry count before it clicks anything.
        sc.sync()
        # switch only when a weapon is in the list. One click per gun, so it
        # lands in slot 2 because the rack is not empty -- restock() runs with
        # a gun already held. An empty rack would put it in slot 1 and the
        # press of 2 would select nothing.
        res = sc.give_many(wanted, switch=any(k in ROSTER for k in wanted))
        if not res['ok']:
            print(f"      [!] spawner: {res['error']} — re-reading the layout "
                  f"and retrying once")
            sc.menu = None
            sc.sync()
            res = sc.give_many(wanted,
                               switch=any(k in ROSTER for k in wanted))
        if not res['ok']:
            print(f"      [!] spawner: {res['error']}")
        elif verbose:
            print(f"      [stock] spawned {len(wanted)} in {res['clicks']} "
                  f"clicks: {', '.join(wanted)}")
    finally:
        # Closed on the way out because the caller's next move is the Tab
        # screen, and the spawner panel swallows Tab. give_many opens it
        # itself, so nothing here opens it.
        if not sc.ensure_panel(False):
            print("      [!] spawner panel would not close")
            return False
    return res['ok']


def restock(ac, sc, want, backpack=BACKPACK,
            drop_unwanted=True, verbose=True, also=(), loose_only=False,
            per=1):
    """Look, top up, tidy. True when the pack holds one of everything wanted.

    `want` is the set of catalogue keys this run needs on hand. Everything
    else nameable in 库存 is surplus from an earlier run and goes on the floor
    unless drop_unwanted is False.

    `also` rides along on the same panel trip without joining the keep-list:
    it is how the weapon gets spawned in the same visit as its parts instead
    of in a second one. Deliberately a separate argument -- `want` doubles as
    the keep-list and is validated to be attachments only, because the first
    version of this was handed a {slot: key} table, read every real part as
    surplus, and put the entire backpack on the floor.
    """
    want = set(want)
    # An unspawnable key is not a harmless typo here. `want` is also the
    # keep-list, so a wrong one makes every real part read as surplus and the
    # whole backpack goes on the floor — which is what happened the first time
    # harvest passed its {slot: key} table in place of the keys.
    bad = sorted(k for k in want if k not in ATTACHMENTS)
    if bad:
        print(f"      [!] not attachment keys: {', '.join(bad)} — refusing to "
              f"take stock against a list this cannot spawn")
        return False
    # Left OPEN. The only thing between this read and the tidy that needs the
    # screen down is the spawner, and that only runs when something is missing
    # — so on the common path where nothing is, the close/open pair this used
    # to pay for was two keypresses and their waits to arrive back where it
    # already was.
    stock = read_stock(ac, close=False)
    if stock is None:
        # No read means no decisions. Spawning the lot blind is the old
        # behaviour and it is what filled the backpack in the first place, so
        # say so plainly rather than pretending this went to plan. The Tab
        # screen has to be down either way — comma does nothing while it is up.
        print("      [!] could not read the backpack — spawning the whole "
              "parts list blind, duplicates and all")
        ac.ensure_tab(False)
        return spawn_missing(sc, sorted(want), backpack=backpack)
    if verbose:
        print(f"      [stock] backpack: {stock.summary()}")

    # ONE trip through the spawner, not two. The backpack and the shortfall
    # used to be separate visits with a tidy wedged between them, on the belief
    # that tidying changes what is missing. It cannot: tidy() only ever drops
    # DUPLICATES and things nothing wants, and duplicates() keeps one of each,
    # so a key that is present before the tidy is present after it. `missing`
    # is therefore already known from the read above.
    #
    # Ordering within the trip is give_many's job -- gear goes first because
    # give_gear is driven blind off fixed coordinates and needs the panel fully
    # collapsed, and an attachment spawned with no backpack worn does not fail
    # cleanly: it lands somewhere else and every later drag is aimed at a row
    # that has moved.
    need = stock.missing(want, loose_only=loose_only, per=per)
    if not stock.backpack and verbose:
        print(f"      [stock] no backpack worn — spawning {backpack}")
    if need and verbose:
        print(f"      [stock] short of {', '.join(sorted(need))}")
    batch = sorted(need) + [k for k in also if k]
    if batch or not stock.backpack:
        # Tab and the spawner panel cannot share the screen.
        ac.ensure_tab(False)
        if not spawn_missing(sc, batch,
                             backpack=None if stock.backpack else backpack,
                             verbose=verbose):
            return False
    elif verbose:
        print("      [stock] nothing missing — spawning nothing")

    # Tidy last. Anything spawned above was missing, so it cannot be a
    # duplicate, and doing it here means the drop pass sees the final contents.
    n, stock = tidy(ac, want, drop_unwanted=drop_unwanted,
                    verbose=verbose, keep=per)
    if stock is None:
        return False
    if verbose and n:
        print(f"      [stock] dropped {n}, backpack now {stock.summary()}")
    return True


def weapon_in_hand(timeout_s=3.0):
    """Is a weapon actually out? -> rounds in the magazine, or None.

    ASK FOR SOMETHING PRESENT, NEVER FOR AN ABSENCE. The tempting test is
    `AdsDetector.scoped()`, and it is wrong in the exact case that matters:
    it answers "is the crosshair gone", and the crosshair is also gone in the
    lobby, in a menu, and — the case here — when the character is EMPTY
    HANDED. Two probes have now paid for that. `probe_pitch_range` read
    `scoped=True` against the lobby screen and printed "posture unreadable"
    three times; `probe_posture_trace` did the same on 2026-08-05 against an
    empty-handed character in the range, reported `ADS up: True` for ten
    transitions in which no button was ever pressed, and concluded the posture
    icon is "NEVER readable" when the truth is there was no HUD to read.

    The ammo counter is present-or-not: digits there mean a weapon is out and
    the weapon HUD is drawn, which is the same condition the posture and fire
    mode readers need.
    """
    from detector.ammo_detector import AmmoDetector
    from detector.cropper import capture_screen
    from config import HUD_REGIONS
    det = AmmoDetector()
    y, x, h, w = HUD_REGIONS['ammo']
    t0 = time.perf_counter()
    while True:
        frame = capture_screen()
        n = det.classify({'ammo': frame[y:y + h, x:x + w]})
        if n is not None:
            return n
        if time.perf_counter() - t0 >= timeout_s:
            return None
        time.sleep(0.15)


def ensure_weapon_in_hand(ac, sc, weapon='m416', slots=(1, 2), verbose=True):
    """Put a gun in the rack if there is none, hold it, prove it. -> slot|None

    Anything reading the weapon HUD — posture, fire mode, ammo, the gun name —
    needs this first, and NONE of them report its absence as its absence: they
    report their own thing as unreadable. So this exists to be called before
    them rather than diagnosed after them.

    Entering the training range EMPTIES THE RACK, and `ensure_ready()`
    re-enters whenever it finds the game back in the lobby, so "there was a gun
    a minute ago" is not a reason to skip the check.

    `ac.hold()` rather than a bare 1/2 key press: those keys are swallowed
    while Tab is up (docs/game_quirks.md) and hold() is what brackets them with
    a close/open. The panel bracket around the spawner is not optional either —
    `collapse_all()` on a CLOSED panel collapses nothing and reports nothing,
    and give_many then clicks from a stale layout.
    """
    # WHICH gun, not just A gun. The ammo counter proves a weapon is out and
    # the HUD is drawn; it says nothing about WHOSE. A rack left loaded by the
    # previous run satisfied that and the caller carried on with the wrong
    # weapon — measured 2026-08-05: tools/fit_pitch_level.py asked for an m416,
    # got the vss still racked from the run before, and every bearing came back
    # "nothing tracks" because the red dot's patches were sitting on the VSS's
    # integral scope body. An hour of the failure looking like the scene.
    with ac.tab_up():
        racked = ac.loadout()['guns']
    for slot in slots:
        if racked.get(slot) != weapon:
            continue
        if ac.hold(slot) and weapon_in_hand() is not None:
            if verbose:
                print(f'      [stock] {weapon} already in slot {slot}')
            return slot

    if verbose:
        others = {s: g for s, g in racked.items() if g}
        print(f'      [stock] no {weapon} in the rack'
              + (f' (holds {others})' if others else ' — rack empty')
              + f' — spawning {weapon}')
    if not sc.ensure_panel(True):
        if verbose:
            print('      [stock] spawner panel would not open')
        return None
    try:
        sc.sync()
        sc.collapse_all()
        r = sc.give_many([weapon], switch=False, weapon_times=1)
        if not r['ok']:
            if verbose:
                print(f"      [stock] spawner: {r['error']}")
            return None
    finally:
        sc.ensure_panel(False)

    with ac.tab_up():
        racked = ac.loadout()['guns']
    for slot in slots:
        if racked.get(slot) != weapon:
            continue
        if ac.hold(slot):
            n = weapon_in_hand()
            if n is not None:
                if verbose:
                    print(f'      [stock] holding {weapon} in slot {slot}, '
                          f'{n} rounds')
                return slot
    if verbose:
        print(f'      [stock] spawned {weapon} but no ammo counter in slots '
              f'{list(slots)} — the gun did not reach the rack')
    return None


# ════════════════════════════════════════════════════════════
# CLI — take stock without running a whole harvest
# ════════════════════════════════════════════════════════════
#
# It came here with the rest of the file rather than staying behind as a shell
# in calibration/. Two reasons, and the second is the one that decided it:
#
#   * control/spawner.py and control/inventory.py already carry their own
#     CLIs (`pixi run spawner`, `pixi run attach`). A driver with a hand
#     entry point is the established shape, not an exception.
#   * a shell in calibration/ would have kept `from stocktake import ...`
#     working, and an import that still resolves is an import nobody
#     migrates. The point of the move is that the callers say where this
#     lives.
#
# It also stopped needing anything from calibration/ once the Rig went: this
# used to import sweep.Rig and harvest.BACKPACK, which would have been a
# control -> calibration dependency pointing the wrong way.

def main():
    import argparse

    try:            # item names are Chinese; a cp1252 console dies on 倍
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(
        description='Read the backpack; optionally tidy it and top it up.')
    ap.add_argument('--want', default='',
                    help='comma-separated catalogue keys the pack should '
                         'hold one of, e.g. comp_ar,vert_grip,red_dot,ext_ar. '
                         'Everything else nameable in it is surplus.')
    ap.add_argument('--read', action='store_true',
                    help='read and print only — drop nothing, spawn nothing')
    ap.add_argument('--keep-unwanted', action='store_true',
                    help='drop duplicates but leave parts this run does not '
                         'ask for')
    ap.add_argument('--countdown', type=int, default=5)
    args = ap.parse_args()

    want = {k.strip() for k in args.want.split(',') if k.strip()}
    bad = sorted(k for k in want if k not in ATTACHMENTS)
    if bad:
        print(f'[!] not attachments: {", ".join(bad)}')
        return 1
    if not want and not args.read:
        ap.error('give --want, or --read to look without touching anything')

    # Local: both build detectors, and importing control.stock to call
    # restock() from a run that already has its own must not pay for a second
    # set.
    from control.inventory import InventoryControl
    from control.spawner import SpawnerControl

    print('>>> Taking the foreground. Stand at an item spawner.')
    if not ensure_focus(countdown_s=args.countdown, label='the stocktake'):
        print('[!] ABORT: could not focus the game.')
        return 1

    ac = InventoryControl(verbose=False)
    sc = SpawnerControl()
    try:
        if args.read:
            stock = read_stock(ac)
            if stock is None:
                return 1
            print(f'backpack: {stock.summary()}')
            for g, slots in sorted(stock.fitted.items()):
                worn = {s: k for s, k in slots.items() if k}
                if worn:
                    print(f'gun{g}: {worn}')
            if want:
                print(f'missing:    {sorted(stock.missing(want)) or "nothing"}')
                print(f'duplicates: {[i.key for i in stock.duplicates()]}')
                print(f'unwanted:   {[i.key for i in stock.unwanted(want)]}')
            return 0
        ok = restock(ac, sc, want, drop_unwanted=not args.keep_unwanted)
        print('ok' if ok else '[!] the backpack is not what was asked for')
        return 0 if ok else 1
    finally:
        ac.close()


if __name__ == '__main__':
    sys.exit(main())
