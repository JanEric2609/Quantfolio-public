"""Deterministic buy gates for LLM portfolio decisions.

All checks are synchronous and do NOT call any LLM.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.foundation.models.entities import Asset, PaperPortfolio
from app.foundation.data_backbone.ingest import DataIngester
from app.foundation.settings import get_public_settings

logger = logging.getLogger(__name__)


def check_buy_gates(
    db: Session,
    user_id: str,
    mandate_config: dict[str, Any],
    ticker: str,
    isin: str | None,
    quantity: Decimal,
    price: Decimal,
    mandate: str | None = None,
) -> dict[str, Any]:
    """Run deterministic buy gates.

    Args:
        db: Database session.
        user_id: User UUID.
        mandate_config: Parsed mandate config dict.
        ticker: Ticker symbol.
        isin: ISIN code (optional).
        quantity: Number of shares/units to buy.
        price: Expected price per unit.
        mandate: Mandate identifier ("A"/"B") used to select the correct
            paper portfolio for the cash-sufficiency check. When omitted the
            most recently created LLM-managed portfolio is used.

    Returns:
        GateResult dict with keys: passed (bool), checks (list[dict]), reason (str).
    """
    checks: list[dict[str, Any]] = []

    # 1. TRADEABILITY
    try:
        from app.decision.discover.tradeability import assess as assess_tradeability

        tradeability = assess_tradeability(db, ticker, isin, ticker)
        trade_pass = tradeability.get("likely_tradeable", True)
        checks.append({
            "name": "TRADEABILITY",
            "passed": trade_pass,
            "detail": tradeability,
        })
    except Exception as exc:
        logger.debug("Tradeability assess failed for %s: %s", ticker, exc)
        checks.append({
            "name": "TRADEABILITY",
            "passed": True,
            "detail": {"note": f"assess unavailable, default pass: {exc}"},
        })

    # 2. UCITS
    ucits_pass = True
    asset = None
    if isin:
        asset = db.query(Asset).filter(Asset.isin == isin).one_or_none()
        if asset and asset.asset_type == "etf":
            ucits_pass = bool(asset.ucits)
            checks.append({
                "name": "UCITS",
                "passed": ucits_pass,
                "detail": {"ucits": asset.ucits, "asset_type": asset.asset_type},
            })
        else:
            checks.append({
                "name": "UCITS",
                "passed": True,
                "detail": {"note": "Not an ETF or no asset record — skipped"},
            })
    else:
        checks.append({
            "name": "UCITS",
            "passed": True,
            "detail": {"note": "No ISIN provided — skipped"},
        })

    # 3. PROFILE COMPLIANCE
    settings = get_public_settings(db)
    exclusions_raw = settings.get("discover_investor_exclusions", "")
    exclusions = {e.strip().lower() for e in str(exclusions_raw).split(",") if e.strip()}
    risk_appetite = settings.get("discover_investor_risk_appetite", "medium")

    profile_pass = True
    profile_details: dict[str, Any] = {"risk_appetite": risk_appetite}

    ticker_lower = (ticker or "").lower()
    name_lower = (asset.name or "").lower() if asset else (ticker or "").lower()
    for ex in exclusions:
        if ex in ticker_lower or ex in name_lower:
            profile_pass = False
            profile_details["exclusion_match"] = ex
            break

    # Risk-band position limit
    max_single = mandate_config.get("max_single_position_pct", 0.20)
    if risk_appetite == "low":
        # Only ETFs allowed
        if asset and asset.asset_type != "etf":
            profile_pass = False
            profile_details["risk_violation"] = "low risk appetite allows ETFs only"
    elif risk_appetite == "medium":
        if max_single > 0.20:
            profile_details["note"] = "medium risk: max single position capped at 20%"
            max_single = 0.20
    elif risk_appetite == "high":
        if max_single > 0.30:
            profile_details["note"] = "high risk: max single position capped at 30%"
            max_single = 0.30

    profile_details["max_single_position_pct"] = max_single
    checks.append({
        "name": "PROFILE_COMPLIANCE",
        "passed": profile_pass,
        "detail": profile_details,
    })

    # 4. PRICED HISTORY
    history_pass = False
    bar_count = 0
    try:
        # SAVEPOINT so a missing table doesn't abort the outer Postgres
        # transaction and poison the remaining gate checks.
        with db.begin_nested():
            result = db.execute(
                text("SELECT count(*) FROM bar_prices WHERE symbol = :symbol"),
                {"symbol": ticker},
            )
            row = result.fetchone()
        bar_count = row[0] if row else 0
        history_pass = bar_count >= 252
    except Exception as exc:
        logger.debug("Bar count query failed for %s: %s", ticker, exc)
        history_pass = False

    if not history_pass:
        try:
            logger.info("PRICED_HISTORY backfill triggered for %s (had %d bars)", ticker, bar_count)
            ingester = DataIngester(db)
            ingest_result = ingester.ingest_bar_prices(ticker, days=365)
            if ingest_result.get("success"):
                result = db.execute(
                    text("SELECT count(*) FROM bar_prices WHERE symbol = :symbol"),
                    {"symbol": ticker},
                )
                row = result.fetchone()
                bar_count = row[0] if row else 0
                history_pass = bar_count >= 252
                logger.info(
                    "PRICED_HISTORY backfill completed for %s: %d bars now, pass=%s",
                    ticker, bar_count, history_pass,
                )
            else:
                logger.warning(
                    "PRICED_HISTORY backfill failed for %s: %s",
                    ticker, ingest_result.get("message"),
                )
        except Exception as exc:
            logger.warning("PRICED_HISTORY backfill error for %s: %s", ticker, exc)

    checks.append({
        "name": "PRICED_HISTORY",
        "passed": history_pass,
        "detail": {"bar_count": bar_count, "required": 252},
    })

    # 5. CASH SUFFICIENCY
    portfolio_query = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.user_id == user_id, PaperPortfolio.managed_by == "llm")
    )
    if mandate:
        portfolio_query = portfolio_query.filter(PaperPortfolio.mandate == mandate)
    portfolio = portfolio_query.order_by(PaperPortfolio.created_at.desc()).first()
    cash_pass = False
    cash_available = Decimal("0")
    needed = quantity * price
    if portfolio:
        # The same cash figure execute_trade checks (trades, fees and
        # dividends), plus the Scalable order fee this buy will pay.
        from app.foundation.broker_fees import scalable_order_fee
        from app.foundation.instrument_names import resolve_instrument_name
        from app.foundation.paper_cash import paper_cash_balance

        cash_available = paper_cash_balance(portfolio, db)
        name = resolve_instrument_name(db, ticker.upper(), isin)
        fee = Decimal(str(scalable_order_fee(float(needed), name)))
        needed = needed + fee
        cash_pass = cash_available >= needed
    checks.append({
        "name": "CASH_SUFFICIENCY",
        "passed": cash_pass,
        "detail": {
            "cash_available": str(cash_available),
            "needed": str(needed),
        },
    })

    overall_pass = all(c["passed"] for c in checks)
    reason = "All gates passed" if overall_pass else "; ".join(
        f"{c['name']} failed: {c['detail']}" for c in checks if not c["passed"]
    )

    return {
        "passed": overall_pass,
        "checks": checks,
        "reason": reason,
    }


# Backwards-compatible alias
GateResult = dict[str, Any]
