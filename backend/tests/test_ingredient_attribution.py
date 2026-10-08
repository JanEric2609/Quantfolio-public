"""A5: weekly per-ingredient table from the shadow ledger (ADR 0019 §5)."""
from datetime import UTC, date, datetime

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import CandidateOutcome, DiscoverCandidateSnapshot, User
from app.interface.api.evidence import (
    INGREDIENT_ATTRIBUTION_HORIZON_DAYS,
    ingredient_attribution,
)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed(db, user_id: str, n_runs: int = 6, n_stocks: int = 12) -> None:
    rng = np.random.default_rng(3)
    for w in range(n_runs):
        run_id = f"run-{w}"
        market = rng.normal(0, 0.03)
        for i in range(n_stocks):
            excess = market + rng.normal(0, 0.02)
            snap = DiscoverCandidateSnapshot(
                user_id=user_id, run_id=run_id,
                issued_at=datetime(2026, 9, 1, tzinfo=UTC), issue_date=date(2026, 9, 1),
                symbol=f"S{i}", instrument_group="stock", evaluable=True,
                composite=0.5, stock_rank=i + 1, n_evaluable_stocks=n_stocks,
                components_json={"composite_inputs": [
                    {"signal": "momentum", "score": float(excess * 10 + rng.normal(0, 0.05)),
                     "weight": 0.2, "contribution": 0.1},
                    {"signal": "risk", "score": float(rng.normal(0.5, 0.1)),
                     "weight": 0.1, "contribution": 0.05},
                ]},
                entry_close=100.0, entry_close_date=date(2026, 9, 1),
                cohort_id="test",
            )
            db.add(snap)
            db.flush()
            db.add(CandidateOutcome(
                snapshot_id=snap.id, user_id=user_id, run_id=run_id, symbol=f"S{i}",
                horizon_days=INGREDIENT_ATTRIBUTION_HORIZON_DAYS,
                target_date=date(2026, 10, 1), ret_eur=excess, bench_ret_eur=0.0,
                excess_eur=excess, status="resolved", benchmark="EUNL.DE",
            ))
    db.commit()


def test_attribution_table_with_holm_and_effective_n():
    db = _memory_db()
    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()
    _seed(db, user.id)

    resp = ingredient_attribution(db=db, user=user)
    assert resp.horizon_days == INGREDIENT_ATTRIBUTION_HORIZON_DAYS
    assert resp.n_runs == 6
    assert resp.n_stocks == 12
    signals = {row.signal: row for row in resp.ingredients}
    assert set(signals) == {"momentum", "risk"}
    assert signals["momentum"].mean_ic is not None and signals["momentum"].mean_ic > 0
    assert all(row.p_holm is not None for row in resp.ingredients)
    assert resp.rho_bar is not None and resp.rho_bar >= 0
    assert resp.effective_n_stocks is not None
    assert resp.effective_n_stocks < resp.n_stocks


def test_empty_ledger_gives_an_empty_table():
    db = _memory_db()
    user = User(username="jan", password_hash="x")
    db.add(user)
    db.commit()
    resp = ingredient_attribution(db=db, user=user)
    assert resp.ingredients == [] and resp.n_runs == 0
    assert resp.effective_n_stocks is None
