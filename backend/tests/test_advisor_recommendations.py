"""Graduated recommendation surface tests (PR2 group E).

Acceptance: the "what would change" diff renders per-position deltas with
calibrated confidence. The former Inbox-card emission (emit_rec_cards) was
removed with the review inbox (audit-fixes todo 8); recommendation
surfacing remains on the recommendations/dossier pages.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import (
    AdvisorStrategy,
    DiscoveryPrediction,
    DkbAccount,
    DkbPosition,
    PaperHolding,
    PaperPortfolio,
    User,
)
from app.decision.advisor.recommendations import compute_what_would_change


def _user(db, name="jan") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _setup_books(db, user: User) -> AdvisorStrategy:
    """Real book: 100% HELD. Champion book: 50% HELD, 50% CAND → clear diff."""
    depot = DkbAccount(
        id=uuid4().hex, user_id=user.id, type="depot",
        iban="DE" + uuid4().hex[:20], balance=Decimal("10000"), currency="EUR",
    )
    db.add(depot)
    db.commit()
    db.add(
        DkbPosition(
            id=uuid4().hex, account_id=depot.id, isin="DE000HELD0001",
            ticker="HELD", name="Held Stock", quantity=Decimal("100"),
            avg_buy_price=Decimal("90"), current_price=Decimal("100"),
            current_value=Decimal("10000"),
        )
    )
    portfolio = PaperPortfolio(
        user_id=user.id, name="Advisor Loop", initial_cash=10_000,
        managed_by="llm", mandate="advisor",
    )
    db.add(portfolio)
    db.commit()
    db.add_all([
        PaperHolding(
            id=uuid4().hex, portfolio_id=portfolio.id, isin="DE000HELD0001",
            ticker="HELD", name="Held Stock", quantity=Decimal("50"),
            avg_buy_price=Decimal("100"),
        ),
        PaperHolding(
            id=uuid4().hex, portfolio_id=portfolio.id, isin="DE000CAND0001",
            ticker="CAND", name="Candidate", quantity=Decimal("125"),
            avg_buy_price=Decimal("40"),
        ),
    ])
    strat = AdvisorStrategy(
        user_id=user.id, role="champion", portfolio_id=portfolio.id, config_json={},
    )
    db.add(strat)
    db.commit()

    # Champion's prediction for CAND with calibrated confidence + thesis.
    db.add(
        DiscoveryPrediction(
            id=uuid4().hex, user_id=user.id, run_id="r", portfolio_id=portfolio.id,
            symbol="CAND", predicted_at=datetime.now(UTC) - timedelta(days=1),
            horizon_days=21, resolve_at=datetime.now(UTC) + timedelta(days=27),
            direction="buy", conviction=0.8, conviction_calibrated=0.62,
            expected_return=0.05, expected_return_low=-0.02, expected_return_high=0.12,
            thesis="Quant screen + MC upside.", outcome_status="pending",
        )
    )
    db.commit()
    return strat


def test_diff_ranks_positions_and_carries_calibrated_confidence():
    db = _memory_db()
    user = _user(db)
    _setup_books(db, user)

    diff = compute_what_would_change(db, user.id)
    assert diff["graduated"] is False
    tickers = {r["ticker"]: r for r in diff["rows"]}
    assert "CAND" in tickers and "HELD" in tickers
    cand = tickers["CAND"]
    assert cand["action"] == "new"          # not in the real book
    assert cand["delta"] > 0
    assert cand["confidence_raw"] == 0.8
    assert cand["confidence_calibrated"] == 0.62
    assert cand["thesis"] == "Quant screen + MC upside."
    assert cand["mc"]["p50"] == 0.05
    held = tickers["HELD"]
    assert held["action"] in ("trim", "exit") and held["delta"] < 0
    assert diff["estimate"] is True and diff["not_financial_advice"] is True
    # Phase 4: the LLM loop never sets weights for the real book.
    assert diff["actionable"] is False and "paper-only" in diff["research_only"]
