"""WP8 (plan 2026-11-12): job result contract, skipped-run alert, tone-call limits."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation import jobs
from app.foundation.core.db import Base
from app.foundation.models.entities import JobRun, Nudge, User


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def factory(monkeypatch):
    f = _memory_db()
    import app.foundation.core.db as core_db
    monkeypatch.setattr(core_db, "SessionLocal", f)
    s = f()
    s.add(User(id="u1", username="u", password_hash="x"))
    s.commit()
    s.close()
    return f


def _last(factory):
    with factory() as db:
        return db.query(JobRun).order_by(JobRun.started_at.desc()).first()


def test_ok_degraded_and_failed_results_are_stored_with_their_counts(factory):
    jobs._track_job("a", lambda: {"status": "ok", "rated": 5})
    assert _last(factory).status == "success"
    jobs._track_job("b", lambda: {"status": "degraded", "reason": "3 of 50 calls failed", "failed": 3})
    row = _last(factory)
    assert row.status == "degraded" and "3 of 50" in row.error_message and '"failed": 3' in row.error_message
    jobs._track_job("c", lambda: {"status": "failed", "reason": "model down"})
    assert _last(factory).status == "error"
    with factory() as db:
        assert db.query(Nudge).filter(Nudge.source == "job_failure").count() == 1   # only the failure alerts


def test_a_raising_job_keeps_its_error_status_when_the_alert_cannot_be_written(factory, monkeypatch):
    def broken_alert(db, name, message):
        raise RuntimeError("alert failed")

    monkeypatch.setattr(jobs, "_alert_job_failure", broken_alert)

    def boom():
        raise ValueError("job broke")

    with pytest.raises(RuntimeError):
        jobs._track_job("d", boom)
    assert _last(factory).status == "error"             # committed before alerting, never commit_failed


def test_a_failed_job_record_insert_raises_an_alert(factory, monkeypatch):
    alerts: list[str] = []
    monkeypatch.setattr(jobs, "_alert_job_failure", lambda db, name, message: alerts.append(message))
    monkeypatch.setattr(jobs, "JobRun", None, raising=False)
    import app.foundation.models.entities as ents

    class Exploding:
        def __init__(self, **_kw):
            raise RuntimeError("insert failed")

    monkeypatch.setattr(ents, "JobRun", Exploding)
    ran: list[int] = []
    jobs._track_job("e", lambda: ran.append(1))
    assert ran == [] and alerts and "skipped" in alerts[0]


def test_news_tone_status_follows_the_counts():
    from app.lab.forward_lab.news_tone import _job_status

    base = {"with_news": 10, "rated": 10, "failed": 0, "stopped_early": False}
    assert _job_status(base) == "ok"
    assert _job_status({**base, "rated": 9, "failed": 1}) == "degraded"
    assert _job_status({**base, "stopped_early": True}) == "degraded"
    assert _job_status({**base, "rated": 0, "failed": 10}) == "failed"
    assert _job_status({**base, "with_news": 0, "rated": 0}) == "ok"


def test_the_answer_schema_and_parser_take_the_object_form():
    from app.lab.forward_lab.news_tone import MAX_TOKENS, _Answer, parse_ratings

    raw = '{"ratings": [{"i": 0, "tone": 0.4, "event": "deal"}, {"i": 1, "tone": -2, "event": "x"}]}'
    got = parse_ratings(raw, 2)
    assert [(r.i, r.event) for r in got] == [(0, "deal")]       # the out-of-range tone is dropped
    assert "enum" in str(_Answer.model_json_schema())
    assert MAX_TOKENS < 1000


def test_an_interval_job_counts_from_its_last_run_not_from_the_restart(factory):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    with factory() as db:
        db.add(JobRun(job_name="news_x", status="success", started_at=now - timedelta(hours=3)))
        db.add(JobRun(job_name="old_x", status="success", started_at=now - timedelta(hours=9)))
        db.commit()

    class Sched:
        def __init__(self):
            self.kw = {}

        def add_job(self, fn, **kw):
            self.kw[kw["id"]] = kw

            class J:
                id = kw["id"]
            return J()

    sched = Sched()
    jobs.register_interval_job("news_x", lambda: None, scheduler=sched, hours=4)
    jobs.register_interval_job("old_x", lambda: None, scheduler=sched, hours=4)
    jobs.register_interval_job("new_x", lambda: None, scheduler=sched, hours=4)
    due = sched.kw["news_x"]["next_run_time"] - now
    assert timedelta(minutes=55) < due < timedelta(hours=1, minutes=5)         # 3 h ago + 4 h
    assert sched.kw["old_x"]["next_run_time"] - now < timedelta(minutes=2)      # overdue: soon after start
    assert "next_run_time" not in sched.kw["new_x"]                              # never ran: unchanged
