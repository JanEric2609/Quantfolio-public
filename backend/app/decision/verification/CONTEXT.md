# Context: Verification

## Responsibility

Three things. The first two are read-only over data owned elsewhere; the third
owns the append-only evidence ledgers of ADR 0018:

1. **"Can I trust it?"** — `trust.py` scores the system's own frozen predictions
   against what happened (Discover picks, advisor calls, mandate verdicts, the
   daily regime label history) with the small-sample statistics in
   `app.foundation.forecast_verification`: exact hit-rate intervals,
   Brier skill bootstrapped over five-week blocks of issue dates, CORP
   reliability, and a four-way evidence state (`too_early` / `skill` /
   `no_evidence` / `harm`). The state comes from the pre-registered
   calendar-time tests in `daily_tests.py` (ADR 0018: F1 ideas sets the
   headline, F2 ranking and F3 advisor are their own families, e ≥ 40 each);
   the per-date basket e-process stays as a secondary row with `e_lower`.
   `ranking_metrics.py` gives the descriptive shadow-ledger numbers (IC, decay,
   quintiles, tiers, gate check) and `history.py` the historical panel (factor
   cards, ranking model and satellite from the stored evidence-gate run).
   Phase 3 adds the ACI range correction (`forecast_verification.aci_coverage`),
   the descriptive advisor-against-picks comparison
   (`daily_tests.paired_comparison`) and `factor_study.py`: the §8 study, run
   once per spec version on back-filled baskets (`trust_factor_study`), and the
   `ideas_neutral` row it gates. It never rewrites a ledger row and shows no number without N and an interval.
2. **Portfolio → Risk** — `risk.py` (VaR, drawdown, Sharpe/Sortino from a
   cash-flow-free series of the *current* holdings' price history; HHI,
   look-through HHI, asset-type and currency exposure with ETF look-through,
   `foundation.etf_currency`, ADR 0007 amendment 4), `stress.py` (three what-if
   scenarios, no probabilities) and `alerts.py` (alert thresholds + single-name
   concentration alerts).
3. **Evidence ledgers (ADR 0018, pre-registered)** — `daily_ledger.py` freezes
   the calendar-time series the primary tests bet on
   (`trust_daily_active_returns`: `ideas`, `advisor`, `ranking`);
   `candidate_outcomes.py` resolves every shadow-ledger snapshot at 5/10/21/63
   trading days (`candidate_outcome`); `ledger_prices.py` is their EUR close
   loader and calendar; `jobs.py` registers `trust_daily_ledger` (nightly
   02:40 UTC, open picks only) and `trust_weekly_ledger` (Saturday 03:40 UTC,
   outcomes and the ranking series). Rows are written once; Postgres triggers
   (migration `0130_trust_evidence_ledgers`) reject any UPDATE. The snapshots
   themselves are written by `discover/shadow_ledger.py` when a run issues.
   Changing a rule here is an amendment to ADR 0018 and must say what had been
   observed when it was made.

The 0–100 confidence score, attribution, comparison, report, summary and audit
endpoints, the 30/30/30/30 baselines in `engine.py`, the `verification_refresh`
nightly job, `track_record.py` and the `confidence_scores` table were deleted in
2026-10 (owner-approved breaking change, migration `0124_trust_ledgers`). The
score was never compared to an outcome, and the series behind it
(`PortfolioSnapshot.total_value`) counted salary and purchases as returns.

## Public surface (facade `app.decision.verification`)

`build_verdict`, `list_calls` (trust); `build_ranking_metrics`, `build_history`;
`jobs.register_trust_daily_ledger_job` /
`jobs.register_trust_weekly_ledger_job` (imported by `app.worker` only);
`compute_risk_metrics`, `RiskAssessment`,
`RiskAlert`, `ReturnBasis` (risk); `run_stress_test`, `StressTestResult`;
`check_concentration_alerts`, `get_alert_thresholds`, `AlertThresholds`.

## Key collaborators

- In: `app.interface.api.trust` (`/api/trust/*`) and
  `app.interface.api.verification` (`/api/verification/risk|stress/{id}`).
  Nothing in `foundation` imports this package any more: the DKB sync used to
  recompute the confidence score through `engine.py`, which removed the last
  seeded cross-layer edge into it.
- Out (lab-tier): `lab.factor_premia` (`latest_cards` and the haircut constants)
  for the historical panel only.
- Out (foundation-tier): `forecast_verification`, `quant_metrics`, `factor_evidence`,
  `portfolio_price_service`, `portfolio_utils`, `etf_currency`,
  `etf_lookthrough`, `settings`, `instrument_taxonomy`, and the ledger entities
  (`DiscoveryPrediction`, `LlmPortfolioDecision`, `PaperPortfolio`,
  `RegimeLabelHistory`). The ledgers are read through the entities, not through
  the advisor/discover/llm_portfolio facades: the trust page needs per-user,
  per-row access that none of the facades expose.

## Contract invariants

- Not a member of "Decision-loop packages are independent" — this package is
  not, and should not become, one of the 7 independence-cluster members.
- Decision-tier under "Global layering"; `app.decision.verification` stays a
  `source_module` of the advisor / llm_portfolio facade-only contracts, so it
  must not import `advisor.*` or `llm_portfolio.*` submodules.
- `regime` calls are recorded (`lab/regime/history.record_regime_label`, one row
  per day) but deliberately not scored: the labels are latent volatility
  states with no realised-state definition. Do not invent one here.
- `verification_alerts` (table/entity) is no longer written; it is kept only
  until a follow-up drops it.
