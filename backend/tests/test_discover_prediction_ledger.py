"""Tests for the Discovery prediction ledger (P0 — #111).

Covers:
- ``write_predictions`` persists one row per shortlist candidate with the
  point-in-time feature snapshot, captured price, and ``outcome_status=pending``;
- raw composite score is stored as ``conviction`` while forward-predictor fields
  (calibration / expected return / thesis) stay NULL at P0;
- a candidate with no cached price is still logged (``price_at_prediction`` NULL),
  so a missing/illiquid symbol never drops a prediction;
- the ``0064_discovery_prediction`` migration applies on a fresh sqlite DB and is
  idempotent.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from conftest import _memory_db
from sqlalchemy import create_engine

from app.foundation.models.entities import DiscoveryPrediction, PriceCache, User
from app.decision.discover.ledger import DEFAULT_HORIZON_DAYS, write_predictions


def _seed_prices(db, ticker: str, closes: list[float]) -> None:
    """Seed PriceCache with business-day closes ending today."""
    days: list[date] = []
    d = date.today()
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    now = datetime.now(UTC)
    for dd, close in zip(days, closes):
        db.add(PriceCache(
            id=uuid4().hex, ticker=ticker.upper(), date=dd,
            close=Decimal(str(round(close, 4))), fetched_at=now,
            source="test", stale=False, currency="EUR",
        ))
    db.commit()


def _shortlist_result(symbol: str, composite: float) -> dict:
    return {
        "symbol": symbol,
        "isin": f"ISIN{symbol}",
        "name": symbol,
        "source": "screen_etf",
        "scores": {
            "alpha_miner": {"ic": 0.06, "icir": 0.4},
            "alpha_screener": {"regime_affinity": 0.7},
        },
        "concerns": ["some_concern"],
        "composite_score": composite,
    }


def test_write_predictions_persists_point_in_time_rows():
    db = _memory_db()
    user = User(id=uuid4().hex, username="u", password_hash="x")
    db.add(user)
    db.commit()

    _seed_prices(db, "AAA", [100.0, 101.0, 102.5])
    _seed_prices(db, "BBB", [50.0, 49.0, 48.0])

    shortlisted = [
        _shortlist_result("AAA", 0.82),
        _shortlist_result("BBB", 0.61),
    ]
    run_id = uuid4().hex

    before = datetime.now(UTC)
    created = write_predictions(
        db, run_id=run_id, user_id=user.id, shortlisted=shortlisted
    )
    after = datetime.now(UTC)

    assert len(created) == 2

    rows = db.query(DiscoveryPrediction).order_by(DiscoveryPrediction.symbol).all()
    assert [r.symbol for r in rows] == ["AAA", "BBB"]

    aaa = rows[0]
    # Point-in-time fields.
    assert aaa.run_id == run_id
    assert aaa.user_id == user.id
    assert aaa.direction == "buy"
    assert aaa.outcome_status == "pending"
    assert aaa.horizon_days == DEFAULT_HORIZON_DAYS
    # predicted_at is stamped at write time; resolve_at = predicted_at + horizon.
    predicted_at = aaa.predicted_at
    if predicted_at.tzinfo is None:
        predicted_at = predicted_at.replace(tzinfo=UTC)
    assert before <= predicted_at <= after
    resolve_at = aaa.resolve_at
    if resolve_at.tzinfo is None:
        resolve_at = resolve_at.replace(tzinfo=UTC)
    # Horizon counts TRADING days (advisor-loop PR1): resolve_at is the
    # trading-day-shifted timestamp, so the calendar span is >= the horizon
    # and bounded by the weekend padding (~2 extra days per 5 trading days).
    calendar_span = (resolve_at - predicted_at).days
    assert DEFAULT_HORIZON_DAYS <= calendar_span <= DEFAULT_HORIZON_DAYS * 1.5 + 3

    # Raw composite captured as conviction; forward fields NULL at P0.
    assert abs(aaa.conviction - 0.82) < 1e-9
    assert aaa.conviction_calibrated is None
    assert aaa.expected_return is None
    assert aaa.expected_return_low is None
    assert aaa.expected_return_high is None
    assert aaa.thesis is None
    assert aaa.risks is None
    assert aaa.realised_return is None
    assert aaa.score_json is None

    # Price captured from latest cached close.
    assert abs(aaa.price_at_prediction - 102.5) < 1e-6

    # Feature snapshot is preserved for auditability.
    features = aaa.features_json
    assert features["composite_score"] == 0.82
    assert features["scores"]["alpha_miner"]["ic"] == 0.06
    assert features["concerns"] == ["some_concern"]


def test_write_predictions_handles_missing_price():
    db = _memory_db()
    user = User(id=uuid4().hex, username="u2", password_hash="x")
    db.add(user)
    db.commit()

    # No PriceCache rows seeded for this symbol → price lookup returns None,
    # but the prediction must still be logged.
    created = write_predictions(
        db,
        run_id=uuid4().hex,
        user_id=user.id,
        shortlisted=[_shortlist_result("ZZZ", 0.5)],
    )

    assert len(created) == 1
    row = db.query(DiscoveryPrediction).one()
    assert row.symbol == "ZZZ"
    assert row.price_at_prediction is None
    assert row.outcome_status == "pending"


def test_write_predictions_empty_shortlist_writes_nothing():
    db = _memory_db()
    user = User(id=uuid4().hex, username="u3", password_hash="x")
    db.add(user)
    db.commit()

    created = write_predictions(
        db, run_id=uuid4().hex, user_id=user.id, shortlisted=[]
    )
    assert created == []
    assert db.query(DiscoveryPrediction).count() == 0


def test_conviction_clamped_to_unit_interval():
    db = _memory_db()
    user = User(id=uuid4().hex, username="u4", password_hash="x")
    db.add(user)
    db.commit()

    created = write_predictions(
        db,
        run_id=uuid4().hex,
        user_id=user.id,
        shortlisted=[
            _shortlist_result("HI", 1.4),
            _shortlist_result("LO", -0.2),
        ],
    )
    by_symbol = {r.symbol: r for r in created}
    assert by_symbol["HI"].conviction == 1.0
    assert by_symbol["LO"].conviction == 0.0


def test_migration_applies_and_is_idempotent_on_fresh_sqlite():
    repo_backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "fresh.db")
        env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"}

        def _upgrade():
            return subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=repo_backend, env=env, capture_output=True, text=True,
            )

        first = _upgrade()
        assert first.returncode == 0, first.stderr
        # Running again must be a no-op (idempotent).
        second = _upgrade()
        assert second.returncode == 0, second.stderr

        # Table exists with the expected columns.
        engine = create_engine(f"sqlite:///{db_path}")
        from sqlalchemy import inspect as sa_inspect
        cols = {c["name"] for c in sa_inspect(engine).get_columns("discovery_prediction")}
        engine.dispose()
        expected = {
            "id", "user_id", "run_id", "asset_id", "symbol", "isin",
            "predicted_at", "horizon_days", "resolve_at", "direction",
            "conviction", "conviction_calibrated", "expected_return",
            "expected_return_low", "expected_return_high", "thesis", "risks",
            "features_json", "price_at_prediction", "realised_return",
            "outcome_status", "score_json",
        }
        assert expected <= cols
