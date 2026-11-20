"""Price matrix and holdings summary for the synced broker positions.

Extracted from the original ``portfolio_bridge.py`` to separate the
query concern from the metrics and resolution utilities. The daily
snapshot writer (``snapshot_book_positions``) lives in
``app.foundation.portfolio_service``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.foundation.live_positions import live_positions, set_position_ticker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Price matrix from the real holdings at every broker
# ---------------------------------------------------------------------------


def build_real_price_matrix(
    db: Session, user_id: str, lookback_days: int = 365
) -> pd.DataFrame | None:
    """Build a date-indexed price matrix from the synced positions (DKB, Scalable).

    Resolves ISIN-only positions to tickers on-the-fly using the full
    resolution chain, then returns a ``DataFrame`` with dates as the index
    and ticker symbols as columns.  Single-asset portfolios are supported.
    """
    positions = live_positions(db, user_id)
    if not positions:
        return None

    # Resolve ISINs that still lack a ticker
    from app.foundation.portfolio.isin_resolver import _try_resolve_isin

    found: set[str] = set()
    resolved_any = False
    for pos in positions:
        ticker = pos.ticker
        if not ticker and pos.isin:
            ticker = _try_resolve_isin(pos.isin, db)
            if ticker:
                set_position_ticker(db, pos, ticker)
                resolved_any = True
        if ticker:
            found.add(ticker.upper())

    if resolved_any:
        db.commit()

    tickers = list(found)

    if not tickers:
        logger.info("No resolvable tickers for user %s", user_id)
        return None

    from app.foundation.eur_prices import eur_closes

    # Closes in EUR (see eur_prices): a USD line's currency return is part of
    # what an investor in EUR earns.
    frames: dict[str, pd.Series] = {}
    rate_cache: dict[str, dict[str, float] | None] = {}
    for ticker in tickers:
        try:
            closes = eur_closes(db, ticker, days=lookback_days, rate_cache=rate_cache)
        except Exception:
            logger.warning("Failed to fetch price history for %s", ticker, exc_info=True)
            continue

        if not closes:
            continue

        series = pd.Series(closes, name=ticker, dtype="float64")
        if not series.empty:
            frames[ticker] = series

    if not frames:
        logger.info("No usable price data for user %s", user_id)
        return None

    df = pd.DataFrame(frames)
    df.index = pd.to_datetime(df.index)
    df.sort_index(inplace=True)

    threshold = len(df) * 0.5
    df = df.dropna(axis=1, thresh=int(threshold))
    df = df.dropna()

    return df if not df.empty else None


# ---------------------------------------------------------------------------
# 2. Real holdings summary
# ---------------------------------------------------------------------------


def _empty_holdings_summary() -> dict[str, Any]:
    return {
        "total_value": 0.0,
        "positions": [],
        "by_ticker": {},
        "by_broker": {},
        "position_count": 0,
        "positions_with_ticker": 0,
        "isin_only_count": 0,
    }


def get_real_holdings_summary(db: Session, user_id: str) -> dict[str, Any]:
    """Return a summary of the user's synced holdings (every broker) for the QuantLab overview.

    One entry per depot in ``positions``; ``by_ticker`` sums the depots per
    ISIN (the name is historical) and lists them under ``depots``;
    ``by_broker`` totals each broker's depots.
    """
    positions = live_positions(db, user_id)
    if not positions:
        return _empty_holdings_summary()

    from app.foundation.portfolio.cost_basis import resolve_depot_costs

    depot_costs = resolve_depot_costs(db, user_id, positions)
    total_value = Decimal("0")
    position_dicts: list[dict[str, Any]] = []

    for pos in positions:
        cv = pos.value
        total_value += cv

        cost_basis: Decimal | None = None
        unrealized_pnl: Decimal | None = None
        dc = depot_costs.get(pos.id)
        if pos.avg_buy_price is None and dc is not None and dc.cost_eur is not None:
            # No broker average (DKB over FinTS): the Einstandswert entered by hand / FIFO lots.
            cost_basis = dc.cost_eur
            unrealized_pnl = cv - cost_basis
        elif pos.avg_buy_price is not None:
            cost_basis = pos.avg_buy_price * pos.quantity
            if pos.current_price is not None:
                unrealized_pnl = (pos.current_price - pos.avg_buy_price) * pos.quantity
            elif cv > 0:
                # Scalable reports a value but no price for some positions.
                unrealized_pnl = cv - cost_basis

        position_dicts.append(
            {
                "isin": pos.isin,
                "ticker": pos.ticker,
                "name": pos.name,
                "quantity": float(pos.quantity),
                "current_price": float(pos.current_price) if pos.current_price is not None else None,
                "current_value": float(cv),
                "weight": None,  # filled below
                "avg_buy_price": float(pos.avg_buy_price) if pos.avg_buy_price is not None else None,
                "cost_basis": float(cost_basis) if cost_basis is not None else None,
                "unrealized_pnl": float(unrealized_pnl) if unrealized_pnl is not None else None,
                "source": pos.source,
                "broker": pos.broker_label,
                "account_id": pos.account_id,
                "position_id": pos.id,
                "cost_source": (dc.source if dc and pos.avg_buy_price is None else None) or (
                    "broker" if pos.avg_buy_price is not None else None
                ),
                "can_enter_cost": pos.source == "dkb" and cost_basis is None,
            }
        )

    for p in position_dicts:
        p["weight"] = float(Decimal(str(p["current_value"])) / total_value) if total_value else 0.0

    by_ticker: dict[str, dict[str, Any]] = {}
    by_broker: dict[str, dict[str, Any]] = {}
    for p in position_dicts:
        depot = {
            "source": p["source"],
            "broker": p["broker"],
            "account_id": p["account_id"],
            "quantity": p["quantity"],
            "current_value": p["current_value"],
        }
        # Keyed by ISIN: a depot whose copy has no ticker yet (Scalable's,
        # before it is resolved) still joins the other depot's row.
        key = p["isin"] or p["ticker"]
        if key in by_ticker:
            by_ticker[key]["ticker"] = by_ticker[key]["ticker"] or p["ticker"]
            by_ticker[key]["quantity"] += p["quantity"]
            by_ticker[key]["current_value"] += p["current_value"]
            if p["unrealized_pnl"] is not None:
                by_ticker[key]["unrealized_pnl"] = (
                    (by_ticker[key]["unrealized_pnl"] or 0.0) + p["unrealized_pnl"]
                )
            by_ticker[key]["depots"].append(depot)
        else:
            by_ticker[key] = {
                "ticker": p["ticker"],
                "isin": p["isin"],
                "name": p["name"],
                "quantity": p["quantity"],
                "current_value": p["current_value"],
                "unrealized_pnl": p["unrealized_pnl"],
                "depots": [depot],
            }

        broker = by_broker.setdefault(
            p["source"],
            {
                "broker": p["broker"],
                "total_value": 0.0,
                "weight": 0.0,
                "position_count": 0,
                # Only positions with a known cost count towards the P&L, and
                # costed_value says how much of the depot that covers.
                "cost_basis": 0.0,
                "costed_value": 0.0,
                "unrealized_pnl": 0.0,
            },
        )
        broker["total_value"] += p["current_value"]
        broker["position_count"] += 1
        if p["cost_basis"] is not None and p["unrealized_pnl"] is not None:
            broker["cost_basis"] += p["cost_basis"]
            broker["costed_value"] += p["current_value"]
            broker["unrealized_pnl"] += p["unrealized_pnl"]

    for broker in by_broker.values():
        broker["weight"] = broker["total_value"] / float(total_value) if total_value else 0.0
        broker["unrealized_pnl_pct"] = (
            broker["unrealized_pnl"] / broker["cost_basis"] if broker["cost_basis"] > 0 else None
        )

    positions_with_ticker = sum(1 for p in positions if p.ticker)

    return {
        "total_value": float(total_value),
        "positions": position_dicts,
        "by_ticker": by_ticker,
        "by_broker": by_broker,
        "position_count": len(positions),
        "positions_with_ticker": positions_with_ticker,
        "isin_only_count": len(positions) - positions_with_ticker,
    }
