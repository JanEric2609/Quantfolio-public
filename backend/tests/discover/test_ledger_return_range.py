"""Discover's ledger rows carry the P10/P90 range their estimate was stated with.

``DiscoveryPrediction.expected_return_low/high`` were only ever filled by the
advisor cycle (Monte Carlo p5/p95). Discover already prints a block-bootstrap
band on the dossier; the ledger gets the same band over its own 21-trading-day
horizon, centred on its own point estimate, so coverage can be scored later.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from conftest import _memory_db

from app.decision.discover import orchestrator
from app.decision.discover.dossier_writer import ledger_return_range
from app.foundation.models.entities import DiscoverCandidate, DiscoverRun, PriceCache
from app.foundation.return_band import compute_return_band


@pytest.fixture(autouse=True)
def _no_earnings_lookup(monkeypatch):
    monkeypatch.setattr(
        "app.decision.discover.orchestrator.attach_earnings_calendar",
        lambda db, symbol, scores: scores,
    )


def _seed_prices(db, ticker: str, closes: list[float]) -> None:
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


def _closes(n: int, seed: int = 7, drift: float = 0.0004, vol: float = 0.012) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(100.0 * np.cumprod(np.exp(rng.normal(drift, vol, size=n))))


def test_band_can_be_stated_over_an_explicit_horizon_and_centre():
    db = _memory_db()
    _seed_prices(db, "RNGA", _closes(800))

    band = compute_return_band(db, "RNGA", 1, 0.0, horizon_steps=21, target_total=0.006)

    assert band is not None and band["method"] == "block_bootstrap"
    assert band["p10"] < band["p50"] < band["p90"]
    # Log-space centring puts the median on the requested total return.
    assert band["p50"] == pytest.approx(0.006, abs=1e-4)
    # 21 trading days of ~1.2 % daily volatility: roughly +/-7 % at P10/P90.
    assert 0.04 < (band["p90"] - band["p10"]) / 2 < 0.12


def test_band_default_behaviour_is_unchanged_without_the_overrides():
    db = _memory_db()
    _seed_prices(db, "RNGB", _closes(800, seed=3))

    a = compute_return_band(db, "RNGB", 6, 0.08)
    b = compute_return_band(db, "RNGB", 6, 0.08, horizon_steps=None, target_total=None)

    assert a == b


def test_ledger_return_range_brackets_the_point_estimate():
    db = _memory_db()
    _seed_prices(db, "RNGC", _closes(800, seed=5))

    low, high = ledger_return_range(db, "RNGC", 0.0055, 21)

    assert low is not None and high is not None
    assert low < 0.0055 < high


def test_ledger_return_range_is_none_without_an_estimate_or_history():
    db = _memory_db()
    _seed_prices(db, "RNGD", _closes(800))

    assert ledger_return_range(db, "RNGD", None, 21) == (None, None)
    assert ledger_return_range(db, "RNGD", -1.0, 21) == (None, None)
    assert ledger_return_range(db, "NOSUCH", 0.005, 21) == (None, None)
    assert ledger_return_range(None, "RNGD", 0.005, 21) == (None, None)


def test_ledger_return_range_skips_the_uncentred_short_history_fallback():
    db = _memory_db()
    _seed_prices(db, "RNGE", _closes(120))  # < 250 returns -> GBM fallback band

    assert ledger_return_range(db, "RNGE", 0.005, 21) == (None, None)


def test_generate_dossiers_threads_the_range_into_the_ledger_fields(monkeypatch):
    db = _memory_db()
    _seed_prices(db, "MS", _closes(800, seed=9))
    run = DiscoverRun(id=str(uuid.uuid4()), user_id="user1", status="running", stage_json="{}", params_json="{}")
    db.add(run)
    cand = DiscoverCandidate(
        id=str(uuid.uuid4()), run_id=run.id, symbol="MS", source="screen_index", status="shortlisted",
        scores_json=json.dumps({"composite": 0.63}), tradeable_json=json.dumps({"likely_tradeable": True}),
    )
    db.add(cand)
    db.commit()

    def fake_write_dossier(*_args, **_kwargs):
        return {
            "dossier_id": str(uuid.uuid4()),
            "recommendation_id": str(uuid.uuid4()),
            "dossier": {"expected_return": 3.4, "thesis": "Fees recover."},
            "generated_by": "llm",
            "fallback_reason": None,
            "annual_estimate": {"prior_annual": 0.07, "tilt_annual": -0.002, "anchor_kind": "trailing_3y_cagr"},
        }

    monkeypatch.setattr("app.foundation.core.db.SessionLocal", sessionmaker(bind=db.get_bind(), autoflush=False))
    monkeypatch.setattr(orchestrator, "write_dossier", fake_write_dossier)

    _count, fields = orchestrator._generate_dossiers(
        db, run, [cand], {"user_id": "user1"},
        prompt_config=None, cancel_token=orchestrator.CancellationToken(db, run.id), started_at=time.time(),
    )

    expected = fields["MS"]["expected_return"]
    assert fields["MS"]["expected_return_low"] < expected < fields["MS"]["expected_return_high"]
