# Context: Discover

## Responsibility

Forward-looking candidate discovery. Runs the 8-stage candidate pipeline
(pipeline) with AlphaCrafter integration behind a headless HMM macro regime
gate (regime_gate) and provider-degradation scoring, generates LLM dossiers
with a deterministic fallback (dossier_writer), persists forward predictions
(ledger/predictor), resolves and scores them at expiry (resolution/scoring/
skill_snapshot), checks EU venue/ISIN tradeability (tradeability), manages
the discovery-config registry with perturbation and review flows
(config/config_perturb/config_review), gates each candidate's cohort against
its own resolved track record before approval (candidate_gate), and
orchestrates background runs (orchestrator/jobs).

## Public surface (facade `app.decision.discover`)

`DEFAULT_HORIZON_DAYS`, `DEFAULT_PROMPT_TEMPLATE`, `DEFAULT_SIGNAL_WEIGHTS`,
`DiscoveryState`, `DegradationAssessor`, `DegradationResult`,
`MacroRegimeGate`, `RegimeGateResult`, `STYLE_FRAMINGS`, `activate_config`,
`assess_tradeability`, `calibrate_prediction`, `create_config`,
`deterministic_template`, `generate_prompt_perturbations`,
`generate_weight_perturbations`, `get_active_config`, `get_champion_config`,
`get_config`, `get_or_seed_active_config`, `is_config_stale`,
`list_configs`, `reap_run_if_stale`, `reap_stale_discover_runs`,
`register_discover_refresh_job`, `register_discovery_resolution_job`,
`register_discovery_review_job`, `run_candidate_pipeline`,
`store_prediction`, `submit_discover_job`, `sync_config_to_default`,
`write_dossier`.

## Key collaborators

- In: `advisor.cycle` / `advisor.strategy` (prediction writes, horizon,
  framings — seeded), `llm_portfolio.gates` (tradeability — seeded),
  `app.interface.api.discover`, `app.worker`, `app.main`.
- Out: `alphacrafter` (miner/screener modules + panel), `portfolio.bridge`
  (price matrix), `llm_portfolio` root + `review._local_llm_sync`
  (dossier generation), `advisor.scorecard` (loop E2 refresh — seeded).

## Contract invariants

- Member of "Decision-loop packages are independent" (independence).
- "Global layering" applies (the four-layer spine subsumes the retired
  "Foundation services never import the decision loop" contract).
- "Decision-loop internals are facade-only: discover": seeded rows for the
  advisor reads above and llm_portfolio.gates → tradeability.

## Owner-wave notes

P0 forward-prediction ledger (#111) and skill snapshots (#112) are this
context's foundation. The ledger has two writers: Discover's weekly
shortlist (`portfolio_id` NULL) and the advisor's daily calls (one row per
ticker and paper sleeve, under the Discover run's `run_id`). Discover's skill
summary, snapshot, calibrator, track-record gate and recency penalty read only
their own source (`portfolio_id` NULL; never filter on `run_id`); advisor rows are
ranked per sleeve and date, and holds carry no IC. Dates overlap (21 trading
days per call), so the IC t is Newey-West, the hit-rate interval is clustered
by date, and both wait for `MIN_INDEPENDENT_WINDOWS` (12) non-overlapping
horizons (docs/archive/audits/2026-09-24-why-it-does-not-work §9 A). Migrations 0088/0089 repaired the seeded config prompt
(data-level, one-time). Remaining debt = the five seeded rows; the
advisor→discover-family edges were sanctioned in Wave A pending exactly this
facade extraction.

**Shadow ledger and provenance (ADR 0018, 2026-10-07).** After the
tradeability gate, `shadow_ledger.capture_snapshot` freezes every universe
member of the run in `discover_candidate_snapshot` (composite, rank among
evaluable stocks, shortlisted/picked, entry close), built from the pipeline's
in-memory results because later stages rewrite `DiscoverCandidate`. Every
prediction (both writers) carries `provenance_json`: Discover's stamp from
`shadow_ledger.discover_stamp`, the advisor's from `advisor.cycle._advisor_stamp`.
`calibrator.py` is staged by effective (date-level) labels (ADR 0018 §6): no
probability below 100 issue dates, a monotone logistic with a slope prior up to
1,000, isotonic above, and none when the P10-P90 spread is under 1 pp.
`shadow_ledger.backfill_snapshots` rebuilds past runs as exploratory rows
(`backfilled`, cohort `legacy_unstamped`; `scripts/backfill_shadow_ledger.py`).
Bump `shadow_ledger.COMPOSITE_FORMULA_VERSION` / `UNIVERSE_RULE_VERSION`, or
`calibrator.CALIBRATOR_VERSION["rule"]`, whenever the ranking, the universe or
the calibration rule changes meaning: each bump starts a new cohort. Issue-time
columns of `discovery_prediction` are append-only on Postgres (migration 0130);
a data repair has to drop the trigger deliberately.

**Track B (2026-08-27 onboarding audit remediation) — OOS gating.**
`stage_alpha_miner`'s validity filter (`pipeline.py`) had hardcoded
`min_ic=0.0, min_icir=0.0` — an almost-always-true no-op unlike every other
caller of `evaluate_candidate` — fixed to the real `alphacrafter.miner`
defaults. **Superseded 2026-08-28** (see the Phase 4 consolidation note
below): the real defaults turned out to be an almost-always-*false* gate on
a 3-4-symbol candidate panel, which is the opposite failure mode with the
same symptom (every dossier reads `"alphacrafter_miner_no_valid_factors"`).
`candidate_gate.evaluate_track_record` cohorts `DiscoveryPrediction` rows by
instrument type across all signal-weight versions, turns them into one
observation per 30-day block of prediction dates (the block's mean excess
return), and tests those with an anytime-valid sequential t-test e-process
at alpha = 1% plus a hit-rate floor (ADR 0017 follow-up 4: the former
per-pick Newey-West t proved a no-skill strategy in 36-63% of simulated
first years). Fewer than 3 completed blocks fails open
(`"insufficient_data"`); only an `"unproven"` cohort
withholds (`Recommendation.approval_state` stays `"draft"` instead of the
auto-approved default). Wired advisory-only into `write_dossier`/
`orchestrator` — every evaluation is logged, and `CandidateResponse`
surfaces `track_record_status`/`withheld` so a withheld candidate stays
visible with a badge, never silently hidden. The same gate is the only way
to a BUY verdict (`discover_verdict`): a `proven` cohort buys, and every
other candidate is `WATCH`. The composite ranks but never decides, because
its level moves with every weight change (ADR 0017 follow-up 3).

**Since ADR 0018's amendment of 2026-11-08 stocks are judged differently**:
`equity` reads the trust ledger's primary selection test (`selection_w`: each
pick against its five most similar same-sector non-picks, precision-weighted,
daily, from 2026-11-09). BUY needs e >= 100 after at least 126 observed days,
no harm, futility, noise-check or F1-harm guard, and the hit-rate floor. The
weekly trust ledger job stores that evidence in `app_settings`
(`trust_buy_gate_json`) because `discover` may not import `verification`;
`_stored_selection_evidence` reads it. The block-mean result stays in the
gate as `legacy`; funds, bonds and cash still use it.

A real **per-candidate** in-sample/out-of-sample split inside
`stage_alpha_miner` (as opposed to the per-*cohort* gate above, which judges
already-resolved history, not the candidate's own factor evaluation) is
deliberately deferred — resolved-prediction volume is too thin per
cohort today (Nikolopoulos 2026, arXiv:2604.15531, on backtest sample-size
requirements) to also support a within-candidate split without both halves
becoming statistically meaningless. Revisit once a cohort's `n` reliably
clears ~60-100 resolved rows: at that point, check for within-cohort
dispersion between the two chronological halves (mirrors
`test_graduation.py`'s `test_consistency_criterion_fails_with_one_negative_half`
pattern) — if the halves diverge materially, that's the signal a per-candidate
split would add real information rather than just halving an already-thin
sample.

**Phase 4 factor mining consolidation (2026-08-28 discovery-pipeline
signal audit).** `stage_alpha_miner` no longer re-runs cross-universe
significance gating (`min_ic`/`min_icir`, calibrated for a broad-universe
offline mining run — System A) against a single candidate's 3-4-symbol
panel — Track B's fix above made that gate real but, on a panel this small,
almost-always-*false* (`abs(ic) > 0.02` essentially never holds with so few
cross-sectional observations), which is what produced the universal
`"alphacrafter_miner_no_valid_factors"` caveat this audit traced. System B's
role is now scoring, not re-validating: it queries active `FactorsLibrary`
rows (already vetted by System A's offline mining) and evaluates them
against the candidate's own panel, gating only on `n_obs >=
_MIN_ALPHA_MINER_OBS` (20) — enough observations to trust the sign, not
statistical significance. When `FactorsLibrary` is empty (cold start), it
falls back to the same three deterministic indicators (`momentum_12_1`,
`rsi_14`, `sma_ratio_50_200`) Track B originally gated. `blm.py`
(Black-Litterman posterior for LLM-generated return views, used by
`dossier_writer._try_bl_er`/`_llm_view` behind `er_mode == "bl"`) was
relocated here from `llm_portfolio` in the same audit — it was always an
internal implementation detail of dossier generation, never a
`llm_portfolio` responsibility (see that context's `CONTEXT.md`).

**Discover run audit (2026-09-28, ADR 0017 follow-up).** The composite no
longer has a `regime` signal (it was the same ~1.0 for every candidate), and
`ic_icir` participates only through `composite.ic_signal_usable`.
`stage_alpha_miner` was rebuilt the same day (follow-up branch): instead of
measuring each factor's IC across the candidate and three benchmark ETFs (a
four-point rank correlation, and a property of the factor, not the
candidate), it scores the candidate's **exposure** to validated factors.
For every active `FactorsLibrary` factor with a numeric IC/ICIR it computes
the candidate's cross-sectional z-score (clipped at ±3) within the miner
universe (`alphacrafter.universe.MINER_UNIVERSE`, Euro Stoxx 50 + S&P 100) on
the latest date with a ≥30-name cross-section, and combines them as
`Φ(Σ w_f·sign(IC_f)·z_f / √Σw_f²)` with `w_f ∝ |ICIR_f|` (Grinold & Kahn:
alpha = IC × vol × score). `ic_signal_usable` needs `exposure_score`,
`n_factors ≥ 1` and `panel_size ≥ 30`; stored breakdowns from the old design
never qualify. The universe panel and each factor's values on it are cached
per day (`_EXPOSURE_CACHE`), so a run of ~300 candidates computes them once.
The stage writes no `ac_trial_ledger` rows: scoring validated factors is not
a new trial. With an empty library (the likely state until the nightly miner
validates a factor on the 150-name universe) the signal stays out and the
dossier carries `alphacrafter_no_validated_factors`. `MacroRegimeGate.context_dict()` carries `source` (`hmm` or
`rule_based`), so the dossier can say which regime model it shows; the
header chip is always the rule-based one. A tradeability check that throws
is stored as `tradeability.unknown(...)` (`likely_tradeable=None`), never as
"likely". `tradeability.dkb_search_url` builds the DKB check link
(`/privatkunden/investieren/wertpapiersuche?isin=`).

**Pooled ML signal (2026-09-28, follow-up branch).** `stage_ml_signal`
serves `lab.quant_lab.pooled_ml`, not per-ticker models:
- It loads the newest pooled row, and the universe's latest raw features
  from the exposure stage's daily panel, once per day (`_ML_CACHE`).
- It ranks the candidate within that cross-section and returns
  `prediction` as a percentile, with `signal_kind="ml_pooled"`.
- `composite` uses that percentile directly. Stored per-ticker -1/0/+1
  outputs keep their old mapping.
- Without a served model it returns `prediction=None` plus an
  `ml_status`: `no_model`, `rejected_below_gate`, `short_history` or
  `stale_or_small_cross_section`.
- `discover_ml_training` (Saturdays) refits the model from stored bars
  of the miner universe, with a 6-day cooldown.

Shortlist (audit of the 2026-11-05 run): stocks and funds compete in their own
lanes because their composites rest on seven and four signals. Funds take at most
a third of the slots, stocks the rest; each industry gets one name. The industry
label comes from the provider and can be coarse (Finnhub's "Energy"), so this cap
also tightens some sectors. `data_quality.py` reports which signals a name and a
run went without; a dossier states its horizon, while the track record scores the
estimate over 21 trading days and the trust ledger records 126 as well.

### Run memo, figures by code, shadow signals (plan 2026-11-08, `docs/plan-discover-speed-and-grounded-text.md`)

- `run_candidate_pipeline` holds a run memo: each symbol's history is read once at its longest window and shorter windows are cut from it (`_history`), the book's weekly return and the held ETFs' holdings are built once (`_run_cached`), and an ingest forgets the symbol so its new bars are read. Outside a run nothing is memoised. Run 4f3ddca7 spent 11.5 of 13 minutes rebuilding the 56-holding price matrix for each of 248 candidates. The run logs one `run_candidate_pipeline timings` line per run: seconds per stage, history reads, memo hits.
- The pre-ingest warms the `USD<ccy>=X` pair of every held ticker's currency as well (`fx_symbols`). No `USDCAD=X` existed, so the four Canadian holdings were missing from every portfolio-fit correlation.
- Dossier figures are written by code (`dossier_facts.py`): the model sees keyed phrases (`{momentum}` = "a +29.1 % price return over the 12 months to a month ago, above the middle of this run's candidates"), cites them by key and writes no digit. `render` substitutes them, drops the lead-in words a phrase already carries ("its", "high", a repeated opening) and drops a sentence that writes its own figure or an unknown key; `check_thesis` still runs on the result. Descriptor bins are fixed in code (momentum: strongest from 0.8, as `STRONG_MOMENTUM_RANK`). The jump and a recent fall are part of the momentum phrase, so citing momentum cannot leave them out. `DISCOVER_DOSSIER_SLOTS=0` restores the old prompt.
- Shadow signals, stored per candidate and in the snapshot's `components_json["shadow_signals"]`, never read by the composite: residual momentum (Blitz, Huij & Martens 2011, CAPM residuals vs the benchmark), last close / 52-week high (George & Hwang 2004), information discreteness, momentum without the largest day, the slope-winsorized beta (Welch 2022), the book-volatility change per unit of weight moved into the candidate (`marginal_vol` = rho * sigma_C - sigma_P) and the correlation in the book's falling weeks (Ang & Chen 2002). `verification.ranking_metrics.shadow_signal_ics` and `scripts/shadow_signal_ics.py` give their per-run rank IC with a Newey-West t.

### Jumps, lottery stocks and one beta (review of run 4f3ddca7, 2026-11-08)

- `stage_verification_gate` rejects a candidate past `VOL_REJECT` (100 % a year); 60-100 % stays a flagged risk. In the run only MRNA (187 %) was past it, the next was 88 %. Very high volatility and large daily gains predict low later returns (Ang, Hodrick, Xing and Zhang 2006; Bali, Cakici and Whitelaw 2011); the 100 % line is a judgement.
- `stage_momentum_quality` stores the 12-1 window's largest one-day gain, its date, its share of the window's return, the return without it, and Da, Gurun and Warachka's (2014) information discreteness. `jump_driven` is a day of at least `JUMP_MIN_RETURN` (20 %) giving at least a third of the return; such a stock is ranked on its return without that day, and its dossier names the day as a risk and in the prompt. Momentum from discrete news continued much less than a steady climb in their sample. With the share alone, 96 of 248 names were flagged; with both, 5 (MRNA, DDOG, URI, PLTR, NXPI). Stored scores without the field rank as before.
- The prompt's score dump leaves out the daily `market_beta` when the weekly one exists: the facts name one beta, and IS3S quoted both.
- "High correlation" or "substantial overlap" with the portfolio below a correlation of 0.5 costs its sentence or risk line, like a diversification claim from 0.5.

### Claims, recent falls and insider selling (review of run dd3fe7b9, 2026-11-07)

- A thesis loses a sentence with an unsupported claim instead of being rejected for it: diversification at a correlation from 0.5, "downside protection", "market-implied", "alpha factors". A thesis left under 40 characters is still rejected. Rejected text goes to the log of the process that ran Discover (the API's for a manual run).
- `key_risks` always carry, whoever writes them: gate volatility above 60 %, a close at least 15 % below the 3-month high (`fall_from_3m_high`, which 12-1 momentum cannot show), correlation from 0.5, and net insider selling of at least 0.25 % of market value over 90 days (a judgement, not a tested signal). A stated volatility counts as one risk however it is worded.
- `pit_insider_signal_for_symbol` counts a trade once: a joint Form 4 repeats it per reporting owner.

### Dossier text and shortlist details (review of run 4b6f5ebf, 2026-11-07)

- Known field names in a thesis are put in plain words (`thesis_checks.plain_field_names`); any other code is still rejected. The prompt carries no `trailing_1m_annualized_return`, revenue growth in percent, and insiders as a count of buyers.
- `key_risks` leave out missing data feeds, our own scores, debt-to-equity below 100 %, and earnings more than `EARNINGS_RISK_DAYS` away. Each earnings event and drawdown counts once.
- The fallback template states momentum, correlation, beta and the estimate from the data. A fund gets no single-name risk, and only a listing outside EUR gets a currency risk.
- An industry label that only repeats the sector does not count as an industry, so those names meet the sector cap alone (`candidate_industry`). `shortlist_order` is the one sort key: composite, then portfolio fit, then symbol.

## MSCI World universe (plan 2026-11-08, §6)

The stock pool adds MSCI World's ~1,250 constituents to the static index
lists (which held 770 unique names). `foundation/index_universe.py` reloads
them weekly from the SPDR holdings file and maps each ISIN to its home
listing's Yahoo symbol: a ticker the app already knows for the ISIN first,
else OpenFIGI with the country's exchange code, paced under the free tier.
`index_universe_members.symbol_ok` is cleared when Yahoo has no prices, and
such members stay out. The system cap is 1800 (migration 0145).

`jobs.register_discover_universe_prefetch_job` (01:15 UTC) keeps the large
pool warm: member reload when a week old, then for every pool stock and
currency pair, paced at 0.6 s: bar catch-up, fundamentals past their 7-day
cache and analyst targets past 3 days (`foundation/analyst_cache.py`, which
the run reads first). The run's pre-ingest skips a series already holding
the last completed weekday (`_bars_current`), and each candidate's cached
history is dropped once its stages are done. `UNIVERSE_RULE_VERSION` is 3.

## Tradeability of shares outside the EU and the US (2026-11-10, WP3, D3)

`tradeability.assess` used to call every such share "low", which rejected 40 of the top 50 composites of run 31a6009a (28 Tokyo, 6 Hong Kong, 5 Toronto, 1 Lisbon). It now looks the ISIN up (given, or from `index_universe_members` by symbol) in `venue_instruments`, filled weekly (Mon 03:10 UTC) from Deutsche Börse's "all tradable instruments" files for Xetra and Börse Frankfurt (`foundation/venue_instruments.py`). Listed: "medium", reason "Listed in Frankfurt; confirm at Scalable before ordering". Not listed: "low", reason "No German listing found (Xetra and Börse Frankfurt lists checked)". No list stored yet, or no ISIN: the old "availability uncertain" answer. No gettex or EIX list is published, so Frankfurt is a proxy, not proof. `.LS` and `.IR` count as EU venues.

## Stage cache (2026-11-10, WP9)

`stage_cache.py` caches the four user-independent stages (momentum, backtest against the benchmark, verification gate, quant signals) per (symbol, stage, bar date) in `discover_stage_cache`. The nightly `discover_stage_precompute` job (03:00 UTC, after the 01:15 prefetch) fills it for the universe's stocks; a run loads an entry when its bar date equals the symbol's latest stored bar and computes (and stores) otherwise. Every result is JSON-roundtripped (`canonical`) whether computed or loaded, so a run cannot tell the two apart. Portfolio fit, ranking and the LLM stay on demand; stages that read news or fundamentals are never cached. The key carries the arguments the symbol fixes (`is_etf`, `asset_class`); the benchmark's own bars are assumed to arrive with the symbol's in the same nightly prefetch.

**Not built, on purpose (WP9 steps 4 and 5).** Skipping the expensive stages for names the tradeability gate rejects would shrink the shadow ledger's cohort: `shadow_ledger._POST_PIPELINE_STAGES` counts a `tradeability_gate` rejection as still evaluable, so those names' scores are needed. The 4-worker candidate loop was the fallback for a postponed cache; the cache exists, so it is not needed.

## Shortlist stability draws (WP12b)

`stage_momentum_quality` stores `momentum_12_1m_shifts`: the 12-1 momentum with both window ends moved 1 to 5 trading days back, computed from the bars the stage already loaded. `shortlist_stability.stability` draws one shift per draw (0 to 5, the same for every name) and replaces the momentum signal by the shifted cross-sectional rank. Entries cached before this change carry no shifts; those names keep their own rank until the cache rolls over.
