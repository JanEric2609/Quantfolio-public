"""Risk metrics, factor exposures, and rebalancing on real DKB holdings.

These are thin orchestration wrappers that:
1. Fetch real DKB holdings via ``portfolio.bridge``
2. Build a price matrix over the holdings
3. Delegate computation to ``quant_metrics``, ``quant``, or ``quant_optim``
4. Format the result for the API layer
"""

from __future__ import annotations

import logging
from decimal import Decimal
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


def _depot_cost_rows(depot_rows: list[Any], depot_costs: dict[str, Any]) -> list[dict[str, Any]]:
    """Per depot of one line: where it sits, what it cost, and where that cost came from."""
    rows = []
    for dp in depot_rows:
        dc = depot_costs.get(dp.id)
        rows.append({
            "source": dp.source, "broker": dp.broker_label, "account_id": dp.account_id, "position_id": dp.id,
            "quantity": float(dp.quantity), "value_eur": float(dp.value),
            "cost_eur": None if dc is None or dc.cost_eur is None else round(float(dc.cost_eur), 2),
            "cost_source": dc.source if dc else None,
            # Only a DKB position can take a hand-entered Einstandswert.
            "can_enter_cost": dp.source == "dkb" and (dc is None or dc.cost_eur is None),
        })
    return rows


def _sale_gain(
    sale_eur: float, line_value: float, depot_rows: list[Any], depot_costs: dict[str, Any],
) -> tuple[float | None, dict[str, Any]]:
    """Gain (before Teilfreistellung) of selling ``sale_eur`` of one line, depot by depot.

    The sale is split over the depots in proportion to their value. A depot
    with FIFO lots uses them (oldest first), one with only a broker average
    cost uses the average, one with neither adds nothing and is listed as
    unknown: the other depots still count. ``None`` only when no depot has a cost.
    """
    from app.foundation.portfolio.cost_basis import fifo_gain

    known = 0.0
    bases: set[str] = set()
    unknown: list[str] = []
    for dp in depot_rows:
        dc = depot_costs.get(dp.id)
        value = float(dp.value)
        if value <= 0:
            continue
        share = sale_eur * value / line_value
        if dc is None or dc.cost_eur is None:
            unknown.append(dp.broker_label)
            continue
        if dc.source in ("lots", "lots_estimated") and dc.lots and dp.quantity > 0:
            price = Decimal(str(value)) / Decimal(dp.quantity)
            units = Decimal(str(share)) / price
            known += float(fifo_gain(dc.lots, units, price))
            bases.add("fifo_estimated" if dc.source == "lots_estimated" else "fifo")
        else:
            known += share * max(-1.0, 1.0 - float(dc.cost_eur) / value)
            bases.add("average")
    info = {
        "gain_basis": None if not bases else (next(iter(bases)) if len(bases) == 1 else "mixed"),
        "gain_complete": bool(bases) and not unknown,
        "cost_unknown_depots": unknown,
    }
    return (known if bases else None), info


def generate_rebalancing_suggestions(
    db: Session,
    user_id: str,
    lookback_days: int = 365,
    *,
    target: str = "sleeves",
    contribution_eur: float | None = None,
    band_pp: float | None = None,
    max_weight: float | None = None,
    sleeve_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Band rebalancing of the synced book toward the plan's sleeves or a covariance-only target.

    The default target ``"sleeves"`` is the owner's plan (core / factor tilt /
    stock picks, see ``decision.monthly_plan.sleeve_plan``, passed in as
    ``sleeve_plan``): every core ETF together is one sleeve, so the world core
    is never sold into a small line. A covariance target is used only when
    asked for, one of ``allocation.METHOD_LABELS`` (ERC, ...). New
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

    use_sleeves = target == "sleeves" and sleeve_plan is not None
    if not use_sleeves and target not in METHOD_LABELS:
        target = "erc"  # no plan given, or an unknown name
    methods_out = {**METHOD_LABELS, **({"sleeves": "Your plan (core / tilt / stock picks)"} if sleeve_plan else {})}
    settings = get_public_settings(db)
    contribution = float(settings.get("monthly_contribution_eur") or 0) if contribution_eur is None else contribution_eur
    if use_sleeves and contribution_eur is None and sleeve_plan:
        # Running plans into a locked sleeve (stock picks) are the owner's own budget, as in the monthly plan.
        contribution = max(0.0, contribution - float(sleeve_plan.get("other_budget_eur") or 0.0))
    band = float(settings.get("plan_drift_band_pp") or 5) if band_pp is None else band_pp
    summary = get_real_holdings_summary(db, user_id)
    by_isin = summary.get("by_ticker", {})
    total_value = float(summary.get("total_value", 0) or 0)
    empty = {
        "available": False, "current_weights": {}, "optimal_weights": {}, "suggestions": [], "lines": [],
        "target_method": target, "methods": methods_out, "estimate": True, "not_tax_advice": True,
    }
    if total_value <= 0 or not by_isin:
        return {**empty, "estimate": True, "not_tax_advice": True, "diagnostics": {"reason": "no synced holdings"}}

    price_df = build_real_price_matrix(db, user_id, lookback_days=lookback_days)
    priced_cols = set(price_df.columns) if price_df is not None and not price_df.empty else set()
    from app.foundation.live_positions import live_positions
    from app.foundation.portfolio.cost_basis import resolve_depot_costs

    depot_rows = live_positions(db, user_id)
    depot_costs = resolve_depot_costs(db, user_id, depot_rows)
    depots_by_key: dict[str, list[Any]] = {}
    for dp in depot_rows:
        depots_by_key.setdefault(dp.isin or (dp.ticker or ""), []).append(dp)
    lines: dict[str, dict[str, Any]] = {}
    for key, pos in by_isin.items():
        ticker = (pos.get("ticker") or "").upper()
        lines[key] = {
            "key": key, "isin": pos.get("isin"), "ticker": ticker or None, "name": pos.get("name"),
            "value_eur": float(pos.get("current_value") or 0),
            "depot_rows": depots_by_key.get(key, []),
            "priced": ticker in priced_cols,
        }
    sleeve_rows: list[dict[str, Any]] = []
    sleeve_of: dict[str, str] = {}
    if use_sleeves:
        assert sleeve_plan is not None
        # Sleeves need no price history: every line with a value takes part.
        priced = {k: v for k, v in lines.items() if v["value_eur"] > 0}
        if not priced:
            return {**empty, "diagnostics": {"reason": "no synced positions with a value"}}
        candidates = {}
        if sleeve_plan.get("band_pp") is not None and band_pp is None:
            band = float(sleeve_plan["band_pp"])
        sleeve_of = {k: sleeve_plan["classify"](v.get("isin") or "") for k, v in priced.items()}
        values = {k: v["value_eur"] for k, v in priced.items()}
        s_cur: dict[str, float] = {}
        for k, sl in sleeve_of.items():
            s_cur[sl] = s_cur.get(sl, 0.0) + values[k]
        s_targets = {sl: float(sleeve_plan["targets"].get(sl, 0.0)) for sl in set(sleeve_plan["targets"]) | set(s_cur)}
        s_buys = allocate_contribution(s_cur, s_targets, contribution)
        s_sales = drift_sales(
            s_cur, s_targets, {sl: bool(sleeve_plan["unlocked"].get(sl)) for sl in s_targets}, contribution, band,
        )
        # A sleeve's money moves pro rata over its lines, so lines keep their mix inside the sleeve.
        buys = {k: s_buys.get(sl, 0.0) * values[k] / s_cur[sl] for k, sl in sleeve_of.items()}
        sales = {k: s_sales[sl] * values[k] / s_cur[sl] for k, sl in sleeve_of.items() if s_sales.get(sl, 0.0) > 0}
        targets_pct = {k: s_targets.get(sl, 0.0) * values[k] / s_cur[sl] for k, sl in sleeve_of.items()}
        priced_total = sum(values.values())
        by_ticker = {(v["ticker"] or k): k for k, v in priced.items()}
        after_buys = {k: values[k] + buys.get(k, 0.0) for k in values}
        total_after_s = priced_total + max(0.0, contribution)
        for sl in sorted(s_targets, key=lambda x: -s_targets[x]):
            after = s_cur.get(sl, 0.0) + s_buys.get(sl, 0.0) - s_sales.get(sl, 0.0)
            sleeve_rows.append({
                "key": sl, "label": sleeve_plan.get("labels", {}).get(sl, sl),
                "current_pct": round(100.0 * s_cur.get(sl, 0.0) / priced_total, 2),
                "target_pct": round(s_targets[sl], 2),
                "after_pct": round(100.0 * after / total_after_s, 2) if total_after_s else None,
                "buy_eur": round(s_buys.get(sl, 0.0), 2), "sell_eur": round(s_sales.get(sl, 0.0), 2),
                "unlocked": bool(sleeve_plan["unlocked"].get(sl)),
            })
    else:
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

    sleeve_unlocked = {r["key"]: r["unlocked"] for r in sleeve_rows}
    sleeve_drift = {r["key"]: r["current_pct"] - r["target_pct"] for r in sleeve_rows}
    out_lines: list[dict[str, Any]] = []
    total_after = priced_total + max(0.0, contribution)
    tax_total = 0.0
    for k, v in priced.items():
        sale = sales.get(k, 0.0)
        buy = buys.get(k, 0.0) + reinvest.get(k, 0.0)
        taxable = tax = None
        gain_info: dict[str, Any] = {"gain_basis": None, "gain_complete": False}
        depot_info = _depot_cost_rows(v["depot_rows"], depot_costs)
        if sale > 0 and v["value_eur"] > 0:
            fund_class, _src = _classify_holding(user_fund_class=None, isin=v["isin"], etf_index=etf_index)
            tf = float(teilfreistellung_pct_for_fund_class(fund_class))
            gain_known, gain_info = _sale_gain(sale, v["value_eur"], v["depot_rows"], depot_costs)
            if gain_known is not None:
                taxable = gain_known * (1.0 - tf)
                tax = max(0.0, taxable) * rate
                tax_total += tax
        weight = v["value_eur"] / priced_total
        tgt = targets_pct[k] / 100.0
        out_lines.append({
            **{f: v[f] for f in ("key", "isin", "ticker", "name", "value_eur")},
            "current_weight": weight, "target_weight": tgt, "drift_pp": 100.0 * (weight - tgt),
            "in_band": (
                abs(100.0 * (weight - tgt)) <= band if not use_sleeves
                else (not sleeve_unlocked.get(sleeve_of[k]) or abs(sleeve_drift[sleeve_of[k]]) <= band)
            ),
            **({"sleeve": sleeve_of[k]} if use_sleeves else {}),
            "buy_eur": round(buy, 2), "sell_eur": round(sale, 2),
            "after_weight": (v["value_eur"] + buy - sale) / total_after if total_after > 0 else None,
            "taxable_gain_eur": None if taxable is None else round(taxable, 2),
            "tax_eur": None if tax is None else round(tax, 2),
            **gain_info,
            "depots": depot_info,
        })
    out_lines.sort(key=lambda r: -abs(r["drift_pp"]))
    suggestions = [
        {"ticker": r["ticker"] or r["key"], "action": "sell" if r["sell_eur"] > 0 else "buy",
         "current_weight": round(r["current_weight"], 4), "target_weight": round(r["target_weight"], 4),
         "diff": round(r["target_weight"] - r["current_weight"], 4),
         "estimated_amount": r["sell_eur"] if r["sell_eur"] > 0 else r["buy_eur"]}
        for r in out_lines if r["buy_eur"] > 0.5 or r["sell_eur"] > 0.5
    ]
    # An unlocked sleeve with a target but no held line has nowhere to put its share: show it as a buy row.
    for r in sleeve_rows:
        if r["buy_eur"] > 0.5 and not any(sleeve_of.get(k) == r["key"] for k in priced):
            funds = list(sleeve_plan.get("tilt_isins") or []) if (sleeve_plan and r["key"] == "tilt") else []
            for fund in funds or [None]:
                amount = r["buy_eur"] / len(funds or [None])
                suggestions.append({
                    "ticker": fund or f"{r['label']} (no fund chosen)", "action": "buy", "isin": fund, "sleeve": r["key"],
                    "current_weight": 0.0, "target_weight": round(r["target_pct"] / 100.0, 4),
                    "diff": round(r["target_pct"] / 100.0, 4), "estimated_amount": round(amount, 2),
                })
    sold_lines = [r for r in out_lines if r["sell_eur"] > 0]
    return {
        "available": True,
        "tax_complete": all(r.get("gain_complete") for r in sold_lines),
        "total_value": round(total_value, 2),
        "target_method": target,
        "target_label": methods_out[target],
        "methods": methods_out,
        "sleeves": sleeve_rows,
        "contribution_eur": contribution,
        "band_pp": band,
        "current_weights": {(r["ticker"] or r["key"]): round(r["current_weight"], 4) for r in out_lines},
        "optimal_weights": {(r["ticker"] or r["key"]): round(r["target_weight"], 4) for r in out_lines},
        "lines": out_lines,
        "suggestions": suggestions,
        "sells": bool(sales),
        "tax_eur": round(tax_total, 2),
        "tax_rate": rate,
        "estimate": True,
        "not_tax_advice": True,
        "tax_note": "Gain after Teilfreistellung, before the Sparerpauschbetrag and before any Vorabpauschale "
        "already taxed. Broker statements are the source of truth.",
        "unpriced": [v["key"] for v in lines.values() if not v["priced"]],
        "covariance": candidates.get("covariance"),
        "optimization_status": "completed",
        "optimization_method": target,
        "diagnostics": {"tickers_used": list(by_ticker), "lookback_days": lookback_days},
    }
