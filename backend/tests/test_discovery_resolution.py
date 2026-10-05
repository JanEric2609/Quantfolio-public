"""Tests for the Discovery resolution + scoring pipeline (Phase 2 rules).

Covers:
- ``resolve_due_predictions`` measures each prediction from the last close
  before the prediction day to the first close on/after the horizon, in EUR,
  against the passive core; it waits for a missing horizon close and gives up
  after the grace period;
- ``score_batch`` judges hits on the excess return, computes rank IC per
  prediction date, and takes the Brier score on the calibrated value;
- ``write_skill_snapshot`` / ``skill_summary`` aggregate per-date ICs.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import numpy as np
import pytest
from conftest import _memory_db

from app.decision.discover.resolution import load_closes, resolve_due_predictions
from app.decision.discover.scoring import (
    MIN_IC_NAMES,
    hit_rate_interval,
    ic_summary,
    independent_windows,
    overlap_lags,
    score_batch,
)
from app.foundation.quant_metrics import newey_west_t_stat
from app.decision.discover.skill_snapshot import skill_summary, write_skill_snapshot
from app.foundation.models.entities import DiscoveryPrediction, DiscoverySkillSnapshot, PriceCache, User
from app.foundation.models.entities._core import now_utc
from app.foundation.settings import upsert_public_settings

# Prediction made Monday 2026-08-03 05:00 UTC; 21 trading days -> 2026-09-01.
PREDICTED_AT = datetime(2026, 8, 3, 5, 0, tzinfo=UTC)
RESOLVE_AT = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)

ENTRY = date(2026, 7, 31)  # Friday: the last close before the prediction day
EXIT = date(2026, 9, 1)


def _user(db):
    user = User(id=uuid4().hex, username=uuid4().hex, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _prediction(
    db,
    user,
    symbol: str,
    direction: str = "buy",
    conviction: float | None = 0.5,
    price_at_prediction: float | None = 100.0,
    predicted_at: datetime = PREDICTED_AT,
    resolve_at: datetime = RESOLVE_AT,
    **extra,
) -> DiscoveryPrediction:
    pred = DiscoveryPrediction(
        id=uuid4().hex,
        user_id=user.id,
        run_id=uuid4().hex,
        symbol=symbol.upper(),
        predicted_at=predicted_at,
        horizon_days=21,
        resolve_at=resolve_at,
        direction=direction,
        conviction=conviction,
        price_at_prediction=price_at_prediction,
        outcome_status=extra.pop("outcome_status", "pending"),
        **extra,
    )
    db.add(pred)
    db.commit()
    return pred


def _loader(series: dict[str, dict[date, float]]):
    """A price loader over fixed closes, keyed by symbol."""

    def load(_db, symbol: str, _days: int):
        return sorted(series.get(symbol.upper(), {}).items())

    return load


def _flat_fx(pair: str, rate: float = 1.0) -> dict[str, dict[date, float]]:
    return {pair: {ENTRY: rate, EXIT: rate}}


# --- resolution ----------------------------------------------------------------


def test_resolves_on_the_horizon_close_against_the_benchmark():
    db = _memory_db()
    user = _user(db)
    _prediction(db, user, "SAP.DE")
    _prediction(db, user, "BAYN.DE", direction="sell")

    prices = {
        "SAP.DE": {ENTRY: 100.0, date(2026, 8, 3): 101.0, EXIT: 110.0, date(2026, 9, 2): 150.0},
        "BAYN.DE": {ENTRY: 50.0, EXIT: 45.0},
        "EUNL.DE": {ENTRY: 80.0, EXIT: 84.0},
    }
    resolved = resolve_due_predictions(db, now=NOW, loader=_loader(prices))

    assert len(resolved) == 2
    rows = {r.symbol: r for r in db.query(DiscoveryPrediction).all()}

    sap = rows["SAP.DE"]
    assert sap.outcome_status == "resolved"
    # 100 -> 110 (the horizon close, not the later 150), benchmark 80 -> 84.
    assert sap.realised_return == pytest.approx(0.10)
    assert sap.benchmark_return == pytest.approx(0.05)
    assert sap.excess_return == pytest.approx(0.05)
    outcome = sap.score_json["outcome"]
    assert outcome["entry_date"] == "2026-07-31" and outcome["exit_date"] == "2026-09-01"
    assert outcome["benchmark"] == "EUNL.DE"
    assert outcome["hit_basis"] == "excess"
    assert outcome["return_currency"] == "EUR"

    # A sell bets on a fall relative to the core: -10 % vs +5 % is +15 % excess.
    bayn = rows["BAYN.DE"]
    assert bayn.realised_return == pytest.approx(0.10)
    assert bayn.excess_return == pytest.approx(0.15)


def test_non_eur_listing_is_converted_to_eur():
    db = _memory_db()
    user = _user(db)
    _prediction(db, user, "AAPL")

    prices = {
        "AAPL": {ENTRY: 200.0, EXIT: 220.0},  # +10 % in USD
        "USDEUR=X": {ENTRY: 0.90, EXIT: 0.81},  # the dollar lost 10 %
        "EUNL.DE": {ENTRY: 80.0, EXIT: 80.0},
    }
    resolve_due_predictions(db, now=NOW, loader=_loader(prices))

    row = db.query(DiscoveryPrediction).one()
    assert row.realised_return == pytest.approx(1.10 * 0.9 - 1.0)  # -1 % in EUR
    assert row.excess_return == pytest.approx(-0.01)
    assert row.score_json["outcome"]["local_return"] == pytest.approx(0.10)
    assert row.score_json["outcome"]["currency"] == "USD"


def test_a_dollar_line_on_the_lse_is_converted_from_dollars():
    """IWDA.L trades in USD on the LSE. The listing suffix alone said GBP,
    so its realised return was converted with GBPEUR; the recorded provider
    currency is used instead."""
    from app.foundation.data_backbone.listing_currency import record_listing_currency

    db = _memory_db()
    user = _user(db)
    _prediction(db, user, "IWDA.L")
    record_listing_currency(db, "IWDA.L", "USD", "yfinance_metadata")
    db.commit()

    prices = {
        "IWDA.L": {ENTRY: 100.0, EXIT: 110.0},
        "USDEUR=X": {ENTRY: 0.90, EXIT: 0.81},
        "GBPEUR=X": {ENTRY: 1.20, EXIT: 1.20},
        "EUNL.DE": {ENTRY: 80.0, EXIT: 80.0},
    }
    resolve_due_predictions(db, now=NOW, loader=_loader(prices))

    row = db.query(DiscoveryPrediction).one()
    assert row.score_json["outcome"]["currency"] == "USD"
    assert row.realised_return == pytest.approx(1.10 * 0.9 - 1.0)


def test_missing_fx_keeps_the_local_return_and_says_so():
    db = _memory_db()
    user = _user(db)
    _prediction(db, user, "AAPL")

    prices = {"AAPL": {ENTRY: 200.0, EXIT: 220.0}, "EUNL.DE": {ENTRY: 80.0, EXIT: 80.0}}
    resolve_due_predictions(db, now=NOW, loader=_loader(prices))

    row = db.query(DiscoveryPrediction).one()
    assert row.realised_return == pytest.approx(0.10)
    assert row.score_json["outcome"]["return_currency"] == "USD"


def test_waits_for_a_missing_horizon_close_then_gives_up_after_grace():
    db = _memory_db()
    user = _user(db)
    _prediction(db, user, "SAP.DE")
    stale = {"SAP.DE": {ENTRY: 100.0, date(2026, 8, 28): 104.0}, "EUNL.DE": {ENTRY: 80.0, EXIT: 84.0}}

    assert resolve_due_predictions(db, now=NOW, loader=_loader(stale)) == []
    assert db.query(DiscoveryPrediction).one().outcome_status == "pending"

    later = RESOLVE_AT + timedelta(days=8)
    resolved = resolve_due_predictions(db, now=later, loader=_loader(stale))
    assert len(resolved) == 1
    row = db.query(DiscoveryPrediction).one()
    assert row.outcome_status == "delisted"
    # Scored at its last close (a delisting return), not dropped: survivorship.
    assert row.realised_return == pytest.approx(0.04)
    assert row.score_json["outcome"]["reason"] == "no_close_after_horizon"
    assert row.score_json["outcome"]["exit_basis"] == "last_close"


def test_no_data_at_all_is_delisted_after_grace():
    db = _memory_db()
    user = _user(db)
    _prediction(db, user, "NOPE")

    assert resolve_due_predictions(db, now=NOW, loader=_loader({})) == []
    resolve_due_predictions(db, now=RESOLVE_AT + timedelta(days=8), loader=_loader({}))

    row = db.query(DiscoveryPrediction).one()
    assert row.outcome_status == "delisted"
    assert row.score_json["outcome"]["reason"] == "no_price_data"


def test_missing_benchmark_waits_then_falls_back_to_the_raw_return():
    db = _memory_db()
    user = _user(db)
    _prediction(db, user, "SAP.DE")
    prices = {"SAP.DE": {ENTRY: 100.0, EXIT: 110.0}}

    assert resolve_due_predictions(db, now=NOW, loader=_loader(prices)) == []

    resolve_due_predictions(db, now=RESOLVE_AT + timedelta(days=8), loader=_loader(prices))
    row = db.query(DiscoveryPrediction).one()
    assert row.outcome_status == "resolved"
    assert row.realised_return == pytest.approx(0.10)
    assert row.excess_return is None
    assert row.score_json["outcome"]["hit_basis"] == "raw"


def test_stored_price_is_the_entry_fallback_when_history_is_short():
    db = _memory_db()
    user = _user(db)
    _prediction(db, user, "SAP.DE", price_at_prediction=100.0)
    prices = {"SAP.DE": {EXIT: 120.0}, "EUNL.DE": {ENTRY: 80.0, EXIT: 80.0}}

    resolve_due_predictions(db, now=NOW, loader=_loader(prices))

    row = db.query(DiscoveryPrediction).one()
    assert row.realised_return == pytest.approx(0.20)
    assert row.score_json["outcome"]["entry_basis"] == "stored_price"


def test_benchmark_follows_the_passive_core_setting():
    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {"passive_core_ticker": "VWCE.DE"})
    _prediction(db, user, "SAP.DE")
    prices = {"SAP.DE": {ENTRY: 100.0, EXIT: 110.0}, "VWCE.DE": {ENTRY: 100.0, EXIT: 102.0}}

    resolve_due_predictions(db, now=NOW, loader=_loader(prices))

    row = db.query(DiscoveryPrediction).one()
    assert row.score_json["outcome"]["benchmark"] == "VWCE.DE"
    assert row.excess_return == pytest.approx(0.08)


def test_not_yet_due_predictions_are_untouched():
    db = _memory_db()
    user = _user(db)
    _prediction(db, user, "SAP.DE", resolve_at=NOW + timedelta(days=3))

    assert resolve_due_predictions(db, now=NOW, loader=_loader({})) == []
    assert db.query(DiscoveryPrediction).one().outcome_status == "pending"


def test_load_closes_reads_the_price_cache():
    db = _memory_db()
    for day, close in ((date.today() - timedelta(days=2), 10.0), (date.today() - timedelta(days=1), 11.0)):
        db.add(PriceCache(
            id=uuid4().hex, ticker="SAP.DE", date=day, close=Decimal(str(close)),
            fetched_at=now_utc(), source="test", stale=False, currency="EUR",
        ))
    db.commit()

    closes = load_closes(db, "SAP.DE", 30)

    assert [c for _, c in closes] == [10.0, 11.0]


# --- scoring -------------------------------------------------------------------


def _item(pred: DiscoveryPrediction, *, excess: float | None, realised: float, calibrated=None) -> dict:
    return {
        "prediction_id": pred.id,
        "user_id": pred.user_id,
        "portfolio_id": pred.portfolio_id,
        "direction": pred.direction,
        "symbol": pred.symbol,
        "predicted_on": pred.predicted_at.date().isoformat(),
        "conviction": pred.conviction,
        "conviction_calibrated": calibrated,
        "realised_return": realised,
        "excess_return": excess,
        "outcome_status": "resolved",
    }


def test_a_hit_means_beating_the_benchmark():
    db = _memory_db()
    user = _user(db)
    up_but_behind = _prediction(db, user, "A", conviction=0.8, outcome_status="resolved")
    down_but_ahead = _prediction(db, user, "B", conviction=0.3, outcome_status="resolved")

    score_batch(db, [
        _item(up_but_behind, excess=-0.01, realised=0.05),
        _item(down_but_ahead, excess=0.02, realised=-0.03),
    ])

    rows = {r.symbol: r.score_json for r in db.query(DiscoveryPrediction).all()}
    assert rows["A"]["hit"] == 0 and rows["A"]["hit_basis"] == "excess"
    assert rows["B"]["hit"] == 1
    # Raw score as the "probability" is labelled as such.
    assert rows["A"]["brier"] == pytest.approx(0.64)
    assert rows["A"]["brier_basis"] == "raw_score"


def test_brier_uses_the_calibrated_probability_when_present():
    db = _memory_db()
    user = _user(db)
    pred = _prediction(db, user, "A", conviction=0.9, outcome_status="resolved")

    score_batch(db, [_item(pred, excess=0.01, realised=0.02, calibrated=0.6)])

    sj = db.query(DiscoveryPrediction).one().score_json
    assert sj["brier"] == pytest.approx(0.16)
    assert sj["brier_basis"] == "calibrated"


def test_rank_ic_is_computed_per_prediction_date():
    db = _memory_db()
    user = _user(db)
    week1 = PREDICTED_AT
    week2 = PREDICTED_AT + timedelta(days=7)
    items = []
    # Week 1: conviction orders the excess returns perfectly (IC +1).
    for i in range(MIN_IC_NAMES):
        p = _prediction(db, user, f"W1{i}", conviction=0.1 * (i + 1), predicted_at=week1, outcome_status="resolved")
        items.append(_item(p, excess=0.01 * i, realised=0.10 + 0.01 * i))
    # Week 2: reversed (IC -1), and every name is far up in raw terms.
    for i in range(MIN_IC_NAMES):
        p = _prediction(db, user, f"W2{i}", conviction=0.1 * (i + 1), predicted_at=week2, outcome_status="resolved")
        items.append(_item(p, excess=-0.01 * i, realised=0.30 - 0.01 * i))
    # Week 3: too few names for an IC.
    thin = _prediction(db, user, "W30", predicted_at=week1 + timedelta(days=14), outcome_status="resolved")
    items.append(_item(thin, excess=0.0, realised=0.0))

    score_batch(db, items)

    rows = {r.symbol: r.score_json for r in db.query(DiscoveryPrediction).all()}
    assert rows["W10"]["rank_ic"] == pytest.approx(1.0)
    assert rows["W10"]["rank_ic_n"] == MIN_IC_NAMES
    assert rows["W20"]["rank_ic"] == pytest.approx(-1.0)
    assert rows["W30"]["rank_ic"] is None

    snapshot = write_skill_snapshot(db, items)
    assert snapshot is not None
    # Mean of +1 and -1; a pooled correlation would have mixed the weeks.
    assert snapshot.rank_ic == pytest.approx(0.0)
    assert set(snapshot.details_json["ic_by_date"]) == {"2026-08-03", "2026-08-10"}
    assert snapshot.details_json["hit_basis"] == "excess"
    assert snapshot.icir == pytest.approx(0.0)


def test_advisor_rows_are_their_own_cross_section_and_holds_carry_no_ic():
    db = _memory_db()
    user = _user(db)
    items = []
    # Discover's shortlist: composite score orders the outcome (IC +1).
    for i in range(MIN_IC_NAMES):
        p = _prediction(
            db, user, f"D{i}", conviction=0.1 * (i + 1), outcome_status="resolved",
            realised_return=0.01 * i, excess_return=0.01 * i,
        )
        items.append(_item(p, excess=0.01 * i, realised=0.01 * i))
    # One advisor sleeve the same day, confidence reversed (IC -1), plus a
    # hold that would have broken the ranking.
    for i in range(MIN_IC_NAMES):
        p = _prediction(
            db, user, f"A{i}", conviction=0.1 * (i + 1), outcome_status="resolved", portfolio_id="sleeve-1",
            realised_return=-0.01 * i, excess_return=-0.01 * i,
        )
        items.append(_item(p, excess=-0.01 * i, realised=-0.01 * i))
    hold = _prediction(
        db, user, "H", direction="neutral", conviction=0.99, outcome_status="resolved", portfolio_id="sleeve-1"
    )
    items.append(_item(hold, excess=-0.5, realised=-0.5))

    score_batch(db, items)

    rows = {r.symbol: r.score_json for r in db.query(DiscoveryPrediction).all()}
    assert rows["D0"]["rank_ic"] == pytest.approx(1.0)
    assert rows["A0"]["rank_ic"] == pytest.approx(-1.0)
    assert rows["A0"]["rank_ic_n"] == MIN_IC_NAMES
    assert rows["H"]["rank_ic"] is None

    # Discover's skill ignores the advisor's rows entirely.
    snapshot = write_skill_snapshot(db, items)
    assert snapshot is not None
    assert snapshot.total_predictions == MIN_IC_NAMES
    assert snapshot.rank_ic == pytest.approx(1.0)
    summary = skill_summary(db, user.id)
    assert summary["resolved"] == MIN_IC_NAMES
    assert summary["mean_rank_ic"] == pytest.approx(1.0)
    # An advisor-only batch writes no Discover snapshot.
    assert write_skill_snapshot(db, [i for i in items if i["portfolio_id"]]) is None


def test_ic_summary_statistics():
    # Monthly dates do not overlap: a plain t.
    summary = ic_summary({"2026-01-05": (0.10, 20), "2026-02-05": (0.05, 20), "2026-03-09": (0.15, 20)})
    assert summary["mean_ic"] == pytest.approx(0.10)
    assert summary["icir"] == pytest.approx(2.0)
    assert summary["t_stat"] == pytest.approx(2.0 * 3 ** 0.5)
    assert summary["dates"] == 3
    assert summary["nw_lags"] == 0
    assert summary["independent_windows"] == 3
    assert ic_summary({})["mean_ic"] is None
    assert ic_summary({"2026-01-05": (0.1, 9)})["icir"] is None


def test_weekly_dates_overlap_so_the_t_is_newey_west():
    start = date(2026, 1, 5)
    ics = {(start + timedelta(weeks=i)).isoformat(): (0.1 + 0.05 * ((-1) ** i), 15) for i in range(12)}

    summary = ic_summary(ics)

    # A 29-day horizon holds four later weekly dates, and twelve weeks hold
    # three horizons that do not overlap.
    assert summary["nw_lags"] == 4
    assert summary["independent_windows"] == 3
    assert summary["t_stat"] == pytest.approx(newey_west_t_stat(np.array([v for v, _ in ics.values()]), 4))
    assert summary["t_stat"] != pytest.approx(summary["icir"] * 12 ** 0.5)


def test_independent_windows_and_overlap_lags():
    start = date(2026, 1, 1)
    daily = [start + timedelta(days=i) for i in range(60)]
    assert independent_windows(daily) == 3  # day 0, 29, 58
    assert overlap_lags(daily) == 28
    assert overlap_lags([start]) == 0


def test_hit_rate_interval_waits_for_independent_windows_and_clusters_by_date():
    start = date(2026, 1, 5)
    # Eleven months: not enough windows for an interval.
    short = [(start + timedelta(days=30 * m), bool(i % 2)) for m in range(11) for i in range(15)]
    assert hit_rate_interval(short) is None

    # Fifteen calls per month, all hit or all miss together: one market
    # window decides them, so the interval is far wider than 15 coin flips.
    together = [(start + timedelta(days=30 * m), m % 2 == 0) for m in range(24) for _ in range(15)]
    half = hit_rate_interval(together)
    binomial = 1.96 * (0.25 / len(together)) ** 0.5
    assert half is not None and half > 3 * binomial
    # Independent calls inside each month come out near the binomial width.
    mixed = [(start + timedelta(days=30 * m), i % 2 == 0) for m in range(24) for i in range(16)]
    assert hit_rate_interval(mixed) == pytest.approx(0.0, abs=binomial)


def test_empty_batch_is_a_no_op():
    db = _memory_db()
    score_batch(db, [])
    assert write_skill_snapshot(db, []) is None
    assert db.query(DiscoverySkillSnapshot).count() == 0


def test_skill_summary_counts_pending_and_judges_against_the_benchmark():
    db = _memory_db()
    user = _user(db)
    _prediction(db, user, "P1", resolve_at=datetime(2026, 10, 15, tzinfo=UTC))
    _prediction(db, user, "P2", resolve_at=datetime(2026, 10, 20, tzinfo=UTC))
    _prediction(db, user, "R1", outcome_status="resolved", realised_return=0.05, excess_return=-0.01)
    _prediction(db, user, "R2", outcome_status="resolved", realised_return=0.08, excess_return=0.03)

    summary = skill_summary(db, user.id)

    assert summary["benchmark"] == "EUNL.DE"
    assert summary["pending"] == 2
    assert summary["next_resolve_at"].startswith("2026-10-15")
    assert summary["resolved"] == 2
    assert summary["hit_rate"] == pytest.approx(0.5)  # raw returns would say 100 %
    assert summary["mean_excess_return"] == pytest.approx(0.01)
    assert summary["mean_rank_ic"] is None  # two names are not a cross-section


def test_skill_summary_endpoint():
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user = _user(db)
    _prediction(db, user, "P1", resolve_at=datetime(2026, 10, 15, tzinfo=UTC))
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        resp = TestClient(app).get("/api/discover/skill-summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pending"] == 1
        assert body["resolved"] == 0
        assert body["benchmark"] == "EUNL.DE"
    finally:
        app.dependency_overrides.clear()


def test_load_closes_catches_up_a_frozen_series(monkeypatch):
    """A pick that left the Discover universe keeps its stored bars; the
    nightly resolution must ask for a catch-up before reading them."""
    calls = []
    monkeypatch.setattr(
        "app.foundation.market.refresh_stale_bars",
        lambda db, symbol, max_age_days=3.0: calls.append((symbol, max_age_days)) or False,
    )
    db = _memory_db()
    load_closes(db, "OLD.PA", 30)
    assert calls == [("OLD.PA", 1.5)]
