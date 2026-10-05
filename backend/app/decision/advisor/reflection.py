"""Reflection memory for the advisor loop (PR2 A2/A3).

After each scored cycle the LLM distils concrete lessons — which
signals/regimes/sectors helped or hurt, what to do more/less of — from the
matured 4-axis scorecard and the decisions that produced it. Lessons are
persisted as ``StrategyLesson`` rows and injected into the next decision's
prompt context.

Deterministic guardrails keep the memory bounded: at most
``MAX_NEW_PER_CYCLE`` lessons per reflection, existing lesson ranks decay each
cycle, and anything past ``MAX_ACTIVE_LESSONS`` (or below ``MIN_RANK``) is
deactivated. The LLM is stubbed in tests via the injectable ``llm_call``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.foundation.models.entities import AdvisorScorecard, AdvisorStrategy, StrategyLesson
from app.foundation.llm.structured import (
    STRUCTURED_JSON_SAMPLING,
    build_retry_messages,
    parse_structured_completion,
    unusable_reason,
)

logger = logging.getLogger(__name__)

MAX_NEW_PER_CYCLE = 3
MAX_ACTIVE_LESSONS = 10
RANK_DECAY = 0.85
MIN_RANK = 0.3
_MAX_LESSON_CHARS = 400
_MAX_JSON_RETRIES = 2

# Bounds for the remaining free-text fields. A tag is a label, not a sentence,
# and the arrays are read as tags too — so a handful of short strings each.
_MAX_TAG_CHARS = 48
_MAX_TAGS_PER_LIST = 6

# Completion budget. At most ``MAX_NEW_PER_CYCLE`` lessons of
# ``_MAX_LESSON_CHARS`` plus their tags is a few hundred tokens; the rest is
# headroom. The point is that a bound exists at all — without one the call took
# ``DEFAULT_MAX_TOKENS`` (8192), so a runaway completion could burn the whole
# budget and still be a JSON fragment.
_COMPLETION_TOKENS = 1024

# How much of a rejected completion is quoted back on a retry, so echoing one
# runaway answer cannot push the next attempt past the context window.
_RETRY_EXCERPT_CHARS = 800

_SYSTEM_PROMPT = (
    "You are the reflection module of an autonomous paper-trading advisor. "
    "You receive the strategy's matured 4-axis scorecard (risk-adjusted "
    "return, calibration, magnitude accuracy, downside discipline) and the "
    "recent trade decisions with outcomes. Distil at most "
    f"{MAX_NEW_PER_CYCLE} concrete, actionable lessons grounded ONLY in the "
    "provided numbers — which signals/regimes helped vs hurt, and what to "
    "do more or less of next cycle.\n"
    "Each decision includes instrument_type ('equity', 'etf', or "
    "'money_market'). The 'sectors' tag is for equity sector attribution "
    "ONLY: populate it only when every decision it draws on has "
    "instrument_type=='equity' and you can name the sector with reasonable "
    "confidence from the ticker itself. Never populate 'sectors' from an "
    "'etf' or 'money_market' decision — a fund is not a single sector and "
    "you have no sector data for it; leave sectors as [] for lessons about "
    "those. Do not guess a sector when unsure.\n"
    "Respond with exactly one JSON object, no markdown:\n"
    '{"lessons": [{"lesson": "one concrete sentence", '
    '"tags": {"signals": ["..."], "regime": "...", "sectors": ["..."]}}]}'
)


# Grammar-constrains the reflection output. ``tags`` is spelled out rather
# than left as a free-form object: llama.cpp's schema→GBNF converter needs
# concrete properties, and these are the only keys ``_parse_lessons`` reads.
#
# Every string carries a ``maxLength``, for the reason the decision schema does
# (see ``llm_decision.MAX_THESIS_CHARS``): a grammar-constrained
# ``{"type": "string"}`` has no length bound, so the model may keep extending
# one until it hits the completion budget — at which point the JSON is a
# fragment and the whole reflection is discarded. A grammar bounds *structure*,
# not *termination*; inside a `"lesson": "…"` every repeated token is still
# legal JSON. llama.cpp compiles these into GBNF repetitions and fails open
# above 2000, so all four caps stay far below that.
LESSON_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lessons": {
            "type": "array",
            "maxItems": MAX_NEW_PER_CYCLE,
            "items": {
                "type": "object",
                "properties": {
                    # minLength closes the same gap fixed in advisor/llm_decision.py's
                    # thesis field (audit-fixes-2026-08 todo 6): without it, the
                    # grammar has no reason to refuse "" for a required field.
                    "lesson": {"type": "string", "minLength": 8, "maxLength": _MAX_LESSON_CHARS},
                    "tags": {
                        "type": "object",
                        "properties": {
                            "signals": {
                                "type": "array",
                                "maxItems": _MAX_TAGS_PER_LIST,
                                "items": {"type": "string", "maxLength": _MAX_TAG_CHARS},
                            },
                            "regime": {"type": "string", "maxLength": _MAX_TAG_CHARS},
                            "sectors": {
                                "type": "array",
                                "maxItems": _MAX_TAGS_PER_LIST,
                                "items": {"type": "string", "maxLength": _MAX_TAG_CHARS},
                            },
                        },
                    },
                },
                "required": ["lesson"],
            },
        }
    },
    "required": ["lessons"],
}


def _default_llm_call(
    db: Session,
    messages: list[dict[str, str]],
    telemetry: dict[str, Any] | None = None,
) -> str:
    from app.decision.llm_portfolio.review import _local_llm_sync

    # Same model, same structured-output job, same failure mode — so the same
    # preset. The default this would otherwise get (temperature 0.2 with
    # ``presence_penalty`` zeroed) is the near-greedy, no-repetition-defence
    # combination that produced the runaway completions on the decision call.
    return _local_llm_sync(
        db,
        messages,
        json_schema=LESSON_JSON_SCHEMA,
        sampling=STRUCTURED_JSON_SAMPLING,
        max_tokens=_COMPLETION_TOKENS,
        telemetry=telemetry,
    )


def _scorecard_payload(scorecard: AdvisorScorecard) -> dict[str, Any]:
    details: dict[str, Any] = scorecard.details_json or {}
    return {
        "window": [scorecard.window_start.isoformat(), scorecard.window_end.isoformat()],
        "n_resolved": scorecard.n_resolved,
        "risk_adjusted_return": {
            "sharpe": scorecard.sharpe,
            "sortino": scorecard.sortino,
            "calmar": scorecard.calmar,
            # The model is drawing lessons from these numbers, so it needs to
            # know how much of one there is: a Sharpe whose 95% interval spans
            # zero is not evidence the strategy worked or failed.
            "sharpe_standard_error": details.get("sharpe_se"),
            "sharpe_ci_spans_zero": details.get("sharpe_ci_spans_zero"),
            "n_nav_returns": details.get("n_nav_returns"),
        },
        "calibration": {"brier_avg": scorecard.brier_avg, "log_loss_avg": scorecard.log_loss_avg},
        "magnitude_accuracy": {"mz_slope": scorecard.mz_slope, "mz_r2": scorecard.mz_r2},
        "downside_discipline": {
            "max_drawdown": scorecard.max_drawdown,
            "cvar_95": scorecard.cvar_95,
        },
    }


def _parse_lessons(raw: str) -> tuple[list[dict[str, Any]], str]:
    """Parse + validate the LLM reflection output; invalid items are dropped.

    Returns ``(items, how)`` with ``how`` in ``strict``/``embedded``/
    ``salvaged``/``unparseable`` — a completion cut off at the token limit is
    a well-formed prefix of valid JSON (``{"lessons": [ {…}, {"lesson":``),
    and discarding the whole batch over one incomplete tail element throws
    away lessons the model did commit to.
    """
    parsed, how = parse_structured_completion(raw, "lessons")
    if parsed is None:
        return [], how
    items = parsed.get("lessons")
    if not isinstance(items, list):
        return [], "unparseable"
    valid: list[dict[str, Any]] = []
    for item in items[:MAX_NEW_PER_CYCLE]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("lesson", "")).strip()
        if not text:
            continue
        tags = item.get("tags")
        valid.append({
            "lesson": text[:_MAX_LESSON_CHARS],
            "tags": tags if isinstance(tags, dict) else {},
        })
    return valid, how


def decay_and_prune_lessons(db: Session, strategy_id: str) -> None:
    """Decay ranks of active lessons; deactivate stale/overflow ones."""
    active = (
        db.query(StrategyLesson)
        .filter(StrategyLesson.strategy_id == strategy_id, StrategyLesson.active.is_(True))
        .order_by(StrategyLesson.rank.desc(), StrategyLesson.created_at.desc())
        .all()
    )
    for lesson in active:
        lesson.rank = float(lesson.rank) * RANK_DECAY
        if lesson.rank < MIN_RANK:
            lesson.active = False
    still_active = [lesson for lesson in active if lesson.active]
    for lesson in still_active[MAX_ACTIVE_LESSONS:]:
        lesson.active = False
    db.flush()


def reflect_on_cycle(
    db: Session,
    strategy: AdvisorStrategy,
    scorecard: AdvisorScorecard,
    decisions: list[dict[str, Any]],
    *,
    llm_call: Callable[[Session, list[dict[str, str]]], str] | None = None,
) -> list[StrategyLesson]:
    """Distil and persist lessons from a matured scorecard (A2).

    Args:
        db: Active session (caller owns the transaction; rows are flushed).
        strategy: The strategy that produced the cycle (lessons attach here).
        scorecard: The matured 4-axis scorecard for the window.
        decisions: Recent decision dicts (ticker/action/thesis/confidence +
            realised outcome where known) — the raw material for reflection.
        llm_call: Injectable LLM callable for tests.

    Returns:
        Newly created ``StrategyLesson`` rows (possibly empty — an unusable
        LLM response yields no lessons, never fabricated ones).
    """
    telemetry: dict[str, Any] = {}

    def _instrumented_call(session: Session, msgs: list[dict[str, str]]) -> str:
        telemetry.clear()
        return _default_llm_call(session, msgs, telemetry=telemetry)

    call = llm_call or _instrumented_call
    payload = {
        "scorecard": _scorecard_payload(scorecard),
        "decisions": decisions[:50],
    }
    base_messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, default=str)},
    ]
    messages = list(base_messages)
    items: list[dict[str, Any]] = []
    for attempt in range(_MAX_JSON_RETRIES + 1):
        try:
            raw = call(db, messages)
        except Exception:
            logger.exception("reflection: LLM call failed — no lessons this cycle")
            return []

        items, parsed_as = _parse_lessons(raw)
        if items:
            if parsed_as == "salvaged":
                logger.warning(
                    "reflection: completion cut off at the token limit; recovered "
                    "%d complete lesson(s) and discarded the truncated tail",
                    len(items),
                )
            break
        if attempt < _MAX_JSON_RETRIES:
            reason = unusable_reason(raw, telemetry)
            logger.warning(
                "reflection: unusable response (attempt %d/%d): %s",
                attempt + 1, _MAX_JSON_RETRIES + 1, reason,
            )
            instruction = (
                f"That response was unusable: {reason}. Reply with exactly one JSON "
                'object matching the schema, no markdown: {"lessons": '
                '[{"lesson": "one concrete sentence", "tags": {...}}]}. Keep every '
                f"lesson under {_MAX_LESSON_CHARS} characters so the object closes "
                "inside the token budget."
            )
            # Rebuilt from the base, carrying only an excerpt: appending each
            # rejected completion to a growing list is how a single runaway
            # answer makes every remaining retry fail too, since llama-server
            # runs with ``--no-context-shift``.
            messages = build_retry_messages(base_messages, raw, instruction, excerpt_chars=_RETRY_EXCERPT_CHARS)

    if not items:
        logger.warning("reflection: unusable LLM response after retries — no lessons persisted")
        return []

    # Defense-in-depth beyond the prompt instruction: if nothing in this
    # batch was an equity, there is no legitimate basis for a sector tag at
    # all — strip it rather than trust the LLM followed the prompt (prod
    # incident: XEON.DE, a money-market ETF, was tagged "sectors":
    # ["technology"] with no equity anywhere in its decision history).
    any_equity = any(d.get("instrument_type") == "equity" for d in decisions[:50])
    if not any_equity:
        for item in items:
            tags = item.get("tags")
            if isinstance(tags, dict) and tags.get("sectors"):
                tags["sectors"] = []

    # Decay first so fresh lessons enter at full rank on a level field.
    decay_and_prune_lessons(db, strategy.id)

    created: list[StrategyLesson] = []
    for item in items:
        row = StrategyLesson(
            user_id=strategy.user_id,
            strategy_id=strategy.id,
            portfolio_id=strategy.portfolio_id,
            lesson_text=item["lesson"],
            tags_json=item["tags"],
            source_scorecard_id=scorecard.id,
            rank=1.0,
            active=True,
        )
        db.add(row)
        created.append(row)
    db.flush()

    # Re-apply the cap including the newcomers (newest+highest rank win).
    decayed = (
        db.query(StrategyLesson)
        .filter(StrategyLesson.strategy_id == strategy.id, StrategyLesson.active.is_(True))
        .order_by(StrategyLesson.rank.desc(), StrategyLesson.created_at.desc())
        .all()
    )
    for lesson in decayed[MAX_ACTIVE_LESSONS:]:
        lesson.active = False
    db.flush()

    logger.info("reflection: %d lessons persisted for strategy %s", len(created), strategy.id)
    return created


def get_active_lessons(db: Session, strategy_id: str, *, limit: int = MAX_ACTIVE_LESSONS) -> list[str]:
    """Load the strategy's active lesson texts, best-ranked first (A3)."""
    rows = (
        db.query(StrategyLesson)
        .filter(StrategyLesson.strategy_id == strategy_id, StrategyLesson.active.is_(True))
        .order_by(StrategyLesson.rank.desc(), StrategyLesson.created_at.desc())
        .limit(limit)
        .all()
    )
    return [r.lesson_text for r in rows]
