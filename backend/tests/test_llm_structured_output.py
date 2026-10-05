"""Tests for schema-constrained generation on the local LLM path.

Prompt-only JSON is what broke the advisor decision step: Qwen3's hidden
<think> block ate the completion budget and the caller got unparseable output.
These cover the grammar-constrained request, its degradation path, and the
reasoning-strip guard.
"""
import json
from typing import Any

import httpx
import pytest
from conftest import _memory_db

from app.decision.advisor.llm_decision import DECISION_JSON_SCHEMA
from app.decision.advisor.reflection import LESSON_JSON_SCHEMA
from app.decision.llm_portfolio import review as review_mod
from app.decision.llm_portfolio.review import _local_llm_sync, _strip_reasoning


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]


def _content(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}]}


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict[str, Any]] = []

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        calls.append({"url": url, "json": dict(json or {})})
        return _FakeResponse(_content('{"ok": true}'))

    monkeypatch.setattr(httpx, "post", _fake_post)
    return calls


def test_schema_is_sent_as_json_object_with_schema(captured):
    """llama.cpp's longest-supported shape is json_object + nested schema."""
    db = _memory_db()

    _local_llm_sync(db, [{"role": "user", "content": "hi"}], json_schema={"type": "object"})

    body = captured[0]["json"]
    assert body["response_format"] == {"type": "json_object", "schema": {"type": "object"}}


def test_thinking_is_disabled_via_chat_template_kwargs(captured):
    """llama-server ignores reasoning_effort; the template kwarg is the real knob."""
    db = _memory_db()

    _local_llm_sync(db, [{"role": "user", "content": "hi"}])

    body = captured[0]["json"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in body
    assert "response_format" not in body  # unconstrained unless a schema is given


def test_schema_rejection_degrades_to_an_unconstrained_retry(monkeypatch):
    """A server without schema support must still answer, not 4xx the caller."""
    db = _memory_db()
    calls: list[dict[str, Any]] = []

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        # Snapshot: httpx serialises immediately, so a live reference here
        # would hide whether the retry reused or rebuilt the body.
        calls.append(dict(json or {}))
        if "response_format" in (json or {}):
            return _FakeResponse({}, status_code=400, text="grammar not supported")
        return _FakeResponse(_content('{"decisions": []}'))

    monkeypatch.setattr(httpx, "post", _fake_post)

    out = _local_llm_sync(db, [{"role": "user", "content": "hi"}], json_schema={"type": "object"})

    assert out == '{"decisions": []}'
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]


def test_server_error_is_not_retried_unconstrained(monkeypatch):
    """A 5xx is not a schema problem — it must surface, not silently degrade."""
    db = _memory_db()
    calls: list[dict[str, Any]] = []

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        calls.append(dict(json or {}))
        return _FakeResponse({}, status_code=503, text="unavailable")

    monkeypatch.setattr(httpx, "post", _fake_post)

    with pytest.raises(httpx.HTTPStatusError):
        _local_llm_sync(db, [{"role": "user", "content": "hi"}], json_schema={"type": "object"})
    assert len(calls) == 1


def test_inline_reasoning_block_is_stripped(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr(
        httpx, "post",
        lambda url, json=None, timeout=None: _FakeResponse(
            _content('<think>weighing it up</think>{"decisions": []}')
        ),
    )

    assert _local_llm_sync(db, [{"role": "user", "content": "hi"}]) == '{"decisions": []}'


def test_null_content_does_not_crash(monkeypatch):
    """Some builds return content: null when the whole budget went to reasoning."""
    db = _memory_db()
    monkeypatch.setattr(
        httpx, "post",
        lambda url, json=None, timeout=None: _FakeResponse({"choices": [{"message": {"content": None}}]}),
    )

    assert _local_llm_sync(db, [{"role": "user", "content": "hi"}]) == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("plain", "plain"),
        ("<think>a</think>answer", "answer"),
        ("<think>unterminated", ""),
        ('  {"a": 1}  ', '{"a": 1}'),
    ],
)
def test_strip_reasoning_cases(raw, expected):
    assert _strip_reasoning(raw) == expected


def test_advisor_default_call_passes_the_decision_schema(monkeypatch):
    db = _memory_db()
    seen: dict[str, Any] = {}

    def _spy(_db, messages, json_schema=None, sampling=None, **kwargs):
        seen["schema"] = json_schema
        seen["sampling"] = sampling
        seen.update(kwargs)
        return json.dumps({"decisions": []})

    monkeypatch.setattr(review_mod, "_local_llm_sync", _spy)

    from app.decision.advisor.llm_decision import (
        DECISION_SAMPLING,
        _default_llm_call,
        completion_budget,
    )

    telemetry: dict[str, Any] = {}
    _default_llm_call(
        db,
        [{"role": "user", "content": "x"}],
        max_tokens=completion_budget(12),
        telemetry=telemetry,
    )

    assert seen["schema"] is DECISION_JSON_SCHEMA
    # The decision call opts out of the near-greedy prose default.
    assert seen["sampling"] == DECISION_SAMPLING
    # A completion budget sized to the universe, and a sink for the endpoint's
    # own account of the call — without the latter a truncated completion is
    # indistinguishable from a model that ignored the schema.
    assert seen["max_tokens"] == completion_budget(12)
    assert seen["telemetry"] is telemetry


def test_decision_sampling_uses_the_published_non_thinking_preset():
    """The preset is adopted whole, including the field that bounds repetition.

    ``presence_penalty`` 1.5 is part of Qwen's published non-thinking preset,
    recommended specifically for quantized checkpoints. Taking the rest of the
    preset and dropping it left the decision call with no repetition defence at
    all — the server runs ``repeat_penalty`` 1.0, ``frequency_penalty`` 0 and
    ``dry_multiplier`` 0, and ``_local_llm_sync`` zeroes the server's own
    ``--presence-penalty 1.5`` for every caller.
    """
    from app.decision.advisor.llm_decision import DECISION_SAMPLING

    assert DECISION_SAMPLING == {
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "presence_penalty": 1.5,
    }


def test_local_llm_sync_neutralises_the_servers_presence_penalty(monkeypatch):
    """The server runs --presence-penalty 1.5; the default neutralises it.

    Prose callers are deliberately near-greedy and have never shown the runaway
    this penalty guards against, so they keep the zero. Structured callers opt
    back in through ``sampling`` — see ``DECISION_SAMPLING``.
    """
    seen: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}

    def _post(_url, json=None, timeout=None):
        seen.update(json or {})
        return _Resp()

    monkeypatch.setattr(review_mod.httpx, "post", _post)
    monkeypatch.setattr(review_mod, "resolve_llm_base_url", lambda _db: "http://x/v1")
    monkeypatch.setattr(review_mod, "resolve_llm_model", lambda _db: "m")

    review_mod._local_llm_sync(_memory_db(), [{"role": "user", "content": "x"}])
    assert seen["presence_penalty"] == 0.0
    # Prose callers keep the deliberate near-greedy default.
    assert seen["temperature"] == 0.2


def test_local_llm_sync_sampling_overrides_win(monkeypatch):
    seen: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}

    def _post(_url, json=None, timeout=None):
        seen.update(json or {})
        return _Resp()

    monkeypatch.setattr(review_mod.httpx, "post", _post)
    monkeypatch.setattr(review_mod, "resolve_llm_base_url", lambda _db: "http://x/v1")
    monkeypatch.setattr(review_mod, "resolve_llm_model", lambda _db: "m")

    review_mod._local_llm_sync(
        _memory_db(),
        [{"role": "user", "content": "x"}],
        sampling={"temperature": 0.7, "top_k": 20},
    )
    assert seen["temperature"] == 0.7
    assert seen["top_k"] == 20
    # Not asked for, so the neutralising default stands.
    assert seen["presence_penalty"] == 0.0

    # Asked for, so it has to survive payload assembly. The default is seeded
    # first and ``sampling`` applied over it; reordering those two steps would
    # silently restore the zero and take the repetition defence away again.
    from app.decision.advisor.llm_decision import DECISION_SAMPLING

    seen.clear()
    review_mod._local_llm_sync(
        _memory_db(),
        [{"role": "user", "content": "x"}],
        sampling=DECISION_SAMPLING,
    )
    assert seen["presence_penalty"] == 1.5


def test_ticker_enum_is_pinned_to_the_allowed_universe():
    """An out-of-universe ticker should be ungrammatical, not merely invalid."""
    from app.decision.advisor.llm_decision import build_decision_schema

    schema = build_decision_schema(["iwda.as", "ASML.AS"])
    ticker = schema["properties"]["decisions"]["items"]["properties"]["ticker"]
    assert ticker["enum"] == ["ASML.AS", "IWDA.AS"]
    # No universe given (the module-level template) leaves ticker unconstrained.
    assert "enum" not in build_decision_schema()["properties"]["decisions"]["items"][
        "properties"
    ]["ticker"]


def test_thesis_is_declared_before_action():
    """llama.cpp emits object keys in declared order — this is the in-band CoT."""
    from app.decision.advisor.llm_decision import build_decision_schema

    order = list(
        build_decision_schema()["properties"]["decisions"]["items"]["properties"]
    )
    assert order.index("thesis") < order.index("action")


def test_decision_schema_matches_the_validator_contract():
    """The grammar must not permit an action the validator would reject."""
    from app.decision.advisor.llm_decision import VALID_ACTIONS

    item = DECISION_JSON_SCHEMA["properties"]["decisions"]["items"]
    assert set(item["properties"]["action"]["enum"]) == set(VALID_ACTIONS)
    assert set(item["required"]) == {
        "ticker", "action", "target_weight", "thesis", "confidence",
    }
    weight = item["properties"]["target_weight"]
    assert weight["minimum"] == 0 and weight["maximum"] == 1


def test_lesson_schema_declares_the_tag_keys_the_parser_reads():
    """llama.cpp's schema→GBNF converter needs concrete properties, not free-form."""
    tags = LESSON_JSON_SCHEMA["properties"]["lessons"]["items"]["properties"]["tags"]
    assert set(tags["properties"]) == {"signals", "regime", "sectors"}
    assert LESSON_JSON_SCHEMA["properties"]["lessons"]["items"]["required"] == ["lesson"]


def test_lesson_schema_bounds_every_free_text_field():
    """An unbounded grammar string runs to the token limit and truncates the object.

    This is the exposure the decision schema had before ``MAX_THESIS_CHARS``:
    a grammar constrains structure, not termination, so inside a string every
    repeated token is still legal JSON. Each cap must also stay under
    llama.cpp's 2000-repetition threshold, above which the grammar fails open.
    """
    from app.decision.advisor.reflection import _MAX_LESSON_CHARS, _MAX_TAG_CHARS

    item = LESSON_JSON_SCHEMA["properties"]["lessons"]["items"]
    tags = item["properties"]["tags"]["properties"]
    caps = [
        item["properties"]["lesson"]["maxLength"],
        tags["regime"]["maxLength"],
        tags["signals"]["items"]["maxLength"],
        tags["sectors"]["items"]["maxLength"],
    ]
    assert caps == [_MAX_LESSON_CHARS, _MAX_TAG_CHARS, _MAX_TAG_CHARS, _MAX_TAG_CHARS]
    assert all(0 < cap < 2000 for cap in caps)
    # Bounded arrays too — three unbounded lists of bounded strings is still an
    # unbounded completion.
    assert tags["signals"]["maxItems"] and tags["sectors"]["maxItems"]


def test_reflection_call_is_budgeted_and_uses_the_structured_preset(monkeypatch):
    """Without a ``max_tokens`` the reflection call took the 8192 default."""
    from app.decision.advisor import reflection as reflection_mod
    from app.decision.advisor.llm_decision import DECISION_SAMPLING

    seen: dict = {}

    def _fake_sync(db, messages, **kwargs):
        seen.update(kwargs)
        return "{}"

    monkeypatch.setattr(
        "app.decision.llm_portfolio.review._local_llm_sync", _fake_sync
    )
    reflection_mod._default_llm_call(_memory_db(), [{"role": "user", "content": "x"}])

    assert seen["json_schema"] is LESSON_JSON_SCHEMA
    assert seen["max_tokens"] == reflection_mod._COMPLETION_TOKENS
    assert seen["max_tokens"] < 8192
    assert seen["sampling"] is DECISION_SAMPLING


def _stub_endpoint(monkeypatch, body: dict, *, status: int = 200, seen: dict | None = None):
    """Point ``_local_llm_sync`` at a canned chat-completions response."""

    class _Resp:
        status_code = status

        def raise_for_status(self):
            return None

        def json(self):
            return body

        @property
        def text(self):
            return json.dumps(body)

    def _post(_url, json=None, timeout=None):
        if seen is not None:
            seen.update(json or {})
        return _Resp()

    monkeypatch.setattr(review_mod.httpx, "post", _post)
    monkeypatch.setattr(review_mod, "resolve_llm_base_url", lambda _db: "http://x/v1")
    monkeypatch.setattr(review_mod, "resolve_llm_model", lambda _db: "m")


def test_telemetry_reports_a_completion_cut_off_at_the_token_limit(monkeypatch):
    """finish_reason is the only thing that distinguishes the two JSON failures.

    A grammar cannot emit malformed JSON, so a parse failure means either the
    model ignored the schema or the completion ran out of budget. Discarding
    finish_reason meant sixteen cycles recorded the same error string for
    causes with opposite fixes.
    """
    _stub_endpoint(
        monkeypatch,
        {
            "choices": [{"message": {"content": '{"decisions": [{"tick'}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 2043, "completion_tokens": 1180, "total_tokens": 3223},
        },
    )

    telemetry: dict[str, Any] = {}
    review_mod._local_llm_sync(
        _memory_db(), [{"role": "user", "content": "x"}],
        json_schema={"type": "object"}, max_tokens=1180, telemetry=telemetry,
    )

    assert telemetry["finish_reason"] == "length"
    assert telemetry["truncated"] is True
    assert telemetry["prompt_tokens"] == 2043
    assert telemetry["max_tokens"] == 1180
    assert telemetry["schema_constrained"] is True
    assert telemetry["schema_dropped"] is False


def test_telemetry_marks_a_clean_stop_as_not_truncated(monkeypatch):
    _stub_endpoint(
        monkeypatch,
        {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}},
    )

    telemetry: dict[str, Any] = {}
    review_mod._local_llm_sync(
        _memory_db(), [{"role": "user", "content": "x"}], telemetry=telemetry
    )

    assert telemetry["truncated"] is False
    assert telemetry["schema_constrained"] is False


def test_max_tokens_defaults_and_can_be_overridden(monkeypatch):
    seen: dict[str, Any] = {}
    _stub_endpoint(
        monkeypatch,
        {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
        seen=seen,
    )

    review_mod._local_llm_sync(_memory_db(), [{"role": "user", "content": "x"}])
    assert seen["max_tokens"] == review_mod.DEFAULT_MAX_TOKENS

    review_mod._local_llm_sync(
        _memory_db(), [{"role": "user", "content": "x"}], max_tokens=1024
    )
    assert seen["max_tokens"] == 1024


def test_answer_parked_on_reasoning_content_is_still_read(monkeypatch):
    """Some builds leave ``content`` empty and put the answer on the side field.

    Returning "" there is recorded downstream as "the model returned nothing",
    which is wrong about both the model and the fix.
    """
    payload = json.dumps({"decisions": []})
    _stub_endpoint(
        monkeypatch,
        {
            "choices": [
                {"message": {"content": "", "reasoning_content": payload}, "finish_reason": "stop"}
            ]
        },
    )

    telemetry: dict[str, Any] = {}
    out = review_mod._local_llm_sync(
        _memory_db(), [{"role": "user", "content": "x"}], telemetry=telemetry
    )

    assert out == payload
    assert telemetry["used_reasoning_content"] is True
