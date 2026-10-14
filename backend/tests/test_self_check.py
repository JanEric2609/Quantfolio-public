import json
from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation import control_center, self_check
from app.foundation.app_errors import fingerprint, prune_errors, recent_errors, record_error
from app.foundation.control_center import attention_items
from app.foundation.core.db import Base
from app.foundation.models.entities import AppError, AppSetting, JobRun, Notification, TelegramAccount, User


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _ids(db):
    return {item["id"] for item in attention_items(db)}


def _quiet(monkeypatch):
    """Silence the checks a test is not about."""
    monkeypatch.setattr(control_center, "_extract_items", lambda clock, items: None)
    monkeypatch.setattr(control_center, "_price_items", lambda db, now, items: None)
    monkeypatch.setattr(control_center, "_disk_items", lambda items: None)


def _status_file(tmp_path, monkeypatch, **status):
    path = tmp_path / "status.json"
    if status:
        path.write_text(json.dumps(status))
    monkeypatch.setattr(control_center, "BACKUP_STATUS_FILE", str(path))
    return path


# --- backups ---------------------------------------------------------------------


def test_missing_backup_status_is_silent_on_a_dev_database(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    _status_file(tmp_path, monkeypatch)
    assert not {"backup-missing", "backup-failed", "backup-stale"} & _ids(_memory_db())


def test_missing_backup_status_is_an_error_in_production(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    _status_file(tmp_path, monkeypatch)
    monkeypatch.setattr(control_center, "_is_postgres", lambda db: True)
    items = {item["id"]: item for item in attention_items(_memory_db())}
    assert items["backup-missing"]["severity"] == "error"


def test_failed_backup_is_an_error(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    _status_file(tmp_path, monkeypatch, status="failed", finished_at=datetime.now(UTC).isoformat(), detail="pg_dump: x")
    items = {item["id"]: item for item in attention_items(_memory_db())}
    assert items["backup-failed"]["detail"] == "pg_dump: x"


def test_backup_older_than_36_hours_is_stale(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    old = (datetime.now(UTC) - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _status_file(tmp_path, monkeypatch, status="ok", finished_at=old)
    assert "backup-stale" in _ids(_memory_db())


def test_fresh_backup_raises_nothing(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    fresh = (datetime.now(UTC) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _status_file(tmp_path, monkeypatch, status="ok", finished_at=fresh)
    assert not {"backup-missing", "backup-failed", "backup-stale"} & _ids(_memory_db())


# --- prices ----------------------------------------------------------------------


def test_stale_benchmark_close_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(control_center, "_extract_items", lambda clock, items: None)
    monkeypatch.setattr(control_center, "_disk_items", lambda items: None)
    _status_file(tmp_path, monkeypatch, status="ok", finished_at=datetime.now(UTC).isoformat())
    monkeypatch.setattr(
        "app.foundation.market.history",
        lambda db, ticker, days=30, allow_live=True: [{"date": date.today() - timedelta(days=12), "close": 100.0}],
    )
    items = {item["id"]: item for item in attention_items(_memory_db())}
    assert items["prices-stale"]["severity"] == "error"
    assert "EUNL.DE" in items["prices-stale"]["detail"]


def test_fresh_benchmark_close_raises_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(control_center, "_extract_items", lambda clock, items: None)
    monkeypatch.setattr(control_center, "_disk_items", lambda items: None)
    _status_file(tmp_path, monkeypatch, status="ok", finished_at=datetime.now(UTC).isoformat())
    monkeypatch.setattr(
        "app.foundation.market.history",
        lambda db, ticker, days=30, allow_live=True: [{"date": date.today(), "close": 100.0}],
    )
    assert "prices-stale" not in _ids(_memory_db())


# --- jobs ------------------------------------------------------------------------


def test_job_running_for_hours_is_reported_stuck(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    _status_file(tmp_path, monkeypatch, status="ok", finished_at=datetime.now(UTC).isoformat())
    db = _memory_db()
    db.add(JobRun(job_name="discover_weekly", status="running", started_at=datetime.now(UTC) - timedelta(hours=9)))
    db.add(JobRun(job_name="regime_daily", status="running", started_at=datetime.now(UTC) - timedelta(minutes=5)))
    db.commit()
    items = {item["id"]: item for item in attention_items(db)}
    assert items["jobs-stuck"]["detail"].startswith("discover_weekly.")


def test_job_whose_next_run_passed_long_ago_is_overdue(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    _status_file(tmp_path, monkeypatch, status="ok", finished_at=datetime.now(UTC).isoformat())
    db = _memory_db()
    now = datetime.now(UTC)
    db.add(AppSetting(key="worker_heartbeat_json", value_json=json.dumps({
        "started_at": (now - timedelta(days=1)).isoformat(),
        "beat_at": now.isoformat(),
        "interval_minutes": 5,
        "jobs": [
            {"id": "price_backfill_daily", "schedule": "Daily", "next_run": (now - timedelta(hours=5)).isoformat()},
            {"id": "regime_daily", "schedule": "Daily", "next_run": (now + timedelta(hours=5)).isoformat()},
        ],
    })))
    db.commit()
    items = {item["id"]: item for item in attention_items(db)}
    assert items["jobs-overdue"]["detail"].startswith("price_backfill_daily.")


# --- app errors ------------------------------------------------------------------


def test_same_error_is_one_row_with_a_count():
    db = _memory_db()
    record_error(db, source="api", kind="KeyError", message="'x' for id 123", location="GET /api/a/{id}")
    record_error(db, source="api", kind="KeyError", message="'x' for id 456", location="GET /api/a/{id}")
    rows = db.query(AppError).all()
    assert len(rows) == 1 and rows[0].count == 2


def test_fingerprint_ignores_numbers_but_not_the_route():
    assert fingerprint("api", "E", "GET /a", "id 1") == fingerprint("api", "E", "GET /a", "id 2")
    assert fingerprint("api", "E", "GET /a", "id 1") != fingerprint("api", "E", "GET /b", "id 1")


def test_errors_of_the_last_day_are_an_attention_item(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    _status_file(tmp_path, monkeypatch, status="ok", finished_at=datetime.now(UTC).isoformat())
    db = _memory_db()
    record_error(db, source="client", kind="TypeError", message="x is undefined", location="/plan")
    items = {item["id"]: item for item in attention_items(db)}
    assert "TypeError at /plan" in items["app-errors"]["detail"]


def test_old_errors_are_pruned_and_not_recent():
    db = _memory_db()
    old = datetime.now(UTC) - timedelta(days=120)
    record_error(db, source="api", kind="E", message="m", location="l", now=old)
    assert recent_errors(db) == []
    assert prune_errors(db) == 1


# --- the self-check --------------------------------------------------------------


def _admin_with_telegram(db):
    user = User(username="owner", password_hash="x", role="admin")
    db.add(user)
    db.commit()
    db.add(TelegramAccount(user_id=user.id, chat_id="42"))
    db.commit()
    return user


def test_problems_are_sent_to_telegram_and_the_bell(monkeypatch):
    db = _memory_db()
    _admin_with_telegram(db)
    monkeypatch.setattr(self_check, "collect_problems", lambda db, probe=True: [
        {"id": "backup-stale", "severity": "error", "title": "Database backup is out of date",
         "detail": "Last good backup 2026-10-10.", "href": "/settings/status", "action": "View"},
    ])
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr("app.foundation.telegram_bot.send_telegram_message",
                        lambda db, chat_id, text: sent.append((chat_id, text)) or True)
    result = self_check.run_self_check(db, now=datetime(2026, 10, 15, 6, tzinfo=UTC))
    assert result["notified"] is True
    assert sent[0][0] == "42" and "Database backup is out of date" in sent[0][1]
    assert db.query(Notification).filter(Notification.source == "self_check").count() == 1
    assert self_check.last_result(db)["problems"][0]["id"] == "backup-stale"


def test_quiet_when_fine_except_on_monday(monkeypatch):
    db = _memory_db()
    _admin_with_telegram(db)
    monkeypatch.setattr(self_check, "collect_problems", lambda db, probe=True: [])
    sent: list[str] = []
    monkeypatch.setattr("app.foundation.telegram_bot.send_telegram_message",
                        lambda db, chat_id, text: sent.append(text) or True)
    self_check.run_self_check(db, now=datetime(2026, 10, 15, 6, tzinfo=UTC))  # a Thursday
    assert sent == []
    self_check.run_self_check(db, now=datetime(2026, 10, 19, 6, tzinfo=UTC))  # a Monday
    assert sent == ["Quantfolio self-check: all fine."]
    assert db.query(Notification).count() == 0


def test_offsite_probe_reports_an_unreachable_server():
    # Port 1 on localhost refuses connections.
    item = self_check._probe_offsite("127.0.0.1:1")
    assert item is not None and item["id"] == "backup-offsite-down"
    assert self_check._probe_offsite("") is None
    assert self_check._probe_offsite("no-port")["id"] == "backup-offsite-config"


def test_client_error_endpoint_records_without_the_query_string(monkeypatch):
    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user = User(username="u", password_hash="x", role="user")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        client = TestClient(app)
        response = client.post("/api/errors/client", json={
            "kind": "TypeError", "message": "boom", "location": "/portfolio/holdings?isin=DE123", "stack": "at x",
        })
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    row = db.query(AppError).one()
    assert row.source == "client" and row.location == "/portfolio/holdings"
