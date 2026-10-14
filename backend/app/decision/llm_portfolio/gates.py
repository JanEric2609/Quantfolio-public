"""Deterministic buy and sell gates for LLM portfolio decisions.

All checks are synchronous and do NOT call any LLM.
"""
from __future__ import annotations

import logging
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.foundation.models.entities import Asset, PaperPortfolio
from app.foundation.data_backbone.ingest import DataIngester
from app.foundation.sell_caps import (
    MAX_CUMULATIVE_SELL_PCT,
    MAX_SELL_PCT_OF_POSITION,
    effective_sell_cap,
)
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


def check_sell_gates(
    *,
    ticker: str,
    quantity: Decimal,
    price: Decimal,
    book: dict[str, tuple[float, str | None]],
    cash: float,
    mandate_config: dict[str, Any],
    cumulative_totals: dict[str, tuple[float, float]],
) -> dict[str, Any]:
    """Run deterministic sell gates (plan 2026-10-12, D6). Pure: no DB.

    Mandate B sold 250 of 265 EUNL in one review to cure a cap breach that
    was not one. A sell is now:

    * rejected if it would push ETF weight below ``min_etf_pct``;
    * clipped to ``MAX_SELL_PCT_OF_POSITION`` of the position per review
      (ADR 0012) and to what the trailing-window cumulative cap leaves
      (ADR 0013).

    Args:
        ticker: Ticker to sell.
        quantity: Units the model asked to sell.
        price: EUR price per unit.
        book: ticker -> ``(EUR market value, asset_type)`` for every holding.
        cash: Cash balance in EUR.
        mandate_config: Parsed mandate config (``min_etf_pct``).
        cumulative_totals: ``sell_caps.cumulative_sell_totals`` for the portfolio.

    Returns:
        ``passed``, ``checks``, ``reason`` and ``quantity`` (the units that may
        be sold; less than requested when clipped).
    """
    checks: list[dict[str, Any]] = []
    sym = ticker.upper()
    current_value, asset_type = book.get(sym, (0.0, None))
    total_value = sum(v for v, _ in book.values()) + max(cash, 0.0)
    requested_value = float(quantity * price)

    # 1. Per-review and cumulative caps: clip, do not reject.
    cap_value = effective_sell_cap(
        sym,
        current_value,
        total_value,
        max_sell_pct_of_position=float(mandate_config.get("max_sell_pct_of_position", MAX_SELL_PCT_OF_POSITION)),
        cumulative_totals=cumulative_totals,
        max_cumulative_sell_pct=float(mandate_config.get("max_cumulative_sell_pct", MAX_CUMULATIVE_SELL_PCT)),
        instrument_types={},
        core_holding_floor_pct=0.0,
    )
    allowed_value = min(requested_value, cap_value)
    checks.append({
        "name": "SELL_RATE_CAP",
        "passed": allowed_value > 0,
        "detail": {
            "requested_value": round(requested_value, 2),
            "cap_value": round(cap_value, 2),
            "clipped": allowed_value < requested_value,
        },
    })

    # 2. ETF floor: a sell that leaves ETF weight under the floor is rejected.
    min_etf = float(mandate_config.get("min_etf_pct", 0.30))
    is_etf = (asset_type or "").lower() == "etf"
    if is_etf and total_value > 0:
        etf_value = sum(v for v, t in book.values() if (t or "").lower() == "etf")
        etf_after = (etf_value - allowed_value) / total_value
        floor_pass = etf_after >= min_etf - 1e-9
        checks.append({
            "name": "ETF_FLOOR",
            "passed": floor_pass,
            "detail": {"etf_pct_after": round(etf_after * 100, 2), "floor_pct": round(min_etf * 100, 2)},
        })

    passed = all(c["passed"] for c in checks)
    allowed_qty = Decimal("0")
    if passed and price > 0:
        allowed_qty = (Decimal(str(allowed_value)) / price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        allowed_qty = min(allowed_qty, quantity)
        if allowed_qty <= 0:
            passed = False
    if passed:
        reason = "All sell gates passed" if allowed_qty >= quantity else (
            f"Sell clipped from {quantity} to {allowed_qty} units by the sell-rate cap"
        )
    else:
        reason = "; ".join(f"{c['name']} failed: {c['detail']}" for c in checks if not c["passed"]) or (
            "Nothing may be sold this review"
        )
    return {"passed": passed, "checks": checks, "reason": reason, "quantity": allowed_qty}


# Backwards-compatible alias
GateResult = dict[str, Any]
