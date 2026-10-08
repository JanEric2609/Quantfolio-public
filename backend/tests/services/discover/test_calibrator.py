"""The staged calibrator (ADR 0018 §6).

No probability below 100 effective (date-level) labels; a shrunk, monotone
logistic up to 1,000; isotonic above. Hit = excess > 0 only, matured
outcomes of the same source, with a 21-trading-day embargo.
"""
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import numpy as np
import pytest
from conftest import _memory_db

from app.decision.discover.calibrator import (
    CALIBRATOR_VERSION,
    MIN_N_EFF,
    calibrate_prediction,
    fit_calibrator,
)
from app.foundation.models.entities import DiscoveryPrediction, User

NOW = datetime.now(UTC)


def _make_user(db) -> User:
    user = User(id=uuid4().hex, username=f"user_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.flush()
    return user


def _pred(db, user_id, *, conviction=0.5, excess=None, status="pending", issued=None, resolve=None,
          portfolio_id=None, symbol="AAPL") -> DiscoveryPrediction:
    issued = issued or NOW
    pred = DiscoveryPrediction(
        user_id=user_id, run_id="run", symbol=symbol, predicted_at=issued, horizon_days=21,
        resolve_at=resolve or issued + timedelta(days=30), direction="buy", conviction=conviction,
        realised_return=excess, excess_return=excess, outcome_status=status, portfolio_id=portfolio_id,
        features_json={},
    )
    db.add(pred)
    db.flush()
    return pred


def _history(db, user_id, n_dates: int, *, per_date: int = 3, signal: bool = True, seed: int = 1,
             portfolio_id=None) -> None:
    """*n_dates* matured issue dates, *per_date* calls each; with *signal*, high scores beat the ETF more often."""
    rng = np.random.default_rng(seed)
    for d in range(n_dates):
        issued = NOW - timedelta(days=200 + 3 * d)
        for k in range(per_date):
            score = float(rng.uniform())
            if signal:
                excess = 0.02 if rng.uniform() < 0.3 + 0.4 * score else -0.02
            else:  # every score of a date shares its outcome: the score says nothing
                excess = 0.02 if d % 2 == 0 else -0.02
            _pred(db, user_id, conviction=score, excess=excess, status="resolved", issued=issued,
                  resolve=issued + timedelta(days=30), portfolio_id=portfolio_id, symbol=f"H{d}_{k}")


def test_unknown_prediction_returns_none():
    db = _memory_db()
    user = _make_user(db)
    assert calibrate_prediction(db, "nope", user_id=user.id) is None


def test_below_one_hundred_issue_dates_no_probability_is_stated():
    db = _memory_db()
    user = _make_user(db)
    # 300 calls, but on 60 dates: 60 effective labels.
    _history(db, user.id, 60, per_date=5)
    target = _pred(db, user.id, conviction=0.9, symbol="NEW")

    out = calibrate_prediction(db, target.id, user_id=user.id)

    assert out is not None and out.conviction_calibrated is None


def test_the_shrunk_logistic_takes_over_from_one_hundred_dates():
    db = _memory_db()
    user = _make_user(db)
    _history(db, user.id, 150)
    high = _pred(db, user.id, conviction=0.95, symbol="HIGH")
    low = _pred(db, user.id, conviction=0.05, symbol="LOW")

    hi = calibrate_prediction(db, high.id, user_id=user.id).conviction_calibrated
    lo = calibrate_prediction(db, low.id, user_id=user.id).conviction_calibrated

    assert hi is not None and lo is not None
    assert lo < 0.5 < hi and hi - lo < 0.5  # shrunk towards the base rate


def test_a_score_without_signal_states_no_probability():
    db = _memory_db()
    user = _make_user(db)
    _history(db, user.id, 150, signal=False, seed=4)
    target = _pred(db, user.id, conviction=0.9, symbol="NEW")

    assert calibrate_prediction(db, target.id, user_id=user.id).conviction_calibrated is None


def test_another_source_and_another_user_do_not_count():
    db = _memory_db()
    user = _make_user(db)
    other = _make_user(db)
    _history(db, user.id, 150, portfolio_id="sleeve-1")
    _history(db, other.id, 150)
    target = _pred(db, user.id, conviction=0.9, symbol="NEW")

    assert calibrate_prediction(db, target.id, user_id=user.id).conviction_calibrated is None


def test_outcomes_inside_the_embargo_are_not_used():
    db = _memory_db()
    user = _make_user(db)
    # Plenty of history, all of it resolved within 21 trading days of the new call.
    for d in range(150):
        issued = NOW - timedelta(days=40)
        _pred(db, user.id, conviction=d / 150, excess=0.02 if d > 75 else -0.02, status="resolved",
              issued=issued - timedelta(days=d % 3), resolve=NOW - timedelta(days=5), symbol=f"E{d}")
    target = _pred(db, user.id, conviction=0.9, symbol="NEW")

    assert calibrate_prediction(db, target.id, user_id=user.id).conviction_calibrated is None


def test_a_calibrated_value_is_written_once():
    db = _memory_db()
    user = _make_user(db)
    _history(db, user.id, 150)
    target = _pred(db, user.id, conviction=0.9, symbol="NEW")
    first = calibrate_prediction(db, target.id, user_id=user.id).conviction_calibrated
    _history(db, user.id, 150, seed=9)  # more history later does not rewrite it

    assert calibrate_prediction(db, target.id, user_id=user.id).conviction_calibrated == first


def test_fit_stages_follow_the_effective_label_count():
    day = date(2025, 1, 1)
    few = [(0.5, True, day)] * 500  # 500 rows, one date
    assert fit_calibrator(few).stage == "base_rate" and fit_calibrator(few).n_eff == 1
    rng = np.random.default_rng(0)
    rows = []
    for d in range(1200):
        s = float(rng.uniform())
        rows.append((s, bool(rng.uniform() < 0.3 + 0.4 * s), day + timedelta(days=d)))
    assert fit_calibrator(rows[:MIN_N_EFF]).stage == "logistic"
    iso = fit_calibrator(rows)
    assert iso.stage == "isotonic" and iso.viable
    assert iso.predict(0.9) > iso.predict(0.1)
    assert fit_calibrator([]).base_rate is None


def test_the_slope_is_never_negative():
    day = date(2025, 1, 1)
    rows = [(i / 200, i < 100, day + timedelta(days=i)) for i in range(200)]  # high scores lose
    fit = fit_calibrator(rows)
    assert fit.params["slope"] == pytest.approx(0.0, abs=1e-6)
    assert not fit.viable  # a flat curve has no usable signal


def test_the_version_names_the_rule():
    assert CALIBRATOR_VERSION["rule"] == 2 and CALIBRATOR_VERSION["min_n_eff"] == 100
    assert CALIBRATOR_VERSION["hit"] == "excess>0"
