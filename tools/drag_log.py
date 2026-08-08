"""What separates the gestures that landed from the ones that did not.

Reads calibration/artifacts/drag/journal.jsonl, which control/inventory.py appends to on every
gesture. Offline; the game does not have to be running, and it does not have to
have been YOUR run — the journal is shared, always on, and stamped with the pid
of whoever wrote each line.

    pixi run drag-log                 # summary + every failure in full
    pixi run drag-log --all           # every gesture, one line each
    pixi run drag-log --since 40      # only the last 40
    pixi run drag-log --kind drag     # one kind only
    pixi run drag-log --pid 12345     # one process only
    pixi run drag-log --guns          # only what touched a weapon

THE QUESTION THIS EXISTS FOR is not "did it fail" — the caller already knows
that — but "what was different about the failures". A drop can miss in three
places and they need different fixes:

    the cursor never got there    `place.grab.ok` false, or `got.grab` off
                                  `want.grab`. Something is still sending
                                  motion; see Pointer.place.
    it was released somewhere     `got.release` off `want.release`. The item
    other than the drop point     goes back to the column it came from.
    the gesture was clean but     `gesture` true, `moved` false. The release
    nothing arrived               point was right and the game did not take
                                  it — a real drop failure, not a cursor one.

`gap_s` is seconds since the previous gesture ENDED, because "the second one
fails" is a claim about sequence and nothing else in the record can test it.

FIVE KINDS, and the ones that are not drags were the missing half:

    drag      press-travel-release
    click     a right click: equip, unequip, or the collector's auto_equip
    drop      the whole weapon out of the rack, on purpose
    refused   a gesture this layer declined to send, and which guard fired
    tab/hold  the screen toggles and weapon switches BETWEEN the gestures,
              which is what a log of drags alone could never show

⚠ THE LINE TO GREP FOR IS `gun_lost`. A right click aimed at an attachment
slot that is empty — or that the cursor drifted off — reaches the weapon row
underneath and throws the whole gun on the floor. The slot reads empty
afterwards either way, so the gesture reports success; only the name-plate ink
(`plate`, before and after) separates them. That failure cost 74 parts across
11 collector runs before it was written down.
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LOG = os.path.join(ROOT, 'calibration', 'artifacts', 'drag', 'journal.jsonl')

# Ink on a weapon name plate, below which the rack row is empty. A COPY of
# control.inventory.PLATE_INK_MIN, deliberately: this reader imports nothing,
# so it still runs when the thing being debugged is a broken import — which is
# the state a run that died halfway tends to leave behind. `pixi run locations`
# asserts the two are equal, so the copy cannot drift silently.
PLATE_INK_MIN = 200


def load(path):
    """Every line of the journal, oldest first — INCLUDING the rolled half.

    control.inventory rolls the file to `.1` at 8 MB. Reading only the live
    one would quietly drop the older half of the history, and "quietly" is the
    problem: a summary over three of yesterday's runs looks exactly like a
    summary over all six.
    """
    return _read(path + '.1') + _read(path)


def _read(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Lines written before the journal covered anything but drags have
            # no `kind`. They are still drags, and dropping them would throw
            # away the runs the investigation is built on.
            r.setdefault('kind', 'drag')
            out.append(r)
    return out


def landed(r):
    """Did this gesture do what it was asked to?

    `moved` is None when nothing could be counted (a slot destination, two
    full lists, or auto_equip which verifies nothing by design), and that is
    NOT a failure — it is an unanswered question, and lumping it in with the
    failures would invent a pattern.
    """
    if not r.get('gesture'):
        return False if r['kind'] != 'refused' else None
    return None if r.get('moved') is None else bool(r['moved'])


def gun_lost(r):
    """Did a weapon leave the rack when nobody asked it to?

    Two ways to see it, and the explicit flag is only on the paths that look:
    `gun_lost` is set by right_click_unequip, and the plate pair catches the
    rest. A `drop` line is excluded by construction — it carries no gun_lost
    and its plate falling to zero is the request being granted.
    """
    if r.get('gun_lost'):
        return True
    if r['kind'] == 'drop':
        return False
    a, b = (r.get('plate') or [None, None])[:2]
    return bool(a and b is not None and a >= PLATE_INK_MIN
                and b < PLATE_INK_MIN)


def touches_gun(r):
    for k in ('src', 'dst'):
        if (r.get(k) or '').startswith('gun'):
            return True
    return r.get('plate') is not None


def off_by(want, got):
    if not want or not got:
        return None
    return (got[0] - want[0], got[1] - want[1])


def clock(r):
    t = r.get('t')
    return '  --:--:--' if not t else time.strftime('  %H:%M:%S',
                                                    time.localtime(t))


def one_line(i, r):
    v = landed(r)
    mark = {True: 'ok  ', False: 'MISS', None: '?   '}[v]
    gap = '   -  ' if r.get('gap_s') is None else f'{r["gap_s"]:6.2f}'
    kind = r['kind']
    head = (f'{i:>4}{clock(r)} {mark} {kind:<7}'
            f' {str(r.get("src") or "-"):>10} -> {str(r.get("dst") or "-"):<9}'
            f' gap{gap}s')

    if kind in ('tab', 'hold'):
        extra = (f'  want={r.get("want")}  presses={r.get("presses")}'
                 if kind == 'tab' else f'  held={r.get("held")}')
        return head + extra + (f'  [{r["failed_at"]}]'
                               if r.get('failed_at') else '')
    if kind == 'refused':
        return head + f'  by {r.get("by")}: {r.get("failed_at")}'

    pg = (r.get('place') or {}).get('grab') or {}
    pd = (r.get('place') or {}).get('dst') or {}
    got = r.get('got') or {}
    dg = off_by((r.get('want') or {}).get('grab'), got.get('grab'))
    dr = off_by((r.get('want') or {}).get('release'), got.get('release'))
    line = head + f'  place {pg.get("tries", "-")}/{pd.get("tries", "-")}'
    line += f'  grab{"±0" if dg == (0, 0) else dg}'
    if kind == 'drag':
        line += (f'  rel{"±0" if dr == (0, 0) else dr}'
                 f'  rows {r.get("rows_before")}'
                 f'  polls {len(r.get("poll") or [])}')
    if r.get('plate'):
        line += f'  plate {r["plate"][0]}->{r["plate"][1]}'
    if gun_lost(r):
        line += '  ⚠GUN LOST'
    return line + (f'  [{r["failed_at"]}]' if r.get('failed_at') else '')


def detail(i, r):
    print(f'\n--- #{i}  {r["kind"]}  {r.get("src")} -> {r.get("dst")}  '
          f'attempt {r.get("attempt")}  pid {r.get("pid")} '
          f'{r.get("proc") or ""}{clock(r)}')
    for k in ('gap_s', 'drag_s', 'steps', 'gesture', 'moved', 'failed_at',
              'by', 'rows_before', 'plate', 'held', 'was', 'now', 'via',
              'slot_state', 'content', 'presses'):
        if r.get(k) is not None:
            print(f'    {k:<12} {r[k]}')
    if gun_lost(r):
        print(f'    {"GUN LOST":<12} the plate went '
              f'{r["plate"][0]} -> {r["plate"][1]}, i.e. the rack row emptied')
    for end in ('grab', 'dst'):
        p = (r.get('place') or {}).get(end)
        if p:
            print(f'    place.{end:<6} tries={p.get("tries")} '
                  f'ok={p.get("ok")} off={p.get("off")}')
    w, g = r.get('want') or {}, r.get('got') or {}
    if isinstance(w, dict) and (w.get('grab') or g.get('grab')):
        print(f'    cursor       want grab {w.get("grab")} got {g.get("grab")}'
              f' | want release {w.get("release")} got {g.get("release")}'
              f' held {g.get("held")}')
    for c in (r.get('checks') or []):
        print(f'    check        gun{c.get("gun")}.{c.get("slot")} '
              f'want {c.get("want")!r} seen {c.get("seen")!r} ok={c.get("ok")}')
    if r.get('poll'):
        print(f'    poll (t, src_rows, dst_rows):')
        for t, ns, nd in r['poll']:
            print(f'        {t:>6.3f}  {ns}  {nd}')


def spread(rs, get, label):
    vals = [get(r) for r in rs]
    vals = [v for v in vals if v is not None]
    if not vals:
        return f'{label:<22} -'
    vals.sort()
    return (f'{label:<22} n={len(vals):<4} min={vals[0]:<8.3g} '
            f'med={vals[len(vals) // 2]:<8.3g} max={vals[-1]:.3g}')


def bursts(rows):
    """Split into runs of back-to-back gestures. -> [[rec, ...], ...]

    PER PROCESS, which is not a detail: several agents share this game and
    therefore this file, so consecutive LINES can come from two runs and the
    gap between them is a perf_counter difference across processes, i.e.
    meaningless. Grouping by pid first is what keeps "the third drag of a
    burst" a real claim.
    """
    by_pid = {}
    for r in rows:
        by_pid.setdefault(r.get('pid'), []).append(r)
    out = []
    for group in by_pid.values():
        cur = []
        for r in group:
            if r.get('gap_s') is None or r['gap_s'] > 5.0:
                if cur:
                    out.append(cur)
                cur = []
            cur.append(r)
        if cur:
            out.append(cur)
    return out


def summarise(rows):
    if not rows:
        print(f'\nNothing logged yet. The journal is written by '
              f'InventoryControl;\nrun anything that moves items and it '
              f'will appear at\n  {os.path.relpath(LOG, ROOT)}')
        return

    procs = {}
    for r in rows:
        procs.setdefault((r.get('pid'), r.get('proc')), 0)
        procs[(r.get('pid'), r.get('proc'))] += 1
    span = [r.get('t') for r in rows if r.get('t')]
    print(f'{len(rows)} gesture(s) from {len(procs)} process(es)'
          + (f', {time.strftime("%m-%d %H:%M", time.localtime(min(span)))}'
             f' .. {time.strftime("%H:%M", time.localtime(max(span)))}'
             if span else ''))
    if len(procs) > 1:
        for (pid, proc), n in sorted(procs.items(), key=lambda kv: -kv[1]):
            print(f'    pid {pid} {proc or "?"}: {n}')

    print('\nby kind:')
    for kind in ('drag', 'click', 'drop', 'refused', 'tab', 'hold'):
        rs = [r for r in rows if r['kind'] == kind]
        if not rs:
            continue
        a = sum(landed(r) is True for r in rs)
        b = sum(landed(r) is False for r in rs)
        c = sum(landed(r) is None for r in rs)
        print(f'  {kind:<8} {len(rs):>4}   landed {a:>4}  missed {b:>4}  '
              f'not countable {c:>4}')

    # ── the expensive failure, first and alone ──
    lost = [(i, r) for i, r in enumerate(rows, 1) if gun_lost(r)]
    if lost:
        print(f'\n⚠ {len(lost)} gesture(s) took a WEAPON off the rack without '
              f'being asked to:')
        for i, r in lost[-8:]:
            print(f'  #{i}{clock(r)} {r["kind"]:<6} {r.get("src")} '
                  f'-> {r.get("dst")}  plate {r["plate"][0]}->{r["plate"][1]}'
                  f'  pid {r.get("pid")}')
        print('  A right click on a slot that is empty (or that the cursor '
              'drifted off)\n  reaches the weapon row underneath. See '
              'unequip() in control/inventory.py.')

    refused = [r for r in rows if r['kind'] == 'refused']
    if refused:
        print('\nguards that fired (a refusal is a failure that did NOT '
              'happen):')
        by = {}
        for r in refused:
            k = f'{r.get("by")}: {r.get("failed_at")}'
            by[k] = by.get(k, 0) + 1
        for k, n in sorted(by.items(), key=lambda kv: -kv[1])[:10]:
            print(f'  {n:>4}  {k}')

    drags = [r for r in rows if r['kind'] in ('drag', 'click', 'drop')]
    ok = [r for r in drags if landed(r) is True]
    miss = [r for r in drags if landed(r) is False]

    # WHERE it went wrong, which is the split that picks the fix.
    by_stage = {}
    for r in miss:
        stage = (r.get('failed_at') or
                 ('released clean, nothing arrived' if r.get('gesture')
                  else 'gesture refused'))
        by_stage[stage] = by_stage.get(stage, 0) + 1
    if by_stage:
        print('\nwhere the failures happened:')
        for k, n in sorted(by_stage.items(), key=lambda kv: -kv[1]):
            print(f'  {n:>4}  {k}')

    # WAS THERE ANYTHING TO GRAB? The geometry fields were byte-for-byte
    # identical between landed and missed (place tries 1.00 both, dy 0.00
    # both), which rules out the gesture and leaves the TARGET. src_key is
    # read from the source row at grab time, so it separates three failures
    # that look the same from the caller:
    #   None                -> that row was empty. The plan was computed
    #                          against a stale read; rows below a removed one
    #                          scroll up, so the gesture aimed at a row that
    #                          had already moved. This is the row-shift bug.
    #   '(read failed: ..)' -> the detector could not name it (park/tooltip).
    #   an actual key       -> the item WAS there and the game refused. Only
    #                          this third bucket is a timing or firmware
    #                          question; the other two are our own bookkeeping.
    def _bucket(r):
        k = r.get('src_key')
        if k is None:
            return 'source row was EMPTY (stale plan / row shift)'
        if isinstance(k, str) and k.startswith('('):
            return k
        return 'item was there, the game refused'

    have = [r for r in drags if 'src_key' in r]
    if have:
        print('\nwhat sat on the grab point (only rows logged since src_key '
              'was added):')
        for name, rs in (('landed', [r for r in have if landed(r) is True]),
                         ('missed', [r for r in have if landed(r) is False])):
            if not rs:
                continue
            by = {}
            for r in rs:
                b = _bucket(r)
                by[b] = by.get(b, 0) + 1
            print(f'  {name} ({len(rs)}):')
            for k, n in sorted(by.items(), key=lambda kv: -kv[1]):
                print(f'    {n:>4}  ({100 * n / len(rs):>4.0f}%)  {k}')
        stale = sum(1 for r in have
                    if landed(r) is False and r.get('src_key') is None)
        bad = sum(1 for r in have if landed(r) is False)
        if bad:
            print(f'  -> {stale}/{bad} of the misses were aimed at an empty '
                  f'row. Anything near 100% means\n     the fix is to re-read '
                  f'before each gesture, not to slow the gesture down.')

        # THE RETRY IS THE CONTROL, and it costs no new instrumentation. A
        # miss followed by attempt 2 on the SAME address re-reads the row, so
        # the two records answer the question the bucket above cannot: a row
        # holding SOMETHING is not a row holding the RIGHT something, and a
        # shifted row still reads as a key. Comparing the pair splits them.
        #
        #   src_key CHANGED between the attempts -> the row moved under us.
        #     The first gesture grabbed whatever had scrolled into that slot,
        #     which is a bookkeeping bug on our side (re-read before each
        #     gesture) and NOT a timing one.
        #   src_key IDENTICAL and the retry landed -> same item, same point,
        #     one refusal then one acceptance. That is the game, or something
        #     between the gestures. This is the bucket that stays open.
        pairs = []
        for i, r in enumerate(have):
            if landed(r) is not False or 'src_key' not in r:
                continue
            for nxt in have[i + 1:i + 3]:
                if (nxt.get('src') == r.get('src')
                        and nxt.get('dst') == r.get('dst')
                        and (nxt.get('attempt') or 0) > (r.get('attempt') or 0)):
                    pairs.append((r, nxt))
                    break
        if pairs:
            same = sum(1 for a, b in pairs
                       if a.get('src_key') == b.get('src_key'))
            won = sum(1 for a, b in pairs
                      if a.get('src_key') == b.get('src_key')
                      and landed(b) is True)
            print(f'\n  the retry as a control ({len(pairs)} miss->retry '
                  f'pairs on the same address):')
            print(f'    {same:>4}  the re-read found the SAME item  '
                  f'({won} of those then landed)')
            print(f'    {len(pairs) - same:>4}  the re-read found a DIFFERENT '
                  f'item -> the row had moved')

    if ok or miss:
        print('\nlanded vs missed, on the three candidate causes:')
        for name, rs in (('landed', ok), ('missed', miss)):
            if not rs:
                continue
            print(f'  {name}:')
            print('    ' + spread(rs, lambda r: r.get('gap_s'),
                                  'gap since prev (s)'))
            print('    ' + spread(rs, lambda r: ((r.get('place') or {})
                                                 .get('grab') or {})
                                  .get('tries'), 'grab place tries'))
            print('    ' + spread(rs, lambda r: ((r.get('place') or {})
                                                 .get('dst') or {})
                                  .get('tries'), 'release place tries'))
            print('    ' + spread(rs, lambda r: r.get('drag_s'),
                                  'gesture (s)'))

    runs = bursts(rows)
    if any(len(x) > 1 for x in runs):
        print('\nby position within a burst (gap > 5 s starts a new one, '
              'per process):')
        pos = {}
        for run in runs:
            n = 0
            for r in run:
                if r['kind'] not in ('drag', 'click', 'drop'):
                    continue        # a Tab toggle is not an attempt
                n += 1
                v = landed(r)
                a, b = pos.get(n, (0, 0))
                pos[n] = (a + (v is True), b + (v is False))
        for i in sorted(pos):
            a, b = pos[i]
            if a + b:
                print(f'  #{i:<3} landed {a:>4}   missed {b:>4}')

    # WHAT HAPPENED JUST BEFORE A MISS. The open question in control/CLAUDE.md
    # is why only the first gesture of a burst fails, and the probe that could
    # not reproduce it differed from the real collector in exactly this: what
    # sat between the bursts. Now that toggles and switches are journalled, the
    # log can answer it directly instead of by hypothesis.
    if miss:
        print('\nthe two gestures before each miss:')
        idx = {id(r): i for i, r in enumerate(rows)}
        before = {}
        for r in miss:
            i = idx[id(r)]
            key = ' , '.join(x['kind'] for x in rows[max(0, i - 2):i]) or '-'
            before[key] = before.get(key, 0) + 1
        for k, n in sorted(before.items(), key=lambda kv: -kv[1])[:8]:
            print(f'  {n:>4}  {k}')



def churn_report(rows):
    """Tab screens left and re-entered with nothing done in between.

    The pattern was measurable from this file already -- a close row followed
    by an open row -- but not ATTRIBUTABLE, because a Tab row records the press
    and not the code that asked for it. control.inventory._churn stamps the
    caller onto the offending open row; this is the half that reads it.

    Printed even when the count is zero. A gate whose output nobody sees is a
    gate nobody knows is armed -- which is how the drag verdict stayed wrong.
    """
    hits = [r for r in rows if isinstance(r.get('churn'), dict)]
    print()
    print('=== Tab churn: left the inventory and came straight back ===')
    if not hits:
        print('  none in this slice, or the run predates the churn stamp')
        return
    by = {}
    for r in hits:
        c = r['churn']
        pair = (f"{c.get('closed_by') or '?'}  ->  "
                f"{c.get('by') or '?'}")
        e = by.setdefault(pair, [0, 0.0])
        e[0] += 1
        e[1] += c.get('gap_s') or 0.0
    print(f'  {len(hits)} occurrence(s), {len(hits) * 2} Tab presses, '
          f'{sum(v[1] for v in by.values()):.0f}s of wall clock')
    print()
    print('  count   secs   closed by  ->  reopened by')
    for who, (n, secs) in sorted(by.items(), key=lambda kv: -kv[1][0]):
        print(f'  {n:4d}  {secs:6.1f}s   {who}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', default=LOG)
    ap.add_argument('--all', action='store_true', help='one line per gesture')
    ap.add_argument('--since', type=int, default=0,
                    help='only the last N gestures')
    ap.add_argument('--kind', help='drag | click | drop | refused | tab | hold')
    ap.add_argument('--pid', type=int, help='one process only')
    ap.add_argument('--guns', action='store_true',
                    help='only gestures that touched a weapon or its plate')
    ap.add_argument('--clear', action='store_true',
                    help='truncate the journal, so the next run starts clean')
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    if args.clear:
        os.makedirs(os.path.dirname(args.log), exist_ok=True)
        open(args.log, 'w').close()
        print(f'cleared {os.path.relpath(args.log, ROOT)}')
        if os.path.exists(args.log + '.1'):
            print(f'  (the rolled half is still at '
                  f'{os.path.relpath(args.log, ROOT)}.1 — delete it by hand '
                  f'if you want a truly clean start)')
        return 0

    rows = load(args.log)
    if args.kind:
        rows = [r for r in rows if r['kind'] == args.kind]
    if args.pid:
        rows = [r for r in rows if r.get('pid') == args.pid]
    if args.guns:
        rows = [r for r in rows if touches_gun(r)]
    if args.since:
        rows = rows[-args.since:]
    summarise(rows)

    if args.all:
        print()
        for i, r in enumerate(rows, 1):
            print(one_line(i, r))
    else:
        bad = [(i, r) for i, r in enumerate(rows, 1)
               if landed(r) is False or gun_lost(r)]
        for i, r in bad[-12:]:
            detail(i, r)
        if len(bad) > 12:
            print(f'\n({len(bad) - 12} earlier failure(s) not shown)')
    churn_report(rows)
    return 0


if __name__ == '__main__':
    sys.exit(main())
