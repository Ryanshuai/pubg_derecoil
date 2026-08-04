"""ensure_kit's diff, offline. No game, no screen, no hardware.

    pixi run kit

"Put this set of attachments on the gun and prove it landed" existed four
times over -- harvest.Kitter.apply, capture_ads.AttachEquip._fit,
collect_templates.fit_one, weapon_axis.fit_mags -- with a different retry
count and a different idea of "already correct" in each. control/inventory.py
now has one, and the part worth testing is the part that never needed the game
to begin with: given what the gun IS wearing and what it SHOULD be wearing,
which actions is that, in which order.

Two halves:

  plan_kit / kit_faults / slot_matches   pure functions, called directly
  ensure_kit                             driven against a fake screen, so the
                                         ordering, the hold() and the restock
                                         hook are checked without a Pico

The shape of the assertions is deliberate: they pin the action SEQUENCE, not
just the end state. A kitter that reaches the right loadout by taking
everything off and putting it all back on is wrong in the way that matters --
every extra drag is another chance for the game to drop a part on the floor,
and the floor is what the next weapon auto-fits from.
"""
import contextlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError):
    pass

from control.inventory import (InventoryControl, at_ground, at_inv, at_slot,
                               kit_faults, plan_kit, slot_matches)
from detector.attachment_catalog import ATTACHMENTS

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"}  {name:<52} {got!r}'
          + ('' if ok else f'\n{"":>62}!= {want!r}'))
    if not ok:
        FAILS.append(name)


def truthy(name, got):
    ok = bool(got)
    print(f'  {"ok  " if ok else "FAIL"}  {name:<52} {got!r}')
    if not ok:
        FAILS.append(name)


def moves(plan):
    """The plan as a comparable sequence: (action, slot, key)."""
    return [(s['action'], s['slot'], s['key']) for s in plan['steps']]


ASSET = {k: v['asset'] for k, v in ATTACHMENTS.items()}
BY_ASSET = {v: k for k, v in ASSET.items() if v}

# A bare M416: every slot it has, all empty. read_slots() answers '' for a slot
# that is drawn empty AND for one the weapon does not have, which is why
# plan_kit never has to tell those apart.
BARE = {'scope': '', 'muzzle': '', 'grip': '', 'magazine': '', 'stock': ''}


def worn(**kw):
    """A slot readback, by catalogue key. worn(muzzle='comp_ar')."""
    out = dict(BARE)
    out.update({s: (ASSET[k] or f'<{k}>') for s, k in kw.items()})
    return out


# ════════════════════════════════════════════════════════════
print('\n=== the two vocabularies meet in slot_matches ===')
# read_slots answers in template stems, callers speak catalogue keys.
check('comp_ar reads as its own asset',
      slot_matches('Muzzle_Compensator_Large_C', 'comp_ar'), True)
check('empty slot matches nothing', slot_matches('', 'comp_ar'), False)
check('a different muzzle is not it',
      slot_matches('Muzzle_Suppressor_Large_C', 'comp_ar'), False)
# The bank ships stems the catalogue does not name: 'laser' is catalogued
# Lower_LaserPointer_C and matched as SideRail_LaserPointer_C. An exact
# comparison here reads a fitted laser as absent and loops forever.
check('laser, catalogued and matched under different stems',
      slot_matches('SideRail_LaserPointer_C', 'laser'), True)
# The pair this must never confuse: weapon_axis swaps 加长快速弹匣 for 扩容弹匣
# and calls the difference a measurement.
check('quickext is not ext',
      slot_matches('Magazine_ExtendedQuickDraw_Large_C', 'ext_ar'), False)
check('ext is not quickext',
      slot_matches('Magazine_Extended_Large_C', 'quickext_ar'), False)
check('the SMG magazine is not the AR one',
      slot_matches('Magazine_Extended_Medium_C', 'ext_ar'), False)


# ════════════════════════════════════════════════════════════
print('\n=== a slot that is already right is not touched ===')
p = plan_kit({'muzzle': 'comp_ar'}, worn(muzzle='comp_ar'),
             {at_inv(0): 'comp_ar'}, weapon='m416')
check('nothing to do', moves(p), [])
check('  and it says so', p['unchanged'], ['muzzle'])
check('  ok', p['ok'], True)

p = plan_kit({'muzzle': 'comp_ar', 'grip': 'vert_grip', 'stock': None},
             worn(muzzle='comp_ar', grip='vert_grip'),
             {at_inv(0): 'comp_ar'}, weapon='m416')
check('a whole kit already fitted', moves(p), [])

# An empty slot asked to be empty is also already right — this is the case
# that must not turn into a drag, because there is nothing to drag.
p = plan_kit({'grip': None, 'stock': None}, BARE, {}, weapon='m416')
check('empty slots asked to stay empty', moves(p), [])
check('  counted as unchanged', p['unchanged'], ['grip', 'stock'])


# ════════════════════════════════════════════════════════════
print('\n=== an empty slot gets one fit, from the nearest copy ===')
p = plan_kit({'muzzle': 'comp_ar'}, BARE, {at_inv(3): 'comp_ar'},
             weapon='m416')
check('one equip', moves(p), [('equip', 'muzzle', 'comp_ar')])
check('  out of 库存 row 3', p['steps'][0]['src'], at_inv(3))
check('  onto an empty slot', p['steps'][0]['was'], '')

p = plan_kit({'muzzle': 'comp_ar'}, BARE,
             {at_ground(0): 'comp_ar', at_inv(4): 'comp_ar'}, weapon='m416')
check('库存 is preferred over the floor', p['steps'][0]['src'], at_inv(4))

p = plan_kit({'muzzle': 'comp_ar'}, BARE,
             {at_inv(4): 'comp_ar', at_inv(1): 'comp_ar'}, weapon='m416')
check('and the lowest row of it', p['steps'][0]['src'], at_inv(1))

p = plan_kit({'muzzle': 'comp_ar'}, BARE, None, weapon='m416')
check('found=None plans without checking the panels',
      moves(p), [('equip', 'muzzle', 'comp_ar')])
check('  with no source picked', p['steps'][0]['src'], None)
check('  and nothing declared missing', p['missing'], [])


# ════════════════════════════════════════════════════════════
print('\n=== the wrong part is ONE step, not two ===')
# A part dropped on an occupied slot swaps, and the displaced one goes back to
# the panel the new one came from (docs/game_quirks.md). Unequip-then-equip is
# twice the drags and twice the chances to lose a part on the floor.
p = plan_kit({'muzzle': 'comp_ar'}, worn(muzzle='supp_ar'),
             {at_inv(0): 'comp_ar'}, weapon='m416')
check('swap in place', moves(p), [('equip', 'muzzle', 'comp_ar')])
check('  and it remembers what it displaced', p['steps'][0]['was'],
      ASSET['supp_ar'])


# ════════════════════════════════════════════════════════════
print('\n=== None means the slot MUST be empty ===')
# Not "leave it alone". PUBG auto-fits from the backpack, so an unmanaged slot
# is whatever the last strip left lying around -- the first BARE run this
# project measured came back wearing a cheek pad, and a cheek pad damps recoil.
p = plan_kit({'grip': None}, worn(grip='vert_grip'), {}, weapon='m416')
check('one unequip', moves(p), [('unequip', 'grip', None)])
check('  no source needed', p['steps'][0]['src'], None)

p = plan_kit({'muzzle': 'comp_ar', 'grip': None, 'stock': None},
             worn(grip='vert_grip', stock='tactical_stock'),
             {at_inv(0): 'comp_ar'}, weapon='m416')
check('removals go before fits', moves(p),
      [('unequip', 'grip', None), ('unequip', 'stock', None),
       ('equip', 'muzzle', 'comp_ar')])

# A slot the weapon does not have reads exactly like one that is drawn empty,
# so demanding emptiness there is free rather than an error.
p = plan_kit({'grip': None}, BARE, {}, weapon='akm')   # the AKM has no grip
check('emptiness is not refused on a slot the gun lacks', p['ok'], True)
check('  and costs nothing', moves(p), [])


# ════════════════════════════════════════════════════════════
print('\n=== a part that is not on screen is reported, not skipped ===')
p = plan_kit({'muzzle': 'comp_ar', 'grip': 'vert_grip'}, BARE,
             {at_inv(0): 'vert_grip'}, weapon='m416')
check('the missing one still gets a step', moves(p),
      [('equip', 'muzzle', 'comp_ar'), ('equip', 'grip', 'vert_grip')])
check('  carrying why', p['steps'][0]['error'], 'not on screen')
check('  and named in missing', p['missing'], ['comp_ar'])
check('  which makes the plan not ok', p['ok'], False)
check('  the one that IS there is still planned', p['steps'][1]['error'], None)
truthy('  and the summary says which slot', 'muzzle' in (p['error'] or ''))


# ════════════════════════════════════════════════════════════
print('\n=== the catalogue gate, before the mouse moves ===')
# A part released over a slot the weapon does not have goes on the floor, so
# these have to be refused at planning time, not discovered by a readback.
for name, want, weapon, needle in [
    ('gun has no such slot', {'grip': 'vert_grip'}, 'akm', 'no grip slot'),
    ('gun refuses this part', {'muzzle': 'comp_ar'}, 'groza',
     'does not take'),
    ('part is for another slot', {'grip': 'comp_ar'}, 'm416', 'is a muzzle'),
    ('no such attachment', {'muzzle': 'comp_xyz'}, 'm416', 'unknown attach'),
    ('no such slot at all', {'bayonet': 'comp_ar'}, 'm416', 'not one of'),
    ('no such weapon', {'muzzle': 'comp_ar'}, 'sten', 'unknown weapon'),
]:
    p = plan_kit(want, BARE, {at_inv(0): 'comp_ar', at_inv(1): 'vert_grip'},
                 weapon=weapon)
    err = p['steps'][0]['error'] or ''
    ok = not p['ok'] and needle in err
    print(f'  {"ok  " if ok else "FAIL"}  {name:<52} {err!r}')
    if not ok:
        FAILS.append(name)

# Without a weapon key there is no gate at all — same as plan_equip.
p = plan_kit({'grip': 'vert_grip'}, BARE, {at_inv(0): 'vert_grip'})
check('no weapon key, no compatibility gate', p['ok'], True)


# ════════════════════════════════════════════════════════════
print('\n=== kit_faults reads the answer back ===')
check('everything as asked', kit_faults({'muzzle': 'comp_ar', 'grip': None},
                                        worn(muzzle='comp_ar')), [])
f = kit_faults({'grip': None}, worn(grip='vert_grip'))
check('a slot that should be empty is not', [x['slot'] for x in f], ['grip'])
f = kit_faults({'muzzle': 'comp_ar'}, worn(muzzle='supp_ar'))
check('a slot holding the wrong thing', [x['key'] for x in f], ['comp_ar'])
check('  which is a real fault', f[0]['verifiable'], True)
# A part with no icon template: the slot can be read as occupied, never as
# holding that part. "Cannot be proven" is a different verdict from "is wrong".
#
# THE KEY IS SYNTHETIC ON PURPOSE. This used to name brake_ar, and it went red
# the day brake_ar got a template — the assertion was riding on which
# attachments happened to be uncovered rather than on the branch it meant to
# cover, and today every entry in ATTACHMENTS has an asset. The branch is not
# dead: it is what the next attachment the game adds will land in.
ATTACHMENTS['__untemplated__'] = {'slot': 'muzzle', 'zh': 'x',
                                  'asset': None, 'classes': ('AR',)}
f = kit_faults({'muzzle': '__untemplated__'}, worn(muzzle='comp_ar'))
check('an untemplated part cannot be verified', f[0]['verifiable'], False)
check('  and every real attachment now HAS a template',
      [k for k, v in ATTACHMENTS.items()
       if not v.get('asset') and not k.startswith('__')], [])
del ATTACHMENTS['__untemplated__']


# ════════════════════════════════════════════════════════════
# ensure_kit against a fake screen
# ════════════════════════════════════════════════════════════

class FakeTab:
    """Just enough of InventoryControl for ensure_kit to drive.

    The game's own rule is modelled: fitting onto an occupied slot swaps, and
    the displaced part lands back in the panel the new one came from, as a NEW
    row. That is what makes a plan's source rows stale the moment anything
    moves, and it is why ensure_kit re-finds by name rather than trusting the
    row it planned with.
    """

    # The real planning helpers, borrowed. Only the surface that touches the
    # screen, the mouse and the keyboard is faked -- everything that decides
    # anything is the code that ships.
    _kit_plan = InventoryControl._kit_plan
    _kit_run = InventoryControl._kit_run
    _worn_of = InventoryControl._worn_of

    def __init__(self, slots, loose, weapon='m416'):
        self.worn = dict(slots)
        self.loose = dict(loose)
        self.guns = {1: weapon, 2: weapon}
        self.held = None
        self.log = []               # every action taken, in order
        self.looks = 0

    def _log(self, msg):
        pass

    @contextlib.contextmanager
    def tab_up(self, restore=True):
        self.log.append(('tab', 'up'))
        yield True

    def ensure_tab(self, want, tries=3):
        self.log.append(('tab', want))
        return True

    def hold(self, gun):
        self.log.append(('hold', gun))
        self.held = gun
        return True

    def look(self):
        self.looks += 1
        return dict(self.loose)     # a {loc: key} map, not a TabView

    def read_slots(self, gun=None):
        return dict(self.worn)

    def _spill(self, asset, panel):
        """A displaced part lands back in `panel` as a new row, at the end."""
        if not asset:
            return
        row = max([r for p, r in self.loose if p == panel] + [-1]) + 1
        self.loose[(panel, row)] = BY_ASSET.get(asset, asset)

    def _take(self, src):
        """Pull a row out. Everything below it moves UP one -- the whole
        reason a source row is only valid for the pass it came from."""
        panel, row = src
        if src not in self.loose:
            return
        self.loose.pop(src)
        for p, r in sorted([k for k in self.loose if k[0] == panel
                            and k[1] > row]):
            self.loose[(p, r - 1)] = self.loose.pop((p, r))

    def _rec(self, ok=True, src=None, dst=None, error=None):
        return {'ok': ok, 'verified': True, 'src': src, 'dst': dst,
                'checks': [], 'attempts': 1, 'error': error}

    def equip(self, gun, slot=None, src=None, att=None, weapon=None,
              retries=1, gesture='auto'):
        self.log.append(('equip', slot, att, src))
        self._take(src)
        self._spill(self.worn.get(slot, ''), src[0])
        self.worn[slot] = ASSET.get(att) or f'<{att}>'
        return self._rec(src=src, dst=at_slot(gun, slot))

    def unequip(self, gun, slot, to=None, retries=1):
        self.log.append(('unequip', slot))
        self._spill(self.worn.get(slot, ''), 'inventory')
        self.worn[slot] = ''
        return self._rec(src=at_slot(gun, slot), dst=at_inv())


def run(fake, want, **kw):
    return InventoryControl.ensure_kit(fake, 2, want, settle=0, **kw)


print('\n=== ensure_kit: nothing to do means nothing done ===')
fk = FakeTab(worn(muzzle='comp_ar', grip='vert_grip'), {at_inv(0): 'supp_ar'})
r = run(fk, {'muzzle': 'comp_ar', 'grip': 'vert_grip', 'stock': None})
check('ok', r['ok'], True)
check('no steps', r['steps'], [])
check('nothing was dragged, clicked or switched to',
      [c for c in fk.log if c[0] in ('equip', 'unequip', 'hold')], [])
check('all three slots already right', r['unchanged'],
      ['muzzle', 'grip', 'stock'])


print('\n=== ensure_kit: one wrong slot, one action ===')
fk = FakeTab(worn(muzzle='supp_ar'), {at_inv(0): 'comp_ar'})
r = run(fk, {'muzzle': 'comp_ar'})
check('ok', r['ok'], True)
check('one step', [(s['action'], s['slot']) for s in r['steps']],
      [('equip', 'muzzle')])
# hold() first: right-click is the only gesture that lands on this screen, and
# it reaches the HELD weapon only (docs/game_quirks.md, 4/4 against 0/4).
check('the gun was taken in hand first',
      [c for c in fk.log if c[0] in ('hold', 'equip')],
      [('hold', 2), ('equip', 'muzzle', 'comp_ar', at_inv(0))])
check('the record carries both the plan and the drag',
      sorted(set(r['steps'][0]) & {'action', 'slot', 'key', 'was', 'ok',
                                   'verified', 'attempts', 'error'}),
      ['action', 'attempts', 'error', 'key', 'ok', 'slot', 'verified', 'was'])
check('the readback agrees', r['worn']['muzzle'], ASSET['comp_ar'])
check('no faults', r['bad'], [])


print('\n=== ensure_kit: strip and fit in one call ===')
fk = FakeTab(worn(muzzle='supp_ar', grip='vert_grip', stock='tactical_stock'),
             {at_inv(0): 'comp_ar'})
r = run(fk, {'muzzle': 'comp_ar', 'grip': None, 'stock': None})
check('ok', r['ok'], True)
check('removals first, then the fit',
      [c for c in fk.log if c[0] in ('equip', 'unequip')],
      [('unequip', 'grip'), ('unequip', 'stock'),
       ('equip', 'muzzle', 'comp_ar', at_inv(0))])
check('the gun ends up as asked',
      {s: r['worn'][s] for s in ('muzzle', 'grip', 'stock')},
      {'muzzle': ASSET['comp_ar'], 'grip': '', 'stock': ''})
# The two unequips pushed rows into 库存 under the planned source, so the
# equip's row had to be re-found rather than trusted.
truthy('the screen was re-read after something moved', fk.looks > 1)


print('\n=== ensure_kit: rows move under the plan ===')
# Pulling 库存 row 0 out shifts row 1 up into its place. A plan that trusted
# the row it was made with would fit the muzzle and then drag whatever slid
# into row 1 -- so the second source has to be re-found by name.
fk = FakeTab(BARE, {at_inv(0): 'comp_ar', at_inv(1): 'vert_grip'})
r = run(fk, {'muzzle': 'comp_ar', 'grip': 'vert_grip'})
check('ok', r['ok'], True)
check('the second fit followed its part up the list',
      [c for c in fk.log if c[0] == 'equip'],
      [('equip', 'muzzle', 'comp_ar', at_inv(0)),
       ('equip', 'grip', 'vert_grip', at_inv(0))])
check('both landed', {s: r['worn'][s] for s in ('muzzle', 'grip')},
      {'muzzle': ASSET['comp_ar'], 'grip': ASSET['vert_grip']})
check('and the gun was taken in hand once, not per part',
      [c for c in fk.log if c[0] == 'hold'], [('hold', 2)])


print('\n=== ensure_kit: a part nobody has ===')
fk = FakeTab(BARE, {})
r = run(fk, {'muzzle': 'comp_ar'})
check('not ok', r['ok'], False)
check('missing names the part', r['missing'], ['comp_ar'])
check('nothing was attempted', [c for c in fk.log if c[0] == 'equip'], [])
check('the step says why', r['steps'][0]['error'], 'not on screen')
check('and the readback backs it up', [b['slot'] for b in r['bad']], ['muzzle'])


print('\n=== ensure_kit: the restock hook ===')
fk = FakeTab(BARE, {})
asked = []


def restock(keys):
    asked.append(list(keys))
    fk.loose[at_inv(0)] = 'comp_ar'      # the hook put it in the backpack


r = run(fk, {'muzzle': 'comp_ar'}, restock=restock)
check('the hook was asked for exactly what was short', asked, [['comp_ar']])
# It drives the spawner panel, which the Tab screen sits on top of.
check('and was called with Tab closed, then reopened',
      [c for c in fk.log if c[0] == 'tab'],
      [('tab', 'up'), ('tab', False), ('tab', True)])
check('then the part went on', r['ok'], True)
check('  in one action', [(s['action'], s['slot']) for s in r['steps']],
      [('equip', 'muzzle')])

fk = FakeTab(BARE, {})
r = run(fk, {'muzzle': 'comp_ar'})
check('no hook, no restock: still a clean failure', r['ok'], False)


print('\n=== ensure_kit: the catalogue gate stops it before the mouse ===')
fk = FakeTab(BARE, {at_inv(0): 'vert_grip'}, weapon='akm')
r = run(fk, {'grip': 'vert_grip'})
check('not ok', r['ok'], False)
check('nothing was dragged',
      [c for c in fk.log if c[0] in ('equip', 'unequip', 'hold')], [])
truthy('the step says the AKM has no grip slot',
       'no grip slot' in (r['steps'][0]['error'] or ''))


print()
if FAILS:
    print(f'{len(FAILS)} FAILED: {", ".join(FAILS)}')
    sys.exit(1)
print('all ok')
