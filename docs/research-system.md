# Research System

QuantFolio includes a multi-agent research pipeline that produces structured
analysis to assist — not replace — investment decision-making. All agents are
LLM personas operating on the local AI endpoint configured in Control Center
(default model `qwen3.6-35b-a3b`; see `docs/integrations.md`).
They have no ability to place trades, approve transactions, or modify portfolio
state. Outputs are research aids only.

> **Gate retired.** AlphaCrafter (dossier generation, factor mining, screener,
> trader backtest) and Verification run unconditionally — the former
> `experimental_features_enabled` gate was removed end-to-end (rollback =
> git revert). Their scheduled jobs register unconditionally and run
> exclusively in the `quantfolio-worker` process. Output quality still varies;
> treat results as research aids, not advice.

## Stock Research Hub

The Stock Research Hub provides per-ticker deep research accessible from the
**Analyze → Stock Research** sidebar entry (`/research/stock/{ticker}`).

### What It Delivers

- **LLM Research Report** — full narrative analysis covering fundamentals,
  technicals, sentiment, and valuation. Cached per user; force-refresh available.
- **Multi-Horizon Verdicts** — BUY / HOLD / SELL assessments across short,
  medium, and long time horizons.
- **Piotroski F-Score** — fundamental strength score (0-9) derived from
  financial statement signals.
- **Sentiment Analysis** — aggregated news sentiment with optional BERT
  enrichment, configurable look-back window.
- **Technical Analysis** — chart data, price history, and technical indicators.
- **Aggregated Context** — single endpoint combining quote, chart, report,
  news, and fundamentals for a ticker.

### Portfolio-Level Report

The Portfolio Report (`/research/portfolio`) synthesises all individual stock
reports, allocation data, regime state, and the user's risk profile into a
cohesive portfolio-level LLM analysis. Cached for 7 days. Available via:

- `GET  /api/research/portfolio-report` — get or generate
- `POST /api/research/portfolio-report` — force-regenerate

### Key Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/research/stock/{ticker}` | Aggregated context (quote, chart, report, news) |
| `GET /api/research/stock/{ticker}/report` | Get/generate LLM research report |
| `POST /api/research/stock/{ticker}/report` | Force-regenerate report |
| `GET /api/research/stock/{ticker}/piotroski` | Piotroski F-Score |
| `GET /api/research/stock/{ticker}/sentiment` | News sentiment |
| `GET /api/research/stock/{ticker}/verdicts` | Multi-horizon verdicts |
| `GET /api/research/risk-profile` | User risk profile |
| `PUT /api/research/risk-profile` | Update risk profile |
| `GET /api/research/portfolio-report` | Portfolio-level analysis |
| `POST /api/research/portfolio-report` | Force-regenerate portfolio analysis |

### Frontend Pages

- `StockDetailPage` (`frontend/src/pages/StockDetailPage.tsx`) — per-ticker
  deep-dive with tabs for report, verdicts, Piotroski, sentiment, chart
- `PortfolioReportPage` (`frontend/src/pages/PortfolioReportPage.tsx`) —
  portfolio-level analysis with regime context

## Agent Roles

### Research Summariser
Ingests the current portfolio positions, recent news feed items, and any PDF
report uploaded to Report Analysis. Produces a concise situation brief: what has
changed since the last sync, which positions are affected, and what the key macro
themes are. Output feeds the Dossiers panel.

### Quant Critique
Reviews the numeric outputs of the Research Lab (optimisation weights, factor
exposures, backtest equity curves, RL rollout results). Flags statistical concerns:
overfitting signs, look-ahead bias, insufficient history, survivorship, Sharpe
inflation from short samples. Provides a written critique alongside a numeric
confidence score. Never approves an allocation.

### Devil's Advocate
Given a recommendation in `needs_review` state, argues the contrary case.
Considers: concentration risk, liquidity risk, tax implications of realisation,
correlation with existing positions, and historical analogues where similar
setups failed. Designed to surface reasons the recommendation may be wrong.

### Portfolio Fit
Assesses whether a proposed trade fits the user's stated constraints (geographic
allocation targets, sector limits, max single-position size, ESG exclusions stored
in Control Center). Flags deviations and suggests adjustments without executing
them.

### Tax Awareness
Highlights German tax consequences of proposed actions: Abgeltungsteuer,
Kirchensteuer, Vorabpauschale timing, Freistellungsauftrag headroom, FIFO lot
impacts. All output labelled `estimate: true` and `not_tax_advice: true`. Refer
to a qualified tax adviser before acting.

### Strategy Designer
Given a strategy hypothesis from the user (e.g., "momentum on European ETFs,
monthly rebalance, top-3 by 12-1 momentum"), structures it as a backtest
configuration for the Backtests tab. Suggests universe, signal logic, rebalance
frequency, and risk limits. Does not run the backtest — the user must trigger it
manually.

## Safety Constraints

- No agent can approve a recommendation, place a trade, or initiate a DKB sync.
- All agent conclusions are advisory. The user must explicitly click Approve in
  the Review Inbox to move a recommendation from `needs_review` to `approved`.
- Strategy Designer output feeds only the backtest configuration panel — it never
  writes to live portfolio state.
- Tax Awareness outputs always carry the statutory disclaimer. Broker statements
  remain the source of truth for tax reporting.
