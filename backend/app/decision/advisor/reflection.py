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

Lessons are checked against numbers computed here, not taken on trust
(2026-11-09 audit: prod lessons claimed "over 70% of buys gained" when they had
not, contradicted each other, repeated themselves and drew on trades from
before the sleeve's last reset). ``track_record_stats`` computes the sleeve's
own record since its inception; every lesson must cite one of its groups
(``stat``) with at least ``MIN_LESSON_N`` resolved trades, every percentage it
quotes must match that group, and its ``verdict`` must agree with the group's
sign. A newer lesson on the same group replaces the older one, so the memory
cannot hold two contradicting lessons about the same evidence.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.foundation.instrument_taxonomy import classify_instrument
from app.foundation.models.entities import (
    AdvisorScorecard,
    AdvisorStrategy,
    DiscoveryPrediction,
    PaperPortfolio,
    StrategyLesson,
)
from app.foundation.llm.structured import (
    STRUCTURED_JSON_SAMPLING,
    build_retry_messages,
    parse_structured_completion,
    unusable_reason,
)

logger = logging.getLogger(__name__)

MAX_NEW_PER_CYCLE = 3
MAX_ACTIVE_LESSONS = 5
# A lesson resting on fewer resolved trades than this is an anecdote: the
# research pass (2026-11-09) found free-text reflections on thin samples mostly
# record the market's move, not the decision (ExpeL, MemTrial).
MIN_LESSON_N = 20
# And its hit rate must differ from a coin flip at roughly 80 % two-sided
# confidence (|z| >= 1.28): at n = 20 that is <= 36 % or >= 64 %.
MIN_LESSON_Z = 1.28
# The groups a lesson may cite (``track_record_stats``'s ``groups`` keys).
STAT_KEYS: tuple[str, ...] = (
    "all", "buys", "sells", "high_confidence", "low_confidence", "equities", "funds",
)
VERDICTS: tuple[str, ...] = ("working", "not_working")
HIGH_CONFIDENCE = 0.6
COIN_FLIP_BRIER = 0.25
# A quoted percentage may differ from the computed one by rounding, not more.
_PCT_TOLERANCE = 2.0
_CONFIDENCE_BINS: tuple[tuple[float, float], ...] = ((0.0, 0.5), (0.5, 0.65), (0.65, 1.0001))
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
    "You receive `track_record`: the sleeve's own resolved trades since its "
    "last reset, grouped (all, buys, sells, high_confidence, low_confidence, "
    "equities, funds). Per group: n, hit_rate (share of trades that beat "
    "EUNL.DE, MSCI World, over 21 trading days), mean_excess_pct (average "
    "return over EUNL.DE in percent), mean_confidence and brier (0.25 is a "
    "coin flip; higher is worse). `calibration.bins` compares stated "
    "confidence with how often those trades actually won. You also get the "
    "matured scorecard and the recent decisions with outcomes. Distil at most "
    f"{MAX_NEW_PER_CYCLE} concrete, actionable lessons grounded ONLY in these "
    "numbers — what to do more or less of next cycle.\n"
    "Every lesson must name the ONE group it rests on in `stat` and state "
    "`verdict`: `working` only if that group's mean_excess_pct > 0 and "
    "hit_rate >= 0.5, `not_working` only if mean_excess_pct <= 0 and "
    f"hit_rate <= 0.5. Cite only groups with n >= {MIN_LESSON_N} whose hit "
    "rate is clearly away from 50%; with mixed or thin evidence write no lesson — an empty list is a valid answer. Any "
    "percentage you quote must be that group's own number. Lessons that break "
    "these rules are discarded.\n"
    "Each decision includes instrument_type ('equity', 'etf', or "
    "'money_market'). The 'sectors' tag is for equity sector attribution "
    "ONLY: populate it only when every decision it draws on has "
    "instrument_type=='equity' and you can name the sector with reasonable "
    "confidence from the ticker itself. Never populate 'sectors' from an "
    "'etf' or 'money_market' decision — a fund is not a single sector and "
    "you have no sector data for it; leave sectors as [] for lessons about "
    "those. Do not guess a sector when unsure.\n"
    "Respond with exactly one JSON object, no markdown:\n"
    '{"lessons": [{"lesson": "one concrete sentence", "stat": "buys", '
    '"verdict": "not_working", '
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
                    "stat": {"type": "string", "enum": list(STAT_KEYS)},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
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
                "required": ["lesson", "stat", "verdict"],
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


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _sleeve_start(db: Session, portfolio_id: str | None) -> datetime | None:
    """When the sleeve's current paper run began (its last reset)."""
    if portfolio_id is None:
        return None
    portfolio = db.get(PaperPortfolio, portfolio_id)
    return _aware(portfolio.inception_at) if portfolio is not None else None


def _judged_excess(p: DiscoveryPrediction) -> float:
    """Return over the passive core in the bet's direction (raw move before
    the benchmark was recorded) — the same yardstick the scorecard's hit uses."""
    if p.excess_return is not None:
        return float(p.excess_return)
    return float(p.realised_return or 0.0)


def _group_stats(rows: list[DiscoveryPrediction]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0, "hit_rate": None, "mean_excess_pct": None, "mean_confidence": None, "brier": None}
    excess = [_judged_excess(r) for r in rows]
    hits = [1 if e > 0 else 0 for e in excess]
    confs = [float(r.conviction) for r in rows if r.conviction is not None]
    pairs = [(float(r.conviction), h) for r, h in zip(rows, hits) if r.conviction is not None]
    return {
        "n": n,
        "hit_rate": round(sum(hits) / n, 3),
        "mean_excess_pct": round(100.0 * sum(excess) / n, 2),
        "mean_confidence": round(sum(confs) / len(confs), 3) if confs else None,
        "brier": round(sum((c - h) ** 2 for c, h in pairs) / len(pairs), 3) if pairs else None,
    }


def track_record_stats(db: Session, user_id: str, portfolio_id: str | None) -> dict[str, Any]:
    """The sleeve's own resolved directional calls since its last reset.

    Deterministic, so a lesson can be checked against it and the decision
    prompt can show the model its real calibration instead of its memory of it.
    """
    since = _sleeve_start(db, portfolio_id)
    rows: list[DiscoveryPrediction] = []
    if portfolio_id is not None:
        query = db.query(DiscoveryPrediction).filter(
            DiscoveryPrediction.user_id == user_id,
            DiscoveryPrediction.portfolio_id == portfolio_id,
            DiscoveryPrediction.outcome_status == "resolved",
            DiscoveryPrediction.realised_return.isnot(None),
            DiscoveryPrediction.direction.in_(("buy", "sell", "bull", "bear")),
        )
        rows = [
            r for r in query.all()
            if since is None or (_aware(r.predicted_at) or since) >= since
        ]

    def conf(r: DiscoveryPrediction) -> float | None:
        return float(r.conviction) if r.conviction is not None else None

    groups = {
        "all": rows,
        "buys": [r for r in rows if r.direction in ("buy", "bull")],
        "sells": [r for r in rows if r.direction in ("sell", "bear")],
        "high_confidence": [r for r in rows if (c := conf(r)) is not None and c >= HIGH_CONFIDENCE],
        "low_confidence": [r for r in rows if (c := conf(r)) is not None and c < HIGH_CONFIDENCE],
        "equities": [r for r in rows if classify_instrument(r.symbol) == "equity"],
        "funds": [r for r in rows if classify_instrument(r.symbol) != "equity"],
    }
    bins = []
    for lo, hi in _CONFIDENCE_BINS:
        members = [r for r in rows if (c := conf(r)) is not None and lo <= c < hi]
        g = _group_stats(members)
        bins.append({
            "stated": f"{lo:.2f}-{min(hi, 1.0):.2f}",
            "n": g["n"],
            "mean_stated": g["mean_confidence"],
            "realised_hit_rate": g["hit_rate"],
        })
    overall = _group_stats(rows)
    return {
        "since": since.date().isoformat() if since else None,
        "benchmark": "EUNL.DE",
        "horizon_trading_days": 21,
        "groups": {key: _group_stats(members) for key, members in groups.items()},
        "calibration": {
            "brier": overall["brier"],
            "coin_flip_brier": COIN_FLIP_BRIER,
            "bins": bins,
        },
    }


_PERCENT_RE = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*(?:%|percent|pp\b)", re.IGNORECASE)
_FRACTION_RE = re.compile(r"(?<![\d.])(0[.,]\d{1,3})(?![\d%])")


def lesson_rejection(item: dict[str, Any], stats: dict[str, Any]) -> str | None:
    """Why a lesson does not match the computed record (``None`` = it does)."""
    key = item.get("stat")
    verdict = item.get("verdict")
    group = (stats.get("groups") or {}).get(key) if isinstance(key, str) else None
    if group is None:
        return f"unknown stat {key!r}"
    n = int(group.get("n") or 0)
    if n < MIN_LESSON_N:
        return f"{key} rests on {n} trade(s), fewer than {MIN_LESSON_N}"
    hit = group.get("hit_rate")
    excess = group.get("mean_excess_pct")
    if hit is None or excess is None:
        return f"{key} has no outcome numbers"
    z = (float(hit) - 0.5) / (0.25 / n) ** 0.5
    if abs(z) < MIN_LESSON_Z:
        return f"{key}'s hit rate {hit:.0%} over {n} trades is not distinguishable from a coin flip"
    if verdict == "working":
        if not (excess > 0 and hit >= 0.5):
            return f"verdict 'working' contradicts {key}: hit {hit:.0%}, excess {excess:+.2f}%"
    elif verdict == "not_working":
        if not (excess <= 0 and hit <= 0.5):
            return f"verdict 'not_working' contradicts {key}: hit {hit:.0%}, excess {excess:+.2f}%"
    else:
        return f"unknown verdict {verdict!r}"

    # Every number the lesson quotes must be one of the numbers it rests on.
    text = str(item.get("lesson", ""))
    allowed_pct = [float(hit) * 100, float(excess)]
    # A percentage that happens to equal the stated confidence must not pass
    # as a hit rate ("70% of buys gained" when confidence averaged 0.70 and
    # 25% won), so the confidence figures count only where the lesson is
    # talking about confidence.
    if "confiden" in text.lower():
        allowed_pct += [float(v) * 100 for v in (group.get("mean_confidence"),) if v is not None]
        allowed_pct += [
            float(v) * 100
            for b in ((stats.get("calibration") or {}).get("bins") or [])
            for v in (b.get("mean_stated"), b.get("realised_hit_rate"))
            if v is not None
        ]
    for raw in _PERCENT_RE.findall(text):
        value = float(raw.replace(",", "."))
        if not any(abs(abs(value) - abs(a)) <= _PCT_TOLERANCE for a in allowed_pct):
            return f"quotes {value:g}% but {key} shows hit {hit:.0%}, excess {excess:+.2f}%"
    allowed_frac = [a / 100 for a in allowed_pct]
    calibration = stats.get("calibration") or {}
    allowed_frac += [
        float(b) for b in (group.get("brier"), calibration.get("brier"), COIN_FLIP_BRIER) if b is not None
    ]
    for raw in _FRACTION_RE.findall(text):
        value = float(raw.replace(",", "."))
        if not any(abs(value - a) <= _PCT_TOLERANCE / 100 for a in allowed_frac):
            return f"quotes {value:g}, which is not among {key}'s numbers"
    return None


def _parse_lessons(raw: str) -> tuple[list[dict[str, Any]], str]:
    """Parse + validate the LLM reflection output; invalid items are dropped.

    Returns ``(items, how)`` with ``how`` in ``strict``/``embedded``/
    ``salvaged``/``unparseable`` — a completion cut off at the token limit is
    a well-formed prefix of valid JSON (``{"lessons": [ {…}, {"lesson":``),
    and discarding the whole batch over one incomplete tail element throws
    away lessons the model did commit to. An empty ``lessons`` list parses as
    ``strict`` with no items: "nothing is supported by the numbers" is a valid
    answer, not a failure to retry.
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
            "stat": item.get("stat"),
            "verdict": item.get("verdict"),
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


def _is_current(lesson: StrategyLesson, since: datetime | None) -> bool:
    """A verified lesson (cites a stat) written during the current paper run."""
    tags = lesson.tags_json or {}
    if not isinstance(tags, dict) or tags.get("stat") not in STAT_KEYS:
        return False
    created = _aware(lesson.created_at)
    return since is None or created is None or created >= since


def retire_unverified_lessons(db: Session, strategy: AdvisorStrategy) -> int:
    """Deactivate lessons that cite no stat or predate the sleeve's last reset."""
    since = _sleeve_start(db, strategy.portfolio_id)
    retired = 0
    for lesson in (
        db.query(StrategyLesson)
        .filter(StrategyLesson.strategy_id == strategy.id, StrategyLesson.active.is_(True))
        .all()
    ):
        if not _is_current(lesson, since):
            lesson.active = False
            retired += 1
    db.flush()
    return retired


def reflect_on_cycle(
    db: Session,
    strategy: AdvisorStrategy,
    scorecard: AdvisorScorecard,
    decisions: list[dict[str, Any]],
    *,
    llm_call: Callable[[Session, list[dict[str, str]]], str] | None = None,
    stats: dict[str, Any] | None = None,
) -> list[StrategyLesson]:
    """Distil and persist lessons from a matured scorecard (A2).

    Args:
        db: Active session (caller owns the transaction; rows are flushed).
        strategy: The strategy that produced the cycle (lessons attach here).
        scorecard: The matured 4-axis scorecard for the window.
        decisions: Recent decision dicts (ticker/action/thesis/confidence +
            realised outcome where known) — the raw material for reflection.
        llm_call: Injectable LLM callable for tests.
        stats: The sleeve's ``track_record_stats``; computed when omitted.

    Returns:
        Newly created ``StrategyLesson`` rows (possibly empty — an unusable
        LLM response, too little data, or lessons the numbers do not back
        yield no lessons, never fabricated ones).
    """
    retire_unverified_lessons(db, strategy)
    if stats is None:
        stats = track_record_stats(db, strategy.user_id, strategy.portfolio_id)
    n_total = int(((stats.get("groups") or {}).get("all") or {}).get("n") or 0)
    if n_total < MIN_LESSON_N:
        logger.info(
            "reflection: %d resolved trade(s) since the sleeve began — fewer than %d, no lessons yet",
            n_total, MIN_LESSON_N,
        )
        return []

    telemetry: dict[str, Any] = {}

    def _instrumented_call(session: Session, msgs: list[dict[str, str]]) -> str:
        telemetry.clear()
        return _default_llm_call(session, msgs, telemetry=telemetry)

    call = llm_call or _instrumented_call
    payload = {
        "track_record": stats,
        "scorecard": _scorecard_payload(scorecard),
        "decisions": decisions[:50],
    }
    base_messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, default=str)},
    ]
    messages = list(base_messages)
    items: list[dict[str, Any]] = []
    parsed_as = "unparseable"
    for attempt in range(_MAX_JSON_RETRIES + 1):
        try:
            raw = call(db, messages)
        except Exception:
            logger.exception("reflection: LLM call failed — no lessons this cycle")
            return []

        items, parsed_as = _parse_lessons(raw)
        if items or parsed_as in ("strict", "embedded"):
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
                '[{"lesson": "one concrete sentence", "stat": "...", "verdict": "...", '
                '"tags": {...}}]}. Keep every '
                f"lesson under {_MAX_LESSON_CHARS} characters so the object closes "
                "inside the token budget."
            )
            # Rebuilt from the base, carrying only an excerpt: appending each
            # rejected completion to a growing list is how a single runaway
            # answer makes every remaining retry fail too, since llama-server
            # runs with ``--no-context-shift``.
            messages = build_retry_messages(base_messages, raw, instruction, excerpt_chars=_RETRY_EXCERPT_CHARS)

    if not items:
        if parsed_as in ("strict", "embedded"):
            logger.info("reflection: the model found nothing the numbers support — no lessons")
        else:
            logger.warning("reflection: unusable LLM response after retries — no lessons persisted")
        return []

    # Keep only lessons the numbers back, at most one per group.
    accepted: list[dict[str, Any]] = []
    for item in items:
        reason = lesson_rejection(item, stats)
        if reason is None and any(a["stat"] == item["stat"] for a in accepted):
            reason = f"second lesson on {item['stat']} in one batch"
        if reason is not None:
            logger.info("reflection: discarded lesson %r — %s", item["lesson"][:80], reason)
            continue
        accepted.append(item)
    if not accepted:
        logger.info("reflection: no lesson matched the computed record — none persisted")
        return []

    # Defense-in-depth beyond the prompt instruction: if nothing in this
    # batch was an equity, there is no legitimate basis for a sector tag at
    # all — strip it rather than trust the LLM followed the prompt (prod
    # incident: XEON.DE, a money-market ETF, was tagged "sectors":
    # ["technology"] with no equity anywhere in its decision history).
    any_equity = any(d.get("instrument_type") == "equity" for d in decisions[:50])
    if not any_equity:
        for item in accepted:
            tags = item.get("tags")
            if isinstance(tags, dict) and tags.get("sectors"):
                tags["sectors"] = []

    # Decay first so fresh lessons enter at full rank on a level field.
    decay_and_prune_lessons(db, strategy.id)

    # A newer lesson on the same evidence replaces the older one: two active
    # lessons about the same group are either duplicates or a contradiction.
    cited = {item["stat"] for item in accepted}
    for old in (
        db.query(StrategyLesson)
        .filter(StrategyLesson.strategy_id == strategy.id, StrategyLesson.active.is_(True))
        .all()
    ):
        if (old.tags_json or {}).get("stat") in cited:
            old.active = False

    groups = stats.get("groups") or {}
    created: list[StrategyLesson] = []
    for item in accepted:
        row = StrategyLesson(
            user_id=strategy.user_id,
            strategy_id=strategy.id,
            portfolio_id=strategy.portfolio_id,
            lesson_text=item["lesson"],
            tags_json={
                **item["tags"],
                "stat": item["stat"],
                "verdict": item["verdict"],
                "n": int((groups.get(item["stat"]) or {}).get("n") or 0),
            },
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
    """The strategy's verified lessons from the current run, best-ranked first (A3).

    Each reads ``[group, n=…, verdict] text`` so the model sees how much
    evidence a lesson rests on. Lessons that cite no stat or predate the
    sleeve's last reset are never shown, even before the next reflection
    retires them.
    """
    strategy = db.get(AdvisorStrategy, strategy_id)
    since = _sleeve_start(db, strategy.portfolio_id) if strategy is not None else None
    rows = (
        db.query(StrategyLesson)
        .filter(StrategyLesson.strategy_id == strategy_id, StrategyLesson.active.is_(True))
        .order_by(StrategyLesson.rank.desc(), StrategyLesson.created_at.desc())
        .all()
    )
    out: list[str] = []
    for r in rows:
        if not _is_current(r, since):
            continue
        tags = r.tags_json or {}
        out.append(f"[{tags.get('stat')}, n={tags.get('n')}, {tags.get('verdict')}] {r.lesson_text}")
        if len(out) >= limit:
            break
    return out
