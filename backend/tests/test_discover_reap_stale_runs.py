"""Tests for reap_stale_discover_runs — reconciliation of DiscoverRun rows
left inconsistent by an interrupted worker process (e.g. a deploy restart).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import DiscoverRun, User
from app.decision.discover.orchestrator import reap_run_if_stale, reap_stale_discover_runs
from app.decision.discover.state import DiscoveryState, make_stage_json


def _user(db) -> User:
    user = User(id=uuid4().hex, username="alice", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _run(db, user, *, status, created_at, stage_json="{}", updated_at=None) -> DiscoverRun:
    run = DiscoverRun(
        id=uuid4().hex,
        user_id=user.id,
        status=status,
        stage_json=stage_json,
        created_at=created_at,
        # updated_at defaults to "now" (mapped_column default=now_utc) unless
        # explicitly overridden here, which would defeat tests that need to
        # simulate a stale last-write time distinct from created_at.
        updated_at=updated_at if updated_at is not None else created_at,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def test_cancelled_run_with_complete_dossiers_promoted_to_completed():
    """A run killed by a process restart right after finishing all dossiers
    (but before _finalise_run committed "completed") is mislabelled
    "cancelled". It must be promoted to "completed" since the work is done.
    """
    db = _memory_db()
    user = _user(db)
    stage = make_stage_json(
        DiscoveryState.WRITING_DOSSIER,
        total_candidates=15,
        processed_candidates=15,
        message="Dossiers: 15/15 generated",
    )
    run = _run(
        db, user,
        status="cancelled",
        created_at=datetime.now(UTC) - timedelta(minutes=5),
        stage_json=json.dumps({"discover": stage}),
    )

    reconciled = reap_stale_discover_runs(db)

    db.refresh(run)
    assert reconciled == 1
    assert run.status == "completed"
    assert run.completed_at is not None


def test_cancelled_run_with_incomplete_dossiers_left_alone():
    """A genuinely cancelled run — dossier stage not at 100% — must not be
    touched (it was a real cancellation, not a mislabel)."""
    db = _memory_db()
    user = _user(db)
    stage = make_stage_json(
        DiscoveryState.WRITING_DOSSIER,
        total_candidates=15,
        processed_candidates=7,
        message="Dossiers: 7/15 generated",
    )
    run = _run(
        db, user,
        status="cancelled",
        created_at=datetime.now(UTC) - timedelta(minutes=5),
        stage_json=json.dumps({"discover": stage}),
    )

    reconciled = reap_stale_discover_runs(db)

    db.refresh(run)
    assert reconciled == 0
    assert run.status == "cancelled"


def test_stale_running_run_marked_failed():
    """A "running" run older than the reap window (no writer left alive to
    ever finish it) is marked "failed" rather than left as a zombie."""
    db = _memory_db()
    user = _user(db)
    run = _run(
        db, user,
        status="running",
        created_at=datetime.now(UTC) - timedelta(minutes=60),
    )

    reconciled = reap_stale_discover_runs(db, max_age_minutes=35.0)

    db.refresh(run)
    assert reconciled == 1
    assert run.status == "failed"
    assert run.error_message == "Interrupted by service restart"


def test_recent_running_run_left_alone():
    """A "running" run that is still within the reap window may genuinely
    still be in progress — must not be touched."""
    db = _memory_db()
    user = _user(db)
    run = _run(
        db, user,
        status="running",
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    reconciled = reap_stale_discover_runs(db, max_age_minutes=35.0)

    db.refresh(run)
    assert reconciled == 0
    assert run.status == "running"


def test_running_run_with_complete_dossiers_stale_promoted_to_completed():
    """A "running" run whose dossier stage hit 100% but hasn't written any
    further progress in a while (the process died before reaching the fast,
    best-effort prediction-ledger + finalise step) is promoted to
    "completed" — matching the same promotion the "cancelled" branch already
    does, but recovered much faster since it doesn't need to wait out the
    general staleness window.
    """
    db = _memory_db()
    user = _user(db)
    stage = make_stage_json(
        DiscoveryState.WRITING_DOSSIER,
        total_candidates=15,
        processed_candidates=15,
        message="Dossiers: 15/15 generated",
    )
    run = _run(
        db, user,
        status="running",
        created_at=datetime.now(UTC) - timedelta(minutes=10),
        updated_at=datetime.now(UTC) - timedelta(minutes=4),
        stage_json=json.dumps({"discover": stage}),
    )

    reconciled = reap_stale_discover_runs(db, dossier_complete_grace_minutes=3.0)

    db.refresh(run)
    assert reconciled == 1
    assert run.status == "completed"
    assert run.completed_at is not None


def test_running_run_with_complete_dossiers_recent_left_alone():
    """Same as above but the last write was recent — the prediction-ledger
    step may still genuinely be in flight, so it must not be touched yet."""
    db = _memory_db()
    user = _user(db)
    stage = make_stage_json(
        DiscoveryState.WRITING_DOSSIER,
        total_candidates=15,
        processed_candidates=15,
        message="Dossiers: 15/15 generated",
    )
    run = _run(
        db, user,
        status="running",
        created_at=datetime.now(UTC) - timedelta(minutes=1),
        updated_at=datetime.now(UTC) - timedelta(seconds=10),
        stage_json=json.dumps({"discover": stage}),
    )

    reconciled = reap_stale_discover_runs(db, dossier_complete_grace_minutes=3.0)

    db.refresh(run)
    assert reconciled == 0
    assert run.status == "running"


def test_reap_run_if_stale_heals_on_read_without_waiting_for_periodic_sweep():
    """A single stuck run must self-heal the moment it's read (e.g. via
    GET /runs/{id} or the debug endpoint), not just once the worker's
    5-minute periodic sweep next fires.
    """
    db = _memory_db()
    user = _user(db)
    stage = make_stage_json(
        DiscoveryState.WRITING_DOSSIER,
        total_candidates=15,
        processed_candidates=15,
        message="Dossiers: 15/15 generated",
    )
    run = _run(
        db, user,
        status="running",
        created_at=datetime.now(UTC) - timedelta(minutes=10),
        updated_at=datetime.now(UTC) - timedelta(minutes=4),
        stage_json=json.dumps({"discover": stage}),
    )

    changed = reap_run_if_stale(db, run, dossier_complete_grace_minutes=3.0)

    assert changed is True
    assert run.status == "completed"
    assert run.completed_at is not None


def test_reap_run_if_stale_leaves_active_run_alone():
    db = _memory_db()
    user = _user(db)
    run = _run(
        db, user,
        status="running",
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    changed = reap_run_if_stale(db, run)

    assert changed is False
    assert run.status == "running"


def test_completed_run_untouched():
    db = _memory_db()
    user = _user(db)
    run = _run(
        db, user,
        status="completed",
        created_at=datetime.now(UTC) - timedelta(minutes=60),
    )

    reconciled = reap_stale_discover_runs(db)

    db.refresh(run)
    assert reconciled == 0
    assert run.status == "completed"
