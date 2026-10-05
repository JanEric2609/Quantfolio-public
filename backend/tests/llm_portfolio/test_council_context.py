"""Tests for build_council_context — the bridge from quant_proposal (MC/skfolio)
to the Multi-Agent Council's 18-key context dict.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import PaperHolding, PaperPortfolio, PriceCache, User
from app.decision.llm_portfolio.council_context import build_council_context

_REQUIRED_KEYS = {
    "portfolio_id", "holdings", "cash", "market_data", "performance",
    "discover_items", "var_95", "cvar_95", "max_drawdown", "correlation_matrix",
    "concentration", "risk_free_rate", "macro_indicators", "sector_performance",
    "hmm_regime", "risk_budget", "current_state", "debate_feedback", "strategy",
}


def _user(db) -> User:
    user = User(id=uuid4().hex, username=f"u_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _portfolio(db, user: User) -> PaperPortfolio:
    p = PaperPortfolio(
        id=uuid4().hex, user_id=user.id, name="Test", mandate="A",
        initial_cash=Decimal("10000"),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _seed_prices(db, ticker: str, closes: list[float]) -> None:
    from app.foundation.models.entities import ListingCurrency

    # A ticker without a suffix reads as a USD listing; these quote in EUR.
    if db.get(ListingCurrency, ticker.upper()) is None:
        db.add(ListingCurrency(symbol=ticker.upper(), currency="EUR", source="test"))
    days: list[date] = []
    day = date.today()
    while len(days) < len(closes):
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    now = datetime.now(UTC)
    for dd, close in zip(days, closes):
        db.add(
            PriceCache(
                id=uuid4().hex, ticker=ticker.upper(), date=dd,
                close=Decimal(str(round(close, 4))), fetched_at=now,
                source="test", stale=False, currency="EUR",
            )
        )
    db.commit()


def _wiggly(base: float, n: int = 300, drift: float = 0.0004) -> list[float]:
    price = base
    out = []
    for i in range(n):
        price *= 1 + drift + (0.01 if i % 7 == 0 else -0.005 if i % 5 == 0 else 0.0)
        out.append(round(price, 4))
    return out


def test_build_council_context_has_all_18_keys_with_holdings_and_prices():
    db = _memory_db()
    user = _user(db)
    portfolio = _portfolio(db, user)
    _seed_prices(db, "AAA", _wiggly(100.0))
    _seed_prices(db, "BBB", _wiggly(50.0))
    db.add(PaperHolding(
        id=uuid4().hex, portfolio_id=portfolio.id, ticker="AAA", isin="DE000AAA111",
        name="Alpha", asset_type="stock", quantity=Decimal("10"), avg_buy_price=Decimal("90"),
    ))
    db.add(PaperHolding(
        id=uuid4().hex, portfolio_id=portfolio.id, ticker="BBB", isin="DE000BBB111",
        name="Beta", asset_type="stock", quantity=Decimal("5"), avg_buy_price=Decimal("45"),
    ))
    db.commit()

    context = build_council_context(db, portfolio, strategy_prompt="Balanced Improver")

    assert set(context.keys()) == _REQUIRED_KEYS
    assert context["portfolio_id"] == portfolio.id
    assert context["strategy"] == "Balanced Improver"
    assert set(context["holdings"].keys()) == {"AAA", "BBB"}
    assert context["holdings"]["AAA"]["quantity"] == 10.0
    assert context["cash"]["balance"] == 10000.0
    assert context["current_state"] == {"holdings": context["holdings"], "cash": context["cash"]}
    assert context["debate_feedback"] is None
    assert context["risk_budget"] == 1.0
    # Two priced, correlated names should yield a non-empty correlation matrix.
    assert context["correlation_matrix"].get("assets")


def test_build_council_context_degrades_cleanly_with_no_holdings_or_prices():
    db = _memory_db()
    user = _user(db)
    portfolio = _portfolio(db, user)

    context = build_council_context(db, portfolio, strategy_prompt="Momentum Tilted")

    assert set(context.keys()) == _REQUIRED_KEYS
    assert context["holdings"] == {}
    assert context["correlation_matrix"] == {}
    assert context["var_95"] == 0.0
    assert context["cvar_95"] == 0.0
    assert context["max_drawdown"] == 0.0
    assert context["concentration"] == {"hhi": 0.0}
    # No portfolio ever produced a snapshot yet — falls back to initial_cash.
    assert context["cash"]["balance"] == 10000.0


def test_build_council_context_accepts_precomputed_trade_proposal():
    from app.foundation.quant_proposal import McSummary, TradeProposal

    db = _memory_db()
    user = _user(db)
    portfolio = _portfolio(db, user)

    proposal = TradeProposal(
        portfolio_id=portfolio.id,
        suggested_weights={"AAA": 0.6, "BBB": 0.4},
        mc_summaries={
            "AAA": McSummary("AAA", "stock", -0.1, 0.02, 0.15, 0.6, 100.0),
        },
        risk_envelope={"var_95_daily": 0.01, "cvar_95_daily": 0.015, "max_drawdown": 0.05},
        current_book={"AAA": 600.0, "BBB": 400.0},
        optimizer_status="completed",
    )

    context = build_council_context(
        db, portfolio, strategy_prompt="x", trade_proposal=proposal,
    )

    assert context["var_95"] == 0.01
    assert context["cvar_95"] == 0.015
    assert context["max_drawdown"] == 0.05
    assert context["concentration"]["hhi"] == 0.6**2 + 0.4**2
    assert "AAA" in context["market_data"]
