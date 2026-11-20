"""Shadow forecasts on the same inputs (ADR 0025, plan 2026-11-10 WS3).

Each cycle day, after both sleeves have run, the champion and the challenger
each forecast EVERY candidate on that day's Discover shortlist: the
probability that the stock beats the benchmark (EUNL, MSCI World EUR) over 21
trading days, and a p5-p95 band for its EUR return. Matched forecasts are
what the paired promotion test (:mod:`promotion`) compares: same tickers, same
dates, so the two strategies are scored on identical questions, unlike their
trades, which differ in what and when.

LLM budget: one batched call per strategy per day, the whole shortlist in one
prompt, on the local model first (the same call path as the cycle's decision).
Resolution runs in the evolution round against EUR closes from
:mod:`app.foundation.eur_prices`.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Callable

import numpy as np
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.foundation.models.entities import AdvisorShadowForecast, AdvisorStrategy
from app.foundation.models.entities._core import now_utc

logger = logging.getLogger(__name__)

HORIZON_DAYS = 21
#: Calendar days after the horizon a missing close is waited for.
RESOLUTION_GRACE_DAYS = 10
#: The band is read as p5-p95: 5 % below, 90 % inside, 5 % above.
_BAND_CUMULATIVE = (0.05, 0.95)
_P_CLIP = (0.01, 0.99)

SHADOW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "forecasts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "p_beat": {"type": "number"},
                    "band_low": {"type": "number"},
                    "band_high": {"type": "number"},
                },
                "required": ["ticker", "p_beat", "band_low", "band_high"],
            },
        }
    },
    "required": ["forecasts"],
}

_SYSTEM = (
    "You forecast stocks for a paper-trading experiment. For EVERY ticker listed, give the "
    "probability (0 to 1) that its total return in EUR over the next 21 trading days beats "
    "the MSCI World ETF EUNL.DE, and a 5th-95th percentile band for its 21-day EUR return "
    "as decimals (0.05 = +5 %). Answer with JSON only: "
    '{"forecasts": [{"ticker": ..., "p_beat": ..., "band_low": ..., "band_high": ...}]}.'
)

LlmCall = Callable[[Session, list[dict[str, str]]], str]
ClosesLoader = Callable[[Session, str, int], dict[str, float]]


def _default_llm_call(db: Session, messages: list[dict[str, str]]) -> str:
    from app.decision.advisor.llm_decision import _default_llm_call as decision_call

    return decision_call(db, messages, json_schema=SHADOW_JSON_SCHEMA, max_tokens=2048)


def _messages(strategy: AdvisorStrategy, candidates: list[dict[str, Any]], lessons: list[str]) -> list[dict[str, str]]:
    framing = (strategy.config_json or {}).get("prompt_framing")
    system = _SYSTEM + (f"\n\nYour style: {framing}" if framing else "")
    if lessons:
        system += "\n\nLessons from your own track record:\n" + "\n".join(f"- {x}" for x in lessons)
    lines = [f"- {c['symbol']}" + (f" ({c['name']})" if c.get("name") else "") for c in candidates]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "Tickers:\n" + "\n".join(lines)},
    ]


def parse_forecasts(raw: str, tickers: set[str]) -> dict[str, tuple[float, float, float]]:
    """``{ticker: (p_beat, band_low, band_high)}`` for the listed tickers only."""
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        payload = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return {}
    out: dict[str, tuple[float, float, float]] = {}
    for item in payload.get("forecasts") or []:
        try:
            ticker = str(item["ticker"]).strip().upper()
            p = float(item["p_beat"])
            lo, hi = float(item["band_low"]), float(item["band_high"])
        except (KeyError, TypeError, ValueError):
            continue
        if ticker not in tickers or not all(np.isfinite([p, lo, hi])):
            continue
        p = min(max(p, _P_CLIP[0]), _P_CLIP[1])
        out[ticker] = (p, min(lo, hi), max(lo, hi))
    return out


def run_shadow_forecasts(
    db: Session,
    user_id: str,
    strategies: list[AdvisorStrategy],
    *,
    llm_call: LlmCall | None = None,
    today: date | None = None,
) -> dict[str, int]:
    """Write today's shadow forecasts for each strategy; idempotent per day."""
    from app.decision.advisor.cycle import _latest_candidates
    from app.decision.advisor.reflection import get_active_lessons

    day = today or now_utc().date()
    candidates, _run_id, _stale = _latest_candidates(db, user_id)
    by_ticker = {str(c["symbol"]).upper(): c for c in candidates if c.get("symbol")}
    written: dict[str, int] = {}
    if not by_ticker:
        return written
    call = llm_call or _default_llm_call
    for strategy in strategies:
        exists = (
            db.query(AdvisorShadowForecast.id)
            .filter(AdvisorShadowForecast.strategy_id == strategy.id, AdvisorShadowForecast.forecast_date == day)
            .first()
        )
        if exists is not None:
            continue
        try:
            raw = call(db, _messages(strategy, list(by_ticker.values()), get_active_lessons(db, strategy.id)))
        except Exception:
            logger.warning("shadow forecasts: LLM call failed for strategy %s", strategy.id, exc_info=True)
            continue
        parsed = parse_forecasts(raw, set(by_ticker))
        n = 0
        for ticker, (p, lo, hi) in parsed.items():
            db.add(AdvisorShadowForecast(
                user_id=user_id, strategy_id=strategy.id, forecast_date=day, ticker=ticker,
                horizon_days=HORIZON_DAYS, p_beat=p, band_low=lo, band_high=hi,
            ))
            n += 1
        try:
            db.commit()
        except IntegrityError:  # a concurrent writer got there first
            db.rollback()
            n = 0
        written[strategy.id] = n
        if len(parsed) < len(by_ticker):
            logger.info(
                "shadow forecasts: strategy %s answered %d of %d tickers",
                strategy.id, len(parsed), len(by_ticker),
            )
    return written


def band_rps(low: float, high: float, realised: float) -> float:
    """RPS of the p5-p95 band over three outcomes: below, inside, above."""
    observed = (1.0 if realised < low else 0.0, 1.0 if realised <= high else 0.0)
    return float(sum((f - o) ** 2 for f, o in zip(_BAND_CUMULATIVE, observed)) / 2.0)


def _close_before(closes: dict[str, float], day: date, max_gap: int = 5) -> float | None:
    for back in range(1, max_gap + 2):
        value = closes.get((day - timedelta(days=back)).isoformat())
        if value and value > 0:
            return value
    return None


def _close_on_or_after(closes: dict[str, float], day: date, limit: date) -> float | None:
    d = day
    while d <= limit:
        value = closes.get(d.isoformat())
        if value and value > 0:
            return value
        d += timedelta(days=1)
    return None


def resolve_shadow_forecasts(
    db: Session,
    *,
    today: date | None = None,
    closes_loader: ClosesLoader | None = None,
) -> int:
    """Score every due shadow forecast against the benchmark; returns the count resolved."""
    from app.foundation.eur_prices import benchmark_ticker, eur_closes

    day = today or now_utc().date()
    pending = (
        db.query(AdvisorShadowForecast)
        .filter(AdvisorShadowForecast.resolved_at.is_(None))
        .all()
    )
    due = [
        r for r in pending
        if np.busday_offset(r.forecast_date, r.horizon_days or HORIZON_DAYS, roll="forward").astype(date) < day
    ]
    if not due:
        return 0
    loader = closes_loader or (lambda s, sym, n: eur_closes(s, sym, days=n))
    span = (day - min(r.forecast_date for r in due)).days + 15
    cache: dict[str, dict[str, float]] = {}

    def closes(symbol: str) -> dict[str, float]:
        if symbol not in cache:
            try:
                cache[symbol] = loader(db, symbol, span)
            except Exception:  # noqa: BLE001 - a missing series waits
                cache[symbol] = {}
        return cache[symbol]

    bench = benchmark_ticker(db)
    resolved = 0
    for row in due:
        exit_day = np.busday_offset(row.forecast_date, row.horizon_days or HORIZON_DAYS, roll="forward").astype(date)
        stock, ref = closes(row.ticker), closes(bench)
        s0, s1 = _close_before(stock, row.forecast_date), _close_on_or_after(stock, exit_day, day)
        b0, b1 = _close_before(ref, row.forecast_date), _close_on_or_after(ref, exit_day, day)
        if None in (s0, s1, b0, b1):
            if (day - exit_day).days > RESOLUTION_GRACE_DAYS:
                row.resolved_at = now_utc()  # unresolvable: no score, excluded from pairs
                resolved += 1
            continue
        assert s0 and s1 and b0 and b1
        realised = s1 / s0 - 1.0
        beat = realised > b1 / b0 - 1.0
        row.realised_return = realised
        row.beat = beat
        row.brier = (row.p_beat - (1.0 if beat else 0.0)) ** 2
        row.rps = band_rps(row.band_low, row.band_high, realised)
        row.resolved_at = now_utc()
        resolved += 1
    db.commit()
    return resolved
