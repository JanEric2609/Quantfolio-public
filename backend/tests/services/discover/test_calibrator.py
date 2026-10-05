"""Tests for the Discovery calibrator (P2 — Task 3).

Covers ``calibrate_prediction`` — uncalibrated without history, Mincer regression path,
clamping, self-exclusion, user scoping, lookback window, and persistence.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import DiscoveryPrediction, User
from app.decision.discover.calibrator import calibrate_prediction


def _make_user(db) -> User:
    user = User(id=uuid4().hex, username=f"user_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.flush()
    return user


def _make_prediction(
    db,
    user_id: str,
    *,
    conviction: float = 0.5,
    realised_return: float | None = None,
    outcome_status: str = "pending",
    predicted_at: datetime | None = None,
    symbol: str = "AAPL",
    run_id: str = "run-001",
) -> DiscoveryPrediction:
    """Helper to create a DiscoveryPrediction row with minimal boilerplate."""
    pred = DiscoveryPrediction(
        user_id=user_id,
        run_id=run_id,
        symbol=symbol,
        predicted_at=predicted_at or (datetime.now(timezone.utc) - timedelta(days=30)),
        horizon_days=120,
        resolve_at=datetime.now(timezone.utc) + timedelta(days=90),
        direction="long",
        conviction=conviction,
        conviction_calibrated=None,
        realised_return=realised_return,
        outcome_status=outcome_status,
        features_json={},
        is_estimate=True,
        is_financial_advice=False,
    )
    db.add(pred)
    db.flush()
    return pred


# ---------------------------------------------------------------------------
# calibrate_prediction
# ---------------------------------------------------------------------------


def test_calibrate_prediction_not_found():
    """Unknown prediction_id returns None."""
    db = _memory_db()
    user = _make_user(db)
    db.commit()

    result = calibrate_prediction(db, "nonexistent-id", user_id=user.id)
    assert result is None


def test_calibrate_thin_history_stays_uncalibrated():
    """<20 resolved predictions: no calibrated value (it used to be the raw
    score relabelled)."""
    db = _memory_db()
    user = _make_user(db)

    # Create prediction to calibrate (excluded from training set).
    target = _make_prediction(db, user.id, conviction=0.7, symbol="TARGET")
    # Create only 5 resolved predictions (below min_rows=20).
    for i in range(5):
        _make_prediction(
            db,
            user.id,
            conviction=0.3 + i * 0.1,
            realised_return=0.02 * i,
            outcome_status="resolved",
            symbol=f"HIST{i}",
        )
    db.commit()

    result = calibrate_prediction(db, target.id, user_id=user.id, min_rows=20)
    assert result is not None
    assert result.conviction_calibrated is None  # too little history: uncalibrated


def test_calibrate_mincer_path():
    """>=20 resolved predictions uses Mincer regression (logistic on hit)."""
    db = _memory_db()
    user = _make_user(db)

    target = _make_prediction(db, user.id, conviction=0.7, symbol="TARGET")
    # Create 25 resolved predictions with a mix of positive/negative returns.
    # Threshold at conviction ~0.4: low conviction → negative, high → positive.
    for i in range(25):
        c = 0.1 + i * 0.03  # 0.1 to ~0.82
        r = -0.05 if c < 0.4 else 0.05
        _make_prediction(
            db,
            user.id,
            conviction=c,
            realised_return=r,
            outcome_status="resolved",
            symbol=f"HIST{i}",
        )
    db.commit()

    result = calibrate_prediction(db, target.id, user_id=user.id, min_rows=20)
    assert result is not None
    assert result.conviction_calibrated is not None
    # Conviction=0.7 is well above the ~0.4 decision boundary → calibrated > 0.5
    assert result.conviction_calibrated > 0.5


def test_calibrate_clamps_range():
    """Calibrated value clamped to [0, 1]."""
    db = _memory_db()
    user = _make_user(db)

    target = _make_prediction(db, user.id, conviction=2.0, symbol="TARGET")
    # Create 25 resolved predictions to trigger Mincer path.
    for i in range(25):
        c = 0.1 + i * 0.03
        r = -0.05 if c < 0.4 else 0.05
        _make_prediction(
            db,
            user.id,
            conviction=c,
            realised_return=r,
            outcome_status="resolved",
            symbol=f"HIST{i}",
        )
    db.commit()

    result = calibrate_prediction(db, target.id, user_id=user.id, min_rows=20)
    assert result is not None
    assert result.conviction_calibrated is not None
    assert 0.0 <= result.conviction_calibrated <= 1.0


def test_calibrate_excludes_self():
    """Prediction being calibrated is excluded from training set."""
    db = _memory_db()
    user = _make_user(db)

    target = _make_prediction(
        db,
        user.id,
        conviction=0.9,
        realised_return=0.5,
        outcome_status="resolved",
        symbol="TARGET",
    )
    # Create 25 other resolved predictions.
    for i in range(25):
        c = 0.1 + i * 0.03
        r = -0.05 if c < 0.4 else 0.05
        _make_prediction(
            db,
            user.id,
            conviction=c,
            realised_return=r,
            outcome_status="resolved",
            symbol=f"HIST{i}",
        )
    db.commit()

    result = calibrate_prediction(db, target.id, user_id=user.id, min_rows=20)
    assert result is not None
    assert result.conviction_calibrated is not None
    # Target is resolved but excluded — the Mincer fit comes only from the 25
    # other predictions. Conviction=0.9 is well above the ~0.4 boundary.
    assert result.conviction_calibrated > 0.5


def test_calibrate_empty_history():
    """No resolved predictions: uncalibrated."""
    db = _memory_db()
    user = _make_user(db)

    target = _make_prediction(db, user.id, conviction=0.6, symbol="TARGET")
    # No resolved predictions at all.
    db.commit()

    result = calibrate_prediction(db, target.id, user_id=user.id, min_rows=20)
    assert result is not None
    assert result.conviction_calibrated is None  # no history: uncalibrated


def test_calibrate_wrong_user():
    """Predictions from other user are not used."""
    db = _memory_db()
    user_a = _make_user(db)
    user_b = _make_user(db)

    target = _make_prediction(db, user_a.id, conviction=0.7, symbol="TARGET")
    # user_b has many resolved predictions — should not be visible.
    for i in range(25):
        _make_prediction(
            db,
            user_b.id,
            conviction=0.1 + i * 0.03,
            realised_return=0.05 + (0.1 + i * 0.03) * 0.8,
            outcome_status="resolved",
            symbol=f"HIST{i}",
        )
    db.commit()

    # user_a has 0 resolved -> uncalibrated
    result = calibrate_prediction(db, target.id, user_id=user_a.id, min_rows=20)
    assert result is not None
    assert result.conviction_calibrated is None


def test_calibrate_lookback_window():
    """Only rows within lookback_days are used."""
    db = _memory_db()
    user = _make_user(db)

    target = _make_prediction(db, user.id, conviction=0.7, symbol="TARGET")
    now = datetime.now(timezone.utc)

    # 25 old predictions (beyond 365-day lookback).
    for i in range(25):
        _make_prediction(
            db,
            user.id,
            conviction=0.1 + i * 0.03,
            realised_return=0.05 + (0.1 + i * 0.03) * 0.8,
            outcome_status="resolved",
            symbol=f"OLD{i}",
            predicted_at=now - timedelta(days=400 + i),
        )
    db.commit()

    # lookback=365 means those old predictions are excluded -> uncalibrated
    result = calibrate_prediction(db, target.id, user_id=user.id, lookback_days=365, min_rows=20)
    assert result is not None
    assert result.conviction_calibrated is None


def test_calibrate_stores_and_returns():
    """Verify conviction_calibrated is actually persisted on the correct row."""
    db = _memory_db()
    user = _make_user(db)

    target = _make_prediction(db, user.id, conviction=0.7, symbol="TARGET")
    for i in range(25):
        c = 0.1 + i * 0.03
        r = -0.05 if c < 0.4 else 0.05
        _make_prediction(
            db,
            user.id,
            conviction=c,
            realised_return=r,
            outcome_status="resolved",
            symbol=f"HIST{i}",
        )
    db.commit()

    calibrate_prediction(db, target.id, user_id=user.id, min_rows=20)

    # Confirm persistence by re-reading from the same session (commit was called).
    refreshed = db.query(DiscoveryPrediction).filter_by(id=target.id).first()
    assert refreshed is not None
    assert refreshed.conviction_calibrated is not None
    assert refreshed.conviction_calibrated > 0.5

    # Verify other rows were NOT modified.
    count_calibrated = (
        db.query(DiscoveryPrediction)
        .filter(DiscoveryPrediction.conviction_calibrated.isnot(None))
        .count()
    )
    assert count_calibrated == 1  # only the target was updated
