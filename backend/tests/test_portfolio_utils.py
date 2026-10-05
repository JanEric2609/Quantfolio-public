"""Tests for portfolio_utils.holding_market_value."""
from decimal import Decimal
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import Holding, Portfolio, PriceCache, User
from app.foundation.portfolio_utils import holding_market_value


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_user(db) -> User:
    user = User(id=uuid4().hex, username=f"user_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.flush()
    return user


def _make_portfolio(db, user_id: str) -> Portfolio:
    portfolio = Portfolio(id=uuid4().hex, user_id=user_id, name="Test", currency="EUR")
    db.add(portfolio)
    db.flush()
    return portfolio


def _make_holding(db, portfolio_id: str, ticker: str = "AAPL", quantity: float = 10, avg_buy_price: float = 150.0) -> Holding:
    holding = Holding(
        id=uuid4().hex,
        portfolio_id=portfolio_id,
        ticker=ticker,
        name=f"{ticker} Inc.",
        asset_type="stock",
        quantity=Decimal(str(quantity)),
        avg_buy_price=Decimal(str(avg_buy_price)),
        currency="EUR",
    )
    db.add(holding)
    db.flush()
    return holding


def _make_price_cache(db, ticker: str, close: float, date_str: str = "2025-01-15") -> PriceCache:
    from datetime import date as date_type
    d = date_type.fromisoformat(date_str)
    pc = PriceCache(
        id=uuid4().hex,
        ticker=ticker,
        date=d,
        close=Decimal(str(close)),
        source="test",
        stale=False,
    )
    db.add(pc)
    db.flush()
    return pc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHoldingMarketValue:
    def test_falls_back_to_cost_basis_without_price_cache(self):
        db = _memory_db()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        holding = _make_holding(db, portfolio.id, ticker="XYZ", quantity=10, avg_buy_price=200.0)

        result = holding_market_value(db, holding)
        # No PriceCache entry → cost basis = 10 * 200 = 2000
        assert result == 2000.0

    def test_uses_market_price_when_price_cache_exists(self):
        db = _memory_db()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        holding = _make_holding(db, portfolio.id, ticker="AAPL", quantity=10, avg_buy_price=150.0)
        _make_price_cache(db, "AAPL", close=200.0)

        result = holding_market_value(db, holding)
        # Market price = 200, so 10 * 200 = 2000 (not 10 * 150 = 1500)
        assert result == 2000.0

    def test_uses_latest_price_when_multiple_cache_entries(self):
        db = _memory_db()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        holding = _make_holding(db, portfolio.id, ticker="MSFT", quantity=5, avg_buy_price=100.0)
        _make_price_cache(db, "MSFT", close=120.0, date_str="2025-01-01")
        _make_price_cache(db, "MSFT", close=150.0, date_str="2025-06-01")

        result = holding_market_value(db, holding)
        # Latest price = 150, so 5 * 150 = 750
        assert result == 750.0

    def test_case_insensitive_ticker_lookup(self):
        db = _memory_db()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        holding = _make_holding(db, portfolio.id, ticker="aapl", quantity=2, avg_buy_price=100.0)
        _make_price_cache(db, "AAPL", close=300.0)

        result = holding_market_value(db, holding)
        # Ticker stored lowercase, PriceCache has uppercase → should match
        assert result == 600.0

    def test_no_ticker_falls_back_to_cost_basis(self):
        db = _memory_db()
        user = _make_user(db)
        portfolio = _make_portfolio(db, user.id)
        holding = _make_holding(db, portfolio.id, ticker=None, quantity=8, avg_buy_price=50.0)  # type: ignore[arg-type]

        result = holding_market_value(db, holding)
        # No ticker → cost basis = 8 * 50 = 400
        assert result == 400.0
