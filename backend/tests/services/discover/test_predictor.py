"""Tests for the Discovery forward predictor (P0 — #111).

Covers ``store_prediction`` and ``predictor_should_generate_dossier``.
"""
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import DiscoveryPrediction, User
from app.decision.discover.predictor import (
    predictor_should_generate_dossier,
    store_prediction,
)


def _make_user(db) -> User:
    user = User(id=uuid4().hex, username=f"user_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.flush()
    return user


# ---------------------------------------------------------------------------
# store_prediction
# ---------------------------------------------------------------------------


def test_store_prediction_creates_row():
    """Happy path: minimum valid args produce a well-formed row."""
    db = _memory_db()
    user = _make_user(db)

    pred = store_prediction(
        db,
        symbol="AAPL",
        composite_score=0.75,
        signal_breakdown={"composite": 0.75, "regime": 0.6},
        direction_hint="buy",
        user_id=user.id,
        run_id="run-001",
        isin="US0378331005",
    )

    assert pred.symbol == "AAPL"
    assert pred.isin == "US0378331005"
    assert pred.conviction == 0.75
    assert pred.conviction_calibrated is None
    assert pred.direction == "buy"
    assert pred.outcome_status == "pending"
    # Advisor-loop PR1: default horizon is 21 trading days (env-overridable).
    from app.decision.discover.ledger import DEFAULT_HORIZON_DAYS

    assert pred.horizon_days == DEFAULT_HORIZON_DAYS == 21
    assert pred.resolve_at > pred.predicted_at
    assert pred.features_json["signal_breakdown"]["composite"] == 0.75
    assert pred.id is not None
    assert len(pred.id) == 36  # UUID hex
    assert pred.is_estimate is True
    assert pred.is_financial_advice is False


def test_store_prediction_clamps_score():
    """Composite score is clamped to [0, 1]."""
    db = _memory_db()
    user = _make_user(db)

    over = store_prediction(
        db,
        symbol="TSLA",
        composite_score=1.5,
        signal_breakdown={},
        user_id=user.id,
        run_id="run-002",
    )
    assert over.conviction == 1.0

    under = store_prediction(
        db,
        symbol="TSLA",
        composite_score=-0.3,
        signal_breakdown={},
        user_id=user.id,
        run_id="run-002",
    )
    assert under.conviction == 0.0


def test_store_prediction_none_score():
    """None composite_score sets conviction to None."""
    db = _memory_db()
    user = _make_user(db)

    pred = store_prediction(
        db,
        symbol="MSFT",
        composite_score=None,  # type: ignore[arg-type]
        signal_breakdown={},
        user_id=user.id,
        run_id="run-003",
    )
    assert pred.conviction is None


def test_store_prediction_invalid_direction_falls_back():
    """Unknown direction_hint logs a warning and falls back to 'buy'."""
    db = _memory_db()
    user = _make_user(db)

    pred = store_prediction(
        db,
        symbol="GOOGL",
        composite_score=0.5,
        signal_breakdown={},
        direction_hint="long",  # "long" is not in VALID_DIRECTIONS; triggers fallback
        user_id=user.id,
        run_id="run-004",
    )
    assert pred.direction == "buy"  # fallback


def test_store_prediction_no_isin():
    """Omitting isin is valid — column is nullable."""
    db = _memory_db()
    user = _make_user(db)

    pred = store_prediction(
        db,
        symbol="NFLX",
        composite_score=0.6,
        signal_breakdown={"momentum": 0.7},
        user_id=user.id,
        run_id="run-005",
    )
    assert pred.isin is None
    assert pred.symbol == "NFLX"


def test_store_prediction_sell_and_neutral():
    """direction_hint accepts 'sell' and 'neutral'."""
    db = _memory_db()
    user = _make_user(db)

    short = store_prediction(
        db,
        symbol="TSLA",
        composite_score=0.2,
        signal_breakdown={},
        direction_hint="sell",
        user_id=user.id,
        run_id="run-006",
    )
    assert short.direction == "sell"

    neutral = store_prediction(
        db,
        symbol="TSLA",
        composite_score=0.5,
        signal_breakdown={},
        direction_hint="neutral",
        user_id=user.id,
        run_id="run-006",
    )
    assert neutral.direction == "neutral"


def test_store_prediction_flush_not_commit():
    """Row is flushed but not committed — caller owns the transaction."""
    db = _memory_db()
    user = _make_user(db)

    store_prediction(
        db,
        symbol="IBM",
        composite_score=0.5,
        signal_breakdown={},
        user_id=user.id,
        run_id="run-007",
    )

    # Row visible inside the session (flush).
    count = db.query(DiscoveryPrediction).filter_by(symbol="IBM").count()
    assert count == 1

    # Rollback to prove caller controls commit.
    db.rollback()
    count_after = db.query(DiscoveryPrediction).filter_by(symbol="IBM").count()
    assert count_after == 0


# ---------------------------------------------------------------------------
# predictor_should_generate_dossier
# ---------------------------------------------------------------------------


def test_should_generate_true_no_thesis():
    """Returns True when prediction exists and thesis is NULL."""
    db = _memory_db()
    user = _make_user(db)

    pred = store_prediction(
        db,
        symbol="AAPL",
        composite_score=0.75,
        signal_breakdown={},
        user_id=user.id,
        run_id="run-010",
    )
    db.flush()

    assert predictor_should_generate_dossier(pred.id, db) is True


def test_should_generate_false_when_thesis_exists():
    """Returns False when thesis is already populated."""
    db = _memory_db()
    user = _make_user(db)

    pred = store_prediction(
        db,
        symbol="AAPL",
        composite_score=0.75,
        signal_breakdown={},
        user_id=user.id,
        run_id="run-011",
    )
    pred.thesis = "Strong buy thesis with upside catalysts."
    db.flush()

    assert predictor_should_generate_dossier(pred.id, db) is False


def test_should_generate_false_when_prediction_missing():
    """Returns False when prediction_id does not match any row."""
    db = _memory_db()
    assert predictor_should_generate_dossier("nonexistent-id", db) is False


def test_should_generate_false_empty_string_id():
    """Returns False for an empty-string prediction ID."""
    db = _memory_db()
    assert predictor_should_generate_dossier("", db) is False
