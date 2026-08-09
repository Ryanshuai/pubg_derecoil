# Every stored m416 magazine holds 40 rounds. Tonight's hold 42.

Found 2026-08-09 by the capacity refusal in `calibration/collect_timed.py`,
which is the check that exists for exactly this and had never fired before:

    magazine 1: this magazine holds 42 rounds and every stored m416 magazine
    holds [40]; a cell whose burst is a different LENGTH cannot be compared
    with the others

## What is observed, and only that

| | |
|---|---|
| stored | **143 magazines, all 40 rounds, all from 2026-08-08** |
| tonight | **42 rounds**, first magazine of the first m416 cell |
| what changed | the setup order: gun spawned ALONE, its factory magazine stripped, parts spawned after |
| `config` on every stored magazine | `{}` — **the magazine slot is not in the pooling key at all** |

## What `control/stock.py` already measured, which explains it

That file carries a measurement from 2026-08-07, four m416 cells:

> The three that spawned `give_many(['ext_ar', 'm416'])` all fired 40 rounds —
> the factory **quickext_ar** — while the slot read back `ext_ar`, because the
> two icons are close enough that the template calls both the same. The one
> that spawned the parts first and `give_many(['m416'])` after fired **42**.

40 is the factory quick-draw magazine. 42 is the extended one that was asked
for. So the reading is that **the stored m416 corpus was fired on the wrong
magazine**, and tonight's order — strip the factory magazine while it is the
only magazine in play, then spawn — produces the requested one.

⚠ **THAT IS AN INFERENCE, NOT A MEASUREMENT TAKEN HERE.** What was observed is
that the capacity changed when the setup order changed, and that a file in this
repository already records which order yields which number. Nobody has read the
m416's magazine slot back on both orders in one session and compared. Until
somebody does, "the corpus is on quickext_ar" is the best available reading and
not a fact.

## Why nothing caught it for a day

`config_key` is built from `RECOIL_SLOTS`, and the magazine is not among them.
So two guns differing only in magazine pool into the same cell and the key
cannot say they differ — the same shape as the optic before `--sight` was
checked against the readback, and as ADS before `ads_end` existed. **The one
witness was the round count**, which is why the capacity refusal is the check
that found it.

## What has NOT been done

- **Nothing is quarantined.** `calibration/samples.py` has the mechanism —
  rename to `<weapon>__<config>.MISLABELLED_<why>.jsonl`, never delete — and
  143 magazines is not a call to make unattended on an inference.
- **The fitted curve on disk is untouched.**
  `data/curves/m416__grip-vert_grip_muzzle-comp_ar_stock-tactical_stock.json`
  says 28 magazines and 895 counts. If the corpus is on the wrong magazine,
  that curve is a 40-round answer to a 42-round question.
- **The m416 cells fail closed** and the night moves to the next weapon. That is
  the refusal working; no bad data enters the pool.

## The check worth adding either way

The magazine belongs in the pooling key, or the capacity belongs in the config.
As it stands the store cannot represent the difference it just refused.
