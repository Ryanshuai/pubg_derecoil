"""What the game refused to fit, learned by trying.

detector/attachment_catalog.py says what goes on what. It is hand-built, it
was right on the day it was written, and the game keeps moving: weapons get
added, rails get taken away, a part's compatibility line changes in a patch.
When it drifts, the symptom is a harvest run that spawns a gun, fails to kit
it, and moves on without firing — half a roster producing no data because one
table is stale.

So every kit failure is counted here, across runs, and a (weapon, slot, part)
that has failed in FAILS_TO_BELIEVE separate cells is REPORTED as a probable
catalogue error.

THIS FILE NEVER OVERRIDES THE CATALOGUE. It is evidence, not authority.
detector/attachment_catalog.py stays hand-edited, because a wrong entry there
usually means a compatibility line was misread, and the same misreading is
probably sitting on other weapons too — silently routing around one instance
hides the pattern. The end of a run prints what to go and check.

What DOES happen automatically is in-flight degradation: a slot that will not
take its part is dropped from the config and the cell is measured without it,
so a stale catalogue costs a slot rather than a whole weapon's data.

    from kit_facts import KitFacts
    kf = KitFacts()
    kf.note_failure('famas', 'grip', 'vert_grip')
    kf.refuted('famas', 'grip', 'vert_grip')   # enough evidence to go look?
    kf.save()

Why more than one failure: a drag can miss for reasons that have nothing to do
with compatibility — the row moved under it, the inventory did not sync, the
game ate the click. One of those should cost a retry, not a table entry. Two
separate cells failing the same way is not a coincidence.

Nothing here ever learns a POSITIVE either. A part that fits when the
catalogue says it cannot is the same kind of bug, pointing the other way.
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detector.attachment_catalog import fits, has_slot

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PATH = os.path.join(ROOT, 'docs', 'compat', 'kit_facts.json')

# Separate cells that must fail the same (weapon, slot, part) before it is
# believed to be impossible rather than unlucky.
FAILS_TO_BELIEVE = 2


class KitFacts:
    """The catalogue, corrected by what the game actually allowed."""

    def __init__(self, path=PATH):
        self.path = path
        self.data = {}
        self.dirty = False
        if os.path.exists(path):
            try:
                self.data = json.load(open(path, encoding='utf-8'))
            except Exception as e:
                print(f"[kit_facts] {path} unreadable ({e}) — starting empty")

    # ── queries ──

    @staticmethod
    def _key(weapon, slot, part):
        return f'{weapon}.{slot}.{part}'

    def failures(self, weapon, slot, part):
        return int(self.data.get(self._key(weapon, slot, part), {})
                   .get('failures', 0))

    def refuted(self, weapon, slot, part):
        """Has the game refused this often enough to be worth going to look?

        Deliberately not consulted by anything that plans a run — see the
        module docstring. This answers "should a human check the catalogue",
        not "should the tool skip this".
        """
        return self.failures(weapon, slot, part) >= FAILS_TO_BELIEVE

    @staticmethod
    def can_fit(weapon, slot, part):
        """The catalogue's answer, unmodified."""
        return bool(part) and has_slot(weapon, slot) and fits(weapon, part)

    # ── learning ──

    def note_failure(self, weapon, slot, part, note=''):
        """One cell could not get `part` into `slot`. Returns the new count."""
        k = self._key(weapon, slot, part)
        rec = self.data.setdefault(k, {'weapon': weapon, 'slot': slot,
                                       'part': part, 'failures': 0})
        rec['failures'] += 1
        rec['last'] = datetime.now().isoformat(timespec='seconds')
        if note:
            rec['note'] = note
        self.dirty = True
        return rec['failures']

    def note_success(self, weapon, slot, part):
        """It went on after all. Clears the count — whatever went wrong before
        was not compatibility, and leaving a stale strike behind would blacklist
        a working combination on the next unlucky drag."""
        k = self._key(weapon, slot, part)
        if k in self.data:
            del self.data[k]
            self.dirty = True

    def save(self):
        if not self.dirty:
            return
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2,
                      sort_keys=True)
        os.replace(tmp, self.path)
        self.dirty = False

    def settled(self):
        """Strikes the catalogue has since caught up with. Returns the keys.

        Evidence outlives the thing it was evidence for. The js9's vertical
        grip failed three times, calibration/scan_compat.py later measured that
        the js9 has no lower rail at all, and the catalogue was corrected the
        same afternoon -- after which the report went on telling a human to go
        and check a combination the run already refuses to attempt. A standing
        instruction to look at something already fixed is worse than silence,
        because it makes the whole report easy to stop reading.
        """
        return [k for k, v in self.data.items()
                if not self.can_fit(v['weapon'], v['slot'], v['part'])]

    def retire_settled(self):
        """Drop strikes the catalogue now agrees with. Returns what was dropped."""
        gone = self.settled()
        for k in gone:
            del self.data[k]
        if gone:
            self.dirty = True
        return gone

    def report(self):
        """What to go and check by hand. Nothing here was acted on."""
        if not self.data:
            return
        settled = set(self.settled())
        live = [v for k, v in self.data.items() if k not in settled]
        believed = [v for v in live if v['failures'] >= FAILS_TO_BELIEVE]
        pending = [v for v in live if v['failures'] < FAILS_TO_BELIEVE]
        if settled and not live:
            print(f"\n[kit_facts] {len(settled)} past failure(s) now agree with "
                  f"the catalogue — nothing left to check. Clear them with "
                  f"`pixi run python calibration/kit_facts.py --retire`.")
            return
        if not live:
            return
        print('\n' + '=' * 66)
        print('KIT FAILURES — the catalogue may be wrong. Nothing was '
              'auto-corrected.')
        print('=' * 66)
        if believed:
            print(f"  {len(believed)} combination(s) have now failed "
                  f"{FAILS_TO_BELIEVE}+ times. Check these in the game and "
                  f"edit detector/attachment_catalog.py:")
            for v in sorted(believed, key=lambda x: (x['weapon'], x['slot'])):
                print(f"    {v['weapon']:<8} {v['slot']:<9} {v['part']:<16} "
                      f"failed {v['failures']}x   last {v.get('last', '?')}")
        if pending:
            print(f"  {len(pending)} failed once — could be a missed drag; "
                  f"left to be retried rather than believed:")
            for v in sorted(pending, key=lambda x: (x['weapon'], x['slot'])):
                print(f"    {v['weapon']:<8} {v['slot']:<9} {v['part']}")
        if settled:
            print(f"  ({len(settled)} older strike(s) hidden — the catalogue "
                  f"already forbids them)")
        print(f"  evidence -> {os.path.relpath(self.path)}")


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    kf = KitFacts()
    print(f'{kf.path}: {len(kf.data)} entr{"y" if len(kf.data)==1 else "ies"}')
    if '--retire' in sys.argv:
        gone = kf.retire_settled()
        kf.save()
        print(f'retired {len(gone)}: {", ".join(gone) or "nothing"}')
    kf.report()
