# Context: Verification

## Responsibility

Two things, both read-only over data owned elsewhere:

1. **"Can I trust it?"** — `trust.py` scores the system's own frozen predictions
   against what happened (Discover picks, advisor calls, mandate verdicts, the
   daily regime label history) with the small-sample statistics in
   `app.foundation.forecast_verification`: exact hit-rate intervals,
   block-bootstrap Brier skill, an anytime-valid e-process per type, and a
   four-way evidence state (`too_early` / `skill` / `no_evidence` / `harm`).
   It never rewrites a ledger row and shows no number without N and an interval.
2. **Portfolio → Risk** — `risk.py` (VaR, drawdown, Sharpe/Sortino from a
   cash-flow-free series of the *current* holdings' price history; HHI,
   look-through HHI, asset-type and currency exposure with ETF look-through,
   `foundation.etf_currency`, ADR 0007 amendment 4), `stress.py` (three what-if
   scenarios, no probabilities) and `alerts.py` (alert thresholds + single-name
   concentration alerts).

The 0–100 confidence score, attribution, comparison, report, summary and audit
endpoints, the 30/30/30/30 baselines in `engine.py`, the `verification_refresh`
nightly job, `track_record.py` and the `confidence_scores` table were deleted in
2026-10 (owner-approved breaking change, migration `0124_trust_ledgers`). The
score was never compared to an outcome, and the series behind it
(`PortfolioSnapshot.total_value`) counted salary and purchases as returns.

## Public surface (facade `app.decision.verification`)

`build_verdict`, `list_calls` (trust); `compute_risk_metrics`, `RiskAssessment`,
`RiskAlert`, `ReturnBasis` (risk); `run_stress_test`, `StressTestResult`;
`check_concentration_alerts`, `get_alert_thresholds`, `AlertThresholds`.

## Key collaborators

- In: `app.interface.api.trust` (`/api/trust/*`) and
  `app.interface.api.verification` (`/api/verification/risk|stress/{id}`).
  Nothing in `foundation` imports this package any more: the DKB sync used to
  recompute the confidence score through `engine.py`, which removed the last
  seeded cross-layer edge into it.
- Out (foundation-tier): `forecast_verification`, `quant_metrics`,
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
