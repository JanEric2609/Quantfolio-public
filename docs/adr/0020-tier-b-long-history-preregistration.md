# 0020 — Tier B: Long-History Tests of ETF Overlays, Pre-Registered

**Status:** Proposed (pre-registration; nothing has been run)
**Date:** 2026-10-12
**Context source:** the 2026-10-12 prod audit and its plan (`docs/plan-audit-fixes-2026-10-12.md`,
decision D13, section 4 "Trustworthy ETF recommendations quickly")
**Extends:** [0015](0015-honest-measurement-rebuild.md) (the trial ledger and the hard gate),
[0018](0018-trust-evidence-pre-registration.md) (live evidence as a kill switch),
[0019](0019-etf-fund-choice-and-stock-ranking.md) (the fund rule a passing tilt is bought through)
**Consumed by:** `backend/app/lab/tier_b.py`, `backend/scripts/tier_b_long_history.py`

This ADR is a **pre-registration**. It fixes the strategies, data, periods, costs, metrics and pass
bars *before* any result is seen. At the time of writing, no Tier B data had been pulled and no
statistic computed. Any amendment must say what had been observed when it was made.

## Context

Live data cannot show a realistic ETF tilt's skill in a lifetime. With tracking error TE and edge α,
a t-statistic of 2 needs about (2·TE/α)² years: a 2 %/yr edge at 5 % TE takes about 25 years, and a
1 % edge at 10 % TE about 400. E-processes give validity, not power. The app's Tier A decisions
(global cap-weighted core, fund choice by rule, asset mix by owner targets, covariance-only weights)
need no forecast. Anything beyond that (a trend overlay, a factor tilt) needs evidence that only
long history can give, gathered once, out of sample, with its tests fixed in advance.

## Decision

### Strategies (one trial each in `trial_ledger`, context `tier_b`)

| Trial | Rule |
|---|---|
| `B1_trend_10m` | Hold the equity index while its total-return level is above its 10-month moving average at month-end; otherwise hold cash (T-bills; €STR for euro investors). The signal at month *t* sets the position for month *t+1*. |
| `B1_trend_200d` | The same with a 200-trading-day average (about 9.5 months; on monthly data a 9-month window). Same family as B1. |
| `B2_min_vol` | Low-variance portfolio returns against the market. |
| `B3_size` | Small minus big (SMB). |
| `B4_value` | High minus low book-to-market (HML). |
| `B5_momentum` | 12-1 month momentum (UMD/WML). |

These six are the whole search. A further variant (another window, another cash proxy, another
weighting) is a new trial: it is recorded before it is run and raises `n_trials` for every gate.

### Data

- **CRSP monthly market** (`crsp.msi.vwretd`, 1926–) and the 30-day T-bill (`crsp.mcti.t30ret`),
  pulled from WRDS Postgres by `scripts/tier_b_long_history.py fetch-crsp` into parquet. The
  vendored `backend/wrds/` is a stub and is not used.
- **Ken French data library**: US factors 1926–; Developed ex US and Europe 1990–
  (`fetch-french`).
- **MSCI World / Europe end-of-month index levels** (MSCI's free end-of-day history, 1970–), a manual
  export, as a cross-check of the French market series.
- Nothing after **2025-12** is loaded, so the live period stays clean.

### Periods

- In sample: US 1926-07 to 1989-12.
- Out of sample: US 1990-01 to 2025-12, plus Developed ex US and Europe 1990-01 to 2025-12.

### Costs and taxes

- The TER difference against the core ETF (EUNL, 0.20 %) for any fund that would implement a tilt.
- B1: 0.10 % per switch.
- B1: German tax on the realised gain at each exit to cash, 26.375 % × (1 − 0.30) (the same
  constant as `factor_premia.AFTER_TAX_FACTOR`); a loss earns no refund (conservative). Reported a
  second time with no tax (NV-Bescheinigung).

### Pass bar (every condition must hold)

1. Newey-West t ≥ 2.0 (12 lags) on the **net** excess return in **every** out-of-sample region
   (at least two regions).
2. The same sign in sample as out of sample.
3. For B1 only: a maximum drawdown at least **25 % smaller** than buy-and-hold out of sample, with
   net CAGR no more than **1 pp/yr below** buy-and-hold.
4. A published premium is scaled by `PUBLICATION_DECAY = 0.58` (McLean and Pontiff) before the test.

### What a pass does

A pass yields a **tilt suggestion** inside `plan_tilt_max_pct`, implemented through the evidence gate
and the ADR-0019 fund rule. A live e-process monitors the tilt from its start and acts only as a kill
switch. A fail is recorded and is a result, not a reason to try another variant unrecorded.

### Expected outcomes, written before the run

- B1 is likely to pass the drawdown condition but not beat buy-and-hold net of costs and tax. Recent
  work finds short-horizon trend in equity indices weak since about 2009; the slower 10-month signal
  is the one with long-run support.
- B2 and B3 may pass in sample and weaken out of sample (post-publication decay).
- "No" is a valid and useful result.

## Consequences

**Gained.** An answer in weeks, from decades of data, to "does this tilt or overlay help?". Every
trial counts in `n_trials`, so the search is visible and paid for.

**Accepted.** Long history is US-heavy before 1990; the out-of-sample regions start in 1990. Factor
returns are paper portfolios, not investable funds; the TER and cost lines narrow, but do not close,
that gap. MSCI's free history needs a manual export.

## References

- Faber (2007), *A Quantitative Approach to Tactical Asset Allocation*, Journal of Wealth Management
  — the 10-month moving-average rule
- Moskowitz, Ooi & Pedersen (2012), *Time Series Momentum*, Journal of Financial Economics
- Fama & French (1993), *Common Risk Factors in the Returns on Stocks and Bonds*, JFE
- Jegadeesh & Titman (1993), *Returns to Buying Winners and Selling Losers*, Journal of Finance
- Blitz & van Vliet (2007), *The Volatility Effect*, Journal of Portfolio Management
- McLean & Pontiff (2016), *Does Academic Research Destroy Stock Return Predictability?*, JF
- Newey & West (1987), *A Simple, Positive Semi-Definite, Heteroskedasticity and Autocorrelation
  Consistent Covariance Matrix*, Econometrica
- Bailey & López de Prado (2014), *The Deflated Sharpe Ratio* — why every trial is counted
