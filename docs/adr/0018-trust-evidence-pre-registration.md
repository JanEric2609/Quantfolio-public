# 0018 — Trust Evidence Pre-Registration: Calendar-Time Tests, a Shadow Ledger, Provenance

**Status:** Accepted (Phases 1–3 implemented; see the amendments of 2026-10-07, 2026-10-08,
2026-10-12 and 2026-10-14)
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
  *Superseded by the amendment of 2026-10-12: T0 = 2026-10-15.*

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

## Amendment of 2026-10-12 (release after the prod audit)

Observed when this was written (prod audit 2026-10-12): over 24 trading days against
EUNL.DE the "ideas" sleeve stood at about −4.6 % and the advisor at about −4.4 %; 46
resolved Discover predictions had a mean excess return of −1.28 % (t ≈ −1.5, not
significant). No trading day on or after the original T0 (2026-10-12) had resolved.

What changes, in one release (plan `docs/plan-audit-fixes-2026-10-12.md`, D12):

- the bars every return is computed from (one adjusted provider per series, ingest guard,
  quarantine) and the benchmarks they are judged against (total return, by asset class,
  both legs in EUR);
- the advisor optimiser (minimum variance with Ledoit-Wolf inside the owner's plan, 20 %
  turnover cap) and the mandates' sell gates;
- the Discover ETF lane (plan lines only, one fund per index, held indices skipped, no
  momentum rank for bonds, cash and gold), the composite's fundamentals inputs and the
  dossier's direction (deterministic). The shadow ledger's cohort spec moves to
  `composite_formula_version = "adr0017.2-2026-10-12"` and `universe_rule_version = 2`,
  so rows before and after are separate cohorts.

Because every one of these changes what the ledgers measure, the primary e-processes
restart:

- **New T0 = 2026-10-15** — the first trading day after the release was deployed
  (2026-10-14) and the five paper sleeves were archived and reset through the existing
  reset (the evening of 2026-10-14). `daily_tests.PRIMARY_TEST_START` carries the date.
- Ledger rows before the new T0 stay as they are (never edited or deleted) and are
  reported as the pre-release cohort; they do not enter the primary tests.
- Tests, metrics, thresholds and priors are otherwise unchanged.

## Amendment of 2026-10-14 (selection, futility, mandates)

Observed when this was written (audit of 2026-10-14, plan `docs/plan-audit-2026-10-14.md`):
- The F1 series had 2 trading days after the original T0, both before the new T0.
- No selection, futility or mandate value had been computed for any day.
- From pre-T0 data only, the audit measured the noise these tests are sized on:
  - picks against MSCI World: 4.64 % per 21 days;
  - picks against the same run's same-sector non-picks: 2.28 % per 21 days.
  The figures came from the 223 evaluable stocks of the 2026-10-14 run over 533 days in EUR.
- On the 2019-2025 replay the composite's monthly rank IC was 0.032 (t 1.61) with an IC
  standard deviation of 0.213.

None of the choices below could have been informed by an outcome they judge. The selection
series can be rebuilt from the new T0 later, because `discover_candidate_snapshot` is frozen at
issue. This amendment must therefore be merged before any code computes a selection, futility
or mandate value on data from T0 on, not before the first close.

**Why.** At a realistic edge (3-6 % a year), F1 needs decades. Two other questions can be
answered sooner:
- Comparing the picks with the stocks Discover could have picked removes the market, region,
  FX and most of the sector noise. That makes the test about 4× faster, and it tests what
  Discover controls: its selection.
- "Is the edge too small to be worth following?" is a futility question. It reaches a verdict
  well before "is it better?" does.

### 1. Families

| Family | Question | Series | Hypotheses | e-BH | One rejection needs | Role |
|---|---|---|---|---|---|---|
| **S Selection** | Do the picks beat the same run's sector-matched non-picks? | `selection` | skill, harm | K = 2, α = 5 % | e ≥ 40 | **primary**; sets the headline together with U |
| **U Futility** | Is the picks' selection edge below what pays for costs? | `selection` (shifted by δ) | futility | K = 1, α = 5 % | e ≥ 20 | **primary**: "worth following?" |
| **F1 Ideas vs ETF** | Do the picks beat MSCI World? | `ideas` | skill, harm | K = 2, α = 5 % | e ≥ 40 | **alarm only** (see §6 below) |
| **F2 Ranking** | unchanged | `ranking` | skill, harm | K = 2 | e ≥ 40 | primary, own row |
| **F3 Advisor** | unchanged | `advisor` | skill, harm | K = 2 | e ≥ 40 | secondary |
| **M Mandates** | Is each mandate worth running? | `mandate_a`, `mandate_b` | futility per mandate | K = 2, α = 5 % | e ≥ 40 | retire rule |

- Each family is its own e-BH set. No family's e-values are combined with another's, and the
  page never adds S, U and F1 up into one claim.
- §2's "Mandates still get no verdict" is replaced by family M. A mandate can now be judged
  not worth running. It still cannot be judged skilled, because a stated expectation has no
  naive benchmark for skill.

### 2. The `selection` series (S and U), frozen rule

- **Source:**
  - one Discover run's `discover_candidate_snapshot` rows with `instrument_group = 'stock'`
    and `evaluable = true`;
  - the run's issue date *d* is the UTC date of its snapshot.
- **Picks and controls:**
  - picks are the rows with `picked = true`;
  - a pick's controls are the same run's non-picks in the same `sector`;
  - shortlisted and sector-capped non-picks count as controls.
- **Fallback:**
  - when a pick's sector has fewer than 3 controls, its controls are all evaluable non-picks
    of the run;
  - the day counts these picks as `n_fallback`, and the page shows it.
- **Control return:**
  - the equal-weighted mean of the controls' EUR returns that day;
  - each control is carried like a member in §3. A control that stops trading earns 0 for
    the rest of the window and is never dropped.
- **The day's observation:**
  - r_s = (1/N) Σ (r_pick,s − r_control,s) over the N open picks;
  - the benchmark cancels, so r_s does not depend on the passive core.
- **Unchanged from F1 (§3):**
  - membership (*d* < *s* ≤ the 21st trading day after *d*) and entry at close(*d*);
  - the calendar of the passive core's EUR closes;
  - EUR restatement through `foundation.eur_prices` and carried prices (`n_stale`);
  - freezing: written once, a 5-trading-day grace, and days with no members kept with
    `n_open = 0`.
- **ETFs** are not in the series. Discover's ETF lane recommends plan lines and is judged
  under ADR 0019 and the plan's rule tests, not here.

### 3. The selection test (S)

The test is §4 with a smaller reference noise:
- **σ_ref:** 2.3 % per 21 days, the measured figure, so 0.50 % daily.
- **Clip:** C = 4.5 × σ_ref = **2.25 %** daily.
- **Map:** y_s = (clip(r_s, ±C) + C)/(2C), with p0 = 0.5.
- **Bettor:** aGRAPA with pseudo-variance **0.0121** and bet cap 0.75/p0.
- **Order of e-values:** skill bets on y_s and harm on 1 − y_s, then e-BH with K = 2.
- **Autocorrelation:** as amended on 2026-10-07. The lag-1 autocorrelation of the first 63
  days is measured once and shown. Nothing is whitened.

### 4. The futility test (U)

- **Null:** H0: E[x_s | F_{s−1}] ≥ δ_d. Rejecting it says "the selection edge is below δ".
- **Bet:** the bettor bets on δ_d − x_s.
  - u_s = clip(r_s − δ_d, ±C) and y_s = (u_s + C)/(2C);
  - the e-process bets on 1 − y_s with the same bettor, pseudo-variance and cap as S;
  - it is one hypothesis, so a rejection needs e ≥ 1/α = 20.
- **Margin δ:** **0.4 % per 21 days net of costs** (about 5 % a year). It is converted to the
  gross selection series with the ADR 0021 trade-check cost model:
  - the picks turn over once per 21-day window, a buy and a sell at a 10 bp half-spread each:
    0.2 %;
  - order fees are negligible at Scalable Prime;
  - tax is 0 while the NV certificate holds.
  - So **δ_gross = 0.6 % per 21 days**, and δ_d = 0.6 %/21 ≈ 0.0286 % per day.
  - The figure is fixed here. It is not re-derived when the tax position changes; a new
    figure would be a new amendment.
- **On the page, a rejection reads:** "The picks' selection edge is below what pays for costs:
  not worth following." No rejection after many years reads "not shown to be too small", never
  "worth following".

### 5. Mandate futility (M)

- **Series** `mandate_a` and `mandate_b`:
  - each mandate sleeve's daily active return, its NAV's daily return minus that of its own
    passive benchmark (the same start held in the passive core);
  - the NAV is in EUR, after fees and dividends, with cash accruing €STR;
  - frozen like §3.
- **Null:** H0: E[x_s | F_{s−1}] ≥ δ_m, with **δ_m = 0.2 % per 21 days net** (about 2.4 % a
  year). That is what active management has to add to justify the attention it takes.
  - The NAV is already net of costs, so there is no gross conversion.
  - The e-process bets on δ_m/21 − x_s, the same construction as U.
- **σ_ref** is measured on each series' first 63 observed days and then frozen.
  - The e-process starts on the 64th observed day, so σ_ref never uses data the test bets on.
  - C = 4.5 × σ_ref, the pseudo-variance is 0.0121 and the cap is 0.75/p0.
  - σ_ref is at least 0.1 % per 21 days. A sleeve that held the benchmark for its first 63
    days would otherwise get σ_ref = 0 and never be tested (added 2026-10-15, before any
    mandate had 63 observed days).
  - Only the sleeve's current run counts: a reset starts the series again from its new
    inception day. The ledger keeps the earlier days.
- **e-BH** runs over the two mandates, K = 2, with e ≥ 40 for one rejection.
- **Retire rule.** A mandate whose futility hypothesis is rejected is **retired**:
  - its reviews and trades stop;
  - its NAV is still marked;
  - the owner gets one Telegram message;
  - the page shows the verdict.
  - A retirement is never reversed by later data. Restarting a mandate is a new mandate under
    a new amendment.

### 6. F1 becomes an alarm, and the headline

- F1 is computed exactly as before.
- It shows in the headline only when it crosses: harm, or skill (an edge large enough to
  beat MSCI World on its own). Otherwise it sits under "alarms".
- The headline comes from S and U.
- The basket's tilt against MSCI World is **reported, not tested**: its tracking error and
  how much of it is sector, style and FX exposure. This is a risk budget for the owner, not
  a hypothesis.

### 7. Scoring changes after T0 keep the clock

- The nulls are conditional: every day's mean, given the past, is at most 0 (or at least δ).
  That holds whatever strategy produced the day, as long as the strategy is fixed before the
  day starts. So the e-processes stay valid when Discover's scoring changes.
- **From the new T0 on, a cohort change does not restart the primary e-processes.**
  - A cohort change is any bump of `COMPOSITE_FORMULA_VERSION` or `UNIVERSE_RULE_VERSION`,
    as with the fixes planned in WP-A6.
  - It is recorded in `provenance_json` as before.
  - Per-cohort rows stay descriptive (§10).
- A verdict therefore speaks for "Discover as it was run since T0", and the page says so.
- **Exception:** a change to the frozen rule above, to δ, δ_m or to any test parameter is not a
  cohort change. It needs a new amendment that states what had been observed.

### 8. The factor-neutral row (§8) is superseded

- The selection family does the factor study's job with about 2.5× the variance reduction
  (4.1× against 1.6× faster), from day one, and without a back-filled panel.
- As of 2026-10-14 the study had never run:
  - prod has no back-filled runs;
  - `IQQ0.DE` has no stored bars;
  - `trust_factor_study` is empty.
- The study and the `ideas_neutral` row are withdrawn. **No factor-neutral row will be
  built.**
- The `trust_factor_study` table stays.
- The code goes in its own change (private #344): the weekly job hook, the study module and
  the page row are removed there. Until that merges, the hook stays wired but cannot
  produce a row, because it needs back-filled runs that prod does not have.

### 9. A residual-slope family (gated)

- The sector/style diagnostic (plan WP-B1) runs once on the 2019-2025 replay and is recorded
  as one `trial_ledger` row with context `diagnostic`.
  - Each month it regresses the composite on sector dummies and style z-scores, then takes the
    residual's rank IC.
- **Gate:**
  - if the monthly residual IC standard deviation is **≤ 0.12**, an "R Residual slope" family
    may be pre-registered, in a further amendment before its live data is computed;
  - otherwise there is no R family.
- The residual mean IC is reported as descriptive only. The gate uses the standard deviation
  alone.

### 10. Time to know

- The single "years" figure becomes a grid per family: the median years to a verdict at edges
  of 0.25, 0.5 and 1 % per 21 days.
- σ is the reference above until 63 days are observed, then the measured one.
- Paths longer than 40 years show as "> 40 years".
- Amendment 2026-10-07 item 4's simulator is unchanged apart from these inputs.

### 11. The noise check (S, U and M)

**Why.** The calibration of 2026-10-14 (`docs/research/calibration/calibration-2026-10-14.md`)
found the tests hold under fat tails, volatility clustering and regimes, but not under
serial correlation:
- at a daily autocorrelation of 0.15, false alarms run about 2-2.5× the nominal rate;
- when each pick's drift persists through its 21 days (the `overlap_21d` stress model),
  56-62 % (52-58 % before the harness fix of 2026-10-15; the noise check's own rates did not
  change).

The check below catches the second case. It cannot catch the first within a year, and the page
says so.

**The statistic.** VR(21) is the variance ratio of the series' daily values:
- it is Lo and MacKinlay's overlapping 21-day estimator with the unbiased denominator;
- `foundation.forecast_verification.variance_ratio` computes it;
- the series' mean is subtracted, so the check uses the series' noise, never the sign or size of
  its mean.

**When and the cutoffs.** Each series (`selection`, `mandate_a`, `mandate_b`) is checked on its
observed days from T0, at three looks:

| Look (observed days) | Fails when VR(21) > |
|---|---|
| 63 | 3.0 |
| 126 | 2.3 |
| 252 | 1.8 |

- Each look uses the series' first *n* observed days, so a look's value never changes once
  it is reached.
- The cutoffs come from 20,000 simulated paths per model of the 2026-10-14 harness. The
  chance that a series fails at any of the three looks is:
  - 4.9 % on iid normal noise, 6.1 % on t(4)-GARCH and 8.3 % on regime noise;
  - 20 % at AR(1) 0.15;
  - 99 % under `overlap_21d`, and 53 % of those paths already fail at day 63.
- **The 1.3 cutoff the plan proposed was dropped.** With 63 days, one VR(21) estimate has a
  standard error of about 0.6, so 1.3 fails a quarter of clean series.

**What a failure does.**
- A failed check is final for that series.
- The page marks the series' verdicts **"not valid: serial correlation"**, and no S, U or M
  verdict on it is acted on.
- The series' test moves to non-overlapping observations, each one counted as a single
  observation:
  - for S and U: one Discover run every 21 trading days, scored on its picks' whole 21-day
    pick-minus-control return;
  - for M: consecutive 21-day sums of the mandate's daily active return.
- The new test uses the same map, bettor and thresholds, with σ_ref × √21 as its reference
  noise. It starts from the failing day, and the earlier e-values are discarded.
- A further amendment fixes the code that builds it before its first value is read.

**Before the last look.**
- An S or U verdict reached before the series passes its day-252 look is shown as
  **provisional**.
- A mandate is retired only once its series has passed all three looks.
  - M's e-process starts on day 64, and its median time to a verdict at a 2 % edge is about
    2 years, so this costs nothing in practice.

## Amendment of 2026-10-26 (a silent symbol stops holding days back)

Observed (prod check of 2026-10-26): WBD, a Discover pick of 2026-10-14, was delisted. It stays
a member for its 21-day window, and under §3 every one of those days waited the full 5-day grace
before freezing. `ideas`, `ranking` and `selection` therefore ran 5 trading days behind the
calendar until the window closed.

**Change.** A day still waits for a member's close, for at most 5 trading days. A symbol whose
last close is already 5 or more trading days behind the end of the calendar no longer makes a
later day wait: that day is frozen at once with the carried price, counted in `n_stale`.

**What does not change.** The value written for such a day is the one the old rule wrote 5 days
later: a pick that stops trading is cash (active return −r_bench) for the rest of its window, as
§3 says. Only a data outage longer than 5 trading days that is later filled in could differ, and
under the old rule the first days of that outage were already frozen as stale. No test statistic,
look or threshold depends on when a day is written.

## Amendment of 2026-11-08 (lower-noise series, and the BUY gate reads them)

**Observed when this was written** (plan `docs/plan-faster-evidence-2026-11-08.md`):
- Only standard deviations of the live series were read, never a mean, an e-value or a state:
  - over 16 observed days from T0, `selection` 3.76 % per 21 days, `ranking` 3.11 % and
    `ideas` 3.59 %.
- The Discover BUY gate (`discover/candidate_gate.py`) still ran the ADR 0017 block-mean test on
  the picks against MSCI World:
  - with its prior precision c = 2, nothing could be proven before nine 30-day blocks;
  - at a 1 % monthly edge it was proven within three years about half the time.
- No value of `selection_w` or `ranking_ef` had been computed, by any code, for any day.

**Why.** For an anytime-valid test the evidence grows by at most ½ log(1 + μ²/σ²) per observation
(Wang & Ramdas 2024, Prop. 6.5), so at a fixed edge the time to a verdict scales with σ².
Observing more often within the same span adds nothing about the mean (Merton 1980). The only
ways to know sooner without lowering the bar are therefore:
- less noise per observation;
- the test the owner acts on asking the least noisy question.

### 1. The noise study (outcome-blind)

`lab/calibration/noise_study.py` with `scripts/run_noise_study.py`; results in
`docs/research/calibration/noise-study-2026-11-08.json`. It ran on the stored EUR closes of
2026-11-08's 237 evaluable stocks over 761 trading days, in two parts:
- **Random picks and scores.** 98 issues, 10 seeds. They have no edge, so the measured standard
  deviation is the design's noise and nothing else.
- **The nine live runs as issued.** Real composites and picks, so the portfolios carry the real
  signal's factor tilts. Weights were fixed on the year two years before each issue and measured
  on the year just before it. No return after any issue day entered.

Real runs, standard deviation per 21 days of one run's portfolio:

| Design | sd | Times faster at a fixed edge |
|---|---|---|
| `selection` (frozen: equal-weighted same-sector non-picks) | 3.90 % | 1 |
| + hedge of the beta difference | 3.85 % | 1.02 |
| + precision weights (1/var of each pick's trailing spread) | 2.73 % | 2.04 |
| 5 nearest same-sector controls + precision weights | **2.50 %** | **2.44** |
| `ranking` (frozen centred-rank weights) | 3.65 % | 1 |
| Ledoit-Wolf-Zhao efficient weights, same rank exposure | **2.44 %** | **2.23** |
| the same on a random 100 of the 237 stocks: rank / efficient | 4.05 % / 2.82 % | 0.81 / 1.67 |

- Beta hedging buys almost nothing once controls share the sector, so it is not adopted. It
  would also change the hypothesis to an alpha.
- Efficient weights gain more from breadth, as Ledoit, Wolf & Zhao (2019) report: going from 100
  to 237 stocks makes them 1.34 times faster against 1.23 for rank weights.
- These figures include the duplicate and weight-cap rules of §2 and §3. Before those rules
  were added (same day, same data) they were 2.48 % / ×2.47 and 2.43 % / ×2.21.

### 2. The `selection_w` series (frozen rule)

- **Source:** as `selection` (amendment of 2026-10-14, §2): one run's evaluable stocks, picks
  `picked = true`, entry at close(*d*), 21 trading days, EUR, carried prices, frozen once.
- **Window:** for each run, the 252 trading days of the passive core's calendar ending on the
  last trading day before the issue day *d*.
  - A stock is in the window when it has a close on or before the window's first day and one in
    its last 5 days.
  - Nothing from day *d* or later enters.
- **Features:** each in-window stock's OLS beta to the passive core over the window, the
  standard deviation of its last 63 daily returns, and 12-1 momentum. They are z-scored over the
  run's in-window stocks (`foundation/evidence_weights.py:stock_features`).
- **Controls:** the 5 nearest same-sector non-picks by Euclidean distance in feature space.
  - With fewer than 3 in-window same-sector non-picks, the 5 nearest of all in-window non-picks
    are used, counted as `n_fallback`.
  - A pick without a full window keeps the 2026-10-14 rule's controls.
- **Weight:** 1 / variance of the pick-minus-controls daily spread over the window's last 126
  days. A pick without one takes the median weight of its run's other picks. Every weight is
  capped at 4 times its run's median, so one very quiet pick cannot carry a day.
- **Second listings:** a non-pick whose window returns correlate above 0.95 with another of the
  run's stocks (a second share class, such as GOOG and GOOGL) is never a control: its spread
  against its twin compares nothing.
- **The day's observation:** r_s = Σ_i w_i (r_pick,i,s − mean_controls r_c,s) / Σ_i w_i over
  the open picks. Every weight is fixed before the issue day, so under H0 (each pick's
  conditional mean spread ≤ 0) the weighted mean's conditional mean is ≤ 0 and every
  e-process stays valid.

### 3. The `ranking_ef` series (frozen rule)

- **Cohort:** each live run with at least 30 evaluable stocks in the window of §2.
- **Second listings:** of two stocks whose window returns correlate above 0.95, the later in
  symbol order is dropped (Ledoit, Wolf & Zhao drop such pairs); a cohort that falls below 30
  is skipped.
- **Signal:** m = their centred-rank weights (§5 of this ADR) over that set.
- **Covariance:** C = the Ledoit-Wolf (2004) shrinkage of the window's daily returns.
- **Weights:** w = argmin w′Cw subject to 1′w = 0 and m′w = m′m (Ledoit, Wolf & Zhao 2019,
  "Efficient sorting"), held for 21 trading days with the ledger's calendar and carry rules.
- The day's value is the mean over open cohorts, as for `ranking`.

### 4. Tests

| Family | Series | Start | σ_ref per 21 days | Clip | Bar | Role |
|---|---|---|---|---|---|---|
| **S** skill/harm, **U** futility | `selection_w` | **2026-11-09** | 2.0 % | 2 % a day | e-BH K = 2 at 5 % (e ≥ 40); U e ≥ 20, δ unchanged | **primary**, headline |
| **F2** skill/harm | `ranking_ef` | **2026-11-09** | 2.1 % | 2 % a day | e-BH K = 2 (e ≥ 40) | **primary** |
| S, U (2026-10-14 design) | `selection` | 2026-10-15 | unchanged | unchanged | unchanged | secondary, shown, no headline |
| F2 (2026-10-14 design) | `ranking` | 2026-10-15 | unchanged | unchanged | unchanged | secondary |

- **σ_ref** comes from the study, not from any live value of these series:
  - `selection_w`'s 2.0 % lies between 1.5 % (the frozen 2.3 % times the study's ratio
    2.50/3.90) and 2.5 % (one run alone);
  - `ranking_ef`'s 2.1 % is the study's 2.44 % times 0.86, the live `ranking` series' ratio of
    overlapping to single-run noise.
- **Same machinery:** map, bettor, pseudo-variance 0.0121, cap 0.75/p0, the §11 noise check and
  "provisional" before day 252.
- **Start:** 2026-11-09 is the first trading day after this amendment was merged, so no day a
  verdict bets on was computed by anyone before the rule was fixed. Earlier days of the new
  series are written and shown "before pre-registration", as in §2.
- **Multiplicity:** the old and new designs never form one claim. The new ones are the primary
  tests; the old ones are records.

**Calibration** (`docs/research/calibration/selection-w-calibration-2026-11-08.json`, harness of
2026-10-14, 20 years):

| False alarm, S2 at e ≥ 100 | iid | t(4)-GARCH | regime | AR(1) 0.15 | overlap_21d |
|---|---|---|---|---|---|
| Share of null paths that ever cross | 0.8 % | 0.9 % | 0.6 % | 2.8 % | 51 % |

Median years to e ≥ 100:

| Edge per 21 days | frozen `selection`, σ 2.3 % / 3.1 % | `selection_w`, σ × 0.636 |
|---|---|---|
| 0.5 % | 19.9 / 38.9 | 7.0 / 14.5 |
| 1 % | 4.5 / 8.9 | **1.7 / 3.0** |
| 2 % | 1.1 / 1.9 | **0.7 / 0.8** |

### 5. The Discover BUY gate reads `selection_w` (stocks)

- **Rule.** For instrument type `equity`, `candidate_gate.evaluate_track_record` is `proven` when
  all of these hold:
  - `selection_w`'s skill e-value has reached **100** (the owner's 1 %, unchanged);
  - the series has at least **126** observed days, the noise check's second look;
  - none of these guards has fired: the S harm rejection, the U futility rejection, a failed
    noise check, or F1's harm rejection against MSCI World;
  - and, as before, the resolved picks' hit rate is above 50 %.
- **Why 126 days.** In 2,000 simulated 20-year null paths, a false BUY reached e ≥ 100 before
  any noise-check look failed in:
  - 8.1 % of overlap_21d paths when BUY could come from day 63, and 3.7 % from day 126;
  - 1.9 % / 1.8 % of AR(1) 0.15 paths;
  - 0.3 % of iid paths either way.
- **Funds, bonds and cash** keep the block-mean gate: they are not in the selection series.
- **Delivery.** The weekly ledger job stores the evidence per user in `app_settings`
  (`trust_buy_gate_json`) and Discover reads that copy. The decision-loop independence
  contract forbids `discover` from importing `verification`.
- **The record.** The old block-mean result stays in every stock's gate as `legacy`.
- **What a BUY now says:** the picks beat the most similar stocks Discover could have picked,
  with the stated false-BUY rate, and nothing shows them doing worse than MSCI World. It no
  longer says "proven to beat MSCI World". The basket's sector, region and style tilt is a risk
  the owner takes, reported on the Trust page as tracking error.

**What does not change:** every earlier series, test, threshold, δ and prior; the T0 of the
families in the table above.

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
