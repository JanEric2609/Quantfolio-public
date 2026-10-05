"""Tests for the nightly provider-health real-probe job (plan todo 3, audit §2.1).

The job must UPSERT ProviderHealth rows truthfully:
- probe ok          -> status "healthy", last_success_at stamped, error cleared
- probe fail        -> status "degraded", last_error_at stamped, error <= 500 chars
- paid API, no key  -> status "disabled" (honest) instead of healthy
- per-provider failures never escape the job handler
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import ProviderHealth


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class _FakeScheduler:
    """Records add_job calls so registration parameters can be asserted."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def add_job(self, fn, **kwargs):
        self.calls.append({"fn": fn, **kwargs})
        return type("Job", (), {"id": kwargs.get("id", "job")})()


def _seed(db, provider: str, capability: str = "status", **overrides):
    row = ProviderHealth(
        provider=provider,
        capability=capability,
        configured=True,
        available=False,
        status="degraded",
        message="stale",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Per-status application logic
# ---------------------------------------------------------------------------


def test_ok_status_flips_row_to_healthy_with_timestamp():
    from app.worker import _apply_provider_health_status

    db = _memory_db()
    row = _seed(db, "openbb")
    assert row.status == "degraded"

    _apply_provider_health_status(
        db,
        {"provider": "openbb", "enabled": True, "available": True, "message": "OpenBB API is available."},
        secret_present=True,
    )
    db.commit()
    db.refresh(row)

    assert row.status == "healthy"
    assert row.available is True
    assert row.configured is True
    assert row.last_success_at is not None
    assert row.last_error_at is None
    assert "available" in row.message.lower()


def test_fail_status_marks_degraded_and_truncates_error():
    from app.worker import _apply_provider_health_status

    db = _memory_db()
    row = _seed(db, "openbb")

    long_error = "x" * 900
    _apply_provider_health_status(
        db,
        {"provider": "openbb", "enabled": True, "available": False, "message": long_error},
        secret_present=True,
    )
    db.commit()
    db.refresh(row)

    assert row.status == "degraded"
    assert row.last_error_at is not None
    assert row.last_success_at is None
    assert len(row.message) <= 500


def test_paid_provider_without_secret_is_disabled_not_healthy():
    from app.worker import _apply_provider_health_status

    db = _memory_db()
    row = _seed(db, "tiingo")

    _apply_provider_health_status(
        db,
        {"provider": "tiingo", "enabled": True, "available": True, "message": "would be healthy"},
        secret_present=False,
    )
    db.commit()
    db.refresh(row)

    assert row.status == "disabled"
    assert row.configured is False
    assert row.available is False


def test_paid_provider_with_secret_follows_normal_path():
    from app.worker import _apply_provider_health_status

    db = _memory_db()
    row = _seed(db, "tiingo")

    _apply_provider_health_status(
        db,
        {"provider": "tiingo", "enabled": True, "available": False, "message": "HTTP 404"},
        secret_present=True,
    )
    db.commit()
    db.refresh(row)

    assert row.status == "degraded"


def test_malformed_status_payload_does_not_raise():
    from app.worker import _apply_provider_health_status

    db = _memory_db()

    # Garbage payload must not raise; caller's try/except contract holds.
    _apply_provider_health_status(db, {"provider": "junk"}, secret_present=True)
    db.commit()

    row = db.query(ProviderHealth).filter(ProviderHealth.provider == "junk").one_or_none()
    assert row is not None
    assert row.status in {"missing", "disabled", "unknown"}


# ---------------------------------------------------------------------------
# Job handler: iterates statuses, isolates per-provider failures
# ---------------------------------------------------------------------------

def test_job_handler_updates_rows_and_isolates_failures(monkeypatch):
    import app.worker as worker_mod
    import app.foundation.market as market_service
    import app.foundation.settings as settings_service

    db = _memory_db()
    stale_openbb = _seed(db, "openbb")
    _seed(db, "yfinance")

    statuses = [
        {"provider": "openbb", "enabled": True, "available": True, "message": "ok"},
        # Malformed entry raises inside the per-provider apply -> must be isolated.
        {"provider": "boom"},
        {"provider": "yfinance", "enabled": True, "available": False, "message": "down"},
    ]

    monkeypatch.setattr(market_service, "provider_status", lambda _db: statuses)
    monkeypatch.setattr(worker_mod, "_PAID_KEYED_PROVIDERS", {"tiingo"})
    monkeypatch.setattr(
        settings_service, "get_secret", lambda _db, _name: (None, {})
    )

    # Isolation probe: one provider's apply blows up; others must still land.
    real_apply = worker_mod._apply_provider_health_status

    def flaky_apply(db, payload, *, secret_present):
        if payload.get("provider") == "boom":
            raise RuntimeError("synthetic per-provider failure")
        return real_apply(db, payload, secret_present=secret_present)

    monkeypatch.setattr(worker_mod, "_apply_provider_health_status", flaky_apply)

    worker_mod._provider_health_probe_once(db=db)

    db.expire_all()
    refreshed_openbb = db.query(ProviderHealth).filter_by(provider="openbb").one()
    refreshed_yf = db.query(ProviderHealth).filter_by(provider="yfinance").one()
    assert refreshed_openbb.id == stale_openbb.id
    assert refreshed_openbb.status == "healthy"
    assert refreshed_openbb.last_success_at is not None
    assert refreshed_yf.status == "degraded"
    assert refreshed_yf.last_error_at is not None


def test_registration_uses_expected_cron_schedule():
    from app.worker import register_provider_health_probe_job

    sched = _FakeScheduler()
    job_id = register_provider_health_probe_job(scheduler=sched)

    assert job_id == "provider_health_probe"
    assert len(sched.calls) == 1
    call = sched.calls[0]
    assert call["id"] == "provider_health_probe"
    assert call["hour"] == 5
    assert call["minute"] == 10


def test_worker_registers_probe_job_unconditionally():
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parent.parent / "app" / "worker.py"
    ).read_text()
    assert "register_provider_health_probe_job(scheduler)" in source
    # Gate retirement: the experimental gate is gone from worker.py entirely,
    # so the probe job (like every other standard job) registers unconditionally.
    assert "is_experimental_enabled" not in source
