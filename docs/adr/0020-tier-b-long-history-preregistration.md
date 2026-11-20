# 0020 — Tier B: Long-History Tests of ETF Overlays, Pre-Registered

**Status:** Accepted 2026-10-14, amended the same day before any data was loaded; run once the same day: no trial passes (see Results)
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

## Amendment 2026-10-14 (accepted with the ADR; nothing loaded or computed yet)

**Observed when this was written:** nothing. No Tier B data had been downloaded and no statistic had
been computed; the harness had only run on synthetic test data. The owner decided points 1 and 2.

1. **Publication decay, now effective.** The harness multiplied every out-of-sample return by 0.58.
   A constant rescaling leaves a t-statistic unchanged, so the rule did nothing (and 0.58 was used
   as the share kept, while `factor_premia` uses it as the share lost). Now returns dated **before**
   a strategy's publication keep `1 − PUBLICATION_DECAY` = 42 % of their size. Returns from the
   publication year on are real post-publication data, which already carry any decay, and are not
   cut again. Publication years: B1 2007 (Faber), B2 2007 (Blitz & van Vliet), B3 1981 (Banz),
   B4 1992 (Fama & French), B5 1993 (Jegadeesh & Titman). In sample only the sign is used, which the
   decay does not change.
2. **B2 data.** Ken French publishes variance-sorted portfolios for the US only. B2 has two
   out-of-sample regions: US (the value-weighted lowest-variance quintile, `Lo 20`, minus the market;
   in sample 1963-07 to 1989-12) and world (MSCI World Minimum Volatility minus MSCI World, monthly,
   EUR, net, a manual export from MSCI's end-of-day history). MSCI's minimum volatility history
   before its 2008 launch is MSCI's own back-calculation; this is accepted.
   The EUR export starts in December 2001, so the world region covers 2002-01 to 2025-12
   (288 months), not 1990 onward; the owner chose to run it so rather than switch to USD series.
   Recorded before any statistic was computed. MSCI's data stay outside both repositories (MSCI
   forbids redistribution); only summary statistics are published here.
3. **Costs for B2–B5.** The tilt's excess return pays `factor_premia.IMPLEMENTATION_COST` (0.4 %/yr:
   the TER gap to the core plus in-fund trading), which covers the TER difference named above. Tax
   then scales the excess by `AFTER_TAX_FACTOR`, since the tilt and the core are taxed alike.
4. **Two lines per trial.** The **taxed** line decides; the NV-Bescheinigung line (no tax) is
   reported beside it.
5. **Data as run.** B1 uses the Ken French market (Mkt-RF + RF, built from CRSP) with RF as cash, in
   USD for every region; €STR is not used, because the French international series are in USD.
   CRSP `crsp.msi` from WRDS, when fetched, is a correlation cross-check of the French US market,
   not an input. The MSCI World export replaces the planned MSCI cross-check.

## Results 2026-10-14 (run once, as registered)

Run after the six trials were recorded in prod's `trial_ledger` (context `tier_b`, n_trials 6,253)
and after the amendment above was committed. Data: Ken French library (downloaded 2026-10-14, cut
at 2025-12) and the MSCI EUR exports. **No trial passes, taxed or without tax.** Satellite and
overlay suggestions stay off; the core-only plan (Tier A) is unchanged.

Out-of-sample Newey-West t of the net excess return (taxed line; t ≥ 2.0 needed in every region),
mean excess per year in brackets:

| Trial | In sample (US) | US 1990–2025 | Developed ex US | Europe | Failed on |
|---|---|---|---|---|---|
| B1_trend_10m | −1.50 | −1.54 (−3.1 %) | −1.40 (−2.2 %) | −1.24 (−2.1 %) | t in every region; net CAGR 2.8 pp/yr below buy-and-hold (drawdown cut 50 %, passes) |
| B1_trend_200d | −1.72 | −1.51 (−3.3 %) | −1.20 (−2.1 %) | −1.18 (−2.2 %) | t in every region; net CAGR 3.1 pp/yr below (drawdown cut 55 %, passes) |
| B2_min_vol | −0.35 | −0.38 (−0.5 %) | — | world 2002–2025: −1.07 (−1.1 %) | t in both regions |
| B3_size | +1.51 | +0.05 (+0.1 %) | −0.70 (−0.7 %) | −0.75 (−0.8 %) | t everywhere; sign flips outside the US |
| B4_value | +2.99 | +0.73 (+1.1 %) | +2.19 (+3.6 %) | +1.59 (+2.9 %) | US and Europe below 2.0 |
| B5_momentum | +4.46 | +1.64 (+4.0 %) | +3.51 (+6.1 %) | +4.28 (+8.1 %) | US below 2.0 |

Without tax (NV-Bescheinigung) the t-statistics of B2–B5 are unchanged (tax scales the excess) and
B1 improves but stays negative in every region (US −0.77 / −0.76), with net CAGR 0.9 and 1.2 pp/yr
below buy-and-hold.

What this says, as the expected outcomes predicted: the trend overlay roughly halves drawdowns but
costs about 3 pp a year after switching costs and tax. Size and minimum volatility show nothing out
of sample. Value and momentum remain positive long-short premia outside the US, but neither clears
the bar in every region, and both are paper long-short portfolios; a long-only ETF tilt would capture
less. Momentum is the nearest miss (US t 1.64). Per this ADR, a fail is a result: no variant is to
be tried unrecorded, and any new trial raises `n_trials` for every gate.

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
- Banz (1981), *The Relationship between Return and Market Value of Common Stocks*, JFE
- Fama & French (1992), *The Cross-Section of Expected Stock Returns*, Journal of Finance
- Fama & French (1993), *Common Risk Factors in the Returns on Stocks and Bonds*, JFE
- Jegadeesh & Titman (1993), *Returns to Buying Winners and Selling Losers*, Journal of Finance
- Blitz & van Vliet (2007), *The Volatility Effect*, Journal of Portfolio Management
- McLean & Pontiff (2016), *Does Academic Research Destroy Stock Return Predictability?*, JF
- Newey & West (1987), *A Simple, Positive Semi-Definite, Heteroskedasticity and Autocorrelation
  Consistent Covariance Matrix*, Econometrica
- Bailey & López de Prado (2014), *The Deflated Sharpe Ratio* — why every trial is counted
