"""Save a picture with what was known about it. One call, one sidecar.

    from detector.snapshot import snap

    snap(crop, 'calibration/artifacts/tab/runs/x/muzzle.png', kind='crop', screen='tab',
         roi=(316, 2409, 63, 63), parent='00_baseline.png',
         source='calibrate-template/attachment',
         labels=[{'target': 'attachment', 'value': 'comp_ar',
                  'source': REQUESTED}])

WHY THIS EXISTS. At the moment `cv2.imwrite` runs, everything about the frame
is in scope: which gun, which attachments, which screen, what this run was
trying to produce. None of it is written down, so recovering it later means a
human looking at the picture. The information is free at write time and
expensive forever after.

The measurable consequence is in tools/regression_check.py. It picks a file up
if it is under docs/ and happens to be full-screen, which means:

  * the rule is implicit — it lives in a glob and a size comparison, so which
    shots count depends on where they landed rather than on what they are
  * CROPS ARE ENTIRELY WASTED. calibration/artifacts/spawner/runs/*/col1_row01_label.png and
    its kind outnumber the full-screen shots several times over, and they are
    exactly what template matching wants. None of them are in the harness.
  * the assertion is only ever "same as last time", which catches a library
    bump and nothing else. With a label it becomes "same as the truth", which
    catches a wrong answer.

THE LABEL VOCABULARY IS calibration/capture_run.py's, DELIBERATELY NOT A NEW
ONE. Same three states, same meanings, and the third one is the one that
matters:

    REQUESTED   we asked for this and something confirmed it. Ground truth.
    DETECTED    a detector read it. Context and cross-checks only.
    (no label)  nobody looked.

An unverified intention gets NO label. Writing it as REQUESTED is what turned
two ADS runs into confidently-wrong ground truth — see capture_run's docstring
for the full account. `snap()` enforces the vocabulary and nothing else: it
will not invent a source, and a label without one is refused.

WHY IT LIVES IN detector/. It is the offline layer's test: give it a PNG, no
game, no hardware. That is also the practical answer — control/, calibration/
and tools/ all need to write these, and detector/ is the only layer all three
may import.
"""
import json
import os
import time

import cv2

REQUESTED = 'requested'
DETECTED = 'detected'
SOURCES = (REQUESTED, DETECTED)

KIND_FULL = 'full'      # a whole screen, the shape regression already knows
KIND_CROP = 'crop'      # a region cut out of one

SIDECAR_EXT = '.json'
VERSION = 1


class BadLabel(ValueError):
    """A label that does not say who established it."""


def _check(labels):
    """Every label names a target, a value and a source. -> the list, or raise.

    Refusing is the point. The alternative -- defaulting a missing source to
    REQUESTED, or to DETECTED -- is a guess about who looked, written into the
    file as though someone knew. There is no safe default for that question.
    """
    out = []
    for lab in labels or ():
        missing = [k for k in ('target', 'value', 'source') if not lab.get(k)]
        if missing:
            raise BadLabel(f'label {lab!r} is missing {missing}. A label with '
                           f'no source cannot say whether anyone looked; leave '
                           f'it out and put the intention in `facts` instead.')
        if lab['source'] not in SOURCES:
            raise BadLabel(f'source {lab["source"]!r} is not one of {SOURCES}')
        out.append(dict(lab))
    return out


def sidecar_path(image_path):
    return os.path.splitext(image_path)[0] + SIDECAR_EXT


def snap(img, path, kind=KIND_CROP, screen=None, roi=None, parent=None,
         source=None, labels=(), state=None, stamp=None, **facts):
    """Write `img` to `path` and a sidecar beside it. -> the sidecar dict.

    kind    KIND_FULL for a whole screen, KIND_CROP for a region of one.
    roi     (y, x, h, w) of the crop within its parent, when known.
    parent  file name of the full shot this came out of, when known. It is
            what lets a crop be re-cut after the geometry is re-measured.
    source  who produced this, free text ('calibrate-screen/spawner'). Not the
            same field as a LABEL's source, which says who established a fact.
    state   whatever GameState knew. Dumped verbatim, never interpreted.
    facts   anything else. Kept, never interpreted -- a sidecar should stay
            readable by a tool this module has never heard of.

    A label is refused unless it says target, value and source. See _check.
    """
    labels = _check(labels)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    if img is not None:
        cv2.imwrite(path, img)
    meta = {'v': VERSION,
            'ts': stamp or time.strftime('%Y%m%d_%H%M%S'),
            'kind': kind, 'screen': screen,
            'roi': list(roi) if roi else None,
            'parent': parent, 'source': source,
            'labels': labels, 'state': state}
    meta.update(facts)
    with open(sidecar_path(path), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return meta


def read_sidecar(image_path):
    """The sidecar beside `image_path`, or None. Never raises on a bad file."""
    p = sidecar_path(image_path)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def truth(meta, target=None):
    """Ground-truth labels only. [] when nobody looked.

    NEVER returns DETECTED, for the same reason CaptureRun.labelled() does
    not: a detector's reading cannot be the truth the detector is judged
    against, and making that a property of the API beats warning about it in
    prose each caller copies and lets drift.
    """
    out = [lab for lab in (meta or {}).get('labels', ())
           if lab.get('source') == REQUESTED]
    return [lab for lab in out if target is None or lab.get('target') == target]


def readings(meta, target=None):
    """DETECTED labels — context and cross-checks, never assertions."""
    out = [lab for lab in (meta or {}).get('labels', ())
           if lab.get('source') == DETECTED]
    return [lab for lab in out if target is None or lab.get('target') == target]
