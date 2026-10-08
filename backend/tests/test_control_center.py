import json
from datetime import UTC, date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.control_center import attention_items, pending_restart_keys
from app.foundation.core.db import Base
from app.foundation.jobs import WORKER_HEARTBEAT_KEY, build_worker_heartbeat
from app.foundation.models.entities import AppSetting, JobRun, ProviderHealth
from app.foundation.settings import record_connection_test, set_secret, upsert_public_settings
from app.interface.api.jobs import job_schedule


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _heartbeat(db, *, started_at, beat_at=None, jobs=()):
    payload = {
        "started_at": started_at.isoformat(),
        "beat_at": (beat_at or datetime.now(UTC)).isoformat(),
        "interval_minutes": 5,
        "jobs": [{"id": job, "schedule": "Daily 02:00 UTC", "next_run": None} for job in jobs],
    }
    db.add(AppSetting(key=WORKER_HEARTBEAT_KEY, value_json=json.dumps(payload)))
    db.commit()


def _ids(db, **kwargs):
    return {item["id"] for item in attention_items(db, **kwargs)}


def test_missing_heartbeat_is_reported_as_unknown_not_down(monkeypatch):
    monkeypatch.setattr("app.foundation.control_center._extract_items", lambda clock, items: None)
    db = _memory_db()

    ids = _ids(db)

    assert "worker-unknown" in ids
    assert "worker-down" not in ids


def test_stale_heartbeat_is_an_error(monkeypatch):
    monkeypatch.setattr("app.foundation.control_center._extract_items", lambda clock, items: None)
    db = _memory_db()
    old = datetime.now(UTC) - timedelta(hours=2)
    _heartbeat(db, started_at=old, beat_at=old)

    items = attention_items(db)

    assert items[0]["id"] == "worker-down"
    assert items[0]["severity"] == "error"


def test_restart_setting_saved_after_worker_start_is_pending():
    db = _memory_db()
    _heartbeat(db, started_at=datetime.now(UTC) - timedelta(minutes=10))
    upsert_public_settings(db, {"duckdb_threads": 4, "currency": "USD"})

    from app.foundation.control_center import worker_heartbeat

    assert pending_restart_keys(db, worker_heartbeat(db)) == ["duckdb_threads"]


def test_failed_connection_test_links_to_its_drawer(monkeypatch):
    monkeypatch.setattr("app.foundation.control_center._extract_items", lambda clock, items: None)
    db = _memory_db()
    set_secret(db, "finnhub", "key")
    record_connection_test(db, "finnhub", False, "401 Unauthorized", reason="auth", probe_symbol="AAPL")

    item = next(i for i in attention_items(db) if i["id"] == "connection-finnhub")

    assert item["href"] == "/settings/market-data?connection=finnhub"
    assert "401" in item["detail"]


PROD_LEGACY = {
    "tiingo": "Tiingo IEX returned no data for EUNL.DE.",
    "twelvedata": "HTTP 404 for /quote (symbol=EUNL, mic_code=XETR)",
    "finnhub": "Finnhub covers US listings only; skipping EUNL.DE.",
    "databento": "Databento covers US listings only; skipping EUNL.DE.",
    "eod": "EODHD daily rate limit exceeded (20/day).",
}


def test_legacy_prod_results_are_not_tested_yet_info_never_warnings(monkeypatch):
    monkeypatch.setattr("app.foundation.control_center._extract_items", lambda clock, items: None)
    db = _memory_db()
    for service, message in PROD_LEGACY.items():
        set_secret(db, service, "key")
        record_connection_test(db, service, False, message)  # old format: no reason, no probe symbol

    items = [i for i in attention_items(db) if i["id"].startswith("connection-")]

    assert len(items) == len(PROD_LEGACY)
    assert {i["severity"] for i in items} == {"info"}
    assert all("not tested yet" in i["title"] for i in items)


def test_coded_harmless_outcomes_are_never_surfaced(monkeypatch):
    monkeypatch.setattr("app.foundation.control_center._extract_items", lambda clock, items: None)
    db = _memory_db()
    for service, reason in (("finnhub", "not_applicable"), ("eod", "rate_limited")):
        set_secret(db, service, "key")
        record_connection_test(db, service, False, "whatever text", reason=reason, probe_symbol="AAPL")

    assert not [i for i in attention_items(db) if i["id"].startswith("connection-")]


def test_optional_provider_failure_severity_follows_the_code(monkeypatch):
    monkeypatch.setattr("app.foundation.control_center._extract_items", lambda clock, items: None)
    db = _memory_db()
    for service, reason in (("finnhub", "http_error"), ("tiingo", "auth"), ("twelvedata", "no_data")):
        set_secret(db, service, "key")
        record_connection_test(db, service, False, "x", reason=reason, probe_symbol="AAPL")

    by_id = {i["id"]: i for i in attention_items(db)}

    assert by_id["connection-finnhub"]["severity"] == "info"
    assert by_id["connection-tiingo"]["severity"] == "warning"
    assert by_id["connection-twelvedata"]["severity"] == "info"


def test_unconfigured_provider_with_old_failed_test_is_ignored(monkeypatch):
    monkeypatch.setattr("app.foundation.control_center._extract_items", lambda clock, items: None)
    db = _memory_db()
    record_connection_test(db, "finnhub", False, "401 Unauthorized", reason="auth", probe_symbol="AAPL")

    assert "connection-finnhub" not in {i["id"] for i in attention_items(db)}


def test_old_provider_health_rows_are_not_current_problems(monkeypatch):
    monkeypatch.setattr("app.foundation.control_center._extract_items", lambda clock, items: None)
    db = _memory_db()
    db.add(ProviderHealth(provider="tiingo", capability="status", configured=True, available=False,
                          status="degraded", message="old", updated_at=datetime.now(UTC) - timedelta(days=90)))
    db.add(ProviderHealth(provider="eod", capability="status", configured=True, available=False,
                          status="degraded", message="today", updated_at=datetime.now(UTC)))
    db.commit()

    ids = _ids(db)

    assert "provider-eod" in ids
    assert "provider-tiingo" not in ids


def test_nv_certificate_expiry_and_ignored_alphacrafter_basket(monkeypatch):
    monkeypatch.setattr("app.foundation.control_center._extract_items", lambda clock, items: None)
    db = _memory_db()
    upsert_public_settings(db, {
        "tax_nv_certificate": True,
        "tax_nv_valid_until": "2026-10-15",
        "alphacrafter_index_basket": "IWDA.L,VWRL.L,EIMI.L,SXR8.DE",
    })

    ids = _ids(db, today=date(2026, 9, 30))

    assert {"nv-expiring", "alphacrafter-basket"} <= ids


def test_job_schedule_separates_registered_from_retired_jobs():
    db = _memory_db()
    now = datetime.now(UTC)
    _heartbeat(db, started_at=now - timedelta(hours=1), jobs=["regime_daily", "worker_heartbeat"])
    db.add(JobRun(job_name="regime_daily", status="error", started_at=now, error_message="boom", triggered_by="scheduler"))
    db.add(JobRun(job_name="competition_round", status="success", started_at=now - timedelta(days=40), triggered_by="scheduler"))
    db.commit()

    result = job_schedule(_user=object(), db=db)

    assert result.worker is not None and result.worker.alive
    assert [job.id for job in result.jobs] == ["regime_daily"]
    assert result.jobs[0].schedule == "Daily 02:00 UTC"
    assert result.jobs[0].last_error == "boom"
    assert [job.id for job in result.retired] == ["competition_round"]


def test_job_schedule_without_heartbeat_keeps_every_job_live():
    db = _memory_db()
    db.add(JobRun(job_name="competition_round", status="success", started_at=datetime.now(UTC), triggered_by="scheduler"))
    db.commit()

    result = job_schedule(_user=object(), db=db)

    assert result.worker is None
    assert [job.id for job in result.jobs] == ["competition_round"]
    assert result.retired == []


def test_worker_heartbeat_lists_scheduled_jobs():
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(lambda: None, "cron", id="news_cleanup", hour=2, minute=0)
    scheduler.add_job(lambda: None, "interval", id="price_refresh", minutes=15)

    beat = build_worker_heartbeat(scheduler, datetime(2026, 9, 30, tzinfo=UTC))

    assert [(job["id"], job["schedule"]) for job in beat["jobs"]] == [
        ("news_cleanup", "Daily 02:00 UTC"),
        ("price_refresh", "Every 15 min"),
    ]
