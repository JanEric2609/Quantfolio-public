"""One audit row per LLM call (plan 2026-10-14, M7 / WP-D4.1)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import AuditLog, LlmAuditEvent


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class _Response:
    def __init__(self, body: dict, status: int = 200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)  # type: ignore[arg-type]


def _local(monkeypatch, body: dict | Exception):
    from app.decision.llm_portfolio import review

    monkeypatch.setattr(review, "resolve_llm_base_url", lambda db: "http://llm:8080/v1")
    monkeypatch.setattr(review, "resolve_llm_model", lambda db: "qwen-9b")
    monkeypatch.setattr(review, "llm_auth_headers", lambda db: {})

    def post(url, json=None, timeout=None, **kw):
        if isinstance(body, Exception):
            raise body
        return _Response(body)

    monkeypatch.setattr(review.httpx, "post", post)
    return review


def test_a_local_call_is_recorded_with_its_sampling_tokens_and_caller(monkeypatch):
    review = _local(monkeypatch, {
        "choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127},
    })
    db = _memory_db()
    messages = [{"role": "user", "content": "hello"}]
    out = review._local_llm_sync(db, messages, sampling={"temperature": 0.7, "presence_penalty": 1.5})
    db.commit()
    assert out == '{"ok": true}'
    row = db.query(AuditLog).filter(AuditLog.source == "llm").one()
    meta = json.loads(row.payload_json)
    assert meta["model"] == "qwen-9b" and meta["caller"] == __name__
    assert meta["sampling"]["temperature"] == 0.7 and meta["sampling"]["presence_penalty"] == 1.5
    assert (meta["tokens_in"], meta["tokens_out"], meta["outcome"]) == (120, 7, "ok")
    assert len(meta["prompt_sha256"]) == 64 and meta["latency_ms"] >= 0
    body = db.query(LlmAuditEvent).one()
    assert json.loads(body.prompt_json) == messages and json.loads(body.response_json)["content"] == '{"ok": true}'


def test_a_truncated_call_is_recorded_as_truncated(monkeypatch):
    review = _local(monkeypatch, {
        "choices": [{"message": {"content": "{\"a\": "}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 50},
    })
    db = _memory_db()
    review._local_llm_sync(db, [{"role": "user", "content": "x"}])
    db.commit()
    assert json.loads(db.query(AuditLog).one().payload_json)["outcome"] == "truncated"


def test_a_failed_call_is_recorded_and_still_raises(monkeypatch):
    review = _local(monkeypatch, httpx.ConnectError("refused"))
    db = _memory_db()
    with pytest.raises(httpx.ConnectError):
        review._local_llm_sync(db, [{"role": "user", "content": "x"}])
    db.commit()
    row = db.query(AuditLog).one()
    assert row.event_type == "llm_call_failed" and json.loads(row.payload_json)["outcome"] == "error:ConnectError"


def test_a_routed_call_is_recorded():
    from app.foundation.llm.base import Completion
    from app.foundation.llm.router import _audit

    class Fake:
        model = "claude"

    db = _memory_db()
    done = Completion(content="hi", model="claude", tokens_in=3, tokens_out=1, backend="anthropic")
    _audit(db, Fake(), {"caller": "app.foundation.research", "task_type": "batch_research"},
           [{"role": "user", "content": "q"}], None, 0.0, done, "ok", False)
    db.commit()
    meta = json.loads(db.query(AuditLog).one().payload_json)
    assert meta["caller"] == "app.foundation.research" and meta["tokens_out"] == 1 and meta["is_degraded"] is False


def test_call_metadata_is_kept_180_days():
    from app.foundation.llm.audit import prune_call_metadata

    db = _memory_db()
    now = datetime.now(UTC)
    db.add(AuditLog(source="llm", event_type="llm_call", message="old", created_at=now - timedelta(days=181)))
    db.add(AuditLog(source="llm", event_type="llm_call", message="new", created_at=now - timedelta(days=179)))
    db.add(AuditLog(source="auth", event_type="login", message="other", created_at=now - timedelta(days=400)))
    db.commit()
    assert prune_call_metadata(db, now=now) == 1
    db.commit()
    assert sorted(r.message for r in db.query(AuditLog).all()) == ["new", "other"]


def test_a_routed_call_records_the_answering_model_its_label_and_sampling():
    from app.foundation.llm.base import Completion
    from app.foundation.llm.local_llama import LocalLlamaBackend
    from app.foundation.llm.router import _audit

    db = _memory_db()
    backend = LocalLlamaBackend("http://llm.invalid/v1", model="configured")
    done = Completion(content="hi", model="Qwen3.5-9B", tokens_in=3, tokens_out=1, backend="local_llama")
    _audit(db, backend, {}, [{"role": "user", "content": "q"}], None, 0.0, done, "ok", False)
    _audit(db, backend, {}, [{"role": "user", "content": "q"}], None, 0.0, None, "error:Timeout", False)
    db.commit()
    rows = [json.loads(r.payload_json) for r in db.query(AuditLog).all()]
    assert sorted(r["model"] for r in rows) == ["Qwen3.5-9B", "configured"]
    assert {r["backend"] for r in rows} == {"local_llama"}
    assert all(r["sampling"]["temperature"] == LocalLlamaBackend.sampling["temperature"] for r in rows)
