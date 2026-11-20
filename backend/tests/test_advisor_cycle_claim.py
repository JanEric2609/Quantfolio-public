"""One advisor cycle per sleeve per day (plan 2026-11-10 WS1, ADR 0025).

The cycle claims its day with a ``running`` row before doing any work; a
partial unique index on (portfolio_id, mandate, cycle_date) over the slot
statuses lets only one such row exist. ``failed`` frees the slot.
"""
from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from conftest import _memory_db
from test_advisor_loop import (
    _seed_discover_run,
    _seed_prices,
    _seed_real_book,
    _stub_llm,
    _user,
    _wiggly_series,
)

from app.decision.advisor import cycle as cycle_mod
from app.decision.advisor.cycle import (
    WORKER_RESTART_ERROR,
    reap_stale_cycle_claims,
    run_advisor_cycle,
)
from app.decision.advisor.evolution import run_evolution_round
from app.decision.advisor.strategy import ensure_champion, spawn_challenger
from app.foundation.models.entities import LlmPortfolioDecision, PaperPortfolio


def _rows(db, portfolio_id: str) -> list[LlmPortfolioDecision]:
    return (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.portfolio_id == portfolio_id)
        .order_by(LlmPortfolioDecision.created_at)
        .all()
    )


def _trading_setup(db):
    user = _user(db)
    _seed_real_book(db, user, cash=50_000.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly_series(90.0))
    _seed_prices(db, "CAND", _wiggly_series(40.0))
    return user


_BUY = [{"ticker": "CAND", "action": "buy", "target_weight": 0.05, "thesis": "t", "confidence": 0.6}]


def test_second_call_same_day_returns_already_ran_and_writes_no_row():
    db = _memory_db()
    user = _trading_setup(db)
    first = run_advisor_cycle(db, user.id, llm_call=_stub_llm(_BUY))
    second = run_advisor_cycle(db, user.id, llm_call=_stub_llm(_BUY))

    assert first["status"] == "completed"
    assert second["status"] == "already_ran_today"
    rows = _rows(db, first["portfolio_id"])
    assert len(rows) == 1
    # The claim row was finished in place, not duplicated.
    assert rows[0].status == "completed"
    assert rows[0].cycle_date == datetime.now(UTC).date()


def test_running_claim_blocks_a_second_trigger_as_in_progress():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)
    portfolio_id = run_advisor_cycle(db, user.id, llm_call=_stub_llm([]))["portfolio_id"]
    # Turn today's finished row back into a live claim (a cycle mid-run).
    row = _rows(db, portfolio_id)[0]
    row.status = "running"
    db.commit()

    result = run_advisor_cycle(db, user.id, llm_call=_stub_llm([]))
    assert result["status"] == "in_progress"
    assert len(_rows(db, portfolio_id)) == 1


def test_index_rejects_a_second_slot_row_directly():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)
    portfolio_id = run_advisor_cycle(db, user.id, llm_call=_stub_llm([]))["portfolio_id"]
    today = datetime.now(UTC).date()
    db.add(
        LlmPortfolioDecision(
            portfolio_id=portfolio_id, mandate="advisor", review_date=datetime.now(UTC),
            status="running", cycle_date=today,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_failed_run_frees_the_slot_and_a_retry_succeeds(monkeypatch):
    db = _memory_db()
    user = _trading_setup(db)

    def _boom(*_a, **_k):
        raise RuntimeError("llm exploded")

    monkeypatch.setattr(cycle_mod, "decide_trades", _boom)
    with pytest.raises(RuntimeError):
        run_advisor_cycle(db, user.id, llm_call=_stub_llm(_BUY))
    monkeypatch.undo()

    portfolio = db.query(PaperPortfolio).filter(PaperPortfolio.mandate == "advisor").one()
    rows = _rows(db, portfolio.id)
    assert [r.status for r in rows] == ["failed"]
    assert "llm exploded" in (rows[0].error or "")

    retry = run_advisor_cycle(db, user.id, llm_call=_stub_llm(_BUY))
    assert retry["status"] == "completed"
    assert sorted(r.status for r in _rows(db, portfolio.id)) == ["completed", "failed"]


def test_failed_attempt_cap_still_holds(monkeypatch):
    db = _memory_db()
    user = _trading_setup(db)
    monkeypatch.setattr(
        cycle_mod, "decide_trades", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    for _ in range(cycle_mod._MAX_FAILED_ATTEMPTS_PER_DAY):
        with pytest.raises(RuntimeError):
            run_advisor_cycle(db, user.id, llm_call=_stub_llm(_BUY))
    assert run_advisor_cycle(db, user.id, llm_call=_stub_llm(_BUY))["status"] == "already_ran_today"


def test_stale_running_claim_is_reaped():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)
    portfolio_id = run_advisor_cycle(db, user.id, llm_call=_stub_llm([]))["portfolio_id"]
    old = datetime.now(UTC) - timedelta(hours=3)
    stale = _rows(db, portfolio_id)[0]
    stale.status = "running"
    stale.review_date = old
    # A fresh claim on another sleeve must survive the reaper.
    other = PaperPortfolio(user_id=user.id, mandate="adv-fresh", name="x")
    db.add(other)
    db.flush()
    fresh = LlmPortfolioDecision(
        portfolio_id=other.id, mandate="adv-fresh", review_date=datetime.now(UTC),
        status="running", cycle_date=datetime.now(UTC).date(),
    )
    db.add(fresh)
    db.commit()

    assert reap_stale_cycle_claims(db) == 1
    db.refresh(stale)
    db.refresh(fresh)
    assert stale.status == "failed" and stale.error == WORKER_RESTART_ERROR
    assert fresh.status == "running"
    # The slot is free again: a retry claims it.
    assert run_advisor_cycle(db, user.id, llm_call=_stub_llm([]))["status"] == "no_candidates"


def test_claim_from_a_previous_day_is_reaped_even_if_recent():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)
    portfolio_id = run_advisor_cycle(db, user.id, llm_call=_stub_llm([]))["portfolio_id"]
    row = _rows(db, portfolio_id)[0]
    row.status = "running"
    row.cycle_date = datetime.now(UTC).date() - timedelta(days=1)
    db.commit()
    now = datetime.now(UTC)
    assert reap_stale_cycle_claims(db, now=now) == 1


def test_champion_and_challenger_each_run_once_on_the_same_day():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)
    champion = ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=1)
    assert champion.portfolio_id != challenger.portfolio_id

    for strategy in (champion, challenger):
        assert run_advisor_cycle(db, user.id, strategy=strategy, llm_call=_stub_llm([]))[
            "status"
        ] == "no_candidates"
    for strategy in (champion, challenger):
        assert run_advisor_cycle(db, user.id, strategy=strategy, llm_call=_stub_llm([]))[
            "status"
        ] == "already_ran_today"
        assert len(_rows(db, strategy.portfolio_id)) == 1


def test_evolution_round_runs_no_cycle():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)
    ensure_champion(db, user.id)
    spawn_challenger(db, user.id, seed=1)
    run_evolution_round(db, user.id, reflect=False)
    assert db.query(LlmPortfolioDecision).count() == 0


def _load_migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0148_ladder_sleeves_cycle_claim.py"
    spec = importlib.util.spec_from_file_location("mig_0148", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_backfill_keeps_exactly_one_cycle_date_per_day():
    db = _memory_db()
    user = _user(db)
    portfolio = PaperPortfolio(user_id=user.id, mandate="advisor", name="Advisor Loop")
    mandate_a = PaperPortfolio(user_id=user.id, mandate="A", name="Mandate A")
    db.add_all([portfolio, mandate_a])
    db.flush()
    day = datetime(2026, 11, 3, 10, 38, tzinfo=UTC)
    # The prod pattern (2-5 Nov): two idle rows a day from the 10:00/10:30 race,
    # plus a failed row and a mandate review that must stay untouched.
    for stamp, status in (
        (day, "idle_no_trades"),
        (day + timedelta(minutes=27), "idle_no_trades"),
        (day - timedelta(minutes=30), "failed"),
        (day + timedelta(days=1), "completed"),
    ):
        db.add(
            LlmPortfolioDecision(
                id=uuid4().hex, portfolio_id=portfolio.id, mandate="advisor",
                review_date=stamp, status=status,
            )
        )
    db.add(
        LlmPortfolioDecision(
            id=uuid4().hex, portfolio_id=mandate_a.id, mandate="A",
            review_date=day, status="completed",
        )
    )
    db.commit()

    _load_migration()._backfill_cycle_date(db.connection())
    db.commit()
    db.expire_all()

    rows = db.query(LlmPortfolioDecision).all()
    dated = [r for r in rows if r.cycle_date is not None]
    assert sorted((r.cycle_date.isoformat(), r.status) for r in dated) == [
        ("2026-11-03", "idle_no_trades"),
        ("2026-11-04", "completed"),
    ]
    first = min(
        (r for r in rows if r.status == "idle_no_trades"), key=lambda r: r.review_date
    )
    assert first.cycle_date is not None
    assert all(r.cycle_date is None for r in rows if r.mandate == "A" or r.status == "failed")


def test_migration_0148_gives_every_touched_table_the_model_columns(tmp_path):
    """Plan 2026-11-10: the migrated schema matches the models for every table 0148 touches.

    A guard on a misspelled table name (``discovery_predictions`` for
    ``discovery_prediction``) once skipped a column silently; the e2e smoke test
    caught it as a 500. This catches it here.
    """
    import os
    import subprocess
    import sys

    from sqlalchemy import create_engine
    from sqlalchemy import inspect as sa_inspect

    from app.foundation.core.db import Base

    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "mig.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"}
    done = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=backend, env=env,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = sa_inspect(engine)
    try:
        for table in ("llm_portfolio_decisions", "trial_ledger", "discovery_prediction",
                      "advisor_shadow_forecasts", "ladder_candidates", "ladder_evidence_daily"):
            migrated = {c["name"] for c in inspector.get_columns(table)}
            declared = {c.name for c in Base.metadata.tables[table].columns}
            assert declared <= migrated, (table, declared - migrated)
        indexes = {i["name"] for i in inspector.get_indexes("llm_portfolio_decisions")}
        assert "uq_llm_decision_cycle_slot" in indexes
    finally:
        engine.dispose()
