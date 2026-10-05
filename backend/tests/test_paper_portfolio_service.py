"""Tests for the paper portfolio service: trade accounting and cash balance."""
from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import _memory_db

from app.foundation.models.entities import DiscoverCandidate, DiscoverRun, PaperHolding, PaperPortfolio, PaperTrade, User
from app.decision.paper_portfolio import (
    _current_cash_balance,
    execute_trade,
    get_or_create_paper_portfolio,
    get_summary,
    recompute_baseline_value,
    resolve_paper_asset_type,
)


def _user(db) -> User:
    user = User(username="alice", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_get_or_create_returns_tuple_with_fallback_cash():
    db = _memory_db()
    user = _user(db)
    portfolio, seeded = get_or_create_paper_portfolio(db, user.id)
    assert isinstance(portfolio, PaperPortfolio)
    assert seeded is False  # no DKB sync
    assert portfolio.initial_cash == Decimal("100000")
    assert portfolio.mandate == "manual"


def test_buy_then_sell_updates_cash_and_holdings():
    db = _memory_db()
    user = _user(db)
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)

    buy = execute_trade(db, portfolio.id, ticker="aapl", side="buy", quantity=10, price=100.0)
    assert buy["value"] == 1000.0
    assert _current_cash_balance(portfolio, db) == Decimal("99000")

    sell = execute_trade(db, portfolio.id, ticker="AAPL", side="sell", quantity=4, price=110.0)
    assert sell["value"] == 440.0
    # 100000 - 1000 (buy) + 440 (sell)
    assert _current_cash_balance(portfolio, db) == Decimal("99440")

    # Holding reduced to 6 units, ticker normalised to upper-case
    trades = db.query(PaperTrade).filter(PaperTrade.portfolio_id == portfolio.id).all()
    assert {t.ticker for t in trades} == {"AAPL"}


def test_resolve_paper_asset_type_defaults_to_stock_when_unknown():
    db = _memory_db()
    assert resolve_paper_asset_type(db, "UNKNOWNTICKER") == "stock"


def test_resolve_paper_asset_type_detects_money_market_from_discover_candidate():
    """Prod incident (2026-08-20): new PaperHolding rows hardcoded
    asset_type='stock' regardless of what was actually bought — XEON.DE
    paper positions were stored as 'stock'. execute_trade should now
    classify a money-market ETF correctly using the Discover candidate's
    own name, the way discover_candidates.name is actually populated."""
    db = _memory_db()
    user = _user(db)
    run = DiscoverRun(user_id=user.id, status="completed")
    db.add(run)
    db.commit()
    db.add(DiscoverCandidate(
        run_id=run.id,
        symbol="XEON.DE",
        name="Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C",
        source="screen_etf",
    ))
    db.commit()

    assert resolve_paper_asset_type(db, "XEON.DE") == "money_market"

    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    execute_trade(db, portfolio.id, ticker="XEON.DE", side="buy", quantity=10, price=150.0)
    holding = (
        db.query(PaperHolding)
        .filter(PaperHolding.portfolio_id == portfolio.id, PaperHolding.ticker == "XEON.DE")
        .one()
    )
    assert holding.asset_type == "money_market"


def test_buy_insufficient_cash_raises():
    db = _memory_db()
    user = _user(db)
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    with pytest.raises(ValueError, match="Insufficient cash"):
        execute_trade(db, portfolio.id, ticker="AAPL", side="buy", quantity=10, price=999999.0)


def test_sell_more_than_held_raises():
    db = _memory_db()
    user = _user(db)
    portfolio, _ = get_or_create_paper_portfolio(db, user.id)
    execute_trade(db, portfolio.id, ticker="AAPL", side="buy", quantity=2, price=100.0)
    with pytest.raises(ValueError, match="Insufficient holdings"):
        execute_trade(db, portfolio.id, ticker="AAPL", side="sell", quantity=5, price=100.0)


def _seed_portfolio_with_securities(db, user, *, cash: str, qty: str, price: str) -> PaperPortfolio:
    portfolio = PaperPortfolio(
        user_id=user.id,
        name="Manual Baseline",
        initial_cash=Decimal(cash),
        mandate="manual",
        managed_by="manual",
    )
    db.add(portfolio)
    db.commit()
    db.refresh(portfolio)
    db.add(
        PaperHolding(
            portfolio_id=portfolio.id,
            isin="IE00B4L5Y983",
            ticker="EUNL.DE",
            name="iShares Core MSCI World",
            quantity=Decimal(qty),
            avg_buy_price=Decimal(price),
            asset_type="etf",
        )
    )
    db.commit()
    return portfolio


def test_baseline_value_prevents_phantom_return(monkeypatch):
    """A DKB-seeded portfolio (small cash sleeve + securities) must not report a
    phantom ~1543% return. The basis is initial_cash + cost basis of holdings."""
    db = _memory_db()
    user = _user(db)
    portfolio = _seed_portfolio_with_securities(db, user, cash="2228.67", qty="100", price="343.98")

    baseline = recompute_baseline_value(db, portfolio.id)
    assert baseline == Decimal("36626.67")  # cash + 100 * 343.98, NOT just cash

    # Market price == cost -> ~0% return (regression: old code gave ~15.43 == 1543%).
    monkeypatch.setattr("app.decision.paper_portfolio.market_quote", lambda db, ticker: {"price": 343.98})
    summary = get_summary(db, portfolio.id)
    assert summary["baseline_value"] == 36626.67
    assert abs(summary["total_return_pct"]) < 0.001


def test_total_return_pct_reflects_real_gain(monkeypatch):
    db = _memory_db()
    user = _user(db)
    portfolio = _seed_portfolio_with_securities(db, user, cash="1000", qty="10", price="100")
    recompute_baseline_value(db, portfolio.id)  # baseline = 1000 + 1000 = 2000

    # +10% price -> securities 1100, total 2100, basis 2000 -> +5% total return (fraction).
    monkeypatch.setattr("app.decision.paper_portfolio.market_quote", lambda db, ticker: {"price": 110.0})
    summary = get_summary(db, portfolio.id)
    assert abs(summary["total_return_pct"] - 0.05) < 1e-6
