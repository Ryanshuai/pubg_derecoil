"""The shared contract between the calibration skills: what a capture run is.

Driving the game is the expensive part of every calibration here — it needs
the foreground, the one Pico, and a slot in the queue behind whatever other
agent is running. Analysing captures is free and repeatable. So the rule is
**capture once, analyse many times**, and this module is the format that makes
a run readable by a skill that did not produce it.

    run = CaptureRun.create('slot_scan', note='30 live weapons')
    run.add(frame, 'm416', weapon='m416', slots={...})            # observed
    run.add_fit(frame, 'm416', 'grip', 'vert_grip')               # requested
    run.save()

    run = CaptureRun.load('20260802_155222')      # native, by stamp
    run = CaptureRun.load_dir(some_directory)     # native OR legacy, by path
    run.entries                # everything
    run.labelled()             # ONLY samples whose label was specified

WHY `labelled()` EXISTS, AND WHY IT IS NOT A FILTER YOU APPLY YOURSELF:

A template cannot be validated against samples that a template labelled. The
failure is on record in detector/CLAUDE.md: a drifted `Lower_ThumbGrip_C` made
Mk12's grip read as `laser` — in-catalogue, confident, wrong. Detectors do not
fail loudly when they drift; they return a plausible answer. So a sample whose
identity came from `AttachmentDetector` is worthless for judging
`AttachmentDetector`.

The only non-circular label is a part that was **fitted on purpose**:
`InventoryControl.equip(..., att='vert_grip')` returning success means that tile
holds the real rendering of `vert_grip`, whatever any template thinks.

Both kinds live in the same run, because the same screenshot carries both — a
slot-geometry scan also photographs whatever PUBG auto-fitted. Rather than
warn about the difference in prose that each skill copies and lets drift, every
label carries `source`, and `labelled()` hands back only `LABEL_REQUESTED`.
Code that wants ground truth cannot silently get the other kind.

    LABEL_REQUESTED  we asked for this and something confirmed it. Ground truth.
    LABEL_DETECTED   a detector read it. Context and cross-checks only.
    (no label)       nobody looked. See below — this is the third state, and
                     leaving it out is what produced the two bad runs.

THE THIRD STATE IS NOT AN OVERSIGHT. Two sources describe who established a
fact; neither describes "we asked and never checked". That gap is exactly
where the ADS dataset went wrong: capture_ads tapped the right button, wrote
`state: ads` into every record, and nothing on earth verified that a sight
picture ever appeared. Run 20260801_222936 is 64 frames of shoulder aim
labelled ADS, and 20260802_015545 is 40 frames of the wrong weapon labelled
`iron`. Had those been written as LABEL_REQUESTED the format would have made
them *worse* — confidently wrong instead of merely unlabelled.

So: **a label exists only when someone looked, and `source` says who.** An
unverified intention gets no label; it stays a plain fact on the entry, where
it reads as "this is what the capture procedure did", which is true, rather
than "this is what the screen showed", which nobody knows. `add_fit` says the
same thing from the other side: "a label that records an intention rather than
an outcome is worse than no label".

A label is `{slot, asset, source}`. `slot` names WHICH ASPECT of the capture is
being labelled — an attachment slot for a kit scan, `'scope'` for an ADS run —
and `asset` names what it was, with `''` meaning "confirmed empty" and `None`
meaning "something is there and nobody named it".

Layout, under <run root>/<kind>/<stamp>/:
    manifest.json      the record below
    <name>.png         captures, referenced by manifest entries. May sit in a
                       subdirectory and may be .jpg — see `add`.

WHERE A RUN LIVES IS PART OF ITS MEANING, so `create` takes an explicit `path`
and two producers use it. docs/runs/ is the default root, but
tools/test_tab_open.py reads the directory as ground truth: `docs/ads/runs/**`
is gameplay with Tab SHUT, `docs/runs/**` is a capture OF the Tab screen.
Filing an ADS run under docs/runs/ would silently relabel 400 frames in that
corpus and fail a regression that has nothing to do with this module. What
unifies is the manifest, not the path.

READING THE TWO PRE-CaptureRun FORMATS
--------------------------------------
`load_dir` also reads the two run shapes that predate this module:

    docs/ads/runs/<stamp>/          index.jsonl (one JSON per frame) + meta.json
    docs/attachments/runs/<stamp>/  index.json  ({..., 'crops': [...]})

An adapter, deliberately, and NOT a one-time conversion. Those 610 frames are
unreproducible without tens of minutes of game time, and a converter would
produce a lossy copy of an original that cannot be re-made. Old runs are read
in place, byte for byte untouched, and consumers migrate one at a time.

**Every label recovered from a legacy run is LABEL_DETECTED**, including the
ones that were, in fact, requested and confirmed. A legacy directory has no
`source` field anywhere in it, so nothing in the file distinguishes a confirmed
request from a detector's reading — and inventing the stronger of the two from
outside the file is precisely the move that produced the bad runs above. The
format cannot tell, so it must not claim. The useful consequence: the two known
bad ADS runs stop being ground truth the moment they are read through this API,
without anyone having to remember which stamps they were.
"""
import json
import os
import time

import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_ROOT = os.path.join(ROOT, 'docs', 'runs')

# Roots that hold runs but are not under RUNS_ROOT, for the same reason `create`
# takes a path: their directory names are load-bearing elsewhere. Listed here so
# `index()` — the "what has already been captured?" question every skill is told
# to ask before driving the game — covers them whatever format they are in.
EXTRA_ROOTS = (os.path.join(ROOT, 'docs', 'ads', 'runs'),
               os.path.join(ROOT, 'docs', 'attachments', 'runs'))

LABEL_REQUESTED = 'requested'
LABEL_DETECTED = 'detected'

MANIFEST = 'manifest.json'
LEGACY_ADS = 'index.jsonl'
LEGACY_ATTACHMENTS = 'index.json'

# 1  entries only
# 2  + run-level `facts` (what meta.json / index.json's top level used to hold),
#    + captures may be .jpg and may sit in a subdirectory
VERSION = 2


class CaptureRun:
    """One session of captures plus what is known about each of them."""

    def __init__(self, kind, stamp, note='', entries=None, path=None,
                 facts=None, quality=None, readonly=False):
        self.kind = kind
        self.stamp = stamp
        self.note = note
        self.entries = entries if entries is not None else []
        self.path = path or os.path.join(RUNS_ROOT, kind, stamp)
        # Run-level parameters: the view seed and sample times for an ADS run,
        # the rebuild queue for a template run. Never interpreted here.
        self.facts = dict(facts or {})
        self.quality = quality
        # Set on anything read out of a legacy directory. save() would write a
        # manifest.json beside 400 irreplaceable frames and start a second
        # source of truth for the same run, so it refuses instead.
        self.readonly = readonly

    # ── Creating ──

    @classmethod
    def create(cls, kind, note='', stamp=None, path=None, facts=None,
               quality=None):
        """A new run. `path` overrides the root — see the module docstring.

        `quality` is the JPEG quality for captures saved with a .jpg name; None
        means every capture is PNG. It exists because full-screen frames at
        3440x1440 cost ~700 KB as JPEG q95 and ~4 MB as PNG, and one ADS run is
        ~220 of them.
        """
        run = cls(kind, stamp or time.strftime('%Y%m%d_%H%M%S'), note,
                  path=path, facts=facts, quality=quality)
        os.makedirs(run.path, exist_ok=True)
        return run

    def _encode(self, rel):
        if self.quality is not None and os.path.splitext(rel)[1].lower() in (
                '.jpg', '.jpeg'):
            return [cv2.IMWRITE_JPEG_QUALITY, int(self.quality)]
        return []

    def add(self, frame, name, labels=(), **facts):
        """Save a capture and whatever is known about it.

        `name` may carry an extension and a subdirectory (`iron/hip_v0.jpg`);
        without an extension it is saved as PNG, which is what every caller
        written before v2 expects. Subdirectories are how the ADS runs keep one
        folder per scope, which is worth preserving — a human opening the run
        finds the frames sorted by the thing they vary.

        `labels` is a list of dicts with at least {slot, asset, source}, and an
        empty list is a legitimate answer meaning nobody looked. Everything
        else — weapon, slot states, scores — goes in as facts and is never
        interpreted here; a run should stay readable by a skill this module has
        never heard of.
        """
        rel = name if os.path.splitext(name)[1] else f'{name}.png'
        if frame is not None:
            dst = os.path.join(self.path, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            cv2.imwrite(dst, frame, self._encode(rel))
        entry = {'capture': rel.replace('\\', '/'), 'labels': list(labels)}
        entry.update(facts)
        self.entries.append(entry)
        self.save()
        return entry

    def add_fit(self, frame, name, weapon, slot, asset, **facts):
        """A part fitted on purpose and confirmed — ground truth.

        Only call this after the equip actually verified. A label that records
        an intention rather than an outcome is worse than no label: it is
        wrong exactly when the drag silently failed.
        """
        return self.add(frame, name, weapon=weapon,
                        labels=[{'slot': slot, 'asset': asset,
                                 'source': LABEL_REQUESTED}], **facts)

    def add_observed(self, frame, name, weapon, read, **facts):
        """Whatever a detector read. Context, never ground truth."""
        labels = [{'slot': s, 'asset': a, 'source': LABEL_DETECTED}
                  for s, a in (read or {}).items() if a]
        return self.add(frame, name, weapon=weapon, labels=labels, **facts)

    # ── Reading ──

    @classmethod
    def load(cls, stamp, kind=None):
        """By stamp, searching every kind under RUNS_ROOT unless one is given.

        Kept beside load_dir because a stamp is what a run PRINTS and what an
        operator pastes back ("Re-read offline with --report 20260802_161802").
        Runs outside RUNS_ROOT have no unique stamp namespace, so they are
        reachable only by path.
        """
        kinds = [kind] if kind else sorted(
            d for d in os.listdir(RUNS_ROOT)
            if os.path.isdir(os.path.join(RUNS_ROOT, d)))
        for k in kinds:
            p = os.path.join(RUNS_ROOT, k, stamp)
            if os.path.exists(os.path.join(p, MANIFEST)):
                return cls.load_dir(p)
        raise FileNotFoundError(f'no run {stamp} under {RUNS_ROOT}')

    @classmethod
    def load_dir(cls, path):
        """Any run directory: this format, or either of the two it replaced.

        Sniffed by which index file is present rather than by where the
        directory sits, so a legacy run stays readable after it is moved and a
        new run written into a legacy root (which is what capture_ads does) is
        read as what it is.
        """
        path = os.path.abspath(path)
        if os.path.exists(os.path.join(path, MANIFEST)):
            with open(os.path.join(path, MANIFEST), encoding='utf-8') as f:
                d = json.load(f)
            return cls(d['kind'], d['stamp'], d.get('note', ''), d['entries'],
                       path, d.get('facts'), d.get('quality'))
        if os.path.exists(os.path.join(path, LEGACY_ADS)):
            return cls._from_ads(path)
        if os.path.exists(os.path.join(path, LEGACY_ATTACHMENTS)):
            return cls._from_attachments(path)
        raise FileNotFoundError(
            f'{path} holds no {MANIFEST}, {LEGACY_ADS} or {LEGACY_ATTACHMENTS}')

    # ── The two pre-CaptureRun formats ──
    #
    # Both adapters follow the same three rules, and the reasons are in the
    # module docstring:
    #
    #   the run's parameters (meta.json / index.json's top level)  -> facts
    #   one capture record                                         -> one entry
    #   every recoverable label                                    -> DETECTED
    #
    # Nothing is written back. readonly=True is what enforces that.

    @classmethod
    def _from_ads(cls, path):
        """docs/ads/runs/<stamp>/ — index.jsonl + meta.json.

        One record per frame: file, scope, state (hip/ads/hip_after), t_ms,
        weapon, slot, and `verified` (what the scope slot read back, when
        anybody looked).

        THE SCOPE IS THE ONLY LABEL RECOVERED. `state` stays a fact, because it
        is a statement about the capture procedure ("the right button was
        tapped and this frame is 700 ms later") and not about the screen. That
        distinction is the whole lesson of 20260801_222936, where the procedure
        ran exactly as written and produced no sight picture at all. Ground
        truth about the sight picture exists only where a human adjudicated it
        — calibration/fit_ads_detector.py's NOT_SCOPED / SCOPED — and no
        capture program can produce it.
        """
        entries = []
        with open(os.path.join(path, LEGACY_ADS), encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                e = {k: v for k, v in rec.items() if k != 'file'}
                e['capture'] = rec['file']
                scope = rec.get('scope')
                e['labels'] = ([{'slot': 'scope', 'asset': scope,
                                 'source': LABEL_DETECTED}] if scope else [])
                entries.append(e)
        facts = {}
        meta = os.path.join(path, 'meta.json')
        if os.path.exists(meta):
            with open(meta, encoding='utf-8') as f:
                facts = json.load(f)
        return cls('ads', facts.get('stamp') or os.path.basename(path),
                   facts.get('note', ''), entries, path, facts, readonly=True)

    @classmethod
    def _from_attachments(cls, path):
        """docs/attachments/runs/<stamp>/ — index.json.

        `crops` is the per-capture list; everything else at the top level
        (targets, gun, angles, unreachable, and the `bad` rebuild queue the
        calibrate-template skill is pointed at) is the run's parameters.

        A crop's `key` names what was collected and `read` is what the current
        detector made of it. `key` becomes the label — as DETECTED, per the
        rule above, even though this producer's chain (spawner coordinate ->
        库存 row -> drag, each hop checked without a template) is the strongest
        ground truth in the repository. The file cannot say so, so this cannot
        either.
        """
        with open(os.path.join(path, LEGACY_ATTACHMENTS), encoding='utf-8') as f:
            d = json.load(f)
        entries = []
        for c in d.get('crops', ()):
            e = {k: v for k, v in c.items() if k != 'file'}
            e['capture'] = c['file']
            key = c.get('key')
            # 'type' is the 类型 marker, an ink-count measurement with no asset
            # to name, so there is nothing to label.
            e['labels'] = ([{'slot': c.get('slot') or c.get('target'),
                             'asset': key, 'source': LABEL_DETECTED}]
                           if key and c.get('target') != 'type' else [])
            entries.append(e)
        facts = {k: v for k, v in d.items() if k != 'crops'}
        return cls('attachments', os.path.basename(path), facts.get('note', ''),
                   entries, path, facts, readonly=True)

    # ── Querying ──

    @classmethod
    def latest(cls, kind):
        d = os.path.join(RUNS_ROOT, kind)
        stamps = sorted(s for s in os.listdir(d)
                        if os.path.exists(os.path.join(d, s, MANIFEST)))
        if not stamps:
            raise FileNotFoundError(f'no runs of kind {kind}')
        return cls.load(stamps[-1], kind)

    def labelled(self, asset=None):
        """Ground-truth samples only. -> [(entry, label, capture_path)]

        Never returns LABEL_DETECTED. See the module docstring: a detector's
        reading cannot serve as truth for the detector being calibrated, and
        making that a property of the API rather than a warning is the point
        of this format.
        """
        out = []
        for e in self.entries:
            for lab in e.get('labels', ()):
                if lab.get('source') != LABEL_REQUESTED:
                    continue
                if asset and lab.get('asset') != asset:
                    continue
                out.append((e, lab, os.path.join(self.path, e['capture'])))
        return out

    def frame(self, entry):
        return cv2.imread(os.path.join(self.path, entry['capture']))

    def save(self):
        if self.readonly:
            raise RuntimeError(
                f'{self.path} was read from a pre-CaptureRun run and is '
                f'read-only. Those captures cannot be re-made; write a new run '
                f'rather than a second index beside them.')
        os.makedirs(self.path, exist_ok=True)
        with open(os.path.join(self.path, MANIFEST), 'w',
                  encoding='utf-8') as f:
            json.dump({'version': VERSION, 'kind': self.kind,
                       'stamp': self.stamp, 'note': self.note,
                       'quality': self.quality, 'facts': self.facts,
                       'entries': self.entries}, f,
                      ensure_ascii=False, indent=1)

    def __repr__(self):
        n = len(self.entries)
        return (f'<CaptureRun {self.kind}/{self.stamp} {n} captures, '
                f'{len(self.labelled())} ground-truth labels>')


def _run_dirs():
    """(kind, stamp, directory) for every run on disk, in either format."""
    out = []
    if os.path.isdir(RUNS_ROOT):
        for kind in sorted(os.listdir(RUNS_ROOT)):
            d = os.path.join(RUNS_ROOT, kind)
            if not os.path.isdir(d):
                continue
            for stamp in sorted(os.listdir(d)):
                if os.path.exists(os.path.join(d, stamp, MANIFEST)):
                    out.append((kind, stamp, os.path.join(d, stamp)))
    for root in EXTRA_ROOTS:
        if not os.path.isdir(root):
            continue
        kind = os.path.basename(os.path.dirname(root))
        for stamp in sorted(os.listdir(root)):
            p = os.path.join(root, stamp)
            if any(os.path.exists(os.path.join(p, n))
                   for n in (MANIFEST, LEGACY_ADS, LEGACY_ATTACHMENTS)):
                out.append((kind, stamp, p))
    return out


def index():
    """Every run on disk. -> [(kind, stamp, n_captures, n_labelled)]"""
    out = []
    for kind, stamp, d in _run_dirs():
        r = CaptureRun.load_dir(d)
        out.append((kind, stamp, len(r.entries), len(r.labelled())))
    return out


def main():
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    rows = index()
    if not rows:
        print(f'no runs under {RUNS_ROOT}')
        return 0
    print(f'{"kind":14} {"stamp":17} {"captures":>9} {"ground truth":>13}')
    for kind, stamp, n, lab in rows:
        print(f'{kind:14} {stamp:17} {n:9d} {lab:13d}')
    print('\nground truth = labels a caller specified and something confirmed.\n'
          'Detector readings are stored too, but labelled() never returns '
          'them —\na detector cannot supply the truth it is judged against.\n'
          'Runs captured before this format read back with ZERO ground truth:\n'
          'their files carry no `source`, so the stronger claim cannot be '
          'made for them.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
