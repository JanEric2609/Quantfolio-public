"""Nightly second-source check of stored prices (plan 2026-11-09).

The bar guard (``bar_guard.py``) catches what one series shows on its own:
splits, spikes, non-session days. It cannot see an error that looks like an
ordinary day — a dividend the provider never adjusted for, a 3:2 split on a
foreign listing, bars that stopped moving, a ×100 scale break inside a quiet
week. Those only show against a second provider, and almost every stored
series comes from yfinance (886 of 888 on prod, 2026-11-09).

What is compared: daily log returns over the last ``WINDOW_SESSIONS`` common
sessions, never raw levels — two providers' adjusted closes legitimately sit
on different bases. ``x_t = ln(stored_t / reference_t)`` is flat when they
agree; a jump that persists is a break, one that reverts is a spike. A
persistent jump is matched against the known error patterns.

Who is asked, within free limits (research note F, 2026-11-09):

* US listings: Alpaca, all of them, in a few multi-symbol calls.
* Everything else: EODHD's 20 free calls a day, rotating through the
  non-US series (18 a night) so each is seen about once a month.
* Tie-break for a US error: Tiingo (50 an hour), at most ``MAX_TIE_BREAKS``.

What is done: a row in ``price_cross_checks`` per symbol checked. An
``error`` pattern (unadjusted split, ×100 scale) that a second check confirms
— a tie-break agreeing, or a split event on record near the day — triggers
``DataIngester.repull_series``, which re-verifies and marks the series
``split_unadjusted`` if the problem survives. Another provider's bars are
never stored. Everything else is a warning for the Control Center.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.foundation.models.entities import PriceCrossCheck
from app.foundation.providers.utils import is_us_listing

logger = logging.getLogger(__name__)

WINDOW_SESSIONS = 15
#: Calendar days fetched to cover ``WINDOW_SESSIONS`` sessions plus holidays.
FETCH_DAYS = 30
MIN_COMMON = 6
#: One day's return may differ by this much (SIP close vs consolidated close).
TOL_US = 0.005
#: Looser for foreign listings and IEX: different closing auctions and feeds.
TOL_OTHER = 0.01
#: A persistent level step smaller than this is rounding.
MIN_STEP = 0.0015
#: A step up to this size that is not a split is a missed dividend.
MAX_DIVIDEND_STEP = 0.08
SPLIT_RATIOS = (1.5, 2, 3, 4, 5, 8, 10, 15, 20, 25, 50)
SPLIT_TOL = 0.04
EODHD_PER_NIGHT = 18
MAX_TIE_BREAKS = 30
ERROR_PATTERNS = frozenset({"split_unadjusted", "scale_x100"})


@dataclass
class Comparison:
    verdict: str  # ok | warn | error | no_reference
    pattern: str | None = None
    n_days: int = 0
    max_return_diff: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _classify_step(step: float) -> str:
    size = abs(step)
    if abs(size - math.log(100)) < SPLIT_TOL:
        return "scale_x100"
    if any(abs(size - math.log(r)) < SPLIT_TOL for r in SPLIT_RATIOS):
        return "split_unadjusted"
    if size <= MAX_DIVIDEND_STEP:
        return "dividend_mismatch"
    return "level_break"


def compare(
    stored: dict[str, float], reference: dict[str, float], *, tol: float, repulled_on: str | None = None,
) -> Comparison:
    """Compare two close series keyed by ISO date (pure).

    *repulled_on* is the day of the series' last full re-pull. Stored bars are
    written once and restated only by the weekly re-pull (``series_repull``),
    so a dividend whose ex-date falls after it legitimately shows as a
    dividend-sized step until the next re-pull: that is drift, not an error.
    """
    days = sorted(d for d in set(stored) & set(reference) if stored[d] > 0 and reference[d] > 0)
    days = days[-(WINDOW_SESSIONS + 1):]
    if len(days) < MIN_COMMON:
        return Comparison("no_reference", n_days=len(days), detail={"reason": "too few common sessions"})
    x = [math.log(stored[d] / reference[d]) for d in days]
    dx = [b - a for a, b in zip(x, x[1:])]
    max_diff = max(abs(v) for v in dx)
    result = Comparison("ok", n_days=len(days), max_return_diff=round(max_diff, 6))

    # Stale: the stored close did not move while the reference did.
    stale_run = best_run = 0
    for a, b in zip(days, days[1:]):
        ref_move = abs(math.log(reference[b] / reference[a]))
        if stored[b] == stored[a] and ref_move > 0.003:
            stale_run += 1
            best_run = max(best_run, stale_run)
        else:
            stale_run = 0
    if best_run >= 2:
        result.verdict, result.pattern = "warn", "stale"
        result.detail = {"stale_sessions": best_run}
        return result

    worst: tuple[str, float, str] | None = None  # (pattern, step, day)
    for i, step in enumerate(dx):
        if abs(step) <= tol:
            continue
        if i > 0 and abs(dx[i - 1]) > tol and abs(step + dx[i - 1]) <= tol:
            continue  # the way back from a one-day spike, already counted
        before = x[i]
        after = x[i + 1 :]
        persistent = all(abs(v - before) > tol for v in after[: 3]) if len(after) > 1 else True
        if not persistent:
            pattern = "spike"
        elif abs(step) < MIN_STEP:
            continue
        else:
            pattern = _classify_step(step)
        rank = {"scale_x100": 4, "split_unadjusted": 3, "level_break": 2, "dividend_mismatch": 1, "spike": 0}
        if worst is None or rank[pattern] > rank[worst[0]]:
            worst = (pattern, step, days[i + 1])
    if worst is not None:
        pattern, step, day = worst
        result.pattern = pattern
        result.verdict = "error" if pattern in ERROR_PATTERNS else "warn"
        result.detail = {"day": day, "step_log": round(step, 6), "factor": round(math.exp(step), 4)}
        if pattern == "dividend_mismatch" and repulled_on is not None and day > repulled_on:
            result.verdict = "ok"
            result.detail["note"] = "ex-date after the last full re-pull; the weekly re-pull restates it"
    return result


# ── data access ─────────────────────────────────────────────────────────────


def stored_closes(db: Session, symbol: str, start: date) -> dict[str, float]:
    rows = db.execute(
        text("SELECT ts, close FROM bar_prices WHERE symbol = :s AND ts >= :start ORDER BY ts"),
        {"s": symbol, "start": datetime.combine(start, datetime.min.time(), tzinfo=UTC)},
    ).fetchall()
    out: dict[str, float] = {}
    for ts, close in rows:
        if close is None:
            continue
        day = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
        out[day] = float(close)
    return out


def repull_days(db: Session) -> dict[str, str]:
    """``{symbol: ISO day of the last full re-pull}``."""
    rows = db.execute(text("SELECT symbol, last_full_repull_at FROM bar_series_meta")).fetchall()
    return {str(s): str(ts)[:10] for s, ts in rows if ts is not None}


def stored_symbols(db: Session) -> list[str]:
    rows = db.execute(text("SELECT symbol FROM bar_series_meta WHERE quarantine_reason IS NULL")).fetchall()
    return sorted(str(r[0]) for r in rows if r[0] and not str(r[0]).startswith("^"))


def _history_closes(registry: Any, symbol: str, provider: str, start: date) -> dict[str, float]:
    res = registry.get_price_history(symbol, start=start.isoformat(), provider=provider)
    if not res.get("ok"):
        return {}
    out: dict[str, float] = {}
    for row in res.get("data") or []:
        day, close = row.get("date"), row.get("close")
        if day is None or close is None:
            continue
        out[str(day)[:10]] = float(close)
    return out


def rotation(symbols: list[str], today: date, per_night: int = EODHD_PER_NIGHT) -> list[str]:
    """The slice of *symbols* due tonight; every symbol comes round every ceil(n/per_night) nights."""
    if not symbols:
        return []
    n = len(symbols)
    start = (today.toordinal() * per_night) % n
    return [symbols[(start + i) % n] for i in range(min(per_night, n))]


def _record(db: Session, symbol: str, today: date, reference: str, cmp: Comparison, action: str = "none") -> None:
    row = (
        db.query(PriceCrossCheck)
        .filter(PriceCrossCheck.symbol == symbol, PriceCrossCheck.checked_on == today,
                PriceCrossCheck.reference == reference)
        .one_or_none()
    )
    if row is None:
        row = PriceCrossCheck(symbol=symbol, checked_on=today, reference=reference)
        db.add(row)
    row.n_days = cmp.n_days
    row.max_return_diff = cmp.max_return_diff
    row.verdict = cmp.verdict
    row.pattern = cmp.pattern
    row.action = action
    row.detail_json = cmp.detail


def _split_on_record(ingester: Any, symbol: str, day: str | None) -> bool:
    if not day:
        return False
    try:
        events = ingester.split_events(symbol) or []
    except Exception:  # noqa: BLE001 - the split lookup is a hint, not a requirement
        return False
    target = date.fromisoformat(day)
    return any(abs((ts.date() - target).days) <= 5 for ts, _ratio in events)


def run_cross_check(db: Session, *, today: date | None = None, registry: Any = None, ingester: Any = None) -> dict[str, Any]:
    """Compare tonight's stored series with a second source; act on confirmed errors."""
    from app.foundation.data_backbone.ingest import DataIngester
    from app.foundation.providers.registry import build_provider_registry

    today = today or datetime.now(UTC).date()
    ingester = ingester or DataIngester(db)
    registry = registry or getattr(ingester, "registry", None) or build_provider_registry(db)
    start = today - timedelta(days=FETCH_DAYS)
    symbols = stored_symbols(db)
    repulled = repull_days(db)
    us = [s for s in symbols if is_us_listing(s)]
    other = [s for s in symbols if not is_us_listing(s)]
    stats: dict[str, Any] = {"us": len(us), "other_due": 0, "ok": 0, "warn": 0, "error": 0,
                             "no_reference": 0, "repulled": [], "feed": None}

    # US: Alpaca, everything at once.
    alpaca = next((p for p in getattr(registry, "providers", []) if p.name == "alpaca"), None)
    reference: dict[str, dict[str, float]] = {}
    if alpaca is not None and us:
        for feed in ("sip", "iex"):
            try:
                reference = alpaca.get_closes_multi(us, start.isoformat(), feed=feed)
                stats["feed"] = feed
                break
            except Exception as exc:  # noqa: BLE001 - SIP history may be refused on the free plan
                logger.info("cross_check: alpaca %s feed failed: %s", feed, exc)
    tie_breaks = 0
    for sym in us:
        ref = reference.get(sym) or {}
        tol = TOL_US if stats["feed"] == "sip" else TOL_OTHER
        cmp = (
            compare(stored_closes(db, sym, start), ref, tol=tol, repulled_on=repulled.get(sym))
            if ref else Comparison("no_reference")
        )
        action = "none"
        if cmp.verdict == "error":
            confirmed = _split_on_record(ingester, sym, cmp.detail.get("day"))
            if not confirmed and tie_breaks < MAX_TIE_BREAKS:
                tie_breaks += 1
                second = compare(
                    stored_closes(db, sym, start), _history_closes(registry, sym, "tiingo", start),
                    tol=TOL_US, repulled_on=repulled.get(sym),
                )
                confirmed = second.pattern == cmp.pattern
                cmp.detail["tie_break"] = {"provider": "tiingo", "pattern": second.pattern, "verdict": second.verdict}
            action = _act(ingester, sym, confirmed, stats)
        _record(db, sym, today, "alpaca", cmp, action)
        stats[cmp.verdict] += 1

    # Everything else: EODHD's free calls, rotating.
    due = rotation(other, today)
    stats["other_due"] = len(due)
    for sym in due:
        ref = _history_closes(registry, sym, "eod", start)
        cmp = (
            compare(stored_closes(db, sym, start), ref, tol=TOL_OTHER, repulled_on=repulled.get(sym))
            if ref else Comparison("no_reference")
        )
        action = "none"
        if cmp.verdict == "error":
            action = _act(ingester, sym, _split_on_record(ingester, sym, cmp.detail.get("day")), stats)
        _record(db, sym, today, "eod", cmp, action)
        stats[cmp.verdict] += 1
    db.commit()
    try:
        stats["twelvedata_probe"] = probe_twelvedata(db, registry)
    except Exception as exc:  # noqa: BLE001 - the probe is a one-off, never a reason to fail the check
        logger.info("cross_check: Twelve Data probe failed: %s", exc)
    logger.info("cross_check %s: %s", today, stats)
    return stats


#: SPY's close of 2023-12-29: 475.31 raw; adjusted for the dividends since it
#: sits near 465-470. A close under this is adjusted.
_PROBE_SYMBOL, _PROBE_DAY, _PROBE_ADJUSTED_BELOW = "SPY", "2023-12-29", 472.0


def probe_twelvedata(db: Session, registry: Any) -> dict[str, Any] | None:
    """Run the Twelve Data adjusted-close acceptance test once and store the verdict.

    Returns the stored probe, or ``None`` when Twelve Data is not configured.
    A failed call is not stored, so the probe runs again the next night.
    """
    from app.foundation.models.entities import AppSetting
    from app.foundation.providers.registry import TWELVEDATA_PROBE_KEY, invalidate_registry_cache
    from app.foundation.settings import upsert_public_settings

    if db.query(AppSetting).filter(AppSetting.key == TWELVEDATA_PROBE_KEY).one_or_none() is not None:
        return None
    if not any(p.name == "twelvedata" for p in getattr(registry, "providers", [])):
        return None
    res = registry.get_price_history(_PROBE_SYMBOL, start="2023-12-27", end="2024-01-03", provider="twelvedata")
    closes = {str(r.get("date"))[:10]: r.get("close") for r in (res.get("data") or []) if res.get("ok")}
    close = closes.get(_PROBE_DAY)
    if close is None:
        logger.info("cross_check: Twelve Data probe got no %s close: %s", _PROBE_DAY, res.get("error"))
        return None
    probe = {
        "verified": float(close) < _PROBE_ADJUSTED_BELOW,
        "close": float(close),
        "raw_close": 475.31,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    upsert_public_settings(db, {TWELVEDATA_PROBE_KEY: probe})
    invalidate_registry_cache()
    logger.info("cross_check: Twelve Data adjusted probe %s", probe)
    return probe


def _act(ingester: Any, symbol: str, confirmed: bool, stats: dict[str, Any]) -> str:
    if not confirmed:
        return "tie_break_disagreed"
    try:
        ingester.repull_series(symbol)
    except Exception as exc:  # noqa: BLE001 - a failed repull leaves the series as it was
        logger.warning("cross_check: repull of %s failed: %s", symbol, exc)
        return "repull_failed"
    stats["repulled"].append(symbol)
    return "repulled"
