"""The live estimate-revision source (data_backbone.analyst_estimates).

Discover's estimate-revision signal read a hand-exported WRDS IBES extract;
past its 92-day staleness limit it was empty. Yahoo's EPS trend gives the
current-year consensus today and 30 days ago, for European names too.
"""
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from conftest import _memory_db

from app.decision.discover import pipeline
from app.decision.discover.jobs import analyst_estimates_snapshot_inner
from app.foundation.data_backbone.analyst_estimates import (
    live_revision,
    revision_from,
    snapshot,
    snapshot_many,
)
from app.foundation.models.entities import AnalystEstimateSnapshot, DiscoverCandidate, DiscoverRun, User

TODAY = date(2026, 9, 29)

SAP = {
    "eps_current": 7.137, "eps_7d_ago": 7.134, "eps_30d_ago": 7.0, "eps_60d_ago": 7.169, "eps_90d_ago": 7.242,
    "analysts": 23, "currency": "EUR",
}


def _fetch(values):
    calls: list[str] = []

    def fetch(symbol):
        calls.append(symbol)
        return values

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_the_revision_is_the_30_day_change_in_consensus():
    db = _memory_db()
    result = live_revision(db, "sap.de", fetch=_fetch(SAP), today=TODAY)

    assert result is not None
    assert result["revision_momentum"] == pytest.approx((7.137 - 7.0) / 7.0)
    assert result["analysts"] == 23 and result["source"] == "yfinance_eps_trend"
    stored = db.query(AnalystEstimateSnapshot).one()
    assert stored.symbol == "SAP.DE" and stored.snapshot_date == TODAY and stored.currency == "EUR"


def test_a_recent_snapshot_is_reused():
    db = _memory_db()
    fetch = _fetch(SAP)
    snapshot(db, "SAP.DE", fetch=fetch, today=TODAY)
    snapshot(db, "SAP.DE", fetch=fetch, today=TODAY + timedelta(days=2))
    snapshot(db, "SAP.DE", fetch=fetch, today=TODAY + timedelta(days=5))

    assert fetch.calls == ["SAP.DE", "SAP.DE"]  # type: ignore[attr-defined]
    assert db.query(AnalystEstimateSnapshot).count() == 2


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"analysts": 2}, "two estimates are not a consensus"),
        ({"eps_30d_ago": 3.0}, "+138% in a month: a fiscal-year roll"),
        ({"eps_30d_ago": None}, "no earlier figure"),
        ({"eps_30d_ago": 0.0}, "no base to divide by"),
    ],
)
def test_unusable_consensus_gives_no_signal(change, reason):
    row = AnalystEstimateSnapshot(symbol="X", snapshot_date=TODAY, period="0y", **{**SAP, **change})
    assert revision_from(row) is None, reason


def test_no_eps_trend_is_no_data_and_is_not_stored():
    db = _memory_db()
    assert live_revision(db, "EUNL.DE", fetch=_fetch(None), today=TODAY) is None
    assert db.query(AnalystEstimateSnapshot).count() == 0


def test_the_stage_prefers_ibes_and_falls_back_to_the_live_consensus(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr(
        "app.foundation.data_backbone.analyst_estimates.fetch_eps_trend", _fetch(SAP),
    )
    ibes = pd.DataFrame(
        {"sue": [0.8], "revision_momentum": [0.03], "dispersion": [0.1], "data_confidence": ["ticker_match"]}
    )
    monkeypatch.setattr(pipeline, "pit_ibes_estimate_signal_for_symbol", lambda *_a: ibes)
    fresh, _ = pipeline.stage_estimate_revision_signal(db, "SAP.DE")
    assert fresh is not None and fresh["estimate_source"] == "ibes" and fresh["sue"] == 0.8

    stale = ibes.assign(sue=[float("nan")], revision_momentum=[float("nan")])
    monkeypatch.setattr(pipeline, "pit_ibes_estimate_signal_for_symbol", lambda *_a: stale)
    live, _ = pipeline.stage_estimate_revision_signal(db, "SAP.DE")
    assert live is not None and live["estimate_source"] == "yfinance_eps_trend"
    assert live["sue"] is None and live["revision_momentum"] == pytest.approx(0.1371 / 7.0, rel=1e-3)
    assert live["dispersion"] == 0.1 and live["concerns"] == []


def test_the_stage_without_any_source_is_unavailable(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr(pipeline, "pit_ibes_estimate_signal_for_symbol", lambda *_a: None)
    result, _ = pipeline.stage_estimate_revision_signal(db, "EUNL.DE")
    assert result is not None and result["concerns"] == ["estimate_data_unavailable"]
    assert result["estimate_source"] is None


def test_the_nightly_job_snapshots_the_latest_runs_candidates(monkeypatch):
    db = _memory_db()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    old = DiscoverRun(user_id=user.id, status="completed", created_at=datetime(2026, 9, 20, tzinfo=UTC))
    db.add(old)
    db.commit()
    latest = DiscoverRun(user_id=user.id, status="completed", created_at=datetime(2026, 9, 28, tzinfo=UTC))
    db.add(latest)
    db.commit()
    db.add_all([
        DiscoverCandidate(run_id=old.id, symbol="OLD", source="index_screen", status="shortlisted"),
        DiscoverCandidate(run_id=latest.id, symbol="SAP.DE", source="index_screen", status="shortlisted"),
        DiscoverCandidate(run_id=latest.id, symbol="EUNL.DE", source="etf_screen", status="rejected"),
    ])
    db.commit()
    seen: list[str] = []
    monkeypatch.setattr(
        "app.foundation.data_backbone.analyst_estimates.fetch_eps_trend",
        lambda s: seen.append(s) or (SAP if s == "SAP.DE" else None),
    )

    counts = analyst_estimates_snapshot_inner(db)

    assert sorted(seen) == ["EUNL.DE", "SAP.DE"]
    assert counts == {"stored_or_recent": 1, "no_data": 1}


def test_snapshot_many_skips_blanks_and_duplicates():
    db = _memory_db()
    fetch = _fetch(SAP)
    counts = snapshot_many(db, ["SAP.DE", "sap.de", "", "  "], fetch=fetch, today=TODAY)
    assert counts == {"stored_or_recent": 1, "no_data": 0}
    assert fetch.calls == ["SAP.DE"]  # type: ignore[attr-defined]
