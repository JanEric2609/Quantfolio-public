# Context: Portfolio

## Responsibility

DKB-holdings integration for analytics. Resolves ISINs to tickers
(isin_resolver: yfinance, justETF, overrides), resolves position names
(name_resolver: DB, yfinance, fallback), builds real price matrices and
holdings summaries (bridge; daily snapshots live in
`app.foundation.portfolio_service`), and wraps risk metrics, factor exposures,
and rebalancing suggestions (metrics_wrappers).

## Public surface (facade `app.foundation.portfolio`)

`build_real_price_matrix`, `compute_factor_exposures_real`,
`compute_real_holdings_metrics`, `generate_rebalancing_suggestions`,
`get_real_holdings_summary`, `resolve_isin_to_ticker`,
`resolve_position_name`.

## Key collaborators

- In: `dkb.service`, `llm_portfolio.context`, `discover.pipeline`,
  `research`, `portfolio_service`, `portfolio_analysis`, `price_backfill`,
  `goal_optimizer`, `llm_research`, `app.interface.api.quant.portfolio`,
  `app.interface.api.admin_repair`.
- Out: none cross-context; consumes market data providers internally.

## Contract invariants

- Member of "Decision-loop packages are independent" (independence).
- Not covered by the Phase-F facade-only bans (not a decision-loop context);
  consumers may still import submodules directly.
- Global layering holds: this package is foundation-tier and imports no
  `app.lab`/`app.decision`/`app.interface` module.

## Owner-wave notes

Facade already conformed pre-Phase-F (no changes made). Known ungated debt:
underscore helpers `_try_resolve_isin` (used by portfolio_price_service,
bridge) and `_is_real_name` (used by portfolio_service, admin_repair) are
consumed cross-context despite being private — candidates for promotion or
encapsulation in a future wave.
