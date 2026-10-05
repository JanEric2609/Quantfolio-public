"""Risk metrics, factor exposures, and rebalancing on real DKB holdings.

These are thin orchestration wrappers that:
1. Fetch real DKB holdings via ``portfolio.bridge``
2. Build a price matrix over the holdings
3. Delegate computation to ``quant_metrics``, ``quant``, or ``quant_optim``
4. Format the result for the API layer
"""

from __future__ import annotations

import logging
from typing import Any, cast

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 4a. Risk metrics on real holdings
# ---------------------------------------------------------------------------


def compute_real_holdings_metrics(
    db: Session,
    user_id: str,
    benchmark: str | None = None,
    lookback_days: int = 365,
    risk_free: float | None = None,
) -> dict[str, Any]:
    """Sharpe, Sortino, Calmar, CVaR, beta/alpha of the synced book, all in EUR.

    ``benchmark`` defaults to the configured one (MSCI World in EUR); both
    series are EUR returns paired by date.
    """
    from app.foundation.eur_prices import benchmark_returns_eur
    from app.foundation.portfolio.bridge import build_real_price_matrix, get_real_holdings_summary
    from app.foundation import quant_metrics
    from app.foundation.settings import get_risk_free_rate

    if risk_free is None:
        risk_free = get_risk_free_rate(db)

    price_df = build_real_price_matrix(db, user_id, lookback_days=lookback_days)
    if price_df is None or price_df.empty:
        return {
            "available": False,
            "metrics": {},
            "equity_curve": [],
            "diagnostics": {"reason": "insufficient price data"},
        }

    # Current weights from holdings summary
    summary = get_real_holdings_summary(db, user_id)
    by_ticker = summary.get("by_ticker", {})
    total_value = summary.get("total_value", 0)
    if total_value <= 0 or not by_ticker:
        return {
            "available": False,
            "metrics": {},
            "equity_curve": [],
            "diagnostics": {"reason": "no holdings with value"},
        }

    # Build weight series aligned to price matrix columns
    weight_map: dict[str, float] = {}
    for key, pos in by_ticker.items():
        ticker = (pos.get("ticker") or pos.get("isin") or key).upper()
        if ticker in price_df.columns:
            weight_map[ticker] = weight_map.get(ticker, 0) + pos.get("current_value", 0) / total_value

    if not weight_map:
        return {
            "available": False,
            "metrics": {},
            "equity_curve": [],
            "diagnostics": {"reason": "no tickers matched price matrix"},
        }

    # Normalise weights
    w_sum = sum(weight_map.values())
    if w_sum > 0:
        weight_map = {k: v / w_sum for k, v in weight_map.items()}

    # Compute weighted portfolio returns
    returns_df = price_df[list(weight_map.keys())].pct_change().dropna()
    weight_arr = np.array([weight_map[t] for t in weight_map])
    port_returns = returns_df.mul(weight_arr, axis=1).sum(axis=1)
    port_list = port_returns.tolist()
    port_dates = [d.strftime("%Y-%m-%d") for d in pd.DatetimeIndex(port_returns.index)]

    metrics = quant_metrics.full_risk_report(port_list, risk_free=risk_free)
    symbol = None
    overlap = 0
    try:
        symbol, bench_by_date = benchmark_returns_eur(db, benchmark, days=lookback_days + 30)
        _dates, p_al, b_al = quant_metrics.align_by_date(dict(zip(port_dates, port_list)), bench_by_date)
        overlap = len(_dates)
        if overlap >= 20:
            paired = quant_metrics.full_risk_report(p_al, benchmark_returns=b_al, risk_free=risk_free)
            for key in (
                "beta", "alpha", "r_squared", "treynor", "alpha_t_stat", "beta_dimson",
                "tracking_error", "information_ratio", "benchmark_samples",
            ):
                metrics[key] = paired.get(key)
    except Exception:
        logger.debug("Benchmark %s unavailable for real holdings metrics", benchmark, exc_info=True)

    # Constant-weight growth index of today's holdings (base 100). It shows
    # how the current mix moved, not the book's own return: that is the TWR
    # unit value of /portfolio/real/performance.
    equity = 100.0
    equity_curve: list[dict[str, Any]] = []
    for d, ret in zip(port_dates, port_list, strict=True):
        equity *= 1 + ret
        equity_curve.append({"date": d, "value": round(equity, 6)})

    return {
        "available": True,
        "metrics": {k: round(v, 6) if isinstance(v, float) else v for k, v in metrics.items() if k != "drawdown"},
        "max_drawdown": metrics.get("drawdown", {}),
        "equity_curve": equity_curve[-252:],  # last ~1 year
        "diagnostics": {
            "tickers_used": list(weight_map.keys()),
            "weight_count": len(weight_map),
            "return_samples": len(port_list),
            "benchmark": symbol,
            "benchmark_currency": "EUR",
            "benchmark_samples": overlap,
            "lookback_days": lookback_days,
        },
    }


# ---------------------------------------------------------------------------
# 4b. Factor exposures on real holdings
# ---------------------------------------------------------------------------


def compute_factor_exposures_real(
    db: Session,
    user_id: str,
    lookback_days: int = 365,
) -> dict[str, Any]:
    """Factor exposures (market, size, value, momentum) on real DKB holdings."""
    from app.foundation.portfolio.bridge import build_real_price_matrix, get_real_holdings_summary
    from app.foundation import market as market_service
    from app.foundation.quant import factor_exposures

    price_df = build_real_price_matrix(db, user_id, lookback_days=lookback_days)
    if price_df is None or price_df.empty:
        return {"available": False, "exposures": {}, "r_squared": 0.0, "diagnostics": {"reason": "insufficient price data"}}

    summary = get_real_holdings_summary(db, user_id)
    by_ticker = summary.get("by_ticker", {})
    total_value = summary.get("total_value", 0)
    if total_value <= 0 or not by_ticker:
        return {"available": False, "exposures": {}, "r_squared": 0.0, "diagnostics": {"reason": "no holdings"}}

    weight_map: dict[str, float] = {}
    for key, pos in by_ticker.items():
        ticker = (pos.get("ticker") or pos.get("isin") or key).upper()
        if ticker in price_df.columns:
            weight_map[ticker] = weight_map.get(ticker, 0) + pos.get("current_value", 0) / total_value

    if not weight_map:
        return {"available": False, "exposures": {}, "r_squared": 0.0, "diagnostics": {"reason": "no tickers in price matrix"}}

    w_sum = sum(weight_map.values())
    if w_sum > 0:
        weight_map = {k: v / w_sum for k, v in weight_map.items()}

    returns_df = price_df[list(weight_map.keys())].pct_change().dropna()
    weight_arr = np.array([weight_map[t] for t in weight_map])
    port_returns = returns_df.mul(weight_arr, axis=1).sum(axis=1)

    # Factor proxy ETFs
    proxies = {
        "market": "EUNL.DE",
        "size": "IUSN.DE",
        "value": "IWVL.L",
        "momentum": "IS3R.DE",
    }
    factor_data: dict[str, dict[str, float]] = {}
    for name, ticker in proxies.items():
        try:
            rows = market_service.history(db, ticker, days=lookback_days + 60)
            if rows:
                factor_data[name] = {row["date"].isoformat(): row["close"] for row in rows}
        except Exception:
            logger.debug("Factor proxy %s unavailable", ticker, exc_info=True)

    from app.foundation.quant import price_matrix_to_returns

    factor_frame = price_matrix_to_returns(factor_data)

    if factor_frame.empty or len(factor_frame) < 20:
        return {"available": False, "exposures": {}, "r_squared": 0.0, "diagnostics": {"reason": "insufficient factor proxy data"}}

    result = factor_exposures(port_returns, factor_frame)
    return {
        "available": result.get("status") == "completed",
        "exposures": result.get("exposures", {}),
        "r_squared": result.get("r_squared", 0.0),
        "alpha_daily": result.get("alpha_daily", 0.0),
        "proxies": proxies,
        "diagnostics": {"return_samples": len(port_returns), "factor_samples": len(factor_frame)},
    }


# ---------------------------------------------------------------------------
# 4c. Rebalancing suggestions for real holdings
# ---------------------------------------------------------------------------


def generate_rebalancing_suggestions(
    db: Session,
    user_id: str,
    lookback_days: int = 365,
    *,
    target: str = "erc",
    contribution_eur: float | None = None,
    band_pp: float | None = None,
    max_weight: float | None = None,
) -> dict[str, Any]:
    """Band rebalancing of the synced book toward a covariance-only target.

    The target is one of ``allocation.METHOD_LABELS`` (ERC by default). New
    money (``contribution_eur``, default the monthly contribution) goes to
    the most underweight lines first; a line is sold only when it sits more
    than ``band_pp`` (default the plan's drift band) above target and a year
    of contributions would not fix it, and the proceeds go to the
    underweights. Each sale shows the gain it realises, after
    Teilfreistellung, and the flat-rate tax on it before any allowance or NV
    certificate. Lines without prices are left as they are.
    """
    from app.foundation.allocation import METHOD_LABELS, allocate_contribution, allocation_candidates, drift_sales
    from app.foundation.portfolio.bridge import build_real_price_matrix, get_real_holdings_summary
    from app.foundation.settings import get_public_settings
    from app.foundation.tax_calc import teilfreistellung_pct_for_fund_class
    from app.foundation.tax_calc.jurisdictions.de.allowance_split import withholding_rate
    from app.foundation.tax_cockpit import _church_rate, _classify_holding, _safe_etf_index

    if target not in METHOD_LABELS:
        target = "erc"
    settings = get_public_settings(db)
    contribution = float(settings.get("monthly_contribution_eur") or 0) if contribution_eur is None else contribution_eur
    band = float(settings.get("plan_drift_band_pp") or 5) if band_pp is None else band_pp
    summary = get_real_holdings_summary(db, user_id)
    by_isin = summary.get("by_ticker", {})
    total_value = float(summary.get("total_value", 0) or 0)
    empty = {
        "available": False, "current_weights": {}, "optimal_weights": {}, "suggestions": [], "lines": [],
        "target_method": target, "methods": METHOD_LABELS,
    }
    if total_value <= 0 or not by_isin:
        return {**empty, "diagnostics": {"reason": "no synced holdings"}}

    price_df = build_real_price_matrix(db, user_id, lookback_days=lookback_days)
    priced_cols = set(price_df.columns) if price_df is not None and not price_df.empty else set()
    lines: dict[str, dict[str, Any]] = {}
    for key, pos in by_isin.items():
        ticker = (pos.get("ticker") or "").upper()
        costs = [p.get("cost_basis") for p in summary.get("positions", []) if (p.get("isin") or p.get("ticker")) == key]
        # One depot without a cost leaves the gain unknown, not overstated.
        cost = sum(costs) if costs and all(c is not None for c in costs) else None
        lines[key] = {
            "key": key, "isin": pos.get("isin"), "ticker": ticker or None, "name": pos.get("name"),
            "value_eur": float(pos.get("current_value") or 0), "cost_eur": cost,
            "priced": ticker in priced_cols,
        }
    priced = {k: v for k, v in lines.items() if v["priced"] and v["value_eur"] > 0}
    if len(priced) < 1 or price_df is None:
        return {**empty, "diagnostics": {"reason": "no price history for the synced positions"}}

    returns = cast(pd.DataFrame, price_df[[v["ticker"] for v in priced.values()]].pct_change(fill_method=None).dropna())
    priced_total = sum(v["value_eur"] for v in priced.values())
    current_w = {v["ticker"]: v["value_eur"] / priced_total for v in priced.values()}
    candidates = allocation_candidates(returns, current_w, max_weight=max_weight)
    method = candidates.get("methods", {}).get(target)
    if not method:
        return {**empty, "diagnostics": {"reason": candidates.get("errors", {}).get(target, "target unavailable")}}

    by_ticker = {v["ticker"]: k for k, v in priced.items()}
    values = {k: v["value_eur"] for k, v in priced.items()}
    targets_pct = {by_ticker[t]: 100.0 * w for t, w in method["weights"].items()}
    buys = allocate_contribution(values, targets_pct, contribution)
    after_buys = {k: values[k] + buys.get(k, 0.0) for k in values}
    sales = drift_sales(values, targets_pct, {k: True for k in values}, contribution, band)
    after_sales = {k: after_buys[k] - sales.get(k, 0.0) for k in values}
    reinvest = allocate_contribution(after_sales, targets_pct, sum(sales.values())) if sales else {}
    rate = float(withholding_rate(_church_rate(db)))
    etf_index = _safe_etf_index()

    out_lines: list[dict[str, Any]] = []
    total_after = priced_total + max(0.0, contribution)
    tax_total = 0.0
    for k, v in priced.items():
        sale = sales.get(k, 0.0)
        buy = buys.get(k, 0.0) + reinvest.get(k, 0.0)
        taxable = tax = None
        if sale > 0 and v["cost_eur"] is not None and v["value_eur"] > 0:
            fund_class, _src = _classify_holding(user_fund_class=None, isin=v["isin"], etf_index=etf_index)
            tf = float(teilfreistellung_pct_for_fund_class(fund_class))
            gain = sale * max(-1.0, 1.0 - v["cost_eur"] / v["value_eur"])
            taxable = gain * (1.0 - tf)
            tax = max(0.0, taxable) * rate
            tax_total += tax
        weight = v["value_eur"] / priced_total
        tgt = targets_pct[k] / 100.0
        out_lines.append({
            **{f: v[f] for f in ("key", "isin", "ticker", "name", "value_eur")},
            "current_weight": weight, "target_weight": tgt, "drift_pp": 100.0 * (weight - tgt),
            "in_band": abs(100.0 * (weight - tgt)) <= band,
            "buy_eur": round(buy, 2), "sell_eur": round(sale, 2),
            "after_weight": (v["value_eur"] + buy - sale) / total_after if total_after > 0 else None,
            "taxable_gain_eur": None if taxable is None else round(taxable, 2),
            "tax_eur": None if tax is None else round(tax, 2),
        })
    out_lines.sort(key=lambda r: -abs(r["drift_pp"]))
    suggestions = [
        {"ticker": r["ticker"] or r["key"], "action": "sell" if r["sell_eur"] > 0 else "buy",
         "current_weight": round(r["current_weight"], 4), "target_weight": round(r["target_weight"], 4),
         "diff": round(r["target_weight"] - r["current_weight"], 4),
         "estimated_amount": r["sell_eur"] if r["sell_eur"] > 0 else r["buy_eur"]}
        for r in out_lines if r["buy_eur"] > 0.5 or r["sell_eur"] > 0.5
    ]
    return {
        "available": True,
        "total_value": round(total_value, 2),
        "target_method": target,
        "target_label": METHOD_LABELS[target],
        "methods": METHOD_LABELS,
        "contribution_eur": contribution,
        "band_pp": band,
        "current_weights": {r["ticker"]: round(r["current_weight"], 4) for r in out_lines},
        "optimal_weights": {r["ticker"]: round(r["target_weight"], 4) for r in out_lines},
        "lines": out_lines,
        "suggestions": suggestions,
        "sells": bool(sales),
        "tax_eur": round(tax_total, 2),
        "tax_rate": rate,
        "unpriced": [v["key"] for v in lines.values() if not v["priced"]],
        "covariance": candidates.get("covariance"),
        "optimization_status": "completed",
        "optimization_method": target,
        "diagnostics": {"tickers_used": list(by_ticker), "lookback_days": lookback_days},
    }
