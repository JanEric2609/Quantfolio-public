"""Tests for PortfolioPriceService."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pandas as pd
import pytest
from conftest import _memory_db

from app.foundation.models.entities import DkbAccount, DkbPosition, Holding, Portfolio, User
from app.foundation.portfolio_price_service import DEFAULT_FACTOR_PROXIES, PortfolioPriceService


@pytest.fixture(autouse=True)
def _prices_already_in_eur():
    """FX is covered in test_eur_prices; here every close is taken as EUR."""
    with patch(
        "app.foundation.portfolio_price_service.to_eur",
        side_effect=lambda db, symbol, closes, **kw: (dict(closes), "EUR"),
    ):
        yield


def _make_user(db) -> User:
    user = User(
        id=str(uuid4()),
        username=f"user_{uuid4().hex[:8]}",
        password_hash="hashed",
    )
    db.add(user)
    db.flush()
    return user


def _make_portfolio(db, user_id: str, name: str = "Test Portfolio") -> Portfolio:
    portfolio = Portfolio(id=str(uuid4()), user_id=user_id, name=name)
    db.add(portfolio)
    db.flush()
    return portfolio


def _make_holding(
    db,
    portfolio_id: str,
    ticker: str,
    quantity: float = 10.0,
    avg_buy_price: float | None = 100.0,
    currency: str = "EUR",
) -> Holding:
    holding = Holding(
        id=str(uuid4()),
        portfolio_id=portfolio_id,
        ticker=ticker,
        name=ticker,
        asset_type="stock",
        quantity=Decimal(str(quantity)),
        avg_buy_price=Decimal(str(avg_buy_price)) if avg_buy_price is not None else None,
        currency=currency,
    )
    db.add(holding)
    db.flush()
    return holding


def _make_dkb_account(db, user_id: str) -> DkbAccount:
    account = DkbAccount(
        id=str(uuid4()),
        user_id=user_id,
        type="depot",
        iban=f"DE893704004405320130{uuid4().hex[:2]}",
        currency="EUR",
    )
    db.add(account)
    db.flush()
    return account


def _make_dkb_position(
    db,
    account_id: str,
    ticker: str,
    isin: str = "US1234567890",
    quantity: float = 5.0,
    current_value: float = 500.0,
) -> DkbPosition:
    position = DkbPosition(
        id=str(uuid4()),
        account_id=account_id,
        isin=isin,
        ticker=ticker,
        name=ticker,
        quantity=Decimal(str(quantity)),
        current_value=Decimal(str(current_value)),
    )
    db.add(position)
    db.flush()
    return position


def _bars(ticker: str, days: int = 5, start_price: float = 100.0, growth: float = 0.01):
    """Generate deterministic mock price bars."""
    today = date.today()
    return [
        {
            "date": today - timedelta(days=days - 1 - i),
            "close": start_price * ((1 + growth) ** i),
        }
        for i in range(days)
    ]


def _history_side_effect(bars_by_ticker: dict[str, list[dict]]):
    def _side_effect(db, ticker: str, days: int = 730):
        return bars_by_ticker.get(ticker, [])

    return _side_effect


def test_empty_portfolio_returns_empty_matrix_and_diagnostics():
    db = _memory_db()
    user = _make_user(db)
    svc = PortfolioPriceService(db, user.id)

    with patch("app.foundation.portfolio_price_service.market_service") as mock_market:
        mock_market.history.return_value = []
        matrix, weights, diagnostics = svc.price_matrix()

    assert matrix == {}
    assert weights == {}
    assert diagnostics["priced_assets"] == []
    assert diagnostics["missing_history"] == []
    assert diagnostics["manual_ticker_holdings"] == 0
    assert diagnostics["dkb_position_count"] == 0
    assert diagnostics["partial_data"] is False


def test_single_manual_holding_builds_matrix_and_weights():
    db = _memory_db()
    user = _make_user(db)
    portfolio = _make_portfolio(db, user.id)
    _make_holding(db, portfolio.id, ticker="AAPL", quantity=10, avg_buy_price=150)

    bars = _bars("AAPL", days=10, start_price=100.0)
    svc = PortfolioPriceService(db, user.id)

    with patch("app.foundation.portfolio_price_service.market_service") as mock_market:
        mock_market.history.side_effect = _history_side_effect({"AAPL": bars})
        matrix, weights, diagnostics = svc.price_matrix()

    assert set(matrix.keys()) == {"AAPL"}
    assert len(matrix["AAPL"]) == len(bars)
    # Market value at the latest close, not cost.
    assert weights["AAPL"] == pytest.approx(10 * bars[-1]["close"])
    assert diagnostics["priced_assets"] == ["AAPL"]
    assert diagnostics["missing_history"] == []
    assert diagnostics["manual_ticker_holdings"] == 1


def test_multiple_holdings_to_frame_has_correct_shape():
    db = _memory_db()
    user = _make_user(db)
    portfolio = _make_portfolio(db, user.id)
    _make_holding(db, portfolio.id, ticker="AAPL")
    _make_holding(db, portfolio.id, ticker="MSFT")

    today = date.today()
    shared_dates = [today - timedelta(days=2 - i) for i in range(3)]
    bars = {
        "AAPL": [{"date": d, "close": 100.0 + i} for i, d in enumerate(shared_dates)],
        "MSFT": [{"date": d, "close": 200.0 + i} for i, d in enumerate(shared_dates)],
    }
    svc = PortfolioPriceService(db, user.id)

    with patch("app.foundation.portfolio_price_service.market_service") as mock_market:
        mock_market.history.side_effect = _history_side_effect(bars)
        matrix, _, _ = svc.price_matrix()

    frame = svc.to_frame(matrix)

    assert isinstance(frame, pd.DataFrame)
    assert list(frame.columns) == ["AAPL", "MSFT"]
    assert len(frame) == 3
    assert frame.index.is_monotonic_increasing


def test_weighted_returns_normalizes_weights():
    today = date.today()
    shared_dates = [today - timedelta(days=2 - i) for i in range(3)]
    matrix = {
        "AAPL": {
            shared_dates[0].isoformat(): 100.0,
            shared_dates[1].isoformat(): 110.0,
            shared_dates[2].isoformat(): 121.0,
        },
        "MSFT": {
            shared_dates[0].isoformat(): 200.0,
            shared_dates[1].isoformat(): 200.0,
            shared_dates[2].isoformat(): 220.0,
        },
    }
    weights = {"AAPL": 1000.0, "MSFT": 2000.0}
    svc = PortfolioPriceService(_memory_db(), str(uuid4()))

    returns = svc.weighted_returns(matrix, weights)

    assert isinstance(returns, pd.Series)
    assert len(returns) == 2
    expected_first = (1 / 3) * 0.10 + (2 / 3) * 0.0
    expected_second = (1 / 3) * 0.10 + (2 / 3) * 0.10
    assert returns.iloc[0] == pytest.approx(expected_first)
    assert returns.iloc[1] == pytest.approx(expected_second)


def test_to_frame_drops_gap_dates_without_forward_filling():
    """A mid-series price gap in one ticker must exclude that date entirely,
    not forward-fill it into a spurious 0% return (non-synchronous/stale-
    trading bias). Regression test for the ffill-before-pct_change bug that
    also affected weighted_returns() and factor_proxy_returns()."""
    matrix = {
        "AAPL": {
            "2026-01-01": 100.0,
            "2026-01-02": 101.0,
            "2026-01-03": 103.0,
            "2026-01-04": 104.0,
        },
        "MSFT": {
            "2026-01-01": 200.0,
            "2026-01-02": 202.0,
            # 2026-01-03 intentionally missing: a mid-series gap for MSFT only.
            "2026-01-04": 206.0,
        },
    }
    svc = PortfolioPriceService(_memory_db(), str(uuid4()))

    frame = svc.to_frame(matrix)

    assert "2026-01-03" not in [str(idx) for idx in frame.index]
    assert len(frame) == 3

    weights = {"AAPL": 1.0, "MSFT": 1.0}
    returns = svc.weighted_returns(matrix, weights)
    assert "2026-01-03" not in [str(idx) for idx in returns.index]
    assert len(returns) == 2


def test_returns_list_static_returns_tuple():
    today = date.today()
    shared_dates = [today - timedelta(days=2 - i) for i in range(3)]
    matrix = {
        "AAPL": {
            shared_dates[0].isoformat(): 100.0,
            shared_dates[1].isoformat(): 110.0,
            shared_dates[2].isoformat(): 121.0,
        },
        "MSFT": {
            shared_dates[0].isoformat(): 200.0,
            shared_dates[1].isoformat(): 200.0,
            shared_dates[2].isoformat(): 220.0,
        },
    }
    weights = {"AAPL": 1000.0, "MSFT": 2000.0}

    series, dates = PortfolioPriceService.returns_list_static(matrix, weights)

    assert isinstance(series, list)
    assert isinstance(dates, list)
    assert len(series) == 2
    assert len(dates) == 2
    assert dates[0] == shared_dates[1].isoformat()
    assert dates[1] == shared_dates[2].isoformat()
    assert series[0] == pytest.approx((1 / 3) * 0.10 + (2 / 3) * 0.0)
    assert series[1] == pytest.approx((1 / 3) * 0.10 + (2 / 3) * 0.10)


def test_missing_history_recorded_in_diagnostics():
    db = _memory_db()
    user = _make_user(db)
    portfolio = _make_portfolio(db, user.id)
    _make_holding(db, portfolio.id, ticker="NOHIST")
    svc = PortfolioPriceService(db, user.id)

    with patch("app.foundation.portfolio_price_service.market_service") as mock_market:
        mock_market.history.return_value = []
        matrix, weights, diagnostics = svc.price_matrix()

    assert matrix == {}
    assert weights == {}
    assert diagnostics["missing_history"] == ["NOHIST"]
    assert diagnostics["partial_data"] is False
    assert "NOHIST" not in diagnostics["priced_assets"]


def test_holding_with_unknown_avg_buy_price_is_valued_at_its_latest_close():
    """A manual Holding with avg_buy_price=None (unknown cost basis) still has
    a market value; risk needs that, not the cost."""
    db = _memory_db()
    user = _make_user(db)
    portfolio = _make_portfolio(db, user.id)
    _make_holding(db, portfolio.id, ticker="UNKNOWNCOST", quantity=10, avg_buy_price=None)
    bars = _bars("UNKNOWNCOST", days=5)
    svc = PortfolioPriceService(db, user.id)

    with patch("app.foundation.portfolio_price_service.market_service") as mock_market:
        mock_market.history.side_effect = _history_side_effect({"UNKNOWNCOST": bars})
        matrix, weights, diagnostics = svc.price_matrix()

    assert weights["UNKNOWNCOST"] == pytest.approx(10 * bars[-1]["close"])
    assert "UNKNOWNCOST" in matrix
    assert diagnostics["missing_history"] == []


def test_holding_without_history_is_flagged_missing():
    db = _memory_db()
    user = _make_user(db)
    portfolio = _make_portfolio(db, user.id)
    _make_holding(db, portfolio.id, ticker="NOHIST", quantity=10, avg_buy_price=None)
    svc = PortfolioPriceService(db, user.id)

    with patch("app.foundation.portfolio_price_service.market_service") as mock_market:
        mock_market.history.return_value = []
        matrix, weights, diagnostics = svc.price_matrix()

    assert "NOHIST" not in weights and "NOHIST" not in matrix
    assert diagnostics["missing_history"] == ["NOHIST"]


def test_dkb_position_included_in_matrix_and_weights():
    db = _memory_db()
    user = _make_user(db)
    account = _make_dkb_account(db, user.id)
    _make_dkb_position(db, account.id, ticker="TSLA", current_value=1200.0)

    bars = _bars("TSLA", days=5)
    svc = PortfolioPriceService(db, user.id)

    with patch("app.foundation.portfolio_price_service.market_service") as mock_market:
        mock_market.history.side_effect = _history_side_effect({"TSLA": bars})
        matrix, weights, diagnostics = svc.price_matrix()

    assert set(matrix.keys()) == {"TSLA"}
    assert weights["TSLA"] == 1200.0
    assert diagnostics["dkb_position_count"] == 1
    assert diagnostics["priced_assets"] == ["TSLA"]


def test_default_factor_proxies_have_expected_tickers():
    svc = PortfolioPriceService(_memory_db(), str(uuid4()))

    proxies = svc._get_factor_proxies()

    assert proxies == DEFAULT_FACTOR_PROXIES
    assert proxies["market"] == "EUNL.DE"
    assert proxies["size"] == "IUSN.DE"
    assert proxies["value"] == "IWVL.L"
    assert proxies["momentum"] == "IS3R.DE"


def test_factor_proxy_prices_and_returns_use_configured_proxies():
    db = _memory_db()
    user = _make_user(db)
    custom_proxies = {"market": "VWCE.DE", "size": "SLYC"}
    svc = PortfolioPriceService(db, user.id, factor_proxies=custom_proxies)

    today = date.today()
    bars = {
        "VWCE.DE": [{"date": today - timedelta(days=2 - i), "close": 100.0 + i} for i in range(3)],
        "SLYC": [{"date": today - timedelta(days=2 - i), "close": 50.0 + i} for i in range(3)],
    }

    with patch("app.foundation.portfolio_price_service.market_service") as mock_market:
        mock_market.history.side_effect = _history_side_effect(bars)
        prices = svc.factor_proxy_prices()
        returns = svc.factor_proxy_returns()

    assert set(prices.keys()) == {"market", "size"}
    assert set(returns.columns) == {"market", "size"}
    assert len(returns) == 2


def test_returns_list_delegates_to_static_method():
    today = date.today()
    shared_dates = [today - timedelta(days=2 - i) for i in range(3)]
    matrix = {
        "AAPL": {
            shared_dates[0].isoformat(): 100.0,
            shared_dates[1].isoformat(): 110.0,
            shared_dates[2].isoformat(): 121.0,
        },
    }
    weights = {"AAPL": 1000.0}
    svc = PortfolioPriceService(_memory_db(), str(uuid4()))

    series, dates = svc.returns_list(matrix, weights)

    assert len(series) == 2
    assert series[0] == pytest.approx(0.10)
    assert series[1] == pytest.approx(0.10)


def test_returns_list_static_keeps_single_day_returns_across_a_foreign_holiday():
    # B's market is shut on 2026-01-02; A trades every day. The 01-05 portfolio
    # return must be a one-day return, not a two-day one.
    a = {"2026-01-01": 100.0, "2026-01-02": 110.0, "2026-01-05": 121.0}
    b = {"2026-01-01": 50.0, "2026-01-05": 55.0}
    series, dates = PortfolioPriceService.returns_list_static({"A": a, "B": b}, {"A": 1.0, "B": 1.0})
    assert dates == ["2026-01-02", "2026-01-05"]
    assert series[0] == pytest.approx(0.5 * 0.10)  # B carried flat
    assert series[1] == pytest.approx(0.5 * 0.10 + 0.5 * 0.10)
