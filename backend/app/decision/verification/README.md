# Verification — "Can I trust it?" and Portfolio → Risk

Two jobs, one package:

1. **Can I trust the system?** (`trust.py`, `GET /api/trust/*`, page `/trust`) scores the
   system's own *frozen* predictions against what happened, honestly at small N.
2. **Is my portfolio at risk?** (`risk.py`, `stress.py`, `alerts.py`,
   `GET /api/verification/risk|stress/{id}`, page Portfolio → Risk) measures the portfolio as it is.

All outputs carry no guarantees and are for informational use only.

## What was removed (2026-10, owner-approved breaking change)

The hand-weighted 0–100 confidence score (`0.30·hist + 0.30·live + 0.20·risk + 0.20·regime`) and
its `confidence_scores` table, plus the attribution, comparison, report, summary, audit-log and
nightly `verification_refresh` machinery around it. The score was never compared to any outcome,
and the return series behind it (`PortfolioSnapshot.total_value` = cash + securities + manual
holdings) counted salary and purchases as returns. Nothing replaces it as a single number: the
Verdict tab shows one evidence state per prediction type instead.

## Trust (`trust.py`)

Per prediction type, from the immutable ledgers:

| Type | Ledger | A hit is |
|---|---|---|
| ideas | `DiscoveryPrediction` rows without a paper sleeve | `excess_return > 0` vs the passive core ETF over the call's horizon |
| advisor | `DiscoveryPrediction` rows with a paper sleeve (holds excluded) | same |
| mandates | `LlmPortfolioDecision.verdict` (unresolvable excluded) | verdict `hit` (partial and miss count as not hit); no naive benchmark, so tested against a coin flip |
| regime | `RegimeLabelHistory` (one call per day) | not scored: no realised-state definition exists for the latent volatility labels, the history just accumulates |

Statistics live in `app.foundation.forecast_verification`: exact Clopper–Pearson hit-rate interval,
Brier score and skill with a moving-block bootstrap, an anytime-valid betting e-process
(threshold 20 ≈ 5 %, 100 ≈ 1 %) for skill and for harm, calls-needed (153 calls to tell 60 % from
50 %), Spiegelhalter Z, reliability bins, range coverage. States: `too_early` (< 20 calls, no
crossing), `skill`, `harm`, `no_evidence`. Calls issued on the same day are correlated, so
`n_issue_days` is reported beside N.

Range coverage uses `expected_return_low/high`: Discover's P10–P90 block-bootstrap band over the
ledger horizon (`dossier_writer.ledger_return_range`) and the advisor's Monte Carlo P5–P95.

## Risk (`risk.py`, `stress.py`, `alerts.py`)

* **Return-based numbers** (VaR 95/99, max/current drawdown, Sharpe/Sortino) come from
  `portfolio_return_series`: daily returns of the *current* holdings weighted by position value,
  from price history (`PortfolioPriceService`, the series Quant Lab's Risk view uses). Never from
  snapshot `total_value`. The response carries `return_basis` (N, window, priced and missing
  assets) so the page can say what the numbers rest on.
* **Holdings-based numbers**: HHI, look-through HHI, asset-type exposure, currency exposure with
  index-ETF look-through (`foundation.etf_currency`, ADR 0007 amendment 4).
* **Alerts**: drawdown (`verification_drawdown_*` settings), currency, look-through note, and
  single-name concentration (`alerts.check_concentration_alerts`; baskets such as an MSCI World
  ETF are not single names; `verification_concentration_*` settings). Computed on request, not stored.
* **Stress**: three what-if scenarios (market drop, euro falls, dollar slump). No probabilities and
  no Monte Carlo number are attached: they are illustrations of exposure.

## Files

| File | Role |
|---|---|
| `trust.py` | verdict + resolved-calls table over the ledgers |
| `risk.py` | return series, VaR/drawdown, concentration, currency exposure, risk alerts |
| `stress.py` | what-if scenarios |
| `alerts.py` | alert thresholds and single-name concentration alerts |
