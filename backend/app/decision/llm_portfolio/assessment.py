"""Deterministic portfolio assessment facts for the weekly mandate review.

Philosophy: *facts are computed in Python; the LLM only narrates them.* This
module never calls the LLM. It turns holdings + mandate config + snapshots into
a structured `assessment` dict that (a) is fed into the review prompt and (b) is
persisted verbatim on the decision so the UI can render objective facts instead
of trusting the model's prose.

Facts produced:
  * concentration  — largest position weight vs `max_single_position_pct`
  * etf_floor      — ETF weight vs `min_etf_pct`
  * cash           — cash weight (informational)
  * drawdown       — portfolio max drawdown (from the latest PaperSnapshot)
  * benchmark_excess — portfolio return minus EUNL.DE over the interval since the
                       previous review (best-effort; `unresolvable` if no data)

The benchmark is **EUNL.DE**, the house standard already used by
``services/recommendation_outcomes.py``. SPY is deliberately avoided — SPY-vs-EU
comparison is known-broken in this repo.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.foundation.live_positions import live_prices_by_isin
from app.foundation.models.entities import (
    LlmPortfolioDecision,
    PaperHolding,
    PaperPortfolio,
    PaperSnapshot,
)

logger = logging.getLogger(__name__)

BENCHMARK_TICKER = "EUNL.DE"


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_etf(asset_type: Optional[str]) -> bool:
    return (asset_type or "").strip().lower() == "etf"


def compute_assessment(
    *,
    positions: list[dict[str, Any]],
    cash_balance: Optional[float],
    max_drawdown: Optional[float],
    total_return_pct: Optional[float],
    benchmark_excess: Optional[dict[str, Any]],
    max_single_position_pct: float,
    min_etf_pct: float,
    as_of: Optional[datetime] = None,
) -> dict[str, Any]:
    """Compute deterministic assessment facts. Pure — no DB, no network.

    Args:
        positions: One dict per holding with keys ``isin``, ``ticker``, ``name``,
            ``asset_type``, ``quantity``, ``price`` (``price`` may be ``None``).
        cash_balance: Portfolio cash sleeve (``None`` if unknown).
        max_drawdown: Portfolio max drawdown as a fraction (e.g. ``-0.12``).
        total_return_pct: Since-inception return as a percentage number.
        benchmark_excess: Result of :func:`benchmark_excess_since` or ``None``.
        max_single_position_pct: Concentration cap as a fraction (A 0.15 / B 0.20).
        min_etf_pct: ETF floor as a fraction (A 0.40 / B 0.30).
        as_of: Timestamp stamped onto the result (defaults to now, UTC).

    Returns:
        A JSON-serialisable dict with ``positions``, ``checks`` and a
        ``summary_line`` suitable both for the prompt and for storage.
    """
    as_of = as_of or datetime.now(UTC)
    cash = _to_float(cash_balance) or 0.0

    priced: list[dict[str, Any]] = []
    unpriced: list[dict[str, Any]] = []
    for p in positions:
        qty = _to_float(p.get("quantity")) or 0.0
        price = _to_float(p.get("price"))
        entry = {
            "isin": p.get("isin"),
            "ticker": p.get("ticker"),
            "name": p.get("name"),
            "asset_type": p.get("asset_type"),
            "market_value": (qty * price) if price is not None else None,
        }
        if price is None:
            unpriced.append(entry)
        else:
            priced.append(entry)

    securities_value = sum(e["market_value"] for e in priced)
    total_value = securities_value + cash

    # Per-position weights against the full portfolio (securities + cash).
    weighted: list[dict[str, Any]] = []
    for e in priced:
        weight = (e["market_value"] / total_value) if total_value > 0 else 0.0
        weighted.append({**e, "weight_pct": round(weight * 100, 2)})
    weighted.sort(key=lambda e: e["market_value"], reverse=True)

    checks: list[dict[str, Any]] = []

    # 1. Concentration — largest position vs single-position cap.
    if total_value > 0 and weighted:
        breaches = [
            {"ticker": e["ticker"], "name": e["name"], "weight_pct": e["weight_pct"]}
            for e in weighted
            if e["market_value"] / total_value > max_single_position_pct
        ]
        top = weighted[0]
        checks.append({
            "key": "concentration",
            "label": "Single-position cap",
            "status": "breach" if breaches else "ok",
            "cap_pct": round(max_single_position_pct * 100, 2),
            "top_position": top["ticker"] or top["name"],
            "top_weight_pct": top["weight_pct"],
            "breaches": breaches,
        })
    else:
        checks.append({
            "key": "concentration",
            "label": "Single-position cap",
            "status": "unresolvable",
            "cap_pct": round(max_single_position_pct * 100, 2),
            "reason": "No priced positions",
        })

    # 2. ETF floor — aggregate ETF weight vs floor.
    if total_value > 0:
        etf_value = sum(e["market_value"] for e in priced if _is_etf(e["asset_type"]))
        etf_pct = etf_value / total_value
        checks.append({
            "key": "etf_floor",
            "label": "ETF floor",
            "status": "breach" if etf_pct < min_etf_pct else "ok",
            "floor_pct": round(min_etf_pct * 100, 2),
            "etf_pct": round(etf_pct * 100, 2),
        })
    else:
        checks.append({
            "key": "etf_floor",
            "label": "ETF floor",
            "status": "unresolvable",
            "floor_pct": round(min_etf_pct * 100, 2),
            "reason": "No priced positions",
        })

    # 3. Cash weight — informational.
    checks.append({
        "key": "cash",
        "label": "Cash weight",
        "status": "info",
        "value_pct": round((cash / total_value) * 100, 2) if total_value > 0 else None,
    })

    # 4. Max drawdown — informational (already a fraction on the snapshot).
    dd = _to_float(max_drawdown)
    checks.append({
        "key": "drawdown",
        "label": "Max drawdown",
        "status": "info" if dd is not None else "unresolvable",
        "value_pct": round(dd * 100, 2) if dd is not None else None,
    })

    # 5. Benchmark excess since last review — informational / best-effort.
    if benchmark_excess is None:
        checks.append({
            "key": "benchmark_excess",
            "label": f"Excess vs {BENCHMARK_TICKER} since last review",
            "status": "unresolvable",
            "reason": "No prior review or price data",
        })
    else:
        checks.append({
            "key": "benchmark_excess",
            "label": f"Excess vs {BENCHMARK_TICKER} since last review",
            "status": "info",
            **benchmark_excess,
        })

    return {
        "as_of": as_of.isoformat(),
        "benchmark_ticker": BENCHMARK_TICKER,
        "total_value": round(total_value, 2),
        "securities_value": round(securities_value, 2),
        "cash_balance": round(cash, 2),
        "total_return_pct": _to_float(total_return_pct),
        "positions": weighted,
        "unpriced_positions": unpriced,
        "checks": checks,
        "summary_line": _summary_line(checks),
    }


def _summary_line(checks: list[dict[str, Any]]) -> str:
    """One-line human summary of the checks, breaches first."""
    by_key = {c["key"]: c for c in checks}
    parts: list[str] = []

    conc = by_key.get("concentration", {})
    if conc.get("status") == "breach":
        parts.append(
            f"Concentration breach: {conc['top_position']} "
            f"{conc['top_weight_pct']}% > {conc['cap_pct']}% cap"
        )

    etf = by_key.get("etf_floor", {})
    if etf.get("status") == "breach":
        parts.append(f"ETF {etf['etf_pct']}% below {etf['floor_pct']}% floor")

    dd = by_key.get("drawdown", {})
    if dd.get("value_pct") is not None:
        parts.append(f"Max DD {dd['value_pct']}%")

    bx = by_key.get("benchmark_excess", {})
    if bx.get("status") == "info" and bx.get("excess_pct") is not None:
        sign = "+" if bx["excess_pct"] >= 0 else ""
        parts.append(f"{sign}{bx['excess_pct']}% vs {BENCHMARK_TICKER}")

    if not parts:
        return "No mandate breaches; portfolio within constraints."
    return " · ".join(parts)


def _fetch_interval_return(
    db: Session, ticker: str, start: datetime, end: datetime
) -> Optional[float]:
    """Simple return for a ticker over ``[start, end]`` (best-effort).

    Routed through ``market.history()`` (provider-chain fallback +
    PriceCache/bar_prices caching) rather than a raw ``yfinance`` call —
    ``[start, end]`` is always in the past, so enough lookback is fetched
    to cover it and the result is filtered to that window.
    """
    from app.foundation.market import history as market_history

    try:
        days = max((datetime.now(UTC) - start).days, 1) + 5  # small buffer
        rows = market_history(db, ticker, days=days, allow_live=True)
        start_date = start.date()
        end_date = end.date()
        closes: list[float] = []
        for row in rows:
            row_date = row.get("date")
            if isinstance(row_date, str):
                row_date = date.fromisoformat(row_date)
            close = row.get("close")
            if row_date is None or close is None:
                continue
            if start_date <= row_date <= end_date:
                closes.append(float(close))
        if len(closes) < 2:
            return None
        return float((closes[-1] / closes[0]) - 1.0)
    except Exception as exc:  # network / data errors are expected and tolerated
        logger.debug("Benchmark fetch failed for %s: %s", ticker, exc)
        return None


def benchmark_excess_since(
    db: Session,
    portfolio_id: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Portfolio return minus EUNL.DE over the interval since the previous review.

    Portfolio interval return is derived from the since-inception
    ``total_return_pct`` on the snapshot nearest each endpoint:
    ``(1 + r_end) / (1 + r_start) - 1``. Returns ``None`` when there is no prior
    review or insufficient snapshot / benchmark data — the caller renders that as
    ``unresolvable`` rather than a misleading zero.
    """
    now = now or datetime.now(UTC)

    prev_review = (
        db.query(LlmPortfolioDecision)
        .filter(
            LlmPortfolioDecision.portfolio_id == portfolio_id,
            LlmPortfolioDecision.status == "completed",
            LlmPortfolioDecision.review_date < now,
        )
        .order_by(LlmPortfolioDecision.review_date.desc())
        .first()
    )
    if prev_review is None or prev_review.review_date is None:
        return None

    start = prev_review.review_date

    start_snap = (
        db.query(PaperSnapshot)
        .filter(
            PaperSnapshot.portfolio_id == portfolio_id,
            PaperSnapshot.date <= start.date(),
        )
        .order_by(PaperSnapshot.date.desc())
        .first()
    )
    end_snap = (
        db.query(PaperSnapshot)
        .filter(PaperSnapshot.portfolio_id == portfolio_id)
        .order_by(PaperSnapshot.date.desc())
        .first()
    )
    if start_snap is None or end_snap is None or start_snap.date == end_snap.date:
        return None

    r_start = _to_float(start_snap.total_return_pct)
    r_end = _to_float(end_snap.total_return_pct)
    if r_start is None or r_end is None:
        return None

    # total_return_pct is stored as a percentage number (e.g. 4.5 == 4.5%).
    portfolio_interval = ((1 + r_end / 100) / (1 + r_start / 100)) - 1.0

    bench = _fetch_interval_return(db, BENCHMARK_TICKER, start, now)
    if bench is None:
        return None

    excess = portfolio_interval - bench
    return {
        "window_days": (end_snap.date - start_snap.date).days,
        "portfolio_return_pct": round(portfolio_interval * 100, 2),
        "benchmark_return_pct": round(bench * 100, 2),
        "excess_pct": round(excess * 100, 2),
    }


def _price_map_for_portfolio(db: Session, portfolio: PaperPortfolio) -> dict[str, Decimal]:
    """ISIN -> latest broker current_price (DKB, Scalable), mirroring context._build_positions_block."""
    return live_prices_by_isin(db, portfolio.user_id)


def build_assessment(
    db: Session,
    portfolio: PaperPortfolio,
    mandate_config: dict[str, Any],
) -> dict[str, Any]:
    """Gather DB inputs and return the deterministic assessment dict.

    Prices come from the latest broker ``current_price`` per ISIN; holdings without
    a broker price fall back to their ``avg_buy_price`` so a freshly seeded position
    still contributes a weight rather than vanishing from the denominator.
    """
    holdings = (
        db.query(PaperHolding)
        .filter(PaperHolding.portfolio_id == portfolio.id)
        .all()
    )
    price_map = _price_map_for_portfolio(db, portfolio)

    positions: list[dict[str, Any]] = []
    for h in holdings:
        price = price_map.get(h.isin or "")
        if price is None:
            price = h.avg_buy_price
        positions.append({
            "isin": h.isin,
            "ticker": h.ticker,
            "name": h.name,
            "asset_type": h.asset_type,
            "quantity": h.quantity,
            "price": price,
        })

    latest_snap = (
        db.query(PaperSnapshot)
        .filter(PaperSnapshot.portfolio_id == portfolio.id)
        .order_by(PaperSnapshot.date.desc())
        .first()
    )
    cash_balance = float(latest_snap.cash_balance) if latest_snap and latest_snap.cash_balance is not None else None
    max_drawdown = latest_snap.max_drawdown if latest_snap else None
    total_return_pct = float(latest_snap.total_return_pct) if latest_snap and latest_snap.total_return_pct is not None else None

    try:
        benchmark = benchmark_excess_since(db, portfolio.id)
    except Exception as exc:  # never let assessment abort the review
        logger.warning("Benchmark excess computation failed: %s", exc)
        benchmark = None

    return compute_assessment(
        positions=positions,
        cash_balance=cash_balance,
        max_drawdown=max_drawdown,
        total_return_pct=total_return_pct,
        benchmark_excess=benchmark,
        max_single_position_pct=float(mandate_config.get("max_single_position_pct", 0.20)),
        min_etf_pct=float(mandate_config.get("min_etf_pct", 0.30)),
    )
