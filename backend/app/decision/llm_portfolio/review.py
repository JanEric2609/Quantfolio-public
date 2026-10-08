"""Weekly mandate review with bounded tool loop and deterministic gates."""

from __future__ import annotations

import asyncio
import json

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    LlmAdviceCard,
    LlmPortfolioDecision,
    PaperHolding,
    PaperTrade,
)
from app.foundation.llm.structured import (
    STRUCTURED_JSON_SAMPLING,
    build_retry_messages,
    parse_structured_completion,
    unusable_reason,
)
from app.foundation.settings import llm_auth_headers, resolve_llm_base_url, resolve_llm_model

logger = logging.getLogger(__name__)

# Was 5: every pass through the loop below — a tool call, a JSON-format
# retry, or an invalid-action nudge — consumes one, and the model was never
# told how many remained. That silently exhausted the budget on ordinary
# tool exploration and landed on fallback_hold with no decision at all.
# Raised for headroom; the remaining-turns reminder injected into each loop
# message is what actually gets the model to commit before running out.
_MAX_TOOL_CALLS = 8
_MAX_JSON_RETRIES = 2

# Completion budget for a prose caller. Structured-JSON callers should size
# their own budget from the answer they actually need: llama-server is started
# with ``--no-context-shift``, so prompt + completion has to fit inside
# ``--ctx-size`` (32768) or generation is cut mid-token-stream. Under a grammar
# a cut completion is unparseable JSON, which is indistinguishable from a model
# that ignored the schema unless ``finish_reason`` is inspected.
DEFAULT_MAX_TOKENS = 8192

# Only actions the trade executor below can act on. Anything else ("rebalance",
# "reduce", …) would previously persist as status="completed" while silently
# creating no trade.
_ALLOWED_ACTIONS = {"buy", "sell", "hold"}

_TOOL_NAMES = ("get_quote", "get_history_stats", "get_etf_profile", "get_discover_shortlist")

# Same defense as advisor/llm_decision.py's MAX_THESIS_CHARS: a grammar bounds
# structure but not string-field termination, so an unbounded thesis can run
# to the completion budget and leave the JSON unclosed. Kept as its own
# constant (not imported from advisor) — advisor and llm_portfolio are both
# decision-loop packages and import-linter forbids member-to-member imports
# between them.
_MANDATE_MAX_THESIS_CHARS = 320
_MANDATE_MAX_ARRAY_ITEMS = 5
_MANDATE_MAX_ITEM_CHARS = 200

# Grammar-constrains each turn's reply to exactly the two shapes the prompt
# protocol (context.py:_PROMPT_PROTOCOL) describes: a tool call or the final
# decision. Prompt-only JSON (asking nicely, then regex-sanitizing the
# response) was what produced the ~32% of mandate reviews that landed on
# fallback_hold with no trade — the same failure class advisor/llm_decision.py
# closed for the advisor loop's own decision step. ``additionalProperties:
# False`` on each branch is what makes the two shapes mutually exclusive in
# the compiled grammar instead of degenerating into "any object with either
# key". Bounds are deliberately generous but finite: unbounded arrays/strings
# are exactly where a grammar stops constraining anything (every repeated
# token is still legal JSON), so a still-legal-but-runaway completion can
# exhaust the token budget and leave the object unclosed.
_MANDATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "enum": list(_TOOL_NAMES)},
                "args": {"type": "object"},
            },
            "required": ["tool"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "object",
                    "properties": {
                        "thesis": {
                            "type": "string",
                            "minLength": 8,
                            "maxLength": _MANDATE_MAX_THESIS_CHARS,
                        },
                        "action": {"type": "string", "enum": sorted(_ALLOWED_ACTIONS)},
                        "ticker": {"type": "string"},
                        "quantity": {"type": "number", "minimum": 0},
                        "alternatives_considered": {
                            "type": "array",
                            "maxItems": _MANDATE_MAX_ARRAY_ITEMS,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ticker": {"type": "string"},
                                    "why_rejected": {
                                        "type": "string",
                                        "maxLength": _MANDATE_MAX_ITEM_CHARS,
                                    },
                                },
                            },
                        },
                        "key_risks": {
                            "type": "array",
                            "maxItems": _MANDATE_MAX_ARRAY_ITEMS,
                            "items": {"type": "string", "maxLength": _MANDATE_MAX_ITEM_CHARS},
                        },
                        "expectation": {
                            "type": "object",
                            "properties": {
                                "metric": {
                                    "type": "string",
                                    "enum": [
                                        "abs_return_pct",
                                        "benchmark_excess_pct",
                                        "max_drawdown_pct",
                                    ],
                                },
                                "direction": {"type": "string", "enum": ["increase", "decrease"]},
                                "magnitude": {"type": "number"},
                                "horizon_weeks": {"type": "integer", "minimum": 1, "maximum": 12},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": [
                                "metric", "direction", "magnitude", "horizon_weeks", "confidence",
                            ],
                        },
                    },
                    "required": ["thesis", "action", "ticker", "quantity"],
                },
            },
            "required": ["decision"],
            "additionalProperties": False,
        },
    ],
}

# One correction retry (echoing the rejected completion back, per
# build_retry_messages) before a run of unparseable JSON gives up on the
# review entirely — this is a hard stop independent of the tool-call turn
# budget, so a broken completion doesn't quietly eat turns meant for tool
# exploration before landing on the same fallback anyway.
_JSON_PARSE_MAX_CONSECUTIVE_RETRIES = 1

# How much of a review_failed completion is persisted onto the decision row's
# `error` column. Before this, an unparseable completion was discarded the
# moment fallback_hold was chosen, so a later audit of "why do reviews fail"
# had no sample to look at — this is what makes that sample exist going
# forward.
_RAW_RESPONSE_LOG_CHARS = 2000


def _strip_reasoning(text: str) -> str:
    """Remove a leading ``<think>…</think>`` block from a model response.

    Reasoning models emit the block before any answer content. When thinking is
    correctly disabled this is a no-op; when a server build ignores the switch
    it keeps schema-constrained callers working instead of failing to parse.
    """
    if "<think>" not in text:
        return text.strip()
    head, _, tail = text.partition("</think>")
    if not _:
        # Unterminated block: the whole completion was reasoning, nothing usable.
        return ""
    if "<think>" in head:
        return tail.strip()
    return text.strip()


def _local_llm_sync(
    db: Session,
    messages: list[dict[str, str]],
    timeout_s: float = 120.0,
    json_schema: dict[str, Any] | None = None,
    sampling: dict[str, Any] | None = None,
    *,
    max_tokens: int | None = None,
    telemetry: dict[str, Any] | None = None,
) -> str:
    """Call the local LLM via its OpenAI-compatible HTTP endpoint (synchronous).

    Args:
        db: Active session (resolves the endpoint and model).
        messages: Chat messages.
        timeout_s: Read timeout; the connect timeout stays short so an
            unreachable endpoint fails in seconds.
        json_schema: When given, constrains generation to this JSON schema via
            llama.cpp's grammar-based sampling, so the response *cannot* be
            malformed. Prompt-only JSON was what broke the advisor decision
            step. Falls back to an unconstrained call if the server rejects
            the field, so a server that predates schema support still works.

            The constraint is best-effort by nature: llama-server *fails open*
            when a schema converts to a grammar it then cannot parse — it logs
            ``failed to parse grammar`` and returns 200 with unconstrained
            text (ggml-org/llama.cpp#19051), and the same happens when the
            grammar exceeds its repetition threshold (#21228). So callers must
            still validate, and ``telemetry`` is how they tell "the model
            ignored the schema" apart from "the completion was cut off".
        sampling: Per-call overrides for the decoding parameters. Callers that
            want a decision rather than prose pass Qwen's published preset
            here; the prose callers keep the near-greedy default deliberately.
        max_tokens: Completion budget. Defaults to ``DEFAULT_MAX_TOKENS``;
            structured callers should pass a figure sized to their schema so
            prompt + completion cannot overrun the server's context window.
        telemetry: Optional sink filled with what the response says about
            itself — ``finish_reason``, token usage, whether the schema was
            dropped, whether the content arrived on ``reasoning_content``.
            Without it a truncated completion is silently reported by the
            caller as "not valid JSON", which points at the prompt instead of
            at the token budget.
    """
    base_url = resolve_llm_base_url(db)
    model = resolve_llm_model(db)
    budget = int(max_tokens) if max_tokens else DEFAULT_MAX_TOKENS

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": budget,
        # Qwen3 burns the token budget on hidden <think> tokens before
        # emitting any content unless reasoning is explicitly disabled.
        #
        # llama-server does NOT implement the OpenAI `reasoning_effort`
        # field — an unknown key is ignored, which is why the earlier
        # `reasoning: false` / `enable_reasoning: false` attempts silently
        # did nothing. The template kwarg below is the request-level knob
        # llama-server actually honours; the `--reasoning-budget 0` server
        # flag is the belt to this braces.
        "chat_template_kwargs": {"enable_thinking": False},
        # The server is started with ``--presence-penalty 1.5``; this neutralises
        # it for every caller, and a caller re-raises it via ``sampling``.
        #
        # The reasoning this default was originally given — that under a grammar
        # the penalty can only move probability across the enums and the numbers,
        # i.e. the answer itself — is wrong. The penalty applies inside string
        # fields too, and an unbounded string is exactly where a grammar stops
        # constraining anything: every repeated token is still legal JSON. So
        # zeroing it removes the only sampler discouraging a runaway thesis.
        # ``DECISION_SAMPLING`` restores Qwen's published 1.5 for that reason.
        #
        # It stays 0.0 by default because the prose callers are deliberately
        # near-greedy and none of them has shown this failure.
        "presence_penalty": 0.0,
    }
    if sampling:
        payload.update(sampling)
    if json_schema is not None:
        # llama.cpp accepts the schema nested under a json_object response
        # format; this shape is the one its OpenAI-compatible handler has
        # supported longest.
        payload["response_format"] = {"type": "json_object", "schema": json_schema}

    if telemetry is not None:
        telemetry.update(
            {
                "max_tokens": budget,
                "schema_constrained": json_schema is not None,
                "schema_dropped": False,
            }
        )

    timeout = httpx.Timeout(timeout_s, connect=5.0)
    auth = llm_auth_headers(db)
    auth_kwargs: dict[str, Any] = {"headers": auth} if auth else {}
    response = httpx.post(f"{base_url}/chat/completions", json=payload, timeout=timeout, **auth_kwargs)
    if json_schema is not None and response.status_code in range(400, 500):
        logger.warning(
            "LLM rejected schema-constrained request (%s); retrying unconstrained: %s",
            response.status_code,
            response.text[:200],
        )
        if telemetry is not None:
            telemetry["schema_dropped"] = True
            telemetry["schema_reject_status"] = response.status_code
            telemetry["schema_reject_body"] = response.text[:300]
        # Build a fresh body rather than mutating the one already sent — the
        # original must stay intact for logging and for anything holding it.
        unconstrained = {k: v for k, v in payload.items() if k != "response_format"}
        response = httpx.post(f"{base_url}/chat/completions", json=unconstrained, timeout=timeout, **auth_kwargs)

    response.raise_for_status()
    data = response.json()
    choice = data["choices"][0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    # Some builds surface reasoning on a separate field and leave content
    # empty; others inline a <think> block. Strip inline blocks so a caller
    # that only wants JSON is not defeated by a stray preamble.
    stripped = _strip_reasoning(content)
    reasoning = str(message.get("reasoning_content") or "")
    if not stripped and reasoning:
        # Content empty, answer parked on the reasoning field. Reading it is the
        # difference between a usable answer and a cycle recorded as "the model
        # returned nothing", and the caller validates it either way.
        stripped = _strip_reasoning(reasoning)
        if telemetry is not None:
            telemetry["used_reasoning_content"] = True

    if telemetry is not None:
        usage = data.get("usage") or {}
        finish_reason = choice.get("finish_reason")
        telemetry.update(
            {
                "finish_reason": finish_reason,
                # llama-server stops at the budget rather than shifting context
                # (--no-context-shift), so "length" means the JSON is a fragment.
                "truncated": finish_reason == "length",
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "content_chars": len(stripped),
                "raw_content_chars": len(content),
                "reasoning_leak": "<think>" in content or "</think>" in content,
            }
        )
    return stripped


def _run_review_tool(db: Session, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a single review tool call."""
    if tool_name == "get_quote":
        from app.foundation.market import quote

        ticker = args.get("ticker", "")
        return quote(db, ticker)
    if tool_name == "get_history_stats":
        from app.foundation.market import history

        ticker = args.get("ticker", "")
        days = args.get("days", 252)
        hist = history(db, ticker, days=days)
        if not hist:
            return {"error": "No history available"}
        closes = [h["close"] for h in hist if h.get("close") is not None]
        if not closes:
            return {"error": "No close prices in history"}
        import statistics

        return {
            "count": len(closes),
            "mean": round(statistics.mean(closes), 4),
            "stdev": round(statistics.stdev(closes), 4) if len(closes) > 1 else 0.0,
            "latest": closes[-1],
        }
    if tool_name == "get_etf_profile":
        from app.foundation.market import fundamentals

        ticker = args.get("ticker", "")
        return fundamentals(db, ticker)
    if tool_name == "get_discover_shortlist":
        from app.foundation.models.entities import DiscoverCandidate

        rows = (
            db.query(DiscoverCandidate)
            .filter(DiscoverCandidate.status == "shortlisted")
            .order_by(DiscoverCandidate.id.desc())
            .limit(args.get("limit", 10))
            .all()
        )
        return {
            "candidates": [
                {"symbol": r.symbol, "name": r.name, "isin": r.isin, "scores_json": r.scores_json}
                for r in rows
            ]
        }
    return {"error": f"Unknown tool: {tool_name}"}


def _run_council_advisory(db: Session, portfolio: Any, mandate: str) -> dict[str, Any] | None:
    """Run the Multi-Agent Council for advisory context (not a decision).

    Returns the council's ``CouncilResult`` as a dict, or ``None`` on any
    failure — a slow/unreachable LLM endpoint must never block the review,
    it only means the review proceeds without this extra context, exactly as
    it always has. Individual agent failures are already absorbed inside the
    orchestrator (each failing agent degrades to an empty report); this catch
    only needs to guard against total failure (e.g. LLM endpoint down).
    """
    try:
        from app.decision.llm_portfolio.agents import CouncilOrchestrator, create_default_config
        from app.decision.llm_portfolio.council_context import build_council_context
        from app.decision.llm_portfolio.context import _build_system_prompt

        base_url = resolve_llm_base_url(db)
        model = resolve_llm_model(db)
        strategy_prompt = _build_system_prompt(mandate)
        council_context = build_council_context(db, portfolio, strategy_prompt=strategy_prompt)

        config = create_default_config(portfolio.id, strategy_prompt, base_url, model)
        orchestrator = CouncilOrchestrator(
            config.model_copy(),  # analyst
            config.model_copy(),  # risk
            config.model_copy(),  # macro
            config.model_copy(),  # portfolio manager
        )
        result = asyncio.run(orchestrator.run_council(**council_context))
        return result.model_dump(mode="json")
    except Exception as exc:
        logger.warning("Council advisory run failed for mandate %s: %s", mandate, exc)
        return None


def _resolve_isin(db: Session, ticker: str) -> str | None:
    """Resolve an ISIN for a ticker that is not currently held.

    Without this, new-position buys reach the TRADEABILITY gate with
    isin=None and are rejected for any non-EU-suffixed symbol.
    """
    from app.foundation.models.entities import Asset, DiscoverCandidate

    asset = (
        db.query(Asset)
        .filter(Asset.symbol == ticker, Asset.isin.isnot(None))
        .order_by(Asset.updated_at.desc())
        .first()
    )
    if asset and asset.isin:
        return asset.isin

    candidate = (
        db.query(DiscoverCandidate)
        .filter(DiscoverCandidate.symbol == ticker, DiscoverCandidate.isin.isnot(None))
        .order_by(DiscoverCandidate.id.desc())
        .first()
    )
    return candidate.isin if candidate else None


def run_mandate_review(db: Session, user_id: str, mandate: str) -> dict[str, Any]:
    """Run a full mandate review cycle.

    Args:
        db: Database session.
        user_id: User UUID.
        mandate: Mandate identifier ("A" or "B").

    Returns:
        Summary dict with review status, decisions, and trades.
    """
    from app.decision.llm_portfolio.context import assemble_review_context
    from app.decision.llm_portfolio.gates import check_buy_gates
    from app.decision.llm_portfolio.mirror import ensure_mandate_portfolio
    from app.decision.llm_portfolio.divergence import compute_divergence

    # Ensure portfolio exists
    portfolio = ensure_mandate_portfolio(db, user_id, mandate)
    portfolio_id = portfolio.id

    # Build context
    context = assemble_review_context(db, portfolio_id, mandate)
    messages = list(context["messages"])
    token_estimate = context["token_estimate"]
    # Authoritative deterministic facts — persisted on the decision so the UI
    # renders computed truth, not whatever the LLM echoed back.
    assessment_facts = context.get("blocks", {}).get("assessment")

    # Multi-Agent Council: advisory-only context, folded into the prompt.
    # The tool loop below remains the sole author of the persisted decision —
    # this never replaces or blocks it, only informs it. A council failure
    # (or a thin/empty degraded report) just means no extra block below.
    council_result = _run_council_advisory(db, portfolio, mandate)
    if council_result is not None:
        council_decision = council_result.get("decision") or {}
        messages.append(
            {
                "role": "user",
                "content": (
                    "## Quant Committee Input (advisory context, not a command — "
                    "your own decision still governs)\n"
                    + json.dumps(
                        {
                            "strategy_narrative": council_decision.get("strategy_narrative"),
                            "trade_proposals": council_decision.get("trade_proposals"),
                            "target_weights": council_decision.get("target_weights"),
                            "confidence": council_decision.get("confidence"),
                            "errors": council_result.get("errors") or [],
                        },
                        default=str,
                    )
                ),
            }
        )

    # Bounded tool loop
    tool_results: list[dict[str, Any]] = []
    tool_calls = 0
    final_decision: dict[str, Any] | None = None
    invalid_action_decision: dict[str, Any] | None = None
    decision_status = "completed"
    llm_error: str | None = None
    json_error_detail: str | None = None
    json_failure_raw_excerpt: str | None = None
    json_parse_retries = 0

    loop_iterations = 0
    while loop_iterations < _MAX_TOOL_CALLS:
        loop_iterations += 1
        telemetry: dict[str, Any] = {}
        try:
            raw = _local_llm_sync(
                db,
                messages,
                timeout_s=120.0,
                json_schema=_MANDATE_RESPONSE_SCHEMA,
                sampling=STRUCTURED_JSON_SAMPLING,
                telemetry=telemetry,
            )
        except Exception as exc:
            logger.warning("LLM call failed for mandate %s: %s", mandate, exc)
            llm_error = str(exc)
            break

        parsed, _how = parse_structured_completion(raw, array_key="key_risks")
        if parsed is None:
            json_parse_retries += 1
            if json_parse_retries > _JSON_PARSE_MAX_CONSECUTIVE_RETRIES:
                json_error_detail = unusable_reason(raw, telemetry)
                json_failure_raw_excerpt = raw.strip()[:_RAW_RESPONSE_LOG_CHARS]
                logger.warning(
                    "Mandate %s: unparseable JSON after %d retries: %s",
                    mandate, json_parse_retries - 1, json_error_detail,
                )
                break
            messages = build_retry_messages(
                messages,
                raw,
                "That reply was not valid JSON. Reply with EITHER a tool call "
                "JSON or the final decision JSON, matching the schema exactly — "
                "no markdown, no commentary outside the JSON object.",
            )
            continue
        json_parse_retries = 0

        if "decision" in parsed:
            candidate = parsed["decision"]
            candidate_action = (
                str(candidate.get("action", "")).lower() if isinstance(candidate, dict) else ""
            )
            if isinstance(candidate, dict) and candidate_action not in _ALLOWED_ACTIONS:
                # Non-schema action ("rebalance", …): nudge once per loop
                # iteration instead of accepting a decision no trade can follow.
                invalid_action_decision = candidate
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f'"{candidate_action}" is not a valid action. The action must be exactly '
                            "one of: buy, sell, hold. To rebalance, pick the single most impactful "
                            "buy or sell for this review. Please return corrected decision JSON."
                        ),
                    }
                )
                continue
            final_decision = candidate
            break

        if "tool" in parsed:
            tool_calls += 1
            tool_name = parsed["tool"]
            tool_args = parsed.get("args", {})
            try:
                result = _run_review_tool(db, tool_name, tool_args)
            except Exception as exc:
                result = {"error": str(exc)}
                logger.warning("Tool %s failed for mandate %s: %s", tool_name, mandate, exc)
            tool_results.append({"tool": tool_name, "result": result})
            messages.append({"role": "assistant", "content": raw})
            remaining = _MAX_TOOL_CALLS - loop_iterations
            budget_note = (
                f" {remaining} turn(s) remain before this review defaults to a hold "
                "with no trade — reply with the final decision JSON once you have "
                "enough information, rather than continuing to explore."
                if remaining <= 3
                else ""
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Tool result for {tool_name}: {json.dumps(result, default=str)}"
                        f"{budget_note}"
                    ),
                }
            )
            continue

        messages.append(
            {
                "role": "user",
                "content": "Unrecognised response. Please return either a tool call or the decision JSON.",
            }
        )

    # Fallback if no decision — record honestly instead of masquerading as a
    # completed review, so the journal and UI can distinguish an outage from
    # a genuine hold decision.
    if final_decision is None and invalid_action_decision is not None and llm_error is None:
        # The LLM did decide, but with an action the executor cannot act on
        # and never corrected it. Persist honestly: keep its thesis, hold.
        decision_status = "invalid_action"
        bad_action = str(invalid_action_decision.get("action", "?"))
        llm_error = (
            f"LLM returned non-schema action '{bad_action}' and did not correct it; "
            "held instead (allowed: buy, sell, hold)."
        )
        final_decision = {
            **invalid_action_decision,
            "action": "hold",
            "quantity": 0,
        }

    if final_decision is None:
        if json_error_detail is not None:
            # Distinct from fallback_hold: the model never produced parseable
            # JSON even under the grammar constraint plus one correction
            # retry, rather than running out of tool-call turns while
            # otherwise cooperating. The raw excerpt lands in `error` (below)
            # since today nothing else persists a failing completion —
            # without it, this failure mode can't be sampled or replayed later.
            decision_status = "review_failed"
            llm_error = json_error_detail
            if json_failure_raw_excerpt:
                llm_error = f"{json_error_detail} | raw completion: {json_failure_raw_excerpt}"
            fallback_thesis = (
                f"LLM produced unparseable JSON even after a grammar-constrained "
                f"retry — no decision was made; holding by default. ({json_error_detail})"
            )
        elif llm_error is not None:
            decision_status = "llm_unavailable"
            fallback_thesis = f"LLM endpoint unreachable — no decision was made; holding by default. ({llm_error})"
        else:
            decision_status = "fallback_hold"
            fallback_thesis = "Max tool calls exceeded without a decision; holding by default."
        final_decision = {
            "action": "hold",
            "ticker": "",
            "quantity": 0,
            "thesis": fallback_thesis,
            "expected_outcome": "Maintain current allocation.",
            "confidence": 0.0,
        }

    # Validate / coerce decision shape
    for _ in range(_MAX_JSON_RETRIES):
        if isinstance(final_decision, dict) and "action" in final_decision:
            break
        # Ask LLM to fix shape
        messages.append(
            {
                "role": "user",
                "content": (
                    "Decision JSON must include keys: action, ticker, quantity, thesis, "
                    "key_risks, alternatives_considered, expectation. Please return corrected JSON."
                ),
            }
        )
        try:
            raw = _local_llm_sync(
                db,
                messages,
                timeout_s=120.0,
                json_schema=_MANDATE_RESPONSE_SCHEMA,
                sampling=STRUCTURED_JSON_SAMPLING,
            )
            parsed, _how = parse_structured_completion(raw, array_key="key_risks")
            if parsed and "decision" in parsed:
                final_decision = parsed["decision"]
        except Exception as exc:
            logger.warning("Decision retry failed: %s", exc)
            break

    if not isinstance(final_decision, dict) or "action" not in final_decision:
        decision_status = "fallback_hold"
        final_decision = {
            "action": "hold",
            "ticker": "",
            "quantity": 0,
            "thesis": "Decision shape invalid after retries; holding by default.",
            "expected_outcome": "Maintain current allocation.",
            "confidence": 0.0,
        }

    assert final_decision is not None

    action = str(final_decision.get("action", "hold")).lower()
    ticker = str(final_decision.get("ticker", "")).upper()
    quantity_dec = Decimal(str(final_decision.get("quantity", 0)))
    thesis = str(final_decision.get("thesis", ""))
    confidence = float(final_decision.get("confidence", 0.0))

    # Run gates for buys
    gate_result: dict[str, Any] | None = None
    trade_created = False
    trade = None
    if action == "buy" and ticker and quantity_dec > 0:
        # Resolve ISIN for ticker (best effort)
        holding = (
            db.query(PaperHolding)
            .filter(PaperHolding.portfolio_id == portfolio_id, PaperHolding.ticker == ticker)
            .one_or_none()
        )
        isin = holding.isin if holding else None
        if isin is None:
            isin = _resolve_isin(db, ticker)

        # Need a price for gates; try quote
        price = Decimal("0")
        from app.decision.paper_portfolio import paper_quote_eur

        # The book is in EUR: a quote without an exchange rate is no price.
        pq = paper_quote_eur(db, ticker)
        eur = None if pq.get("stale") else pq["price"]
        if not eur or eur <= 0:
            logger.warning("No current EUR price for %s; skipping buy gates.", ticker)
            gate_result = {
                "passed": False,
                "checks": [],
                "reason": f"No EUR price for {ticker}",
            }
        else:
            price = Decimal(str(eur))

        mandate_config = json.loads(portfolio.mandate_config_json or "{}")
        if gate_result is None:
            gate_result = check_buy_gates(
                db,
                user_id,
                mandate_config,
                ticker,
                isin,
                quantity_dec,
                price,
                mandate=mandate,
            )

        if gate_result["passed"]:
            # Same path as every other paper sleeve: execute_trade checks cash
            # (trades, fees, dividends), books the Scalable order fee and
            # upserts the holding.
            from app.decision.paper_portfolio import execute_trade, paper_order_fee

            fee = paper_order_fee(db, ticker, float(quantity_dec * price), isin=isin)
            try:
                booked = execute_trade(
                    db,
                    portfolio_id,
                    ticker,
                    "buy",
                    float(quantity_dec),
                    float(price),
                    confidence=confidence,
                    rationale=thesis,
                    fee=fee,
                )
            except ValueError as exc:
                gate_result = {
                    "passed": False,
                    "checks": gate_result.get("checks", []),
                    "reason": f"Buy of {ticker} not executed: {exc}",
                }
                logger.info("Buy of %s rejected by the book: %s", ticker, exc)
            else:
                trade = db.get(PaperTrade, booked["id"])
                trade_created = True
                if isin:
                    new_holding = (
                        db.query(PaperHolding)
                        .filter(PaperHolding.portfolio_id == portfolio_id, PaperHolding.ticker == ticker)
                        .one_or_none()
                    )
                    if new_holding is not None and not new_holding.isin:
                        new_holding.isin = isin
        else:
            logger.info("Buy rejected by gates for %s: %s", ticker, gate_result["reason"])

    elif action == "sell" and ticker and quantity_dec > 0:
        holding = (
            db.query(PaperHolding)
            .filter(PaperHolding.portfolio_id == portfolio_id, PaperHolding.ticker == ticker)
            .one_or_none()
        )
        held_qty = (holding.quantity or Decimal("0")) if holding else Decimal("0")
        if holding is None or held_qty < quantity_dec:
            gate_result = {
                "passed": False,
                "checks": [],
                "reason": f"Cannot sell {ticker}: holding {held_qty} < requested {quantity_dec}",
            }
            logger.info(
                "Sell rejected for %s: insufficient holdings (%s < %s)",
                ticker,
                held_qty,
                quantity_dec,
            )
        else:
            # A sell is booked at the current EUR quote only. Falling back to
            # the average buy price would book a fill at a price nobody quoted.
            from app.decision.paper_portfolio import (
                execute_trade,
                paper_order_fee,
                paper_quote_eur,
            )

            pq = paper_quote_eur(db, ticker)
            eur = None if pq.get("stale") else pq["price"]
            if not eur or eur <= 0:
                gate_result = {
                    "passed": False,
                    "checks": [],
                    "reason": f"Sell of {ticker} skipped: no current EUR price",
                }
                logger.info("Sell of %s skipped: no current EUR price", ticker)
            else:
                price = Decimal(str(eur))
                fee = paper_order_fee(
                    db, ticker, float(quantity_dec * price), isin=holding.isin, name=holding.name, side="sell"
                )
                try:
                    booked = execute_trade(
                        db,
                        portfolio_id,
                        ticker,
                        "sell",
                        float(quantity_dec),
                        float(price),
                        confidence=confidence,
                        rationale=thesis,
                        fee=fee,
                    )
                except ValueError as exc:
                    gate_result = {
                        "passed": False,
                        "checks": [],
                        "reason": f"Sell of {ticker} not executed: {exc}",
                    }
                    logger.info("Sell of %s rejected by the book: %s", ticker, exc)
                else:
                    trade = db.get(PaperTrade, booked["id"])
                    trade_created = True
                    gate_result = {"passed": True, "checks": [], "reason": "Sell executed"}

    # Overwrite whatever the LLM echoed for `assessment` with the authoritative
    # Python-computed facts, so stored facts can never be model-fabricated.
    if isinstance(final_decision, dict) and assessment_facts is not None:
        final_decision["assessment"] = assessment_facts

    # Horizon for outcome scoring comes from the structured expectation. Only
    # genuine (non-fallback) decisions carry one; fallbacks stay unscored.
    horizon_weeks: int | None = None
    expectation = final_decision.get("expectation") if isinstance(final_decision, dict) else None
    if isinstance(expectation, dict) and expectation.get("horizon_weeks") is not None:
        try:
            horizon_weeks = int(expectation["horizon_weeks"])
        except (TypeError, ValueError):
            horizon_weeks = None

    # Persist LlmPortfolioDecision
    decision_row = LlmPortfolioDecision(
        portfolio_id=portfolio_id,
        review_date=datetime.now(UTC),
        mandate=mandate,
        decision_json=json.dumps(final_decision),
        reflection_json=json.dumps(
            {
                "tool_results": tool_results,
                "gate_result": gate_result,
                "trade_created": trade_created,
                "council": council_result,
            },
            default=str,
        ),
        context_token_estimate=token_estimate,
        status=decision_status,
        horizon_weeks=horizon_weeks,
        error=llm_error,
    )
    db.add(decision_row)
    db.flush()

    # Link trade to decision if created
    if trade_created and trade is not None:
        db.refresh(decision_row)
        trade.ai_decision_id = decision_row.id

    db.commit()

    # Divergence + advice cards
    try:
        divergence = compute_divergence(db, user_id, portfolio_id)
        perf_delta = float(divergence.get("perf_delta", 0.0))
        if abs(perf_delta) > 0.05:
            advice_text = (
                f"Paper portfolio divergence detected (delta {perf_delta:.2%}). "
                "Review recommended. Any trades are never automated — confirm at DKB yourself."
            )
            card = LlmAdviceCard(
                user_id=user_id,
                portfolio_id=portfolio_id,
                divergence_json=json.dumps(divergence),
                advice_text=advice_text,
                performance_delta=Decimal(str(perf_delta)),
                status="new",
            )
            db.add(card)
            db.commit()
    except Exception as exc:
        logger.warning("Divergence check failed: %s", exc)

    # Telegram digest — best-effort notification, never fails the review.
    try:
        from app.foundation.telegram_bot import get_linked_account, send_telegram_message
        from app.foundation.telegram_replies import format_mandate_review_digest

        account = get_linked_account(db, user_id)
        if account is not None and isinstance(final_decision, dict):
            digest = format_mandate_review_digest(
                mandate,
                final_decision,
                gate_result,
                trade_created,
                decision_status,
            )
            send_telegram_message(db, account.chat_id, digest)
    except Exception as exc:
        logger.warning("Telegram digest failed for mandate %s: %s", mandate, exc)

    return {
        "portfolio_id": portfolio_id,
        "mandate": mandate,
        "decision": final_decision,
        "gate_result": gate_result,
        "trade_created": trade_created,
        "tool_calls": tool_calls,
        "token_estimate": token_estimate,
        "status": decision_status,
    }
