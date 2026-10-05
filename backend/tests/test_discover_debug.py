"""Diagnostic endpoint for stuck Discover runs (/api/discover/runs/{id}/debug)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import _memory_db
from fastapi import HTTPException

from app.interface.api.discover import debug_run
from app.foundation.models.entities import DiscoverCandidate, DiscoverRun, User


def _user(db, name="alice") -> User:
    user = User(username=name, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _stage(**kw) -> str:
    return json.dumps({"discover": {"state": "pipeline_candidates", **kw}})


def test_debug_flags_stalled_warmup_run(monkeypatch):
    # The APScheduler registry is a module-level global shared per pytest
    # process; under xdist another test may initialise it before this one
    # runs. Pin the "no scheduler in this process" precondition so this
    # unit test is order-independent.
    monkeypatch.setattr("app.foundation.jobs._scheduler", None)
    db = _memory_db()
    user = _user(db)
    # Created 5 min ago, but the last stage_json write (tracked via
    # updated_at, bumped on every write) landed 290s ago — no progress since.
    run = DiscoverRun(
        user_id=user.id,
        status="running",
        created_at=datetime.now(UTC) - timedelta(seconds=300),
        updated_at=datetime.now(UTC) - timedelta(seconds=290),
        stage_json=_stage(
            current_stage="warming_price_cache",
            current_candidate="AAPL",
            elapsed_seconds=10,
            warmup_done=55,
            warmup_total=55,
            message="Warming price history: 55/55 symbols",
        ),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    for sym in ("AAPL", "MSFT"):
        db.add(DiscoverCandidate(run_id=run.id, symbol=sym, source="screen_etf", status="pending"))
    db.commit()

    result = debug_run(run.id, db=db, user=user)

    assert result["status"] == "running"
    assert result["candidates"]["total"] == 2
    assert result["candidates"]["by_status"] == {"pending": 2}
    assert result["seconds_since_last_progress"] is not None
    assert result["seconds_since_last_progress"] > 90
    assert "No progress" in result["hint"]
    assert result["scheduler_job"]["status"] == "no_scheduler_in_this_process"


def test_debug_reports_finished_run():
    db = _memory_db()
    user = _user(db)
    run = DiscoverRun(
        user_id=user.id,
        status="failed",
        error_message="boom",
        stage_json=_stage(state="failed", message="Pipeline failed"),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    result = debug_run(run.id, db=db, user=user)
    assert result["status"] == "failed"
    assert "boom" in result["hint"]


def test_debug_is_owner_only():
    db = _memory_db()
    owner = _user(db, "owner")
    other = _user(db, "other")
    run = DiscoverRun(user_id=owner.id, status="running", stage_json=_stage())
    db.add(run)
    db.commit()
    db.refresh(run)

    with pytest.raises(HTTPException) as exc:
        debug_run(run.id, db=db, user=other)
    assert exc.value.status_code == 404
