"""Tests for the discover pipeline's exponential-decay recency penalty.

docs/adr/0009-discover-recency-penalty.md: an audit found the discover
pipeline kept re-suggesting the same tickers run after run because nothing
penalized re-recommending a name shortly after a prior recommendation.
_apply_recency_penalty (pipeline.py) closes this for single-name equities
only, exempting ETFs and strong-conviction candidates.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import DiscoveryPrediction, User
from app.decision.discover.pipeline import _apply_recency_penalty


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db) -> User:
    user = User(username=f"u_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.commit()
    return user


def _seed_prediction(db, user_id: str, symbol: str, predicted_at: datetime) -> None:
    db.add(
        DiscoveryPrediction(
            user_id=user_id,
            run_id=uuid4().hex,
            symbol=symbol,
            predicted_at=predicted_at,
            horizon_days=30,
            resolve_at=predicted_at + timedelta(days=30),
            conviction=0.5,
        )
    )
    db.commit()


def test_etf_exempt_regardless_of_history():
    db = _memory_db()
    user = _user(db)
    _seed_prediction(db, user.id, "EUNL.DE", datetime.now(UTC))

    adjusted, detail = _apply_recency_penalty(db, user.id, "EUNL.DE", "etf", 0.4)

    assert adjusted == 0.4
    assert detail == {}


def _seed_prediction_with_raw(db, user_id: str, symbol: str, predicted_at: datetime, raw: float) -> None:
    db.add(
        DiscoveryPrediction(
            user_id=user_id,
            run_id=uuid4().hex,
            symbol=symbol,
            predicted_at=predicted_at,
            horizon_days=30,
            resolve_at=predicted_at + timedelta(days=30),
            conviction=raw,
            features_json={"signal_breakdown": {"composite_raw": raw}},
        )
    )
    db.commit()


def test_high_score_alone_no_longer_exempts():
    """ADR 0017: every 2026-09-25 shortlist name scored >= 0.68, above the old
    0.65 strong-conviction exemption, so the penalty never fired. A high
    level is no longer enough: BBVA.MC recommended yesterday at the same
    score is penalised, and days_since_last is populated."""
    db = _memory_db()
    user = _user(db)
    _seed_prediction_with_raw(db, user.id, "BBVA.MC", datetime.now(UTC) - timedelta(days=1), 0.72)

    adjusted, detail = _apply_recency_penalty(
        db, user.id, "BBVA.MC", "equity", 0.72
    )

    assert detail["exempt_reason"] is None
    assert detail["days_since_last"] == pytest.approx(1.0, abs=0.01)
    assert detail["last_composite"] == pytest.approx(0.72)
    assert detail["penalty_fraction"] > 0.2
    assert adjusted < 0.72


def test_materially_improved_score_is_exempt():
    db = _memory_db()
    user = _user(db)
    _seed_prediction_with_raw(db, user.id, "ASML", datetime.now(UTC) - timedelta(days=3), 0.60)

    adjusted, detail = _apply_recency_penalty(db, user.id, "ASML", "equity", 0.66)

    assert adjusted == 0.66
    assert detail["exempt_reason"] == "improved_since_last"
    assert detail["penalty_fraction"] == 0.0
    assert detail["days_since_last"] == pytest.approx(3.0, abs=0.01)


def test_small_improvement_is_not_exempt():
    db = _memory_db()
    user = _user(db)
    _seed_prediction_with_raw(db, user.id, "ASML", datetime.now(UTC), 0.60)

    adjusted, detail = _apply_recency_penalty(db, user.id, "ASML", "equity", 0.64)

    assert detail["exempt_reason"] is None
    assert adjusted < 0.64


def test_legacy_row_without_raw_composite_never_qualifies_as_improved():
    """Older rows only carry the post-penalty conviction; comparing against
    it would make a penalised name look "improved" on its next run."""
    db = _memory_db()
    user = _user(db)
    _seed_prediction(db, user.id, "SAP.DE", datetime.now(UTC))  # conviction=0.5, no composite_raw

    _, detail = _apply_recency_penalty(db, user.id, "SAP.DE", "equity", 0.9)

    assert detail["exempt_reason"] is None
    assert detail["last_composite"] is None


def test_never_recommended_no_penalty():
    db = _memory_db()
    user = _user(db)

    adjusted, detail = _apply_recency_penalty(db, user.id, "NVDA", "equity", 0.4)

    assert adjusted == 0.4
    assert detail["exempt_reason"] == "never_recommended"
    assert detail["days_since_last"] is None


def test_recommended_today_applies_max_penalty_scale():
    db = _memory_db()
    user = _user(db)
    _seed_prediction(db, user.id, "TSLA", datetime.now(UTC))

    adjusted, detail = _apply_recency_penalty(db, user.id, "TSLA", "equity", 0.4)

    assert detail["exempt_reason"] is None
    assert detail["days_since_last"] == 0.0
    # Day 0: full penalty scale (0.25) applied (within float/timing tolerance).
    assert abs(detail["penalty_fraction"] - 0.25) < 1e-3
    assert abs(adjusted - 0.4 * (1 - 0.25)) < 1e-3


def test_penalty_decays_toward_zero_over_time(monkeypatch):
    """A ticker recommended one half-life ago carries half the day-0 penalty;
    far beyond several half-lives the penalty is negligible."""
    from app.decision.discover import pipeline as pipeline_mod

    db = _memory_db()
    user = _user(db)
    monkeypatch.setattr(pipeline_mod, "_recency_penalty_halflife_days", lambda _db: 30.0)

    _seed_prediction(db, user.id, "SIE.DE", datetime.now(UTC) - timedelta(days=30))
    _, detail_one_halflife = _apply_recency_penalty(db, user.id, "SIE.DE", "equity", 0.4)
    assert abs(detail_one_halflife["penalty_fraction"] - 0.125) < 1e-6  # 0.25 * 0.5

    db2 = _memory_db()
    user2 = _user(db2)
    _seed_prediction(db2, user2.id, "SIE.DE", datetime.now(UTC) - timedelta(days=300))
    _, detail_far_past = _apply_recency_penalty(db2, user2.id, "SIE.DE", "equity", 0.4)
    assert detail_far_past["penalty_fraction"] < 0.001


def test_configurable_halflife_from_public_settings():
    """The half-life is read from the discover_recency_penalty_halflife_days
    public setting, not hardcoded — an operator can widen or narrow the
    decay window without a deploy."""
    from app.foundation.settings import upsert_public_settings
    from app.decision.discover.pipeline import _recency_penalty_halflife_days

    db = _memory_db()
    upsert_public_settings(db, {"discover_recency_penalty_halflife_days": 7})

    assert _recency_penalty_halflife_days(db) == 7.0


def test_default_halflife_is_thirty_days_when_unset():
    from app.decision.discover.pipeline import _recency_penalty_halflife_days

    db = _memory_db()
    assert _recency_penalty_halflife_days(db) == 30.0
