# 0017 — Discover Scoring Integrity: Data Honesty, De-duplicated Momentum, Market-Implied Expected Returns

**Status:** Accepted
**Date:** 2026-09-26
**Context source:** memory `project_quantfolio_2026-09-26_bbva_dossier_audit.md`
(prod audit of the BBVA.MC dossier from run `2930a59b`, 2026-09-25)
**Supersedes:** the strong-conviction exemption in [0009](0009-discover-recency-penalty.md)
**Consumed by:** `backend/app/decision/discover/{pipeline,composite,config,dossier_writer,orchestrator}.py`,
`backend/app/foundation/expected_return.py`, `backend/app/foundation/providers/{yfinance_provider,registry}.py`,
`backend/app/decision/discover/resolution.py`, `backend/app/foundation/market.py`,
`backend/app/foundation/providers/finnhub_provider.py`,
`backend/alembic/versions/{0112_adr17_signal_weights,0113_refetch_fundamentals}.py`,
`frontend/src/components/discover/DossierDrawer.tsx`

## Context

The BBVA.MC dossier showed signal score 0.72, a +6.0% 12-month estimate
"anchored on the trailing 3-year annualized return", and a thesis that
called the same 59.8% figure "12-1 month momentum". Every headline number
reproduced exactly from prod prices. The defects were in which data reached
the score and how the score was built:

1. **Analyst data silently dropped.** The provider registry skips a
   throttled provider without waiting. OpenBB's self-imposed 30/min
   guardrail throttled 127 of 271 candidates. Each kept a default
   `analyst_estimate_score` of 0.5, weighted into the composite as if real,
   with no concern flag. BBVA.MC's real consensus was a median target 5.5%
   below the price (22 analysts, "hold").
2. **Prompt never named the anchor.** The LLM saw
   `expected_return_anchor_pct = 59.8` with no kind. It guessed "12-1
   momentum" because that figure happened to be 60.9%.
3. **Trailing returns counted repeatedly.** Momentum (0.20) and 3y
   benchmark excess return (0.10) carried ~35% of effective weight. The
   momentum score `0.5 + m` saturated for any name up 50% or more. The
   expected-return anchor is a trailing return too. The dossier's signal
   breakdown hid momentum, benchmark and risk, the inputs that actually
   produced the 0.72.
4. **One sector bet.** 9 of 15 shortlisted names were European banks and
   5 were energy/utilities. Individual-stock momentum is largely industry
   momentum (Moskowitz & Grinblatt 1999).
5. **Recency penalty inert.** Its exemption (`composite >= 0.65`) covered
   every shortlisted name (all ≥ 0.68). The exempt path also skipped the
   lookup, so "days since last recommended" showed "—" for a name
   shortlisted the day before.
6. **Estimates below cash.** Shrinking trailing returns toward 0% put 6 of
   15 LONG picks below the 2.50% ECB deposit rate.
7. **Two conflicting betas.** The daily beta vs a US-listed ETF (0.85) is
   biased low by non-synchronous closes (weekly: 1.10). The thesis cited a
   collinear multi-factor loading (1.33).

## Decision

### A. Data honesty

- **yfinance serves analyst consensus.** `YFinanceProvider.get_analyst_estimates`
  maps Yahoo's `.info` targets. `ProviderRegistry.get_analyst_estimates`
  routes yfinance → OpenBB → Finnhub. OpenBB serves the same Yahoo data
  behind the 30/min guardrail, and Finnhub's free tier returns no price
  target. A prod probe covered all 13 sampled European equities in ~4s.
- **Missing is missing.** `analyst_estimate_score` stays `None` when no
  provider answers, and single-name equities get an `analyst_unavailable`
  concern. The composite and dossier leave the signal out instead of
  weighting in 0.5. Funds skip the lookup. The median target is preferred
  over the mean (HFG.DE: mean 4.82 vs median 3.50); `target_high` is never
  used. Stored pre-ADR breakdowns (0.5 with no `analyst_source`) replay under
  their old gate.
- **Prompt grounding.** Before the LLM call, `write_dossier` resolves the
  annual forward estimate and adds plain-language lines to the context: the
  anchor's kind (with 12-1 momentum stated separately when it differs), the
  system estimate and its components, every composite input with its applied
  weight, the canonical weekly beta (with an instruction not to cite factor
  loadings as market beta), and analyst consensus or its absence.
- **Full composite breakdown.** The pipeline stores
  `scores["composite_inputs"]` (`composite_contributions`: score, applied
  weight, contribution, largest first) and `scores["composite_raw"]`. The
  dossier carries `composite_breakdown` and `expected_return_components`,
  which the API exposes as optional fields and the drawer renders as "What
  drives the score".

### B. Score design

- **Weights** (code default, and migration 0112 for the never-customised prod
  row): momentum 0.20 → 0.15, benchmark 0.10 → 0.05, fundamentals
  0.05 → 0.15. Risk stays at 0.10: raising it lifted the XEON.DE
  money-market regression composite from 0.63 to 0.70, reopening the cash
  attractor, because near-zero volatility scores ~1.0 there. Fundamentals
  cannot do that, since cash funds have none. Its 0-1 score moves in a
  narrow band, so the practical effect is less trailing-return dominance,
  not a heuristic taking over.
- **Momentum is a cross-sectional rank.** After all candidates are scored,
  `assign_momentum_ranks` stamps a mid-rank percentile of 12-1m momentum
  into `momentum_quality.momentum_rank` (needs ≥ 20 rankable candidates),
  so `derive_signals_from_scores` replays reproduce it. The fallback is
  `0.5 + 0.5·tanh(m/0.5)`: slope 1 at zero like before, but no ceiling.
  Composites are therefore computed after the per-candidate loop.
- **Benchmark is risk-adjusted.** `stage_backtest_vs_benchmark` adds tracking
  error, information ratio and `active_t_stat = IR·√years`. The signal is
  `0.5 + 0.5·tanh(t/2)` instead of `0.5 + excess`, which hit 0.97 for BBVA
  from raw excess return alone.
- **Sector cap.** At most `max(2, ⌊0.2·N⌋)` names per sector (3 of 15) reach
  the LLM shortlist (`apply_sector_cap`). Names without a sector (funds) are
  exempt. Skipped names are labelled `reject_stage="sector_cap"`, and the
  orchestrator's tradeability backfill respects the same cap.
- **Recency exemption is relative.** A recently recommended equity escapes
  the penalty only when its pre-penalty composite beats its own
  pre-penalty composite at the last recommendation by ≥ 0.05. The last
  recommendation is always looked up, so `days_since_last` and
  `last_composite` are always populated. Rows written before `composite_raw`
  existed never qualify, because comparing against a post-penalty
  conviction would make a penalised name look improved.

### C. Expected return: market-implied prior plus a credibility tilt

Annual forward estimate (trailing mode, the prod default):

```
prior  = rf + β_adj · ERP          (money market: rf; bond: rf + max(0, β)·ERP)
weight = min(cap, τ² / (τ² + σ²/T))  (money market: cap)
ER     = prior + weight · (anchor − prior)
```

- `rf`: the €STR cache (`get_risk_free_rate`, 2.44% on 2026-09-25). It is
  also correct for USD listings from a EUR investor's view under uncovered
  interest parity.
- `β_adj`: Blume (1971) `2/3·β + 1/3` on the new **weekly** beta
  (`market_beta_weekly`: 2 years of Friday returns vs the regional
  benchmark, restated into the listing currency). Falls back to the daily
  beta, then to 1.0.
- `ERP = 4.5%` over cash. Sources: UBS Global Investment Returns Yearbook
  2026, ~3.5% geometric and ~5% arithmetic over bills; Damodaran's implied
  premium, 4.2–4.5% over T-bonds in 2026; Kroll's eurozone recommendation,
  5.0–5.5%. It pairs with the blocks estimator's 7% equity grand mean.
  Refresh annually.
- `weight`: Bühlmann credibility of a `T`-year mean return with volatility
  `σ`, against a cross-sectional dispersion of true expected returns
  `τ = 3%`. That is about twice the CAPM-implied ERP·sd(β). Trailing
  returns carry little to no forward information at 12 months (Goyal & Welch
  2008; De Bondt & Thaler 1985), so a stricter weight than the flat 0.10 is
  warranted. `cap` is the existing per-class constant (equity 0.10, money
  market 0.90), or the fitted MZ slope once it activates.
- Horizon conversion stays linear, and a momentum-anchor tilt is still
  capped at one month.

BBVA.MC on prod data (weekly β 1.047 → 1.031 adjusted, 3y volatility
28.8%): prior 2.44 + 1.031·4.5 = 7.1%, weight 3.2%, estimate 8.8% a year.
The old formula gave 6.0%, and GLEN.L's old 0.9% becomes 7.0%.

**Why not `er_mode = "blocks"`.** The building-block estimator needs a
dividend yield plus a single-period earnings growth figure. That figure is
noisy (it has to be clamped), and the CAPE term has no data feed yet. It
also ignores beta. It stays available behind the setting.

### Follow-ups found by replaying run 2930a59b

A read-only replay of the 2026-09-25 run on the prod worker re-ran the
changed stages for all 267 evaluable candidates. It found three problems
that the rest of this ADR would otherwise have shipped:

- **Analyst upside is ranked within the sector.** Once coverage was real
  (238 of 239 equities, up from roughly half), `0.5 + 2·upside` pinned many
  names at 1.0, because consensus targets sit above prices on average
  (Bradshaw, Brown & Huang 2013). `assign_analyst_ranks` stamps the
  percentile of upside within the sector (pool-wide below 5 names), since
  target prices are informative mainly as within-industry relative valuations
  (Da & Schaumburg 2011). BBVA's −5.5% ranks 0.09 among financials.
- **Cash funds get a neutral risk score**, for the same reason momentum
  already was neutral. Without that, XEON.DE's structural ~1.0 lifted it into
  the replayed top 15 once equity composites stopped saturating. It now
  ranks 97th.
- **Sector falls back to the analyst payload**, because US listings resolve
  fundamentals via Finnhub, which carries no sector, so the cap skipped them.

Replayed shortlist: 3 financials (6 more capped), 2 each from consumer
defensive, energy, utilities and funds, and 1 each from healthcare, basic
materials, consumer cyclical and industrials. Before, it held 9 banks and 5
energy/utility names. Every shortlisted estimate is at least 4.3%, against a
2.44% cash rate. BBVA's pre-penalty composite is 0.638, and the recency
penalty (recommended 0.4 days earlier) removes it.

### Data defects found on the same branch

A prod sweep (logs, caches, provider payloads) before opening the PR turned
up defects that fed wrong inputs into the scores above:

- **Finnhub fundamentals were in different units.** Finnhub answers first
  for US listings and reports market cap in millions, ratios in percent and
  volumes in millions of shares, but every consumer reads yfinance's units.
  AAPL's cached "market cap" of 4.9M failed the universe's 250M floor, so
  AAPL, MSFT and NVDA were missing from the universe. The 2026-09-25 run held
  31 US names, an alphabetical slice of the S&P 500, down from 164 on
  2026-09-15. The fundamentals score also read ROE 137 (percent) as
  13,700%, pinning the ROE and growth terms for every US name. Now that
  fundamentals weigh 0.15, that would have favoured US names across the
  board. `FinnhubProvider.get_fundamentals` now returns yfinance units and
  adds volume, average volume, gross margin and dividend yield. Migration
  0113 deletes the cached Finnhub and `universe_screen` rows and backdates
  the rest, so nothing serves the old units for the 7-day cache window.
- **yfinance dividend yields under 0.5% were stored 100× too high.** Since
  yfinance 0.2.54 `dividendYield` is a percentage (AAPL 0.32 means 0.32%).
  The old rule divided only values above 0.5. The field feeds the
  building-block estimator.
- **Stored price series froze.** `market.history` serves `bar_prices`
  without a live fetch whenever any rows exist (audit M2). Nothing
  re-ingests FX pairs, so `USDEUR=X` stopped at 2026-09-01, and the
  benchmark and beta legs forward-filled one rate for three weeks (0.863
  against 0.878 live). Discover's FX cache also lived as long as the worker
  process. The new `market.refresh_stale_bars` catches a series up from its
  last bar and retries at most once per 6 hours per symbol. Discover's FX
  loader calls it, and its cache is now keyed by day. Outcome resolution
  calls it for every series it reads. Without that, a pick that left the
  universe would have kept its old bars, and once past the grace window the
  first resolutions (due 2026-10-06) would have been marked `delisted`,
  with USD returns left unconverted.

The replay figures above predate these fixes. The universe was missing
most US large caps, and US fundamentals scores were inflated.

## Consequences

- Composite values shift. The track-record gate starts a fresh cohort under
  the new config id, and a recency comparison against a pre-ADR prediction
  never grants the improvement exemption. Both effects fade within one
  30-day half-life.
- The first run after deploy re-fetches fundamentals for the whole
  universe (about 800 symbols, 8 threads), so universe building is slower
  once. After that the universe holds US large caps again, and the
  US/EU mix shifts.
- The AlphaCrafter IC was still a cross-section of the candidate plus three
  index ETFs, so it carried little information. The 2026-09-28 follow-up
  below drops it.

### Follow-up: Discover run audit (2026-09-28, run 71d23ec4)

The run completed, but every composite landed between 0.600 and 0.634. The
changes below each rest on a defect measured on prod.

**Composite.**
- The `regime` signal was the mean of the screener's regime-affinity
  multipliers over five categories. That comes out at 1.02 in bull, 1.04 in
  sideways and 1.02 in bear, clipped to 1.0: the same value for every
  candidate. It is removed, and its 0.05 weight is dropped rather than
  reassigned, so `WEIGHTS` now sums to 0.95. Renormalisation keeps every
  other signal's relative weight.
- `ic_icir` now requires `panel_size >= 10` as well as `n_obs >= 20`
  (`composite.ic_signal_usable`, shared with config review and the
  dossier), so it is off until the IC panel is widened.

**Data honesty.**
- Stale data now drops out of the composite instead of scoring as neutral:
  - Insider windows past the end of the SEC extract (which ends
    2024-03-29) become NaN. They used to score as 133 × "no insider
    trading".
  - IBES revisions are no longer computed across a fiscal-year roll (the
    LRCX "+65%" revision was one) and carry a 92-day staleness limit.
  - `_safe_float` now returns None for NaN; it used to return 0.0.
- Finnhub P/E and P/B come from TTM and current fields. MU had read
  P/E 143 against a TTM figure of 24.

**Measurement.**
- Portfolio fit correlates weekly returns over two years. Daily
  correlation between US shares and Xetra-listed holdings is biased toward
  zero by asynchronous closes (Burns, Engle & Mezrich 1998).
- The ML signal is served the full 252-bar feature windows it was trained
  on, instead of 90 days.
- US shares are regressed on US factors (ADR 0007 amendment).

**Universe.**
- The index pool is ranked by EUR market cap before `index_cap` is
  applied. It used to be truncated in list order, so no FTSE, Nikkei or
  S&P 500-only name was ever reached.
- The size floors are applied in EUR. They used to compare yen and pounds
  against a euro threshold.
- The Euro Stoxx 50 sample is the real 50, and 53 dead symbols were
  removed.

**Not changed, recorded here.**
- No run can reach a BUY verdict (the maximum composite was 0.634, below
  0.65) until the shortlist's evidence improves.
- All 43 per-ticker ML models are rejected against their baseline. The
  gate is fair: the dummy classifier scores about the same out of sample.
  The models simply show no skill.
- The HMM regime relabels identical features after its weekly refit
  (bull → sideways).

### Follow-up 2: the findings left open above (2026-09-28)

**`ic_icir` is now factor exposure.** The old stage measured each factor's
IC across the candidate and three benchmark ETFs. That is a property of the
factor, not of the candidate, and a rank correlation over four names is
noise. The signal now follows Grinold & Kahn (alpha = IC × volatility ×
score):

- For every validated `FactorsLibrary` factor, the candidate gets a
  cross-sectional z-score within the 150-name miner universe (Euro Stoxx 50
  plus S&P 100). The z-score is clipped at ±3.
- The z-scores are combined into `Φ(Σ w·sign(IC)·z / √Σw²)`, with
  `w ∝ |ICIR|`.
- `ic_signal_usable` needs an `exposure_score`, at least one factor and a
  cross-section of at least 30 names.
- The dossier stores `factor_exposure` and the per-factor z-scores.
- The key stays `ic_icir`, so stored configs keep working.
- Scoring a factor that has already been validated is not a new trial, so
  the stage no longer writes `ac_trial_ledger` rows.

**The miner mines a real cross-section.**
- `alphacrafter.universe.MINER_UNIVERSE` has 150 names. It replaces the
  four-ETF basket, which rejected every factor at every run. Any configured
  basket under 30 names is replaced by it.
- `data_ingestion` now downloads missing bars through
  `DataIngester.ingest_bar_prices`. Its only provider step used to raise
  `NotImplementedError`.
- On that universe over 2021–2026, `momentum_12_1` reaches IC 0.037 but
  ICIR 0.15, below the 0.3 gate. An empty library is therefore an honest
  outcome, and the signal stays out of the composite until the miner
  validates a factor.

**`ml_signal` comes from one pooled model (ADR 0015 ruling #24).**
- The per-ticker triple-barrier classifiers are gone. All 43 were
  rejected, and a replay gave Cohen's kappa ≤ 0 for every ticker.
- `lab.quant_lab.pooled_ml` ranks the miner universe on the 20-day forward
  vol-scaled return, using 10 price features.
- The weekly job refits it under purged walk-forward CV.
- It is served only when its out-of-sample rank IC is ≥ 0.02 with
  Newey-West t ≥ 3.
- The candidate's `prediction` is its percentile within today's
  cross-section.
- On the first real run the served LightGBM scored IC 0.014 (t 1.17),
  below momentum 12-1 alone (IC 0.046). It is rejected, and `ml_signal`
  stays out of the composite, as it already was in practice.

**The rest of the audit's open findings are fixed on the same branch:**
- The regime model is now the statistical jump model (ADR 0004
  amendment).
- Each macro series is carried forward on its own, and a stale feature
  row is refused.
- An earnings report within 14 days is named in the dossier as a concern.
  It is not scored.
- Returns are restated in USD before the Ken French regressions, and
  Tokyo listings use the Japan factors (ADR 0007 amendment 2).

### Follow-up 3: the verdict (2026-09-28)

The Recommendation verdict was BUY when the composite reached 0.65, and
HOLD otherwise. The composite is a ranking score whose level moves with
every weight change, so the share of BUYs followed the code:

- 100% of the shortlist was BUY on 2026-09-07, 15, 24 and 25.
- 0% was BUY after this ADR on 09-26 and 09-28.

The label also carried no skill. The 503 picks from 2026-06 to 2026-08
with 21 trading days of prices since were checked against EUNL.DE, with
USD, GBP, CHF and JPY listings converted to EUR:

| Verdict | Picks | Mean 21-day excess return | Hit rate |
|---|---|---|---|
| BUY | 312 | +0.03% | 47% |
| HOLD | 191 | +0.67% | 56% |

Pooled, the score's rank correlation with the excess return was −0.07
(p = 0.11).

"HOLD" was also the wrong word for a name the user does not own.
`candidate_gate.discover_verdict` now returns:

- BUY only when the candidate's `(config_id, instrument_type)` cohort is
  `proven`: resolved picks whose excess return clears the deflated
  Newey-West bar, with a hit rate above 50%.
- WATCH otherwise (`insufficient_data` or `unproven`).

No cohort is proven yet: none of the 105 ledger rows since 09-07 has
resolved. The composite keeps ranking the shortlist.
`STRONG_CONVICTION_THRESHOLD` is gone.

**No calibration is not a calibration.** Without 20 resolved rows from the
same source, the Platt calibrator stored the raw score as
`conviction_calibrated`. Every one of the 204 prod predictions therefore
carried "calibrated" = raw. The advisor UI showed "0.65 → 0.65" where it
has an "uncalibrated" state, and scoring would have labelled raw-score
Brier values `brier_basis: "calibrated"`. The calibrator now leaves the
value NULL, and migration 0117 clears the stored copies.

### Follow-up 4: what "proven" means (2026-09-29)

Follow-up 3 made the track record the only way to a BUY, and a check of that
track record showed it would have produced false BUYs.

The old gate ran a 5-lag Newey-West t over every resolved pick of a
`(config_id, instrument_type)` cohort, in no particular order. Discover picks
are not independent observations:

- A run shortlists about 10 names, and runs come every ~2 trading days.
- Each pick's 21-day excess return shares most of its window with the picks
  of the neighbouring runs.
- The 443 picks of 2026-06..08 fell on only 17 dates. Their excess moved
  together: about +1% for every date from Aug 7 to 12, and −1.5% to −3% for
  every date from Aug 20 to 25.

The gate was also re-run after every run, which a fixed-sample test does not
allow. In a simulation calibrated to these numbers (per-pick SD 6.9%,
per-date mean SD ~2%), a strategy with **no** edge was "proven" at some
evaluation within its first year in **36–63%** of runs. The t > 3 bar
implies about 0.1%.

The cohort was also the wrong unit. The config id is a hash of the code's
default weights, so it changed on 09-26 and changes again with every weight
edit. No version would live long enough to be judged.

**Now** (owner decisions 2026-09-29):

| | |
|---|---|
| Observation | The mean excess return of all picks whose prediction date falls in one 30-day block (one 21-trading-day horizon). A block counts once it is over and all its picks have resolved; blocks are used in order. A pick still pending 14 days after its resolve date is left out. |
| Test | The semi-one-sided sequential t-test e-process of Wang & Ramdas (arXiv 2310.03722, Thm 4.11), with mixture precision c = 2. It is anytime-valid: under "no edge", the chance that it **ever** reaches 1/α is at most α, however often it is checked. |
| Bar | α = 1%, so an e-value of 100. The pick-level hit rate must also exceed 50%. At least 3 blocks before a cohort is judged. |
| Cohort | Instrument type, across all signal-weight versions. |

In the same simulation this held the no-skill false-BUY rate at **1.0%**
over three years. A real excess return of 1% a month (~13% a year) is
proven with probability about 3% within one year, 24% within two and 54%
within three.

c = 1 had more early power, but let the small overlap between adjacent
blocks' return windows push the false rate to 1.8%. Keeping only picks from
the first half of each block removed that overlap but cost about a third of
the power. At c = 2 the e-value after n blocks cannot exceed
2·√(c²/(n+c²))·((n+c²)/c²)^(n/2): about 93 after 8 blocks and 224 after 9.
So nothing is proven in less than nine months.

The expected-maximum deflation by the global trial count (finding F15) is
dropped for this gate. It corrected for picking the best of many configs,
and the cohort is no longer a config.

### Follow-up 5: the two signals with dead data (2026-09-29)

Two composite inputs, `insider_signal` and `estimate_revision` (5% each),
read point-in-time extracts that no job refreshed.

- **Insider.** The extract ended on 2024-03-29, so the signal was unknown
  for every US name (and before follow-up 1, scored as "no insider
  trading").
  - Owner decision: daily ingestion from SEC EDGAR
    (`data_engineering.sec_edgar_insider`). The job backfills SEC's
    quarterly sets since 2024 Q2 and then reads each day's Form 4 filings
    from the daily index.
  - A day ingested this way matched SEC's quarterly set on all 2,054 rows.
  - The quarterly loader had rejected all of 2026 Q2 because of two lines
    with no transaction code.
- **Estimate revision.** IBES is a hand-run WRDS export, and it goes blank
  92 days after its last consensus.
  - Owner decision: both sources. A fresh IBES row wins. Otherwise the
    stage uses the 30-day change in Yahoo's current-year consensus EPS
    (`data_backbone.analyst_estimates`), the same quantity as IBES's monthly
    FPI-1 revision, without SUE.
  - A nightly job stores the consensus for the names of the latest run, so a
    point-in-time history builds up.
  - Fewer than 3 analysts, or a move over 50% in 30 days (a fiscal-year
    roll, the LRCX trap), gives no signal.
  - `estimate_source` records which source was used.
- **Freshness.** Settings → Data now lists each research extract (insider,
  IBES, JKP, the WRDS link tables) with its newest date, and flags a stale
  one.

## References

Moskowitz & Grinblatt (1999), *Do Industries Explain Momentum?*, JF ·
Jegadeesh & Titman (1993) · Asness, Moskowitz & Pedersen (2013), *Value and
Momentum Everywhere*, JF · Daniel & Moskowitz (2016), *Momentum Crashes*, JFE ·
Blitz, Huij & Martens (2011), *Residual Momentum*, JEF · Blume (1971), *On the
Assessment of Risk*, JF · Scholes & Williams (1977); Dimson (1979) on
non-synchronous trading · He & Litterman (1999); Pastor & Stambaugh (1999),
*Costs of Equity Capital and Model Mispricing*, JF · Lewellen (2015), *The
Cross-section of Expected Stock Returns*, CFR · Goyal & Welch (2008), RFS ·
De Bondt & Thaler (1985), JF · Bradshaw, Brown & Huang (2013), *Do sell-side
analysts exhibit differential target price forecasting ability?*, RAST ·
Frazzini & Pedersen (2014), *Betting Against Beta*, JFE · Novy-Marx (2013), JFE ·
Burns, Engle & Mezrich (1998), *Correlations and Volatilities of
Asynchronous Data*, J. Derivatives · Wang & Ramdas (2024), *Anytime-valid
t-tests and confidence sequences for Gaussian means with unknown variance*,
arXiv 2310.03722 · Ville (1939) on nonnegative supermartingales.
