"""Portfolio analytics API endpoints — portfolio metrics, optimisation, projection and the real book."""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.interface.api.quant._common import (
    _portfolio_returns_series,
    book_volatility,
    compute_factor_model,
)
from app.foundation.core.db import get_db
from app.foundation.models.entities import User
from app.foundation import quant_metrics
from app.foundation.auth import current_user
from app.foundation.portfolio_price_service import PortfolioPriceService
from app.foundation.quant import (
    historical_var,
    parametric_var,
)
from app.foundation.settings import get_risk_free_rate

router = APIRouter(tags=["quant-portfolio"])
logger = logging.getLogger(__name__)

# A year of daily returns before an annualised figure is shown without a
# warning, and two months of common dates before a benchmark statistic is.
MIN_RISK_HISTORY = 252
MIN_BENCHMARK_OVERLAP = 40

# All-World / ACWI funds: World + Emerging Markets in one.
ALL_WORLD_ISINS = {"IE00BK5BQT80", "IE00B3RBWM25", "IE00B6R52259", "IE00B3YLTY66"}


# ---------------------------------------------------------------------------
# Response schemas for the 3 previously-untyped risk endpoints
# (`/portfolio/risk`, `/portfolio/real/summary`, `/portfolio/real/risk`).
#
# Ported field-for-field from the hand-written TS interfaces in
# frontend/src/lib/api.ts (QuantRiskMetrics, QuantRiskSeries,
# QuantPortfolioRisk, RealPortfolioMetricsResponse, RealPortfolioRiskResponse)
# and cross-checked against captured live JSON from the actual handler bodies
# below (`quant_metrics.full_risk_report` / `risk_series`,
# `portfolio.metrics_wrappers.compute_real_holdings_metrics` /
# `compute_factor_exposures_real`) — not just the TS types, which the source
# audit (docs/archive/audits/2026-08-26/deepdive-03-quantlab.md, Issue 6) flags as
# "unverified hand-maintained guesses".
#
# All handlers below still return plain dicts (unchanged), and every field
# here is Optional / has a default — this is purely additive typing for the
# OpenAPI contract, not a behaviour change. `response_model_exclude_unset`
# is used on the two endpoints whose helper functions omit keys entirely on
# certain early-return branches (rather than emitting `null`), so the wire
# payload's key presence is preserved exactly as-is (verified in
# tests/test_quant_portfolio_response_models.py).
#
# One field found in live JSON but absent from the frontend's hand-written
# type: `metrics.samples` (an int, from `quant_metrics.full_risk_report()`'s
# `"samples": len(rs)` entry) — not documented on `QuantRiskMetrics` in
# api.ts. Included here as an optional field so it round-trips unchanged.
# ---------------------------------------------------------------------------


class QuantRiskMetricsCore(BaseModel):
    """Scalar risk/return metrics from `quant_metrics.full_risk_report()`.

    Every field is Optional because this same shape backs the real-holdings
    `metrics` sub-object, which is `{}` on every early-return branch (no
    price data / no holdings / no matched tickers) and a `Partial<...>` on
    the frontend for exactly that reason. `beta`/`alpha`/`r_squared`/
    `treynor` are additionally the genuine "only present with a benchmark"
    fields noted in api.ts's `QuantRiskMetrics` doc comment.
    """

    samples: Optional[int] = None
    annualised_return: Optional[float] = None
    annualised_volatility: Optional[float] = None
    downside_deviation: Optional[float] = None
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    calmar: Optional[float] = None
    skewness: Optional[float] = None
    kurtosis_excess: Optional[float] = None
    historical_cvar: Optional[float] = None
    parametric_cvar: Optional[float] = None
    beta: Optional[float] = None
    alpha: Optional[float] = None
    r_squared: Optional[float] = None
    treynor: Optional[float] = None
    alpha_t_stat: Optional[float] = None
    beta_dimson: Optional[float] = None
    tracking_error: Optional[float] = None
    information_ratio: Optional[float] = None
    benchmark_samples: Optional[int] = None


class QuantDrawdownSchema(BaseModel):
    """`quant_metrics.max_drawdown()` output. Fields optional so an empty
    `{}` upstream (e.g. real-holdings unavailable branches) round-trips as
    `{}` rather than a pair of explicit `null`s under `exclude_unset`."""

    max_drawdown: Optional[float] = None
    max_drawdown_duration: Optional[float] = None


class QuantSeriesPoint(BaseModel):
    """One `{date, value}` point from a `quant_metrics.risk_series()` series."""

    date: str
    value: float


class QuantRiskFull(QuantRiskMetricsCore):
    """The `risk` object returned by `GET /portfolio/risk` on its available
    branch: `full_risk_report()` + `historical_var`/`parametric_var` +
    `risk_series()`'s chart-ready series + `insufficient_history`."""

    drawdown: QuantDrawdownSchema = Field(default_factory=QuantDrawdownSchema)
    historical_var: Optional[float] = None
    parametric_var: Optional[float] = None
    insufficient_history: Optional[bool] = None
    risk_free: Optional[float] = None
    equity_curve: list[QuantSeriesPoint] = Field(default_factory=list)
    rolling_drawdown: list[QuantSeriesPoint] = Field(default_factory=list)
    returns: list[float] = Field(default_factory=list)
    regression_points: list[tuple[float, float]] = Field(default_factory=list)
    rolling_beta: list[QuantSeriesPoint] = Field(default_factory=list)


class QuantPortfolioRiskResponse(BaseModel):
    """`GET /portfolio/risk`. On the unavailable branch (`portfolio.py`
    line ~74) the handler returns only `{"available": False, "diagnostics":
    ...}` — `benchmark`/`risk` are entirely absent, not null — hence
    Optional with no default rendering (see `response_model_exclude_unset`
    on the route)."""

    available: bool
    benchmark: Optional[str] = None
    # Share (0-100) of the priced book held in the benchmark fund itself.
    benchmark_overlap_pct: Optional[float] = None
    risk: Optional[QuantRiskFull] = None
    diagnostics: Optional[dict[str, Any]] = None


class RealPortfolioMetricsResponse(BaseModel):
    """`GET /portfolio/real/summary`, i.e.
    `metrics_wrappers.compute_real_holdings_metrics()`'s return shape.

    `max_drawdown` is only present (as a key at all) on the success path —
    each of the three `available: False` early returns omits it rather than
    nulling it, so it stays unset/omitted via `response_model_exclude_unset`.
    `metrics` and `equity_curve` are always present, just `{}`/`[]` on
    failure branches, so they're typed with defaults rather than Optional.
    """

    available: bool
    metrics: QuantRiskMetricsCore = Field(default_factory=QuantRiskMetricsCore)
    max_drawdown: Optional[QuantDrawdownSchema] = None
    equity_curve: list[QuantSeriesPoint] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class RealPortfolioRiskDiagnostics(BaseModel):
    metrics_diagnostics: dict[str, Any] = Field(default_factory=dict)
    factor_diagnostics: dict[str, Any] = Field(default_factory=dict)


class RealPortfolioRiskResponse(BaseModel):
    """`GET /portfolio/real/risk`. Unlike `real/summary`, this handler
    (`portfolio.py`) always builds every key via `.get(key, default)`, so
    every field here is always present on the wire (never omitted) —
    `max_drawdown`/`factor_exposures` are simply `{}` rather than absent on
    the unavailable branches."""

    available: bool
    metrics: QuantRiskMetricsCore = Field(default_factory=QuantRiskMetricsCore)
    max_drawdown: QuantDrawdownSchema = Field(default_factory=QuantDrawdownSchema)
    factor_exposures: dict[str, float] = Field(default_factory=dict)
    factor_r_squared: float = 0.0
    diagnostics: RealPortfolioRiskDiagnostics = Field(default_factory=RealPortfolioRiskDiagnostics)


@router.get("/portfolio/summary")
def portfolio_quant_summary(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    svc = PortfolioPriceService(db, user.id)
    matrix, weights, diagnostics = svc.price_matrix()
    returns, dates = _portfolio_returns_series(matrix, weights)
    if not returns:
        return {
            "available": False,
            "message": "Not enough priced history to compute analytics.",
            "diagnostics": diagnostics,
        }
    risk = quant_metrics.full_risk_report(returns, risk_free=get_risk_free_rate(db))
    risk.update(quant_metrics.risk_series(returns, dates))
    return {
        "available": True,
        "samples": len(returns),
        "weights": weights,
        "risk": risk,
        "diagnostics": diagnostics,
    }


@router.get("/portfolio/risk", response_model=QuantPortfolioRiskResponse, response_model_exclude_unset=True)
def portfolio_quant_risk(
    benchmark: str | None = Query(
        None, description="Benchmark ticker; defaults to the configured one (MSCI World in EUR, EUNL.DE)."
    ),
    confidence: float = 0.95,
    risk_free: float | None = Query(
        None, description="Annualised risk-free rate override; defaults to the resolved ECB rate."
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Risk of the book in EUR against a benchmark in EUR, paired by date.

    Both series are EUR returns (see ``eur_prices``) and every two-series
    statistic (beta, alpha, R^2, the scatter, rolling beta) is computed on
    the dates both have a return, never by trailing position.
    """
    from app.foundation.eur_prices import benchmark_returns_eur

    if risk_free is None:
        risk_free = get_risk_free_rate(db)
    svc = PortfolioPriceService(db, user.id)
    matrix, weights, diagnostics = svc.price_matrix()
    returns, dates = _portfolio_returns_series(matrix, weights)
    if not returns:
        return {"available": False, "diagnostics": diagnostics}

    symbol, bench_by_date = benchmark_returns_eur(db, benchmark)
    aligned_dates, port_al, bench_al = quant_metrics.align_by_date(dict(zip(dates, returns)), bench_by_date)
    diagnostics = {
        **diagnostics,
        "benchmark_currency": "EUR",
        "benchmark_samples": len(aligned_dates),
        "benchmark_available": len(aligned_dates) >= MIN_BENCHMARK_OVERLAP,
    }

    risk = quant_metrics.full_risk_report(returns, risk_free=risk_free, confidence=confidence)
    if len(aligned_dates) >= MIN_BENCHMARK_OVERLAP:
        paired = quant_metrics.full_risk_report(
            port_al, benchmark_returns=bench_al, risk_free=risk_free, confidence=confidence,
        )
        for key in (
            "beta", "alpha", "r_squared", "treynor", "alpha_t_stat", "beta_dimson",
            "tracking_error", "information_ratio", "benchmark_samples",
        ):
            risk[key] = paired.get(key)
    risk["historical_var"] = historical_var(returns, confidence)
    risk["parametric_var"] = parametric_var(returns, confidence)
    risk.update(quant_metrics.risk_series(
        returns, dates,
        benchmark_returns=bench_by_date if len(aligned_dates) >= MIN_BENCHMARK_OVERLAP else None,
    ))
    risk["insufficient_history"] = len(returns) < MIN_RISK_HISTORY
    risk["risk_free"] = risk_free
    total_value = sum(v for v in weights.values() if v > 0)
    overlap = weights.get(symbol.upper(), 0.0) if symbol else 0.0
    return {
        "available": True,
        "benchmark": symbol,
        "benchmark_overlap_pct": round(100.0 * overlap / total_value, 2) if total_value > 0 else 0.0,
        "risk": risk,
        "diagnostics": diagnostics,
    }


@router.get("/portfolio/optimization")
def portfolio_optimization(
    max_weight: float | None = Query(None, gt=0, le=1, description="Cap per instrument (fraction)."),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Covariance-only target allocations for the instruments held (EUR returns, Ledoit-Wolf).

    No expected returns: equal weight, inverse volatility, minimum variance,
    equal risk contribution and HRP, each with its volatility, risk
    contributions, diversification ratio and turnover from today's weights.
    ``weights_current`` are fractions (they used to be EUR values, which the
    weights-diff sheet subtracted from target fractions).
    """
    from app.foundation.allocation import allocation_candidates
    from app.foundation.quant import price_matrix_to_returns

    svc = PortfolioPriceService(db, user.id)
    matrix, values, diagnostics = svc.price_matrix()
    if not matrix:
        return {"available": False, "diagnostics": diagnostics}
    returns = price_matrix_to_returns(matrix).tail(504)  # two years of trading days
    total = sum(v for k, v in values.items() if k in returns.columns and v > 0)
    current = {k: v / total for k, v in values.items() if k in returns.columns and v > 0} if total else {}
    result = allocation_candidates(returns, current, max_weight=max_weight)
    return {
        **result,
        "weights_current": current,
        "values_current_eur": {k: values[k] for k in current},
        "optimizations": {"methods": result.get("methods", {})},
        "diagnostics": diagnostics,
    }


@router.get("/portfolio/allocator")
def portfolio_allocator(
    contribution_eur: float | None = Query(None, ge=0, description="Amount to split; defaults to monthly_contribution_eur."),
    max_weight: float | None = Query(None, gt=0, le=1, description="Cap per instrument (fraction)."),
    isins: str | None = Query(None, description="Comma-separated candidate ISINs; defaults to allocator_universe_isins."),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """How to split the next contribution across a candidate ETF universe.

    Targets are covariance-only (Ledoit-Wolf, EUR returns); the money goes to
    the most underweight candidates first and nothing is sold. Candidates not
    held yet count with a value of 0.
    """
    from app.foundation.allocation import candidate_prices, history_span, new_money_plan
    from app.foundation.live_positions import combined_positions
    from app.foundation.portfolio.isin_resolver import _try_resolve_isin
    from app.foundation.quant import price_matrix_to_returns
    from app.foundation.settings import get_public_settings

    s = get_public_settings(db)
    amount = float(contribution_eur if contribution_eur is not None else (s.get("monthly_contribution_eur") or 0))

    def _split(value: Any) -> list[str]:
        items = value.replace(";", ",").split(",") if isinstance(value, str) else list(value or [])
        return [str(i).strip().upper() for i in items if str(i).strip()]

    held = {p.isin.upper(): p for p in combined_positions(db, user.id, include_unreconciled=True) if p.isin}
    chosen = _split(isins) or _split(s.get("allocator_universe_isins"))
    default_universe = not chosen
    if default_universe:
        # MSCI World + EM IMI: building blocks that do not overlap. FTSE All-World is held but
        # already contains both, so it is not a default target.
        chosen = ["IE00B4L5Y983", "IE00BKM4GZ66"]
    chosen = list(dict.fromkeys(chosen))

    universe: list[dict[str, Any]] = []
    ticker_of: dict[str, str] = {}
    for isin in chosen:
        pos = held.get(isin)
        ticker = (pos.ticker if pos and pos.ticker else None) or _try_resolve_isin(isin, db)
        ticker = ticker.upper() if ticker else None
        universe.append({
            "isin": isin,
            "ticker": ticker,
            "name": pos.name if pos else None,
            "held_value_eur": float(pos.value) if pos else 0.0,
        })
        if ticker:
            ticker_of[isin] = ticker
    tickers = list(dict.fromkeys(ticker_of.values()))
    base: dict[str, Any] = {
        "universe": universe,
        "universe_is_default": default_universe,
        "unresolved": [u["isin"] for u in universe if not u["ticker"]],
    }
    if any(i in ALL_WORLD_ISINS for i in chosen):
        base["overlap_note"] = (
            "All-World funds already contain World + Emerging Markets, but a covariance-only optimiser "
            "treats them as separate assets and can double-count the same shares."
        )
    prices, missing = candidate_prices(db, tickers)
    base["missing_history"] = missing
    if not prices:
        return {**base, "available": False, "reason": "No price history for any candidate yet."}
    span = history_span(prices)
    base["history"] = span
    if span["days"] < 60:
        return {**base, "available": False, "reason": f"Only {span['days']} shared trading days; at least 60 are needed."}
    returns = price_matrix_to_returns(prices).tail(504)
    values: dict[str, float] = {}
    for u in universe:
        if u["ticker"] in prices:
            values[u["ticker"]] = values.get(u["ticker"], 0.0) + u["held_value_eur"]
    plan = new_money_plan(returns, values, amount, max_weight=max_weight)
    return {**base, **plan}


@router.get("/portfolio/factors")
def portfolio_factors(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    # Regress the portfolio against real ETF-proxy factors (compute_factor_model),
    # NOT against its own return series. The previous {"market": returns} regressed
    # the portfolio on itself, which is a tautology that always reports beta=1,
    # R^2=1 and tells the user nothing.
    result = compute_factor_model(db, user.id)
    diagnostics = result.pop("diagnostics", {})
    available = result.get("status") == "completed"
    return {
        "available": available,
        "exposures": result,
        "diagnostics": diagnostics,
    }


# The single-asset scenario endpoints (POST /portfolio/scenarios and the saved
# scenarios) were removed on 2026-10-04 with the old Monte Carlo tab; the
# projection below replaces them.


# ---------------------------------------------------------------------------
# Real holdings endpoints (every synced broker)
# ---------------------------------------------------------------------------


@router.get("/portfolio/real/holdings")
def real_holdings(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    from app.foundation.portfolio.bridge import get_real_holdings_summary
    return get_real_holdings_summary(db, user.id)


# POST /portfolio/real/snapshot and /portfolio/real/resolve-tickers were
# removed on 2026-10-04: every DKB and Scalable sync and the daily 20:00 job
# already snapshot the book and resolve tickers, and nothing read the manual
# snapshot's per-position rows.


@router.get("/portfolio/real/performance")
def real_portfolio_performance(
    benchmark: str | None = Query(None, description="Defaults to the configured benchmark (MSCI World in EUR)."),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Time-weighted unit value of the real book (every broker, EUR), its IRR and a rebased benchmark."""
    from app.foundation.book_performance import compute_book_performance

    return compute_book_performance(db, user.id, benchmark=benchmark)


@router.get("/portfolio/projection")
def portfolio_projection(
    years: int = Query(30, ge=1, le=60),
    goal_eur: float | None = Query(None, gt=0, description="Goal in today's euros; defaults to the setting."),
    contribution_eur: float | None = Query(None, ge=0, description="Monthly, in today's euros; defaults to the plan."),
    real_return: float | None = Query(None, ge=-0.1, le=0.2, description="Geometric real return a year; defaults to the CMA setting."),
    volatility: float | None = Query(None, gt=0, le=1.0),
    start_value_eur: float | None = Query(None, ge=0, description="Defaults to the synced book's value."),
    paths: int = Query(10_000, ge=1_000, le=50_000),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Monte Carlo projection of the book in real EUR (foundation/wealth_planner.py)."""
    from app.foundation.live_positions import live_positions
    from app.foundation.settings import get_public_settings
    from app.foundation.wealth_planner import (
        DEFAULT_CMA_AS_OF,
        DEFAULT_CMA_REAL_RETURN,
        DEFAULT_CMA_SOURCE,
        PlanInputs,
        project,
    )

    settings = get_public_settings(db)
    start = (
        float(sum((p.value for p in live_positions(db, user.id)), start=0))
        if start_value_eur is None else start_value_eur
    )
    vol_source = "input"
    if volatility is None:
        volatility, vol_source = book_volatility(db, user.id)
    goal = goal_eur if goal_eur is not None else (float(settings["mc_goal_eur"]) if settings.get("mc_goal_eur") else None)
    inputs = PlanInputs(
        start_value=start,
        monthly_contribution=(
            float(settings.get("monthly_contribution_eur") or 0) if contribution_eur is None else contribution_eur
        ),
        years=years,
        real_return=(
            float(settings.get("mc_cma_real_return", DEFAULT_CMA_REAL_RETURN)) if real_return is None else real_return
        ),
        volatility=volatility,
        goal=goal,
        paths=paths,
    )
    result = project(inputs)
    result["assumptions"] = {
        "real_return_source": settings.get("mc_cma_source") or DEFAULT_CMA_SOURCE,
        "real_return_as_of": settings.get("mc_cma_as_of") or DEFAULT_CMA_AS_OF,
        "real_return_overridden": real_return is not None,
        "volatility_source": vol_source,
        "fat_tails": f"Student-t, {inputs.nu:g} degrees of freedom",
    }
    return result


@router.get("/portfolio/real/summary", response_model=RealPortfolioMetricsResponse, response_model_exclude_unset=True)
def real_portfolio_summary(
    benchmark: str | None = Query(None, description="Defaults to the configured benchmark (MSCI World in EUR)."),
    lookback_days: int = Query(365, ge=30, le=1825),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.portfolio.metrics_wrappers import compute_real_holdings_metrics
    return compute_real_holdings_metrics(db, user.id, benchmark=benchmark, lookback_days=lookback_days)


@router.get("/portfolio/real/risk", response_model=RealPortfolioRiskResponse, response_model_exclude_unset=True)
def real_portfolio_risk(
    benchmark: str | None = Query(None, description="Defaults to the configured benchmark (MSCI World in EUR)."),
    lookback_days: int = Query(365, ge=30, le=1825),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    from app.foundation.portfolio.metrics_wrappers import compute_factor_exposures_real, compute_real_holdings_metrics

    metrics_result = compute_real_holdings_metrics(db, user.id, benchmark=benchmark, lookback_days=lookback_days)
    factors_result = compute_factor_exposures_real(db, user.id, lookback_days=lookback_days)
    return {
        "available": metrics_result.get("available", False),
        "metrics": metrics_result.get("metrics", {}),
        "max_drawdown": metrics_result.get("max_drawdown", {}),
        "factor_exposures": factors_result.get("exposures", {}),
        "factor_r_squared": factors_result.get("r_squared", 0.0),
        "diagnostics": {
            "metrics_diagnostics": metrics_result.get("diagnostics", {}),
            "factor_diagnostics": factors_result.get("diagnostics", {}),
        },
    }


@router.get("/portfolio/real/rebalance")
def real_portfolio_rebalance(
    lookback_days: int = Query(365, ge=30, le=1825),
    target: str = Query("sleeves", description="sleeves (your plan, default), or a covariance target: equal, inverse_vol, min_variance, erc, hrp"),
    contribution_eur: float | None = Query(None, ge=0, description="Defaults to the monthly contribution."),
    band_pp: float | None = Query(None, ge=0, le=50, description="Defaults to the plan's drift band."),
    max_weight: float | None = Query(None, gt=0, le=1),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Band rebalancing toward the plan's sleeves (default) or a covariance-only target: new money first, sales only on a breach."""
    from app.decision.monthly_plan import sleeve_plan
    from app.foundation.portfolio.metrics_wrappers import generate_rebalancing_suggestions
    return generate_rebalancing_suggestions(
        db, user.id, lookback_days=lookback_days, target=target, contribution_eur=contribution_eur,
        band_pp=band_pp, max_weight=max_weight, sleeve_plan=sleeve_plan(db, user.id) if target == "sleeves" else None,
    )
