"""Descriptive ranking metrics from the shadow ledger (ADR 0018 §5)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.verification.ranking_metrics import build_ranking_metrics
from app.foundation.core.db import Base
from app.foundation.models.entities import CandidateOutcome, DiscoverCandidateSnapshot, User


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _snap(db, user, run, day, symbol, *, composite, group="stock", evaluable=True, reject=None, picked=False,
          sector="Tech", backfilled=False):
    row = DiscoverCandidateSnapshot(
        user_id=user.id, run_id=run, issued_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        issue_date=day, symbol=symbol, instrument_group=group, sector=sector, evaluable=evaluable,
        reject_stage=reject, composite=composite if evaluable else None, components_json={},
        n_evaluable_stocks=40, shortlisted=picked, sector_capped=False, picked=picked,
        cohort_id="legacy_unstamped" if backfilled else "c1-x", provenance_json={}, backfilled=backfilled,
    )
    db.add(row)
    db.flush()
    return row


def _out(db, snap, horizon, ret, bench=0.01):
    db.add(CandidateOutcome(
        snapshot_id=snap.id, user_id=snap.user_id, run_id=snap.run_id, symbol=snap.symbol, horizon_days=horizon,
        target_date=snap.issue_date + timedelta(days=horizon), ret_eur=ret, bench_ret_eur=bench,
        excess_eur=None if ret is None else ret - bench, status="resolved" if ret is not None else "no_price",
        benchmark="EUNL.DE",
    ))


def _seed_run(db, user, run, day, *, strength, rng, backfilled=False):
    for i in range(40):
        comp = float(i) / 40
        snap = _snap(db, user, run, day, f"S{i}", composite=comp, picked=i >= 35, sector=("Tech", "Health")[i % 2],
                     backfilled=backfilled)
        for h in (5, 10, 21, 63):
            _out(db, snap, h, strength * comp * h / 21 + float(rng.normal(0, 0.01)))
    for i in range(12):
        snap = _snap(db, user, run, day, f"E{i}", composite=float(i), group="etf", backfilled=backfilled)
        _out(db, snap, 21, 0.001 * i)
    for i in range(5):
        snap = _snap(db, user, run, day, f"R{i}", composite=None, evaluable=False, reject="liquidity",
                     backfilled=backfilled)
        _out(db, snap, 21, -0.03)


def test_metrics_measure_a_ranking_that_works_and_keep_backfill_apart():
    db = _memory_db()
    user = User(username="rank", password_hash="x")
    db.add(user)
    db.commit()
    rng = np.random.default_rng(1)
    for k in range(20):
        _seed_run(db, user, f"run{k}", date(2026, 10, 12) + timedelta(weeks=k), strength=0.1, rng=rng)
    for k in range(3):
        _seed_run(db, user, f"old{k}", date(2026, 6, 1) + timedelta(weeks=k), strength=-0.1, rng=rng, backfilled=True)
    # A small run (fewer than 30 evaluable stocks) is not a cohort.
    for i in range(10):
        _snap(db, user, "tiny", date(2026, 10, 13), f"T{i}", composite=float(i))
    db.commit()

    m = build_ranking_metrics(db, user.id)
    live = m["live"]

    assert live["n_runs"] == 21 and live["n_cohort_runs"] == 20
    assert live["ic"]["n_runs"] == 20 and live["ic"]["mean"] > 0.5
    assert live["ic"]["t_nw"] > 3 and live["ic"]["share_positive"] == 1.0
    assert [d["horizon_days"] for d in live["ic_decay"]] == [5, 10, 21, 63]
    assert all(d["mean_ic"] > 0 for d in live["ic_decay"])
    assert live["sector_neutral_ic"]["mean"] > 0.3
    q = [x["mean_excess"] for x in live["quintiles"]]
    assert q[0] > q[-1]  # quintile 1 = highest composites
    assert live["picked_vs_rest"]["mean_gap"] > 0 and live["picked_vs_rest"]["ci"] is not None
    gate = live["gate_check"][0]
    assert gate["reject_stage"] == "liquidity" and gate["n_runs"] == 20 and gate["mean_gap"] < 0
    assert live["etf_ic"]["n_runs"] == 20 and live["etf_ic"]["mean"] == pytest.approx(1.0)

    explo = m["exploratory"]
    assert explo["n_runs"] == 3 and explo["ic"]["mean"] < 0  # never mixed into the live numbers


def test_an_empty_ledger_reports_nothing_rather_than_zeros():
    db = _memory_db()
    user = User(username="none", password_hash="x")
    db.add(user)
    db.commit()

    m = build_ranking_metrics(db, user.id)

    assert m["exploratory"] is None
    assert m["live"]["n_runs"] == 0 and m["live"]["ic"]["mean"] is None
    assert m["live"]["picked_vs_rest"]["mean_gap"] is None and m["live"]["gate_check"] == []


def test_ranking_endpoint_serves_the_metrics_for_the_signed_in_user():
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user = User(username="api", password_hash="x")
    db.add(user)
    db.commit()
    _seed_run(db, user, "r1", date(2026, 10, 12), strength=0.1, rng=np.random.default_rng(2))
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        body = TestClient(app).get("/api/trust/ranking").json()
    finally:
        app.dependency_overrides.clear()

    assert body["live"]["ic"]["n_runs"] == 1 and body["exploratory"] is None
    assert body["min_stocks"] == 30 and "Descriptive only" in body["verdict_note"]


def test_score_tiers_report_run_weighted_hit_rates_with_the_base_rate():
    db = _memory_db()
    user = User(username="tiers", password_hash="x")
    db.add(user)
    db.commit()
    rng = np.random.default_rng(3)
    for k in range(12):
        _seed_run(db, user, f"t{k}", date(2026, 10, 12) + timedelta(weeks=k), strength=0.1, rng=rng)
    db.commit()

    tiers = build_ranking_metrics(db, user.id)["live"]["tiers"]

    assert tiers["n_eff"] == 12
    top, middle, bottom = tiers["tiers"]
    assert (top["tier"], middle["tier"], bottom["tier"]) == ("top", "middle", "bottom")
    assert top["hit_rate"] > tiers["base"]["hit_rate"] > bottom["hit_rate"]
    lo, hi = top["range"]
    assert 0.5 < lo < hi <= 1.0  # 12 effective labels: a wide range even at a perfect record
