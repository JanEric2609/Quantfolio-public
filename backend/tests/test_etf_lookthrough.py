"""Tests for ETF look-through analysis."""
from decimal import Decimal
from unittest.mock import patch

from conftest import _memory_db

from app.foundation.models.entities import Holding, Portfolio, User
from app.foundation.etf_lookthrough import portfolio_lookthrough


def _make_user(db):
    user = User(username="testuser", password_hash="hash")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_portfolio(db, user_id):
    portfolio = Portfolio(user_id=user_id, name="Main", currency="EUR")
    db.add(portfolio)
    db.commit()
    db.refresh(portfolio)
    return portfolio


def _make_holding(db, portfolio_id, name, asset_type, quantity, avg_buy_price, ticker=None, isin=None):
    holding = Holding(
        portfolio_id=portfolio_id,
        name=name,
        asset_type=asset_type,
        quantity=Decimal(str(quantity)),
        avg_buy_price=Decimal(str(avg_buy_price)),
        ticker=ticker,
        isin=isin,
        currency="EUR",
    )
    db.add(holding)
    db.commit()
    db.refresh(holding)
    return holding


def _mock_etf_composition(ticker):
    from app.foundation.etf_lookup import EtfComposition, EtfHolding

    return EtfComposition(
        ticker=ticker,
        name="Mock ETF",
        holdings=[
            EtfHolding(ticker="AAPL", weight=0.04, name="Apple Inc."),
            EtfHolding(ticker="MSFT", weight=0.03, name="Microsoft Corp."),
            EtfHolding(ticker="NVDA", weight=0.025, name="NVIDIA Corp."),
            EtfHolding(ticker="AMZN", weight=0.02, name="Amazon.com Inc."),
            EtfHolding(ticker="META", weight=0.015, name="Meta Platforms Inc."),
        ],
        sectors={
            "Technology": 0.25,
            "Healthcare": 0.12,
            "Financial Services": 0.15,
        },
        regions={
            "North America": 0.55,
            "Europe": 0.20,
            "Asia Pacific": 0.20,
        },
    )


@patch("app.foundation.etf_lookthrough.get_etf_composition", side_effect=_mock_etf_composition)
def test_single_etf_effective_hhi_lower_than_simple(_mock):
    """One ETF holding expanded into constituents should have lower effective HHI."""
    db = _memory_db()
    user = _make_user(db)
    portfolio = _make_portfolio(db, user.id)
    _make_holding(db, portfolio.id, "VWCE", "etf", 10, 100.0, ticker="VWCE.DE", isin="IE00BK5BQT80")

    result = portfolio_lookthrough(db, user.id)

    assert result["has_lookthrough_data"] is True
    assert result["etf_holdings_count"] == 1
    assert result["direct_holdings_count"] == 0
    assert result["lookthrough_count"] == 5
    assert result["effective_hhi"] < 1.0
    assert result["effective_hhi"] > 0.0
    assert len(result["top_constituents"]) == 5
    assert result["sector_exposure"]["Technology"] > 0
    # Countries come from the tracked index (FTSE All-World, read through
    # MSCI ACWI), not from the composition's regions.
    assert "North America" not in result["region_exposure"]
    assert 0.6 < result["region_exposure"]["United States"] < 0.7
    assert abs(sum(result["region_exposure"].values()) - 1.0) < 0.01


@patch("app.foundation.etf_lookthrough.get_etf_composition", side_effect=_mock_etf_composition)
def test_etf_without_a_mapped_index_has_no_region_exposure(_mock):
    db = _memory_db()
    user = _make_user(db)
    portfolio = _make_portfolio(db, user.id)
    _make_holding(db, portfolio.id, "Some Theme ETF", "etf", 10, 100.0, ticker="THEME.DE", isin="IE0000000000")

    assert portfolio_lookthrough(db, user.id)["region_exposure"] == {}


def test_no_etf_holdings_returns_simple_count():
    """Portfolio with only stocks/direct holdings returns simple position count."""
    db = _memory_db()
    user = _make_user(db)
    portfolio = _make_portfolio(db, user.id)
    _make_holding(db, portfolio.id, "AAPL", "stock", 5, 150.0, ticker="AAPL")
    _make_holding(db, portfolio.id, "MSFT", "stock", 3, 200.0, ticker="MSFT")

    result = portfolio_lookthrough(db, user.id)

    assert result["has_lookthrough_data"] is False
    assert result["etf_holdings_count"] == 0
    assert result["direct_holdings_count"] == 2
    assert result["lookthrough_count"] == 2
    assert result["effective_hhi"] > 0.0
    assert result["effective_hhi"] < 1.0
    assert len(result["top_constituents"]) == 2


@patch("app.foundation.etf_lookthrough.get_etf_composition", side_effect=_mock_etf_composition)
def test_mixed_etf_and_stock_portfolio(_mock):
    """Mixed portfolio: ETF expanded + direct stock treated as single position."""
    db = _memory_db()
    user = _make_user(db)
    portfolio = _make_portfolio(db, user.id)
    _make_holding(db, portfolio.id, "VWCE", "etf", 10, 100.0, ticker="VWCE.DE")
    _make_holding(db, portfolio.id, "TSLA", "stock", 5, 150.0, ticker="TSLA")

    result = portfolio_lookthrough(db, user.id)

    assert result["has_lookthrough_data"] is True
    assert result["etf_holdings_count"] == 1
    assert result["direct_holdings_count"] == 1
    assert result["lookthrough_count"] == 6
    assert result["effective_hhi"] > 0.0
    assert result["effective_hhi"] < 1.0
    assert len(result["top_constituents"]) == 6
    assert any(c["ticker"] == "TSLA" for c in result["top_constituents"])
    assert any(c["ticker"] == "MSFT" for c in result["top_constituents"])
