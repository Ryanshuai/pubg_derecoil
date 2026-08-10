"""Store what the 库存 rows SAID, so a future reading has something to differ from.

    pixi run row-names --write        # install the reading below
    pixi run row-names                # compare it against the catalogue

WHY A SEPARATE FILE AND NOT A FIELD IN attachment_catalog. The catalogue is
this repo's CLAIM about what a key means. This file is the SCREEN'S ANSWER,
with the date it was given. Writing the answer into the claim would destroy
the only thing either is good for: two independent statements that can
disagree.

That disagreement is the whole point, and it has a name here. 41.1 replaced
the Angled Foregrip with the Tilted Grip IN PLACE -- same spawner position,
same list length -- so nothing moved, nothing failed, and four months of
`angled_grip` recoil numbers belonged to a different part. A key resolves to a
POSITION in a spawner category; the drive path reads no text at all. So the
only way to notice a swap is to read the names on purpose and compare them to
the last time someone did.

⚠ THE NAMES BELOW WERE READ BY A VISION MODEL FROM FOUR FULL-SCREEN FRAMES,
not by an OCR and not from a template. Each batch spawned a known set of keys
and the names read had to match that set exactly -- every key present once,
nothing else. All four batches passed. The frames are kept beside the reading
(`calibration/artifacts/rows_vlm/<stamp>__rows.png`) because a name is a
judgement and the picture is the evidence for it.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detector.attachment_catalog import ATTACHMENTS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(ROOT, 'data', 'attachment_names.json')

# key -> the English label the game printed, verbatim including its class list.
# ⚠ THE PARENTHESES ARE PART OF THE READING. "Extended Mag (DMR, SR)" and
# "Extended Mag (AR, DMR, M249, S12K)" are different items sharing a stem, and
# dropping the qualifier would merge two rows that the game keeps apart -- the
# same shape as the mistake this file exists to catch.
READING = {
    # batch 0 -- 20260809_213510
    'ext_sr':        'Extended Mag (DMR, SR)',
    'comp_sr':       'Compensator (DMR, SR)',
    'cheek_pad':     'Cheek Pad (DMR, SR)',
    'bullet_loops':  'Bullet Loops (SG, SR, Win94)',
    'ext_ar':        'Extended Mag (AR, DMR, M249, S12K)',
    'brake_ar':      'Muzzle Brake (AR, DMR, O12, S12K)',
    'flash_ar':      'Flash Hider (AR, DMR, O12, S12K)',
    'comp_ar':       'Compensator (AR, DMR, O12, S12K)',
    'ext_smg':       'Extended Mag (Handgun, SMG)',
    'flash_smg':     'Flash Hider (SMG)',
    'comp_smg':      'Compensator (SMG)',
    'duckbill':      'Duckbill (SG)',
    'choke':         'Choke (SG)',
    # batch 1 -- 20260809_213843
    'holo':          'Holographic Sight',
    'red_dot':       'Red Dot Sight',
    'quick_sr':      'QuickDraw Mag (DMR, SR)',
    'quickext_sr':   'Ext.QuickDraw Mag (DMR, SR)',
    'flash_sr':      'Flash Hider (DMR, SR)',
    'light_grip':    'Lightweight Grip',
    'laser':         'Laser Sight',
    'half_grip':     'Halfgrip',
    'quick_ar':      'QuickDraw Mag (AR, DMR, M249, S12K)',
    'quickext_ar':   'Ext.QuickDraw Mag (AR, DMR, M249, S12K)',
    'heavy_stock':   'Heavy Stock (SMG, AR, M249)',
    'quick_smg':     'QuickDraw Mag (Handgun, SMG)',
    'quickext_smg':  'Ext.Quickdraw Mag (Handgun, SMG)',
    # batch 2 -- 20260809_213933
    'scope_15x':     '15x Scope',
    'scope_8x':      '8x Scope',
    'scope_6x':      '6x Scope',
    'scope_4x':      '4x Scope',
    'scope_3x':      '3x Scope',
    'scope_2x':      '2x Scope',
    'supp_sr':       'Suppressor (DMR, SR)',
    'tilted_grip':   'Tilted Grip',
    'thumb_grip':    'Thumbgrip',
    'supp_ar':       'Suppressor (AR, DMR, O12, S12K)',
    'tactical_stock': 'Tactical Stock (SMG, AR, M249)',
    'supp_smg':      'Suppressor (Handgun, SMG)',
    'uzi_stock':     'Folding Stock (Skorpion, Micro UZI, MP9)',
    # batch 3 -- 20260809_214026
    'variable':      'Hybrid Scope',
    'vert_grip':     'Vertical Foregrip',
}

# The row order each batch came back in. Kept because the game inserts into its
# OWN sort order, which is neither the spawn order nor alphabetical -- and a
# collector that assumes otherwise files crops under the wrong names (that is
# how legacy_collect_templates got seven of them wrong).
BATCHES = {
    '20260809_213510': ['ext_sr', 'comp_sr', 'cheek_pad', 'bullet_loops',
                        'ext_ar', 'brake_ar', 'flash_ar', 'comp_ar', 'ext_smg',
                        'flash_smg', 'comp_smg', 'duckbill', 'choke'],
    '20260809_213843': ['holo', 'red_dot', 'quick_sr', 'quickext_sr',
                        'flash_sr', 'light_grip', 'laser', 'half_grip',
                        'quick_ar', 'quickext_ar', 'heavy_stock', 'quick_smg',
                        'quickext_smg'],
    '20260809_213933': ['scope_15x', 'scope_8x', 'scope_6x', 'scope_4x',
                        'scope_3x', 'scope_2x', 'supp_sr', 'tilted_grip',
                        'thumb_grip', 'supp_ar', 'tactical_stock', 'supp_smg',
                        'uzi_stock'],
    '20260809_214026': ['variable', 'vert_grip'],
}


# ⚠ THE SAME NAME WRAPS DIFFERENTLY IN THE TWO PANELS, and that is a fact
# about the game, not about this reading. 附近 is the narrower column, so a
# label that fits on two lines in 库存 can take three:
#
#     库存    Ext.QuickDraw Mag (AR,  /  DMR, M249, S12K)
#     附近    Ext.QuickDraw Mag  /  (AR, DMR, M249,  /  S12K)
#
# Same glyphs, different picture -- so one template cannot serve both panels,
# and a bank cut only from 库存 reads 附近 at 0.54 (below any usable gate).
# These rows are the second rendering, read off the SAME frames: each batch's
# shot shows the previous batch lying on the floor. They are ground truth of
# exactly the same kind as READING -- a vision model naming rows whose keys
# were already known -- and they are what the `.nearby` variants are cut from.
#
# Rows are listed in panel order and stop where the reading stops: the 附近
# list is a WINDOW and its last row can be clipped by the panel edge.
NEARBY = {
    '20260809_213843': ['tactical_stock', 'heavy_stock', 'ext_ar', 'brake_ar',
                        'flash_ar', 'comp_ar', 'ext_smg', 'flash_smg',
                        'comp_smg', 'duckbill', 'choke'],
    '20260809_213933': ['holo', 'red_dot', 'quick_sr', 'quickext_sr',
                        'flash_sr', 'light_grip', 'laser', 'half_grip',
                        'quick_ar', 'quickext_ar', 'heavy_stock', 'quick_smg',
                        'quickext_smg'],
    '20260809_214026': ['scope_15x', 'scope_8x', 'scope_6x', 'scope_4x',
                        'scope_3x', 'scope_2x', 'supp_sr', 'tilted_grip',
                        'thumb_grip', 'supp_ar', 'tactical_stock', 'supp_smg'],
}


def check():
    """What the reading and the catalogue disagree about. -> [str]"""
    out = []
    missing = sorted(set(ATTACHMENTS) - set(READING))
    extra = sorted(set(READING) - set(ATTACHMENTS))
    if missing:
        out.append(f'in the catalogue but never read: {", ".join(missing)}')
    if extra:
        out.append(f'read but not in the catalogue: {", ".join(extra)}')
    # Every batch's rows must be exactly the keys that batch named -- the same
    # set test the collector applies live, re-applied to what got stored.
    seen = [k for rows in BATCHES.values() for k in rows]
    if sorted(seen) != sorted(READING):
        out.append('the per-batch row lists do not add up to READING')
    dupes = {k for k in seen if seen.count(k) > 1}
    if dupes:
        out.append(f'a key appears in two batches: {", ".join(sorted(dupes))}')
    # ⚠ A DUPLICATE LABEL IS A REAL ALARM, not a formatting nit. Two keys
    # printing the same name means either the catalogue holds one item twice or
    # the game merged two entries -- and both make every stored label for the
    # pair unattributable.
    by_name = {}
    for k, n in READING.items():
        by_name.setdefault(n, []).append(k)
    for n, ks in sorted(by_name.items()):
        if len(ks) > 1:
            out.append(f'two keys print {n!r}: {", ".join(sorted(ks))}')
    # The floor reading names the same items a second time, so it may only
    # contain keys the 库存 reading already knows. A key appearing ONLY on the
    # floor would be a part nobody spawned -- left over from another run, and
    # therefore not evidence about any batch.
    floor = {k for rows in NEARBY.values() for k in rows}
    unknown = sorted(floor - set(READING))
    if unknown:
        out.append(f'named on the floor but never spawned: {", ".join(unknown)}')
    return out


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true', help='install to data/')
    args = ap.parse_args()

    problems = check()
    for p in problems:
        print(f'[!] {p}')
    print(f'{len(READING)} of {len(ATTACHMENTS)} catalogue keys have a name '
          f'read from the screen')
    if problems:
        return 1

    if args.write:
        payload = {
            'read_on': '2026-08-09',
            'how': 'vision model reading four full-screen 库存 frames; each '
                   'batch spawned a known key set and the names read matched '
                   'that set exactly',
            'frames': {s: f'calibration/artifacts/rows_vlm/{s}__rows.png'
                       for s in BATCHES},
            'rows': BATCHES,
            'names': READING,
        }
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        with open(STORE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=1, ensure_ascii=False, sort_keys=True)
        print(f'-> {os.path.relpath(STORE, ROOT)}')
    else:
        print('(--write to install; nothing changed)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
