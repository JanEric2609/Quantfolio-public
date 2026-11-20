from datetime import date, timedelta
from unittest.mock import patch

from conftest import _memory_db

from app.foundation.models.entities import DkbAccount, DkbPosition, Goal, Holding, Portfolio, PriceCache, User


def test_isin_resolution_via_etf_universe():
    """ISIN-only DKB position resolves to ticker via EtfUniverseProvider."""
    db = _memory_db()

    user = User(username="alice", password_hash="x", role="user")
    db.add(user)
    db.commit()

    account = DkbAccount(user_id=user.id, type="depot", iban="DE1234567890", balance=0)
    db.add(account)
    db.commit()

    pos = DkbPosition(
        account_id=account.id,
        isin="IE00B4L5Y983",
        name="iShares Core MSCI World",
        quantity=10,
        avg_buy_price=50,
        current_price=55,
        current_value=550,
    )
    db.add(pos)
    db.commit()

    from app.foundation.portfolio.isin_resolver import resolve_isin_to_ticker

    with patch(
        "app.foundation.portfolio.isin_resolver._try_resolve_isin"
    ) as mock_resolve:
        mock_resolve.return_value = "IWDA.AS"
        result = resolve_isin_to_ticker(db, user.id)

    assert result["resolved"] == 1
    assert result["unresolved"] == 0
    db.refresh(pos)
    assert pos.ticker == "IWDA.AS"


def test_single_asset_mc_returns_valid_fan_chart():
    """Monte Carlo endpoint works with a single-asset portfolio."""
    db = _memory_db()

    user = User(username="bob", password_hash="x", role="user")
    db.add(user)
    db.commit()

    portfolio = Portfolio(user_id=user.id, name="Main")
    db.add(portfolio)
    db.commit()

    holding = Holding(
        portfolio_id=portfolio.id,
        name="World ETF",
        ticker="IWDA.AS",
        asset_type="etf",
        quantity=10,
        avg_buy_price=50,
        currency="EUR",
    )
    db.add(holding)
    db.commit()

    # Seed price cache so _portfolio_price_matrix finds data
    base_date = date.today() - timedelta(days=35)
    for i in range(30):
        db.add(
            PriceCache(
                ticker="IWDA.AS",
                date=base_date + timedelta(days=i),
                close=50.0 + i * 0.1,
                source="yfinance",
            )
        )
    db.commit()

    from app.interface.api.quant._common import _portfolio_price_matrix

    matrix, weights = _portfolio_price_matrix(db, user.id)
    assert matrix is not None
    assert "IWDA.AS" in matrix

    from app.foundation.quant_mc.projection import gbm_fan_projection

    mc = gbm_fan_projection(
        start_value=500,
        annual_return=0.07,
        annual_volatility=0.15,
        years=1,
        simulations=100,
    )
    assert "fan_data" in mc
    assert len(mc["fan_data"]["median"]) > 0
    assert mc["median_terminal"] > 0


def test_goal_tied_mc_compares_against_trajectory():
    """Goal-tied MC endpoint returns fan chart and trajectory comparison."""
    db = _memory_db()

    user = User(username="carol", password_hash="x", role="user")
    db.add(user)
    db.commit()

    portfolio = Portfolio(user_id=user.id, name="Main")
    db.add(portfolio)
    db.commit()

    holding = Holding(
        portfolio_id=portfolio.id,
        name="World ETF",
        ticker="IWDA.AS",
        asset_type="etf",
        quantity=10,
        avg_buy_price=50,
        currency="EUR",
    )
    db.add(holding)
    db.commit()

    base_date = date.today() - timedelta(days=35)
    for i in range(30):
        db.add(
            PriceCache(
                ticker="IWDA.AS",
                date=base_date + timedelta(days=i),
                close=50.0 + i * 0.1,
                source="yfinance",
            )
        )
    db.commit()

    goal = Goal(
        user_id=user.id,
        title="Retirement",
        target_amount=100000,
        target_date=date.today() + timedelta(days=365 * 10),
        progress=5000,
        monthly_contribution=500,
    )
    db.add(goal)
    db.commit()

    from fastapi import HTTPException

    from app.interface.api.quant import goal_monte_carlo

    try:
        result = goal_monte_carlo(goal_id=goal.id, db=db, user=user)
    except HTTPException as exc:
        # Should not raise 404 since goal exists
        raise AssertionError(f"Unexpected HTTPException: {exc.detail}") from exc

    assert result["status"] == "completed"
    assert isinstance(result["fan_chart"], list)
    assert len(result["fan_chart"]) > 0
    assert isinstance(result["goal_trajectory"], list)
    assert len(result["goal_trajectory"]) > 0
    assert "on_track" in result
    assert isinstance(result["on_track"], bool)
    assert "required_return" in result
    assert isinstance(result["median_path"], list)
    assert len(result["median_path"]) > 0
    assert result["assumptions"]["years"] == 10
