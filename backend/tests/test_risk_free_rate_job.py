"""Tests for register_risk_free_rate_refresh_job (F12/F13)."""

from unittest.mock import MagicMock

from conftest import _memory_db

from app.worker import register_risk_free_rate_refresh_job
from app.foundation.settings import get_risk_free_rate


class FakeScheduler:
    def __init__(self):
        self.calls = []

    def add_job(self, fn, **kw):
        self.calls.append((fn, kw))

        class _J:
            id = kw.get("id")

        return _J()


def test_register_risk_free_rate_refresh_job_cron_params():
    sched = FakeScheduler()
    job_id = register_risk_free_rate_refresh_job(sched)

    assert job_id == "risk_free_rate_refresh"
    assert len(sched.calls) == 1
    _fn, kw = sched.calls[0]
    assert kw["trigger"] == "cron"
    assert kw["hour"] == 6
    assert kw["minute"] == 30
    assert kw["id"] == "risk_free_rate_refresh"


def test_risk_free_rate_refresh_inner_upserts_cache_on_success(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", lambda: db)

    mock_provider = MagicMock()
    mock_provider.get_history.return_value = {
        "ok": True,
        "data": [
            {"key": "0:0:0:0:0", "value": "3.10"},
            {"key": "1:0:0:0:0", "value": "3.25"},
        ],
    }
    monkeypatch.setattr(
        "app.foundation.providers.ecb_provider.EcbProvider",
        lambda: mock_provider,
    )

    sched = FakeScheduler()
    register_risk_free_rate_refresh_job(sched)
    fn, _kw = sched.calls[0]
    fn()

    # Most recent observation (last in the window) wins, expressed as a fraction.
    assert get_risk_free_rate(db) == 0.0325


def test_risk_free_rate_refresh_inner_leaves_cache_untouched_on_failure(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", lambda: db)

    mock_provider = MagicMock()
    mock_provider.get_history.return_value = {"ok": False, "error": "network error"}
    monkeypatch.setattr(
        "app.foundation.providers.ecb_provider.EcbProvider",
        lambda: mock_provider,
    )

    sched = FakeScheduler()
    register_risk_free_rate_refresh_job(sched)
    fn, _kw = sched.calls[0]
    fn()  # must not raise

    from app.foundation.settings import FALLBACK_RISK_FREE_RATE

    # No cache was ever written — falls through to the hardcoded fallback.
    assert get_risk_free_rate(db) == FALLBACK_RISK_FREE_RATE
