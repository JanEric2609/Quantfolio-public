"""Tests for LocalLlamaBackend completion kwargs + LLM model default resolution.

Covers T1.4 (Qwen non-thinking sampling preset, default model qwen3.5-9b)
and T2.1 (grammar-constrained structured output with graceful fallback).
"""

import json
import logging
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from conftest import _memory_db

from app.foundation.llm.local_llama import (
    LocalLlamaBackend,
    _build_completion_kwargs,
)
from app.foundation.settings import resolve_llm_model


class _TinyStructuredOutput(BaseModel):
    """Minimal structured-output model for response_format tests."""

    answer: str


def _assert_no_penalty_keys(kwargs: dict[str, Any]) -> None:
    """Plan D3: the client never sends penalty params — the llama-server
    process owns ``--presence-penalty 1.5``; sending them client-side would
    double-apply. Checked on the serialized form so nested dicts are covered."""
    serialized = repr(kwargs)
    assert "presence_penalty" not in serialized
    assert "frequency_penalty" not in serialized


def test_build_completion_kwargs_freeform_uses_qwen_sampling() -> None:
    """Freeform kwargs carry the Qwen non-thinking preset: temp 0.7 / top-p 0.8
    / top-k 20 (top_k via extra_body — AsyncOpenAI rejects bare top_k), and no
    response_format."""
    kwargs = _build_completion_kwargs(None)

    assert kwargs["temperature"] == 0.7
    assert kwargs["top_p"] == 0.8
    assert kwargs["extra_body"] == {"top_k": 20}
    assert "response_format" not in kwargs
    _assert_no_penalty_keys(kwargs)


def test_build_completion_kwargs_structured_adds_json_object() -> None:
    """Structured kwargs add a json_object response_format carrying the JSON
    schema — the shape llama.cpp's OpenAI-compatible handler has supported
    longest (mirrors review.py:_local_llm_sync). Sampling fields unchanged."""
    kwargs = _build_completion_kwargs(_TinyStructuredOutput)

    assert kwargs["response_format"] == {
        "type": "json_object",
        "schema": _TinyStructuredOutput.model_json_schema(),
    }
    assert kwargs["temperature"] == 0.7
    assert kwargs["top_p"] == 0.8
    assert kwargs["extra_body"] == {"top_k": 20}
    _assert_no_penalty_keys(kwargs)


def test_default_model_is_qwen35(monkeypatch) -> None:
    """resolve_llm_model with empty DB and no env var returns qwen3.5-9b."""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    db = _memory_db()

    assert resolve_llm_model(db) == "qwen3.5-9b"


# ---------------------------------------------------------------------------
# T2.1 — grammar-constrained structured output with graceful fallback
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, message: _FakeMessage) -> None:
        self.message = message


class _FakeUsage:
    def __init__(self, prompt_tokens: int = 11, completion_tokens: int = 7) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeCompletion:
    """Shaped like openai's ChatCompletion for the attributes complete() reads."""

    def __init__(self, content: str, model: str = "fake-model") -> None:
        self.choices = [_FakeChoice(_FakeMessage(content))]
        self.usage = _FakeUsage()
        self.model = model


class _FakeCompletions:
    """Async create() that replays scripted outcomes and captures kwargs."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _fake_backend(outcomes: list[Any]) -> tuple[LocalLlamaBackend, _FakeCompletions]:
    backend = LocalLlamaBackend(base_url="http://localhost:8080/v1")
    completions = _FakeCompletions(outcomes)

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = completions

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = _FakeChat()

    backend._client = _FakeClient()
    return backend, completions


def _bad_request_error() -> Exception:
    from openai import BadRequestError

    request = httpx.Request("POST", "http://t")
    response = httpx.Response(400, request=request)
    return BadRequestError("nope", response=response, body=None)


@pytest.mark.asyncio
async def test_structured_output_sends_json_object_with_schema() -> None:
    """complete() with structured_output sends the schema nested under the
    json_object response format (review.py precedent shape)."""
    backend, completions = _fake_backend(
        [_FakeCompletion(content='{"answer": "ok"}')]
    )

    completion = await backend.complete(
        messages=[{"role": "user", "content": "x"}],
        structured_output=_TinyStructuredOutput,
    )

    assert completion.content == '{"answer": "ok"}'
    assert len(completions.calls) == 1
    assert completions.calls[0]["response_format"] == {
        "type": "json_object",
        "schema": _TinyStructuredOutput.model_json_schema(),
    }


@pytest.mark.asyncio
async def test_schema_rejection_falls_back_gracefully(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HTTP 4xx on the constrained attempt logs a warning and retries exactly
    once WITHOUT response_format, appending a repair instruction carrying the
    schema; the caller still gets a valid completion."""
    backend, completions = _fake_backend(
        [_bad_request_error(), _FakeCompletion(content='{"answer": "ok"}')]
    )

    with caplog.at_level(logging.WARNING, logger="app.foundation.llm.local_llama"):
        completion = await backend.complete(
            messages=[{"role": "user", "content": "x"}],
            structured_output=_TinyStructuredOutput,
        )

    assert completion.content == '{"answer": "ok"}'
    assert len(completions.calls) == 2
    retry_kwargs = completions.calls[1]
    assert "response_format" not in retry_kwargs
    repair_messages = [
        m for m in retry_kwargs["messages"] if m["role"] == "system"
    ]
    assert len(repair_messages) == 1
    schema_json = json.dumps(_TinyStructuredOutput.model_json_schema())
    assert schema_json in repair_messages[0]["content"]
    assert "rejected schema-constrained request" in caplog.text


@pytest.mark.asyncio
async def test_freeform_unaffected_by_fallback_logic() -> None:
    """Freeform success path makes exactly one create call with no repair
    messages — fallback machinery never engages without structured_output."""
    backend, completions = _fake_backend([_FakeCompletion(content="plain text")])

    completion = await backend.complete(messages=[{"role": "user", "content": "x"}])

    assert completion.content == "plain text"
    assert len(completions.calls) == 1
    roles = [m["role"] for m in completions.calls[0]["messages"]]
    assert roles == ["user"]


@pytest.mark.asyncio
async def test_second_failure_propagates() -> None:
    """When the unconstrained retry also fails, the APIError is wrapped into
    RuntimeError exactly as today — no silent swallowing."""
    backend, completions = _fake_backend(
        [_bad_request_error(), _bad_request_error()]
    )

    with pytest.raises(RuntimeError, match="Local llama.cpp endpoint error"):
        await backend.complete(
            messages=[{"role": "user", "content": "x"}],
            structured_output=_TinyStructuredOutput,
        )

    assert len(completions.calls) == 2
    assert isinstance(completions.calls[0].get("response_format"), dict)
    assert "response_format" not in completions.calls[1]

