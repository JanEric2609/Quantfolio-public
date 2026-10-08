"""LLM decision wrapper for the advisor loop (C3).

The LLM tilts within the risk-gate-passing set, and may additionally reduce any
holding the gate rejected. The gate runs only once, before this step, so
``_validate_decisions`` is the last line of defence: it enforces the allowed
universe and keeps gate-blocked holdings sell-only.

The prompt shows *signed* distances to target. An absolute value here is what
produced one trade in twenty days — a required trim arrived looking exactly
like a buy, in a block named ``candidates``, beside a positive forward return.

The schema enforces a real buy/sell/hold choice per ticker (see memory:
default-hold is the failure mode to design against), plus a thesis and a 0-1
confidence, with ``thesis`` declared ahead of ``action`` so the grammar makes
the model justify before it commits.

Reflection lessons (PR2 A3) are injected via ``lessons``; strategy prompt
framings (champion/challenger variants) via ``system_suffix``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.decision.advisor.decision import TradeProposal
from app.decision.advisor.risk_gate import RiskGateResult
from app.foundation.llm.structured import (
    STRUCTURED_JSON_SAMPLING,
    build_retry_messages,
    completion_budget as _shared_completion_budget,
    parse_structured_completion,
    salvage_truncated_array,
    unusable_reason as _shared_unusable_reason,
)

logger = logging.getLogger(__name__)

VALID_ACTIONS = ("buy", "sell", "hold")
_MAX_JSON_RETRIES = 2
# Enough of each completion to see what the model actually answered, bounded so
# a runaway response cannot bloat the audit row.
_RAW_RESPONSE_LOG_CHARS = 4000

# A grammar-constrained `{"type": "string"}` has no length bound, so the model
# may keep extending a thesis until it hits the completion budget — at which
# point the JSON is a fragment and the cycle records "not valid JSON". Three
# sentences fit comfortably here. llama.cpp compiles minLength/maxLength into
# GBNF repetitions and rejects any single repetition above 2000, so the cap has
# to stay well under that (ggml-org/llama.cpp#25746).
MAX_THESIS_CHARS = 320

# Completion budget, sized from the answer instead of fixed. Each decision is
# roughly the thesis plus five short fields; the floor covers a one-line reply
# and the ceiling keeps prompt + completion inside the server's context window
# even after a retry (llama-server runs with --no-context-shift).
_TOKENS_PER_DECISION = 140
_MIN_COMPLETION_TOKENS = 512
_MAX_COMPLETION_TOKENS = 4096

# How much of a rejected completion is quoted back on a retry. The loop used to
# append the *whole* answer, so one runaway completion pushed the next attempt's
# prompt past the context window and guaranteed the retry failed too.
_RETRY_EXCERPT_CHARS = 800

_SYSTEM_PROMPT = (
    "You are the trading agent for an autonomous paper portfolio. You receive "
    "quant-screened candidates with Monte-Carlo forward-return distributions, "
    "optimizer-suggested target weights, a hard risk envelope, the current "
    "book, and your cash and cost constraints. Choose trades ONLY among the "
    "allowed tickers, within the envelope.\n"
    "Respond with exactly one JSON object, no markdown, matching:\n"
    '{"decisions": [{"ticker": "SYM", "thesis": "1-3 sentences", '
    '"action": "buy"|"sell"|"hold", "target_weight": 0.08, '
    '"confidence": 0.7}]}\n'
    "State the thesis for each name BEFORE its action — reason first, then "
    "commit.\n"
    "Direction: every candidate carries `delta_to_target_eur`, the signed "
    "distance from what you hold now to the optimiser's target. A NEGATIVE "
    "value means the position must be REDUCED — answer `sell` with "
    "`target_weight` set to `suggested_weight`. A POSITIVE value means "
    "increase it — answer `buy`. A candidate marked `actionable: false` is "
    "already close enough to its target that no order could clear the minimum "
    "ticket — answer `hold` with `target_weight` equal to its "
    "`current_weight`. `hold` means the position stays at its CURRENT weight, "
    "so a `hold` whose target_weight differs from `current_weight` is a "
    "contradiction and is rejected.\n"
    "A candidate marked `sell_only` is held but rejected by the risk gate: you "
    "may reduce it or hold it, never increase it.\n"
    "Every held position competes on its own forecast, exactly like a "
    "brand-new candidate — `mc_forward_21d` and `prob_positive` are evidence "
    "for keeping or adding to it, not only a funding source to be drained. "
    "`sell_candidates` lists what the optimizer's diversification target "
    "implies is overweight; it is not an instruction to sell. Propose a sell "
    "there only when the position's own forward forecast, not diversification "
    "math alone, makes reducing it the better trade than holding or adding to "
    "it. A held position's sell is capped at `max_sellable_eur` this cycle "
    "regardless of how much you request — a full exit takes more than one "
    "cycle by design, so size sell orders you intend to repeat rather than "
    "expecting one to finish the job. `max_sellable_eur` also folds in a "
    "trailing-window cumulative limit (repeated small sells of the same "
    "position across recent cycles count against it too, not just this "
    "cycle's own sell) and, for a diversified/index holding, a floor below "
    "which it cannot be reduced at all — repeatedly trimming a name a little "
    "each cycle does not evade either limit.\n"
    "Rules: every listed ticker needs an explicit action — do not default to "
    "hold; justify each non-hold with a thesis grounded in the provided "
    "numbers; confidence is your probability the trade beats cash over 21 "
    "trading days.\n"
    "Funding: the portfolio is close to fully invested, so cash is scarce. "
    "Buys are financed from `cash_eur` plus the proceeds of the sells you "
    "choose in this same response — sells always settle first. To open or "
    "increase a position beyond available cash you must free the money by "
    "cutting target_weight on something you already hold. An unfunded buy is "
    "rejected and wasted.\n"
    "Costs: every order pays a commission (see `trading_costs`). Small trades "
    "and round-trips are disproportionately expensive, so only trade when the "
    "expected edge clears the fee."
)


@dataclass
class TradeDecision:
    """One validated LLM trade decision."""

    ticker: str
    action: str
    target_weight: float
    thesis: str
    confidence: float


def build_decision_schema(allowed: Sequence[str] | None = None) -> dict[str, Any]:
    """Build the decision schema, pinning ``ticker`` to ``allowed`` when given.

    Property order is generation order: llama.cpp's ``json_schema_to_grammar``
    emits object keys in declared order, so ``thesis`` sitting ahead of
    ``action`` forces the model to write its justification before committing to
    a verb. With reasoning disabled this is the only chain-of-thought available,
    and it costs nothing.

    The ``ticker`` enum makes an out-of-universe name unrepresentable rather
    than merely invalid — that error class accounted for most of the failed
    cycles, and a grammar refusal is cheaper than a retry.

    Both length bounds exist to make the *completion* finite. The universe is
    the natural ``maxItems`` (one decision per nameable ticker), and
    ``maxLength`` on the thesis stops the one unbounded field in the grammar
    from running to the token limit and truncating the object it sits in.
    """
    ticker: dict[str, Any] = {"type": "string"}
    if allowed:
        ticker["enum"] = sorted({str(s).upper() for s in allowed})
    decisions: dict[str, Any] = {
        "type": "array",
        "minItems": 1,
    }
    if allowed:
        decisions["maxItems"] = max(1, len({str(s).upper() for s in allowed}))
    return {
        "type": "object",
        "properties": {
            "decisions": {
                **decisions,
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": ticker,
                        # minLength closes the gap that produced "non-hold without
                        # thesis" errors: on longer decision batches the model would
                        # satisfy the schema's `required` with an empty string for
                        # later items instead of an actual justification — the
                        # grammar had no reason to refuse that. A real thesis is
                        # several words; 8 chars is far below any genuine one but
                        # excludes "" and whitespace.
                        "thesis": {"type": "string", "minLength": 8, "maxLength": MAX_THESIS_CHARS},
                        "action": {"type": "string", "enum": list(VALID_ACTIONS)},
                        "target_weight": {"type": "number", "minimum": 0, "maximum": 1},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": [
                        "ticker", "thesis", "action", "target_weight", "confidence",
                    ],
                },
            }
        },
        "required": ["decisions"],
    }


# Grammar-constrains the completion so a malformed decision payload is not
# representable. The nudge-and-retry loop below stays as a safety net for
# servers without schema support (the call degrades to unconstrained there).
DECISION_JSON_SCHEMA: dict[str, Any] = build_decision_schema()

# Qwen's published non-thinking preset, in full. Near-greedy decoding is
# mode-seeking, and on this prompt the mode was buy/hold.
#
# ``presence_penalty`` is part of that same published preset, and Qwen
# recommends 1.5 (up to 2.0) specifically for *quantized* checkpoints to
# suppress repetition — this server runs Qwen3.5-9B at Q4_K_XL. It is restated
# here because ``_local_llm_sync`` zeroes the server's ``--presence-penalty
# 1.5`` for every caller, and taking the rest of the preset while dropping
# this field left the decision call with no repetition defence at all: the
# server also runs ``repeat_penalty`` 1.0, ``frequency_penalty`` 0 and
# ``dry_multiplier`` 0.
#
# A grammar bounds *structure*, not *termination* — inside a `"thesis": "…"`
# every repeated token is still legal JSON, which is where the runaway
# completions that read as "LLM response was not valid JSON" came from.
# ``MAX_THESIS_CHARS`` is the hard stop for that; this is the sampler-level
# discouragement that keeps the model from heading there in the first place.
# Alias kept for existing importers (tests, diagnostics.py) — the preset
# itself now lives in app.foundation.llm.structured, shared with reflection.py
# and dossier_writer.py.
DECISION_SAMPLING: dict[str, Any] = STRUCTURED_JSON_SAMPLING


def completion_budget(universe_size: int) -> int:
    """Completion tokens to allow for a decision over *universe_size* tickers."""
    return _shared_completion_budget(
        universe_size,
        tokens_per_item=_TOKENS_PER_DECISION,
        min_tokens=_MIN_COMPLETION_TOKENS,
        max_tokens=_MAX_COMPLETION_TOKENS,
    )


def _default_llm_call(
    db: Session,
    messages: list[dict[str, str]],
    json_schema: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    telemetry: dict[str, Any] | None = None,
) -> str:
    from app.decision.llm_portfolio.review import _local_llm_sync

    return _local_llm_sync(
        db,
        messages,
        json_schema=json_schema or DECISION_JSON_SCHEMA,
        sampling=DECISION_SAMPLING,
        max_tokens=max_tokens,
        telemetry=telemetry,
    )


def salvage_truncated_decisions(raw: str) -> dict[str, Any] | None:
    """Recover the complete decisions from a completion that was cut off.

    Thin wrapper over the shared, key-parameterised implementation — kept as
    its own name since it's part of this module's tested public surface (see
    ``tests/test_advisor_llm_truncation.py``).
    """
    return salvage_truncated_array(raw, "decisions")


def parse_completion(raw: str) -> tuple[dict[str, Any] | None, str]:
    """Parse a decision completion, reporting *how* it had to be read.

    Returns ``(parsed, how)`` with ``how`` in ``strict`` / ``embedded`` /
    ``salvaged`` / ``unparseable``. The caller needs the distinction: a salvaged
    answer is usable but means the completion budget is too small, and reporting
    it as a clean parse hides the actual defect.
    """
    return parse_structured_completion(raw, "decisions")


def _try_parse_json(raw: str) -> dict[str, Any] | None:
    return parse_completion(raw)[0]


def decision_universe(
    proposal: TradeProposal,
    gate_result: RiskGateResult,
) -> list[str]:
    """Every ticker the model may name: gate-passing candidates plus all holdings.

    A holding the gate drops for breaching a ceiling used to vanish from both
    ``allowed_tickers`` and ``candidates`` while the system prompt insisted on
    choosing only among allowed tickers — so the position most in need of
    trimming was the one the model was forbidden to mention.
    """
    return sorted(set(gate_result.passing_symbols) | set(proposal.current_book))


def sell_only_symbols(
    proposal: TradeProposal,
    gate_result: RiskGateResult,
) -> set[str]:
    """Held names the risk gate rejected — reducible, never increasable.

    Widening the prompt to name these is only safe because the validator
    enforces the restriction: ``check_risk_gate`` runs once, before the LLM,
    so nothing downstream would catch a buy on a name the gate had dropped.
    """
    passing = set(gate_result.passing_symbols)
    return {
        sym for sym, value in proposal.current_book.items()
        if float(value) > 0 and sym not in passing
    }


def _build_user_prompt(
    proposal: TradeProposal,
    gate_result: RiskGateResult,
    lessons: list[str],
    *,
    cash_eur: float | None = None,
    total_value_eur: float | None = None,
    fee_schedule: dict[str, Any] | None = None,
    min_ticket_eur: float | None = None,
    max_turnover_eur: float | None = None,
    rl_signal: dict[str, float] | None = None,
    max_sell_pct_of_position: float | None = None,
    max_sellable_eur_by_ticker: dict[str, float] | None = None,
    instrument_names: dict[str, str] | None = None,
) -> str:
    from app.decision.advisor.costs import describe_fee_schedule, trade_fee

    total_value = float(total_value_eur or 0.0)
    universe = decision_universe(proposal, gate_result)
    sell_only = sell_only_symbols(proposal, gate_result)
    # Below this the executor skips the order, so its sign is not a trade
    # signal — reporting one anyway produced buy/sell intent that could never
    # reach the book but still landed in the prediction ledger.
    floor = float(min_ticket_eur or 0.0)

    candidates_block = []
    sell_candidates: list[dict[str, Any]] = []
    for sym in universe:
        mc = proposal.mc_summaries.get(sym)
        held_eur = float(proposal.current_book.get(sym, 0.0))
        # A gate-blocked holding has no valid target above zero: the only
        # direction the envelope permits is down.
        suggested = 0.0 if sym in sell_only else proposal.suggested_weights.get(sym, 0.0)
        current_weight = held_eur / total_value if total_value > 0 else 0.0
        # Signed: negative means reduce. Dropping this sign is what made every
        # cue in the block a buy cue.
        delta = suggested * total_value - held_eur
        actionable = abs(delta) >= floor
        entry: dict[str, Any] = {
            "ticker": sym,
            "held": held_eur > 0,
            "held_eur": round(held_eur, 2),
            "current_weight": round(current_weight, 4),
            "suggested_weight": round(suggested, 4),
            "delta_to_target_eur": round(delta, 2),
            "direction": ("sell" if delta < 0 else "buy") if actionable else "hold",
            "actionable": actionable,
            "mc_forward_21d": (
                {
                    "p5": round(mc.p5, 4),
                    "p50": round(mc.p50, 4),
                    "p95": round(mc.p95, 4),
                    "prob_positive": round(mc.prob_positive, 4),
                }
                if mc is not None
                else None
            ),
            "instrument_type": mc.instrument_type if mc is not None else "equity",
            "estimated_fee_eur": trade_fee(
                abs(delta), fee_schedule, (instrument_names or {}).get(sym), side="buy" if delta > 0 else "sell"
            ),
        }
        if rl_signal is not None and sym in rl_signal:
            entry["rl_suggested_weight"] = round(float(rl_signal[sym]), 4)
        if held_eur > 0:
            if max_sellable_eur_by_ticker is not None and sym in max_sellable_eur_by_ticker:
                entry["max_sellable_eur"] = round(float(max_sellable_eur_by_ticker[sym]), 2)
            elif max_sell_pct_of_position is not None:
                entry["max_sellable_eur"] = round(held_eur * float(max_sell_pct_of_position), 2)
        if not actionable:
            entry["not_actionable_reason"] = (
                f"€{abs(delta):,.0f} from target is below the €{floor:,.0f} minimum "
                "ticket — no order can be placed"
            )
        if sym in sell_only:
            entry["sell_only"] = True
            entry["sell_only_reason"] = (
                "held but rejected by the risk gate — may be reduced or held, "
                "never increased"
            )
        candidates_block.append(entry)

        if delta < 0 and held_eur > 0 and actionable:
            excess = -delta
            sell_candidate: dict[str, Any] = {
                "ticker": sym,
                "held_eur": round(held_eur, 2),
                "current_weight": round(current_weight, 4),
                "suggested_weight": round(suggested, 4),
                "excess_eur": round(excess, 2),
                "frees_cash_eur": round(
                    excess - trade_fee(excess, fee_schedule, (instrument_names or {}).get(sym), side="sell"), 2
                ),
                "sell_only": sym in sell_only,
            }
            if max_sellable_eur_by_ticker is not None and sym in max_sellable_eur_by_ticker:
                sell_candidate["max_sellable_eur"] = round(
                    float(max_sellable_eur_by_ticker[sym]), 2
                )
            elif max_sell_pct_of_position is not None:
                sell_candidate["max_sellable_eur"] = round(
                    held_eur * float(max_sell_pct_of_position), 2
                )
            sell_candidates.append(sell_candidate)
    sell_candidates.sort(key=lambda s: -float(s["excess_eur"]))

    payload: dict[str, Any] = {
        "allowed_tickers": universe,
        "candidates": candidates_block,
        "sell_candidates": sell_candidates,
        "sell_candidates_note": (
            "Positions above their optimiser target. Selling them is how a buy "
            "gets funded: set action=sell and target_weight=suggested_weight. "
            "An empty list means nothing is currently overweight."
        ),
        "current_book_eur": {k: round(v, 2) for k, v in proposal.current_book.items()},
        "risk_envelope": gate_result.envelope,
        "risk_ceilings": gate_result.ceilings,
        "blocked_by_risk_gate": gate_result.blocked,
        "lessons": lessons,  # reflection memory — the LLM reads its own track record
    }
    if rl_signal:
        payload["rl_signal_note"] = (
            "Some candidates carry rl_suggested_weight: a portfolio-allocation "
            "weight from a separately trained and validated reinforcement-"
            "learning policy's rollout. It is one more informative input, not "
            "a directive — weigh it alongside suggested_weight and the MC "
            "forward-return summary rather than following it automatically."
        )
    if cash_eur is not None:
        payload["cash_eur"] = round(float(cash_eur), 2)
    if total_value_eur is not None:
        payload["portfolio_value_eur"] = round(total_value, 2)
    payload["trading_costs"] = {
        "commission_schedule": describe_fee_schedule(fee_schedule),
        "charged_on": "every order, both buys and sells",
    }
    constraints: dict[str, Any] = {}
    if min_ticket_eur is not None:
        constraints["min_order_eur"] = round(float(min_ticket_eur), 2)
        constraints["min_order_note"] = "smaller orders are skipped, not executed"
    if max_turnover_eur is not None:
        constraints["max_total_traded_eur_this_cycle"] = round(float(max_turnover_eur), 2)
        constraints["turnover_note"] = (
            "gross notional across all orders; when it binds, the "
            "lowest-confidence orders are dropped first"
        )
    if constraints:
        payload["constraints"] = constraints
    return json.dumps(payload, default=str)


def _validate_decisions(
    parsed: dict[str, Any],
    allowed: set[str],
    *,
    sell_only: set[str] | None = None,
    current_weights: dict[str, float] | None = None,
    hold_tolerance: float = 0.0,
) -> tuple[list[TradeDecision], list[str]]:
    """Validate the parsed LLM output against the schema and the allowed set.

    Args:
        parsed: Decoded LLM response.
        allowed: Tickers the model may act on.
        sell_only: Held names the risk gate rejected. Any action that would
            raise the position is an error — the gate does not run again after
            the model answers.
        current_weights: Held weight per ticker, used to detect a ``hold``
            that is really a trim in disguise.
        hold_tolerance: Weight distance below which a ``hold`` whose target
            differs from the current weight is treated as rounding rather than
            a contradiction. Set from the minimum ticket.
    """
    sell_only = sell_only or set()
    current_weights = current_weights or {}
    errors: list[str] = []
    decisions: list[TradeDecision] = []
    raw_decisions = parsed.get("decisions")
    if not isinstance(raw_decisions, list) or not raw_decisions:
        return [], ["'decisions' missing or empty"]

    for i, item in enumerate(raw_decisions):
        if not isinstance(item, dict):
            errors.append(f"decision[{i}] not an object")
            continue
        ticker = str(item.get("ticker", "")).upper().strip()
        action = str(item.get("action", "")).lower().strip()
        if not ticker:
            errors.append(f"decision[{i}] missing ticker")
            continue
        if action not in VALID_ACTIONS:
            errors.append(f"decision[{i}] ({ticker}) invalid action {action!r}")
            continue
        if action != "hold" and ticker not in allowed:
            # Out-of-envelope choice — rejected here; the gate re-check is a
            # second line of defence for sizing.
            errors.append(f"decision[{i}] ({ticker}) outside risk-gate-passing set")
            continue
        try:
            weight = float(item.get("target_weight", 0.0))
        except (TypeError, ValueError):
            errors.append(f"decision[{i}] ({ticker}) non-numeric target_weight")
            continue
        weight = max(0.0, min(1.0, weight))
        thesis = str(item.get("thesis", "")).strip()
        if action != "hold" and not thesis:
            errors.append(f"decision[{i}] ({ticker}) non-hold without thesis")
            continue

        held_weight = current_weights.get(ticker)
        if ticker in sell_only:
            # The gate rejected this name; only shrinking it is admissible.
            if action == "buy":
                errors.append(
                    f"decision[{i}] ({ticker}) buy on a risk-gate-blocked holding — "
                    "it may only be reduced"
                )
                continue
            if action == "sell" and held_weight is not None and weight >= held_weight:
                errors.append(
                    f"decision[{i}] ({ticker}) sell target {weight:.4f} does not reduce "
                    f"a risk-gate-blocked holding at {held_weight:.4f}"
                )
                continue
        if (
            action == "hold"
            and held_weight is not None
            and abs(weight - held_weight) > hold_tolerance
        ):
            # ``hold`` must mean the position stays put. Trading on it would
            # execute a word the model used to mean "no action"; letting it
            # through silently is what turned a needed trim into a no-op.
            errors.append(
                f"decision[{i}] ({ticker}) hold with target_weight {weight:.4f} against "
                f"a held weight of {held_weight:.4f} — hold must keep the current "
                "weight; use sell to reduce or buy to add"
            )
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        decisions.append(
            TradeDecision(
                ticker=ticker,
                action=action,
                target_weight=weight,
                thesis=thesis,
                confidence=confidence,
            )
        )
    return decisions, errors


def _contains_sell(parsed: dict[str, Any]) -> bool:
    """Did the response propose any sell at all, valid or not?"""
    raw = parsed.get("decisions")
    if not isinstance(raw, list):
        return False
    return any(
        isinstance(item, dict) and str(item.get("action", "")).lower().strip() == "sell"
        for item in raw
    )


def _funding_gap_note(
    *,
    cash_eur: float | None,
    min_ticket_eur: float | None,
    proposed_a_sell: bool,
) -> str | None:
    """Spell out the funding arithmetic when a retry follows a sell-free answer.

    A retry that only repeats "your JSON was malformed" leaves the model with
    no reason to change the substance of its answer. When there is not enough
    cash to place the smallest legal order and nothing was offered for sale,
    the missing piece is a sell, so the nudge says so.
    """
    if proposed_a_sell or cash_eur is None or min_ticket_eur is None:
        return None
    if float(cash_eur) >= float(min_ticket_eur):
        return None
    return (
        f"Note: cash is €{float(cash_eur):,.2f}, below the €{float(min_ticket_eur):,.2f} "
        "minimum order, and your response contained no sell. No buy can be funded "
        "until you free cash. Send the same response again with your buys "
        "UNCHANGED and add at least one name from `sell_candidates` as "
        "action=sell with target_weight=suggested_weight, so the sale settles "
        "first and pays for them. Selling without re-stating the buys leaves the "
        "portfolio sitting in cash, which is worse than not trading. If you "
        "genuinely intend not to trade, answer hold for everything instead."
    )


def _unusable_reason(raw: str, telemetry: dict[str, Any]) -> str:
    """Name the actual failure behind an unparseable completion.

    Thin wrapper over the shared implementation — kept as its own name since
    it's part of this module's tested public surface (see
    ``tests/test_advisor_llm_truncation.py``).
    """
    return _shared_unusable_reason(raw, telemetry)


def _record_attempt(
    sink: list[dict[str, Any]] | None,
    attempt: int,
    telemetry: dict[str, Any],
    *,
    parsed_as: str,
    errors: list[str],
    decisions: int = 0,
) -> None:
    if sink is None:
        return
    sink.append({
        "attempt": attempt + 1,
        "parsed_as": parsed_as,
        "decisions": decisions,
        "errors": list(errors),
        **telemetry,
    })


def decide_trades(
    db: Session,
    proposal: TradeProposal,
    gate_result: RiskGateResult,
    *,
    lessons: list[str] | None = None,
    llm_call: Callable[[Session, list[dict[str, str]]], str] | None = None,
    system_suffix: str | None = None,
    cash_eur: float | None = None,
    total_value_eur: float | None = None,
    fee_schedule: dict[str, Any] | None = None,
    min_ticket_eur: float | None = None,
    max_turnover_eur: float | None = None,
    raw_responses: list[str] | None = None,
    attempts: list[dict[str, Any]] | None = None,
    rl_signal: dict[str, float] | None = None,
    max_sell_pct_of_position: float | None = None,
    max_sellable_eur_by_ticker: dict[str, float] | None = None,
    instrument_names: dict[str, str] | None = None,
) -> tuple[list[TradeDecision], list[str]]:
    """Ask the LLM for structured trade decisions within the risk envelope.

    Args:
        db: Active session (used to resolve the LLM endpoint).
        proposal: Deterministic proposal (C1).
        gate_result: Risk-gate result (C2) — the LLM only sees passing names.
        lessons: Reflection-memory lessons injected into the prompt (PR2 A3).
        llm_call: Injectable LLM callable for tests (``(db, messages) -> str``).
        system_suffix: Optional strategy prompt framing appended to the system
            prompt (champion/challenger variants, PR2 C2).
        cash_eur: Cash available to fund buys. Omitting it is what let the
            model propose orders it could never afford.
        total_value_eur: Portfolio NAV, the base for target weights.
        fee_schedule: Commission schedule used to price each candidate trade.
        min_ticket_eur: Orders below this are skipped by the executor.
        max_turnover_eur: Gross notional ceiling for this cycle.
        raw_responses: Optional sink; each attempt's raw completion is appended
            so a cycle that decided nothing can be diagnosed from its own audit
            record instead of being reconstructed after the fact.
        attempts: Optional sink; one dict per attempt carrying the endpoint's
            own account of the call (``finish_reason``, token usage, whether the
            schema was dropped) plus how the body parsed and what failed. This
            is what separates "the model wrote prose" from "the completion was
            cut off at the token limit" — the two failures read identically in
            the error text but have opposite fixes.
        rl_signal: Optional per-ticker suggested weights from a validated RL
            policy's rollout (Track D2). Advisory only — surfaced in the
            prompt alongside the MC/skfolio-derived ``suggested_weight``,
            never used to validate or override the model's decision.
        max_sell_pct_of_position: Share of a held position's value that may be
            sold in one cycle (see ``cycle.DEFAULT_MAX_SELL_PCT_OF_POSITION``).
            Surfaced to the model as each held candidate's ``max_sellable_eur``
            so the cap is legible rather than silently clamped downstream;
            the executor enforces it regardless of what the model requests.
            Ignored per-ticker where ``max_sellable_eur_by_ticker`` supplies a
            value instead.
        max_sellable_eur_by_ticker: Optional per-ticker override of
            ``max_sellable_eur``, folding in the trailing-window cumulative
            sell cap and the core-holding floor (see
            ``cycle.DEFAULT_MAX_CUMULATIVE_SELL_PCT`` /
            ``cycle.DEFAULT_CORE_HOLDING_FLOOR_PCT``) alongside the per-cycle
            cap, computed once per cycle so the number shown to the model
            always matches what the executor will actually allow.

    Returns:
        (validated decisions, validation errors). An unusable response yields
        ``([], errors)`` — the cycle logs and skips trading rather than
        fabricating decisions.
    """
    if not gate_result.passing_symbols:
        return [], ["no risk-gate-passing candidates"]

    universe = decision_universe(proposal, gate_result)
    sell_only = sell_only_symbols(proposal, gate_result)
    total_value = float(total_value_eur or 0.0)
    current_weights = {
        sym: (float(value) / total_value if total_value > 0 else 0.0)
        for sym, value in proposal.current_book.items()
    }
    # A hold that misses the held weight by less than one minimum ticket is
    # rounding, not a contradiction.
    hold_tolerance = (
        float(min_ticket_eur) / total_value
        if min_ticket_eur and total_value > 0
        else 0.0005
    )

    # The ticker enum and both length bounds are per-call, so the default caller
    # carries this cycle's universe rather than the module-level template. The
    # telemetry dict is rebound per attempt so each record describes its own
    # call; an injected ``llm_call`` simply leaves it empty.
    schema = build_decision_schema(universe)
    budget = completion_budget(len(universe))
    telemetry: dict[str, Any] = {}

    def _instrumented_call(session: Session, msgs: list[dict[str, str]]) -> str:
        telemetry.clear()
        return _default_llm_call(
            session, msgs, json_schema=schema, max_tokens=budget, telemetry=telemetry
        )

    call: Callable[[Session, list[dict[str, str]]], str] = llm_call or _instrumented_call
    system_prompt = _SYSTEM_PROMPT if not system_suffix else f"{_SYSTEM_PROMPT}\n{system_suffix}"
    base_messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": _build_user_prompt(
                proposal,
                gate_result,
                lessons or [],
                cash_eur=cash_eur,
                total_value_eur=total_value_eur,
                fee_schedule=fee_schedule,
                min_ticket_eur=min_ticket_eur,
                max_turnover_eur=max_turnover_eur,
                rl_signal=rl_signal,
                max_sell_pct_of_position=max_sell_pct_of_position,
                max_sellable_eur_by_ticker=max_sellable_eur_by_ticker,
                instrument_names=instrument_names,
            ),
        },
    ]
    messages = list(base_messages)
    allowed = set(universe)

    errors: list[str] = []
    # First usable answer, kept so a funding retry can never do worse than the
    # response it was trying to improve on.
    fallback: tuple[list[TradeDecision], list[str]] | None = None
    funding_nudged = False
    for attempt in range(_MAX_JSON_RETRIES + 1):
        try:
            raw = call(db, messages)
        except Exception as exc:
            logger.exception("advisor llm_decision: LLM call failed")
            _record_attempt(attempts, attempt, telemetry, parsed_as="call_failed", errors=[str(exc)])
            if fallback is not None:
                return fallback
            return [], [f"llm call failed: {exc}"]

        if raw_responses is not None:
            raw_responses.append(raw[:_RAW_RESPONSE_LOG_CHARS])

        parsed, parsed_as = parse_completion(raw)
        proposed_a_sell = parsed is not None and _contains_sell(parsed)
        if parsed is None:
            errors = [_unusable_reason(raw, telemetry)]
        else:
            decisions, errors = _validate_decisions(
                parsed,
                allowed,
                sell_only=sell_only,
                current_weights=current_weights,
                hold_tolerance=hold_tolerance,
            )
            if parsed_as == "salvaged":
                # Usable, but only because the incomplete tail was dropped. Say
                # so in the audit trail: the budget, not the prompt, is at fault.
                limit = telemetry.get("max_tokens") or budget
                recovered = len(parsed.get("decisions") or [])
                errors = [
                    (
                        f"LLM completion was cut off at the {limit}-token limit; recovered "
                        f"{recovered} complete decision(s) and discarded the truncated tail"
                    ),
                    *errors,
                ]
            if decisions:
                if errors:
                    logger.warning(
                        "advisor llm_decision: %d validation errors: %s", len(errors), errors
                    )
                gap = _funding_gap_note(
                    cash_eur=cash_eur,
                    min_ticket_eur=min_ticket_eur,
                    proposed_a_sell=any(d.action == "sell" for d in decisions),
                )
                # A valid answer can still be unaffordable: buys with no sell
                # and no cash to settle them. That response never reached the
                # retry path, because the retry only ever fired on malformed
                # output — so the one case the nudge exists for was the one it
                # could not see.
                unfunded = gap is not None and any(d.action == "buy" for d in decisions)
                _record_attempt(
                    attempts, attempt, telemetry, parsed_as=parsed_as,
                    errors=errors, decisions=len(decisions),
                )
                if not unfunded or gap is None or funding_nudged or attempt >= _MAX_JSON_RETRIES:
                    return decisions, errors
                logger.info(
                    "advisor llm_decision: %d buys with no funding sell against "
                    "€%.2f cash — asking again",
                    sum(1 for d in decisions if d.action == "buy"), float(cash_eur or 0.0),
                )
                fallback = (decisions, errors)
                funding_nudged = True
                messages = build_retry_messages(base_messages, raw, gap, excerpt_chars=_RETRY_EXCERPT_CHARS)
                continue

        _record_attempt(attempts, attempt, telemetry, parsed_as=parsed_as, errors=errors)
        if attempt < _MAX_JSON_RETRIES:
            logger.warning(
                "advisor llm_decision: unusable response (attempt %d/%d): %s",
                attempt + 1, _MAX_JSON_RETRIES + 1, errors,
            )
            nudge = (
                "That response was unusable: "
                f"{'; '.join(errors)}. Reply with exactly one JSON object matching the "
                "schema, no markdown, with an explicit action for every listed ticker. "
                f"Keep every thesis under {MAX_THESIS_CHARS} characters so the object "
                "closes inside the token budget."
            )
            gap = _funding_gap_note(
                cash_eur=cash_eur,
                min_ticket_eur=min_ticket_eur,
                proposed_a_sell=proposed_a_sell,
            )
            if gap:
                nudge = f"{nudge} {gap}"
            # Rebuilt from the base rather than appended to: stacking every
            # rejected completion is what pushed attempt 3's prompt past the
            # server's context window, so the retries could only ever fail.
            messages = build_retry_messages(base_messages, raw, nudge, excerpt_chars=_RETRY_EXCERPT_CHARS)

    if fallback is not None:
        # The funding retry came back unusable; the earlier valid-but-unfunded
        # answer still beats trading nothing, and the executor raises the cash
        # deterministically from there.
        logger.warning("advisor llm_decision: funding retry failed, keeping first answer")
        return fallback

    logger.warning(
        "advisor llm_decision: no usable decisions after %d attempts: %s",
        _MAX_JSON_RETRIES + 1, errors,
    )
    return [], errors
