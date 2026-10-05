"""Tests for LLM router: priority queue, starvation protection, and queue-wait timeouts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.llm.base import Completion
from app.foundation.llm.router import QueueWaitTimeoutError, call


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.mark.asyncio
async def test_router_call_successful_completion():
    db = _memory_db()
    mock_completion = Completion(content='{"result": "ok"}', tokens_in=10, tokens_out=5, model="test-model", backend="local_llama")

    with patch("app.foundation.llm.router.LocalLlamaBackend.complete", new_callable=AsyncMock) as mock_complete:
        mock_complete.return_value = mock_completion
        result = await call(
            db,
            task_type="interactive",
            messages=[{"role": "user", "content": "hello"}],
            timeout_s=5.0,
        )

    assert result.content == '{"result": "ok"}'
    assert result.tokens_in == 10
    assert result.tokens_out == 5


@pytest.mark.asyncio
async def test_router_call_times_out_and_raises_queue_wait_timeout_error():
    db = _memory_db()

    async def slow_complete(*args, **kwargs):
        await asyncio.sleep(0.5)
        return Completion(content="slow", tokens_in=1, tokens_out=1, model="test-model", backend="local_llama")

    with patch("app.foundation.llm.router.LocalLlamaBackend.complete", side_effect=slow_complete):
        with pytest.raises(QueueWaitTimeoutError) as exc_info:
            await call(
                db,
                task_type="batch_research",
                messages=[{"role": "user", "content": "factor proposal"}],
                timeout_s=0.05,
            )
        assert "timed out" in str(exc_info.value)
