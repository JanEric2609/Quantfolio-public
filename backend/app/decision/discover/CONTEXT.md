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
summary, snapshot and calibrator read only their own source; advisor rows are
ranked per sleeve and date, and holds carry no IC. Dates overlap (21 trading
days per call), so the IC t is Newey-West, the hit-rate interval is clustered
by date, and both wait for `MIN_INDEPENDENT_WINDOWS` (12) non-overlapping
horizons (docs/archive/audits/2026-09-24-why-it-does-not-work §9 A). Migrations 0088/0089 repaired the seeded config prompt
(data-level, one-time). Remaining debt = the five seeded rows; the
advisor→discover-family edges were sanctioned in Wave A pending exactly this
facade extraction.

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
