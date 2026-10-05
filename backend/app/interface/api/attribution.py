"""Attribution analysis API endpoints (Phase 3)."""

from datetime import date, datetime
from typing import Any, cast
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.foundation.core.db import get_db
from app.foundation.auth import current_user
from app.interface.envelope import three_artifacts
from app.lab.attribution import (
    brinson_fachler,
    factor_attribution,
    top_contributors,
    store_attribution_run,
)
from app.foundation.models.entities import AttributionRun, Portfolio, Holding, User
from app.foundation.data_backbone.bars import BarStore
from app.foundation.data_backbone.listing_currency import resolve_currency
from app.foundation.market import usd_per_unit_by_date
from app.foundation.quant_factors import restate_in_usd

router = APIRouter(prefix="/api/attribution", tags=["attribution"])


def _date_indexed_returns(bars: pd.DataFrame) -> pd.Series:
    """Compute a return series indexed by calendar date (not list position).

    ``bars`` is expected to be sorted by ``ts`` ascending (as returned by
    ``BarStore.get_bars``). Returns are aligned to the bar's calendar date
    so downstream code can join/align multiple series by actual trading
    date instead of positional index.
    """
    dates = pd.to_datetime(bars["ts"]).dt.normalize()
    closes = pd.Series(bars["close"].to_numpy(), index=dates)
    return closes.pct_change().dropna()


def _usd_indexed_returns(db: Session, symbol: str, bars: pd.DataFrame) -> pd.Series:
    """:func:`_date_indexed_returns` restated in USD for the Fama-French regression.

    Ken French's factors are USD returns; each holding (and the benchmark) is
    converted from the currency its prices are quoted in, so the regression
    does not read the FX move as a style tilt (ADR 0007 amendment 2). That is
    the listing's resolved currency (``data_backbone.listing_currency``), not
    ``Holding.currency`` (DKB books a Nasdaq share in EUR) and not the bars'
    stored label, which read "EUR" for AAPL until the labels were audited.
    Without a rate the series is returned unconverted.
    """
    series = _date_indexed_returns(bars)
    if series.empty:
        return series
    ccy = resolve_currency(db, symbol)
    if ccy == "USD":
        return series
    first = pd.to_datetime(bars["ts"]).dt.normalize().iloc[0]
    days = (pd.Timestamp.now().normalize() - first).days + 30
    rates = usd_per_unit_by_date(db, ccy, days=max(days, 60), allow_live=False)
    if not rates:
        return series
    values, dates = restate_in_usd(
        series.tolist(), [d.strftime("%Y-%m-%d") for d in pd.DatetimeIndex(series.index)], rates,
        first_start=str(first.date()),
    )
    return pd.Series(values, index=pd.to_datetime(dates))


def _resolve_portfolio(db: Session, user_id: str, portfolio_id: str) -> Portfolio | None:
    """Resolve a portfolio for *user_id*.

    A blank id or the sentinel ``"main"``/``"default"`` selects the user's
    primary (oldest) portfolio, so the UI does not need to know the UUID.
    """
    q = db.query(Portfolio).filter(Portfolio.user_id == user_id)
    if portfolio_id and portfolio_id.lower() not in ("main", "default"):
        return q.filter(Portfolio.id == portfolio_id).first()
    return q.order_by(Portfolio.__table__.c.created_at.asc()).first()


class BrinsonRequest(BaseModel):
    """Request for Brinson-Fachler attribution."""
    portfolio_id: str
    benchmark_ticker: str
    date_from: date
    date_to: date


class FactorAttributionRequest(BaseModel):
    """Request for factor-based attribution."""
    portfolio_id: str
    benchmark_ticker: str
    date_from: date
    date_to: date
    factors: list[str] = ["mkt_rf", "smb", "hml"]


@router.post("/brinson")
def run_brinson_attribution(
    request: BrinsonRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    Run Brinson-Fachler attribution analysis.

    Decomposes active return into allocation and selection effects.
    """
    # Resolve portfolio (supports "main"/blank → user's primary portfolio)
    portfolio = _resolve_portfolio(db, user.id, request.portfolio_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    # Get portfolio holdings
    holdings = db.query(Holding).filter_by(portfolio_id=portfolio.id).all()
    if not holdings:
        raise HTTPException(status_code=400, detail="Portfolio has no holdings")

    bar_store = BarStore(db)
    start_dt = datetime.combine(request.date_from, datetime.min.time())
    end_dt = datetime.combine(request.date_to, datetime.max.time())

    # Build portfolio holdings and fetch price-based returns per security
    portfolio_holdings = []
    portfolio_returns: dict[str, float] = {}

    for h in holdings:
        symbol = h.ticker or h.isin
        if not symbol:
            continue
        isin_key = (h.isin or h.ticker) or ""
        bars = bar_store.get_bars(symbol, start=start_dt, end=end_dt)
        if bars is not None and len(bars) >= 2:
            ret = float(bars["close"].iloc[-1] / bars["close"].iloc[0] - 1)
            # Use last close as proxy value
            value = float(bars["close"].iloc[-1]) * float(h.quantity)
        else:
            if h.avg_buy_price is None:
                continue
            ret = 0.0
            value = float(h.quantity) * float(h.avg_buy_price)
        portfolio_returns[isin_key] = ret
        portfolio_holdings.append(
            {"isin": isin_key, "ticker": h.ticker or "", "name": h.name, "value": value}
        )

    if not portfolio_holdings:
        raise HTTPException(
            status_code=422, detail="No price data available for portfolio holdings in the given date range"
        )

    # Benchmark: single-asset portfolio with weight=1.0 on the benchmark ticker
    bench_bars = bar_store.get_bars(request.benchmark_ticker, start=start_dt, end=end_dt)
    if bench_bars is None or len(bench_bars) < 2:
        raise HTTPException(
            status_code=501,
            detail=f"Benchmark data not available for {request.benchmark_ticker} in the given date range",
        )
    bench_ret = float(bench_bars["close"].iloc[-1] / bench_bars["close"].iloc[0] - 1)
    bench_value = float(bench_bars["close"].iloc[-1])
    benchmark_holdings = [
        {"isin": request.benchmark_ticker, "ticker": request.benchmark_ticker, "name": request.benchmark_ticker, "value": bench_value}
    ]
    benchmark_returns: dict[str, float] = {request.benchmark_ticker: bench_ret}

    # Run attribution
    result = brinson_fachler(portfolio_holdings, benchmark_holdings, portfolio_returns, benchmark_returns)

    # Store result
    attribution_data = {
        "allocation_effect": result.allocation_effect,
        "selection_effect": result.selection_effect,
        "interaction_effect": result.interaction_effect,
        "total_active_return": result.total_active_return,
        "securities": [
            {
                "isin": s.isin,
                "ticker": s.ticker,
                "name": s.name,
                "allocation_effect": s.allocation_effect,
                "selection_effect": s.selection_effect,
                "interaction_effect": s.interaction_effect,
                "total_effect": s.total_effect,
            }
            for s in result.securities[:20]  # Top 20
        ],
    }

    run = store_attribution_run(
        db,
        user_id=user.id,
        kind="brinson",
        portfolio_id=portfolio.id,
        benchmark=request.benchmark_ticker,
        date_from=request.date_from,
        date_to=request.date_to,
        result_dict=attribution_data,
    )
    db.commit()

    # Get top contributors
    top_pos = top_contributors(result.securities, n=5, direction="positive")
    top_neg = top_contributors(result.securities, n=5, direction="negative")

    return three_artifacts(
        research={
            "brinson": attribution_data,
            "top_contributors": [
                {
                    "isin": c.isin,
                    "ticker": c.ticker,
                    "total_contribution": c.total_contribution,
                    "allocation_effect": c.allocation_effect,
                    "selection_effect": c.selection_effect,
                }
                for c in top_pos + top_neg
            ],
        },
        attribution=attribution_data,
        ledger_ref=run.id,
    )


@router.post("/factor")
def run_factor_attribution(
    request: FactorAttributionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Run factor-based attribution analysis."""
    portfolio = _resolve_portfolio(db, user.id, request.portfolio_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    # Fetch real price data for portfolio and benchmark
    bar_store = BarStore(db)
    start_dt = datetime.combine(request.date_from, datetime.min.time())
    end_dt = datetime.combine(request.date_to, datetime.max.time())

    holdings = db.query(Holding).filter_by(portfolio_id=portfolio.id).all()
    if not holdings:
        raise HTTPException(status_code=400, detail="Portfolio has no holdings")

    # Build an equal-weight portfolio daily-return series by averaging per-holding
    # returns, aligned by actual calendar date (not list position) — different
    # holdings can have gaps, different first-trade dates, or missing bars, so
    # index-based alignment would silently mix non-contemporaneous returns.
    holding_return_series: dict[str, pd.Series] = {}
    for h in holdings:
        symbol = h.ticker or h.isin
        if not symbol:
            continue
        bars = bar_store.get_bars(symbol, start=start_dt, end=end_dt)
        if bars is not None and len(bars) >= 2:
            series = _usd_indexed_returns(db, symbol, bars)
            if not series.empty:
                holding_return_series[symbol] = series

    if not holding_return_series:
        raise HTTPException(
            status_code=422,
            detail="No price data available for portfolio holdings in the given date range",
        )

    # Inner-join all holdings on date: only dates where every holding has a
    # return are kept, then average across holdings for that date.
    holdings_df = pd.concat(holding_return_series.values(), axis=1, join="outer").dropna(
        how="any"
    )
    portfolio_series = cast(pd.Series, holdings_df.mean(axis=1))

    # Benchmark daily returns, also date-indexed
    bench_bars = bar_store.get_bars(request.benchmark_ticker, start=start_dt, end=end_dt)
    if bench_bars is None or len(bench_bars) < 2:
        raise HTTPException(
            status_code=501,
            detail=f"Benchmark data not available for {request.benchmark_ticker} in the given date range",
        )
    benchmark_series = _usd_indexed_returns(db, request.benchmark_ticker, bench_bars)

    # Align portfolio vs. benchmark by date (inner join) — only dates where
    # both the (fully-overlapping) portfolio series and the benchmark have
    # data are used for attribution.
    aligned = pd.concat(
        [portfolio_series.rename("portfolio"), benchmark_series.rename("benchmark")], axis=1
    ).dropna(how="any").sort_index()

    aligned_len = len(aligned)
    if aligned_len == 0:
        raise HTTPException(
            status_code=422,
            detail="Insufficient overlapping price data between portfolio and benchmark",
        )

    portfolio_returns = aligned["portfolio"].tolist()
    benchmark_returns = aligned["benchmark"].tolist()

    # Run attribution
    result = factor_attribution(
        portfolio_returns,
        benchmark_returns,
        date_from=str(request.date_from),
        date_to=str(request.date_to),
        factors=request.factors,
        dates=[str(d)[:10] for d in aligned.index],
    )

    attribution_data = {
        "factors": [
            {"name": f.factor_name, "exposure": f.exposure, "contribution": f.contribution}
            for f in result.factors
        ],
        "residual": result.residual,
        "r_squared": result.r_squared,
    }

    run = store_attribution_run(
        db,
        user_id=user.id,
        kind="factor",
        portfolio_id=portfolio.id,
        benchmark=request.benchmark_ticker,
        date_from=request.date_from,
        date_to=request.date_to,
        result_dict=attribution_data,
    )
    db.commit()

    return three_artifacts(
        research=attribution_data,
        attribution=attribution_data,
        ledger_ref=run.id,
    )


@router.get("/runs/{run_id}")
def get_attribution_run(
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get a specific attribution run."""
    run = db.query(AttributionRun).filter_by(id=run_id, user_id=user.id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Attribution run not found")

    import json

    result_data = json.loads(run.result_json) if run.result_json else {}

    return three_artifacts(
        research=result_data,
        attribution=result_data,
        ledger_ref=run.id,
    )


@router.get("/runs")
def list_attribution_runs(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """List attribution runs for user."""
    runs = (
        db.query(AttributionRun)
        .filter_by(user_id=user.id)
        .order_by(AttributionRun.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    import json

    return three_artifacts(
        research={
            "runs": [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "portfolio_id": r.portfolio_id,
                    "benchmark": r.benchmark,
                    "date_from": r.date_from.isoformat(),
                    "date_to": r.date_to.isoformat(),
                    "created_at": r.created_at.isoformat(),
                    "result": json.loads(r.result_json) if r.result_json else {},
                }
                for r in runs
            ]
        }
    )
