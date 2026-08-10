"""The gate on `--spread`: random hosts that still extend rather than resample.

    pixi run spread-hosts

`hosts_spread` picks which GUN each attachment gets photographed on. Every way
it can break is quiet -- it returns a plausible list of guns either way, the
collection runs, and the corpus comes back looking broader than it is. So the
properties are pinned here rather than trusted:

    a drawn host can actually wear the part      else the round photographs an
                                                 empty slot labelled as a part
    a gun already holding it is never drawn      else 10 more crops of a
                                                 rendering already on disk
    what is on disk counts toward `want`         else every re-run asks for the
                                                 same work again
    the seed reproduces the draw                 else a crop that reads wrong
                                                 cannot be re-shot on its gun
    a guessed slot list loses ties               same reason it does in
                                                 hosts_for
    canonical() keys the coverage                41.1 replaced the Angled
                                                 Foregrip in place, and reading
                                                 the stored label instead
                                                 reported tilted_grip at zero
                                                 crops against 76 in the scorer

`have` IS SEEDED SYNTHETIC, not read off disk, except in the one case that is
explicitly about disk. A test fed today's corpus passes because of today's
data and stops testing the rule the moment someone collects a magazine.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from calibration.legacy_collect_templates import hosts_spread, slot_coverage
from detector.attachment_catalog import (ATTACHMENTS, RENAMED, ROSTER, SLOTS,
                                         canonical, fits)

FAIL = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"}  {name}' + (f'  — {detail}' if detail else ''))
    if not ok:
        FAIL.append(name)


def hosts_of(plan):
    """[(gun, [key...])] -> {key: {gun...}}"""
    out = {}
    for gun, keys in plan:
        for k in keys:
            out.setdefault(k, set()).add(gun)
    return out


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

    # A part with many possible hosts, one with few, one with exactly one.
    wide = [k for k in ATTACHMENTS
            if sum(1 for w in ROSTER if fits(w, k)) >= 10]
    narrow = [k for k in ATTACHMENTS
              if 2 <= sum(1 for w in ROSTER if fits(w, k)) <= 3]
    # The scarcest part that has ANY host. Not "exactly one host": nothing in
    # the catalogue has one, and pinning the rule to a population of size 1
    # made the gate fail for want of a fixture rather than for a defect.
    hosted = {k: sum(1 for w in ROSTER if fits(w, k)) for k in ATTACHMENTS}
    scarce = min((k for k in hosted if hosted[k] > 0),
                 key=lambda k: (hosted[k], k), default=None)
    keys = sorted(wide[:4] + narrow[:2])
    print(f'{len(keys)} keys under test: {" ".join(keys)}\n')

    print('every drawn host can wear the part')
    plan, _ = hosts_spread(keys, 3, seed=11)
    bad = [(g, k) for g, ks in plan for k in ks if not fits(g, k)]
    check('fits(host, key)', not bad, f'{bad[:3]}')

    print('\ncount and non-repetition')
    per = hosts_of(plan)
    short = [(k, len(v)) for k, v in per.items()
             if len(v) != min(3, sum(1 for w in ROSTER if fits(w, k)))]
    check('each key gets min(want, possible) distinct guns', not short, f'{short}')
    dupes = [(g, ks) for g, ks in plan if len(ks) != len(set(ks))]
    check('no key twice on one gun', not dupes, f'{dupes}')

    print('\nwhat is already on disk counts, and is never re-drawn')
    # ⚠ A WIDE part, deliberately. This was first written against keys[0],
    # which was `choke` with three possible hosts: with two of them on disk the
    # remaining pool is one gun, so `need = want` and `need = want - len(have)`
    # both draw that one gun and the mutation that ignores existing coverage
    # passed the gate. The fixture has to leave the pool LONGER than the ask.
    k0 = max(keys, key=lambda k: sum(1 for w in ROSTER if fits(w, k)))
    mine = [w for w in sorted(ROSTER) if fits(w, k0)][:2]
    have = {k0: {mine[0]: 10, mine[1]: 10}}
    plan2, _ = hosts_spread(keys, 3, seed=11, have=have)
    got = hosts_of(plan2).get(k0, set())
    check(f'{k0}: 2 on disk, want 3 -> asks for 1', len(got) == 1, f'{sorted(got)}')
    check('and not for a gun it already has', not (got & set(mine)), f'{sorted(got)}')

    satisfied = {k0: {w: 10 for w in [x for x in sorted(ROSTER) if fits(x, k0)][:3]}}
    plan3, _ = hosts_spread(keys, 3, seed=11, have=satisfied)
    check('a key already at `want` drops out entirely',
          k0 not in hosts_of(plan3), f'{sorted(hosts_of(plan3).get(k0, ()))}')

    print('\nthe seed reproduces the draw')
    a = hosts_spread(keys, 2, seed=5)[0]
    b = hosts_spread(keys, 2, seed=5)[0]
    c = hosts_spread(keys, 2, seed=6)[0]
    check('same seed, same plan', a == b)
    check('a different seed moves it', a != c,
          'if this ever fails the draw is not random, it is fixed')

    print('\nmore wanted than exist')
    if scarce:
        n = hosted[scarce]
        plan4, shorted = hosts_spread([scarce], n + 2, seed=1)
        check(f'{scarce}: {n} possible hosts, want {n + 2} -> reported short',
              shorted.get(scarce) == n, f'{shorted}')
        check('and it is still planned on every host that fits',
              len(hosts_of(plan4).get(scarce, ())) == n,
              'a part nobody can fully cover must still be collected as far '
              'as it goes, not dropped')
        _, none_short = hosts_spread([scarce], n, seed=1)
        check('asking for exactly what exists reports nothing short',
              not none_short, f'{none_short}')
    else:
        check('a part with at least one host exists to test with', False,
              'the catalogue has no wearable part at all')

    print('\na guessed slot list loses ties')
    guessed = [w for w in ROSTER if SLOTS.get(w, {}).get('conf') == 'guess']
    if guessed:
        k = next((k for k in ATTACHMENTS
                  if any(fits(w, k) for w in guessed)
                  and sum(1 for w in ROSTER if fits(w, k)) > 2), None)
        if k:
            n_sure = sum(1 for w in ROSTER
                         if fits(w, k) and SLOTS.get(w, {}).get('conf') != 'guess')
            drawn = hosts_of(hosts_spread([k], min(n_sure, 3), seed=3)[0]).get(k, set())
            check(f'{k}: verified hosts drawn before guessed ones',
                  not (drawn & set(guessed)), f'{sorted(drawn & set(guessed))}')
        else:
            check('a part with both guessed and verified hosts exists', True,
                  'skipped: none in the catalogue')
    else:
        check('a guessed slot list exists to test with', True,
              'skipped: every weapon in SLOTS is verified')

    print('\ncoverage is keyed by canonical(), on the real corpus')
    cov = slot_coverage()
    stale = sorted(set(cov) & set(RENAMED))
    check('no pre-rename key survives into the coverage', not stale,
          f'{stale} — a stored label is a picture of the NEW part under the '
          f'OLD name; canonical() resolves it and the manifest is never '
          f'rewritten')
    unresolved = sorted(k for k in cov if canonical(k) != k)
    check('every coverage key is its own canonical form', not unresolved,
          f'{unresolved}')

    # ⚠ THE HOST COLUMN MUST NAME A GUN. Both ways gun_of can go wrong produce
    # a string, not an error: a 库存 row's name is `row00__sks__r1__lbg0.png`,
    # whose third segment is `r1`, so letting rows through reported coverage on
    # 19 "guns" including r1, r2 and r13 — nearly double the truth, with every
    # other gate still green. Anything the spread planner cannot spawn has no
    # business being counted as a host.
    ghosts = sorted({g for v in cov.values() for g in v} - set(ROSTER))
    check('every host in the coverage is a weapon in ROSTER', not ghosts,
          f'{ghosts} — not spawnable, so not a host')

    print(f'\n{"FAILED: " + ", ".join(FAIL) if FAIL else "all gates pass"}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    raise SystemExit(main())
