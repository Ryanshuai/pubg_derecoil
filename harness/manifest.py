"""The cell manifest: what an unattended run intends to measure, and what
became of each one.

Written BEFORE the run, not after it. That is the whole point, and it is the
difference between this and a report:

    a report says       "these 14 cells were measured"
    a manifest says     "these 22 were planned; 14 usable, 5 failed with a
                         reason each, 3 never reached"

"never reached" is invisible in a report, and it is the state an unattended
run ends in most often -- the process died, the range evicted the character,
the night ran out. Without it, the morning cannot tell a run that finished
from a run that stopped.

One file, three jobs:

    resume      pending() returns what has not been measured yet, so a killed
                run is restarted rather than redone
    report      summary() is the morning read
    index       each failed cell names its evidence directory

Every mark() writes the file. Not at the end, not every N -- a manifest that
only becomes true when the process exits cleanly is worthless for the case it
exists to cover. A cell takes minutes; a 20 KB rewrite does not matter.

Modelled on the feature list in Anthropic's long-running-agent harness, where
every feature starts marked failing so the next session can see the shape of
the whole job rather than only what the last one happened to touch.
"""
import json
import os
import tempfile
from datetime import datetime

# A cell's life. `skipped` is deliberate exclusion (not on the roster, not
# full-auto); it is NOT a failure and must not count toward the halt streak.
UNMEASURED = 'unmeasured'
USABLE = 'usable'
FAILED = 'failed'
SKIPPED = 'skipped'

VERSION = 1


def cell_id(weapon, posture, sight, config='bare'):
    """The key a cell is addressed by. Stable across runs, so two nights of
    the same plan can be diffed.

    ⚠ CONFIG IS PART OF THE KEY, and it has to be: a night that measures
    m416 in all eight slot combinations would otherwise write eight cells
    that all address as `m416|standing|red_dot`, so mark() would move the
    same row eight times and seven of the results would be invisible.
    'bare' is folded away so ids written before configs existed still match.
    """
    tail = '' if config in (None, '', 'bare') else f'|{config}'
    return f'{weapon}|{posture}|{sight}{tail}'


class Manifest:
    """The plan and its outcomes. Backed by one JSON file."""

    def __init__(self, path, data):
        self.path = path
        self.data = data

    # ── construction ──

    @classmethod
    def build(cls, path, cells, axis='weapon', params=None):
        """A fresh manifest with every cell UNMEASURED.

        `cells` is an iterable of (weapon, posture, sight) or
        (weapon, posture, sight, config).
        """
        rows = []
        for cell in cells:
            weapon, posture, sight = cell[:3]
            config = cell[3] if len(cell) > 3 else 'bare'
            rows.append({'id': cell_id(weapon, posture, sight, config),
                         'weapon': weapon, 'posture': posture, 'sight': sight,
                         'config': config,
                         'state': UNMEASURED, 'attempts': 0,
                         'verdict': None, 'evidence': None, 'updated': None})
        data = {'version': VERSION, 'axis': axis,
                'created': datetime.now().isoformat(timespec='seconds'),
                'params': dict(params or {}), 'cells': rows}
        m = cls(path, data)
        m.save()
        return m

    @classmethod
    def load(cls, path):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if data.get('version') != VERSION:
            raise ValueError(f'{path}: manifest version {data.get("version")}, '
                             f'this code writes {VERSION}')
        return cls(path, data)

    @classmethod
    def open_or_build(cls, path, cells, axis='weapon', params=None):
        """Resume if the file is there, otherwise start one.

        The resume path deliberately does NOT reconcile the plan against the
        file. A run whose plan changed is a different run and belongs in a
        different manifest -- silently merging them is how a night's data ends
        up labelled with the wrong parameters.
        """
        if os.path.exists(path):
            return cls.load(path), True
        return cls.build(path, cells, axis=axis, params=params), False

    # ── state ──

    def save(self):
        """Atomic: a crash mid-write must not leave an unreadable manifest,
        because the manifest is exactly what the next run reads to know where
        it got to."""
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or '.',
                    exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(
            os.path.abspath(self.path)) or '.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @property
    def cells(self):
        return self.data['cells']

    def get(self, cid):
        for c in self.cells:
            if c['id'] == cid:
                return c
        return None

    def pending(self):
        """Cells still to measure, in plan order."""
        return [c for c in self.cells if c['state'] == UNMEASURED]

    def mark(self, cid, state, verdict=None, evidence=None, attempts=None):
        """Record what became of a cell, and write the file."""
        c = self.get(cid)
        if c is None:
            raise KeyError(f'no cell {cid!r} in {self.path}')
        c['state'] = state
        if verdict is not None:
            c['verdict'] = verdict
        if evidence is not None:
            c['evidence'] = evidence
        if attempts is not None:
            c['attempts'] = attempts
        c['updated'] = datetime.now().isoformat(timespec='seconds')
        self.save()
        return c

    # ── halt ──

    def consecutive_failures(self):
        """Failures at the END of what has been attempted so far.

        Counted over the cells that have a verdict, in plan order, so it is a
        property of the manifest rather than of a counter the loop has to
        remember -- which matters because a resumed run has no counter.

        SKIPPED does not break the streak and does not extend it: it was never
        attempted.
        """
        n = 0
        for c in self.cells:
            if c['state'] == SKIPPED or c['state'] == UNMEASURED:
                continue
            n = n + 1 if c['state'] == FAILED else 0
        return n

    # ── reading it back ──

    def counts(self):
        out = {UNMEASURED: 0, USABLE: 0, FAILED: 0, SKIPPED: 0}
        for c in self.cells:
            out[c['state']] = out.get(c['state'], 0) + 1
        return out

    def by_reason(self):
        """{why: [cell id, ...]} over the failures. The morning's routing
        table: each `why` names one probe."""
        out = {}
        for c in self.cells:
            if c['state'] != FAILED:
                continue
            why = (c.get('verdict') or {}).get('why') or 'unknown'
            out.setdefault(why, []).append(c['id'])
        return out

    def summary(self):
        n = self.counts()
        lines = [f"{self.data['axis']} axis, planned {len(self.cells)}, "
                 f"created {self.data['created']}",
                 f"  usable {n[USABLE]}   failed {n[FAILED]}   "
                 f"never reached {n[UNMEASURED]}   skipped {n[SKIPPED]}"]
        for why, ids in sorted(self.by_reason().items(),
                               key=lambda kv: -len(kv[1])):
            lines.append(f"  failed:{why:<10} {len(ids):2d}  "
                         f"{', '.join(ids[:4])}"
                         + (' …' if len(ids) > 4 else ''))
        return '\n'.join(lines)
