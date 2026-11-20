"""Tests for app.foundation.llm.structured — the shared structured-JSON-call
hardening (Phase 6, unified-portfolio-engine-implementation.md).

These mirror the existing advisor/llm_decision truncation-handling tests
(tests/test_advisor_llm_truncation.py) but at the generic, key-parameterised
level, and additionally cover reflection.py's "lessons" array and
dossier_writer's plain-object shape now sharing this code.
"""
from __future__ import annotations

import json

import pytest

from app.foundation.llm.structured import (
    build_retry_messages,
    completion_budget,
    parse_structured_completion,
    salvage_truncated_array,
    strip_code_fence,
    unusable_reason,
)


def _lesson(text: str) -> dict:
    return {"lesson": text, "tags": {"signals": ["momentum"]}}


def test_strip_code_fence_removes_wrapping_fence():
    assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_salvage_truncated_array_recovers_complete_elements():
    raw = '{"lessons": [' + json.dumps(_lesson("A")) + "," + json.dumps(_lesson("B"))[:20]
    recovered = salvage_truncated_array(raw, "lessons")
    assert recovered is not None
    assert [item["lesson"] for item in recovered["lessons"]] == ["A"]


def test_salvage_truncated_array_none_for_no_complete_element():
    assert salvage_truncated_array('{"lessons": [{"lesson": "A', "lessons") is None
    assert salvage_truncated_array("no json here", "lessons") is None


def test_salvage_truncated_array_key_specific():
    """Salvaging on the wrong array key finds nothing — the key must match
    the field that was actually truncated."""
    raw = '{"decisions": [' + json.dumps({"ticker": "AAA"}) + "]}"
    assert salvage_truncated_array(raw, "lessons") is None


def test_parse_structured_completion_labels_how_the_body_was_read():
    complete = json.dumps({"lessons": [_lesson("A")]})
    assert parse_structured_completion(complete, "lessons")[1] == "strict"
    assert parse_structured_completion(f"```json\n{complete}\n```", "lessons")[1] == "strict"
    assert parse_structured_completion(f"Here you go:\n{complete}", "lessons")[1] == "embedded"
    truncated = '{"lessons": [' + json.dumps(_lesson("A")) + ',{"lesson": "cut off'
    assert parse_structured_completion(truncated, "lessons")[1] == "salvaged"
    assert parse_structured_completion("sorry, no json", "lessons")[1] == "unparseable"


def test_unusable_reason_distinguishes_failure_classes():
    assert "empty completion" in unusable_reason("", {})
    assert "reasoning text" in unusable_reason("", {"reasoning_leak": True})
    truncated = unusable_reason(
        '{"lessons": [{"lesson"', {"truncated": True, "max_tokens": 512}
    )
    assert "512-token limit" in truncated
    assert "cut off" in truncated
    assert "rejected the JSON schema" in unusable_reason(
        "sorry", {"schema_dropped": True, "schema_reject_status": 400}
    )
    assert unusable_reason("sorry", {}) == "LLM response was not valid JSON"


def test_build_retry_messages_rebuilds_from_base_with_excerpt():
    base = [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]
    long_raw = "x" * 2000
    messages = build_retry_messages(base, long_raw, "try again", excerpt_chars=50)
    assert messages[:2] == base
    assert len(messages) == 4
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"].endswith("…[truncated]")
    assert len(messages[2]["content"]) <= 50 + len("…[truncated]")
    assert messages[3] == {"role": "user", "content": "try again"}
    # Rebuilding from base (not accumulating) is the point: a second call on
    # the same base+new raw yields the same 4-message shape, not a growing one.
    messages2 = build_retry_messages(base, "short", "again", excerpt_chars=50)
    assert len(messages2) == 4


def test_completion_budget_scales_and_clamps():
    assert completion_budget(0, tokens_per_item=100, min_tokens=500, max_tokens=4000) == 500
    assert completion_budget(10, tokens_per_item=100, min_tokens=500, max_tokens=4000) == 1500
    assert completion_budget(1000, tokens_per_item=100, min_tokens=500, max_tokens=4000) == 4000


# ---------------------------------------------------------------------------
# Regressions found in the phases 1-8 audit
# ---------------------------------------------------------------------------

_VALID = (
    '{"direction":"long","conviction":0.7,"horizon_months":6,'
    '"thesis":"ok","key_risks":["a"],"signal_breakdown":{}}'
)


@pytest.mark.parametrize(
    "wrapper",
    [
        pytest.param(_VALID, id="bare"),
        pytest.param(f"```json\n{_VALID}\n```", id="fence-at-start"),
        pytest.param(f"Here you go:\n```json\n{_VALID}\n```", id="prose-then-fence"),
        pytest.param(f"{_VALID}\nHope that helps.", id="object-then-prose"),
        pytest.param(f"```json\n{_VALID}\n```\nHope that helps.", id="fence-then-prose"),
    ],
)
def test_common_wrapper_shapes_all_parse(wrapper):
    """Every one of these is a *correct* answer the model produced.

    Before the audit fix only the first two parsed: the fence was stripped only
    at position 0, and the embedded-object path was gated on the object not
    starting at position 0. The other three each burned the full retry budget
    and then fell back, reporting the model's good answer as unparseable.
    """
    parsed, how = parse_structured_completion(wrapper, "key_risks")
    assert parsed is not None, how
    assert parsed["direction"] == "long"


def test_salvage_prefers_the_real_answer_over_an_echoed_example():
    """Models echo the schema example before answering; first-match salvaged it.

    Taking the first array meant the advisor could act on a placeholder ticker
    and report it as a successful salvage.
    """
    raw = (
        'Example: {"decisions":[{"ticker":"EXAMPLE"}]}\n'
        'Now the real answer: {"decisions":[{"ticker":"REAL"},{"ticker":"REAL2"},{"tick'
    )
    salvaged = salvage_truncated_array(raw, "decisions")
    assert salvaged is not None
    assert [d["ticker"] for d in salvaged["decisions"]] == ["REAL", "REAL2"]


def test_nan_and_infinity_are_rejected_not_parsed():
    """Python's json accepts these; JSON does not, and the browser chokes.

    Parsed through, they land in dossier_json and crash the UI's JSON.parse.
    """
    for literal in ("NaN", "Infinity", "-Infinity"):
        raw = _VALID.replace("0.7", literal)
        parsed, how = parse_structured_completion(raw, "key_risks")
        assert parsed is None, f"{literal} should not parse, got {parsed}"
        assert how == "unparseable"
