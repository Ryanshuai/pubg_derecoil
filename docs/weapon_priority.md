# Which gun to measure next, and how much of that order is evidence

Asked 2026-08-09:「接着扩展到其他枪 按照网上统计的使用频率」.

⚠ **THE TOP SEVEN ARE SOURCED AND THE REST IS NOT, and the line between them is
the only reason this file exists.** A priority list that cannot say where each
position came from reads exactly like one that can, and this repository has
already paid for a table where 22 wiki readings, 6 guesses and 2 screenshot
reads were indistinguishable from each other (`attachment_catalog.SLOTS`).

## Sourced — WinnerMeta live telemetry, share of all AR/SMG bullets fired

Read 2026-08-09 from their June 2026 full-auto update (telemetry through
June 7) and the UMP45 weapon page.

| # | weapon | shot share | note from the source |
|---|---|---|---|
| 1 | **AUG** | **18.29%** | most-fired full-auto weapon, >3 pts clear of the M416 |
| 2 | **M416** | ~15% | "tops win rates and pick rates", pro pick rate 65%+ |
| 3 | **MP5K** | — | top SMG "by a big margin" |
| 4 | **M762** (Beryl) | — | fourth by volume, 1.282% kill/shot, best value in the top five |
| 5 | **ACE32** | **9.82%** | 1.263% kill/shot |
| 6 | **UMP45** | **5.75%** | rose from 5.51% (May) and 5.09% (April) |
| 7 | **AKM** | — | seventh by shot share, 1.634% kill/shot — the best conversion of the common full-autos |

Sources:
- <https://www.winnermeta.win/pubg/blog/pubg-weapon-meta-full-auto-june-2026>
- <https://www.winnermeta.win/pubg/weapon/UMP>
- <https://www.winnermeta.win/pubg/weapons>

⚠ **These are PC telemetry.** Several of the other pages returned by the same
search are PUBG **Mobile** tier lists, which are a different game with different
recoil; none of their rankings are used here.

⚠ **Shot share is not the same question as "which curve is worth measuring".**
It says which gun is fired most, not which gun is hardest to control — the AKM
is 7th by volume with the strongest kill/shot of the group, which is the profile
of a weapon people fire less because it is harder to hold. A recoil curve helps
most exactly there. The order below follows usage because that is what was
asked; if the goal changes to "where does compensation buy the most", the AKM
and the M762 move up.

## NOT sourced — everything below rank 7

The June update names only the top five, and the searches did not return a full
table. The rest of the roster is ordered by **class and fire rate**, which is a
property of the guns rather than of anybody's play:

    qbz g36c k2 famas          ARs, the same slot layout as the ones above
    uzi mp9 js9 tommy          SMGs, cheap to measure (short bursts)
    m249                       LMG
    mk14 vss                   DMRs, and both need a baseline from somewhere

**Do not read that block as a popularity ranking.** It is a work order.

## Two weapons cannot start at all

| weapon | why |
|---|---|
| `mk14` | no Kava4 pattern — the script covers ARs and SMGs |
| `vss` | same, and `import_kava4` already says so in its own report |

A gun with no seed fires uncompensated, and for these the view reaches open sky,
where phase correlation returns 0 **confidently** and the magazine is lost with
every gate green. They need a baseline from somewhere else or a low aim.

## One number that looks wrong

`WEAPON_RPM['m249']` reads **194.7**. The M249 fires far faster than that, and
the rate decides both the burst length and the seed's span, so a wrong one
produces a curve of the wrong shape and nothing downstream contradicts it.
⚠ Measure it before spending magazines on that gun — `calibration/rpm_store.py`.
