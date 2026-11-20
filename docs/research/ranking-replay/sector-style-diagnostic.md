# Sector/style diagnostic of the 2019-2025 ranking replay (plan 2026-10-14, WP-B1)

Run on 2026-10-14, read-only, on prod (LXC 111). Pre-registered in ADR 0018's amendment of
2026-10-14, §9. This is one look at the data, to be counted as one `trial_ledger` row with
context `diagnostic` (owner action: the write needs your OK).

## Question

The replay's monthly rank IC had a standard deviation of 0.213. Random sampling alone, at
about 200 stocks a month, would give about 0.071. If sector and style exposures cause most of
that excess, a slope fitted after removing them would be a much faster test. The question was
fixed in advance:

> If the monthly residual IC standard deviation is ≤ 0.12, an "R Residual slope" family may be
> pre-registered. Otherwise stop. The residual mean IC is descriptive only.

## Method

- **Panel:** exam-v2 `composite.parquet` (84 month ends, 2019-01 to 2025-12, about 200 stocks
  a month; SHA-256 in `exam-v2-panels.sha256`).
- **Characteristics:** JKP `wrds_factor_characteristics_pit`, US rows, the last value ingested
  per security and month. Matched on permno, falling back to gvkey.
- **Each month:**
  - regress the composite on GICS sector dummies (the first two digits) and z-scores of log
    size, book-to-market, 12-1 momentum and 60-month beta;
  - take the Spearman IC of the residual against the forward return.
- **Months:** only those with at least 50 matched stocks count.

## Result

| | value |
|---|---|
| Months used | 83 |
| Rows matched to characteristics | 42 % (about 79 stocks a month) |
| Raw IC: mean / sd (matched rows) | 0.014 / 0.230 |
| Residual IC: mean / sd | 0.019 / **0.141** |
| Sampling-only sd at 79 stocks | 0.113 |
| Residual IC t (descriptive) | 1.24 |

## Reading

- **The gate is not met as written:** 0.141 > 0.12. Under the pre-registered rule there is
  **no R family**.
- **Sector and style do explain most of the excess variance.** The excess over sampling noise
  falls from 0.040 (raw) to 0.007 (residual) in variance terms, about 83 %.
- **The breadth of this sample is part of the miss.** At the full ~200 stocks a month, sampling
  noise alone would be about 0.071. With the same residual excess, the residual sd would be
  about 0.11.
  - That extrapolation is a reading **after** the look. Acting on it would need a new
    amendment that states this result.
  - It is the owner's decision, not this note's.
- **The 42 % match is a data gap.** Rows without a GICS code or with a missing characteristic
  in JKP were dropped. A better join (a CRSP sector, or JKP before 2025-12 for every name)
  could raise the coverage. Again, that would be a new, separately recorded look.

## Decision

Stop, as pre-registered: no "R Residual slope" family. The live evidence rests on the S and U
families (picks against same-run, same-sector non-picks), which remove sector exposure by
construction.
