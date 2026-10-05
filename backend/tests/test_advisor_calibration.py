"""Calibration wiring tests (PR2 group B).

Acceptance: given a bucket history where "70% confident" historically hit
~55%, a new 70% raw confidence is calibrated toward ~55%; decisions and
predictions surface both raw and calibrated numbers; thin history falls back
to no calibrated value instead of fabricating one.
"""
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import _memory_db

from app.foundation.models.entities import (
    DiscoverCandidate,
    DiscoverRun,
    DiscoveryPrediction,
    DkbAccount,
    LlmPortfolioDecision,
    PriceCache,
    User,
)
from app.decision.discover.calibrator import calibrate_prediction
from app.decision.discover.predictor import store_prediction


def _user(db, name="jan") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _seed_resolved_history(
    db, user: User, *, conviction: float, n: int, hits: int, portfolio_id: str | None = None
) -> None:
    """Seed *n* resolved predictions at *conviction*, of which *hits* were right."""
    now = datetime.now(UTC)
    for i in range(n):
        realised = 0.04 if i < hits else -0.03
        db.add(
            DiscoveryPrediction(
                id=uuid4().hex, user_id=user.id, run_id="hist",
                symbol=f"H{conviction:.0%}{i}", predicted_at=now - timedelta(days=30),
                horizon_days=21, resolve_at=now - timedelta(days=2),
                direction="buy", conviction=conviction,
                realised_return=realised, outcome_status="resolved",
                portfolio_id=portfolio_id,
            )
        )
    db.commit()


def test_overconfident_bucket_is_calibrated_down():
    db = _memory_db()
    user = _user(db)
    # 70%-confidence bucket historically hit 55%; 30% bucket hit 20%.
    _seed_resolved_history(db, user, conviction=0.7, n=20, hits=11)
    _seed_resolved_history(db, user, conviction=0.3, n=20, hits=4)

    pred = store_prediction(
        db, symbol="NEW", composite_score=0.7, signal_breakdown={},
        user_id=user.id, run_id="new",
    )
    db.commit()
    out = calibrate_prediction(db, pred.id, user_id=user.id)
    assert out is not None and out.conviction_calibrated is not None
    # Calibrated toward the empirical 55% hit-rate, clearly below the raw 70%.
    assert out.conviction_calibrated < 0.68
    assert 0.40 <= out.conviction_calibrated <= 0.65
    assert out.conviction == 0.7  # raw preserved — honesty gap visible


def test_another_sources_history_does_not_calibrate():
    db = _memory_db()
    user = _user(db)
    # Plenty of history, but all of it an advisor sleeve's LLM confidence.
    _seed_resolved_history(db, user, conviction=0.7, n=20, hits=11, portfolio_id="sleeve-1")
    _seed_resolved_history(db, user, conviction=0.3, n=20, hits=4, portfolio_id="sleeve-1")

    pred = store_prediction(
        db, symbol="NEW", composite_score=0.7, signal_breakdown={},
        user_id=user.id, run_id="new",
    )
    db.commit()
    out = calibrate_prediction(db, pred.id, user_id=user.id)
    # A Discover composite score is not calibrated on it: left uncalibrated.
    assert out is not None and out.conviction_calibrated is None


def test_thin_history_stays_uncalibrated():
    db = _memory_db()
    user = _user(db)
    _seed_resolved_history(db, user, conviction=0.7, n=3, hits=1)  # < min_rows
    pred = store_prediction(
        db, symbol="NEW", composite_score=0.6, signal_breakdown={},
        user_id=user.id, run_id="new",
    )
    db.commit()
    out = calibrate_prediction(db, pred.id, user_id=user.id)
    assert out is not None
    assert out.conviction_calibrated is None  # never fabricated, never the raw score relabelled


# ---------------------------------------------------------------------------
# Cycle wiring: predictions carry calibrated values + sleeve attribution, and
# the decision record surfaces raw vs calibrated.
# ---------------------------------------------------------------------------


def _seed_prices(db, ticker: str, closes: list[float]) -> None:
    from app.foundation.models.entities import ListingCurrency

    # Test listings quote in EUR (the paper book values in EUR, and a ticker
    # without a suffix would otherwise be read as a USD listing).
    if db.get(ListingCurrency, ticker.upper()) is None:
        db.add(ListingCurrency(symbol=ticker.upper(), currency="EUR", source="test"))
    end = date.today()
    days: list[date] = []
    d = end
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
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


def _wiggly_series(base: float, n: int = 300, drift_pct: float = 0.0004) -> list[float]:
    out = []
    for i in range(n):
        wiggle = base * (0.005 if i % 7 == 0 else (-0.004 if i % 5 == 0 else 0.0))
        out.append(base * (1 + i * drift_pct) + wiggle)
    return out


def test_cycle_stores_calibrated_confidence_and_attribution():
    from app.decision.advisor.cycle import run_advisor_cycle

    db = _memory_db()
    user = _user(db)
    db.add(
        DkbAccount(
            id=uuid4().hex, user_id=user.id, type="checking",
            iban="DE" + uuid4().hex[:20], balance=Decimal("50000"), currency="EUR",
        )
    )
    db.commit()
    run = DiscoverRun(id=uuid4().hex, user_id=user.id, status="completed",
                      created_at=datetime.now(UTC))
    db.add(run)
    db.add(DiscoverCandidate(id=uuid4().hex, run_id=run.id, symbol="CAND",
                             source="screen_index", status="shortlisted"))
    db.commit()
    _seed_prices(db, "CAND", _wiggly_series(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=lambda _db, _m: json.dumps({"decisions": [
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "MC upside.", "confidence": 0.65},
        ]}),
    )
    assert result["status"] == "completed"

    pred = db.query(DiscoveryPrediction).filter(DiscoveryPrediction.run_id == run.id).one()
    assert pred.conviction == 0.65
    assert pred.conviction_calibrated is None  # no history, no calibration
    assert pred.portfolio_id == result["portfolio_id"]  # sleeve attribution

    record = db.query(LlmPortfolioDecision).one()
    payload = json.loads(record.decision_json)
    decision = payload["decisions"][0]
    assert decision["confidence_raw"] == 0.65
    assert decision["confidence_calibrated"] is None
    assert payload["lessons_injected"] == []
