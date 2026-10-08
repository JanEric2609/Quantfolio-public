# 0018 — Trust Evidence Pre-Registration: Calendar-Time Tests, a Shadow Ledger, Provenance

**Status:** Accepted (Phases 1–3 implemented; see the amendments of 2026-10-07 and 2026-10-08)
**Date:** 2026-10-07
**Context source:** the 2026-10-07 research on the "Can I trust it?" banner ("1 rebalance
dates so far … about 4,100 dates (roughly 16 years)"), and the follow-up plan "honest
evidence, sooner" (six research reports, 234 sources this round, 446 across both rounds;
simulations re-running `foundation.forecast_verification`).
**Extends:** [0015](0015-honest-measurement-rebuild.md) (the hard statistical gate) and
[0017](0017-discover-scoring-integrity.md) (the composite the shadow ledger ranks by)
**Consumed by:** `backend/app/foundation/provenance.py`,
`backend/app/decision/discover/{orchestrator,predictor,shadow_ledger}.py`,
`backend/app/decision/advisor/cycle.py`,
`backend/app/decision/verification/{daily_ledger,daily_tests,candidate_outcomes,ranking_metrics,history,factor_study,trust,jobs}.py`,
`backend/app/decision/discover/calibrator.py`,
`backend/app/foundation/models/entities/trust_ledger.py`,
`backend/alembic/versions/0130_trust_evidence_ledgers.py`, `backend/alembic/versions/0131_trust_factor_study.py`

This ADR is a **pre-registration**. It fixes the tests, the metrics, the thresholds and the
priors *before* the outcomes they judge resolve. At the time of writing, one rebalance date
had resolved, and every e-value was about 1. Changing the design now therefore costs nothing.
Changing it later would be a look at the data. Any amendment must say what had been observed
when it was made.

## Context

The banner was right in kind but optimistic in size. Judging one equal-weight basket per
rebalance date against the ETF needs about 4,100 daily dates at an assumed +1 % per 21 days
with 5 % noise. Overlapping windows that share their market path push that to about 20–24
years (q10/q50/q90 ≈ 12/21/34 years). Firms do not wait that long. They get to a verdict sooner
by measuring with more breadth, by testing historically and holding the live test separately,
and by stating every assumption in advance. The code read found six things:

1. **Discover's LLM neither chooses the picks nor sets their conviction.** The picks are the
   top composite scores after the sector cap and the tradeability gate. `conviction` *is*
   the composite. The LLM only writes the dossier. So the live Discover record tests the
   composite. Only the advisor's conviction is the LLM's own confidence.
2. **Every scored stock is already in the database, but not as evidence.**
   `DiscoverCandidate` keeps the scores of every universe member. Those rows are mutable,
   however: the status changes stage by stage, and `_attach_earnings` rewrites
   `scores_json`. They also have no entry price and are never resolved.
3. **The live test has a formal stopping gap.** It averages lag-h chains (Henzi & Ziegel
   2022) and stops at the first e ≥ 80. Prop. 3.5 gives only E[e_{τ+h−1}] ≤ 1. The safe
   stopping rule (App. A.2) inflates the bar by the worst case of the bets still in flight,
   which here is up to e ≥ 160.
4. **The "time to know" simulator is optimistic.** It draws the 21 chains independently.
5. **The calibrator is fragile.** It runs Platt scaling from 20 resolved picks, which is one
   or two dates sharing one market move. It has no shrinkage, and its hit definition mixes
   excess return with raw return.
6. **Nothing identifies what produced a prediction.** Predictions carry no code version,
   config hash, model identity or calibrator version, and raw prompts expire after 24 h.

## Decision

### 1. What the page judges (unchanged in spirit, now explicit)

The page judges two things, kept apart:

- the **historical evidence**: the factor tilt, the stock-ranking model and the satellite
  (ADR 0015);
- the **live evidence**: the calls this system made after it made them.

One never vouches for the other. The historical panel says plainly that it does not test
Discover, the advisor, the mandates, or anything an LLM wrote.

### 2. Families, hypotheses and multiplicity

| Family | Question | Hypotheses | e-BH | One rejection needs | Role |
|---|---|---|---|---|---|
| **F1 Ideas vs ETF** | Do Discover's picks, held as issued, beat the passive core ETF? | skill, harm | K = 2, α = 5 % | e ≥ 40 | **primary**, drives the headline |
| **F2 Ranking** | Does the composite sort winners from losers among every scored stock? | skill, harm | K = 2, α = 5 % | e ≥ 40 | **primary**, separate question, own row |
| **F3 Advisor vs ETF** | Do the advisor's directional calls beat the ETF? | skill, harm | K = 2, α = 5 % | e ≥ 40 | secondary (paper-only research loop) |

- **The advisor leaves the picks' family.** Before this ADR, F1 and F3 shared one e-BH
  family with K = 4, so a rejection needed e ≥ 80. The advisor is a paper-only research
  loop, so it gets its own family, like the mandates' exclusion. That makes the F1 verdict
  roughly 15–20 % faster. F3's verdict is labelled secondary and never sets the headline.
- **No combined claim.** F1 and F2 answer different questions and are reported separately.
  The page never adds them up into one "skill" claim.
- **Mandates** still get no verdict: a stated expectation has no naive benchmark. **Regime**
  labels stay unscored.
- **Start of the test (T0).** The primary e-processes start on **2026-10-12**, or on the
  first trading day after this ADR is merged if that is later. Ledger rows before T0 are
  kept and shown as "before pre-registration", but they never enter an e-value.

### 3. The calendar-time daily ledger (F1, F3)

The observation is the daily active return of all open calls. This is the calendar-time
portfolio of Fama (1998) and Mitchell & Stafford (2000). It replaces the per-date basket
outcome, so overlapping holding windows stop being a problem.

- **Calendar:** the trading days of the passive core's EUR close series (`EUNL.DE` by
  default; the benchmark is the `passive_core_ticker` setting, the same one resolution and
  the trust page use).
- **Membership:** a call issued on UTC date *d* is a member on trading days *s* with
  *d* < *s* ≤ the *H*-th trading day after *d* (*H* = its `horizon_days`, 21).
  - It enters at the close of *d* and earns its first return from close(*d*) to
    close(*d* + 1). This is stricter than resolution's entry at close(*d* − 1), because
    the weekly run at 05:00 UTC already knows the overnight move.
  - Membership on *s* depends only on rows issued before *s*.
- **Which calls count:**
  - F1: `DiscoveryPrediction` rows without a paper sleeve.
  - F3: rows with a paper sleeve and direction `buy` or `sell`. Logged holds are not
    directional bets and are left out.
- **Returns:** each close is restated in EUR (`foundation.eur_prices`) and carried forward
  across days with no close. Each call's daily active return is
  sign × (r_i,s − r_bench,s), where sign is −1 for sells and +1 otherwise.
  - The day's observation is the equal-weight mean over members:
    r_s = (1/N) Σ sign_i · (r_i,s − r_bench,s).
  - A member with no fresh close that day contributes its carried price (own return 0)
    and is counted in `n_stale`.
  - A pick that stops trading therefore turns into cash (active return = −r_bench) for the
    rest of its window. It is never dropped.
- **Freezing:** a day is written once and never rewritten.
  - A day is written when the benchmark has a close for it, and either every member has a
    close on it or the day is at least 5 trading days old (grace for a data outage).
  - A day with no members is written with `n_open = 0` and no observation.
  - Late price corrections never change a written day.

### 4. The primary test (F1, F2, F3)

- **Clip and map:** x_s = clip(r_s, ±C) with C = **5 %** (4.5 × σ_ref, σ_ref = 1.1 % daily
  = 5 %/√21), mapped to y_s = (x_s + C)/(2C) ∈ [0, 1] with p0 = 0.5.
- **What is tested:** under the clip, H0 is E[x_s | F_{s−1}] ≤ 0 (skill) or ≥ 0 (harm).
  This is a statement about the clipped mean, and the page says so.
- **Bettor:** the aGRAPA bettor of `fv.e_process_bernoulli` with a pseudo-variance of
  (σ_ref/2C)² = 0.0121.
  - The bet cap rises from 0.5/p0 to **0.75/p0** (every factor ≥ 0.25).
  - At these scales the optimal bet stays far below the cap, so the cap only guards
    against outliers.
- **No chains.** Every observation is complete when it is recorded, so there are no bets in
  flight and no stopping gap.
- **Autocorrelation.** After 3 months of daily data the lag-1 autocorrelation of r_s is
  measured once. If |ρ1| > 0.15, the test switches to AR(1) residuals, with ρ1 refitted
  predictably from data before each day. This rule is fixed now. Its outcome is not.
- **The legacy chain test** (the lag-h basket e-process) stays as a secondary row. From now
  on it reports `e_lower`: the e-value if every in-flight call resolves at its worst. Only
  `e_lower` may cross a threshold. It no longer sets any state.

### 5. The shadow ledger (F2 and descriptive metrics)

- **Snapshot:** one immutable `discover_candidate_snapshot` row per run × universe member.
  - It is written right after the tradeability gate and backfill, before dossiers.
  - It is built from the pipeline's in-memory results, not from the mutable
    `DiscoverCandidate` rows.
  - It records `evaluable` (no pipeline `reject_stage`), the composite, the rank among
    evaluable **stocks**, `shortlisted`, `sector_capped` and `picked`.
  - It also records the instrument group (stock / ETF / other) and the entry close as
    known at issue.
- **F2 observation:**
  - Runs with **≥ 30 evaluable stocks** form cohorts.
  - Within a cohort, the weights are proportional to the centred rank of the composite
    (average ranks for ties), scaled so that the long leg sums to +1 and the short leg to −1.
  - The cohort's daily return is Σ w_i r_i,s in EUR, with the same calendar, entry rule,
    carry-forward and 21-day window as §3. The benchmark cancels because Σ w = 0.
  - r_s is the mean over open cohorts. It is frozen and tested exactly as in §3–4.
- **Outcomes** (`candidate_outcome`): one row per snapshot × horizon *H* ∈ {5, 10, 21, 63}
  trading days, on the same calendar.
  - Entry is the last close on or before *d*. Exit is the last close on or before the
    *H*-th trading day. Both are in EUR, together with the benchmark's return over the same
    window and the excess.
  - A name without an exit close 30 days past its horizon is marked `delisted` at its last
    close. One with no entry close is marked `no_price`.
  - Rows are append-only.
- **Descriptive only** (shown, never a verdict):
  - per-run Spearman IC at 21 days on evaluable stocks, mean IC with a Newey-West t (lag 5),
    ICIR, and the share of runs with IC > 0;
  - IC decay across 5/10/21/63 days, quintile spreads, top-15 against the rest, and
    sector-neutral IC;
  - **gate check:** for each `reject_stage`, the run-paired mean 21-day excess of rejected
    against evaluable names, with a run-block bootstrap interval;
  - **ETFs** as their own group, never pooled with stocks.
- **Backfill:** snapshots rebuilt from historical `DiscoverCandidate` rows are flagged
  `backfilled = true` with cohort `legacy_unstamped`. They are shown as exploratory and
  never enter an e-value.

### 6. Calibration (Phase 2; fixed now)

- **Display:**
  - Discover shows a score tier ("top third of this week's pool") plus the tier's measured
    hit rate with a Beta(1, 1) posterior range, and the base rate.
  - **No probability is shown until n_eff ≥ 100**, where n_eff counts issue dates, not picks.
  - Advisor and mandate confidence carry the label "the model's own confidence (not a
    probability)".
- **Calibrator stages, by n_eff:**
  - Below 100: base rate.
  - 100–1,000: logistic with slope ≥ 0, a N(0, 1²) prior on the slope over the
    standardised score, and the likelihood weighted 1/(picks on that date).
  - Above 1,000: isotonic or Venn-Abers.
- **Fitting rules:**
  - Hit = **excess > 0 only**.
  - Fit on matured outcomes with a 21-trading-day embargo, on an expanding window.
  - Each prediction stores its `calibrator_version`.
- **Viability gate:** if the calibrated value moves by less than 1 percentage point between
  the 10th and the 90th score percentile, the page shows "no usable signal" and the base
  rate.
- **Metrics:**
  - Brier skill bootstraps over issue dates in blocks of ≥ 5 weeks.
  - Spiegelhalter Z uses date-aggregated residuals.
  - Reliability uses CORP (PAV) with MCB/DSC/UNC.
- **Ranges:** coverage is measured per date. An online conformal correction (ACI) of the
  stated ranges starts after 30 matured weeks.

### 7. Bayesian posterior (Phase 2; fixed now)

- **Model:** the conjugate normal posterior of the mean daily active return, with a HAC
  variance and the prior α ~ N(0, τ²), **τ = 0.5 % per 21 days**.
- **Display:** P(edge > 0) and a 90 % credible interval, with the prior printed, plus a
  sensitivity strip at τ ∈ {0.25, 0.5, 1, 2} % per 21 days.
- **It is never thresholded.** With no true edge, "P > 95 %" fires at some point in 8–22 %
  of simulated paths. Only §4 can say "skill".

### 8. Factor-neutral row (Phase 3; gated now)

- **The study comes first.** It runs on back-filled Discover baskets against a
  pre-registered factor set:
  - market beta to the passive core, sector, and USD/EUR;
  - the MSCI World value, momentum, quality, minimum-volatility and small-cap factor ETFs.
  - The exact listings are fixed in the study's own spec before it runs.
- **Exposures** use only data from before each window.
- **Gate:** the secondary "after removing factor moves" row is built only if the
  out-of-sample R² ≥ **0.30**. The idea is dropped below **0.15**.
- **If built,** the row is secondary and never in F1's e-BH.

### 9. Factor-tilt review rule (Phase 2; fixed now)

The live tilt goes to **"Under review"** if either of these happens:

- its relative drawdown against the passive core exceeds the 95th percentile of the
  haircut backtest's relative drawdowns, or
- its time under water exceeds the 95th percentile of the haircut backtest's.

This triggers a review, never an automatic sale. The rule does not apply before 12 months
of live data.

### 10. Provenance and cohorts

- **The stamp.** Every `DiscoveryPrediction` written from now on carries `provenance_json`,
  written in the same transaction:
  - `provenance_schema_version`
  - `cohort_id`
  - `code_sha` (the deployed git commit, read from the checkout)
  - `config_hash` (the canonical JSON hash of the *resolved* config, not the mutable
    `config_id`)
  - `calibrator_version`
  - the scoring versions
  - for LLM-backed calls: the served model path and llama.cpp build from `/props`, the
    sampling preset, the prompt template hash, the hash of the accepted output, the parse
    status and the attempt count
  - `degradation_flags`
- **Cohort = the hash of everything that changes what is tested.** For Discover, the LLM
  identity is deliberately *not* in the cohort, because the LLM does not touch the
  composite, the pick or the ranges. For the advisor, it is.
- **What creates a new cohort:**
  - Any change to the hashed spec.
  - Composite and universe semantics changes, which must bump `COMPOSITE_FORMULA_VERSION`
    or `UNIVERSE_RULE_VERSION` in `discover/shadow_ledger.py`.
  - Every cohort is one trial.
- **Rows before this ADR** form one `legacy_unstamped` cohort. They are analysed separately
  and never back-filled with guesses.
- **Verdicts.** The pooled verdict is the pre-registered one. Per-cohort rows are
  descriptive. Rows with degradation flags are counted separately.
- **Append-only (Postgres trigger).**
  - The trigger rejects any update of an issue-time column of `discovery_prediction`.
    `conviction_calibrated` may only go from NULL to a value.
  - It rejects any update at all of `discover_candidate_snapshot`,
    `trust_daily_active_returns` and `candidate_outcome`.
  - Deletes stay possible (user deletion cascades).
  - A deliberate repair must drop the trigger in a migration that says why.
- **Known gap.** The model file's SHA-256 is not stamped. GGUF has no checksum field, and the
  app cannot read the LLM host's disk. The served model path plus the llama.cpp build stands
  in for it.

## Phases

| Phase | Scope | Status |
|---|---|---|
| 0 | This ADR | done |
| 1 | §10 provenance stamp and triggers; §5 snapshot capture and outcome resolution; §3 daily ledger for F1, F2 and F3 (logging only, no UI change) | done with this ADR |
| 2 | §4 tests and the realistic "time to know" simulator; the historical panel; §6 calibration; §7 posterior; backfill; the trust page redesign | done (amendment below) |
| 3 | §8 study, the advisor-against-Discover paired comparison, ACI ranges | done (amendment of 2026-10-08) |

## Amendment of 2026-10-07 (Phase 2)

Observed when this was written: no trading day on or after T0, and one resolved
rebalance date in the legacy ledger. None of these changes could have been
informed by an outcome.

1. **AR(1) whitening withdrawn (§4).** The null is conditional:
   E[x_s | F_{s−1}] ≤ 0. Autocorrelation does not break it, so the e-process
   stays valid as it is. Whitening with a fitted ρ would break it: under H0,
   E[x_s − ρ x_{s−1} | F_{s−1}] can be positive whenever x_{s−1} < 0. The lag-1
   autocorrelation of the first 63 observed days is still measured once and
   shown, flagged above |0.15|. It changes nothing.
2. **"Too early" means fewer than 63 trading days** with an observation and no
   rejection. This is descriptive only; rejections at any time still count.
3. **Posterior floor (§7).** Below 63 days, the standard error of the mean is
   at least σ_ref/√n. Otherwise a lucky quiet first week would look certain.
   τ and the model are unchanged.
4. **Time to know (§4, plan B4).**
   - Each path draws independent daily observations, because calendar-time
     days share no holding window. It runs the production bettor (same clip,
     pseudo-variance and cap) until e ≥ 40. The page shows 10/50/90 % and the
     chance of a verdict within 5, 10 and 20 years.
   - Ideas and advisor assume +1 % per 21 days with 5 % basket noise. From 63
     days on, the measured daily standard deviation replaces the assumed noise.
   - Ranking assumes a rank IC of 0.07 with an IC standard deviation of 0.12
     per run.
   - Results:
     - F1: median 18.6 years (10–90 %: 5.9–42).
     - F2: 2.0 years (0.7–4.2) at IC 0.07, and 6.5 years at IC 0.04. The
       "3–5 years at 0.04" under Consequences was a planning estimate; this
       value replaces it.
   - Measured values feed only this forecast, never the test.
5. **Calibrator embargo (§6).** A resolved row trains a new call only if its
   `resolve_at` is at least 21 business days before the call's issue date.
   Rows resolved without a benchmark price are left out (hit = excess > 0
   only). The rule is `CALIBRATOR_VERSION` rule 2.
6. **Tier display (§6).** Tiers are thirds of a run's evaluable stocks by
   composite.
   - A tier's hit rate is the mean over runs of the share of its names whose
     21-day excess was positive.
   - Its Beta(1, 1) range treats the issue dates as the trials. The rate times
     n_eff counts as the successes.
7. **Tilt review rule (§9).** The page states the rule and says that it
   applies from 12 months of live tilt data. The drawdown comparison will be
   built before then. The rule itself is unchanged.
8. **Legacy chain row.** `e_lower` appends every pending issue date to its
   chain at the clip against the hypothesis and recomputes. It needs e ≥ 40,
   like everything else now that K = 2.

## Amendment of 2026-10-08 (Phase 3)

Observed when this was written: still no trading day on or after T0, one
resolved rebalance date in the legacy ledger, no factor-study result (its table
is new with this amendment), and far fewer than 30 matured weeks of stated
ranges. None of these choices could have been informed by an outcome.

1. **ACI ranges (§6).**
   - **Unit:** the issue date, as everywhere else on the page. A date's error
     is the share of its outcomes outside the corrected band.
   - **Score:** the distance outside the stated band in units of its width
     (negative inside). The corrected band is `[low − q·w, high + q·w]`, with
     `q` the `1 − α_t` empirical quantile of earlier scores; it may narrow by
     at most 49 % of the width on each side.
   - **Update:** `α_{t+1} = α_t + γ(α − err_t)` with γ = **0.005** (Gibbs &
     Candès 2021), applied when a date has matured. A date's correction reads
     only dates whose outcomes matured strictly before it was issued.
   - **Start:** after **30 distinct ISO weeks** of matured issue dates; before
     that the stated band stands and α does not move.
   - **Scope:** Discover's P10–P90 (80 %) and the advisor's P5–P95 (90 %)
     ranges, each on its own. The stored ranges are issue-time columns and are
     never rewritten; the page shows the correction the next range would get
     and the corrected against the stated coverage on the same dates. Feeding
     the correction into the ranges Discover displays is a later decision.
2. **Advisor against Discover (Phase 3).** On every trading day from T0 on
   which both the F1 and the F3 series have an observation, the advisor's
   daily active return minus the picks'. The benchmark cancels. Shown: the
   mean per 21 days with a Newey-West interval, the share of days the advisor
   was ahead, and the cumulative path. **Descriptive only:** no e-value, no
   threshold, no state; F1 keeps the headline. Below 63 common days the page
   says to read it as noise.
3. **The §8 study, spec version 1** (fixed in
   `verification/factor_study.py:spec()`, stored with the result).
   - **Data:** back-filled baskets only. Each back-filled run's picked stocks,
     equal-weight, entered at the issue-date close, held 21 trading days; the
     day's value is the mean `r_i − r_core` over open picks (§3's
     construction).
   - **Listings:** iShares MSCI World factor ETFs on Xetra, in EUR: value
     **IS3S.DE**, momentum **IS3R.DE**, quality **IS3Q.DE**, minimum
     volatility **IQQ0.DE**, size **IUSN.DE**, and Europe: iShares STOXX
     Europe 600 **EXSA.DE**, each minus `r_core`. Market:
     `r_core` (the passive core). USD/EUR: the daily change of EUR per USD
     from market FX bars. Sector: the equal-weight return of the same run's
     evaluable stocks in the pick's sector that were **not picked**, minus
     `r_core` (with a few names per sector, a sector average that included
     the basket would mostly be the basket).
   - **Missing data:** a day enters only if every factor ETF and the USD/EUR
     rate have a value dated that day and one dated the trading day before,
     so every factor is a one-day move like the stock returns. A missing factor is
     never filled in as zero; the job fetches the factor ETFs and the rate
     from the provider, since nothing else keeps them current.
   - **Fit:** OLS with an intercept on at most the 252 days before each day,
     from 126 days on. Out-of-sample R² against the prior mean (Campbell &
     Thompson 2008).
   - **Run once:** recorded the first time it has **126 out-of-sample days**
     (so at least 252 back-filled days), per user, append-only. **build** at
     R² ≥ 0.30, **drop** below 0.15, **neither** in between. "Neither" is not
     re-run: a new factor set is a new spec version, decided by a person.
   - **The row:** only after "build", the Saturday job freezes
     `ideas_neutral`: for each frozen F1 day, the F1 value minus the fitted
     factor part (the intercept stays in), with exposures fitted on the days
     before it (the study's days, then the live ones). Secondary, mean and
     Newey-West interval only, never in F1's e-BH.

**Addendum, still 2026-10-08, before the first study run.** Spec v1 gains a
ninth factor, `europe` (EXSA.DE minus `r_core`). The core is roughly 70 % US
and Discover also picks European names, so nothing else in the set captured a
Europe-against-the-world move. Observed when this was added: the study had
never run (the first run is the Saturday job of 2026-10-10), so
`trust_factor_study` was empty and no fit or R² had been computed. Spec v1 is
therefore still unrecorded, and the version number stays 1.

## Consequences

**Gained.**

- The ranking question can be answered in about 2 years (10–90 %: 0.7–4.2) if the
  composite's true IC is 0.07, or about 6.5 years at 0.04 (amendment, item 4). If it has no edge, the page shows "no large edge" within
  1–2 years.
- The picks' verdict stops having a stopping gap and gets about 15–20 % faster (K = 2).
- Every later comparison can tell which version produced a call.

**Costs.**

- Two daily price passes: one nightly over the open picks (about 15–60 symbols), and one
  weekly (Saturday) over the open ranking cohorts (about 300 symbols, roughly the same load
  as one Discover run).
- About 0.3 M stored values a year.
- A Postgres trigger that a future data repair has to drop deliberately.

**Rejected alternatives.**

- Seeded random "exploration" slots in the shortlist. The LLM chooses nothing, so there is
  nothing to de-bias, and it would show random stocks.
- Thresholding the posterior (see §7).
- LLM fine-tuning and GPU rental: out of scope by decision.
- Keeping K = 4 with the advisor in F1: slower, for a paper-only loop.

## References

- Henzi & Ziegel (2022), *Valid sequential inference on probability forecast performance*,
  arXiv:2103.08402: Prop. 3.4, Prop. 3.5, App. A.2.
- Waudby-Smith & Ramdas (2024), *Estimating means of bounded random variables by betting*
  (aGRAPA).
- Wang & Ramdas (2022), *False discovery rate control with e-values* (e-BH).
- Fama (1998), *Market efficiency, long-term returns, and behavioral finance*; Mitchell &
  Stafford (2000), *Managerial decisions and long-term stock price performance*
  (calendar-time portfolios).
- Grinold & Kahn, *Active Portfolio Management* (IR = IC·√breadth).
- McLean & Pontiff (2016), *Does academic research destroy stock return predictability?*
  (the 58 % decay haircut).
- Dimitriadis, Gneiting & Jordan (2021), *Stable reliability diagrams* (CORP).
- Gibbs & Candès (2021), *Adaptive conformal inference under distribution shift* (ACI).
- Campbell & Thompson (2008), *Predicting excess stock returns out of sample* (out-of-sample R²).
