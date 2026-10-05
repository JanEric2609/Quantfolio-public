"""Portfolio: DKB holdings integration for QuantLab analytics pipeline.

Sub-package extracted from ``portfolio_bridge.py`` to separate concerns:

- ``isin_resolver`` — ISIN→ticker resolution (yfinance, justETF, overrides)
- ``name_resolver`` — Position name resolution (DB, yfinance, fallback)
- ``bridge`` — Price matrix, holdings summary (daily snapshots live in
  ``app.foundation.portfolio_service``)
- ``metrics_wrappers`` — Risk metrics, factor exposures, rebalancing suggestions
"""

from app.foundation.portfolio.isin_resolver import resolve_isin_to_ticker
from app.foundation.portfolio.name_resolver import resolve_position_name
from app.foundation.portfolio.bridge import (
    build_real_price_matrix,
    get_real_holdings_summary,
)
from app.foundation.portfolio.metrics_wrappers import (
    compute_factor_exposures_real,
    compute_real_holdings_metrics,
    generate_rebalancing_suggestions,
)

__all__ = [
    "build_real_price_matrix",
    "compute_factor_exposures_real",
    "compute_real_holdings_metrics",
    "generate_rebalancing_suggestions",
    "get_real_holdings_summary",
    "resolve_isin_to_ticker",
    "resolve_position_name",
]
