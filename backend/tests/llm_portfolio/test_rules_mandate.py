"""Mandate B as the rules control (2026-11-09): same candidates and limits as A, no model."""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import (
    DiscoverCandidate,
    DiscoverRun,
    LlmPortfolioDecision,
    PaperHolding,
    PaperPortfolio,
    User,
)
from app.decision.llm_portfolio import rules
from app.decision.llm_portfolio.shortlist import latest_shortlist


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _series(annual_drift: float, daily_vol: float, n: int = 300) -> list[float]:
    """Deterministic zig-zag path with the given drift and volatility."""
    out, price = [], 100.0
    for i in range(n):
        price *= math.exp(annual_drift / 252 + (daily_vol if i % 2 else -daily_vol))
        out.append(price)
    return out


# ── pure parts ──────────────────────────────────────────────────────────────


def test_momentum_skips_the_last_month():
    closes = [100.0] * 232 + [150.0] * 21  # flat for 11 months, jump in the last month
    mom, vol = rules.momentum_and_vol(closes)
    assert mom == pytest.approx(0.0)
    assert vol is not None and vol > 0


def test_momentum_needs_a_year_of_prices():
    assert rules.momentum_and_vol([100.0] * 100) == (None, None)


def test_targets_drop_negative_momentum_and_the_most_volatile_quarter():
    signals = {
        "UP1": (0.30, 0.20), "UP2": (0.25, 0.25), "UP3": (0.20, 0.30),
        "WILD": (0.50, 0.90), "DOWN": (-0.10, 0.20),
    }
    weights, ranking = rules.rules_targets(signals, held=set(), satellite_share=0.6, max_single=0.15)
    assert set(weights) == {"UP1", "UP2", "UP3"}
    status = {r["ticker"]: r["status"] for r in ranking}
    assert status["WILD"].startswith("dropped: most volatile")
    assert status["DOWN"].startswith("dropped: momentum")
    # Inverse-vol weights, each capped at the single-position limit.
    assert all(w <= 0.15 + 1e-12 for w in weights.values())
    assert weights["UP1"] >= weights["UP2"] >= weights["UP3"]


def test_a_holding_inside_the_buffer_is_kept():
    # Volatility rises down the list, so the filter drops S8 and S9.
    signals = {f"S{i}": (0.5 - i * 0.02, 0.2 + i * 0.01) for i in range(10)}
    weights, _ = rules.rules_targets(signals, held={"S6"}, satellite_share=0.6, max_single=0.15, top_k=5)
    # S6 ranks 7th (inside HOLD_RANK 8): kept, and it displaces the 5th-ranked newcomer.
    assert "S6" in weights and "S4" not in weights and len(weights) == 5


# ── shortlist ───────────────────────────────────────────────────────────────


def _run(db, user_id, symbols, *, days_ago=0, status="completed"):
    run = DiscoverRun(user_id=user_id, status=status, created_at=datetime.now(UTC) - timedelta(days=days_ago))
    db.add(run)
    db.flush()
    for sym in symbols:
        db.add(DiscoverCandidate(run_id=run.id, symbol=sym, source="test", status="shortlisted"))
    db.commit()
    return run


def test_latest_shortlist_is_the_newest_completed_run_only():
    db = _memory_db()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    _run(db, user.id, ["OLD1", "OLD2"], days_ago=30)
    _run(db, user.id, ["NEW1", "NEW2"], days_ago=1)
    _run(db, user.id, ["RUNNING"], days_ago=0, status="running")
    assert [c.symbol for c in latest_shortlist(db, user.id)] == ["NEW1", "NEW2"]


def test_latest_shortlist_keeps_the_pipeline_order_when_cut():
    """Twelve names, cut at ten: the best ten by composite (then fit), not the first ten alphabetically."""
    db = _memory_db()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    run = DiscoverRun(user_id=user.id, status="completed", created_at=datetime.now(UTC))
    db.add(run)
    db.flush()
    # A01 sorts first but scores worst; Z01/Z02 sort last but score best.
    composites = {f"A{i:02d}": 0.10 + i / 100 for i in range(1, 11)} | {"Z01": 0.90, "Z02": 0.80}
    for sym, comp in composites.items():
        db.add(DiscoverCandidate(run_id=run.id, symbol=sym, source="test", status="shortlisted",
                                 scores_json=json.dumps({"composite": comp})))
    # A tie on composite goes to the better portfolio fit.
    db.add(DiscoverCandidate(run_id=run.id, symbol="B00", source="test", status="shortlisted",
                             scores_json=json.dumps({"composite": 0.80, "portfolio_fit": {"fit_score": 0.9}})))
    db.commit()
    top = [c.symbol for c in latest_shortlist(db, user.id)]
    assert len(top) == 10
    assert top[:3] == ["Z01", "B00", "Z02"]
    assert "A01" not in top and "A02" not in top and "A03" not in top


# ── the review ──────────────────────────────────────────────────────────────


def test_rules_review_buys_the_selection_and_journals_it(monkeypatch):
    db = _memory_db()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    _run(db, user.id, ["UP1", "UP2", "DOWN"])
    p = PaperPortfolio(user_id=user.id, name="Mandate B — Momentum Tilted", initial_cash=Decimal("10000"),
                       managed_by="llm", mandate="B",
                       mandate_config_json=json.dumps({"style": "momentum_tilted", "min_etf_pct": 0.30}))
    db.add(p)
    db.commit()

    paths = {"UP1": _series(0.40, 0.01), "UP2": _series(0.30, 0.015), "DOWN": _series(-0.30, 0.01)}
    monkeypatch.setattr(rules, "_closes", lambda _db, sym, days=420: paths.get(sym, []))
    passing = {"passed": True, "checks": [], "reason": "All gates passed"}
    with patch("app.decision.paper_portfolio.paper_quote_eur", return_value={"price": 100.0}), \
            patch("app.decision.llm_portfolio.gates.check_buy_gates", return_value=passing), \
            patch("app.decision.paper_portfolio.paper_order_fee", return_value=1.0):
        from app.decision.llm_portfolio.review import run_mandate_review

        res = run_mandate_review(db, user.id, "B")

    db.refresh(p)
    assert res["status"] == "completed"
    # Adopted the rules config: A's limits, new name.
    assert json.loads(p.mandate_config_json)["style"] == rules.RULES_STYLE
    assert p.name == rules.RULES_NAME
    held = {h.ticker: float(h.quantity) * 100 for h in db.query(PaperHolding).all()}
    assert set(held) == {"UP1", "UP2", "EUNL.DE"}
    assert held["UP1"] == pytest.approx(1500, abs=2)  # capped at 15 %
    assert held["EUNL.DE"] > 4000                       # the rest of the book in the core
    row = db.query(LlmPortfolioDecision).one()
    decision = json.loads(row.decision_json)
    assert decision["action"] == "rebalance"
    assert decision["expectation"]["metric"] == "benchmark_excess_pct"
    assert row.horizon_weeks == rules.HORIZON_WEEKS
    assert {r["ticker"]: r["status"] for r in decision["ranking"]}["DOWN"].startswith("dropped")


def test_rules_review_sells_a_name_that_left_the_selection(monkeypatch):
    db = _memory_db()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    _run(db, user.id, ["UP1"])
    p = PaperPortfolio(user_id=user.id, name="B", initial_cash=Decimal("10000"), managed_by="llm", mandate="B",
                       mandate_config_json=json.dumps(rules.RULES_CONFIG))
    db.add(p)
    db.commit()
    from app.decision.paper_portfolio import execute_trade

    with patch("app.decision.paper_portfolio.paper_quote_eur", return_value={"price": 100.0}):
        execute_trade(db, p.id, "GONE", "buy", 5, 100.0)  # 5 % of the book, no longer shortlisted
    monkeypatch.setattr(rules, "_closes", lambda _db, sym, days=420: _series(0.40, 0.01) if sym == "UP1" else [])
    passing = {"passed": True, "checks": [], "reason": "All gates passed"}
    with patch("app.decision.paper_portfolio.paper_quote_eur", return_value={"price": 100.0}), \
            patch("app.decision.llm_portfolio.gates.check_buy_gates", return_value=passing), \
            patch("app.decision.paper_portfolio.paper_order_fee", return_value=0.0):
        from app.decision import paper_portfolio

        rules.run_rules_review(db, user.id, p, paper=paper_portfolio)
    held = {h.ticker: float(h.quantity) for h in db.query(PaperHolding).all() if float(h.quantity) > 0}
    assert "UP1" in held
    # A's sell gates bind B too (ADR 0012): a dropped name loses at most a
    # third per review instead of being sold outright.
    assert 5 * 2 / 3 - 1e-6 <= held["GONE"] < 5


def test_crash_guard_needs_a_bear_market_and_rising_volatility():
    calm_up = _series(0.10, 0.008, n=600)
    assert rules.crash_state(calm_up)["active"] is False
    assert rules.crash_state(calm_up[:300]) == {"known": False, "active": False}
    # Two years down, then a volatile last month: the state momentum crashed in.
    falling = _series(-0.15, 0.008, n=580)
    wild = falling[-1]
    tail = []
    for i in range(21):
        wild *= math.exp(0.03 if i % 2 else -0.03)
        tail.append(wild)
    state = rules.crash_state(falling + tail)
    assert state["bear"] is True and state["vol_1m"] > state["vol_12m"] and state["active"] is True
    # Down but calm: no guard.
    assert rules.crash_state(_series(-0.15, 0.008, n=600))["active"] is False
