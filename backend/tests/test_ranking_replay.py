"""A4: historical replay of Discover's non-AI ingredients (ADR 0019 §4)."""
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import TrialLedgerEntry
from app.foundation.quant_metrics import holm_bonferroni
from app.lab.ranking_replay import (
    NON_AI_INGREDIENTS,
    rank_ic_series,
    run_replay,
    summarise,
)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _frames(months: int = 60, noise: float = 0.3, start: str = "2020-01-31") -> dict:
    rng = np.random.default_rng(7)
    out = {}
    periods = pd.date_range(start, periods=months, freq="ME")
    for name in (*NON_AI_INGREDIENTS, "composite"):
        rows = []
        for p in periods:
            fwd = rng.normal(0, 0.05, size=20)
            score = fwd + rng.normal(0, noise, size=20)
            for i in range(20):
                rows.append({"period": p, "entity": f"S{i}", "score": score[i], "forward": fwd[i]})
        out[name] = pd.DataFrame(rows)
    return out


def test_perfect_rank_scores_ic_one():
    frame = pd.DataFrame([
        {"period": "2020-01-31", "entity": f"S{i}", "score": float(i), "forward": float(i * 2)}
        for i in range(10)
    ])
    ic = rank_ic_series(frame)
    assert len(ic) == 1 and ic.iloc[0] == pytest.approx(1.0)


def test_holm_adjusts_step_down():
    assert holm_bonferroni([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert holm_bonferroni([]) == []
    assert all(p <= 1.0 for p in holm_bonferroni([0.9, 0.8]))


def test_replay_verdict_yes_and_ledger_counts_each_test_once():
    db = _memory_db()
    report = run_replay(db, _frames(), persist=True)
    assert report["verdict"] == "yes"
    assert report["years"] == [2019, 2025]
    assert set(report["ingredients"]) == {*NON_AI_INGREDIENTS, "composite"}
    assert all("p_holm" in s for s in report["ingredients"].values())
    n = len(NON_AI_INGREDIENTS) + 1
    assert db.query(TrialLedgerEntry).filter(TrialLedgerEntry.context == "discover_replay").count() == n
    run_replay(db, _frames(), persist=True)
    assert db.query(TrialLedgerEntry).filter(TrialLedgerEntry.context == "discover_replay").count() == n


def test_noise_only_replay_verdict_no():
    db = _memory_db()
    report = run_replay(db, _frames(noise=5.0), persist=False)
    assert report["verdict"] == "no"


def test_years_outside_the_lock_are_refused():
    db = _memory_db()
    with pytest.raises(ValueError, match="locked exam"):
        run_replay(db, _frames(), years=(2015, 2025), persist=False)
    late = _frames(start="2026-01-31")
    with pytest.raises(ValueError, match="inside the locked"):
        run_replay(db, late, persist=False)


def test_ai_ingredients_are_refused():
    db = _memory_db()
    frames = _frames()
    frames["ml_signal"] = frames["momentum"]
    with pytest.raises(ValueError, match="not replayable"):
        run_replay(db, frames, persist=False)


def test_summarise_needs_periods():
    assert summarise(pd.Series([0.1]))["t_stat"] is None
