"""Tests for the deterministic mandate-review assessment facts."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import (
    LlmPortfolioDecision,
    PaperPortfolio,
    PaperSnapshot,
)
from app.decision.llm_portfolio import assessment as assess


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _positions():
    return [
        {"isin": "US1", "ticker": "AAPL", "name": "Apple", "asset_type": "stock", "quantity": 10, "price": 100.0},
        {"isin": "IE1", "ticker": "IWDA", "name": "iShares World", "asset_type": "etf", "quantity": 10, "price": 100.0},
        {"isin": "US2", "ticker": "MSFT", "name": "Microsoft", "asset_type": "stock", "quantity": 10, "price": 100.0},
    ]


# ── compute_assessment (pure) ──────────────────────────────────────────────


def test_concentration_breach_flagged():
    # One position at 60% (900 cash makes it smaller); use no cash so AAPL=1/3.
    positions = [
        {"isin": "US1", "ticker": "AAPL", "name": "Apple", "asset_type": "stock", "quantity": 50, "price": 100.0},
        {"isin": "IE1", "ticker": "IWDA", "name": "World", "asset_type": "etf", "quantity": 10, "price": 100.0},
    ]
    result = assess.compute_assessment(
        positions=positions,
        cash_balance=0.0,
        max_drawdown=None,
        total_return_pct=None,
        benchmark_excess=None,
        max_single_position_pct=0.15,
        min_etf_pct=0.40,
    )
    conc = next(c for c in result["checks"] if c["key"] == "concentration")
    assert conc["status"] == "breach"
    assert conc["top_position"] == "AAPL"
    assert any(b["ticker"] == "AAPL" for b in conc["breaches"])
    assert "Concentration breach" in result["summary_line"]


def test_within_cap_is_ok():
    # Equal thirds → each 33% but cap is 0.40, so no breach.
    result = assess.compute_assessment(
        positions=_positions(),
        cash_balance=0.0,
        max_drawdown=None,
        total_return_pct=None,
        benchmark_excess=None,
        max_single_position_pct=0.40,
        min_etf_pct=0.20,
    )
    conc = next(c for c in result["checks"] if c["key"] == "concentration")
    assert conc["status"] == "ok"


def test_etf_floor_breach():
    # ETF is 1/3 (33.3%) vs a 40% floor → breach.
    result = assess.compute_assessment(
        positions=_positions(),
        cash_balance=0.0,
        max_drawdown=None,
        total_return_pct=None,
        benchmark_excess=None,
        max_single_position_pct=0.50,
        min_etf_pct=0.40,
    )
    etf = next(c for c in result["checks"] if c["key"] == "etf_floor")
    assert etf["status"] == "breach"
    assert etf["etf_pct"] == 33.33
    assert "below 40.0% floor" in result["summary_line"]


def test_etf_floor_met():
    result = assess.compute_assessment(
        positions=_positions(),
        cash_balance=0.0,
        max_drawdown=None,
        total_return_pct=None,
        benchmark_excess=None,
        max_single_position_pct=0.50,
        min_etf_pct=0.30,
    )
    etf = next(c for c in result["checks"] if c["key"] == "etf_floor")
    assert etf["status"] == "ok"


def test_cash_weight_included_in_denominator():
    # 3 x 1000 securities + 1000 cash = 4000 total → cash 25%, each pos 25%.
    result = assess.compute_assessment(
        positions=[
            {"isin": f"X{i}", "ticker": f"T{i}", "name": f"N{i}", "asset_type": "stock", "quantity": 10, "price": 100.0}
            for i in range(3)
        ],
        cash_balance=1000.0,
        max_drawdown=None,
        total_return_pct=None,
        benchmark_excess=None,
        max_single_position_pct=0.50,
        min_etf_pct=0.0,
    )
    cash = next(c for c in result["checks"] if c["key"] == "cash")
    assert cash["value_pct"] == 25.0
    assert result["total_value"] == 4000.0


def test_drawdown_passthrough():
    result = assess.compute_assessment(
        positions=_positions(),
        cash_balance=0.0,
        max_drawdown=-0.1234,
        total_return_pct=5.0,
        benchmark_excess=None,
        max_single_position_pct=0.50,
        min_etf_pct=0.0,
    )
    dd = next(c for c in result["checks"] if c["key"] == "drawdown")
    assert dd["status"] == "info"
    assert dd["value_pct"] == -12.34
    assert "Max DD -12.34%" in result["summary_line"]


def test_unpriced_position_excluded_from_denominator():
    positions = [
        {"isin": "US1", "ticker": "AAPL", "name": "Apple", "asset_type": "stock", "quantity": 10, "price": 100.0},
        {"isin": "US2", "ticker": "NOPX", "name": "NoPrice", "asset_type": "stock", "quantity": 10, "price": None},
    ]
    result = assess.compute_assessment(
        positions=positions,
        cash_balance=0.0,
        max_drawdown=None,
        total_return_pct=None,
        benchmark_excess=None,
        max_single_position_pct=0.50,
        min_etf_pct=0.0,
    )
    assert result["total_value"] == 1000.0
    assert len(result["positions"]) == 1
    assert len(result["unpriced_positions"]) == 1
    assert result["unpriced_positions"][0]["ticker"] == "NOPX"


def test_empty_portfolio_is_unresolvable_not_crash():
    result = assess.compute_assessment(
        positions=[],
        cash_balance=None,
        max_drawdown=None,
        total_return_pct=None,
        benchmark_excess=None,
        max_single_position_pct=0.15,
        min_etf_pct=0.40,
    )
    conc = next(c for c in result["checks"] if c["key"] == "concentration")
    etf = next(c for c in result["checks"] if c["key"] == "etf_floor")
    assert conc["status"] == "unresolvable"
    assert etf["status"] == "unresolvable"
    assert result["summary_line"] == "No mandate breaches; portfolio within constraints."


def test_benchmark_excess_merged_into_check():
    result = assess.compute_assessment(
        positions=_positions(),
        cash_balance=0.0,
        max_drawdown=None,
        total_return_pct=None,
        benchmark_excess={
            "window_days": 7,
            "portfolio_return_pct": 2.0,
            "benchmark_return_pct": 0.6,
            "excess_pct": 1.4,
        },
        max_single_position_pct=0.50,
        min_etf_pct=0.0,
    )
    bx = next(c for c in result["checks"] if c["key"] == "benchmark_excess")
    assert bx["status"] == "info"
    assert bx["excess_pct"] == 1.4
    assert "+1.4% vs EUNL.DE" in result["summary_line"]


# ── benchmark_excess_since (DB + monkeypatched fetch) ──────────────────────


def _seed_portfolio(db) -> PaperPortfolio:
    p = PaperPortfolio(user_id="u1", name="Mandate A", mandate="A")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_benchmark_excess_none_without_prior_review():
    db = _memory_db()
    p = _seed_portfolio(db)
    assert assess.benchmark_excess_since(db, p.id) is None


def test_benchmark_excess_computes_interval(monkeypatch):
    db = _memory_db()
    p = _seed_portfolio(db)
    now = datetime.now(UTC)

    # Prior completed review 10 days ago.
    db.add(LlmPortfolioDecision(
        portfolio_id=p.id,
        review_date=now - timedelta(days=10),
        mandate="A",
        status="completed",
        decision_json="{}",
    ))
    # Snapshot at/before start: since-inception return 1.0%.
    db.add(PaperSnapshot(
        portfolio_id=p.id,
        date=(now - timedelta(days=10)).date(),
        total_return_pct=1.0,
    ))
    # Latest snapshot: since-inception return 3.02%.
    db.add(PaperSnapshot(
        portfolio_id=p.id,
        date=now.date(),
        total_return_pct=3.02,
    ))
    db.commit()

    # Benchmark rose 0.6% over the window.
    monkeypatch.setattr(assess, "_fetch_interval_return", lambda *a, **k: 0.006)

    result = assess.benchmark_excess_since(db, p.id, now=now)
    assert result is not None
    # portfolio interval = 1.0302/1.01 - 1 = 0.02 (2.0%)
    assert result["portfolio_return_pct"] == 2.0
    assert result["benchmark_return_pct"] == 0.6
    assert result["excess_pct"] == 1.4


def test_benchmark_excess_none_when_fetch_fails(monkeypatch):
    db = _memory_db()
    p = _seed_portfolio(db)
    now = datetime.now(UTC)
    db.add(LlmPortfolioDecision(
        portfolio_id=p.id,
        review_date=now - timedelta(days=10),
        mandate="A",
        status="completed",
        decision_json="{}",
    ))
    db.add(PaperSnapshot(portfolio_id=p.id, date=(now - timedelta(days=10)).date(), total_return_pct=1.0))
    db.add(PaperSnapshot(portfolio_id=p.id, date=now.date(), total_return_pct=3.0))
    db.commit()
    monkeypatch.setattr(assess, "_fetch_interval_return", lambda *a, **k: None)
    assert assess.benchmark_excess_since(db, p.id, now=now) is None


# ── build_assessment (full DB wiring) ──────────────────────────────────────


def test_build_assessment_falls_back_to_avg_buy_price(monkeypatch):
    from app.foundation.models.entities import PaperHolding

    db = _memory_db()
    p = _seed_portfolio(db)
    # No DKB positions → price_map empty → falls back to avg_buy_price.
    db.add(PaperHolding(
        portfolio_id=p.id, isin="US1", ticker="AAPL", name="Apple",
        asset_type="stock", quantity=10, avg_buy_price=100,
    ))
    db.add(PaperHolding(
        portfolio_id=p.id, isin="IE1", ticker="IWDA", name="World",
        asset_type="etf", quantity=10, avg_buy_price=100,
    ))
    db.add(PaperSnapshot(portfolio_id=p.id, date=datetime.now(UTC).date(),
                         cash_balance=0, total_return_pct=0, max_drawdown=-0.05))
    db.commit()

    result = assess.build_assessment(db, p, {"max_single_position_pct": 0.15, "min_etf_pct": 0.40})
    # Two equal positions → each 50%; ETF floor 40% met; concentration cap 15% breached.
    conc = next(c for c in result["checks"] if c["key"] == "concentration")
    etf = next(c for c in result["checks"] if c["key"] == "etf_floor")
    assert conc["status"] == "breach"
    assert etf["status"] == "ok"
    assert result["total_value"] == 2000.0
