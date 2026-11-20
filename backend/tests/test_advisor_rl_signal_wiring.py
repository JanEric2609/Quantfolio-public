"""Tests for the advisory RL-signal wiring into the LLM decision prompt
(Track D2) — rl_suggested_weight per candidate + rl_signal_note, threaded
through decide_trades -> _build_user_prompt. Never a hard override: the
deterministic suggested_weight stays untouched either way."""
from __future__ import annotations

import json

from conftest import _memory_db

from app.decision.advisor.decision import McSummary, TradeProposal
from app.decision.advisor.llm_decision import _build_user_prompt, decide_trades
from app.decision.advisor.risk_gate import check_risk_gate


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


class TestBuildUserPromptRlSignal:
    def test_no_rl_signal_omits_rl_fields(self):
        proposal = _proposal()
        gate = check_risk_gate(proposal)

        payload = json.loads(_build_user_prompt(proposal, gate, []))

        assert "rl_signal_note" not in payload
        for entry in payload["candidates"]:
            assert "rl_suggested_weight" not in entry

    def test_rl_signal_adds_per_candidate_weight_and_note(self):
        proposal = _proposal()
        gate = check_risk_gate(proposal)
        rl_signal = {"AAA": 0.55}

        payload = json.loads(_build_user_prompt(proposal, gate, [], rl_signal=rl_signal))

        assert "rl_signal_note" in payload
        by_ticker = {c["ticker"]: c for c in payload["candidates"]}
        assert by_ticker["AAA"]["rl_suggested_weight"] == 0.55
        # BBB has no RL weight -> field omitted, not defaulted to a value.
        assert "rl_suggested_weight" not in by_ticker["BBB"]

    def test_rl_signal_never_overrides_the_deterministic_suggested_weight(self):
        proposal = _proposal()
        gate = check_risk_gate(proposal)
        rl_signal = {"AAA": 0.99}

        payload = json.loads(_build_user_prompt(proposal, gate, [], rl_signal=rl_signal))

        by_ticker = {c["ticker"]: c for c in payload["candidates"]}
        assert by_ticker["AAA"]["suggested_weight"] == 0.6
        assert by_ticker["AAA"]["rl_suggested_weight"] == 0.99


class TestDecideTradesThreadsRlSignal:
    def test_rl_signal_reaches_the_prompt_sent_to_the_llm(self):
        db = _memory_db()
        proposal = _proposal()
        gate = check_risk_gate(proposal)
        seen: list[list[dict[str, str]]] = []

        def _capture(_db, messages):
            seen.append([dict(m) for m in messages])
            return json.dumps({"decisions": [_decision("AAA")]})

        decide_trades(
            db, proposal, gate,
            llm_call=_capture,
            rl_signal={"AAA": 0.42},
        )

        assert seen, "llm_call was never invoked"
        user_payload = json.loads(seen[0][1]["content"])
        by_ticker = {c["ticker"]: c for c in user_payload["candidates"]}
        assert by_ticker["AAA"]["rl_suggested_weight"] == 0.42

    def test_absent_rl_signal_leaves_prompt_unchanged(self):
        db = _memory_db()
        proposal = _proposal()
        gate = check_risk_gate(proposal)
        seen: list[list[dict[str, str]]] = []

        def _capture(_db, messages):
            seen.append([dict(m) for m in messages])
            return json.dumps({"decisions": [_decision("AAA")]})

        decide_trades(db, proposal, gate, llm_call=_capture)

        user_payload = json.loads(seen[0][1]["content"])
        assert "rl_signal_note" not in user_payload
