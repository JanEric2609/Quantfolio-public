"""Shared helpers, Pydantic models, and serialisers for the quant API package."""

import json
import logging
from typing import Any


from app.foundation.models.entities import (
    QuantExperiment,
    QuantExperimentRun,
    StrategyGraveyardEntry,
)
from app.foundation.portfolio_price_service import (
    PortfolioPriceService,
    portfolio_price_matrix,
)
from app.foundation.quant import factor_exposures

logger = logging.getLogger(__name__)


def compute_factor_model(db, user_id: str) -> dict[str, Any]:
    """Compute factor model exposures via ETF proxies for a user's portfolio.

    Returns a dict with keys: status, message, exposures, diagnostics.
    Shared between quant/risk.py (route handler) and quant/factors.py (attribution fallback).
    """
    svc = PortfolioPriceService(db, user_id)
    matrix, weights, diagnostics = svc.price_matrix()
    if not matrix:
        return {
            "status": "unavailable",
            "message": "No portfolio price history available.",
            "exposures": {},
            "diagnostics": diagnostics,
        }
    try:
        frame = svc.to_frame(matrix)
        if len(frame) < 30:
            return {
                "status": "unavailable",
                "message": "At least 30 overlapping price observations are needed for a useful factor model.",
                "exposures": {},
                "diagnostics": diagnostics | {"overlap_rows": len(frame)},
            }
        portfolio_returns = svc.weighted_returns(matrix, weights)
        factor_frame = svc.factor_proxy_returns()
        if factor_frame.empty:
            return {
                "status": "unavailable",
                "message": "Factor proxy price data is unavailable.",
                "exposures": {},
                "diagnostics": diagnostics | {"proxy_tickers": list(svc._get_factor_proxies().keys())},
            }
        result = factor_exposures(portfolio_returns, factor_frame)
        result["proxies"] = svc._get_factor_proxies()
        result["source"] = "etf_proxy_factors"
        result["diagnostics"] = diagnostics | {"overlap_rows": len(frame), "factor_rows": len(factor_frame)}
        return result
    except Exception as exc:
        logger.warning("Factor exposure computation failed: %s", exc, exc_info=True)
        return {"status": "unavailable", "message": "Factor exposure computation failed", "exposures": {}, "diagnostics": diagnostics}


def _portfolio_price_matrix(db, user_id: str) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """API-internal alias of :func:`portfolio_price_matrix` (canonical home:
    ``app.foundation.portfolio_price_service``); kept so quant router imports
    and monkeypatch targets stay stable."""
    return portfolio_price_matrix(db, user_id)


def _portfolio_returns_series(
    matrix: dict[str, dict[str, float]],
    weights: dict[str, float],
) -> tuple[list[float], list[str]]:
    return PortfolioPriceService.returns_list_static(matrix, weights)


def _experiment_to_dict(row: QuantExperiment) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "mode": row.mode,
        "universe": json.loads(row.universe_json or "[]"),
        "benchmark_symbol": row.benchmark_symbol,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "rebalance_frequency": row.rebalance_frequency,
        "strategy_type": row.strategy_type,
        "config": json.loads(row.config_json or "{}"),
        "active": row.active,
        "created_at": row.created_at,
    }


def _run_to_dict(row: QuantExperimentRun) -> dict:
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "status": row.status,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "provider_snapshot": json.loads(row.provider_snapshot_json or "{}"),
        "metrics": json.loads(row.metrics_json or "{}"),
        "equity_curve": json.loads(row.equity_curve_json or "[]"),
        "trades": json.loads(row.trades_json or "[]"),
        "warnings": json.loads(row.warnings_json or "[]"),
        "error_message": row.error_message,
    }


def _graveyard_to_dict(row: StrategyGraveyardEntry) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "reason": row.reason,
        "config": json.loads(row.config_json or "{}"),
        "failed_metrics": json.loads(row.failed_metrics_json or "{}"),
        "created_at": row.created_at,
    }


def _sentiment_label(article: dict[str, Any]) -> str:
    """Extract a sentiment label from a news article dict."""
    return str(
        article.get("sentiment")
        or article.get("overall_sentiment")
        or article.get("sentiment_label")
        or ""
    ).lower()


def book_volatility(db, user_id: str) -> tuple[float, str]:
    """Annual volatility of the book at today's weights and where it came from.

    Ledoit-Wolf covariance of up to two years of EUR daily returns; the
    long-run MSCI World figure until the book has a year of history.
    """
    import numpy as np

    from app.foundation.allocation import portfolio_stats, shrunk_covariance
    from app.foundation.quant import price_matrix_to_returns
    from app.foundation.wealth_planner import DEFAULT_VOLATILITY

    matrix, values, _diag = PortfolioPriceService(db, user_id).price_matrix()
    if matrix:
        rets = price_matrix_to_returns(matrix).tail(504)
        total = sum(values.get(c, 0.0) for c in rets.columns)
        if len(rets) >= 252 and total > 0:
            cov, _shrink = shrunk_covariance(rets)
            w = np.array([values.get(c, 0.0) / total for c in rets.columns])
            return float(portfolio_stats(w, cov)["volatility"]), f"your book, {len(rets)} days of EUR returns (Ledoit-Wolf)"
    return DEFAULT_VOLATILITY, "long-run MSCI World in EUR (less than a year of history)"
