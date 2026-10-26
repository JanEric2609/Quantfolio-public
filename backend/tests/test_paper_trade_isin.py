"""A mandate buy of a new name carries its ISIN (prod check 2026-10-26: NEM had none)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from conftest import _memory_db

from app.decision.llm_portfolio import review
from app.decision.paper_portfolio import execute_trade
from app.foundation.models.entities import PaperHolding, PaperPortfolio, PaperTrade, User
from app.foundation.security_master import valid_isin

NEM = "US6516391066"
#: The real lookup, taken before conftest's autouse stub replaces it.
_REAL_YFINANCE_ISIN = review._yfinance_isin


def _portfolio(db) -> PaperPortfolio:
    user = User(username="isin", password_hash="x")
    db.add(user)
    db.commit()
    p = PaperPortfolio(
        user_id=user.id, name="B", initial_cash=Decimal("1000"), baseline_value=Decimal("1000"), mandate="B",
        managed_by="llm", inception_at=datetime.now(UTC) - timedelta(days=30),
    )
    db.add(p)
    db.commit()
    return p


def test_a_new_holding_and_its_trade_carry_the_isin():
    db = _memory_db()
    p = _portfolio(db)
    booked = execute_trade(db, p.id, "NEM", "buy", 1, 100.0, isin=NEM)
    db.commit()
    assert db.get(PaperTrade, booked["id"]).isin == NEM
    assert db.query(PaperHolding).filter_by(portfolio_id=p.id, ticker="NEM").one().isin == NEM


def test_a_holding_without_an_isin_is_backfilled_on_the_next_buy():
    db = _memory_db()
    p = _portfolio(db)
    execute_trade(db, p.id, "NEM", "buy", 1, 100.0)
    booked = execute_trade(db, p.id, "NEM", "buy", 1, 100.0, isin=NEM)
    db.commit()
    assert db.get(PaperTrade, booked["id"]).isin == NEM


def test_resolver_falls_back_to_a_checked_yfinance_isin():
    db = _memory_db()
    with patch.object(review, "_yfinance_isin", return_value=NEM):
        assert review._resolve_isin(db, "NEM") == NEM


def test_yfinance_isin_rejects_a_bad_check_digit():
    with patch("yfinance.Ticker", return_value=MagicMock(isin=NEM)):
        assert _REAL_YFINANCE_ISIN("NEM") == NEM
    for bad in ("US6516391067", "-"):
        with patch("yfinance.Ticker", return_value=MagicMock(isin=bad)):
            assert _REAL_YFINANCE_ISIN("NEM") is None
    assert valid_isin("IE00B4L5Y983") and valid_isin("US23804L1035")
