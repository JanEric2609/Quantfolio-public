"""Tests for the decision stage's completion budget and truncation handling.

Sixteen advisor cycles failed with the single error string "LLM response was not
valid JSON". Under a grammar the model *cannot* emit malformed JSON, so that
error only ever meant one of three things — an empty completion, a schema the
server dropped, or a completion cut off at the token limit — and the loop had no
way to say which. These cover the mechanics that produced it:

* the one unbounded field in the decision grammar (``thesis``) is now capped, so
  a runaway string cannot consume the budget;
* the retry loop no longer echoes a whole rejected completion back into the
  prompt, which is what pushed later attempts past llama-server's context
  window (it runs with ``--no-context-shift``, so an overrun truncates rather
  than rolls);
* a truncated answer's complete decisions are recovered instead of discarded;
* the failure is named for what it was.
"""
import json

from conftest import _memory_db

from app.decision.advisor.decision import McSummary, TradeProposal
from app.decision.advisor.llm_decision import (
    MAX_THESIS_CHARS,
    build_decision_schema,
    completion_budget,
    decide_trades,
    parse_completion,
    salvage_truncated_decisions,
)
from app.decision.advisor.risk_gate import check_risk_gate

# llama.cpp compiles string length bounds into GBNF repetitions and refuses any
# single repetition above this, silently falling back to unconstrained output.
_GRAMMAR_REPETITION_CEILING = 2000


def _proposal() -> TradeProposal:
    return TradeProposal(
        portfolio_id="p1",
        suggested_weights={"AAA": 0.6, "BBB": 0.4},
        mc_summaries={
            "AAA": McSummary("AAA", "equity", -0.05, 0.01, 0.08, 0.6, 100.0),
            "BBB": McSummary("BBB", "equity", -0.15, 0.0, 0.12, 0.5, 50.0),
        },
        risk_envelope={"var_95_daily": 0.01, "cvar_95_daily": 0.02, "max_drawdown": 0.10},
        current_book={},
        optimizer_status="completed",
    )


def _decision(ticker: str, thesis: str = "Positive MC skew.") -> dict:
    return {
        "ticker": ticker, "thesis": thesis, "action": "buy",
        "target_weight": 0.1, "confidence": 0.7,
    }


# ---------------------------------------------------------------------------
# Schema bounds — the completion has to be finite
# ---------------------------------------------------------------------------


def test_thesis_is_length_bounded_so_the_completion_cannot_run_away():
    schema = build_decision_schema(["AAA", "BBB"])
    thesis = schema["properties"]["decisions"]["items"]["properties"]["thesis"]

    assert thesis["maxLength"] == MAX_THESIS_CHARS
    # Above llama.cpp's repetition ceiling the bound is dropped and the grammar
    # falls back to unconstrained generation — the opposite of the intent.
    assert thesis["maxLength"] < _GRAMMAR_REPETITION_CEILING


def test_decisions_array_is_capped_at_the_nameable_universe():
    schema = build_decision_schema(["AAA", "BBB", "aaa"])

    # Case-folded, so a duplicate ticker does not inflate the cap.
    assert schema["properties"]["decisions"]["maxItems"] == 2
    assert schema["properties"]["decisions"]["minItems"] == 1


def test_schema_without_an_allowed_set_stays_unbounded():
    """The module-level template has no universe to size itself against."""
    schema = build_decision_schema()

    assert "maxItems" not in schema["properties"]["decisions"]
    assert "enum" not in schema["properties"]["decisions"]["items"]["properties"]["ticker"]


def test_completion_budget_scales_with_the_universe_and_stays_bounded():
    assert completion_budget(2) < completion_budget(12)
    # A flat 8192-token budget plus a 2k prompt left no room for a retry inside
    # a 16k context; the ceiling keeps two rounds affordable.
    assert completion_budget(500) <= 4096
    assert completion_budget(0) >= 512


# ---------------------------------------------------------------------------
# Salvaging a cut-off completion
# ---------------------------------------------------------------------------


def test_salvage_keeps_complete_decisions_and_drops_the_partial_tail():
    raw = (
        '{"decisions": ['
        '{"ticker": "AAA", "thesis": "one", "action": "buy", "target_weight": 0.1, "confidence": 0.7},'
        '{"ticker": "BBB", "thesis": "two", "action": "sell", "target_weight": 0.0, "confidence": 0.6},'
        '{"ticker": "CCC", "thesis": "cut off mid'
    )

    recovered = salvage_truncated_decisions(raw)

    assert [d["ticker"] for d in recovered["decisions"]] == ["AAA", "BBB"]


def test_salvage_is_not_confused_by_braces_inside_a_thesis():
    raw = (
        '{"decisions": ['
        '{"ticker": "AAA", "thesis": "targets {0.1} and \\"quoted\\" text", '
        '"action": "buy", "target_weight": 0.1, "confidence": 0.7},'
        '{"ticker": "BBB", "thesis": "trunc'
    )

    recovered = salvage_truncated_decisions(raw)

    assert len(recovered["decisions"]) == 1
    assert "{0.1}" in recovered["decisions"][0]["thesis"]


def test_salvage_invents_nothing_when_no_decision_completed():
    assert salvage_truncated_decisions('{"decisions": [{"ticker": "AA') is None
    assert salvage_truncated_decisions("Sure! Here are my picks.") is None


def test_parse_completion_labels_how_the_body_was_read():
    complete = json.dumps({"decisions": [_decision("AAA")]})
    assert parse_completion(complete)[1] == "strict"
    assert parse_completion(f"```json\n{complete}\n```")[1] == "strict"
    assert parse_completion(f"Here you go:\n{complete}")[1] == "embedded"
    assert parse_completion('{"decisions": [' + json.dumps(_decision("AAA")) + ',{"tick')[1] == (
        "salvaged"
    )
    assert parse_completion("no json here")[1] == "unparseable"


# ---------------------------------------------------------------------------
# decide_trades — what the cycle records
# ---------------------------------------------------------------------------


def test_truncated_completion_still_yields_the_decisions_it_completed():
    """A cut-off answer used to trade nothing at all.

    The complete elements are a real answer the model committed to; discarding
    them meant a day the model *did* decide was recorded as a failure.
    """
    db = _memory_db()
    proposal = _proposal()
    gate = check_risk_gate(proposal)
    truncated = (
        '{"decisions": [' + json.dumps(_decision("AAA")) + ',{"ticker": "BBB", "thesis": "cut'
    )

    attempts: list[dict] = []
    decisions, errors = decide_trades(
        db, proposal, gate,
        llm_call=lambda _db, _m: truncated,
        attempts=attempts,
    )

    assert [d.ticker for d in decisions] == ["AAA"]
    assert any("cut off" in e for e in errors)
    assert attempts[0]["parsed_as"] == "salvaged"


def test_each_unusable_completion_gets_its_own_diagnosis():
    """One error string for four distinct failures is what hid the real cause."""
    from app.decision.advisor.llm_decision import _unusable_reason

    truncated = _unusable_reason(
        '{"decisions": [{"ticker"',
        {"finish_reason": "length", "truncated": True, "max_tokens": 792},
    )
    assert "792-token limit" in truncated
    assert "cut off" in truncated

    assert "empty completion" in _unusable_reason("", {})
    assert "reasoning text" in _unusable_reason("", {"reasoning_leak": True})
    assert "rejected the JSON schema" in _unusable_reason(
        "sorry", {"schema_dropped": True, "schema_reject_status": 400}
    )
    # Only a genuinely malformed body keeps the original wording.
    assert _unusable_reason("sorry", {}) == "LLM response was not valid JSON"


def test_retry_prompt_does_not_grow_with_each_rejected_completion():
    """Stacking rejected answers is what made the retries unaffordable.

    Attempt 1 could fill the whole completion budget; echoing that back left
    attempt 3 needing more than the server's context window, so every retry
    after a runaway answer was guaranteed to fail too.
    """
    db = _memory_db()
    proposal = _proposal()
    gate = check_risk_gate(proposal)
    runaway = "x" * 40_000
    seen: list[list[dict[str, str]]] = []

    def _garbage(_db, messages):
        seen.append([dict(m) for m in messages])
        return runaway

    decide_trades(db, proposal, gate, llm_call=_garbage)

    assert len(seen) == 3, "expected the full retry budget to be spent"
    # Every attempt carries system + user, plus at most one correction round.
    assert [len(m) for m in seen] == [2, 4, 4]
    for messages in seen[1:]:
        quoted = messages[2]["content"]
        assert len(quoted) < len(runaway)
        assert quoted.endswith("…[truncated]")
    # The prompt cannot creep upward across attempts.
    assert sum(len(m["content"]) for m in seen[2]) <= sum(len(m["content"]) for m in seen[1])


def test_attempt_records_carry_the_endpoint_account_of_each_call():
    db = _memory_db()
    proposal = _proposal()
    gate = check_risk_gate(proposal)
    attempts: list[dict] = []

    decide_trades(
        db, proposal, gate,
        llm_call=lambda _db, _m: json.dumps({"decisions": [_decision("AAA")]}),
        attempts=attempts,
    )

    assert len(attempts) == 1
    assert attempts[0]["attempt"] == 1
    assert attempts[0]["parsed_as"] == "strict"
    assert attempts[0]["decisions"] == 1
    assert attempts[0]["errors"] == []
