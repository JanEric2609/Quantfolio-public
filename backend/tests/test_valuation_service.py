"""Tests for the unified valuation service (proposal P2).

`services/valuation.py` is the single place quantity×price→converted-total is
computed. It consumes the P1 dated FX layer and is what
``portfolio_utils.holding_market_value`` delegates to.
"""
from datetime import date
from decimal import Decimal
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import FxRate, Holding, Portfolio, PriceCache, User
from app.foundation import valuation
from app.foundation.portfolio_utils import holding_market_value


def _make_user(db) -> User:
    user = User(id=uuid4().hex, username=f"user_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.flush()
    return user


def _make_portfolio(db, user_id: str) -> Portfolio:
    p = Portfolio(id=uuid4().hex, user_id=user_id, name="Test", currency="EUR")
    db.add(p)
    db.flush()
    return p


def _make_holding(db, portfolio_id, ticker: str | None = "AAPL", quantity=10, avg_buy_price: float | None = 150.0, currency="EUR"):
    h = Holding(
        id=uuid4().hex,
        portfolio_id=portfolio_id,
        ticker=ticker,
        name=f"{ticker or 'CASH'} Inc.",
        asset_type="stock",
        quantity=Decimal(str(quantity)),
        avg_buy_price=Decimal(str(avg_buy_price)) if avg_buy_price is not None else None,
        currency=currency,
    )
    db.add(h)
    db.flush()
    return h


def _make_price(db, ticker, close, d="2025-01-15", currency="EUR", stale=False, source="test"):
    pc = PriceCache(
        id=uuid4().hex,
        ticker=ticker,
        date=date.fromisoformat(d),
        close=Decimal(str(close)),
        currency=currency,
        source=source,
        stale=stale,
    )
    db.add(pc)
    db.flush()
    return pc


# ---------------------------------------------------------------------------
# value_holding
# ---------------------------------------------------------------------------

def test_value_holding_uses_latest_cached_price():
    db = _memory_db()
    user = _make_user(db)
    p = _make_portfolio(db, user.id)
    h = _make_holding(db, p.id, ticker="AAPL", quantity=10, avg_buy_price=150.0)
    _make_price(db, "AAPL", close=120.0, d="2025-01-01")
    _make_price(db, "AAPL", close=200.0, d="2025-06-01")

    v = valuation.value_holding(db, h)

    assert v.price_source == "market"
    assert v.price == 200.0
    assert v.converted_value == 2000.0
    assert v.source_currency == "EUR"
    assert v.reporting_ccy == "EUR"
    assert v.is_stale is False


def test_value_holding_converts_by_source_currency_via_dated_fx():
    db = _memory_db()
    user = _make_user(db)
    p = _make_portfolio(db, user.id)
    h = _make_holding(db, p.id, ticker="MSFT", quantity=10, avg_buy_price=100.0)
    _make_price(db, "MSFT", close=200.0, d="2025-01-15", currency="USD")
    # Dated USD->EUR rate on the price date.
    db.add(FxRate(base="USD", quote="EUR", date=date(2025, 1, 15), rate=Decimal("0.9")))
    db.commit()

    v = valuation.value_holding(db, h, as_of=date(2025, 1, 15))

    # 10 * 200 USD * 0.9 = 1800 EUR
    assert v.source_currency == "USD"
    assert v.converted_value == 1800.0


def test_value_holding_falls_back_to_cost_basis_without_price():
    db = _memory_db()
    user = _make_user(db)
    p = _make_portfolio(db, user.id)
    h = _make_holding(db, p.id, ticker="XYZ", quantity=8, avg_buy_price=50.0)

    v = valuation.value_holding(db, h)

    assert v.price_source == "cost_basis"
    assert v.converted_value == 400.0
    assert v.is_stale is True


def test_value_holding_unknown_cost_basis_does_not_raise():
    db = _memory_db()
    user = _make_user(db)
    p = _make_portfolio(db, user.id)
    h = _make_holding(db, p.id, ticker=None, quantity=5, avg_buy_price=None)

    v = valuation.value_holding(db, h)

    assert v.converted_value == 0.0
    assert v.price_source == "unknown_cost"
    assert v.is_stale is True


def test_value_holding_flags_staleness_from_price_cache():
    db = _memory_db()
    user = _make_user(db)
    p = _make_portfolio(db, user.id)
    h = _make_holding(db, p.id, ticker="AAPL", quantity=1, avg_buy_price=1.0)
    _make_price(db, "AAPL", close=99.0, stale=True)

    v = valuation.value_holding(db, h)

    assert v.price_source == "market"
    assert v.is_stale is True


# ---------------------------------------------------------------------------
# value_portfolio
# ---------------------------------------------------------------------------

def test_value_portfolio_folds_holdings_into_converted_total():
    db = _memory_db()
    user = _make_user(db)
    p = _make_portfolio(db, user.id)
    h1 = _make_holding(db, p.id, ticker="AAPL", quantity=10, avg_buy_price=1.0)
    h2 = _make_holding(db, p.id, ticker="MSFT", quantity=5, avg_buy_price=1.0)
    _make_price(db, "AAPL", close=100.0)  # 1000
    _make_price(db, "MSFT", close=200.0)  # 1000

    vp = valuation.value_portfolio(db, [h1, h2])

    assert vp.total_value == 2000.0
    assert vp.reporting_ccy == "EUR"
    assert len(vp.holdings) == 2


# ---------------------------------------------------------------------------
# regression: holding_market_value delegates and returns the same EUR number
# ---------------------------------------------------------------------------

def test_holding_market_value_matches_value_holding():
    db = _memory_db()
    user = _make_user(db)
    p = _make_portfolio(db, user.id)
    h = _make_holding(db, p.id, ticker="AAPL", quantity=10, avg_buy_price=150.0)
    _make_price(db, "AAPL", close=200.0)

    assert holding_market_value(db, h) == 2000.0
    assert holding_market_value(db, h) == valuation.value_holding(db, h).converted_value


def test_holding_market_value_cost_basis_regression():
    db = _memory_db()
    user = _make_user(db)
    p = _make_portfolio(db, user.id)
    h = _make_holding(db, p.id, ticker="XYZ", quantity=10, avg_buy_price=200.0)

    assert holding_market_value(db, h) == 2000.0


def test_value_holding_reads_a_pence_quote_as_hundredths_of_a_pound():
    """SHEL.L is quoted in pence ("GBp"); upper-casing the unit valued
    100 shares at 3,611p as GBP 361,100."""
    db = _memory_db()
    user = _make_user(db)
    p = _make_portfolio(db, user.id)
    h = _make_holding(db, p.id, ticker="SHEL.L", quantity=100, avg_buy_price=30.0)
    _make_price(db, "SHEL.L", close=3611.0, d="2025-01-15", currency="GBp")
    db.add(FxRate(base="GBP", quote="EUR", date=date(2025, 1, 15), rate=Decimal("1.2")))
    db.commit()

    v = valuation.value_holding(db, h, as_of=date(2025, 1, 15))

    assert v.source_currency == "GBp"
    assert v.converted_value == 100 * 36.11 * 1.2


def test_value_holding_prefers_the_audited_listing_currency_over_the_cached_label():
    """History rows were cached as "EUR" whatever the listing; once the
    listing's currency is recorded, that is what the price is in."""
    from app.foundation.data_backbone.listing_currency import record_listing_currency

    db = _memory_db()
    user = _make_user(db)
    p = _make_portfolio(db, user.id)
    h = _make_holding(db, p.id, ticker="IWDA.L", quantity=10, avg_buy_price=100.0)
    _make_price(db, "IWDA.L", close=146.91, d="2025-01-15", currency="EUR")
    record_listing_currency(db, "IWDA.L", "USD", "yfinance_metadata")
    db.add(FxRate(base="USD", quote="EUR", date=date(2025, 1, 15), rate=Decimal("0.9")))
    db.commit()

    v = valuation.value_holding(db, h, as_of=date(2025, 1, 15))

    assert v.source_currency == "USD"
    assert abs(v.converted_value - 10 * 146.91 * 0.9) < 1e-9
