"""Tests for the sell side of the advisor decision prompt and validator.

The paper sleeve made one trade in twenty days against a fully-invested book.
The cause was not the model declining to sell — it was the prompt never showing
a sell to decline. ``trade_to_suggested_eur`` was an absolute value inside a
block named ``candidates``, so a required EUR 15.5k trim of the core holding
arrived as a positive number next to a positive forward return, and a holding
the risk gate dropped vanished from ``allowed_tickers`` entirely while the
system prompt insisted on choosing only among allowed tickers.

These tests pin the shape of the prompt the model reads, because that shape is
the behaviour.
"""
from __future__ import annotations

import json

from app.decision.advisor.decision import McSummary, TradeProposal
from app.decision.advisor.llm_decision import (
    _build_user_prompt,
    _funding_gap_note,
    _validate_decisions,
    decide_trades,
    decision_universe,
    sell_only_symbols,
)
from app.decision.advisor.risk_gate import RiskGateResult

# A fully invested book with EUR 20 of cash and a core
# holding the optimiser wants cut by ~40%.
TOTAL_VALUE = 39_035.00
CASH = 20.00
MIN_TICKET = 390.35


def _mc(symbol: str, p50: float = 0.02, prob: float = 0.58, spot: float = 100.0) -> McSummary:
    return McSummary(
        symbol=symbol,
        instrument_type="equity",
        p5=p50 - 0.09,
        p50=p50,
        p95=p50 + 0.11,
        prob_positive=prob,
        spot=spot,
    )


def _proposal(
    *,
    suggested: dict[str, float] | None = None,
    book: dict[str, float] | None = None,
) -> TradeProposal:
    suggested = suggested if suggested is not None else {
        "IWDA.AS": 0.55, "ALV.DE": 0.05, "ASML.AS": 0.20, "SAP.DE": 0.20,
    }
    book = book if book is not None else {"IWDA.AS": 37_000.0, "ALV.DE": 2_015.0}
    symbols = set(suggested) | set(book)
    return TradeProposal(
        portfolio_id="p1",
        suggested_weights=suggested,
        mc_summaries={s: _mc(s) for s in symbols},
        risk_envelope={"var_95_daily": 0.021, "cvar_95_daily": 0.030, "max_drawdown": 0.18},
        current_book=book,
        optimizer_status="ok",
    )


def _gate(proposal: TradeProposal, *, passing: list[str] | None = None) -> RiskGateResult:
    symbols = sorted(proposal.suggested_weights) if passing is None else sorted(passing)
    return RiskGateResult(
        passed=True,
        envelope=proposal.risk_envelope,
        ceilings={"var_95_daily": 0.03, "cvar_95_daily": 0.045, "max_drawdown": 0.25},
        breaches=[],
        blocked=[],
        passing_symbols=symbols,
    )


def _prompt(proposal: TradeProposal, gate: RiskGateResult) -> dict:
    return json.loads(
        _build_user_prompt(
            proposal, gate, [],
            cash_eur=CASH,
            total_value_eur=TOTAL_VALUE,
            min_ticket_eur=MIN_TICKET,
            max_turnover_eur=5_855.20,
        )
    )


def _entry(payload: dict, ticker: str) -> dict:
    return next(c for c in payload["candidates"] if c["ticker"] == ticker)


# --------------------------------------------------------------------------
# The sign
# --------------------------------------------------------------------------

def test_a_required_trim_is_shown_as_a_negative_delta() -> None:
    """The core holding needs cutting; the number must say so."""
    proposal = _proposal()
    payload = _prompt(proposal, _gate(proposal))

    iwda = _entry(payload, "IWDA.AS")
    # 0.55 * 39,035 - 37,000 = -15,530.75
    assert iwda["delta_to_target_eur"] < 0
    assert round(iwda["delta_to_target_eur"], 2) == -15_530.75
    assert iwda["direction"] == "sell"


def test_a_genuine_buy_keeps_a_positive_delta() -> None:
    proposal = _proposal()
    payload = _prompt(proposal, _gate(proposal))

    asml = _entry(payload, "ASML.AS")
    assert asml["delta_to_target_eur"] > 0
    assert asml["direction"] == "buy"
    assert asml["held"] is False


def test_current_weight_is_stated_so_direction_needs_no_cross_object_arithmetic() -> None:
    proposal = _proposal()
    payload = _prompt(proposal, _gate(proposal))

    iwda = _entry(payload, "IWDA.AS")
    assert iwda["current_weight"] == round(37_000.0 / TOTAL_VALUE, 4)
    assert iwda["held_eur"] == 37_000.0
    assert iwda["current_weight"] > iwda["suggested_weight"]


# --------------------------------------------------------------------------
# The sell_candidates block
# --------------------------------------------------------------------------

def test_sell_candidates_lists_overweight_holdings_by_excess() -> None:
    proposal = _proposal(
        suggested={"IWDA.AS": 0.55, "ALV.DE": 0.01, "ASML.AS": 0.20},
        book={"IWDA.AS": 37_000.0, "ALV.DE": 2_015.0},
    )
    payload = _prompt(proposal, _gate(proposal))

    tickers = [s["ticker"] for s in payload["sell_candidates"]]
    assert tickers == ["IWDA.AS", "ALV.DE"]  # sorted by excess, descending
    assert payload["sell_candidates"][0]["excess_eur"] > 0
    # Proceeds are net of the commission the sell itself pays.
    assert (
        payload["sell_candidates"][0]["frees_cash_eur"]
        < payload["sell_candidates"][0]["excess_eur"]
    )


def test_sell_candidates_is_empty_when_nothing_is_overweight() -> None:
    holdings = 39_000.0
    total = holdings + CASH
    suggested = {"IWDA.AS": 0.5, "ASML.AS": 0.5}
    proposal = _proposal(
        suggested=suggested,
        book={s: w * holdings for s, w in suggested.items()},
    )
    payload = json.loads(
        _build_user_prompt(
            proposal, _gate(proposal), [],
            cash_eur=CASH, total_value_eur=total, min_ticket_eur=MIN_TICKET,
        )
    )
    assert payload["sell_candidates"] == []


# --------------------------------------------------------------------------
# The minimum-ticket floor
# --------------------------------------------------------------------------

def test_a_delta_below_the_minimum_ticket_is_not_a_trade_signal() -> None:
    """Without this the loop churns: unexecutable intent still hits the ledger."""
    holdings = 39_000.0
    total = holdings + CASH
    suggested = {"IWDA.AS": 0.5, "ASML.AS": 0.5}
    proposal = _proposal(
        suggested=suggested,
        book={s: w * holdings for s, w in suggested.items()},
    )
    payload = json.loads(
        _build_user_prompt(
            proposal, _gate(proposal), [],
            cash_eur=CASH, total_value_eur=total, min_ticket_eur=MIN_TICKET,
        )
    )
    for entry in payload["candidates"]:
        # Each delta here is w * 20 — a few euro.
        assert abs(entry["delta_to_target_eur"]) < MIN_TICKET
        assert entry["actionable"] is False
        assert entry["direction"] == "hold"
        assert "minimum ticket" in entry["not_actionable_reason"]


def test_an_actionable_delta_keeps_its_direction() -> None:
    proposal = _proposal()
    payload = _prompt(proposal, _gate(proposal))

    iwda = _entry(payload, "IWDA.AS")
    assert iwda["actionable"] is True
    assert "not_actionable_reason" not in iwda


# --------------------------------------------------------------------------
# The per-position sell cap (2026-09-01 underperformance fix)
# --------------------------------------------------------------------------

def test_max_sellable_eur_is_surfaced_for_held_names() -> None:
    """A held position's per-cycle sell cap must be legible to the model, not
    just silently enforced downstream by the executor."""
    proposal = _proposal()
    payload = json.loads(
        _build_user_prompt(
            proposal, _gate(proposal), [],
            cash_eur=CASH, total_value_eur=TOTAL_VALUE, min_ticket_eur=MIN_TICKET,
            max_sell_pct_of_position=1.0 / 3.0,
        )
    )

    iwda = _entry(payload, "IWDA.AS")
    assert iwda["max_sellable_eur"] == round(37_000.0 / 3.0, 2)
    sell_candidate = payload["sell_candidates"][0]
    assert sell_candidate["ticker"] == "IWDA.AS"
    assert sell_candidate["max_sellable_eur"] == round(37_000.0 / 3.0, 2)


def test_max_sellable_eur_absent_for_unheld_names() -> None:
    proposal = _proposal()
    payload = json.loads(
        _build_user_prompt(
            proposal, _gate(proposal), [],
            cash_eur=CASH, total_value_eur=TOTAL_VALUE, min_ticket_eur=MIN_TICKET,
            max_sell_pct_of_position=1.0 / 3.0,
        )
    )

    asml = _entry(payload, "ASML.AS")
    assert "max_sellable_eur" not in asml


def test_max_sellable_eur_omitted_when_the_cap_is_not_configured() -> None:
    """Existing callers that don't pass the cap must see the prompt unchanged."""
    proposal = _proposal()
    payload = _prompt(proposal, _gate(proposal))

    iwda = _entry(payload, "IWDA.AS")
    assert "max_sellable_eur" not in iwda
    assert "max_sellable_eur" not in payload["sell_candidates"][0]


# --------------------------------------------------------------------------
# Risk-gate-blocked holdings
# --------------------------------------------------------------------------

def test_a_gate_blocked_holding_stays_nameable_and_sellable() -> None:
    proposal = _proposal()
    # The gate drops the core holding for breaching a ceiling.
    gate = _gate(proposal, passing=["ALV.DE", "ASML.AS", "SAP.DE"])

    assert "IWDA.AS" in decision_universe(proposal, gate)
    assert sell_only_symbols(proposal, gate) == {"IWDA.AS"}

    payload = _prompt(proposal, gate)
    assert "IWDA.AS" in payload["allowed_tickers"]
    iwda = _entry(payload, "IWDA.AS")
    assert iwda["sell_only"] is True
    # No target above zero is admissible for a name the envelope rejected.
    assert iwda["suggested_weight"] == 0.0
    assert iwda["direction"] == "sell"


def test_the_validator_refuses_to_buy_a_gate_blocked_holding() -> None:
    """check_risk_gate runs once, before the LLM — nothing downstream catches this."""
    parsed = {"decisions": [
        {"ticker": "IWDA.AS", "thesis": "add", "action": "buy",
         "target_weight": 0.99, "confidence": 0.9},
    ]}
    decisions, errors = _validate_decisions(
        parsed, {"IWDA.AS"}, sell_only={"IWDA.AS"}, current_weights={"IWDA.AS": 0.94},
    )
    assert decisions == []
    assert any("risk-gate-blocked" in e for e in errors)


def test_the_validator_refuses_a_sell_that_does_not_reduce_a_blocked_holding() -> None:
    parsed = {"decisions": [
        {"ticker": "IWDA.AS", "thesis": "trim", "action": "sell",
         "target_weight": 0.97, "confidence": 0.6},
    ]}
    decisions, errors = _validate_decisions(
        parsed, {"IWDA.AS"}, sell_only={"IWDA.AS"}, current_weights={"IWDA.AS": 0.94},
    )
    assert decisions == []
    assert any("does not reduce" in e for e in errors)


def test_a_real_trim_of_a_blocked_holding_is_accepted() -> None:
    parsed = {"decisions": [
        {"ticker": "IWDA.AS", "thesis": "cut the concentration", "action": "sell",
         "target_weight": 0.55, "confidence": 0.7},
    ]}
    decisions, errors = _validate_decisions(
        parsed, {"IWDA.AS"}, sell_only={"IWDA.AS"}, current_weights={"IWDA.AS": 0.94},
    )
    assert errors == []
    assert [(d.ticker, d.action, d.target_weight) for d in decisions] == [
        ("IWDA.AS", "sell", 0.55)
    ]


# --------------------------------------------------------------------------
# hold must mean hold
# --------------------------------------------------------------------------

def test_a_hold_whose_target_contradicts_the_held_weight_is_an_error() -> None:
    """The dominant live failure: right target weight, wrong verb."""
    parsed = {"decisions": [
        {"ticker": "IWDA.AS", "thesis": "keep", "action": "hold",
         "target_weight": 0.55, "confidence": 0.6},
    ]}
    decisions, errors = _validate_decisions(
        parsed, {"IWDA.AS"}, current_weights={"IWDA.AS": 0.9479}, hold_tolerance=0.01,
    )
    assert decisions == []
    assert any("hold must keep the current weight" in e for e in errors)


def test_a_hold_at_the_held_weight_is_accepted() -> None:
    parsed = {"decisions": [
        {"ticker": "IWDA.AS", "thesis": "", "action": "hold",
         "target_weight": 0.9479, "confidence": 0.5},
    ]}
    decisions, errors = _validate_decisions(
        parsed, {"IWDA.AS"}, current_weights={"IWDA.AS": 0.9479}, hold_tolerance=0.01,
    )
    assert errors == []
    assert decisions[0].action == "hold"


def test_a_hold_within_tolerance_is_rounding_not_a_contradiction() -> None:
    parsed = {"decisions": [
        {"ticker": "IWDA.AS", "thesis": "", "action": "hold",
         "target_weight": 0.945, "confidence": 0.5},
    ]}
    decisions, errors = _validate_decisions(
        parsed, {"IWDA.AS"}, current_weights={"IWDA.AS": 0.9479}, hold_tolerance=0.01,
    )
    assert errors == []
    assert decisions[0].action == "hold"


def test_hold_is_never_silently_converted_into_a_trade() -> None:
    """A rejected hold must not reach the executor as a sell."""
    parsed = {"decisions": [
        {"ticker": "IWDA.AS", "thesis": "keep", "action": "hold",
         "target_weight": 0.55, "confidence": 0.6},
    ]}
    decisions, _ = _validate_decisions(
        parsed, {"IWDA.AS"}, current_weights={"IWDA.AS": 0.9479}, hold_tolerance=0.01,
    )
    assert all(d.action != "sell" for d in decisions)


# --------------------------------------------------------------------------
# The funding-gap nudge
# --------------------------------------------------------------------------

def test_the_nudge_names_the_funding_gap_when_no_sell_was_offered() -> None:
    note = _funding_gap_note(cash_eur=20.00, min_ticket_eur=390.35, proposed_a_sell=False)
    assert note is not None
    assert "20.00" in note and "390.35" in note
    assert "sell_candidates" in note


def test_the_nudge_is_silent_when_a_sell_was_already_offered() -> None:
    assert _funding_gap_note(
        cash_eur=20.00, min_ticket_eur=390.35, proposed_a_sell=True
    ) is None


def test_the_nudge_is_silent_when_cash_covers_the_minimum_ticket() -> None:
    assert _funding_gap_note(
        cash_eur=5_000.0, min_ticket_eur=390.35, proposed_a_sell=False
    ) is None


# --------------------------------------------------------------------------
# End to end through decide_trades
# --------------------------------------------------------------------------

def test_decide_trades_accepts_the_trim_the_old_prompt_made_invisible() -> None:
    proposal = _proposal()
    gate = _gate(proposal)
    seen: dict = {}

    def _llm(_db, messages):
        seen["user"] = json.loads(messages[-1]["content"])
        return json.dumps({"decisions": [
            {"ticker": "IWDA.AS", "thesis": "cut the concentration",
             "action": "sell", "target_weight": 0.55, "confidence": 0.7},
            {"ticker": "ASML.AS", "thesis": "fund from the trim",
             "action": "buy", "target_weight": 0.20, "confidence": 0.65},
        ]})

    decisions, errors = decide_trades(
        None, proposal, gate, llm_call=_llm,
        cash_eur=CASH, total_value_eur=TOTAL_VALUE, min_ticket_eur=MIN_TICKET,
    )

    assert errors == []
    assert {d.ticker: d.action for d in decisions} == {
        "IWDA.AS": "sell", "ASML.AS": "buy",
    }
    assert seen["user"]["sell_candidates"][0]["ticker"] == "IWDA.AS"


def test_decide_trades_records_each_raw_response() -> None:
    proposal = _proposal()
    raw: list[str] = []

    decide_trades(
        None, proposal, _gate(proposal),
        llm_call=lambda _db, _m: "not json at all",
        cash_eur=CASH, total_value_eur=TOTAL_VALUE, min_ticket_eur=MIN_TICKET,
        raw_responses=raw,
    )

    # One per attempt, so a silent cycle can be diagnosed from its audit row.
    assert raw and all(r == "not json at all" for r in raw)


def test_a_valid_but_unfunded_answer_triggers_one_funding_retry() -> None:
    """Buys with no sell and no cash used to sail straight through.

    The retry only ever fired on malformed output, so the exact case the
    funding nudge exists for was the one it could never see.
    """
    proposal = _proposal()
    calls: list[list[dict]] = []

    def _llm(_db, messages):
        calls.append(list(messages))
        if len(calls) == 1:
            return json.dumps({"decisions": [
                {"ticker": "ASML.AS", "thesis": "cheap", "action": "buy",
                 "target_weight": 0.45, "confidence": 0.8},
            ]})
        return json.dumps({"decisions": [
            {"ticker": "IWDA.AS", "thesis": "fund the buy", "action": "sell",
             "target_weight": 0.55, "confidence": 0.7},
            {"ticker": "ASML.AS", "thesis": "cheap", "action": "buy",
             "target_weight": 0.45, "confidence": 0.8},
        ]})

    decisions, _ = decide_trades(
        None, proposal, _gate(proposal), llm_call=_llm,
        cash_eur=CASH, total_value_eur=TOTAL_VALUE, min_ticket_eur=MIN_TICKET,
    )

    assert len(calls) == 2, "expected exactly one funding retry"
    assert "no sell" in calls[1][-1]["content"]
    assert any(d.action == "sell" for d in decisions)


def test_the_funding_retry_never_fires_twice() -> None:
    proposal = _proposal()
    calls: list[int] = []

    def _llm(_db, _messages):
        calls.append(1)
        return json.dumps({"decisions": [
            {"ticker": "ASML.AS", "thesis": "cheap", "action": "buy",
             "target_weight": 0.45, "confidence": 0.8},
        ]})

    decisions, _ = decide_trades(
        None, proposal, _gate(proposal), llm_call=_llm,
        cash_eur=CASH, total_value_eur=TOTAL_VALUE, min_ticket_eur=MIN_TICKET,
    )

    assert len(calls) == 2
    # The unfunded answer is still returned: the executor raises cash itself,
    # and trading nothing is worse.
    assert [d.action for d in decisions] == ["buy"]


def test_an_unusable_funding_retry_falls_back_to_the_first_answer() -> None:
    proposal = _proposal()
    calls: list[int] = []

    def _llm(_db, _messages):
        calls.append(1)
        if len(calls) == 1:
            return json.dumps({"decisions": [
                {"ticker": "ASML.AS", "thesis": "cheap", "action": "buy",
                 "target_weight": 0.45, "confidence": 0.8},
            ]})
        return "garbage, not json"

    decisions, _ = decide_trades(
        None, proposal, _gate(proposal), llm_call=_llm,
        cash_eur=CASH, total_value_eur=TOTAL_VALUE, min_ticket_eur=MIN_TICKET,
    )

    assert [(d.ticker, d.action) for d in decisions] == [("ASML.AS", "buy")]


def test_a_funded_answer_is_returned_without_a_retry() -> None:
    proposal = _proposal()
    calls: list[int] = []

    def _llm(_db, _messages):
        calls.append(1)
        return json.dumps({"decisions": [
            {"ticker": "IWDA.AS", "thesis": "trim", "action": "sell",
             "target_weight": 0.55, "confidence": 0.7},
            {"ticker": "ASML.AS", "thesis": "add", "action": "buy",
             "target_weight": 0.20, "confidence": 0.6},
        ]})

    decide_trades(
        None, proposal, _gate(proposal), llm_call=_llm,
        cash_eur=CASH, total_value_eur=TOTAL_VALUE, min_ticket_eur=MIN_TICKET,
    )
    assert len(calls) == 1


def test_a_hold_only_answer_is_not_treated_as_unfunded() -> None:
    """Declining to trade is a legitimate answer, not a funding failure."""
    proposal = _proposal()
    calls: list[int] = []

    def _llm(_db, _messages):
        calls.append(1)
        return json.dumps({"decisions": [
            {"ticker": "IWDA.AS", "thesis": "", "action": "hold",
             "target_weight": round(37_000.0 / TOTAL_VALUE, 4), "confidence": 0.5},
        ]})

    decide_trades(
        None, proposal, _gate(proposal), llm_call=_llm,
        cash_eur=CASH, total_value_eur=TOTAL_VALUE, min_ticket_eur=MIN_TICKET,
    )
    assert len(calls) == 1


def test_decide_trades_pins_the_ticker_enum_to_the_universe() -> None:
    proposal = _proposal()
    gate = _gate(proposal, passing=["ASML.AS", "SAP.DE"])
    universe = decision_universe(proposal, gate)

    # Held names survive even when the gate dropped them.
    assert set(universe) == {"ASML.AS", "SAP.DE", "IWDA.AS", "ALV.DE"}
