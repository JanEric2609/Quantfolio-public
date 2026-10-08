"""Cash of a paper portfolio: the one function every cash check uses."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.foundation.models.entities import PaperCashFlow, PaperPortfolio, PaperTrade

# A price whose bar is older than this many trading days is flagged stale:
# the holding is still valued at it, but no paper order is ever filled at it.
# The one threshold for valuation flags, the advisor cycle and the mandates.
MAX_QUOTE_AGE_TRADING_DAYS = 3


def paper_cash_balance(portfolio: PaperPortfolio, db: Session) -> Decimal:
    """Initial cash - buys + sells - fees + cash flows (net dividends).

    Commissions leave the sleeve on both sides, so they are subtracted once
    regardless of side rather than being folded into ``value`` (which stays the
    clean price * quantity notional the scorecard and attribution rely on).
    """
    rows = (
        db.query(PaperTrade.side, PaperTrade.value, PaperTrade.fee)
        .filter(PaperTrade.portfolio_id == portfolio.id)
        .all()
    )
    total_buys = sum((r[1] for r in rows if r[0] == "buy"), Decimal("0"))
    total_sells = sum((r[1] for r in rows if r[0] == "sell"), Decimal("0"))
    total_fees = sum((r[2] or Decimal("0") for r in rows), Decimal("0"))
    flows = db.query(PaperCashFlow.amount_eur).filter(PaperCashFlow.portfolio_id == portfolio.id).all()
    flows_total = sum((Decimal(r[0] or 0) for r in flows), Decimal("0"))
    return portfolio.initial_cash - total_buys + total_sells - total_fees + flows_total
